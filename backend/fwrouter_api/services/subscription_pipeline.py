from __future__ import annotations

import subprocess
from typing import Any

from fwrouter_api.services.logs import write_operational_log, write_technical_log
from fwrouter_api.services.mihomo_config import (
    MIHOMO_CANDIDATE_CONFIG_PATH,
    reconcile_mihomo_runtime,
    write_mihomo_candidate_config,
)
from fwrouter_api.services.selector import get_vpn_auto_state, select_vpn_auto_server
from fwrouter_api.services.servers import get_routing_global_state
from fwrouter_api.services.subscription import refresh_subscription_inventory


MIHOMO_IMAGE = "metacubex/mihomo:v1.19.19"


def _maybe_select_vpn_auto_after_refresh() -> dict[str, Any]:
    routing = get_routing_global_state() or {}
    if str(routing.get("server_mode") or "auto").strip().lower() != "auto":
        return {
            "ok": True,
            "triggered": False,
            "status": "skipped_not_auto_mode",
            "state": get_vpn_auto_state(),
        }

    state = get_vpn_auto_state()
    if int(state.get("auto_selectable_candidates_count") or 0) <= 0:
        return {
            "ok": True,
            "triggered": False,
            "status": "skipped_no_auto_selectable_candidates",
            "state": state,
        }

    if bool(state.get("active_auto_server_valid")):
        return {
            "ok": True,
            "triggered": False,
            "status": "skipped_existing_valid_active",
            "state": state,
        }

    selector = select_vpn_auto_server(
        apply=True,
        check_on_demand=True,
        exclude_active=bool(state.get("active_auto_server_id")),
        reason="subscription_refresh_auto_select",
        post_check=True,
    )
    return {
        "ok": bool(selector.get("ok")),
        "triggered": True,
        "status": "auto_selected" if selector.get("ok") else "pending_auto_select",
        "selector": selector,
        "state": get_vpn_auto_state(),
    }


def validate_mihomo_candidate_config() -> dict[str, Any]:
    """Validate current Mihomo candidate config with Mihomo docker image."""

    validation = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{MIHOMO_CANDIDATE_CONFIG_PATH}:/config/config.yaml:ro",
            MIHOMO_IMAGE,
            "-t",
            "-f",
            "/config/config.yaml",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    return {
        "ok": validation.returncode == 0,
        "returncode": validation.returncode,
        "stdout_tail": validation.stdout[-1000:],
        "stderr_tail": validation.stderr[-1000:],
    }


def prepare_subscription_refresh() -> dict[str, Any]:
    """Run staged subscription refresh without applying runtime changes.

    Pipeline:
    1. refresh provider subscription;
    2. sync server inventory into SQLite;
    3. generate Mihomo candidate config;
    4. validate candidate config;
    5. do not promote active config;
    6. do not restart Mihomo container.
    """

    refresh_result = refresh_subscription_inventory()

    if not refresh_result["ok"]:
        return {
            "ok": False,
            "stage": refresh_result.get("stage"),
            "refresh": refresh_result,
            "candidate": None,
            "config_validation": None,
            "promoted": False,
            "container_restarted": False,
            "error": refresh_result.get("error"),
        }

    candidate = write_mihomo_candidate_config()
    config_validation = validate_mihomo_candidate_config()

    if not config_validation["ok"]:
        return {
            "ok": False,
            "stage": "config_validation",
            "refresh": refresh_result,
            "candidate": candidate,
            "config_validation": config_validation,
            "promoted": False,
            "container_restarted": False,
            "error": {
                "code": "MIHOMO_CONFIG_VALIDATION_FAILED",
                "message": "Generated Mihomo candidate config failed validation.",
            },
        }

    return {
        "ok": True,
        "stage": "candidate_validated",
        "refresh": refresh_result,
        "candidate": candidate,
        "config_validation": config_validation,
        "promoted": False,
        "container_restarted": False,
        "error": None,
    }


