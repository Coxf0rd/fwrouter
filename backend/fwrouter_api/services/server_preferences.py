from __future__ import annotations

import json
from typing import Any

from fwrouter_api.db.connection import db_session
from fwrouter_api.services.subject_taxonomy import explicit_external_client_allows_virtual_vpn_auto


VIRTUAL_XRAY_VPN_AUTO_SERVER_ID = "virtual:xray:vpn-auto"
MANUAL_SERVER_TTL_HOURS = 24
GLOBAL_FIXED_SERVER_TTL_HOURS = 24


from fwrouter_api.services.server_global_selection import _validate_global_fixed_server
from fwrouter_api.services.server_inventory import get_server, list_servers


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
    reconcile_after_preferences: Any = None,
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
    reconcile_callback = reconcile_after_preferences or _reconcile_mihomo_after_server_preferences
    mihomo_reconcile = reconcile_callback(
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
    reconcile_after_preferences: Any = None,
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
    reconcile_callback = reconcile_after_preferences or _reconcile_mihomo_after_server_preferences
    mihomo_reconcile = reconcile_callback(
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
