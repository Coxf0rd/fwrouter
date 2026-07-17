from __future__ import annotations

import json
from typing import Any

from fwrouter_api.db.connection import db_session


VIRTUAL_XRAY_VPN_AUTO_SERVER_ID = "virtual:xray:vpn-auto"


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


def get_routing_global_state() -> dict[str, Any] | None:
    """Return global routing/server selection state."""

    with db_session() as connection:
        row = connection.execute(
            """
            SELECT
                id,
                desired_mode,
                applied_mode,
                selective_default,
                server_mode,
                desired_fixed_server_id,
                applied_fixed_server_id,
                active_auto_server_id,
                apply_state,
                error_code,
                error_message,
                updated_at
            FROM routing_global_state
            WHERE id = 1
            """
        ).fetchone()

    if row is None:
        return None

    return {
        "desired_mode": row["desired_mode"],
        "applied_mode": row["applied_mode"],
        "selective_default": row["selective_default"],
        "server_mode": row["server_mode"],
        "desired_fixed_server_id": row["desired_fixed_server_id"],
        "applied_fixed_server_id": row["applied_fixed_server_id"],
        "active_auto_server_id": row["active_auto_server_id"],
        "apply_state": row["apply_state"],
        "error_code": row["error_code"],
        "error_message": row["error_message"],
        "updated_at": row["updated_at"],
    }

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



MANUAL_SERVER_TTL_HOURS = 24


def ensure_routing_global_state() -> dict[str, Any]:
    """Ensure singleton global routing state row exists."""

    with db_session() as connection:
        connection.execute(
            """
            INSERT OR IGNORE INTO routing_global_state (
                id,
                desired_mode,
                selective_default,
                server_mode,
                apply_state
            )
            VALUES (1, 'direct', 'direct', 'auto', 'clean')
            """
        )

    state = get_routing_global_state()
    if state is None:
        raise RuntimeError("Failed to initialize routing_global_state row")
    return state


def _get_active_server_row(server_id: str) -> Any | None:
    with db_session() as connection:
        return connection.execute(
            """
            SELECT
                s.server_id,
                s.server_name,
                s.inventory_state,
                COALESCE(p.vpn_auto, 0) AS vpn_auto,
                COALESCE(p.global_list, 1) AS global_list,
                COALESCE(p.manually_deleted_at, '') AS manually_deleted_at
            FROM servers s
            LEFT JOIN server_preferences p ON p.server_id = s.server_id
            WHERE s.server_id = ?
              AND s.inventory_state = 'active'
            """,
            (server_id,),
        ).fetchone()


def _get_subject_row(subject_id: str) -> Any | None:
    with db_session() as connection:
        return connection.execute(
            """
            SELECT
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
                updated_at
            FROM subjects
            WHERE subject_id = ?
              AND COALESCE(is_deleted, 0) = 0
            """,
            (subject_id,),
        ).fetchone()


def _validate_global_fixed_server(server_id: str) -> dict[str, Any]:
    row = _get_active_server_row(server_id)
    if row is None:
        return {
            "ok": False,
            "error_code": "SERVER_NOT_FOUND_OR_INACTIVE",
            "error_message": f"Server is not active in full inventory: {server_id}",
            "server": None,
        }

    if row["manually_deleted_at"]:
        return {
            "ok": False,
            "error_code": "SERVER_MANUALLY_DELETED",
            "error_message": f"Server is manually deleted: {server_id}",
            "server": dict(row),
        }

    return {
        "ok": True,
        "error_code": None,
        "error_message": None,
        "server": dict(row),
    }


def _mihomo_target_for_server(server: dict[str, Any] | None, fallback_server_id: str) -> str:
    if not isinstance(server, dict):
        return str(fallback_server_id)
    return str(server.get("server_name") or fallback_server_id)


def _validate_user_selectable_server(server_id: str) -> dict[str, Any]:
    validation = _validate_global_fixed_server(server_id)
    if not validation["ok"]:
        return validation

    server = validation["server"] or {}
    if not bool(server.get("vpn_auto")) and not bool(server.get("global_list")):
        return {
            "ok": False,
            "error_code": "SERVER_NOT_USER_SELECTABLE",
            "error_message": (
                "Server is not available in vpn-auto or global-list for user manual selection: "
                f"{server_id}"
            ),
            "server": server,
        }

    return validation


