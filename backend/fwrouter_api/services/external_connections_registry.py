from __future__ import annotations

import json
from typing import Any

from fwrouter_api.db.connection import db_session
from fwrouter_api.services.live_probe_cache import clear_live_probe_cache
from fwrouter_api.services.ui_display_settings_common import (
    _json_dumps,
    _json_loads,
    _normalize_custom_external_systems,
    _slugify_system_id,
)


def _row_to_connection(row: Any) -> dict[str, Any]:
    payload = _json_loads(row["value_json"]) or {}
    normalized = _normalize_custom_external_systems([{**payload, "system_id": row["system_id"]}])
    item = normalized[0] if normalized else {}
    item["connection_id"] = str(row["connection_id"])
    item["system_id"] = str(row["system_id"])
    item["label"] = str(row["label"] or item.get("label") or row["system_id"])
    item["connection_type"] = str(
        row["connection_type"] or item.get("connection_type") or "external_management"
    )
    item["runtime_type"] = str(row["runtime_type"] or item.get("runtime_type") or "")
    item["replacement_target"] = str(
        row["replacement_target"] or item.get("replacement_target") or ""
    )
    item["location"] = str(row["location"] or item.get("location") or "manual")
    item["address"] = str(row["address"] or item.get("address") or "")
    item["integration_mode"] = str(
        row["integration_mode"] or item.get("integration_mode") or "api_push"
    )
    item["refresh_mode"] = str(row["refresh_mode"] or item.get("refresh_mode") or "on_change")
    item["enabled"] = bool(row["enabled"])
    item["created_at"] = row["created_at"]
    item["updated_at"] = row["updated_at"]
    item["last_seen_at"] = row["last_seen_at"]
    item["custom"] = True
    return item


def list_external_connections(*, enabled_only: bool = False) -> list[dict[str, Any]]:
    where = "WHERE enabled = 1" if enabled_only else ""
    with db_session() as connection:
        rows = connection.execute(
            f"""
            SELECT *
            FROM external_connections
            {where}
            ORDER BY created_at, connection_id
            """
        ).fetchall()
    return [_row_to_connection(row) for row in rows]


def get_external_connection(connection_id_or_system_id: str) -> dict[str, Any] | None:
    normalized = _slugify_system_id(connection_id_or_system_id)
    if not normalized:
        return None
    with db_session() as connection:
        row = connection.execute(
            """
            SELECT *
            FROM external_connections
            WHERE connection_id = ? OR system_id = ?
            """,
            (normalized, normalized),
        ).fetchone()
    return _row_to_connection(row) if row else None


def _connection_conflict_target(item: dict[str, Any]) -> str:
    if str(item.get("connection_type") or "") != "external_vpn_module":
        return ""
    target = str(item.get("replacement_target") or "").strip().lower()
    if target:
        return target
    endpoints = item.get("endpoints") if isinstance(item.get("endpoints"), dict) else {}
    if endpoints.get("tcp_redir_port") or endpoints.get("udp_tproxy_port"):
        return "mihomo"
    return ""


def _validate_external_vpn_conflict(item: dict[str, Any]) -> None:
    target = _connection_conflict_target(item)
    if not target:
        return
    connection_id = str(item.get("connection_id") or item.get("system_id") or "")
    for existing in list_external_connections(enabled_only=True):
        if str(existing.get("connection_id") or existing.get("system_id")) == connection_id:
            continue
        if _connection_conflict_target(existing) == target:
            from fwrouter_api.services.ui_display_settings_common import ExternalConnectionValidationError

            raise ExternalConnectionValidationError(
                "EXTERNAL_VPN_MODULE_TARGET_CONFLICT",
                "Only one active external VPN module can own one replacement target.",
                {"replacement_target": "conflict"},
            )


