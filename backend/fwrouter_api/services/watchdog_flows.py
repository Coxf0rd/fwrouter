from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


DEFAULT_WATCHDOG_TIMEOUT_MS = 10000
DEFAULT_WATCHDOG_CANDIDATE_LIMIT = 4
WATCHDOG_RUNTIME_RUNNING = "running"
WATCHDOG_RUNTIME_PAUSED = "paused"
WATCHDOG_RUNTIME_DEGRADED = "degraded"


@dataclass(frozen=True)
class WatchdogFlowDeps:
    active_watchdog_vpn_adapter: Callable[[], dict[str, Any]]
    active_quality_degraded: Callable[[dict[str, Any] | None], bool]
    cooldown_fields: Callable[[dict[str, Any] | None], dict[str, Any]]
    degraded_active_check: Callable[[dict[str, Any]], dict[str, Any]]
    detect_recent_vpn_traffic_attempts: Callable[..., dict[str, Any]]
    failover_cooldown_status: Callable[[], dict[str, Any]]
    get_last_runtime_convergence_status: Callable[..., dict[str, Any]]
    get_settings: Callable[[], Any]
    get_vpn_runtime_controller: Callable[..., Any]
    has_scoped_vpn_subjects: Callable[[], bool]
    is_core_bypass_enabled: Callable[[], bool]
    load_routing_state: Callable[[], dict[str, Any] | None]
    load_watchdog_module: Callable[[], dict[str, Any] | None]
    paused_result: Callable[..., dict[str, Any]]
    recent_successful_active_check: Callable[..., dict[str, Any] | None]
    record_successful_failover: Callable[..., dict[str, Any]]
    reset_traffic_failure_candidate: Callable[[], None]
    routing_mode: Callable[[dict[str, Any] | None], str]
    runtime_response_fields: Callable[..., dict[str, Any]]
    set_global_mode: Callable[..., Any]
    traffic_failure_confirmation: Callable[..., dict[str, Any]]
    update_watchdog_module: Callable[..., dict[str, Any] | None]
    watchdog_adapter_subject: Callable[..., str | None]
    write_watchdog_decision_log: Callable[..., None]
    write_watchdog_operational_event: Callable[..., None]