def set_global_fixed_server(
    server_id: str,
    *,
    requested_by: str = "admin",
) -> dict[str, Any]:
    """Persist admin global fixed server desired state.

    Admin global fixed server is selected from full active inventory and is not
    limited by vpn_auto/global_list flags. This function does not switch Mihomo;
    runtime apply is handled by a separate apply step.
    """

    validation = _validate_global_fixed_server(server_id)

    if not validation["ok"]:
        return {
            "ok": False,
            "routing": ensure_routing_global_state(),
            "server": validation["server"],
            "error_code": validation["error_code"],
            "error_message": validation["error_message"],
        }

    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO routing_global_state (
                id,
                desired_mode,
                selective_default,
                server_mode,
                desired_fixed_server_id,
                apply_state,
                error_code,
                error_message,
                updated_at
            )
            VALUES (1, 'direct', 'direct', 'fixed', ?, 'pending', NULL, NULL, CURRENT_TIMESTAMP)
            ON CONFLICT(id) DO UPDATE SET
                server_mode = 'fixed',
                desired_fixed_server_id = excluded.desired_fixed_server_id,
                apply_state = 'pending',
                error_code = NULL,
                error_message = NULL,
                updated_at = CURRENT_TIMESTAMP
            """,
            (server_id,),
        )

    return {
        "ok": True,
        "requested_by": requested_by,
        "routing": get_routing_global_state(),
        "server": validation["server"],
    }


def clear_global_fixed_server(
    *,
    requested_by: str = "admin",
) -> dict[str, Any]:
    """Return global server selection to auto/vpn-auto desired state."""

    ensure_routing_global_state()

    with db_session() as connection:
        connection.execute(
            """
            UPDATE routing_global_state
            SET
                server_mode = 'auto',
                desired_fixed_server_id = NULL,
                applied_fixed_server_id = NULL,
                apply_state = 'pending',
                error_code = NULL,
                error_message = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = 1
            """
        )

    return {
        "ok": True,
        "requested_by": requested_by,
        "routing": get_routing_global_state(),
    }


def set_subject_server_override(
    subject_id: str,
    server_id: str,
    *,
    requested_by: str = "user",
) -> dict[str, Any]:
    """Persist user/device manual server override with 24h TTL.

    User manual selected server is valid only if the server is active and
    currently available in at least one user-visible list: vpn-auto or global-list.
    This function only stores desired override state. Runtime materialization
    depends on subject-specific scoped egress support:
    - LAN and Tailscale-node subjects can materialize inside the owned nft contour.
    - Xray subjects keep the override in control-plane/runtime state, but still
      require a future Xray-specific runtime matcher before they can be applied.
    """

    subject = _get_subject_row(subject_id)
    if subject is None:
        return {
            "ok": False,
            "subject_id": subject_id,
            "server": None,
            "error_code": "SUBJECT_NOT_FOUND",
            "error_message": f"Subject not found or deleted: {subject_id}",
        }

    subject_type = str(subject["subject_type"] or "")
    if str(server_id or "").strip() == VIRTUAL_XRAY_VPN_AUTO_SERVER_ID:
        if subject_type != "xray":
            return {
                "ok": False,
                "subject_id": subject_id,
                "subject": dict(subject),
                "server": None,
                "error_code": "SERVER_OVERRIDE_VPN_AUTO_XRAY_ONLY",
                "error_message": "Virtual vpn-auto override is supported only for Xray subjects.",
            }
        validation = {
            "ok": True,
            "error_code": None,
            "error_message": None,
            "server": {
                "server_id": server_id,
                "server_name": "vpn-auto",
                "inventory_state": "active",
                "vpn_auto": True,
                "global_list": True,
                "virtual": True,
            },
        }
    else:
        validation = _validate_user_selectable_server(server_id)

    if not validation["ok"]:
        return {
            "ok": False,
            "subject_id": subject_id,
            "subject": dict(subject),
            "server": validation["server"],
            "error_code": validation["error_code"],
            "error_message": validation["error_message"],
        }

    with db_session() as connection:
        connection.execute(
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
            VALUES (
                ?,
                ?,
                datetime('now', '+' || ? || ' hours'),
                'pending',
                NULL,
                NULL,
                CURRENT_TIMESTAMP
            )
            ON CONFLICT(subject_id) DO UPDATE SET
                selected_server_id = excluded.selected_server_id,
                selected_until = excluded.selected_until,
                apply_state = 'pending',
                error_code = NULL,
                error_message = NULL,
                updated_at = CURRENT_TIMESTAMP
            """,
            (subject_id, server_id, MANUAL_SERVER_TTL_HOURS),
        )

        row = connection.execute(
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
            WHERE subject_id = ?
            """,
            (subject_id,),
        ).fetchone()

    return {
        "ok": True,
        "requested_by": requested_by,
        "override": dict(row),
        "server": validation["server"],
    }


def clear_subject_server_override(
    subject_id: str,
    *,
    requested_by: str = "user",
) -> dict[str, Any]:
    """Clear manual server override and return subject to global/auto behavior."""

    with db_session() as connection:
        row_before = connection.execute(
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
            WHERE subject_id = ?
            """,
            (subject_id,),
        ).fetchone()

        connection.execute(
            """
            DELETE FROM subject_server_overrides
            WHERE subject_id = ?
            """,
            (subject_id,),
        )

    return {
        "ok": True,
        "requested_by": requested_by,
        "subject_id": subject_id,
        "cleared_override": dict(row_before) if row_before else None,
    }


