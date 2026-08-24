from __future__ import annotations

from fwrouter_api.services.watchdog_flow_deps import (
    DEFAULT_WATCHDOG_CANDIDATE_LIMIT,
    DEFAULT_WATCHDOG_TIMEOUT_MS,
    WATCHDOG_RUNTIME_DEGRADED,
    WATCHDOG_RUNTIME_PAUSED,
    WATCHDOG_RUNTIME_RUNNING,
    WatchdogFlowDeps,
)
from fwrouter_api.services.watchdog_auto_active_quality_flow import handle_response_traffic_auto_flow
from fwrouter_api.services.watchdog_auto_stall_flow import handle_stalled_traffic_auto_flow
from fwrouter_api.services.watchdog_manual_flow import run_vpn_watchdog_check


def run_vpn_watchdog_auto_check(
    deps: WatchdogFlowDeps,
    *,
    allow_switch: bool = True,
    update_ping_state: bool = True,
    timeout_ms: int = DEFAULT_WATCHDOG_TIMEOUT_MS,
    candidate_limit: int = DEFAULT_WATCHDOG_CANDIDATE_LIMIT,
    traffic_window_seconds: int | None = None,
    reason: str = "auto_watchdog_check",
    log_events: bool = False,
) -> dict[str, Any]:
    """Run watchdog with backend-owned traffic signal and module state updates."""

    module = deps.load_watchdog_module()
    routing = deps.load_routing_state()

    if module is None:
        return deps.paused_result(
            status="watchdog_module_missing",
            reason=reason,
            message="Watchdog module row is missing.",
            module=None,
            routing=routing,
        )

    if module["desired_state"] != "enabled":
        return deps.paused_result(
            status="watchdog_disabled",
            reason=reason,
            message="Watchdog automation is disabled.",
            module=module,
            routing=routing,
        )

    if deps.is_core_bypass_enabled():
        updated_module = deps.update_watchdog_module(
            runtime_state=WATCHDOG_RUNTIME_PAUSED,
            status_text="Watchdog paused because FWRouter core bypass is active.",
        )
        return deps.paused_result(
            status="paused_core_bypass",
            reason=reason,
            message="Watchdog paused because FWRouter core bypass is active.",
            module=updated_module,
            routing=routing,
        )

    mode = deps.routing_mode(routing)
    scoped_vpn_subjects = deps.has_scoped_vpn_subjects()
    if mode not in {"vpn", "selective"} and not scoped_vpn_subjects:
        updated_module = deps.update_watchdog_module(
            runtime_state=WATCHDOG_RUNTIME_PAUSED,
            status_text=f"Watchdog paused because global mode is {mode}.",
        )
        return deps.paused_result(
            status="paused_not_vpn",
            reason=reason,
            message=f"Watchdog paused because global mode is {mode}.",
            module=updated_module,
            routing=routing,
        )

    runtime_convergence = deps.get_last_runtime_convergence_status(
        mode=mode,
        scoped_vpn_subjects=scoped_vpn_subjects,
    )
    if not bool(runtime_convergence.get("ok")):
        updated_module = deps.update_watchdog_module(
            runtime_state=WATCHDOG_RUNTIME_DEGRADED,
            status_text="Watchdog could not repair selective/VPN runtime convergence.",
            error_code=runtime_convergence.get("error_code") or "WATCHDOG_RUNTIME_CONVERGENCE_FAILED",
            error_message=runtime_convergence.get("error_message")
            or "Selective/VPN runtime convergence failed.",
        )
        result = {
            "ok": False,
            "automated": True,
            "status": "runtime_convergence_failed",
            "reason": reason,
            "traffic_attempts_observed": False,
            "allow_switch": False,
            "active_server_id": (routing or {}).get("active_auto_server_id"),
            "active_check": None,
            "selector": None,
            "action": "none",
            "message": "Watchdog could not repair selective/VPN runtime convergence.",
            "traffic_signal": None,
            "safe_for_watchdog_auto": False,
            "module": updated_module,
            "routing": routing,
            "runtime_convergence": runtime_convergence,
        }
        deps.write_watchdog_decision_log(
            level="warning",
            event_type="watchdog_switch_suppressed",
            message="Watchdog did not switch VPN-auto because runtime convergence is unhealthy.",
            result=result,
            error_code=str(updated_module.get("error_code") or "WATCHDOG_RUNTIME_CONVERGENCE_FAILED"),
        )
        return result

    vpn_adapter = deps.active_watchdog_vpn_adapter()
    runtime_controller = deps.get_vpn_runtime_controller(vpn_adapter, routing=routing)
    runtime_state = runtime_controller.get_state()
    active_server_id = str(runtime_state.get("active_target_id") or deps.watchdog_adapter_subject(vpn_adapter, routing) or "").strip() or None
    path_key = str(runtime_state.get("path_key") or active_server_id or "").strip() or None
    selection_mode = str(runtime_state.get("selection_mode") or "unknown").strip().lower()
    vpn_auto_state = runtime_state.get("selector_state") if isinstance(runtime_state.get("selector_state"), dict) else None
    runtime_response_fields = deps.runtime_response_fields(
        runtime_state=runtime_state,
        path_key=path_key,
        cooldown=deps.failover_cooldown_status(),
    )
    if not bool(runtime_state.get("ready")):
        updated_module = deps.update_watchdog_module(
            runtime_state=WATCHDOG_RUNTIME_DEGRADED,
            status_text="Watchdog suppressed switching because VPN dataplane adapter is not ready.",
            error_code="WATCHDOG_RUNTIME_UNAVAILABLE",
            error_message=str(vpn_adapter.get("error_message") or "VPN dataplane adapter is not ready."),
        )
        result = {
            "ok": False,
            "automated": True,
            "status": "runtime_unavailable",
            "reason": reason,
            "traffic_attempts_observed": False,
            "allow_switch": False,
            "active_server_id": active_server_id,
            "active_check": None,
            "selector": None,
            "action": "none",
            "message": "VPN runtime is unavailable; watchdog suppressed server switching.",
            "traffic_signal": None,
            "safe_for_watchdog_auto": False,
            "module": updated_module,
            "routing": routing,
            "runtime_convergence": runtime_convergence,
            "vpn_adapter": vpn_adapter,
            "vpn_runtime": runtime_state,
            **runtime_response_fields,
        }
        deps.write_watchdog_decision_log(
            level="warning",
            event_type="watchdog_switch_suppressed",
            message="Watchdog suppressed server switching because VPN runtime is unavailable.",
            result=result,
            error_code="WATCHDOG_RUNTIME_UNAVAILABLE",
        )
        return result

    if (
        selection_mode == "auto"
        and bool(runtime_state.get("initial_select_supported"))
        and not bool(runtime_state.get("active_target_valid"))
    ):
        if allow_switch:
            initial_select = runtime_controller.initial_select(
                apply=True,
                reason=reason,
                update_ping_state=update_ping_state,
                candidate_limit=candidate_limit,
                timeout_ms=timeout_ms,
            )
            selector = initial_select.get("selector")
            if selector["ok"]:
                updated_module = deps.update_watchdog_module(
                    runtime_state=WATCHDOG_RUNTIME_RUNNING,
                    status_text="Watchdog bootstrap selected a valid vpn-auto server.",
                )
                return {
                    "ok": True,
                    "automated": True,
                    "status": "initial_auto_selected",
                    "reason": reason,
                    "traffic_attempts_observed": False,
                    "allow_switch": True,
                    "active_server_id": initial_select.get("selected_target_id") or selector.get("active_after"),
                    "active_check": None,
                    "selector": selector,
                    "action": initial_select.get("action") or "switch_vpn_auto",
                    "message": "Watchdog bootstrap selected a valid vpn-auto server without waiting for traffic attempts.",
                    "traffic_signal": None,
                    "safe_for_watchdog_auto": False,
                    "module": updated_module,
                    "routing": routing,
                    "vpn_auto_state": (initial_select.get("runtime_state") or {}).get("selector_state") or vpn_auto_state,
                    "runtime_convergence": runtime_convergence,
                    "vpn_adapter": vpn_adapter,
                    "vpn_runtime": initial_select.get("runtime_state") or runtime_state,
                    "path_key": (initial_select.get("runtime_state") or runtime_state).get("path_key"),
                    "selection_mode": selection_mode,
                    "active_target_id": initial_select.get("selected_target_id"),
                    "failover_supported": bool((initial_select.get("runtime_state") or runtime_state).get("failover_supported")),
                    **deps.cooldown_fields(deps.failover_cooldown_status()),
                    "runtime_failover": initial_select,
                }

        updated_module = deps.update_watchdog_module(
            runtime_state=WATCHDOG_RUNTIME_DEGRADED,
            status_text="VPN-auto is missing a valid active server and needs initial selection.",
            error_code="WATCHDOG_INITIAL_AUTO_SELECTION_REQUIRED",
            error_message="VPN-auto has no valid active server selected.",
        )
        result = {
            "ok": True,
            "automated": True,
            "status": "needs_initial_auto_selection",
            "reason": reason,
            "traffic_attempts_observed": False,
            "allow_switch": False,
            "active_server_id": (routing or {}).get("active_auto_server_id"),
            "active_check": None,
            "selector": None,
            "action": "none",
            "message": "VPN-auto has no valid active server selected.",
            "traffic_signal": None,
            "safe_for_watchdog_auto": False,
            "module": updated_module,
            "routing": routing,
            "vpn_auto_state": vpn_auto_state,
            "runtime_convergence": runtime_convergence,
            "vpn_adapter": vpn_adapter,
            "vpn_runtime": runtime_state,
            **runtime_response_fields,
        }
        deps.write_watchdog_decision_log(
            level="warning",
            event_type="watchdog_switch_suppressed",
            message="Watchdog did not switch VPN-auto because no valid active auto server is selected.",
            result=result,
            error_code="WATCHDOG_INITIAL_AUTO_SELECTION_REQUIRED",
        )
        return result

    traffic_signal = deps.detect_recent_vpn_traffic_attempts(
        window_seconds=traffic_window_seconds,
    )
    if not bool(traffic_signal.get("authoritative")):
        updated_module = deps.update_watchdog_module(
            runtime_state=WATCHDOG_RUNTIME_DEGRADED,
            status_text="Watchdog traffic signal is stale or unavailable; automatic switching is suppressed.",
            error_code="WATCHDOG_SIGNAL_UNAVAILABLE",
            error_message="Fresh traffic counter snapshots are required for authoritative watchdog decisions.",
        )
        result = {
            "ok": True,
            "automated": True,
            "status": "paused_signal_unavailable",
            "reason": reason,
            "traffic_attempts_observed": False,
            "allow_switch": False,
            "active_server_id": active_server_id,
            "active_check": None,
            "selector": None,
            "action": "none",
            "message": "Watchdog traffic signal is stale or unavailable; automatic switching is suppressed.",
            "traffic_signal": traffic_signal,
            "safe_for_watchdog_auto": False,
            "module": updated_module,
            "routing": routing,
            "runtime_convergence": runtime_convergence,
            "vpn_adapter": vpn_adapter,
            "vpn_runtime": runtime_state,
            **runtime_response_fields,
        }
        return result

    if not bool(traffic_signal.get("observed")):
        deps.reset_traffic_failure_candidate()
    elif bool(traffic_signal.get("response_observed")):
        return handle_response_traffic_auto_flow(
            deps,
            runtime_controller=runtime_controller,
            traffic_signal=traffic_signal,
            active_server_id=active_server_id,
            selection_mode=selection_mode,
            runtime_state=runtime_state,
            reason=reason,
            timeout_ms=timeout_ms,
            update_ping_state=update_ping_state,
            path_key=path_key,
            allow_switch=allow_switch,
            candidate_limit=candidate_limit,
            routing=routing,
            runtime_convergence=runtime_convergence,
            vpn_adapter=vpn_adapter,
            runtime_response_fields=runtime_response_fields,
            vpn_auto_state=vpn_auto_state,
        )
    elif bool(traffic_signal.get("traffic_stalled")):
        return handle_stalled_traffic_auto_flow(
            deps,
            runtime_controller=runtime_controller,
            traffic_signal=traffic_signal,
            active_server_id=active_server_id,
            selection_mode=selection_mode,
            runtime_state=runtime_state,
            reason=reason,
            timeout_ms=timeout_ms,
            update_ping_state=update_ping_state,
            path_key=path_key,
            allow_switch=allow_switch,
            candidate_limit=candidate_limit,
            routing=routing,
            runtime_convergence=runtime_convergence,
            vpn_adapter=vpn_adapter,
            runtime_response_fields=runtime_response_fields,
            vpn_auto_state=vpn_auto_state,
        )
    result = run_vpn_watchdog_check(
        deps,
        traffic_attempts_observed=traffic_signal["observed"],
        allow_switch=allow_switch,
        update_ping_state=update_ping_state,
        timeout_ms=timeout_ms,
        candidate_limit=candidate_limit,
        reason=reason,
        log_events=log_events,
    )

    if result["status"] == "no_failure_no_traffic":
        updated_module = deps.update_watchdog_module(
            runtime_state=WATCHDOG_RUNTIME_RUNNING,
            status_text="Watchdog enabled and waiting for VPN-auto traffic activity.",
        )
    elif result["status"] in {"healthy", "failover_applied", "external_runtime_active"}:
        updated_module = deps.update_watchdog_module(
            runtime_state=WATCHDOG_RUNTIME_RUNNING,
            status_text=result["message"],
        )
    elif result["status"] == "failover_candidate_found":
        updated_module = deps.update_watchdog_module(
            runtime_state=WATCHDOG_RUNTIME_DEGRADED,
            status_text=result["message"],
        )
    elif result["status"] == "runtime_unavailable":
        updated_module = deps.update_watchdog_module(
            runtime_state=WATCHDOG_RUNTIME_DEGRADED,
            status_text=result["message"],
            error_code="WATCHDOG_RUNTIME_UNAVAILABLE",
            error_message=str(result.get("error_message") or result["message"]),
        )
    elif result["status"] == "external_runtime_failover_unavailable":
        updated_module = deps.update_watchdog_module(
            runtime_state=WATCHDOG_RUNTIME_DEGRADED,
            status_text=result["message"],
            error_code="WATCHDOG_EXTERNAL_FAILOVER_UNAVAILABLE",
            error_message=result["message"],
        )
    else:
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
        "safe_for_watchdog_auto": bool((traffic_signal or {}).get("safe_for_watchdog_auto")),
        "module": updated_module,
        "routing": routing,
        "runtime_convergence": runtime_convergence,
        "vpn_adapter": result.get("vpn_adapter") or vpn_adapter,
    }
    if result["status"] in {
        "failover_candidate_found",
        "fail_open_direct_recommended",
        "runtime_unavailable",
        "external_runtime_failover_unavailable",
    }:
        deps.write_watchdog_decision_log(
            level="error" if result["status"] == "fail_open_direct_recommended" else "warning",
            event_type="watchdog_switch_suppressed",
            message=(
                "Watchdog active check failed but did not apply a server switch."
                if result["status"] == "failover_candidate_found"
                else "Watchdog suppressed server switching because VPN runtime is unavailable."
                if result["status"] == "runtime_unavailable"
                else "Watchdog confirmed a VPN traffic stall but the external VPN adapter has no failover adapter."
                if result["status"] == "external_runtime_failover_unavailable"
                else "Watchdog active check failed and found no working failover candidate."
            ),
            result=result,
            error_code=(
                "WATCHDOG_FAIL_OPEN_DIRECT_RECOMMENDED"
                if result["status"] == "fail_open_direct_recommended"
                else "WATCHDOG_RUNTIME_UNAVAILABLE"
                if result["status"] == "runtime_unavailable"
                else "WATCHDOG_EXTERNAL_FAILOVER_UNAVAILABLE"
                if result["status"] == "external_runtime_failover_unavailable"
                else "WATCHDOG_DRY_RUN_ONLY"
            ),
        )
    return result
