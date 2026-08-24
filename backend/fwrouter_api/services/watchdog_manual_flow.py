from __future__ import annotations

from fwrouter_api.services.watchdog_flow_deps import (
    DEFAULT_WATCHDOG_CANDIDATE_LIMIT,
    DEFAULT_WATCHDOG_TIMEOUT_MS,
    WatchdogFlowDeps,
)


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
