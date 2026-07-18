from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import db_session, initialize_database
from fwrouter_api.services.artifacts import atomic_write_json, atomic_write_text
from fwrouter_api.services.core_bypass import BYPASS_SETTINGS_KEY
from fwrouter_api.services.dataplane_status import build_runtime_enforcement_state
from fwrouter_api.services.logs import write_operational_log
from fwrouter_api.services.rules import get_effective_rules, get_manual_rules_texts, get_rules_state, list_rules_metadata
from fwrouter_api.services.runtime import get_scoped_egress_runtime_summary
from fwrouter_api.services.scoped_egress import (
    build_scoped_egress_diagnostics,
    build_scoped_egress_readiness,
    summarize_scoped_subjects,
)
from fwrouter_api.services.subscription import get_subscription_state
from fwrouter_api.services.subject_policy import enrich_subject_with_effective_state
from fwrouter_api.services.system_summary import build_system_summary
from fwrouter_api.services.subjects import get_subject, list_subjects


CONTROL_PLANE_SNAPSHOT_VERSION = "2026-05-14.control-plane-transfer.v2"
TRANSFER_DIRNAME = "transfer"
CONTROL_PLANE_TABLES = (
    "settings",
    "modules",
    "subjects",
    "subject_lan",
    "subject_tailscale",
    "subject_xray",
    "subject_docker",
    "subject_host",
    "subject_fwrouter",
    "servers",
    "server_custom_https_proxy",
    "server_preferences",
    "server_ping_state",
    "routing_global_state",
    "subject_server_overrides",
    "subject_user_overrides",
    "subscription_state",
    "rules_state",
    "rules_metadata",
)


