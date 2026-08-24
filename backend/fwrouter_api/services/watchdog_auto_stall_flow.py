from __future__ import annotations

from typing import Any

from fwrouter_api.services.watchdog_flow_deps import (
    WATCHDOG_RUNTIME_DEGRADED,
    WATCHDOG_RUNTIME_RUNNING,
    WatchdogFlowDeps,
)


def handle_stalled_traffic_auto_flow(
    deps: WatchdogFlowDeps,
    *,
    runtime_controller: Any,
    traffic_signal: dict[str, Any],
    active_server_id: str | None,
    selection_mode: str,
    runtime_state: dict[str, Any],
    reason: str,
    timeout_ms: int,
    update_ping_state: bool,
    path_key: str | None,
    allow_switch: bool,
    candidate_limit: int,
    routing: dict[str, Any] | None,
    runtime_convergence: dict[str, Any],
    vpn_adapter: dict[str, Any],
    runtime_response_fields: dict[str, Any],
    vpn_auto_state: dict[str, Any] | None,
) -> dict[str, Any]:
    confirmation = deps.traffic_failure_confirmation(
        active_server_id=active_server_id,
        traffic_signal=traffic_signal,
        confirm_seconds=deps.get_settings().watchdog_traffic_failure_confirm_seconds,
        path_key=path_key,
    )
    if not bool(confirmation.get("confirmed")):
        updated_module = deps.update_watchdog_module(
            runtime_state=WATCHDOG_RUNTIME_RUNNING,
            status_text="Watchdog saw outbound-only VPN traffic and is waiting for confirmation.",
        )
        result = {
            "ok": True,
            "automated": True,
            "status": "traffic_failure_pending",
            "reason": reason,
            "traffic_attempts_observed": True,
            "allow_switch": False,
            "active_server_id": active_server_id,
            "active_check": None,
            "selector": None,
            "action": "none",
            "message": "Outbound-only VPN traffic was observed once; failover is pending confirmation.",
            "traffic_signal": traffic_signal,
            "traffic_failure_confirmation": confirmation,
            "safe_for_watchdog_auto": bool(traffic_signal.get("safe_for_watchdog_auto")),
            "module": updated_module,
            "routing": routing,
            "runtime_convergence": runtime_convergence,
            "vpn_adapter": vpn_adapter,
            "vpn_runtime": runtime_state,
            **runtime_response_fields,
            "vpn_auto_state": vpn_auto_state,
        }
        deps.write_watchdog_decision_log(
            level="warning",
            event_type="watchdog_switch_suppressed",
            message="Watchdog saw outbound-only VPN traffic but is waiting for confirmation before switching.",
            result=result,
            error_code="WATCHDOG_TRAFFIC_FAILURE_PENDING",
        )
        return result

    active_check = {
        "ok": False,
        "status": "traffic_stalled",
        "server_id": active_server_id,
        "error_code": "WATCHDOG_TRAFFIC_STALLED_CONFIRMED",
        "error_message": "Outbound VPN traffic had no response bytes across the confirmation window.",
        "source": "traffic_counter_snapshots",
    }

    if selection_mode == "manual":
        message = "VPN traffic stall was confirmed, but automatic failover is suppressed by manual selection mode."
        updated_module = deps.update_watchdog_module(
            runtime_state=WATCHDOG_RUNTIME_DEGRADED,
            status_text=message,
            error_code="WATCHDOG_MANUAL_SELECTION",
            error_message=message,
        )
        result = {
            "ok": True,
            "automated": True,
            "status": "manual_selection",
            "reason": reason,
            "traffic_attempts_observed": True,
            "allow_switch": False,
            "active_server_id": active_server_id,
            "active_check": active_check,
            "selector": None,
            "action": "none",
            "path_state": "confirmed_failure",
            "message": message,
            "traffic_signal": traffic_signal,
            "traffic_failure_confirmation": confirmation,
            "failover_supported": bool(runtime_state.get("failover_supported")),
            "active_target_id": active_server_id,
            **deps.cooldown_fields(None),
            "safe_for_watchdog_auto": bool(traffic_signal.get("safe_for_watchdog_auto")),
            "module": updated_module,
            "routing": routing,
            "runtime_convergence": runtime_convergence,
            "vpn_adapter": vpn_adapter,
            "vpn_runtime": runtime_state,
            **runtime_response_fields,
            "vpn_auto_state": vpn_auto_state,
        }
        deps.write_watchdog_decision_log(
            level="warning",
            event_type="watchdog_switch_suppressed",
            message="Watchdog confirmed a VPN traffic stall but manual selection mode suppresses failover.",
            result=result,
            error_code="WATCHDOG_MANUAL_SELECTION",
        )
        return result

    if not bool(runtime_state.get("failover_supported")):
        message = "VPN traffic stall was confirmed, but the active VPN runtime has no FWRouter failover adapter."
        result = {
            "ok": False,
            "status": "external_runtime_failover_unavailable",
            "reason": reason,
            "traffic_attempts_observed": True,
            "allow_switch": False,
            "active_server_id": active_server_id,
            "active_check": active_check,
            "selector": None,
            "action": "none",
            "path_state": "confirmed_failure",
            "message": message,
            "traffic_failure_confirmation": confirmation,
            "failover_supported": False,
            "active_target_id": active_server_id,
            **deps.cooldown_fields(None),
        }
        updated_module = deps.update_watchdog_module(
            runtime_state=WATCHDOG_RUNTIME_DEGRADED,
            status_text=result["message"],
            error_code="WATCHDOG_EXTERNAL_FAILOVER_UNAVAILABLE",
            error_message=result["message"],
        )
        result = {
            **result,
            "automated": True,
            "traffic_signal": traffic_signal,
            "safe_for_watchdog_auto": bool(traffic_signal.get("safe_for_watchdog_auto")),
            "module": updated_module,
            "routing": routing,
            "runtime_convergence": runtime_convergence,
            "vpn_adapter": vpn_adapter,
            "vpn_runtime": runtime_state,
            **runtime_response_fields,
            "vpn_auto_state": vpn_auto_state,
        }
        deps.write_watchdog_decision_log(
            level="warning",
            event_type="watchdog_switch_suppressed",
            message="Watchdog confirmed a VPN traffic stall but the active VPN runtime has no failover adapter.",
            result=result,
            error_code="WATCHDOG_EXTERNAL_FAILOVER_UNAVAILABLE",
        )
        return result

    cooldown = deps.failover_cooldown_status()
    if allow_switch and bool(cooldown.get("active")):
        message = "VPN traffic stall was confirmed, but automatic failover is in cooldown."
        updated_module = deps.update_watchdog_module(
            runtime_state=WATCHDOG_RUNTIME_DEGRADED,
            status_text=message,
            error_code="WATCHDOG_FAILOVER_COOLDOWN",
            error_message=message,
        )
        result = {
            "ok": True,
            "automated": True,
            "status": "failover_cooldown",
            "reason": reason,
            "traffic_attempts_observed": True,
            "allow_switch": False,
            "active_server_id": active_server_id,
            "active_check": active_check,
            "selector": None,
            "action": "none",
            "path_state": "confirmed_failure",
            "message": message,
            "traffic_signal": traffic_signal,
            "traffic_failure_confirmation": confirmation,
            "failover_cooldown": {
                "active": True,
                "cooldown_until": cooldown.get("cooldown_until"),
                "remaining_seconds": cooldown.get("remaining_seconds"),
            },
            "failover_supported": bool(runtime_state.get("failover_supported")),
            "active_target_id": active_server_id,
            **deps.cooldown_fields({
                "active": True,
                "cooldown_until": cooldown.get("cooldown_until"),
                "remaining_seconds": cooldown.get("remaining_seconds"),
            }),
            "safe_for_watchdog_auto": bool(traffic_signal.get("safe_for_watchdog_auto")),
            "module": updated_module,
            "routing": routing,
            "runtime_convergence": runtime_convergence,
            "vpn_adapter": vpn_adapter,
            "vpn_runtime": runtime_state,
            "path_key": path_key,
            "selection_mode": selection_mode,
            "vpn_auto_state": vpn_auto_state,
        }
        deps.write_watchdog_decision_log(
            level="warning",
            event_type="watchdog_switch_suppressed",
            message="Watchdog confirmed a VPN traffic stall but automatic failover is in cooldown.",
            result=result,
            error_code="WATCHDOG_FAILOVER_COOLDOWN",
        )
        return result

    failover = runtime_controller.failover(
        apply=allow_switch,
        reason=reason,
        update_ping_state=update_ping_state,
        candidate_limit=candidate_limit,
        timeout_ms=timeout_ms,
    )
    selector = failover.get("selector")

    if failover["ok"]:
        cooldown_state = None
        if allow_switch and bool(failover.get("applied")):
            current_mode = deps.routing_mode(deps.load_routing_state())
            if current_mode in {"vpn", "selective"}:
                deps.set_global_mode(current_mode, requested_by="watchdog_failover")
            cooldown_state = deps.record_successful_failover(
                path_key=path_key,
                previous_target_id=str(failover.get("previous_target_id") or active_server_id or "") or None,
                selected_target_id=str(failover.get("selected_target_id") or "") or None,
                cooldown_seconds=deps.get_settings().watchdog_failover_cooldown_seconds,
            )

        result = {
            "ok": True,
            "status": "failover_applied" if allow_switch else "failover_candidate_found",
            "reason": reason,
            "traffic_attempts_observed": True,
            "allow_switch": allow_switch,
            "active_server_id": active_server_id,
            "active_check": active_check,
            "selector": selector,
            "action": failover.get("action") or ("switch_vpn_auto" if allow_switch else "dry_run_only"),
            "path_state": "confirmed_failure",
            "message": (
                "VPN traffic stall was confirmed; failover candidate was applied."
                if allow_switch
                else "VPN traffic stall was confirmed; failover candidate found in dry-run."
            ),
            "traffic_failure_confirmation": confirmation,
            "runtime_failover": failover,
            "failover_cooldown": {
                "active": bool(cooldown_state),
                "cooldown_until": cooldown_state.get("cooldown_until") if isinstance(cooldown_state, dict) else None,
                "remaining_seconds": deps.get_settings().watchdog_failover_cooldown_seconds if cooldown_state else 0,
            },
            "failover_supported": bool(runtime_state.get("failover_supported")),
            "active_target_id": active_server_id,
            **deps.cooldown_fields({
                "active": bool(cooldown_state),
                "cooldown_until": cooldown_state.get("cooldown_until") if isinstance(cooldown_state, dict) else None,
                "remaining_seconds": deps.get_settings().watchdog_failover_cooldown_seconds if cooldown_state else 0,
            }),
        }
        updated_module = deps.update_watchdog_module(
            runtime_state=WATCHDOG_RUNTIME_RUNNING,
            status_text=result["message"],
        )
        return {
            **result,
            "automated": True,
            "traffic_signal": traffic_signal,
            "safe_for_watchdog_auto": bool(traffic_signal.get("safe_for_watchdog_auto")),
            "module": updated_module,
            "routing": routing,
            "runtime_convergence": runtime_convergence,
            "vpn_adapter": vpn_adapter,
            "vpn_runtime": failover.get("runtime_state") or runtime_state,
            "path_key": (failover.get("runtime_state") or runtime_state).get("path_key"),
            "selection_mode": selection_mode,
            "vpn_auto_state": (failover.get("runtime_state") or {}).get("selector_state") or vpn_auto_state,
        }

    result = {
        "ok": False,
        "status": "fail_open_direct_recommended",
        "reason": reason,
        "traffic_attempts_observed": True,
        "allow_switch": allow_switch,
        "active_server_id": active_server_id,
        "active_check": active_check,
        "selector": selector,
        "action": "fail_open_direct_recommended",
        "path_state": "confirmed_failure",
        "message": "VPN traffic stall was confirmed and no working failover candidate was found.",
        "traffic_failure_confirmation": confirmation,
        "runtime_failover": failover,
        "failover_supported": bool(runtime_state.get("failover_supported")),
        "active_target_id": active_server_id,
        **deps.cooldown_fields(None),
    }
    updated_module = deps.update_watchdog_module(
        runtime_state=WATCHDOG_RUNTIME_DEGRADED,
        status_text=result["message"],
        error_code="WATCHDOG_FAIL_OPEN_DIRECT_RECOMMENDED",
        error_message=result["message"],
    )
    result = {
        **result,
        "automated": True,
        "traffic_signal": traffic_signal,
        "safe_for_watchdog_auto": bool(traffic_signal.get("safe_for_watchdog_auto")),
        "module": updated_module,
        "routing": routing,
        "runtime_convergence": runtime_convergence,
        "vpn_adapter": vpn_adapter,
        "vpn_runtime": failover.get("runtime_state") or runtime_state,
        "path_key": (failover.get("runtime_state") or runtime_state).get("path_key"),
        "selection_mode": selection_mode,
        "vpn_auto_state": (failover.get("runtime_state") or {}).get("selector_state") or vpn_auto_state,
    }
    deps.write_watchdog_decision_log(
        level="error",
        event_type="watchdog_switch_suppressed",
        message="Watchdog confirmed a VPN traffic stall but found no working failover candidate.",
        result=result,
        error_code="WATCHDOG_FAIL_OPEN_DIRECT_RECOMMENDED",
    )
    return result
