from __future__ import annotations

from typing import Any

from fwrouter_api.services.watchdog_flow_deps import (
    WATCHDOG_RUNTIME_DEGRADED,
    WATCHDOG_RUNTIME_RUNNING,
    WatchdogFlowDeps,
)


def handle_response_traffic_auto_flow(
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
    deps.reset_stalled_traffic_failure_candidate()
    active_check = None
    if (
        selection_mode == "auto"
        and bool(runtime_state.get("probe_supported"))
        and active_server_id
    ):
        checked_by = f"watchdog_active_check:{reason}"
        active_check = deps.recent_successful_active_check(
            server_id=active_server_id,
            checked_by=checked_by,
            timeout_ms=timeout_ms,
        )
        if active_check is None:
            active_check = runtime_controller.probe(
                update_ping_state=update_ping_state,
                timeout_ms=timeout_ms,
                reason=reason,
            )

    if active_check is not None and deps.active_quality_degraded(active_check):
        active_check = deps.degraded_active_check(active_check)
        confirmation = deps.active_quality_degraded_confirmation(
            active_server_id=active_server_id,
            active_check=active_check,
            traffic_signal=traffic_signal,
            confirm_seconds=deps.get_settings().watchdog_active_quality_confirm_seconds,
            bad_checks_required=deps.get_settings().watchdog_active_quality_bad_checks,
            window_checks=deps.get_settings().watchdog_active_quality_window_checks,
            window_bad_checks=deps.get_settings().watchdog_active_quality_window_bad_checks,
            path_key=path_key,
        )
        if not bool(confirmation.get("confirmed")):
            pending = bool(confirmation.get("pending"))
            expedited_delay = (
                deps.get_settings().watchdog_suspicious_interval_seconds
                if pending
                and bool(traffic_signal.get("outbound_observed"))
                and bool(traffic_signal.get("safe_for_watchdog_auto"))
                else None
            )
            message = (
                "VPN traffic has responses, but current-server quality is degraded; "
                "watchdog is observing before failover."
                if pending
                else (
                    "VPN traffic has responses, but current-server quality check is degraded; "
                    "automatic failover is suppressed until degradation persists."
                )
            )
            updated_module = deps.update_watchdog_module(
                runtime_state=WATCHDOG_RUNTIME_DEGRADED,
                status_text=message,
                error_code=(
                    "WATCHDOG_ACTIVE_QUALITY_DEGRADED_PENDING"
                    if pending
                    else "WATCHDOG_ACTIVE_QUALITY_DEGRADED_TRAFFIC_HEALTHY"
                ),
                error_message=message,
            )
            result = {
                "ok": True,
                "automated": True,
                "status": (
                    "active_quality_degraded_pending"
                    if pending
                    else "active_quality_degraded_traffic_healthy"
                ),
                "reason": reason,
                "traffic_attempts_observed": True,
                "allow_switch": False,
                "active_server_id": active_server_id,
                "active_check": active_check,
                "selector": None,
                "action": "none",
                "path_state": "degraded_active_quality",
                "message": message,
                "traffic_signal": traffic_signal,
                "active_quality_confirmation": confirmation,
                "failover_supported": bool(runtime_state.get("failover_supported")),
                "active_target_id": active_server_id,
                "next_check_delay_seconds": expedited_delay,
                **deps.cooldown_fields(None),
                "safe_for_watchdog_auto": bool(traffic_signal.get("safe_for_watchdog_auto")),
                "module": updated_module,
                "routing": routing,
                "runtime_convergence": runtime_convergence,
                "vpn_adapter": vpn_adapter,
                "vpn_runtime": runtime_state,
                **runtime_response_fields,
                "selection_mode": selection_mode,
                "vpn_auto_state": vpn_auto_state,
            }
            deps.write_watchdog_decision_log(
                level="warning",
                event_type="watchdog_switch_suppressed",
                message=(
                    "Watchdog saw degraded active-server quality with response traffic and is waiting before failover."
                    if pending
                    else "Watchdog suppressed VPN-auto failover because real VPN response traffic is still present."
                ),
                result=result,
                error_code=(
                    "WATCHDOG_ACTIVE_QUALITY_DEGRADED_PENDING"
                    if pending
                    else "WATCHDOG_ACTIVE_QUALITY_DEGRADED_TRAFFIC_HEALTHY"
                ),
            )
            return result

        active_check = {
            **active_check,
            "error_code": active_check.get("error_code") or "WATCHDOG_ACTIVE_QUALITY_DEGRADED_CONFIRMED",
            "error_message": active_check.get("error_message") or "Active VPN-auto server quality stayed degraded across the confirmation window.",
            "source": "active_quality_check",
        }

        if selection_mode == "manual":
            message = "Active VPN-auto server quality degradation was confirmed, but automatic failover is suppressed by manual selection mode."
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
                "path_state": "confirmed_active_quality_degraded",
                "message": message,
                "traffic_signal": traffic_signal,
                "active_quality_confirmation": confirmation,
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
                message="Watchdog confirmed degraded active-server quality but manual selection mode suppresses failover.",
                result=result,
                error_code="WATCHDOG_MANUAL_SELECTION",
            )
            return result

        if not bool(runtime_state.get("failover_supported")):
            message = "Active VPN-auto server quality degradation was confirmed, but the active VPN runtime has no FWRouter failover adapter."
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
                "path_state": "confirmed_active_quality_degraded",
                "message": message,
                "active_quality_confirmation": confirmation,
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
                message="Watchdog confirmed degraded active-server quality but the active VPN runtime has no failover adapter.",
                result=result,
                error_code="WATCHDOG_EXTERNAL_FAILOVER_UNAVAILABLE",
            )
            return result

        cooldown = deps.failover_cooldown_status()
        if allow_switch and bool(cooldown.get("active")):
            message = "Active VPN-auto server quality degradation was confirmed, but automatic failover is in cooldown."
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
                "path_state": "confirmed_active_quality_degraded",
                "message": message,
                "traffic_signal": traffic_signal,
                "active_quality_confirmation": confirmation,
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
                message="Watchdog confirmed degraded active-server quality but automatic failover is in cooldown.",
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
                "path_state": "confirmed_active_quality_degraded",
                "message": (
                    "Active VPN-auto server quality degradation was confirmed; failover candidate was applied."
                    if allow_switch
                    else "Active VPN-auto server quality degradation was confirmed; failover candidate found in dry-run."
                ),
                "active_quality_confirmation": confirmation,
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
                "path_key": path_key,
                "selection_mode": selection_mode,
                "vpn_auto_state": vpn_auto_state,
            }
            deps.write_watchdog_decision_log(
                level="info" if allow_switch else "warning",
                event_type="watchdog_switch_applied" if allow_switch else "watchdog_switch_candidate",
                message=result["message"],
                result=result,
                error_code="WATCHDOG_ACTIVE_QUALITY_DEGRADED_CONFIRMED",
            )
            return result

        result = {
            "ok": False,
            "status": "no_working_candidates",
            "reason": reason,
            "traffic_attempts_observed": True,
            "allow_switch": False,
            "active_server_id": active_server_id,
            "active_check": active_check,
            "selector": selector,
            "action": "none",
            "path_state": "confirmed_active_quality_degraded",
            "message": "Active VPN-auto server quality degradation was confirmed and no working failover candidate was found.",
            "traffic_signal": traffic_signal,
            "active_quality_confirmation": confirmation,
            "runtime_failover": failover,
            "failover_supported": bool(runtime_state.get("failover_supported")),
            "active_target_id": active_server_id,
            **deps.cooldown_fields(None),
            "safe_for_watchdog_auto": bool(traffic_signal.get("safe_for_watchdog_auto")),
            "routing": routing,
            "runtime_convergence": runtime_convergence,
            "vpn_adapter": vpn_adapter,
            "vpn_runtime": runtime_state,
            **runtime_response_fields,
            "selection_mode": selection_mode,
            "vpn_auto_state": vpn_auto_state,
        }
        updated_module = deps.update_watchdog_module(
            runtime_state=WATCHDOG_RUNTIME_DEGRADED,
            status_text=result["message"],
            error_code="WATCHDOG_NO_WORKING_CANDIDATES",
            error_message=result["message"],
        )
        result["module"] = updated_module
        deps.write_watchdog_decision_log(
            level="warning",
            event_type="watchdog_switch_suppressed",
            message="Watchdog confirmed degraded active-server quality but found no working failover candidate.",
            result=result,
            error_code="WATCHDOG_NO_WORKING_CANDIDATES",
        )
        return result

    if active_check is not None:
        deps.active_quality_recovery_confirmation(
            active_server_id=active_server_id,
            traffic_signal=traffic_signal,
            recovery_checks_required=deps.get_settings().watchdog_active_quality_recovery_checks,
            window_checks=deps.get_settings().watchdog_active_quality_window_checks,
            path_key=path_key,
        )

    updated_module = deps.update_watchdog_module(
        runtime_state=WATCHDOG_RUNTIME_RUNNING,
        status_text="Watchdog saw VPN traffic responses and current-server quality is healthy.",
    )
    return {
        "ok": True,
        "automated": True,
        "status": "healthy_traffic",
        "reason": reason,
        "traffic_attempts_observed": True,
        "allow_switch": False,
        "active_server_id": active_server_id,
        "active_check": active_check,
        "selector": None,
        "action": "none",
        "next_check_delay_seconds": None,
        "message": "VPN traffic has response bytes and current-server quality check is healthy.",
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