def _utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _fetch_rows(query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with db_session() as connection:
        rows = connection.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def _fetch_one(query: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    with db_session() as connection:
        row = connection.execute(query, params).fetchone()
    return dict(row) if row else None


def _json_loads_or_none(value: str | None) -> Any:
    if not value:
        return None
    return json.loads(value)


def _detail_table_for_subject_type(subject_type: str) -> str | None:
    mapping = {
        "lan": "subject_lan",
        "tailscale": "subject_tailscale",
        "tailscale_node": "subject_tailscale",
        "xray": "subject_xray",
        "docker": "subject_docker",
        "host": "subject_host",
        "fwrouter": "subject_fwrouter",
    }
    return mapping.get(subject_type)


def _export_subjects() -> list[dict[str, Any]]:
    exported: list[dict[str, Any]] = []
    for summary in list_subjects(include_deleted=True, limit=1000):
        subject = get_subject(str(summary["subject_id"])) or summary
        exported.append(subject)
    return exported


def _redact_subscription_state(
    state: dict[str, Any],
    *,
    include_secrets: bool,
) -> tuple[dict[str, Any], list[str]]:
    warnings: list[str] = []
    redacted = dict(state)
    if not include_secrets and redacted.get("url"):
        redacted["url"] = None
        redacted["url_redacted"] = True
        warnings.append("subscription_url_redacted")
    else:
        redacted["url_redacted"] = False
    return redacted, warnings


def _redact_custom_https_proxy_rows(
    rows: list[dict[str, Any]],
    *,
    include_secrets: bool,
) -> tuple[list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    exported: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        if include_secrets:
            item["credentials_redacted"] = False
        else:
            if item.get("username") or item.get("password"):
                warnings.append("custom_server_credentials_redacted")
            item["username"] = None
            item["password"] = None
            item["credentials_redacted"] = True
        exported.append(item)
    deduped_warnings = sorted(set(warnings))
    return exported, deduped_warnings


def _export_settings_rows() -> list[dict[str, Any]]:
    rows = _fetch_rows(
        """
        SELECT key, value_json, updated_at
        FROM settings
        ORDER BY key
        """
    )
    return [
        {
            "key": row["key"],
            "value": _json_loads_or_none(row["value_json"]),
            "updated_at": row["updated_at"],
        }
        for row in rows
    ]


def _export_rules_bundle() -> dict[str, Any]:
    texts = get_manual_rules_texts()
    effective = get_effective_rules()
    return {
        "state": get_rules_state(),
        "metadata_rows": list_rules_metadata(),
        "content": {
            "manual_draft_text": texts["draft_text"],
            "manual_active_text": texts["active_text"],
            "static_direct_text": texts["static_direct_text"],
            "big_direct_text": texts["big_direct_text"],
            "big_vpn_text": texts["big_vpn_text"],
            "effective_json": effective["effective"],
            "effective_text": effective["effective_text"],
            "metadata_json": effective["metadata"],
        },
    }


def _transfer_dir() -> Path:
    path = get_settings().paths.state_dir / TRANSFER_DIRNAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def _snapshot_file_path() -> Path:
    return _transfer_dir() / f"control-plane-snapshot.{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"


def _parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def _resolve_transfer_snapshot_path(file_path: str) -> Path:
    candidate = Path(file_path)
    if not candidate.is_absolute():
        candidate = _transfer_dir() / candidate
    resolved = candidate.resolve(strict=False)
    transfer_root = _transfer_dir().resolve(strict=False)
    if not resolved.is_relative_to(transfer_root):
        raise ValueError("Snapshot file path must stay inside the transfer directory.")
    return resolved


def _load_snapshot_file(path: Path) -> dict[str, Any]:
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError("Snapshot file must contain a JSON object.")
    return loaded


def resolve_control_plane_snapshot_source(
    *,
    snapshot: dict[str, Any] | None = None,
    file_path: str | None = None,
) -> dict[str, Any]:
    if isinstance(snapshot, dict) and snapshot:
        return {
            "ok": True,
            "snapshot": snapshot,
            "source": {
                "kind": "payload",
                "file_path": None,
            },
        }

    if not file_path:
        return {
            "ok": False,
            "error": {
                "code": "CONTROL_PLANE_SNAPSHOT_SOURCE_REQUIRED",
                "message": "Provide either snapshot payload or file_path.",
            },
        }

    try:
        resolved = _resolve_transfer_snapshot_path(file_path)
    except ValueError as exc:
        return {
            "ok": False,
            "error": {
                "code": "CONTROL_PLANE_SNAPSHOT_FILE_PATH_INVALID",
                "message": str(exc),
            },
        }

    if not resolved.exists() or not resolved.is_file():
        return {
            "ok": False,
            "error": {
                "code": "CONTROL_PLANE_SNAPSHOT_FILE_NOT_FOUND",
                "message": f"Snapshot file was not found: {resolved}",
            },
        }

    try:
        loaded = _load_snapshot_file(resolved)
    except json.JSONDecodeError as exc:
        return {
            "ok": False,
            "error": {
                "code": "CONTROL_PLANE_SNAPSHOT_FILE_INVALID_JSON",
                "message": f"Snapshot file is not valid JSON: {exc}",
            },
        }
    except ValueError as exc:
        return {
            "ok": False,
            "error": {
                "code": "CONTROL_PLANE_SNAPSHOT_FILE_INVALID",
                "message": str(exc),
            },
        }

    return {
        "ok": True,
        "snapshot": loaded,
        "source": {
            "kind": "file",
            "file_path": str(resolved),
            "file_name": resolved.name,
            "size_bytes": resolved.stat().st_size,
            "modified_at": datetime.fromtimestamp(resolved.stat().st_mtime, tz=UTC)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z"),
        },
    }


def list_control_plane_snapshot_files() -> dict[str, Any]:
    initialize_database()
    snapshots: list[dict[str, Any]] = []
    for path in sorted(_transfer_dir().glob("control-plane-snapshot.*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
        item: dict[str, Any] = {
            "file_name": path.name,
            "file_path": str(path),
            "size_bytes": path.stat().st_size,
            "modified_at": datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z"),
        }
        try:
            snapshot = _load_snapshot_file(path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            item["read_error"] = str(exc)
            snapshots.append(item)
            continue
        state = _state_from_snapshot(snapshot)
        item.update(
            {
                "snapshot_version": snapshot.get("snapshot_version"),
                "exported_at": snapshot.get("exported_at"),
                "subjects_count": len(state.get("subjects") or []),
                "servers_count": len(state.get("servers") or []),
                "include_secrets": bool(
                    ((snapshot.get("export_options") if isinstance(snapshot.get("export_options"), dict) else {}) or {}).get("include_secrets")
                ),
                "warnings_count": len(snapshot.get("warnings") or []),
            }
        )
        snapshots.append(item)
    return {
        "snapshots": snapshots,
        "transfer_dir": str(_transfer_dir()),
    }


def export_control_plane_snapshot(*, include_secrets: bool = False, write_file: bool = True) -> dict[str, Any]:
    initialize_database()
    subscription_state, redaction_warnings = _redact_subscription_state(
        get_subscription_state(),
        include_secrets=include_secrets,
    )
    custom_https_proxy_rows, custom_server_redaction_warnings = _redact_custom_https_proxy_rows(
        _fetch_rows(
            """
            SELECT
                server_id,
                proxy_type,
                host,
                port,
                username,
                password,
                tls,
                sni,
                skip_cert_verify,
                path,
                updated_at
            FROM server_custom_https_proxy
            ORDER BY server_id
            """
        ),
        include_secrets=include_secrets,
    )

    snapshot = {
        "snapshot_version": CONTROL_PLANE_SNAPSHOT_VERSION,
        "exported_at": _utc_now_iso(),
        "app_version": get_settings().app_version,
        "export_options": {
            "include_secrets": include_secrets,
        },
        "tables": list(CONTROL_PLANE_TABLES),
        "state": {
            "settings": _export_settings_rows(),
            "modules": _fetch_rows(
                """
                SELECT
                    module_name,
                    desired_state,
                    runtime_state,
                    apply_state,
                    status_text,
                    error_code,
                    error_message,
                    updated_at
                FROM modules
                ORDER BY module_name
                """
            ),
            "routing_global_state": _fetch_one(
                """
                SELECT
                    id,
                    desired_mode,
                    applied_mode,
                    selective_default,
                    server_mode,
                    desired_fixed_server_id,
                    applied_fixed_server_id,
                    fixed_server_until,
                    active_auto_server_id,
                    apply_state,
                    error_code,
                    error_message,
                    updated_at
                FROM routing_global_state
                WHERE id = 1
                """
            ),
            "subjects": _export_subjects(),
            "subject_user_overrides": _fetch_rows(
                """
                SELECT
                    subject_id,
                    override_mode,
                    override_until,
                    created_by,
                    updated_at
                FROM subject_user_overrides
                ORDER BY subject_id
                """
            ),
            "subject_server_overrides": _fetch_rows(
                """
                SELECT
                    subject_id,
                    selected_server_id,
                    selected_until,
                    apply_state,
                    error_code,
                    error_message,
                    updated_at
                FROM subject_server_overrides
                ORDER BY subject_id
                """
            ),
            "servers": _fetch_rows(
                """
                SELECT
                    server_id,
                    server_name,
                    provider_name,
                    country_code,
                    region,
                    raw_json,
                    inventory_state,
                    first_seen_at,
                    last_seen_at,
                    missing_since,
                    updated_at
                FROM servers
                ORDER BY server_name, server_id
                """
            ),
            "server_custom_https_proxy": custom_https_proxy_rows,
            "server_preferences": _fetch_rows(
                """
                SELECT
                    server_id,
                    vpn_auto,
                    global_list,
                    remembered_until,
                    manually_deleted_at,
                    updated_at
                FROM server_preferences
                ORDER BY server_id
                """
            ),
            "server_ping_state": _fetch_rows(
                """
                SELECT
                    server_id,
                    status,
                    last_ping_ms,
                    checked_at,
                    checked_by,
                    error_code,
                    error_message,
                    metadata_json
                FROM server_ping_state
                ORDER BY server_id
                """
            ),
            "subscription_state": subscription_state,
            "rules": _export_rules_bundle(),
        },
        "warnings": redaction_warnings + custom_server_redaction_warnings + [
            "inventory_rows_are_cached_snapshot_only",
            "runtime_apply_required_after_import",
        ],
    }

    file_path = None
    if write_file:
        file_path = _snapshot_file_path()
        atomic_write_json(file_path, snapshot)

    return {
        "snapshot": snapshot,
        "file_path": str(file_path) if file_path else None,
    }


def _state_from_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    state = snapshot.get("state")
    return state if isinstance(state, dict) else {}


def validate_control_plane_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    state = _state_from_snapshot(snapshot)
    warnings: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    if snapshot.get("snapshot_version") != CONTROL_PLANE_SNAPSHOT_VERSION:
        errors.append(
            {
                "code": "SNAPSHOT_VERSION_UNSUPPORTED",
                "message": "Snapshot version is not supported by this backend.",
            }
        )

    subjects = state.get("subjects")
    if not isinstance(subjects, list):
        errors.append(
            {
                "code": "SNAPSHOT_SUBJECTS_INVALID",
                "message": "Snapshot subjects section must be a list.",
            }
        )
        subjects = []

    servers = state.get("servers")
    if not isinstance(servers, list):
        errors.append(
            {
                "code": "SNAPSHOT_SERVERS_INVALID",
                "message": "Snapshot servers section must be a list.",
            }
        )
        servers = []

    subscription_state = state.get("subscription_state")
    if isinstance(subscription_state, dict) and bool(subscription_state.get("url_redacted")):
        warnings.append(
            {
                "code": "SNAPSHOT_SUBSCRIPTION_URL_REDACTED",
                "message": "Subscription URL is redacted; import will not restore it.",
            }
        )

    custom_https_proxy_rows = state.get("server_custom_https_proxy")
    if custom_https_proxy_rows is not None and not isinstance(custom_https_proxy_rows, list):
        errors.append(
            {
                "code": "SNAPSHOT_CUSTOM_SERVERS_INVALID",
                "message": "Snapshot custom HTTPS proxy section must be a list.",
            }
        )
        custom_https_proxy_rows = []
    elif not isinstance(custom_https_proxy_rows, list):
        custom_https_proxy_rows = []

    redacted_custom_servers = sum(
        1
        for row in custom_https_proxy_rows
        if isinstance(row, dict) and bool(row.get("credentials_redacted"))
    )
    if redacted_custom_servers:
        warnings.append(
            {
                "code": "SNAPSHOT_CUSTOM_SERVER_CREDENTIALS_REDACTED",
                "message": "Some custom HTTPS proxy credentials are redacted; import will not restore them.",
                "count": redacted_custom_servers,
            }
        )

    unresolved_details = 0
    for subject in subjects:
        if not isinstance(subject, dict):
            continue
        subject_type = str(subject.get("subject_type") or "")
        if _detail_table_for_subject_type(subject_type) and not isinstance(subject.get("detail"), dict):
            unresolved_details += 1
    if unresolved_details:
        warnings.append(
            {
                "code": "SNAPSHOT_SUBJECT_DETAILS_PARTIAL",
                "message": "Some subject detail rows are missing in the snapshot.",
                "count": unresolved_details,
            }
        )

    routing = state.get("routing_global_state")
    if isinstance(routing, dict) and str(routing.get("desired_mode") or "") == "vpn" and not servers:
        warnings.append(
            {
                "code": "SNAPSHOT_VPN_WITHOUT_SERVERS",
                "message": "Routing desired_mode is vpn, but no server inventory rows are present.",
            }
        )

    rules = state.get("rules")
    if not isinstance(rules, dict):
        errors.append(
            {
                "code": "SNAPSHOT_RULES_INVALID",
                "message": "Snapshot rules section must be an object.",
            }
        )
    else:
        content = rules.get("content")
        if not isinstance(content, dict):
            errors.append(
                {
                    "code": "SNAPSHOT_RULES_CONTENT_INVALID",
                    "message": "Snapshot rules content section must be an object.",
                }
            )

    return {
        "ok": not errors,
        "snapshot_version": snapshot.get("snapshot_version"),
        "errors": errors,
        "warnings": warnings,
        "summary": {
            "subjects_count": len(subjects),
            "servers_count": len(servers),
            "modules_count": len(state.get("modules") or []),
            "settings_count": len(state.get("settings") or []),
        },
        "defaults": {
            "normalize_runtime_state": True,
        },
    }


def _snapshot_bypass_state(settings_rows: list[dict[str, Any]]) -> dict[str, Any]:
    state = {
        "enabled": False,
        "updated_at": None,
        "updated_by": None,
        "reason": None,
        "previous_runtime": None,
    }
    for row in settings_rows:
        if str(row.get("key") or "") != BYPASS_SETTINGS_KEY:
            continue
        value = row.get("value")
        if isinstance(value, dict):
            state.update(value)
        break
    return state


def _snapshot_active_override(row: dict[str, Any], *, until_field: str, value_field: str) -> dict[str, Any] | None:
    if row.get(value_field) in {None, ""}:
        return None
    until_value = _parse_datetime(row.get(until_field))
    if until_value is not None and until_value <= datetime.now(UTC):
        return None
    return dict(row)


def _snapshot_routing(state: dict[str, Any]) -> dict[str, Any]:
    routing = state.get("routing_global_state")
    if isinstance(routing, dict):
        return dict(routing)
    return {
        "desired_mode": "direct",
        "applied_mode": None,
        "selective_default": "direct",
        "server_mode": "auto",
        "desired_fixed_server_id": None,
        "applied_fixed_server_id": None,
        "fixed_server_until": None,
        "active_auto_server_id": None,
        "apply_state": "pending",
        "error_code": None,
        "error_message": None,
        "updated_at": None,
    }


def _enriched_subjects_from_snapshot(state: dict[str, Any]) -> dict[str, Any]:
    runtime_enforcement = build_runtime_enforcement_state()
    routing = _snapshot_routing(state)
    settings_rows = [dict(row) for row in (state.get("settings") or []) if isinstance(row, dict)]
    bypass = _snapshot_bypass_state(settings_rows)
    user_override_rows = {
        str(row["subject_id"]): active
        for row in (state.get("subject_user_overrides") or [])
        if isinstance(row, dict)
        if (active := _snapshot_active_override(row, until_field="override_until", value_field="override_mode")) is not None
    }
    server_override_rows = {
        str(row["subject_id"]): active
        for row in (state.get("subject_server_overrides") or [])
        if isinstance(row, dict)
        if (
            active := _snapshot_active_override(
                row,
                until_field="selected_until",
                value_field="selected_server_id",
            )
        )
        is not None
    }

    subjects = [
        enrich_subject_with_effective_state(
            dict(subject),
            routing=routing,
            user_override=user_override_rows.get(str(subject.get("subject_id") or "")),
            server_override=server_override_rows.get(str(subject.get("subject_id") or "")),
            runtime_enforcement=runtime_enforcement,
            bypass_state=bypass,
        )
        for subject in (state.get("subjects") or [])
        if isinstance(subject, dict)
    ]

    scoped_summary = summarize_scoped_subjects(subjects)
    scoped_diagnostics = build_scoped_egress_diagnostics(
        summary=scoped_summary,
        runtime_enforcement=runtime_enforcement,
        bypass=bypass,
    )
    scoped_readiness = build_scoped_egress_readiness(
        diagnostics=scoped_diagnostics,
        runtime_enforcement=runtime_enforcement,
        bypass=bypass,
    )
    return {
        "subjects": subjects,
        "runtime_enforcement": runtime_enforcement,
        "bypass": bypass,
        "routing": routing,
        "scoped_egress": {
            "diagnostics": scoped_diagnostics,
            "readiness": scoped_readiness,
        },
    }


def plan_control_plane_import(
    snapshot: dict[str, Any],
    *,
    normalize_runtime_state: bool = True,
) -> dict[str, Any]:
    initialize_database()
    validation = validate_control_plane_snapshot(snapshot)
    state = _state_from_snapshot(snapshot)
    simulated = _enriched_subjects_from_snapshot(state)
    settings_rows = [dict(row) for row in (state.get("settings") or []) if isinstance(row, dict)]
    modules = [dict(row) for row in (state.get("modules") or []) if isinstance(row, dict)]
    subject_server_overrides = [
        dict(row) for row in (state.get("subject_server_overrides") or []) if isinstance(row, dict)
    ]
    subscription_state = dict(state.get("subscription_state") or {})
    rules_snapshot = state.get("rules") if isinstance(state.get("rules"), dict) else {}
    rules_content = rules_snapshot.get("content") if isinstance(rules_snapshot.get("content"), dict) else {}
    current_rules_paths = get_manual_rules_texts()

    enabled_modules = sum(1 for row in modules if str(row.get("desired_state") or "") == "enabled")
    subject_pending_apply_count = len([subject for subject in simulated["subjects"] if bool(subject.get("is_active"))])
    override_pending_apply_count = len(
        [
            row
            for row in subject_server_overrides
            if _snapshot_active_override(row, until_field="selected_until", value_field="selected_server_id") is not None
        ]
    )
    warnings = list(validation.get("warnings") or [])
    if normalize_runtime_state:
        warnings.append(
            {
                "code": "IMPORT_RUNTIME_NORMALIZATION_ENABLED",
                "message": "Import will reset runtime/apply state and require a fresh Linux-side apply.",
            }
        )

    return {
        "ok": validation["ok"],
        "validation": validation,
        "normalize_runtime_state": normalize_runtime_state,
        "import_actions": {
            "replace_tables": list(CONTROL_PLANE_TABLES),
            "restore_rules_files": [
                {
                    "name": "manual_draft_text",
                    "path": str(current_rules_paths["draft_path"]),
                    "has_content": bool(rules_content.get("manual_draft_text")),
                },
                {
                    "name": "manual_active_text",
                    "path": str(current_rules_paths["active_path"]),
                    "has_content": bool(rules_content.get("manual_active_text")),
                },
                {
                    "name": "effective_json",
                    "path": str(current_rules_paths["effective_json_path"]),
                    "has_content": isinstance(rules_content.get("effective_json"), dict),
                },
            ],
        },
        "post_import_expectations": {
            "routing_apply_required": normalize_runtime_state and bool(state.get("routing_global_state")),
            "modules_pending_apply_count": enabled_modules if normalize_runtime_state else 0,
            "subjects_pending_apply_count": subject_pending_apply_count if normalize_runtime_state else 0,
            "server_override_reapply_count": override_pending_apply_count if normalize_runtime_state else 0,
            "subscription_url_present": bool(subscription_state.get("url")),
            "subscription_url_redacted": bool(subscription_state.get("url_redacted")),
            "core_bypass_enabled": bool(simulated["bypass"].get("enabled")),
        },
        "scoped_egress": simulated["scoped_egress"],
        "runtime_enforcement": simulated["runtime_enforcement"],
        "warnings": warnings,
        "summary": {
            **(validation.get("summary") if isinstance(validation.get("summary"), dict) else {}),
            "active_subjects_count": sum(1 for subject in simulated["subjects"] if bool(subject.get("is_active"))),
            "tracked_scoped_subjects_count": len(
                simulated["scoped_egress"]["diagnostics"].get("bindings") or []
            ),
        },
    }


def _insert_rows(connection, query: str, rows: list[tuple[Any, ...]]) -> None:  # noqa: ANN001
    if rows:
        connection.executemany(query, rows)


def _write_rules_files_from_snapshot(rules_snapshot: dict[str, Any]) -> dict[str, str]:
    current = get_manual_rules_texts()
    content = rules_snapshot.get("content") if isinstance(rules_snapshot.get("content"), dict) else {}
    effective_json = content.get("effective_json")
    metadata_json = content.get("metadata_json")

    atomic_write_text(current["draft_path"], str(content.get("manual_draft_text") or ""))
    atomic_write_text(current["active_path"], str(content.get("manual_active_text") or ""))
    atomic_write_text(current["static_direct_path"], str(content.get("static_direct_text") or ""))
    atomic_write_text(current["big_direct_path"], str(content.get("big_direct_text") or ""))
    atomic_write_text(current["big_vpn_path"], str(content.get("big_vpn_text") or ""))
    atomic_write_text(current["effective_text_path"], str(content.get("effective_text") or ""))
    atomic_write_json(current["effective_json_path"], effective_json if isinstance(effective_json, dict) else {})
    atomic_write_json(current["metadata_path"], metadata_json if isinstance(metadata_json, dict) else {})

    return {
        "manual_draft_path": str(current["draft_path"]),
        "manual_active_path": str(current["active_path"]),
        "static_direct_path": str(current["static_direct_path"]),
        "big_direct_path": str(current["big_direct_path"]),
        "big_vpn_path": str(current["big_vpn_path"]),
        "effective_json_path": str(current["effective_json_path"]),
        "effective_text_path": str(current["effective_text_path"]),
        "metadata_path": str(current["metadata_path"]),
    }


def _normalized_module_row(row: dict[str, Any], *, normalize_runtime_state: bool) -> dict[str, Any]:
    if not normalize_runtime_state:
        return dict(row)
    desired_state = str(row.get("desired_state") or "disabled")
    return {
        **row,
        "runtime_state": "not_configured",
        "apply_state": "pending" if desired_state == "enabled" else "clean",
        "status_text": "Imported from control-plane snapshot. Runtime apply/verify is required.",
        "error_code": None,
        "error_message": None,
    }


def _normalized_routing_row(row: dict[str, Any] | None, *, normalize_runtime_state: bool) -> dict[str, Any] | None:
    if row is None or not normalize_runtime_state:
        return row
    return {
        **row,
        "applied_mode": None,
        "applied_fixed_server_id": None,
        "apply_state": "pending",
        "error_code": None,
        "error_message": None,
    }


def _normalized_subject_row(row: dict[str, Any], *, normalize_runtime_state: bool) -> dict[str, Any]:
    normalized = dict(row)
    subject_id = str(normalized.get("subject_id") or "")
    subject_type = str(normalized.get("subject_type") or "")

    if subject_id == "fwrouter:global" or subject_type == "fwrouter":
        normalized["desired_mode"] = "direct"
        normalized["applied_mode"] = None if normalize_runtime_state else "direct"
        normalized["apply_state"] = "pending" if normalize_runtime_state else "clean"
        return normalized

    if not normalize_runtime_state:
        return normalized
    return {
        **normalized,
        "applied_mode": None,
        "apply_state": "pending",
    }


def _normalized_subject_server_override(row: dict[str, Any], *, normalize_runtime_state: bool) -> dict[str, Any]:
    subject_id = str(row.get("subject_id") or "")
    if subject_id == "fwrouter:global":
        return {}
    if not normalize_runtime_state:
        return dict(row)
    return {
        **row,
        "apply_state": "pending",
        "error_code": None,
        "error_message": None,
    }


def _normalized_subscription_state(row: dict[str, Any], *, normalize_runtime_state: bool) -> dict[str, Any]:
    if not normalize_runtime_state:
        return dict(row)
    normalized = dict(row)
    normalized["status"] = "idle" if normalized.get("url") else "not_configured"
    normalized["error_code"] = None
    normalized["error_message"] = None
    return normalized


def _normalized_rules_state(
    row: dict[str, Any],
    *,
    normalize_runtime_state: bool,
    file_paths: dict[str, str],
) -> dict[str, Any]:
    normalized = {
        **row,
        **file_paths,
    }
    if not normalize_runtime_state:
        return normalized
    normalized.update(
        {
            "last_apply_job_id": None,
            "last_update_job_id": None,
            "status": "pending",
            "error_code": None,
            "error_message": None,
        }
    )
    return normalized


def import_control_plane_snapshot(
    snapshot: dict[str, Any],
    *,
    normalize_runtime_state: bool = True,
) -> dict[str, Any]:
    initialize_database()
    validation = validate_control_plane_snapshot(snapshot)
    if not validation["ok"]:
        return {
            "ok": False,
            "validation": validation,
            "imported": False,
        }

    state = _state_from_snapshot(snapshot)
    rules_snapshot = state.get("rules") if isinstance(state.get("rules"), dict) else {}
    rules_file_paths = _write_rules_files_from_snapshot(rules_snapshot)

    modules = [
        _normalized_module_row(dict(row), normalize_runtime_state=normalize_runtime_state)
        for row in (state.get("modules") or [])
        if isinstance(row, dict)
    ]
    routing = _normalized_routing_row(
        state.get("routing_global_state") if isinstance(state.get("routing_global_state"), dict) else None,
        normalize_runtime_state=normalize_runtime_state,
    )
    subjects = [
        _normalized_subject_row(dict(row), normalize_runtime_state=normalize_runtime_state)
        for row in (state.get("subjects") or [])
        if isinstance(row, dict)
    ]
    subject_user_overrides = [
        dict(row) for row in (state.get("subject_user_overrides") or []) if isinstance(row, dict)
    ]
    subject_server_overrides = [
        _normalized_subject_server_override(dict(row), normalize_runtime_state=normalize_runtime_state)
        for row in (state.get("subject_server_overrides") or [])
        if isinstance(row, dict)
    ]
    subject_server_overrides = [
        row
        for row in subject_server_overrides
        if isinstance(row, dict) and str(row.get("subject_id") or "").strip()
    ]
    settings_rows = [dict(row) for row in (state.get("settings") or []) if isinstance(row, dict)]
    servers = [dict(row) for row in (state.get("servers") or []) if isinstance(row, dict)]
    custom_https_proxy_rows = [
        dict(row) for row in (state.get("server_custom_https_proxy") or []) if isinstance(row, dict)
    ]
    server_preferences = [
        dict(row) for row in (state.get("server_preferences") or []) if isinstance(row, dict)
    ]
    server_ping_state = [
        dict(row) for row in (state.get("server_ping_state") or []) if isinstance(row, dict)
    ]
    subscription_state = _normalized_subscription_state(
        dict(state.get("subscription_state") or {}),
        normalize_runtime_state=normalize_runtime_state,
    )
    rules_state = _normalized_rules_state(
        dict((rules_snapshot.get("state") if isinstance(rules_snapshot.get("state"), dict) else get_rules_state())),
        normalize_runtime_state=normalize_runtime_state,
        file_paths=rules_file_paths,
    )
    rules_metadata_rows = [
        dict(row) for row in (rules_snapshot.get("metadata_rows") or []) if isinstance(row, dict)
    ]

    with db_session() as connection:
        for table in (
            "subject_server_overrides",
            "subject_user_overrides",
            "subject_lan",
            "subject_tailscale",
            "subject_xray",
            "subject_docker",
            "subject_host",
            "subject_fwrouter",
            "routing_global_state",
            "server_ping_state",
            "server_preferences",
            "server_custom_https_proxy",
            "subjects",
            "servers",
            "subscription_state",
            "rules_metadata",
            "rules_state",
            "modules",
            "settings",
        ):
            connection.execute(f"DELETE FROM {table}")

        _insert_rows(
            connection,
            """
            INSERT INTO settings (key, value_json, updated_at)
            VALUES (?, ?, COALESCE(?, CURRENT_TIMESTAMP))
            """,
            [
                (
                    row["key"],
                    json.dumps(row.get("value"), ensure_ascii=False, sort_keys=True),
                    row.get("updated_at"),
                )
                for row in settings_rows
            ],
        )
        _insert_rows(
            connection,
            """
            INSERT INTO modules (
                module_name,
                desired_state,
                runtime_state,
                apply_state,
                status_text,
                error_code,
                error_message,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP))
            """,
            [
                (
                    row["module_name"],
                    row["desired_state"],
                    row["runtime_state"],
                    row["apply_state"],
                    row.get("status_text"),
                    row.get("error_code"),
                    row.get("error_message"),
                    row.get("updated_at"),
                )
                for row in modules
            ],
        )
        _insert_rows(
            connection,
            """
            INSERT INTO subjects (
                subject_id,
                subject_type,
                stable_key,
                display_name,
                alias,
                desired_mode,
                applied_mode,
                apply_state,
                runtime_state,
                is_active,
                is_deleted,
                first_seen_at,
                last_seen_at,
                last_traffic_at,
                inactive_since,
                deleted_at,
                metadata_json,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP))
            """,
            [
                (
                    row["subject_id"],
                    row["stored_subject_type"] if row.get("stored_subject_type") else row["subject_type"],
                    row["stable_key"],
                    row.get("display_name"),
                    row.get("alias"),
                    row["desired_mode"],
                    row.get("applied_mode"),
                    row["apply_state"],
                    row["runtime_state"],
                    1 if row.get("is_active") else 0,
                    1 if row.get("is_deleted") else 0,
                    row.get("first_seen_at"),
                    row.get("last_seen_at"),
                    row.get("last_traffic_at"),
                    row.get("inactive_since"),
                    row.get("deleted_at"),
                    json.dumps(row.get("metadata"), ensure_ascii=False, sort_keys=True)
                    if row.get("metadata") is not None
                    else None,
                    row.get("created_at"),
                    row.get("updated_at"),
                )
                for row in subjects
            ],
        )

        subject_detail_rows: dict[str, list[dict[str, Any]]] = {
            "subject_lan": [],
            "subject_tailscale": [],
            "subject_xray": [],
            "subject_docker": [],
            "subject_host": [],
            "subject_fwrouter": [],
        }
        for subject in subjects:
            detail = subject.get("detail")
            if not isinstance(detail, dict):
                continue
            table = _detail_table_for_subject_type(str(subject["subject_type"]))
            if table is None:
                continue
            subject_detail_rows[table].append({"subject_id": subject["subject_id"], **detail})

        _insert_rows(
            connection,
            """
            INSERT INTO subject_lan (
                subject_id,
                mac_address,
                ip_address,
                hostname,
                dhcp_hostname,
                source_json,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP))
            """,
            [
                (
                    row["subject_id"],
                    row.get("mac_address"),
                    row.get("ip_address"),
                    row.get("hostname"),
                    row.get("dhcp_hostname"),
                    json.dumps(row.get("source"), ensure_ascii=False, sort_keys=True)
                    if row.get("source") is not None
                    else None,
                    row.get("updated_at"),
                )
                for row in subject_detail_rows["subject_lan"]
            ],
        )
        _insert_rows(
            connection,
            """
            INSERT INTO subject_tailscale (
                subject_id,
                node_id,
                tailscale_ip,
                hostname,
                user_name,
                online,
                source_json,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP))
            """,
            [
                (
                    row["subject_id"],
                    row.get("node_id"),
                    row.get("tailscale_ip"),
                    row.get("hostname"),
                    row.get("user_name"),
                    1 if row.get("online") else 0,
                    json.dumps(row.get("source"), ensure_ascii=False, sort_keys=True)
                    if row.get("source") is not None
                    else None,
                    row.get("updated_at"),
                )
                for row in subject_detail_rows["subject_tailscale"]
            ],
        )
        _insert_rows(
            connection,
            """
            INSERT INTO subject_xray (
                subject_id,
                client_id,
                client_uuid,
                email,
                subscription_path,
                last_subscription_at,
                enabled,
                source_json,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP))
            """,
            [
                (
                    row["subject_id"],
                    row.get("client_id"),
                    row.get("client_uuid"),
                    row.get("email"),
                    row.get("subscription_path"),
                    row.get("last_subscription_at"),
                    1 if row.get("enabled", 1) else 0,
                    json.dumps(row.get("source"), ensure_ascii=False, sort_keys=True)
                    if row.get("source") is not None
                    else None,
                    row.get("updated_at"),
                )
                for row in subject_detail_rows["subject_xray"]
            ],
        )
        _insert_rows(
            connection,
            """
            INSERT INTO subject_docker (
                subject_id,
                compose_project,
                compose_service,
                container_name,
                container_id,
                image_name,
                ip_address,
                network_name,
                source_json,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP))
            """,
            [
                (
                    row["subject_id"],
                    row.get("compose_project"),
                    row.get("compose_service"),
                    row.get("container_name"),
                    row.get("container_id"),
                    row.get("image_name"),
                    row.get("ip_address"),
                    row.get("network_name"),
                    json.dumps(row.get("source"), ensure_ascii=False, sort_keys=True)
                    if row.get("source") is not None
                    else None,
                    row.get("updated_at"),
                )
                for row in subject_detail_rows["subject_docker"]
            ],
        )
        _insert_rows(
            connection,
            """
            INSERT INTO subject_host (
                subject_id,
                systemd_unit,
                listen_proto,
                listen_port,
                executable,
                process_name,
                source_json,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP))
            """,
            [
                (
                    row["subject_id"],
                    row.get("systemd_unit"),
                    row.get("listen_proto"),
                    row.get("listen_port"),
                    row.get("executable"),
                    row.get("process_name"),
                    json.dumps(row.get("source"), ensure_ascii=False, sort_keys=True)
                    if row.get("source") is not None
                    else None,
                    row.get("updated_at"),
                )
                for row in subject_detail_rows["subject_host"]
            ],
        )
        _insert_rows(
            connection,
            """
            INSERT INTO subject_fwrouter (
                subject_id,
                component_name,
                source_json,
                updated_at
            )
            VALUES (?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP))
            """,
            [
                (
                    row["subject_id"],
                    row.get("component_name"),
                    json.dumps(row.get("source"), ensure_ascii=False, sort_keys=True)
                    if row.get("source") is not None
                    else None,
                    row.get("updated_at"),
                )
                for row in subject_detail_rows["subject_fwrouter"]
            ],
        )
        _insert_rows(
            connection,
            """
            INSERT INTO servers (
                server_id,
                server_name,
                provider_name,
                country_code,
                region,
                raw_json,
                inventory_state,
                first_seen_at,
                last_seen_at,
                missing_since,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP))
            """,
            [
                (
                    row["server_id"],
                    row["server_name"],
                    row.get("provider_name"),
                    row.get("country_code"),
                    row.get("region"),
                    row.get("raw_json"),
                    row["inventory_state"],
                    row.get("first_seen_at"),
                    row.get("last_seen_at"),
                    row.get("missing_since"),
                    row.get("updated_at"),
                )
                for row in servers
            ],
        )
        _insert_rows(
            connection,
            """
            INSERT INTO server_custom_https_proxy (
                server_id,
                proxy_type,
                host,
                port,
                username,
                password,
                tls,
                sni,
                skip_cert_verify,
                path,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP))
            """,
            [
                (
                    row["server_id"],
                    row.get("proxy_type", "http"),
                    row["host"],
                    row["port"],
                    row.get("username"),
                    row.get("password"),
                    row.get("tls", 1),
                    row.get("sni"),
                    row.get("skip_cert_verify", 0),
                    row.get("path"),
                    row.get("updated_at"),
                )
                for row in custom_https_proxy_rows
            ],
        )
        if routing is not None:
            connection.execute(
                """
                INSERT INTO routing_global_state (
                    id,
                    desired_mode,
                    applied_mode,
                    selective_default,
                    server_mode,
                    desired_fixed_server_id,
                    applied_fixed_server_id,
                    fixed_server_until,
                    active_auto_server_id,
                    apply_state,
                    error_code,
                    error_message,
                    updated_at
                )
                VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP))
                """,
                (
                    routing["desired_mode"],
                    routing.get("applied_mode"),
                    routing["selective_default"],
                    routing["server_mode"],
                    routing.get("desired_fixed_server_id"),
                    routing.get("applied_fixed_server_id"),
                    routing.get("fixed_server_until"),
                    routing.get("active_auto_server_id"),
                    routing["apply_state"],
                    routing.get("error_code"),
                    routing.get("error_message"),
                    routing.get("updated_at"),
                ),
            )
        _insert_rows(
            connection,
            """
            INSERT INTO server_preferences (
                server_id,
                vpn_auto,
                global_list,
                remembered_until,
                manually_deleted_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP))
            """,
            [
                (
                    row["server_id"],
                    row.get("vpn_auto", 0),
                    row.get("global_list", 1),
                    row.get("remembered_until"),
                    row.get("manually_deleted_at"),
                    row.get("updated_at"),
                )
                for row in server_preferences
            ],
        )
        _insert_rows(
            connection,
            """
            INSERT INTO server_ping_state (
                server_id,
                status,
                last_ping_ms,
                checked_at,
                checked_by,
                error_code,
                error_message,
                metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    row["server_id"],
                    row.get("status", "unknown"),
                    row.get("last_ping_ms"),
                    row.get("checked_at"),
                    row.get("checked_by"),
                    row.get("error_code"),
                    row.get("error_message"),
                    row.get("metadata_json"),
                )
                for row in server_ping_state
            ],
        )
        _insert_rows(
            connection,
            """
            INSERT INTO subject_user_overrides (
                subject_id,
                override_mode,
                override_until,
                created_by,
                updated_at
            )
            VALUES (?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP))
            """,
            [
                (
                    row["subject_id"],
                    row.get("override_mode"),
                    row.get("override_until"),
                    row.get("created_by"),
                    row.get("updated_at"),
                )
                for row in subject_user_overrides
            ],
        )
        _insert_rows(
            connection,
            """
            INSERT INTO subject_server_overrides (
                subject_id,
                selected_server_id,
                selected_until,
                apply_state,
                error_code,
                error_message,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP))
            """,
            [
                (
                    row["subject_id"],
                    row.get("selected_server_id"),
                    row.get("selected_until"),
                    row.get("apply_state", "clean"),
                    row.get("error_code"),
                    row.get("error_message"),
                    row.get("updated_at"),
                )
                for row in subject_server_overrides
            ],
        )

        if subscription_state:
            connection.execute(
                """
                INSERT INTO subscription_state (
                    id,
                    url,
                    status,
                    last_refresh_at,
                    last_success_at,
                    server_inventory_updated_at,
                    error_code,
                    error_message,
                    metadata_json,
                    updated_at
                )
                VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP))
                """,
                (
                    subscription_state.get("url"),
                    subscription_state.get("status", "not_configured"),
                    subscription_state.get("last_refresh_at"),
                    subscription_state.get("last_success_at"),
                    subscription_state.get("server_inventory_updated_at"),
                    subscription_state.get("error_code"),
                    subscription_state.get("error_message"),
                    json.dumps(subscription_state.get("metadata"), ensure_ascii=False, sort_keys=True)
                    if subscription_state.get("metadata") is not None
                    else None,
                    subscription_state.get("updated_at"),
                ),
            )

        connection.execute(
            """
            INSERT INTO rules_state (
                id,
                manual_draft_path,
                manual_active_path,
                static_direct_path,
                big_direct_path,
                big_vpn_path,
                effective_json_path,
                effective_text_path,
                metadata_path,
                selective_default,
                last_apply_job_id,
                last_update_job_id,
                status,
                last_success_at,
                last_failed_at,
                error_code,
                error_message,
                updated_at
            )
            VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP))
            """,
            (
                rules_state["manual_draft_path"],
                rules_state["manual_active_path"],
                rules_state["static_direct_path"],
                rules_state["big_direct_path"],
                rules_state["big_vpn_path"],
                rules_state["effective_json_path"],
                rules_state["effective_text_path"],
                rules_state["metadata_path"],
                rules_state["selective_default"],
                rules_state.get("last_apply_job_id"),
                rules_state.get("last_update_job_id"),
                rules_state["status"],
                rules_state.get("last_success_at"),
                rules_state.get("last_failed_at"),
                rules_state.get("error_code"),
                rules_state.get("error_message"),
                rules_state.get("updated_at"),
            ),
        )
        _insert_rows(
            connection,
            """
            INSERT INTO rules_metadata (
                ruleset_id,
                ruleset_type,
                version_name,
                source_url,
                active_path,
                downloaded_at,
                activated_at,
                status,
                last_success_at,
                last_failed_at,
                last_error_code,
                last_error_message,
                last_job_id,
                metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    row["ruleset_id"],
                    row["ruleset_type"],
                    row.get("version_name"),
                    row.get("source_url"),
                    row.get("active_path"),
                    row.get("downloaded_at"),
                    row.get("activated_at"),
                    row.get("status"),
                    row.get("last_success_at"),
                    row.get("last_failed_at"),
                    row.get("last_error_code"),
                    row.get("last_error_message"),
                    row.get("last_job_id"),
                    json.dumps(row.get("metadata_json"), ensure_ascii=False, sort_keys=True)
                    if row.get("metadata_json") is not None
                    else None,
                )
                for row in rules_metadata_rows
            ],
        )

    scoped_egress = get_scoped_egress_runtime_summary()
    system_summary = build_system_summary()
    write_operational_log(
        event_type="control_plane_snapshot_imported",
        message="Control-plane snapshot imported into local backend state.",
        details={
            "normalize_runtime_state": normalize_runtime_state,
            "subjects_count": len(subjects),
            "servers_count": len(servers),
        },
    )
    return {
        "ok": True,
        "validation": validation,
        "imported": True,
        "normalize_runtime_state": normalize_runtime_state,
        "summary": {
            "subjects_count": len(subjects),
            "servers_count": len(servers),
            "modules_count": len(modules),
            "settings_count": len(settings_rows),
        },
        "rules_files": rules_file_paths,
        "post_import": {
            "scoped_egress": scoped_egress,
            "system_summary": system_summary,
        },
    }
