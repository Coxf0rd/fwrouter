from __future__ import annotations

import json
from typing import Any

from fwrouter_api.db.connection import db_session
from fwrouter_api.services.subject_taxonomy import explicit_external_client_allows_virtual_vpn_auto


VIRTUAL_XRAY_VPN_AUTO_SERVER_ID = "virtual:xray:vpn-auto"
MANUAL_SERVER_TTL_HOURS = 24
GLOBAL_FIXED_SERVER_TTL_HOURS = 24


from fwrouter_api.services.server_state import ensure_routing_global_state, get_routing_global_state


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
                fixed_server_until,
                apply_state,
                error_code,
                error_message,
                updated_at
            )
            VALUES (1, 'direct', 'direct', 'fixed', ?, datetime('now', '+' || ? || ' hours'), 'pending', NULL, NULL, CURRENT_TIMESTAMP)
            ON CONFLICT(id) DO UPDATE SET
                server_mode = 'fixed',
                desired_fixed_server_id = excluded.desired_fixed_server_id,
                fixed_server_until = excluded.fixed_server_until,
                apply_state = 'pending',
                error_code = NULL,
                error_message = NULL,
                updated_at = CURRENT_TIMESTAMP
            """,
            (server_id, GLOBAL_FIXED_SERVER_TTL_HOURS),
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
                fixed_server_until = NULL,
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
                fixed_server_until = ?,
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
                previous_state.get("fixed_server_until"),
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
    management_context: dict[str, Any] | None = None,
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
    from fwrouter_api.services.management_attribution import (
        build_incomplete_attribution_error,
        build_management_attribution,
    )
    from fwrouter_api.services.server_ping import check_server_delay

    attribution = build_management_attribution(
        requested_by=requested_by,
        context=management_context,
        default_requested_by="admin",
    )
    attribution_error = build_incomplete_attribution_error(attribution)
    if attribution_error is not None:
        return {
            "ok": False,
            "requested_by": requested_by,
            "management_attribution": attribution,
            "server_id": server_id,
            "active_before": None,
            "routing_before": None,
            "routing_after": None,
            "server": None,
            "pre_check": None,
            "apply_result": None,
            "post_check": None,
            "rolled_back": False,
            "post_check_failed_no_rollback": False,
            "error_code": attribution_error["code"],
            "error_message": attribution_error["message"],
            "error": attribution_error,
        }

    previous_state = ensure_routing_global_state()
    active_before = DEFAULT_MIHOMO_ADAPTER.get_active_server_id()

    validation = _validate_global_fixed_server(server_id)
    if not validation["ok"]:
        return {
            "ok": False,
            "requested_by": requested_by,
            "management_attribution": attribution,
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
            "management_attribution": attribution,
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
            "management_attribution": attribution,
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
            "management_attribution": attribution,
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
                fixed_server_until = ?,
                apply_state = ?,
                error_code = ?,
                error_message = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = 1
            """,
            (
                server_id,
                server_id,
                desired["routing"].get("fixed_server_until"),
                apply_state,
                error_code,
                error_message,
            ),
        )

    routing_after = get_routing_global_state()

    from fwrouter_api.services.logs import write_operational_log

    write_operational_log(
        event_type="global_fixed_server_applied",
        message="Global fixed server was applied.",
        details={
            "requested_by": requested_by,
            "management_attribution": attribution,
            "server_id": server_id,
            "mihomo_target": mihomo_target,
            "active_before": active_before,
            "active_after": DEFAULT_MIHOMO_ADAPTER.get_active_server_id(),
            "fixed_server_until": routing_after.get("fixed_server_until") if routing_after else None,
            "post_check_failed_no_rollback": post_check_failed_no_rollback,
        },
    )

    return {
        "ok": True,
        "requested_by": requested_by,
        "management_attribution": attribution,
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
    management_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return global egress selector to vpn-auto.

    This clears admin global fixed server and switches Mihomo vpn-global back to
    vpn-auto. It does not run selector itself.
    """

    from fwrouter_api.adapters.mihomo import DEFAULT_MIHOMO_ADAPTER
    from fwrouter_api.services.management_attribution import (
        build_incomplete_attribution_error,
        build_management_attribution,
    )

    attribution = build_management_attribution(
        requested_by=requested_by,
        context=management_context,
        default_requested_by="admin",
    )
    attribution_error = build_incomplete_attribution_error(attribution)
    if attribution_error is not None:
        return {
            "ok": False,
            "requested_by": requested_by,
            "management_attribution": attribution,
            "active_before": None,
            "active_after": None,
            "routing_before": None,
            "routing_after": None,
            "desired_result": None,
            "apply_result": None,
            "rolled_back": False,
            "error_code": attribution_error["code"],
            "error_message": attribution_error["message"],
            "error": attribution_error,
        }

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
            "management_attribution": attribution,
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
                fixed_server_until = NULL,
                active_auto_server_id = ?,
                apply_state = 'clean',
                error_code = NULL,
                error_message = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = 1
            """,
            (apply_result.active_server_id,),
        )

    routing_after = get_routing_global_state()

    from fwrouter_api.services.logs import write_operational_log

    write_operational_log(
        event_type="global_fixed_server_cleared",
        message="Global fixed server was cleared and vpn-global was returned to vpn-auto.",
        details={
            "requested_by": requested_by,
            "management_attribution": attribution,
            "active_before": active_before,
            "active_after": DEFAULT_MIHOMO_ADAPTER.get_active_server_id(),
            "active_auto_server_id": routing_after.get("active_auto_server_id") if routing_after else None,
        },
    )

    return {
        "ok": True,
        "requested_by": requested_by,
        "management_attribution": attribution,
        "active_before": active_before,
        "active_after": DEFAULT_MIHOMO_ADAPTER.get_active_server_id(),
        "routing_before": previous_state,
        "routing_after": routing_after,
        "desired_result": desired,
        "apply_result": apply_result.to_dict(),
        "rolled_back": False,
        "error_code": None,
        "error_message": None,
    }