def apply_subscription_refresh() -> dict[str, Any]:
    """Refresh subscription inventory and reconcile Mihomo runtime if changed.

    Pipeline:
    1. refresh provider subscription;
    2. sync server inventory into SQLite;
    3. generate Mihomo candidate config;
    4. validate candidate config;
    5. compare candidate with active config;
    6. promote/restart Mihomo only when candidate differs from active config.
    """

    prepared = prepare_subscription_refresh()
    if not prepared.get("ok"):
        return prepared

    reconcile = reconcile_mihomo_runtime()
    promoted = bool((reconcile.get("promoted") or {}).get("promoted"))
    container_action = str((reconcile.get("container") or {}).get("action") or "none")
    container_restarted = container_action not in {"", "none"}
    reconcile_reason = str(reconcile.get("reconcile_reason") or "")
    reconcile_action = str(reconcile.get("reconcile_action") or "none")

    if reconcile.get("ok"):
        auto_select = _maybe_select_vpn_auto_after_refresh()
        result = {
            **prepared,
            "ok": bool(auto_select.get("ok", True)),
            "stage": "applied" if promoted or container_restarted else "already_current",
            "reconcile": reconcile,
            "promoted": promoted,
            "container_restarted": container_restarted,
            "applied": promoted or container_restarted,
            "update_available": promoted or container_restarted,
            "reconcile_action": reconcile_action,
            "reconcile_reason": reconcile_reason,
            "auto_select": auto_select,
            "error": (
                None
                if auto_select.get("ok", True)
                else {
                    "code": "VPN_AUTO_AUTOSELECT_FAILED",
                    "message": "Subscription refresh completed, but vpn-auto could not select a valid active server.",
                }
            ),
        }
        event_type = (
            "subscription_refresh_applied"
            if result["applied"]
            else "subscription_refresh_skipped"
        )
        message = (
            "Subscription refresh downloaded new data and reconciled Mihomo runtime."
            if result["applied"]
            else "Subscription refresh completed with no active Mihomo config changes."
        )
        details = {
            "stage": result["stage"],
            "applied": result["applied"],
            "promoted": result["promoted"],
            "container_restarted": result["container_restarted"],
            "reconcile_action": reconcile_action,
            "reconcile_reason": reconcile_reason,
            "auto_select": auto_select.get("status"),
        }
        write_operational_log(
            event_type=event_type,
            level="info" if result["ok"] else "warning",
            message=message,
            details=details,
        )
        write_technical_log(
            component="subscription",
            event_type=event_type,
            level="info" if result["ok"] else "warning",
            message=message,
            details=result,
        )
        return result

    error_code = str(
        (reconcile.get("promoted") or {}).get("error_code")
        or (reconcile.get("container") or {}).get("error_code")
        or "SUBSCRIPTION_RUNTIME_RECONCILE_FAILED"
    )
    error_message = str(
        (reconcile.get("promoted") or {}).get("error_message")
        or (reconcile.get("container") or {}).get("error_message")
        or "Subscription refresh failed while applying Mihomo runtime changes."
    )
    result = {
        **prepared,
        "ok": False,
        "stage": "apply_runtime",
        "reconcile": reconcile,
        "promoted": promoted,
        "container_restarted": container_restarted,
        "applied": False,
        "update_available": True,
        "reconcile_action": reconcile_action,
        "reconcile_reason": reconcile_reason,
        "error": {
            "code": error_code,
            "message": error_message,
        },
    }
    write_operational_log(
        event_type="subscription_refresh_apply_failed",
        level="warning",
        message="Subscription refresh failed while reconciling Mihomo runtime.",
        details={
            "stage": result["stage"],
            "error_code": error_code,
            "reconcile_action": reconcile_action,
            "reconcile_reason": reconcile_reason,
        },
    )
    write_technical_log(
        component="subscription",
        event_type="subscription_refresh_apply_failed",
        level="warning",
        message="Subscription refresh failed while reconciling Mihomo runtime.",
        details=result,
    )
    return result
