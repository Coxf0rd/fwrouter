from __future__ import annotations

from typing import Any
from uuid import uuid4

from fwrouter_api.services.watchdog_failure_state import (
    get_recovery_pending,
    set_recovery_pending,
)

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
    pending_attempt = get_recovery_pending()
    distinct_post_reselect_stall = bool(
        isinstance(pending_attempt, dict)
        and str(pending_attempt.get("phase") or "") == "traffic_verifying"
        and str(pending_attempt.get("path_key") or "") == str(path_key or "")
        and str(pending_attempt.get("logical_server_id") or "") == str(active_server_id or "")
        and str(traffic_signal.get("decision_id") or "")
        and str(traffic_signal.get("decision_id") or "") != str(pending_attempt.get("traffic_decision_id") or "")
    )
    if distinct_post_reselect_stall:
        confirmation = {
            "confirmed": True,
            "pending": False,
            "reason": "post_reselection_stall_confirmed",
            "path_key": path_key,
            "server_id": active_server_id,
            "decision_id": traffic_signal.get("decision_id"),
            "stalled_snapshots": 1,
            "stalled_snapshots_required": 1,
        }
    else:
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

    pending = get_recovery_pending()
    pending_matches = bool(
        isinstance(pending, dict)
        and str(pending.get("path_key") or "") == str(path_key or "")
        and str(pending.get("logical_server_id") or "") == str(active_server_id or "")
    )
    if pending_matches:
        decision_id = str(traffic_signal.get("decision_id") or "")
        pending_decision_id = str(pending.get("traffic_decision_id") or "")
        if decision_id and decision_id == pending_decision_id:
            return {
                "ok": True,
                "automated": True,
                "status": "member_reselection_pending",
                "reason": reason,
                "traffic_attempts_observed": True,
                "allow_switch": False,
                "active_server_id": active_server_id,
                "active_check": active_check,
                "selector": None,
                "action": "none",
                "path_state": "member_reselection_pending",
                "message": "Member reselection was requested; watchdog is waiting for the next traffic observation.",
                "traffic_signal": traffic_signal,
                "traffic_failure_confirmation": confirmation,
                "runtime_recovery": {"pending": pending},
                "failover_supported": bool(runtime_state.get("failover_supported")),
                "active_target_id": active_server_id,
                **deps.cooldown_fields(None),
                "safe_for_watchdog_auto": bool(traffic_signal.get("safe_for_watchdog_auto")),
                "module": deps.update_watchdog_module(
                    runtime_state=WATCHDOG_RUNTIME_DEGRADED,
                    status_text="Watchdog is waiting for traffic evidence after member reselection.",
                ),
                "routing": routing,
                "runtime_convergence": runtime_convergence,
                "vpn_adapter": vpn_adapter,
                "vpn_runtime": runtime_state,
                "vpn_auto_state": vpn_auto_state,
            }
        # A new stalled observation means member recovery did not help. Keep
        # the persisted attempt and advance it to the full-refresh phase;
        # traffic confirmation state is intentionally stored separately.
        set_recovery_pending({
            **pending,
            "phase": "full_refresh_pending",
            "last_traffic_decision_id": decision_id,
        })

    current_topology = None
    reselection = pending.get("reselection") if pending_matches and isinstance(pending, dict) else None
    active_member = None
    try:
        from fwrouter_api.services.logical_topology import get_logical_topology
        current_topology = get_logical_topology(active_server_id) if active_server_id else None
        active_member = (current_topology or {}).get("active_member_id")
    except Exception:
        active_member = None
    if not pending_matches:
        generation = uuid4().hex
        attempt_id = uuid4().hex
        set_recovery_pending({
            "phase": "member_reselect_pending",
            "generation": generation,
            "attempt_id": attempt_id,
            "path_key": path_key,
            "logical_server_id": active_server_id,
            "traffic_decision_id": traffic_signal.get("decision_id"),
        })
        reselection = runtime_controller.request_member_reselection(
            logical_server_id=active_server_id,
            exclude_member_runtime_identity=next(
                (
                    str(item.get("runtime_name"))
                    for item in (current_topology or {}).get("members", [])
                    if item.get("member_id") == active_member
                ),
                None,
            ),
            reason=reason,
        )

    # A runtime command is not evidence of recovery.  Require a distinct,
    # fresh traffic observation after the command; latency/probe success is
    # deliberately not considered here.
    verification_signal = traffic_signal
    fresh_verification = bool(
        pending_matches
        and verification_signal.get("authoritative")
        and verification_signal.get("response_observed")
        and verification_signal.get("decision_id")
        and verification_signal.get("decision_id") != str((pending or {}).get("traffic_decision_id") or "")
    )
    recovery = {
        "ok": bool((reselection or {}).get("ok")),
        "supported": bool((reselection or {}).get("supported")),
        "member_reselection": reselection,
        "traffic_verification": verification_signal,
        "traffic_recovered": fresh_verification,
        "previous_member_runtime_identity": active_member,
    }
    if fresh_verification:
        set_recovery_pending(None)
        deps.reset_traffic_failure_candidate()
        message = (
            "VPN traffic stall was confirmed; the current logical server recovered after runtime member reselection."
        )
        updated_module = deps.update_watchdog_module(
            runtime_state=WATCHDOG_RUNTIME_RUNNING,
            status_text=message,
        )
        result = {
            "ok": True,
            "automated": True,
            "status": "logical_group_recovered",
            "reason": reason,
            "traffic_attempts_observed": True,
            "allow_switch": False,
            "active_server_id": active_server_id,
            "active_check": {"source": "traffic_counter_snapshots", "traffic_signal": verification_signal},
            "selector": None,
            "action": "member_reselect_traffic_recovered",
            "path_state": "recovered_current_logical_server",
            "message": message,
            "traffic_signal": traffic_signal,
            "traffic_failure_confirmation": confirmation,
            "runtime_recovery": recovery,
            "failover_supported": bool(runtime_state.get("failover_supported")),
            "active_target_id": active_server_id,
            **deps.cooldown_fields(None),
            "safe_for_watchdog_auto": bool(traffic_signal.get("safe_for_watchdog_auto")),
            "module": updated_module,
            "routing": routing,
            "runtime_convergence": runtime_convergence,
            "vpn_adapter": vpn_adapter,
            "vpn_runtime": runtime_controller.get_state(),
            "selection_mode": selection_mode,
            "vpn_auto_state": vpn_auto_state,
        }
        deps.write_watchdog_decision_log(
            level="info",
            event_type="watchdog_runtime_recovered",
            message=message,
            result=result,
            error_code=None,
        )
        return result

    if not pending_matches and bool((reselection or {}).get("ok")):
        set_recovery_pending(
            {
                "phase": "traffic_verifying",
                "generation": generation,
                "attempt_id": attempt_id,
                "path_key": path_key,
                "logical_server_id": active_server_id,
                "traffic_decision_id": traffic_signal.get("decision_id"),
                "reselection": reselection,
            }
        )
        return {
            "ok": True,
            "automated": True,
            "status": "member_reselection_pending",
            "reason": reason,
            "traffic_attempts_observed": True,
            "allow_switch": False,
            "active_server_id": active_server_id,
            "active_check": active_check,
            "selector": None,
            "action": "member_reselect",
            "path_state": "member_reselection_pending",
            "message": "Member reselection was requested; watchdog is waiting for a fresh traffic observation.",
            "traffic_signal": traffic_signal,
            "traffic_failure_confirmation": confirmation,
            "runtime_recovery": recovery,
            "failover_supported": bool(runtime_state.get("failover_supported")),
            "active_target_id": active_server_id,
            **deps.cooldown_fields(None),
            "safe_for_watchdog_auto": bool(traffic_signal.get("safe_for_watchdog_auto")),
            "module": deps.update_watchdog_module(
                runtime_state=WATCHDOG_RUNTIME_DEGRADED,
                status_text="Watchdog is waiting for traffic evidence after member reselection.",
            ),
            "routing": routing,
            "runtime_convergence": runtime_convergence,
            "vpn_adapter": vpn_adapter,
            "vpn_runtime": runtime_state,
            "vpn_auto_state": vpn_auto_state,
        }

    # Member recovery did not produce fresh response traffic. Refresh all
    # vpn-auto logical groups/members through the adapter before invoking the
    # existing selector.  The selector may use the resulting latency/health
    # as candidate ranking, but traffic remains the watchdog trigger.
    set_recovery_pending({
        **(pending or {}),
        "phase": "full_refresh_pending",
        "generation": str((pending or {}).get("generation") or uuid4().hex),
        "attempt_id": str((pending or {}).get("attempt_id") or uuid4().hex),
        "path_key": path_key,
        "logical_server_id": active_server_id,
    })
    full_refresh = runtime_controller.full_health_refresh(
        timeout_ms=timeout_ms,
        reason=f"{reason}:group_unavailable",
    )

    if not bool((full_refresh or {}).get("ok")):
        pending_state = get_recovery_pending() or {}
        message = "VPN traffic stall was confirmed, but the runtime health refresh did not complete; selector is waiting for fresh health state."
        updated_module = deps.update_watchdog_module(
            runtime_state=WATCHDOG_RUNTIME_DEGRADED,
            status_text=message,
            error_code="WATCHDOG_HEALTH_REFRESH_PENDING",
            error_message=message,
        )
        result = {
            "ok": False,
            "automated": True,
            "status": "full_refresh_pending",
            "reason": reason,
            "traffic_attempts_observed": True,
            "allow_switch": False,
            "active_server_id": active_server_id,
            "active_check": active_check,
            "selector": None,
            "action": "none",
            "path_state": "full_refresh_pending",
            "message": message,
            "traffic_signal": traffic_signal,
            "traffic_failure_confirmation": confirmation,
            "runtime_recovery": recovery,
            "runtime_health_refresh": full_refresh,
            "recovery_pending": pending_state,
            "failover_supported": bool(runtime_state.get("failover_supported")),
            "active_target_id": active_server_id,
            **deps.cooldown_fields(None),
            "safe_for_watchdog_auto": bool(traffic_signal.get("safe_for_watchdog_auto")),
            "module": updated_module,
            "routing": routing,
            "runtime_convergence": runtime_convergence,
            "vpn_adapter": vpn_adapter,
            "vpn_runtime": runtime_state,
            "vpn_auto_state": vpn_auto_state,
        }
        deps.write_watchdog_decision_log(
            level="warning",
            event_type="watchdog_switch_suppressed",
            message=message,
            result=result,
            error_code="WATCHDOG_HEALTH_REFRESH_PENDING",
        )
        return result

    set_recovery_pending({
        **(get_recovery_pending() or {}),
        "phase": "logical_reselect",
        "health_refresh": full_refresh,
    })
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
            "runtime_health_refresh": full_refresh,
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
        set_recovery_pending(None)
        cooldown_state = None
        failover_noop = bool(failover.get("noop")) or str(failover.get("action") or "") == "noop"
        if allow_switch and bool(failover.get("applied")) and not failover_noop:
            cooldown_state = deps.record_successful_failover(
                path_key=path_key,
                previous_target_id=str(failover.get("previous_target_id") or active_server_id or "") or None,
                selected_target_id=str(failover.get("selected_target_id") or "") or None,
                cooldown_seconds=deps.get_settings().watchdog_failover_cooldown_seconds,
            )

        status = "failover_noop" if failover_noop else ("failover_applied" if allow_switch else "failover_candidate_found")
        message = (
            "VPN traffic stall was confirmed, but the selected VPN server is already active."
            if failover_noop
            else (
                "VPN traffic stall was confirmed; failover candidate was applied."
                if allow_switch
                else "VPN traffic stall was confirmed; failover candidate found in dry-run."
            )
        )
        result = {
            "ok": True,
            "status": status,
            "reason": reason,
            "traffic_attempts_observed": True,
            "allow_switch": allow_switch,
            "active_server_id": active_server_id,
            "active_check": active_check,
            "selector": selector,
            "action": failover.get("action") or ("switch_vpn_auto" if allow_switch else "dry_run_only"),
            "noop": failover_noop,
            "noop_reason": failover.get("noop_reason") if failover_noop else None,
            "path_state": "confirmed_failure",
            "message": message,
            "traffic_failure_confirmation": confirmation,
            "runtime_failover": failover,
            "runtime_recovery": recovery,
            "runtime_health_refresh": full_refresh,
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
        "runtime_recovery": recovery,
        "runtime_health_refresh": full_refresh,
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
