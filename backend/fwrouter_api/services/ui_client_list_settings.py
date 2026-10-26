"""Field owner and conditional persistence for client-list visibility."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
from typing import Any

from fwrouter_api.db.connection import connect


SETTINGS_KEY = "ui.admin_client_display.v1"
CONTRACT_KEY = "_show_inactive_contract"
CONTRACT_VERSION = 1
MAX_REVISION = (1 << 63) - 1
SQLITE_BUSY_TIMEOUT_MS = 3000
_INCARNATION_RE = re.compile(r"^[0-9a-f]{32}$")


class ClientListSettingsError(RuntimeError):
    def __init__(self, code: str, status_code: int, *, retryable: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code
        self.retryable = retryable


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def parse_display_row(raw: str | None) -> dict[str, Any]:
    if raw is None:
        return {}
    try:
        value = json.loads(raw, object_pairs_hook=_strict_object)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ClientListSettingsError("CLIENT_LIST_CONFIGURATION_CONFLICT", 409) from exc
    if not isinstance(value, dict):
        raise ClientListSettingsError("CLIENT_LIST_CONFIGURATION_CONFLICT", 409)
    return value


def _validated_contract(value: object) -> tuple[str, int] | None:
    if value is None:
        return None
    if (not isinstance(value, dict) or set(value) != {"version", "incarnation", "revision"}
            or type(value.get("version")) is not int or value["version"] != CONTRACT_VERSION
            or not isinstance(value.get("incarnation"), str)
            or not _INCARNATION_RE.fullmatch(value["incarnation"])
            or type(value.get("revision")) is not int or not 1 <= value["revision"] <= MAX_REVISION):
        raise ClientListSettingsError("CLIENT_LIST_CONFIGURATION_CONFLICT", 409)
    return value["incarnation"], value["revision"]


def _etag(*, raw: str | None, contract: tuple[str, int] | None, effective: bool) -> str:
    if contract is None:
        state = "absent" if raw is None else "legacy:" + raw
    else:
        state = f"managed:{contract[0]}:{contract[1]}:{int(effective)}"
    digest = hashlib.sha256(("fwrouter-client-list-v1\0" + state).encode("utf-8")).hexdigest()
    return f'"cli1-{digest}"'


def _decode(raw: str | None) -> dict[str, Any]:
    row = parse_display_row(raw)
    if CONTRACT_KEY in row and row[CONTRACT_KEY] is None:
        raise ClientListSettingsError("CLIENT_LIST_CONFIGURATION_CONFLICT", 409)
    contract = _validated_contract(row.get(CONTRACT_KEY))
    value = row.get("show_inactive", False)
    if contract is not None and type(value) is not bool:
        raise ClientListSettingsError("CLIENT_LIST_CONFIGURATION_CONFLICT", 409)
    effective = value if type(value) is bool else bool(value)
    if contract is not None:
        status, provenance = "current", "managed"
    elif "show_inactive" not in row:
        status, provenance = "current", "default"
    elif type(value) is bool:
        status, provenance = "adoption_required", "legacy"
    else:
        status, provenance = "adoption_required", "legacy_coercion"
    return {
        "row": row,
        "show_inactive": effective,
        "etag": _etag(raw=raw, contract=contract, effective=effective),
        "compatibility_status": status,
        "provenance": provenance,
        "incarnation": contract[0] if contract else None,
        "revision": contract[1] if contract else None,
    }


def _read_raw(connection: sqlite3.Connection) -> str | None:
    row = connection.execute("SELECT value_json FROM settings WHERE key=?", (SETTINGS_KEY,)).fetchone()
    return None if row is None else str(row["value_json"])


def read_client_list_settings() -> dict[str, Any]:
    connection = connect()
    try:
        return _decode(_read_raw(connection))
    finally:
        connection.close()


def _write_row(connection: sqlite3.Connection, row: dict[str, Any]) -> None:
    connection.execute(
        """INSERT INTO settings(key,value_json,updated_at) VALUES(?,?,CURRENT_TIMESTAMP)
           ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at""",
        (SETTINGS_KEY, json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))),
    )


def _commit_display_delta(
    connection: sqlite3.Connection,
    delta: dict[str, Any],
    *,
    protected_value: bool | None = None,
    expected_etag: str | None = None,
) -> dict[str, Any]:
    raw = _read_raw(connection)
    current = _decode(raw)
    if protected_value is not None:
        if expected_etag is None:
            raise ClientListSettingsError("CLIENT_LIST_PRECONDITION_REQUIRED", 428)
        if not hmac.compare_digest(current["etag"], expected_etag):
            raise ClientListSettingsError("CLIENT_LIST_REVISION_STALE", 412)
    row = dict(current["row"])
    row.update(delta)
    protected_changed = False
    if protected_value is not None:
        old_contract = _validated_contract(row.get(CONTRACT_KEY))
        old_value = current["show_inactive"]
        if old_contract is None:
            protected_changed = True
            row[CONTRACT_KEY] = {"version": CONTRACT_VERSION, "incarnation": secrets.token_hex(16), "revision": 1}
        elif protected_value != old_value:
            protected_changed = True
            if old_contract[1] == MAX_REVISION:
                raise ClientListSettingsError("CLIENT_LIST_REVISION_EXHAUSTED", 409)
            row[CONTRACT_KEY] = {"version": CONTRACT_VERSION, "incarnation": old_contract[0],
                                 "revision": old_contract[1] + 1}
        row["show_inactive"] = protected_value
    else:
        protected_changed = False
    changed = row != current["row"]
    if changed:
        _write_row(connection, row)
    raw_written = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {**_decode(raw_written), "changed": changed, "protected_changed": protected_changed}


def _require_grant() -> None:
    from fwrouter_api.services.presentation_client_list_admission import (
        PresentationAdmissionError, require_presentation_write_grant,
    )
    try:
        require_presentation_write_grant()
    except PresentationAdmissionError as exc:
        raise ClientListSettingsError(exc.code, exc.status_code) from exc


def _run_write(delta: dict[str, Any], *, protected_value: bool | None = None,
               expected_etag: str | None = None) -> dict[str, Any]:
    connection = connect()
    try:
        connection.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("BEGIN IMMEDIATE")
        # A copied request context can queue behind another writer after its
        # owning request has completed. Revalidate the revocable grant only
        # after acquiring SQLite's writer reservation and before touching the
        # row, so queued work cannot outlive request admission.
        if protected_value is not None:
            _require_grant()
        result = _commit_display_delta(connection, delta, protected_value=protected_value,
                                       expected_etag=expected_etag)
        connection.commit()
    except sqlite3.OperationalError as exc:
        connection.rollback()
        if "locked" in str(exc).lower() or "busy" in str(exc).lower():
            raise ClientListSettingsError("CLIENT_LIST_WRITER_BUSY", 503, retryable=True) from exc
        raise
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    if result["changed"]:
        from fwrouter_api.services.live_probe_cache import clear_live_probe_cache_matching
        clear_live_probe_cache_matching(
            lambda key: key == "ui_state.settings_workspace"
            or key.startswith("ui_state.settings_workspace.")
        )
    result["persistence_status"] = "persisted" if result["changed"] else "unchanged"
    result["audit_delivery_status"] = "not_attempted"
    if result["protected_changed"]:
        try:
            from fwrouter_api.services.logs import write_operational_log
            write_operational_log(
                event_type="ui_client_list_visibility_updated",
                message="Client-list visibility preference updated.",
                details={
                    "field": "show_inactive",
                    "incarnation": result["incarnation"],
                    "revision": result["revision"],
                },
            )
            result["audit_delivery_status"] = "delivered"
        except Exception:
            result["audit_delivery_status"] = "unconfirmed"
    return result


def patch_show_inactive(value: object, expected_etag: str | None) -> dict[str, Any]:
    if type(value) is not bool:
        raise ClientListSettingsError("CLIENT_LIST_BOOLEAN_REQUIRED", 422)
    _require_grant()
    return _run_write({}, protected_value=value, expected_etag=expected_etag)


def save_display_settings_delta(
    incoming: dict[str, Any],
    *,
    update_fields: set[str],
    protected_value: bool | None = None,
    expected_etag: str | None = None,
) -> dict[str, Any]:
    """Merge only fields supplied by an admitted display writer into the latest row."""
    if protected_value is not None:
        if type(protected_value) is not bool:
            raise ClientListSettingsError("CLIENT_LIST_BOOLEAN_REQUIRED", 422)
        _require_grant()
    delta = {key: incoming[key] for key in update_fields if key in incoming
             and key not in {"show_inactive", CONTRACT_KEY}}
    return _run_write(delta, protected_value=protected_value, expected_etag=expected_etag)


def merge_external_visibility_delta(delta: dict[str, bool | None]) -> None:
    connection = connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = _decode(_read_raw(connection))
        row = dict(current["row"])
        visibility = row.get("system_visibility")
        visibility = dict(visibility) if isinstance(visibility, dict) else {}
        for key, value in delta.items():
            if value is None:
                visibility.pop(key, None)
            elif type(value) is bool:
                visibility.setdefault(key, value)
        if visibility:
            row["system_visibility"] = visibility
        else:
            row.pop("system_visibility", None)
        _write_row(connection, row)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    from fwrouter_api.services.live_probe_cache import clear_live_probe_cache_matching
    clear_live_probe_cache_matching(
        lambda key: key == "ui_state.settings_workspace"
        or key.startswith("ui_state.settings_workspace.")
    )


def validate_imported_display_settings(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str | None]:
    """Validate the selected field and discard imported authority before side effects."""
    selected: dict[str, Any] | None = None
    selected_count = 0
    output: list[dict[str, Any]] = []
    for raw_row in rows:
        row = dict(raw_row)
        if str(row.get("key") or "") != SETTINGS_KEY:
            output.append(row)
            continue
        selected_count += 1
        if selected_count > 1:
            raise ClientListSettingsError("CLIENT_LIST_IMPORT_SETTINGS_DUPLICATE", 422)
        value = row.get("value")
        if "value" not in row or not isinstance(value, dict):
            raise ClientListSettingsError("CLIENT_LIST_IMPORT_SETTINGS_INVALID", 422)
        selected = dict(value)
        if CONTRACT_KEY in selected:
            imported_contract = selected.pop(CONTRACT_KEY)
            if imported_contract is None or _validated_contract(imported_contract) is None:
                raise ClientListSettingsError("CLIENT_LIST_IMPORT_METADATA_INVALID", 422)
        if type(selected.get("show_inactive", False)) is not bool:
            raise ClientListSettingsError("CLIENT_LIST_IMPORT_BOOLEAN_INVALID", 422)
        row["value"] = selected
        output.append(row)
    if selected is None:
        selected = {"show_inactive": False}
    return output, json.dumps(selected, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def rebase_imported_display_settings(connection: sqlite3.Connection, value_json: str | None) -> None:
    """Install a fresh local incarnation inside the importer's existing transaction."""
    if value_json is None:
        value = {"show_inactive": False}
    else:
        value = parse_display_row(value_json)
        if CONTRACT_KEY in value:
            imported_contract = value.pop(CONTRACT_KEY)
            if imported_contract is None or _validated_contract(imported_contract) is None:
                raise ClientListSettingsError("CLIENT_LIST_IMPORT_METADATA_INVALID", 422)
        if type(value.get("show_inactive", False)) is not bool:
            raise ClientListSettingsError("CLIENT_LIST_IMPORT_BOOLEAN_INVALID", 422)
    value[CONTRACT_KEY] = {"version": CONTRACT_VERSION, "incarnation": secrets.token_hex(16), "revision": 1}
    _write_row(connection, value)
