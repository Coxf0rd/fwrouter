from __future__ import annotations

import json
from typing import Any

from fwrouter_api.db.connection import db_session
from fwrouter_api.services.subject_taxonomy import explicit_external_client_allows_virtual_vpn_auto


VIRTUAL_XRAY_VPN_AUTO_SERVER_ID = "virtual:xray:vpn-auto"
MANUAL_SERVER_TTL_HOURS = 24
GLOBAL_FIXED_SERVER_TTL_HOURS = 24


def _json_loads(value: str | None) -> dict[str, Any] | None:
    if not value:
        return None

    loaded = json.loads(value)
    if isinstance(loaded, dict):
        return loaded

    return {"value": loaded}


def _json_dumps(value: dict[str, Any] | None) -> str | None:
    if value is None:
        return None

    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _row_to_server(row: Any) -> dict[str, Any]:
    return {
        "server_id": row["server_id"],
        "server_name": row["server_name"],
        "kind": "vpn_server",
        "provider_name": row["provider_name"],
        "country_code": row["country_code"],
        "region": row["region"],
        "inventory_state": row["inventory_state"],
        "raw": _json_loads(row["raw_json"]),
        "first_seen_at": row["first_seen_at"],
        "last_seen_at": row["last_seen_at"],
        "missing_since": row["missing_since"],
        "updated_at": row["updated_at"],
        "preferences": {
            "vpn_auto": bool(row["vpn_auto"]) if row["vpn_auto"] is not None else False,
            "vpn_auto_priority": int(row["vpn_auto_priority"] or 0),
            "global_list": (
                bool(row["global_list"]) if row["global_list"] is not None else True
            ),
            "remembered_until": row["remembered_until"],
            "manually_deleted_at": row["manually_deleted_at"],
        },
        "ping": {
            "status": row["ping_status"] or "unknown",
            "last_ping_ms": row["last_ping_ms"],
            "checked_at": row["checked_at"],
            "checked_by": row["checked_by"],
            "error_code": row["ping_error_code"],
            "error_message": row["ping_error_message"],
            "metadata": _json_loads(row["ping_metadata_json"]),
        },
    }


def list_servers(
    *,
    inventory_state: str | None = None,
    vpn_auto: bool | None = None,
    global_list: bool | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """Return VPN server inventory with preferences and ping state.

    This is read-only and does not refresh subscription or run ping checks.
    """

    safe_limit = max(1, min(limit, 1000))

    where: list[str] = []
    params: list[Any] = []

    if inventory_state:
        where.append("s.inventory_state = ?")
        params.append(inventory_state)

    if vpn_auto is not None:
        where.append("COALESCE(p.vpn_auto, 0) = ?")
        params.append(1 if vpn_auto else 0)

    if global_list is not None:
        where.append("COALESCE(p.global_list, 1) = ?")
        params.append(1 if global_list else 0)

    where_sql = f"WHERE {' AND '.join(where)}" if where else ""

    with db_session() as connection:
        rows = connection.execute(
            f"""
            SELECT
                s.server_id,
                s.server_name,
                s.provider_name,
                s.country_code,
                s.region,
                s.raw_json,
                s.inventory_state,
                s.first_seen_at,
                s.last_seen_at,
                s.missing_since,
                s.updated_at,
                p.vpn_auto,
                p.vpn_auto_priority,
                p.global_list,
                p.remembered_until,
                p.manually_deleted_at,
                ps.status AS ping_status,
                ps.last_ping_ms,
                ps.checked_at,
                ps.checked_by,
                ps.error_code AS ping_error_code,
                ps.error_message AS ping_error_message,
                ps.metadata_json AS ping_metadata_json
            FROM servers s
            LEFT JOIN server_preferences p ON p.server_id = s.server_id
            LEFT JOIN server_ping_state ps ON ps.server_id = s.server_id
            {where_sql}
            ORDER BY
                s.inventory_state = 'active' DESC,
                s.server_name ASC
            LIMIT ?
            """,
            (*params, safe_limit),
        ).fetchall()

    return [_row_to_server(row) for row in rows]


def get_server(server_id: str) -> dict[str, Any] | None:
    """Return one server by server_id."""

    servers = list_servers(limit=1000)
    for server in servers:
        if server["server_id"] == server_id:
            return server
    return None


def sync_servers_from_mihomo() -> dict[str, Any]:
    """Sync current read-only Mihomo inventory into SQLite.

    This does not refresh provider subscriptions, does not switch active server
    and does not apply dataplane changes.
    """

    from fwrouter_api.adapters.mihomo import DEFAULT_MIHOMO_ADAPTER

    mihomo_servers = DEFAULT_MIHOMO_ADAPTER.list_servers()
    seen_ids = {server.server_id for server in mihomo_servers}

    with db_session() as connection:
        for server in mihomo_servers:
            connection.execute(
                """
                INSERT INTO servers (
                    server_id,
                    server_name,
                    provider_name,
                    country_code,
                    region,
                    raw_json,
                    inventory_state,
                    missing_since
                )
                VALUES (?, ?, ?, ?, ?, ?, 'active', NULL)
                ON CONFLICT(server_id) DO UPDATE SET
                    server_name = excluded.server_name,
                    provider_name = excluded.provider_name,
                    country_code = excluded.country_code,
                    region = excluded.region,
                    raw_json = excluded.raw_json,
                    inventory_state = 'active',
                    last_seen_at = CURRENT_TIMESTAMP,
                    missing_since = NULL,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    server.server_id,
                    server.server_name,
                    server.provider_name,
                    None,
                    None,
                    _json_dumps(server.raw),
                ),
            )

            connection.execute(
                "INSERT OR IGNORE INTO server_preferences (server_id) VALUES (?)",
                (server.server_id,),
            )
            connection.execute(
                "INSERT OR IGNORE INTO server_ping_state (server_id) VALUES (?)",
                (server.server_id,),
            )

        if seen_ids:
            placeholders = ", ".join("?" for _ in seen_ids)
            connection.execute(
                f"""
                UPDATE servers
                SET
                    inventory_state = 'missing',
                    missing_since = COALESCE(missing_since, CURRENT_TIMESTAMP),
                    updated_at = CURRENT_TIMESTAMP
                WHERE inventory_state = 'active'
                  AND server_id NOT IN ({placeholders})
                  AND server_id NOT IN (
                      SELECT server_id FROM server_custom_https_proxy
                  )
                """,
                tuple(sorted(seen_ids)),
            )
        else:
            connection.execute(
                """
                UPDATE servers
                SET
                    inventory_state = 'missing',
                    missing_since = COALESCE(missing_since, CURRENT_TIMESTAMP),
                    updated_at = CURRENT_TIMESTAMP
                WHERE inventory_state = 'active'
                  AND server_id NOT IN (
                      SELECT server_id FROM server_custom_https_proxy
                  )
                """
            )

        active_count = connection.execute(
            "SELECT COUNT(*) FROM servers WHERE inventory_state = 'active'"
        ).fetchone()[0]
        missing_count = connection.execute(
            "SELECT COUNT(*) FROM servers WHERE inventory_state = 'missing'"
        ).fetchone()[0]

    return {
        "source": "mihomo",
        "seen_count": len(seen_ids),
        "active_count": active_count,
        "missing_count": missing_count,
        "servers": list_servers(limit=1000),
    }