def run_vpn_watchdog_check(
    deps: WatchdogFlowDeps,
    *,
    traffic_attempts_observed: bool = False,
    allow_switch: bool = False,
    update_ping_state: bool = True,
    timeout_ms: int = DEFAULT_WATCHDOG_TIMEOUT_MS,
    candidate_limit: int = DEFAULT_WATCHDOG_CANDIDATE_LIMIT,
    reason: str = "manual_watchdog_check",
    log_events: bool = False,
) -> dict[str, Any]:
    """Evaluate VPN watchdog state.

    This function intentionally does not treat "no traffic" as failure.
    A failure can only be evaluated when the caller tells us that attempts
    through the active VPN dataplane adapter were observed.

    With managed Mihomo it can check/switch vpn-auto. With an external VPN
    adapter it never calls Mihomo selector APIs.
    """

    vpn_adapter = deps.active_watchdog_vpn_adapter()
    runtime_controller = deps.get_vpn_runtime_controller(vpn_adapter, routing=deps.load_routing_state())
    runtime_state = runtime_controller.get_state()
    active_server_id = str(runtime_state.get("active_target_id") or "").strip() or None
    if not bool(runtime_state.get("ready")):
        return {
            "ok": False,
            "status": "runtime_unavailable",
            "reason": reason,
            "traffic_attempts_observed": traffic_attempts_observed,
            "allow_switch": allow_switch,
            "active_server_id": active_server_id,
            "active_check": None,
            "selector": None,
            "action": "none",
            "vpn_adapter": vpn_adapter,
            "vpn_runtime": runtime_state,
            "path_key": runtime_state.get("path_key"),
            "error_code": "WATCHDOG_RUNTIME_UNAVAILABLE",
            "error_message": str(vpn_adapter.get("error_message") or "VPN dataplane adapter is not ready."),
            "message": "VPN runtime is unavailable; watchdog suppressed server switching.",
        }

    if not bool(runtime_state.get("failover_supported")) and not bool(runtime_state.get("probe_supported")):
        if not traffic_attempts_observed:
            result = {
                "ok": True,
                "status": "no_failure_no_traffic",
                "reason": reason,
                "traffic_attempts_observed": False,
                "allow_switch": False,
                "active_server_id": active_server_id or deps.watchdog_adapter_subject(vpn_adapter),
                "active_check": None,
                "selector": None,
                "action": "none",
                "vpn_adapter": vpn_adapter,
                "vpn_runtime": runtime_state,
                "path_key": runtime_state.get("path_key"),
                "message": "No VPN traffic attempts observed; watchdog does not treat idle external runtime as failure.",
            }
            if log_events:
                deps.write_watchdog_operational_event(
                    event_type="vpn_watchdog_no_traffic",
                    level="info",
                    message=result["message"],
                    details=result,
                )
            return result
        return {
            "ok": True,
            "status": "external_runtime_active",
            "reason": reason,
            "traffic_attempts_observed": True,
            "allow_switch": False,
            "active_server_id": active_server_id or deps.watchdog_adapter_subject(vpn_adapter),
            "active_check": {
                "ok": True,
                "status": "external_runtime_ready",
                "source": "vpn_dataplane_adapter",
            },
            "selector": None,
            "action": "none",
            "vpn_adapter": vpn_adapter,
            "vpn_runtime": runtime_state,
            "path_key": runtime_state.get("path_key"),
            "message": "External VPN runtime is active; watchdog did not run Mihomo selector checks.",
        }

    # If no server is active, we MUST select one to boot the system.
    # OR if we have traffic, we must check the active server's health.
    if active_server_id is None or traffic_attempts_observed:
        # Pass-through to the health check and failover logic
        pass
    else:
        # We have a server and no traffic, so assume it's idle and healthy.
        result = {
            "ok": True,
            "status": "no_failure_no_traffic",
            "reason": reason,
            "traffic_attempts_observed": False,
            "allow_switch": allow_switch,
            "active_server_id": active_server_id,
            "active_check": None,
            "selector": None,
            "action": "none",
            "vpn_adapter": vpn_adapter,
            "vpn_runtime": runtime_state,
            "path_key": runtime_state.get("path_key"),
            "message": "No VPN-auto traffic attempts observed; watchdog does not treat idle state as failure.",
        }
        if log_events:
            deps.write_watchdog_operational_event(
                event_type="vpn_watchdog_no_traffic",
                level="info",
                message=result["message"],
                details=result,
            )
        return result

    if active_server_id is None:
        selector_result = runtime_controller.initial_select(
            apply=allow_switch,
            reason=reason,
            update_ping_state=update_ping_state,
            candidate_limit=candidate_limit,
            timeout_ms=timeout_ms,
        )
        selector = selector_result.get("selector")
        return {
            "ok": bool(selector_result.get("ok")),
            "status": "initial_auto_selected" if allow_switch and selector_result.get("ok") else "needs_initial_auto_selection",
            "reason": reason,
            "traffic_attempts_observed": traffic_attempts_observed,
            "allow_switch": allow_switch,
            "active_server_id": selector_result.get("selected_target_id"),
            "active_check": None,
            "selector": selector,
            "action": selector_result.get("action") or "none",
            "vpn_adapter": vpn_adapter,
            "vpn_runtime": selector_result.get("runtime_state") or runtime_state,
            "path_key": (selector_result.get("runtime_state") or runtime_state).get("path_key"),
            "message": (
                "Watchdog bootstrap selected a valid vpn-auto server."
                if allow_switch and selector_result.get("ok")
                else "VPN-auto has no valid active server selected."
            ),
        }

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
    if active_check is None:
        active_check = {
            "ok": False,
            "status": "probe_unavailable",
            "server_id": active_server_id,
            "error_code": "WATCHDOG_PROBE_UNAVAILABLE",
            "error_message": "Active VPN runtime does not expose current-server quality checks.",
        }

    if active_check["ok"]:
        result = {
            "ok": True,
            "status": "healthy",
            "reason": reason,
            "traffic_attempts_observed": True,
            "allow_switch": allow_switch,
            "active_server_id": active_server_id,
            "active_check": active_check,
            "selector": None,
            "action": "none",
            "vpn_adapter": vpn_adapter,
            "vpn_runtime": runtime_state,
            "path_key": runtime_state.get("path_key"),
            "message": "VPN-auto traffic attempts observed and active server check succeeded.",
        }

        if log_events:
            deps.write_watchdog_operational_event(
                event_type="vpn_watchdog_healthy",
                level="info",
                message=result["message"],
                details=result,
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
        # After a successful switch, we must trigger a dataplane reconciliation
        # to ensure routing rules are updated for the new reality.
        if allow_switch:
            current_mode = deps.routing_mode(deps.load_routing_state())
            if current_mode in {"vpn", "selective"}:
                deps.set_global_mode(current_mode, requested_by="watchdog_failover")

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
            "vpn_adapter": vpn_adapter,
            "vpn_runtime": failover.get("runtime_state") or runtime_state,
            "path_key": (failover.get("runtime_state") or runtime_state).get("path_key"),
            "runtime_failover": failover,
            "message": (
                "VPN-auto active check failed; failover candidate was applied."
                if allow_switch
                else "VPN-auto active check failed; failover candidate found in dry-run."
            ),
        }

        if log_events:
            deps.write_watchdog_operational_event(
                event_type="vpn_watchdog_failover",
                level="warning",
                message=result["message"],
                details=result,
            )

        return result

    result = {
        "ok": False,
        "status": "fail_open_direct_recommended",
        "reason": reason,
        "traffic_attempts_observed": True,
        "allow_switch": allow_switch,
        "active_server_id": active_server_id,
        "active_check": active_check,
        "selector": selector,
        "action": "none" if failover.get("error_code") == "WATCHDOG_FAILOVER_UNAVAILABLE" else "fail_open_direct_recommended",
        "vpn_adapter": vpn_adapter,
        "vpn_runtime": failover.get("runtime_state") or runtime_state,
        "path_key": (failover.get("runtime_state") or runtime_state).get("path_key"),
        "runtime_failover": failover,
        "message": (
            "VPN runtime does not expose automatic failover."
            if failover.get("error_code") == "WATCHDOG_FAILOVER_UNAVAILABLE"
            else "VPN-auto active check failed and no working failover candidate was found."
        ),
    }

    if log_events:
        deps.write_watchdog_operational_event(
            event_type="vpn_watchdog_fail_open_direct",
            level="error",
            message=result["message"],
            details=result,
        )

    return result




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
        deps.reset_traffic_failure_candidate()
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
            message = (
                "VPN traffic has responses, but current-server quality check is degraded; "
                "automatic failover is suppressed."
            )
            updated_module = deps.update_watchdog_module(
                runtime_state=WATCHDOG_RUNTIME_DEGRADED,
                status_text=message,
                error_code="WATCHDOG_ACTIVE_QUALITY_DEGRADED_TRAFFIC_HEALTHY",
                error_message=message,
            )
            result = {
                "ok": True,
                "automated": True,
                "status": "active_quality_degraded_traffic_healthy",
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
                "selection_mode": selection_mode,
                "vpn_auto_state": vpn_auto_state,
            }
            deps.write_watchdog_decision_log(
                level="warning",
                event_type="watchdog_switch_suppressed",
                message="Watchdog suppressed VPN-auto failover because real VPN response traffic is still healthy.",
                result=result,
                error_code="WATCHDOG_ACTIVE_QUALITY_DEGRADED_TRAFFIC_HEALTHY",
            )
            return result

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
    elif bool(traffic_signal.get("traffic_stalled")):
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

