from __future__ import annotations

import json
from datetime import datetime
from threading import Lock
from typing import Any, Callable

from fwrouter_api.services.logs import write_technical_log

WATCHDOG_NOOP_SUPPRESSED_STATUSES = {
    "active_quality_degraded_pending",
    "active_quality_degraded_traffic_healthy",
    "no_failure_no_traffic",
    "paused_not_vpn",
    "paused_core_bypass",
    "paused_signal_unavailable",
    "traffic_failure_pending",
    "watchdog_disabled",
    "watchdog_module_missing",
}


def compact_watchdog_traffic_signal(signal: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(signal, dict):
        return None
    keys = (
        "decision_id",
        "source",
        "path_state",
        "response_source",
        "window_seconds",
        "observed",
        "response_observed",
        "traffic_stalled",
        "authoritative",
        "safe_for_watchdog_auto",
        "last_collected_at",
        "last_fresh_sample_at",
        "rx_delta",
        "tx_delta",
    )
    return {key: signal.get(key) for key in keys if key in signal}


def watchdog_decision_fingerprint(details: dict[str, Any]) -> str:
    recovery = details.get("runtime_recovery") if isinstance(details.get("runtime_recovery"), dict) else {}
    pending = details.get("recovery_pending") if isinstance(details.get("recovery_pending"), dict) else {}
    pending_recovery = recovery.get("pending") if isinstance(recovery.get("pending"), dict) else {}
    reselection = recovery.get("reselection") if isinstance(recovery.get("reselection"), dict) else {}
    if not reselection:
        reselection = recovery.get("member_reselection") if isinstance(recovery.get("member_reselection"), dict) else {}
    refresh = details.get("runtime_health_refresh") if isinstance(details.get("runtime_health_refresh"), dict) else {}
    failover = details.get("runtime_failover") if isinstance(details.get("runtime_failover"), dict) else {}
    return json.dumps(
        {
            "event_type": details.get("event_type"),
            "status": details.get("status"),
            "error_code": details.get("error_code"),
            "active_server_id": details.get("active_server_id"),
            "message": details.get("message") or details.get("error_message"),
            "selector_error": (
                details.get("selector", {}).get("error_message")
                if isinstance(details.get("selector"), dict)
                else None
            ),
            "action": details.get("action"),
            "phase": details.get("phase"),
            "recovery_attempt_id": details.get("recovery_attempt_id"),
            "workflow_id": details.get("workflow_id"),
            "outcome": details.get("outcome"),
            "recovery_phase": details.get("phase") or pending.get("phase") or pending_recovery.get("phase"),
            "reselection": {
                key: reselection.get(key)
                for key in ("ok", "status", "action", "old_member_id", "new_member_id", "member_id", "error_code")
                if key in reselection
            },
            "health_refresh": {
                key: refresh.get(key)
                for key in ("ok", "status", "outcome", "error_code", "probed", "healthy", "failed")
                if key in refresh
            },
            "failover": {
                key: failover.get(key)
                for key in ("ok", "applied", "action", "status", "error_code", "previous_target_id", "selected_target_id")
                if key in failover
            },
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def should_write_watchdog_issue_log(
    fingerprint: str,
    *,
    now_fn: Callable[[], datetime],
    lock: Lock,
    state: dict[str, Any],
    suppression_seconds: int,
) -> bool:
    now = now_fn()
    with lock:
        if state.get("last_failure_fingerprint") == fingerprint:
            return False
        state.setdefault("issue_logged_at_by_fingerprint", {})[fingerprint] = now
        state["last_failure_fingerprint"] = fingerprint
        state["last_failure_logged_at"] = now
        return True


def write_watchdog_decision_log(
    *,
    level: str,
    event_type: str,
    message: str,
    result: dict[str, Any],
    timestamp: str,
    should_write: Callable[[str], bool],
    error_code: str | None = None,
) -> None:
    status = str(result.get("status") or "").strip()
    runtime_recovery = result.get("runtime_recovery") if isinstance(result.get("runtime_recovery"), dict) else {}
    pending = result.get("recovery_pending") if isinstance(result.get("recovery_pending"), dict) else {}
    pending_recovery = runtime_recovery.get("pending") if isinstance(runtime_recovery.get("pending"), dict) else {}
    recovery_attempt_id = (
        result.get("recovery_attempt_id")
        or pending.get("attempt_id")
        or pending_recovery.get("attempt_id")
        or runtime_recovery.get("attempt_id")
    )
    workflow_id = (
        result.get("workflow_id")
        or pending.get("workflow_id")
        or pending_recovery.get("workflow_id")
        or (f"watchdog:{recovery_attempt_id}" if recovery_attempt_id else None)
    )
    effective_level = (
        "info"
        if event_type == "watchdog_switch_suppressed"
        and status in WATCHDOG_NOOP_SUPPRESSED_STATUSES
        and level == "warning"
        else level
    )
    details = {
        "event_type": event_type,
        "status": status or result.get("status"),
        "reason": result.get("reason"),
        "message": result.get("message") or message,
        "error_code": error_code or result.get("error_code"),
        "error_message": result.get("error_message") or result.get("message") or message,
        "outcome": result.get("outcome"),
        "active_server_id": result.get("active_server_id"),
        "allow_switch": result.get("allow_switch"),
        "action": result.get("action"),
        "traffic_signal": compact_watchdog_traffic_signal(result.get("traffic_signal")),
        "active_quality_confirmation": result.get("active_quality_confirmation"),
        "traffic_failure_confirmation": result.get("traffic_failure_confirmation"),
        "selector": result.get("selector"),
        "active_check": result.get("active_check"),
        "runtime_recovery": runtime_recovery or None,
        "runtime_health_refresh": result.get("runtime_health_refresh"),
        "runtime_failover": result.get("runtime_failover"),
        "recovery_pending": pending or pending_recovery or None,
        "recovery_attempt_id": recovery_attempt_id,
        "path_key": result.get("path_key"),
        "phase": (
            result.get("phase")
            or pending.get("phase")
            or pending_recovery.get("phase")
            or result.get("path_state")
        ),
        "workflow_id": workflow_id,
        "causation_id": (result.get("traffic_signal") or {}).get("decision_id") if isinstance(result.get("traffic_signal"), dict) else None,
        "timestamp": timestamp,
    }
    fingerprint = watchdog_decision_fingerprint(details)
    if not should_write(fingerprint):
        return
    write_technical_log(
        component="watchdog",
        level=effective_level,
        event_type=event_type,
        message=message,
        details=details,
        workflow_id=workflow_id,
        causation_id=details.get("causation_id"),
    )