def upsert_external_connection_record(item: dict[str, Any]) -> dict[str, Any]:
    normalized = _normalize_custom_external_systems([item])
    if not normalized:
        from fwrouter_api.services.ui_display_settings_common import ExternalConnectionValidationError

        raise ExternalConnectionValidationError(
            "INVALID_EXTERNAL_CONNECTION",
            "External connection payload failed validation.",
            {"system_id": "required"},
        )
    stored = dict(normalized[0])
    connection_id = _slugify_system_id(item.get("connection_id") or stored.get("system_id"))
    stored["connection_id"] = connection_id
    stored["system_id"] = _slugify_system_id(stored.get("system_id") or connection_id)
    stored["enabled"] = item.get("enabled", True) is not False
    _validate_external_vpn_conflict(stored)
    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO external_connections (
                connection_id, system_id, label, connection_type, runtime_type,
                replacement_target, location, address, integration_mode, refresh_mode,
                enabled, value_json, created_at, updated_at, last_seen_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, ?)
            ON CONFLICT(connection_id) DO UPDATE SET
                system_id = excluded.system_id,
                label = excluded.label,
                connection_type = excluded.connection_type,
                runtime_type = excluded.runtime_type,
                replacement_target = excluded.replacement_target,
                location = excluded.location,
                address = excluded.address,
                integration_mode = excluded.integration_mode,
                refresh_mode = excluded.refresh_mode,
                enabled = excluded.enabled,
                value_json = excluded.value_json,
                updated_at = CURRENT_TIMESTAMP,
                last_seen_at = COALESCE(excluded.last_seen_at, external_connections.last_seen_at)
            """,
            (
                connection_id,
                stored["system_id"],
                stored["label"],
                stored["connection_type"],
                stored["runtime_type"],
                stored["replacement_target"],
                stored["location"],
                stored["address"],
                stored["integration_mode"],
                stored["refresh_mode"],
                1 if stored["enabled"] else 0,
                _json_dumps(stored),
                item.get("last_seen_at"),
            ),
        )
        connection.execute(
            """
            INSERT INTO external_connection_generated_state (connection_id, state_json, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(connection_id) DO UPDATE SET
                state_json = excluded.state_json,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                connection_id,
                _json_dumps(
                    {
                        "connection_id": connection_id,
                        "system_id": stored["system_id"],
                        "connection_type": stored["connection_type"],
                        "runtime_type": stored["runtime_type"],
                        "replacement_target": stored["replacement_target"],
                        "integration_mode": stored["integration_mode"],
                        "refresh_mode": stored["refresh_mode"],
                        "collector": f"external_connection:{connection_id}",
                    }
                ),
            ),
        )
    clear_live_probe_cache()
    return get_external_connection(connection_id) or stored


def delete_external_connection_record(connection_id_or_system_id: str) -> bool:
    normalized = _slugify_system_id(connection_id_or_system_id)
    if not normalized:
        return False
    with db_session() as connection:
        deleted = connection.execute(
            """
            DELETE FROM external_connections
            WHERE connection_id = ? OR system_id = ?
            """,
            (normalized, normalized),
        ).rowcount
    clear_live_probe_cache()
    return bool(deleted)


def mark_external_connection_seen(
    connection_id_or_system_id: str,
    *,
    details: dict[str, Any] | None = None,
) -> None:
    normalized = _slugify_system_id(connection_id_or_system_id)
    if not normalized:
        return
    with db_session() as connection:
        row = connection.execute(
            """
            SELECT value_json
            FROM external_connections
            WHERE connection_id = ? OR system_id = ?
            """,
            (normalized, normalized),
        ).fetchone()
        if row is None:
            return
        payload = _json_loads(row["value_json"]) or {}
        if details:
            payload["last_event"] = dict(details)
        connection.execute(
            """
            UPDATE external_connections
            SET value_json = ?, last_seen_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
            WHERE connection_id = ? OR system_id = ?
            """,
            (
                json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                normalized,
                normalized,
            ),
        )
    clear_live_probe_cache()


def get_external_connection_generated_state(
    connection_id_or_system_id: str,
) -> dict[str, Any] | None:
    connection = get_external_connection(connection_id_or_system_id)
    if not connection:
        return None
    with db_session() as db:
        row = db.execute(
            """
            SELECT state_json, updated_at
            FROM external_connection_generated_state
            WHERE connection_id = ?
            """,
            (connection["connection_id"],),
        ).fetchone()
    if row is None:
        return None
    state = _json_loads(row["state_json"]) or {}
    state["updated_at"] = row["updated_at"]
    return state


def upsert_external_connection_generated_state(
    connection_id_or_system_id: str,
    state: dict[str, Any],
) -> dict[str, Any]:
    connection = get_external_connection(connection_id_or_system_id)
    if not connection:
        from fwrouter_api.services.ui_display_settings_common import ExternalConnectionValidationError

        raise ExternalConnectionValidationError(
            "EXTERNAL_CONNECTION_NOT_FOUND",
            "External connection is not registered.",
            {"connection_id": "not_found"},
        )
    payload = dict(state)
    payload["connection_id"] = connection["connection_id"]
    with db_session() as db:
        db.execute(
            """
            INSERT INTO external_connection_generated_state (connection_id, state_json, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(connection_id) DO UPDATE SET
                state_json = excluded.state_json,
                updated_at = CURRENT_TIMESTAMP
            """,
            (connection["connection_id"], _json_dumps(payload)),
        )
    clear_live_probe_cache()
    return get_external_connection_generated_state(connection["connection_id"]) or payload