def get_subject_server_override(subject_id: str) -> dict[str, Any] | None:
    """Return non-expired manual server override for one subject."""

    with db_session() as connection:
        row = connection.execute(
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
            WHERE subject_id = ?
              AND selected_until > CURRENT_TIMESTAMP
            """,
            (subject_id,),
        ).fetchone()

    return dict(row) if row else None


def update_subject_server_override_apply_status(
    subject_id: str,
    *,
    apply_state: str,
    error_code: str | None = None,
    error_message: str | None = None,
) -> dict[str, Any] | None:
    with db_session() as connection:
        connection.execute(
            """
            UPDATE subject_server_overrides
            SET
                apply_state = ?,
                error_code = ?,
                error_message = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE subject_id = ?
            """,
            (apply_state, error_code, error_message, subject_id),
        )

    return get_subject_server_override(subject_id)


def _restore_global_routing_state(previous_state: dict[str, Any]) -> dict[str, Any]:
    """Restore global routing row after failed fixed-server apply."""

    ensure_routing_global_state()

    with db_session() as connection:
        connection.execute(
            """
            UPDATE routing_global_state
            SET
                desired_mode = ?,
                applied_mode = ?,
                selective_default = ?,
                server_mode = ?,
                desired_fixed_server_id = ?,
                applied_fixed_server_id = ?,
                active_auto_server_id = ?,
                apply_state = ?,
                error_code = ?,
                error_message = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = 1
            """,
            (
                previous_state["desired_mode"],
                previous_state["applied_mode"],
                previous_state["selective_default"],
                previous_state["server_mode"],
                previous_state["desired_fixed_server_id"],
                previous_state["applied_fixed_server_id"],
                previous_state["active_auto_server_id"],
                previous_state["apply_state"],
                previous_state["error_code"],
                previous_state["error_message"],
            ),
        )

    restored = get_routing_global_state()
    if restored is None:
        raise RuntimeError("Failed to restore routing_global_state")
    return restored


def apply_global_fixed_server(
    server_id: str,
    *,
    requested_by: str = "admin",
    timeout_ms: int = 10000,
    post_check: bool = True,
) -> dict[str, Any]:
    """Pre-check, persist and apply admin global fixed server.

    Admin global fixed server is selected from full active inventory and is not
    limited by vpn_auto/global_list. Runtime apply switches Mihomo vpn-global,
    not vpn-auto.

    Apply failure rolls back global routing state. Post-check failure does not
    roll back the successful selector switch.
    """

    from fwrouter_api.adapters.mihomo import DEFAULT_MIHOMO_ADAPTER
    from fwrouter_api.services.server_ping import check_server_delay

    previous_state = ensure_routing_global_state()
    active_before = DEFAULT_MIHOMO_ADAPTER.get_active_server_id()

    validation = _validate_global_fixed_server(server_id)
    if not validation["ok"]:
        return {
            "ok": False,
            "requested_by": requested_by,
            "server_id": server_id,
            "active_before": active_before,
            "routing_before": previous_state,
            "routing_after": previous_state,
            "server": validation["server"],
            "pre_check": None,
            "apply_result": None,
            "post_check": None,
            "rolled_back": False,
            "post_check_failed_no_rollback": False,
            "error_code": validation["error_code"],
            "error_message": validation["error_message"],
        }

    pre_check = check_server_delay(
        server_id,
        update_state=True,
        checked_by=f"global_fixed_pre_check:{requested_by}",
        timeout_ms=timeout_ms,
    )

    if not pre_check["ok"]:
        return {
            "ok": False,
            "requested_by": requested_by,
            "server_id": server_id,
            "active_before": active_before,
            "routing_before": previous_state,
            "routing_after": previous_state,
            "server": validation["server"],
            "pre_check": pre_check,
            "apply_result": None,
            "post_check": None,
            "rolled_back": False,
            "post_check_failed_no_rollback": False,
            "error_code": "GLOBAL_FIXED_SERVER_PRE_CHECK_FAILED",
            "error_message": pre_check["error_message"] or "Global fixed server pre-check failed.",
        }

    desired = set_global_fixed_server(
        server_id,
        requested_by=requested_by,
    )

    if not desired["ok"]:
        return {
            "ok": False,
            "requested_by": requested_by,
            "server_id": server_id,
            "active_before": active_before,
            "routing_before": previous_state,
            "routing_after": get_routing_global_state(),
            "server": validation["server"],
            "pre_check": pre_check,
            "apply_result": None,
            "post_check": None,
            "rolled_back": False,
            "post_check_failed_no_rollback": False,
            "error_code": desired["error_code"],
            "error_message": desired["error_message"],
        }

    mihomo_target = _mihomo_target_for_server(validation["server"], server_id)
    apply_result = DEFAULT_MIHOMO_ADAPTER.apply_server_to_selector(
        "vpn-global",
        mihomo_target,
    )

    if not apply_result.ok:
        restored = _restore_global_routing_state(previous_state)

        return {
            "ok": False,
            "requested_by": requested_by,
            "server_id": server_id,
            "active_before": active_before,
            "active_after": DEFAULT_MIHOMO_ADAPTER.get_active_server_id(),
            "routing_before": previous_state,
            "routing_after": restored,
            "server": validation["server"],
            "pre_check": pre_check,
            "apply_result": apply_result.to_dict(),
            "mihomo_target": mihomo_target,
            "post_check": None,
            "rolled_back": True,
            "post_check_failed_no_rollback": False,
            "error_code": apply_result.error_code or "GLOBAL_FIXED_SERVER_APPLY_FAILED",
            "error_message": apply_result.error_message or apply_result.message,
        }

    post_check_result = None
    post_check_failed_no_rollback = False

    if post_check:
        post_check_result = check_server_delay(
            server_id,
            update_state=True,
            checked_by=f"global_fixed_post_check:{requested_by}",
            timeout_ms=timeout_ms,
        )
        post_check_failed_no_rollback = not post_check_result["ok"]

    apply_state = "clean"
    error_code = None
    error_message = None

    if post_check_failed_no_rollback:
        apply_state = "degraded"
        error_code = post_check_result["error_code"] if post_check_result else None
        error_message = (
            post_check_result["error_message"]
            if post_check_result
            else "Global fixed server post-check failed."
        )

    with db_session() as connection:
        connection.execute(
            """
            UPDATE routing_global_state
            SET
                server_mode = 'fixed',
                desired_fixed_server_id = ?,
                applied_fixed_server_id = ?,
                apply_state = ?,
                error_code = ?,
                error_message = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = 1
            """,
            (
                server_id,
                server_id,
                apply_state,
                error_code,
                error_message,
            ),
        )

    routing_after = get_routing_global_state()

    return {
        "ok": True,
        "requested_by": requested_by,
        "server_id": server_id,
        "active_before": active_before,
        "active_after": DEFAULT_MIHOMO_ADAPTER.get_active_server_id(),
        "routing_before": previous_state,
        "routing_after": routing_after,
        "server": validation["server"],
        "pre_check": pre_check,
        "apply_result": apply_result.to_dict(),
        "mihomo_target": mihomo_target,
        "post_check": post_check_result,
        "rolled_back": False,
        "post_check_failed_no_rollback": post_check_failed_no_rollback,
        "error_code": error_code,
        "error_message": error_message,
    }


def apply_global_auto_server(
    *,
    requested_by: str = "admin",
) -> dict[str, Any]:
    """Return global egress selector to vpn-auto.

    This clears admin global fixed server and switches Mihomo vpn-global back to
    vpn-auto. It does not run selector itself.
    """

    from fwrouter_api.adapters.mihomo import DEFAULT_MIHOMO_ADAPTER

    previous_state = ensure_routing_global_state()
    active_before = DEFAULT_MIHOMO_ADAPTER.get_active_server_id()

    desired = clear_global_fixed_server(requested_by=requested_by)

    apply_result = DEFAULT_MIHOMO_ADAPTER.apply_server_to_selector(
        "vpn-global",
        "vpn-auto",
    )

    if not apply_result.ok:
        restored = _restore_global_routing_state(previous_state)
        return {
            "ok": False,
            "requested_by": requested_by,
            "active_before": active_before,
            "active_after": DEFAULT_MIHOMO_ADAPTER.get_active_server_id(),
            "routing_before": previous_state,
            "routing_after": restored,
            "desired_result": desired,
            "apply_result": apply_result.to_dict(),
            "rolled_back": True,
            "error_code": apply_result.error_code or "GLOBAL_AUTO_APPLY_FAILED",
            "error_message": apply_result.error_message or apply_result.message,
        }

    with db_session() as connection:
        connection.execute(
            """
            UPDATE routing_global_state
            SET
                server_mode = 'auto',
                desired_fixed_server_id = NULL,
                applied_fixed_server_id = NULL,
                active_auto_server_id = ?,
                apply_state = 'clean',
                error_code = NULL,
                error_message = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = 1
            """,
            (apply_result.active_server_id,),
        )

    return {
        "ok": True,
        "requested_by": requested_by,
        "active_before": active_before,
        "active_after": DEFAULT_MIHOMO_ADAPTER.get_active_server_id(),
        "routing_before": previous_state,
        "routing_after": get_routing_global_state(),
        "desired_result": desired,
        "apply_result": apply_result.to_dict(),
        "rolled_back": False,
        "error_code": None,
        "error_message": None,
    }


def set_global_mode(
    mode: str,
    *,
    requested_by: str = "api",
) -> dict[str, Any]:
    from fwrouter_api.services.apply_orchestrator import set_global_mode as run_global_mode_transaction

    return run_global_mode_transaction(mode, requested_by=requested_by)


def reconcile_current_routing_if_drift(
    *,
    requested_by: str = "api",
) -> dict[str, Any]:
    from fwrouter_api.services.apply_orchestrator import (
        reconcile_current_routing_if_drift as run_routing_drift_reconcile,
    )

    return run_routing_drift_reconcile(requested_by=requested_by)


def set_selective_default(
    selective_default: str,
    *,
    requested_by: str = "api",
) -> dict[str, Any]:
    from fwrouter_api.services.apply_orchestrator import set_selective_default as run_selective_default_transaction

    return run_selective_default_transaction(
        selective_default,
        requested_by=requested_by,
    )



def _unique_server_ids(server_ids: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []

    for server_id in server_ids:
        normalized = str(server_id or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)

    return result


def _reconcile_mihomo_after_server_preferences(
    *,
    enabled: bool,
) -> dict[str, Any] | None:
    if not enabled:
        return None

    from fwrouter_api.services.mihomo_config import reconcile_mihomo_runtime
    from fwrouter_api.services.xray import reconcile_xray_vpn_auto_subscription

    try:
        xray_result = reconcile_xray_vpn_auto_subscription(
            requested_by="server_preferences_vpn_auto",
        )
    except Exception as exc:
        xray_result = {
            "ok": False,
            "status": "failed",
            "stage": "exception",
            "error_code": "XRAY_VPN_AUTO_RECONCILE_EXCEPTION",
            "error_message": f"{type(exc).__name__}: {exc}",
        }

    mihomo_result = xray_result.get("mihomo_reconcile") if isinstance(xray_result, dict) else None
    if not isinstance(mihomo_result, dict):
        mihomo_result = dict(reconcile_mihomo_runtime() or {})

    result = dict(mihomo_result)
    result["xray_vpn_auto_reconcile"] = xray_result

    if result.get("ok", False) and not xray_result.get("ok", False):
        result["ok"] = False
        result["stage"] = "xray_vpn_auto_reconcile"
        result["error_code"] = xray_result.get("error_code") or "XRAY_VPN_AUTO_RECONCILE_FAILED"
        result["error_message"] = xray_result.get("error_message") or "Xray vpn-auto reconcile failed."

    return result


def _preference_server_summary(server: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(server, dict):
        return None

    preferences = server.get("preferences") if isinstance(server.get("preferences"), dict) else {}
    ping = server.get("ping") if isinstance(server.get("ping"), dict) else {}

    return {
        "server_id": server.get("server_id"),
        "server_name": server.get("server_name"),
        "provider_name": server.get("provider_name"),
        "inventory_state": server.get("inventory_state"),
        "preferences": {
            "vpn_auto": bool(preferences.get("vpn_auto")),
            "vpn_auto_priority": int(preferences.get("vpn_auto_priority") or 0),
            "global_list": bool(preferences.get("global_list", True)),
            "remembered_until": preferences.get("remembered_until"),
            "manually_deleted_at": preferences.get("manually_deleted_at"),
        },
        "ping": {
            "status": ping.get("status"),
            "last_ping_ms": ping.get("last_ping_ms"),
            "checked_at": ping.get("checked_at"),
            "checked_by": ping.get("checked_by"),
            "error_code": ping.get("error_code"),
            "error_message": ping.get("error_message"),
        },
    }


def _preference_server_summaries(servers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        summary
        for summary in (_preference_server_summary(server) for server in servers)
        if summary is not None
    ]


def update_server_preferences(
    server_id: str,
    *,
    vpn_auto: bool | None = None,
    vpn_auto_priority: int | None = None,
    global_list: bool | None = None,
    reconcile_mihomo: bool = True,
    requested_by: str = "api",
) -> dict[str, Any]:
    """Update user-visible preferences for one active server.

    vpn_auto and global_list both affect generated Mihomo selector/runtime
    shape. Any change to either may require config reconcile.
    """

    normalized_server_id = str(server_id or "").strip()
    if not normalized_server_id:
        return {
            "ok": False,
            "changed": False,
            "error_code": "SERVER_ID_EMPTY",
            "error_message": "Server id is empty.",
            "server": None,
            "mihomo_reconcile": None,
        }

    if vpn_auto is None and vpn_auto_priority is None and global_list is None:
        return {
            "ok": False,
            "changed": False,
            "error_code": "SERVER_PREFERENCES_EMPTY",
            "error_message": "Provide at least one of: vpn_auto, vpn_auto_priority, global_list.",
            "server": get_server(normalized_server_id),
            "mihomo_reconcile": None,
        }

    validation = _validate_global_fixed_server(normalized_server_id)
    if not validation["ok"]:
        return {
            "ok": False,
            "changed": False,
            "error_code": validation["error_code"],
            "error_message": validation["error_message"],
            "server": validation["server"],
            "mihomo_reconcile": None,
        }

    current_server = get_server(normalized_server_id)
    current_preferences = (current_server or {}).get("preferences") or {}

    assignments: list[str] = []
    params: list[Any] = []
    changed_fields: list[str] = []

    if vpn_auto is not None:
        new_vpn_auto = bool(vpn_auto)
        if bool(current_preferences.get("vpn_auto")) != new_vpn_auto:
            assignments.append("vpn_auto = ?")
            params.append(1 if new_vpn_auto else 0)
            changed_fields.append("vpn_auto")

    if vpn_auto_priority is not None:
        normalized_priority = int(vpn_auto_priority)
        if normalized_priority < -1 or normalized_priority > 5:
            return {
                "ok": False,
                "changed": False,
                "error_code": "VPN_AUTO_PRIORITY_INVALID",
                "error_message": "vpn_auto_priority must be between -1 and 5.",
                "server": _preference_server_summary(current_server),
                "mihomo_reconcile": None,
            }
        if int(current_preferences.get("vpn_auto_priority") or 0) != normalized_priority:
            assignments.append("vpn_auto_priority = ?")
            params.append(normalized_priority)
            changed_fields.append("vpn_auto_priority")

    if global_list is not None:
        new_global_list = bool(global_list)
        if bool(current_preferences.get("global_list", True)) != new_global_list:
            assignments.append("global_list = ?")
            params.append(1 if new_global_list else 0)
            changed_fields.append("global_list")

    if not changed_fields:
        return {
            "ok": True,
            "changed": False,
            "changed_fields": [],
            "requested_by": requested_by,
            "server_id": normalized_server_id,
            "server": _preference_server_summary(current_server),
            "mihomo_reconcile": None,
            "error_code": None,
            "error_message": None,
        }

    assignments.append("updated_at = CURRENT_TIMESTAMP")

    with db_session() as connection:
        connection.execute(
            "INSERT OR IGNORE INTO server_preferences (server_id) VALUES (?)",
            (normalized_server_id,),
        )
        connection.execute(
            f"""
            UPDATE server_preferences
            SET {", ".join(assignments)}
            WHERE server_id = ?
            """,
            (*params, normalized_server_id),
        )

    server = get_server(normalized_server_id)
    mihomo_reconcile = _reconcile_mihomo_after_server_preferences(
        enabled=reconcile_mihomo and any(field in changed_fields for field in {"vpn_auto", "global_list"}),
    )
    auto_select = None
    if any(field in changed_fields for field in {"vpn_auto", "global_list"}):
        auto_select = _maybe_reselect_vpn_auto_after_membership_change(
            reason="vpn_auto_membership_changed",
        )

    if mihomo_reconcile is not None and not mihomo_reconcile.get("ok", False):
        return {
            "ok": False,
            "changed": True,
            "changed_fields": changed_fields,
            "requested_by": requested_by,
            "server_id": normalized_server_id,
            "server": _preference_server_summary(server),
            "mihomo_reconcile": mihomo_reconcile,
            "auto_select": auto_select,
            "error_code": "MIHOMO_RECONCILE_FAILED",
            "error_message": "Server preferences were updated, but Mihomo runtime reconcile failed.",
        }

    return {
        "ok": True,
        "changed": True,
        "changed_fields": changed_fields,
        "requested_by": requested_by,
        "server_id": normalized_server_id,
        "server": _preference_server_summary(server),
        "mihomo_reconcile": mihomo_reconcile,
        "auto_select": auto_select,
        "error_code": None,
        "error_message": None,
    }


def _current_vpn_auto_server_ids() -> list[str]:
    with db_session() as connection:
        rows = connection.execute(
            """
            SELECT p.server_id
            FROM server_preferences p
            JOIN servers s ON s.server_id = p.server_id
            WHERE COALESCE(p.vpn_auto, 0) = 1
              AND COALESCE(p.manually_deleted_at, '') = ''
              AND s.inventory_state = 'active'
            ORDER BY p.server_id
            """
        ).fetchall()

    return [str(row["server_id"]) for row in rows]


def _persist_active_auto_server_id(server_id: str | None) -> None:
    with db_session() as connection:
        connection.execute(
            """
            UPDATE routing_global_state
            SET
                active_auto_server_id = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = 1
            """,
            (server_id,),
        )


def _maybe_reselect_vpn_auto_after_membership_change(
    *,
    reason: str,
) -> dict[str, Any]:
    from fwrouter_api.services.selector import get_vpn_auto_state, select_vpn_auto_server

    state = get_vpn_auto_state()
    if str(state.get("server_mode") or "auto") != "auto":
        return {
            "ok": True,
            "triggered": False,
            "status": "skipped_not_auto_mode",
            "state": state,
        }

    if int(state.get("auto_selectable_candidates_count") or 0) <= 0:
        _persist_active_auto_server_id(None)
        return {
            "ok": True,
            "triggered": False,
            "status": "vpn_auto_no_auto_selectable_candidates",
            "state": get_vpn_auto_state(),
        }

    if bool(state.get("active_auto_server_valid")):
        return {
            "ok": True,
            "triggered": False,
            "status": "active_auto_server_still_valid",
            "state": state,
        }

    selector_result = select_vpn_auto_server(
        apply=True,
        check_on_demand=True,
        exclude_active=bool(state.get("active_auto_server_id")),
        reason=reason,
        post_check=True,
    )
    if not selector_result.get("ok") and not selector_result.get("selected_server_id"):
        _persist_active_auto_server_id(None)

    return {
        "ok": bool(selector_result.get("ok")),
        "triggered": True,
        "status": "reselected" if selector_result.get("ok") else "no_working_candidates",
        "selector": selector_result,
        "state": get_vpn_auto_state(),
    }



def replace_vpn_auto_servers(
    server_ids: list[str],
    *,
    reconcile_mihomo: bool = True,
    requested_by: str = "api",
) -> dict[str, Any]:
    """Replace the whole vpn-auto membership with an explicit server list."""

    normalized_server_ids = _unique_server_ids(server_ids)
    invalid_servers: list[dict[str, Any]] = []

    for server_id in normalized_server_ids:
        validation = _validate_global_fixed_server(server_id)
        if not validation["ok"]:
            invalid_servers.append(
                {
                    "server_id": server_id,
                    "error_code": validation["error_code"],
                    "error_message": validation["error_message"],
                    "server": validation["server"],
                }
            )

    if invalid_servers:
        return {
            "ok": False,
            "changed": False,
            "requested_by": requested_by,
            "server_ids": normalized_server_ids,
            "invalid_servers": invalid_servers,
            "vpn_auto_servers": _preference_server_summaries(list_servers(inventory_state="active", vpn_auto=True, limit=1000)),
            "mihomo_reconcile": None,
            "error_code": "VPN_AUTO_SERVER_INVALID",
            "error_message": "One or more requested vpn-auto servers are invalid.",
        }

    current_server_ids = _current_vpn_auto_server_ids()
    if set(current_server_ids) == set(normalized_server_ids):
        vpn_auto_servers = list_servers(inventory_state="active", vpn_auto=True, limit=1000)
        return {
            "ok": True,
            "changed": False,
            "requested_by": requested_by,
            "server_ids": normalized_server_ids,
            "current_server_ids": current_server_ids,
            "vpn_auto_count": len(vpn_auto_servers),
            "vpn_auto_servers": _preference_server_summaries(vpn_auto_servers),
            "mihomo_reconcile": None,
            "error_code": None,
            "error_message": None,
        }

    with db_session() as connection:
        connection.execute(
            """
            UPDATE server_preferences
            SET
                vpn_auto = 0,
                updated_at = CURRENT_TIMESTAMP
            WHERE vpn_auto = 1
            """
        )

        if normalized_server_ids:
            connection.executemany(
                "INSERT OR IGNORE INTO server_preferences (server_id) VALUES (?)",
                [(server_id,) for server_id in normalized_server_ids],
            )
            placeholders = ", ".join("?" for _ in normalized_server_ids)
            connection.execute(
                f"""
                UPDATE server_preferences
                SET
                    vpn_auto = 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE server_id IN ({placeholders})
                """,
                tuple(normalized_server_ids),
            )

    vpn_auto_servers = list_servers(inventory_state="active", vpn_auto=True, limit=1000)
    mihomo_reconcile = _reconcile_mihomo_after_server_preferences(
        enabled=reconcile_mihomo,
    )
    auto_select = _maybe_reselect_vpn_auto_after_membership_change(
        reason="vpn_auto_membership_changed",
    )

    if mihomo_reconcile is not None and not mihomo_reconcile.get("ok", False):
        return {
            "ok": False,
            "changed": True,
            "requested_by": requested_by,
            "server_ids": normalized_server_ids,
            "current_server_ids": current_server_ids,
            "vpn_auto_count": len(vpn_auto_servers),
            "vpn_auto_servers": _preference_server_summaries(vpn_auto_servers),
            "mihomo_reconcile": mihomo_reconcile,
            "auto_select": auto_select,
            "error_code": "MIHOMO_RECONCILE_FAILED",
            "error_message": "VPN-auto list was updated, but Mihomo runtime reconcile failed.",
        }

    return {
        "ok": True,
        "changed": True,
        "requested_by": requested_by,
        "server_ids": normalized_server_ids,
        "previous_server_ids": current_server_ids,
        "vpn_auto_count": len(vpn_auto_servers),
        "vpn_auto_servers": _preference_server_summaries(vpn_auto_servers),
        "mihomo_reconcile": mihomo_reconcile,
        "auto_select": auto_select,
        "error_code": None,
        "error_message": None,
    }
