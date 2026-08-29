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
        if (
            state.get("last_failure_fingerprint") == fingerprint
            and state.get("last_failure_logged_at") is not None
            and (now - state["last_failure_logged_at"]).total_seconds()
            < suppression_seconds
        ):
            return False

        logged_at_by_fingerprint = state.setdefault("issue_logged_at_by_fingerprint", {})
        last_for_fingerprint = logged_at_by_fingerprint.get(fingerprint)
        if (
            last_for_fingerprint is not None
            and (now - last_for_fingerprint).total_seconds()
            < suppression_seconds
        ):
            state["last_failure_fingerprint"] = fingerprint
            state["last_failure_logged_at"] = last_for_fingerprint
            return False

        state["last_failure_fingerprint"] = fingerprint
        state["last_failure_logged_at"] = now
        logged_at_by_fingerprint[fingerprint] = now
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
        "active_server_id": result.get("active_server_id"),
        "allow_switch": result.get("allow_switch"),
        "action": result.get("action"),
        "traffic_signal": compact_watchdog_traffic_signal(result.get("traffic_signal")),
        "active_quality_confirmation": result.get("active_quality_confirmation"),
        "traffic_failure_confirmation": result.get("traffic_failure_confirmation"),
        "selector": result.get("selector"),
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
    )
