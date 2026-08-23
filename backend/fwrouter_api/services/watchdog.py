from __future__ import annotations

from datetime import datetime, timezone
from threading import Lock
from typing import Any

from fwrouter_api.core.config import get_settings
from fwrouter_api.services.core_bypass import is_core_bypass_enabled
from fwrouter_api.services.logs import write_technical_log
from fwrouter_api.services.runtime_convergence import get_last_runtime_convergence_status
from fwrouter_api.services.runtime_adapters import active_vpn_dataplane_adapter
from fwrouter_api.services.servers import set_global_mode
from fwrouter_api.services.vpn_runtime_control import get_vpn_runtime_controller
from fwrouter_api.services.watchdog_active_quality import (
    active_quality_degraded as _watchdog_active_quality_degraded,
    degraded_active_check as _watchdog_degraded_active_check,
    recent_successful_active_check as _recent_successful_active_check,
)
from fwrouter_api.services.watchdog_decision_logs import (
    should_write_watchdog_issue_log,
    write_watchdog_decision_log,
)
from fwrouter_api.services.watchdog_failure_state import (
    active_quality_degraded_confirmation,
    active_quality_recovery_confirmation,
    cooldown_fields as _watchdog_cooldown_fields,
    failover_cooldown_status,
    get_traffic_failure_candidate,
    record_successful_failover,
    reset_stalled_traffic_failure_candidate,
    reset_traffic_failure_candidate,
    runtime_response_fields as _watchdog_runtime_response_fields,
    set_traffic_failure_candidate,
    traffic_failure_confirmation,
)
from fwrouter_api.services.watchdog_flows import (
    WatchdogFlowDeps,
    run_vpn_watchdog_auto_check as _run_vpn_watchdog_auto_check,
    run_vpn_watchdog_check as _run_vpn_watchdog_check,
)
from fwrouter_api.services.watchdog_result_helpers import (
    paused_result,
    write_watchdog_operational_event,
)
from fwrouter_api.services.watchdog_scheduler import (
    run_scheduler_tick,
    start_scheduler,
    stop_scheduler,
)
from fwrouter_api.services.watchdog_status import (
    compute_has_scoped_vpn_subjects,
    has_scoped_vpn_subjects,
    load_routing_state,
    load_watchdog_module,
    routing_mode,
    update_watchdog_module,
)
from fwrouter_api.services.watchdog_traffic_signal import (
    detect_recent_vpn_traffic_attempts as _detect_recent_vpn_traffic_attempts,
)


DEFAULT_WATCHDOG_TIMEOUT_MS = 10000
DEFAULT_WATCHDOG_CANDIDATE_LIMIT = 4
DEFAULT_WATCHDOG_ACTIVE_CHECK_TTL_SECONDS = 60
VPN_AUTO_STATE_CACHE_TTL_SECONDS = 45

WATCHDOG_RUNTIME_RUNNING = "running"
WATCHDOG_RUNTIME_PAUSED = "paused"
WATCHDOG_RUNTIME_DEGRADED = "degraded"
WATCHDOG_RUNTIME_STOPPED = "stopped"
WATCHDOG_RUNTIME_FAILED = "failed"

_WATCHDOG_FAILURE_LOG_LOCK = Lock()
_WATCHDOG_LAST_FAILURE_FINGERPRINT: str | None = None
_WATCHDOG_LAST_FAILURE_LOGGED_AT: datetime | None = None
_WATCHDOG_ISSUE_LOGGED_AT_BY_FINGERPRINT: dict[str, datetime] = {}
WATCHDOG_FAILURE_LOG_SUPPRESSION_SECONDS = 300
_WATCHDOG_TRAFFIC_FAILURE_CANDIDATE: dict[str, Any] | None = None


def _active_watchdog_vpn_adapter() -> dict[str, Any]:
    try:
        return active_vpn_dataplane_adapter()
    except Exception as exc:
        return {
            "role": "vpn_dataplane",
            "adapter_id": "unknown",
            "lifecycle_mode": "unknown",
            "ready": False,
            "source": {},
            "reason": "vpn_adapter_probe_failed",
            "error_message": str(exc),
        }


def _watchdog_adapter_subject(adapter: dict[str, Any], routing: dict[str, Any] | None = None) -> str | None:
    source = adapter.get("source") if isinstance(adapter.get("source"), dict) else {}
    return (
        str(source.get("system_id") or source.get("module") or "").strip()
        or str((routing or {}).get("active_auto_server_id") or "").strip()
        or None
    )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_timestamp() -> str:
    return _utc_now().isoformat()


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except ValueError:
        return None


def _load_watchdog_module() -> dict[str, Any] | None:
    return load_watchdog_module()


def _update_watchdog_module(
    *,
    runtime_state: str,
    status_text: str,
    error_code: str | None = None,
    error_message: str | None = None,
) -> dict[str, Any] | None:
    return update_watchdog_module(
        runtime_state=runtime_state,
        status_text=status_text,
        error_code=error_code,
        error_message=error_message,
    )


def _load_routing_state() -> dict[str, Any] | None:
    return load_routing_state()


def _routing_mode(routing: dict[str, Any] | None) -> str:
    # Watchdog must be proactive and check the DESIRED mode, not the APPLIED one.
    return routing_mode(routing)


def _compute_has_scoped_vpn_subjects() -> bool:
    return compute_has_scoped_vpn_subjects()


def _has_scoped_vpn_subjects() -> bool:
    return has_scoped_vpn_subjects(loader=_compute_has_scoped_vpn_subjects)


def _reset_watchdog_traffic_failure_candidate() -> None:
    global _WATCHDOG_TRAFFIC_FAILURE_CANDIDATE
    _WATCHDOG_TRAFFIC_FAILURE_CANDIDATE = None
    reset_traffic_failure_candidate()


def _reset_watchdog_stalled_traffic_failure_candidate() -> None:
    global _WATCHDOG_TRAFFIC_FAILURE_CANDIDATE
    set_traffic_failure_candidate(_WATCHDOG_TRAFFIC_FAILURE_CANDIDATE)
    reset_stalled_traffic_failure_candidate()
    _WATCHDOG_TRAFFIC_FAILURE_CANDIDATE = get_traffic_failure_candidate()


def _watchdog_traffic_failure_confirmation(
    *,
    active_server_id: str | None,
    traffic_signal: dict[str, Any],
    confirm_seconds: int,
    path_key: str | None = None,
) -> dict[str, Any]:
    """Debounce traffic-only watchdog failure detection across fresh snapshots."""

    global _WATCHDOG_TRAFFIC_FAILURE_CANDIDATE
    set_traffic_failure_candidate(_WATCHDOG_TRAFFIC_FAILURE_CANDIDATE)
    result = traffic_failure_confirmation(
        active_server_id=active_server_id,
        traffic_signal=traffic_signal,
        confirm_seconds=confirm_seconds,
        path_key=path_key,
        now_fn=_utc_now,
        parse_timestamp=_parse_timestamp,
    )
    _WATCHDOG_TRAFFIC_FAILURE_CANDIDATE = get_traffic_failure_candidate()
    return result


def _watchdog_active_quality_degraded_confirmation(
    *,
    active_server_id: str | None,
    active_check: dict[str, Any],
    traffic_signal: dict[str, Any],
    confirm_seconds: int,
    bad_checks_required: int,
    path_key: str | None,
) -> dict[str, Any]:
    global _WATCHDOG_TRAFFIC_FAILURE_CANDIDATE
    set_traffic_failure_candidate(_WATCHDOG_TRAFFIC_FAILURE_CANDIDATE)
    result = active_quality_degraded_confirmation(
        active_server_id=active_server_id,
        active_check=active_check,
        traffic_signal=traffic_signal,
        confirm_seconds=confirm_seconds,
        bad_checks_required=bad_checks_required,
        path_key=path_key,
        now_fn=_utc_now,
        parse_timestamp=_parse_timestamp,
    )
    _WATCHDOG_TRAFFIC_FAILURE_CANDIDATE = get_traffic_failure_candidate()
    return result


def _watchdog_active_quality_recovery_confirmation(
    *,
    active_server_id: str | None,
    traffic_signal: dict[str, Any],
    recovery_checks_required: int,
    path_key: str | None,
) -> dict[str, Any]:
    global _WATCHDOG_TRAFFIC_FAILURE_CANDIDATE
    set_traffic_failure_candidate(_WATCHDOG_TRAFFIC_FAILURE_CANDIDATE)
    result = active_quality_recovery_confirmation(
        active_server_id=active_server_id,
        traffic_signal=traffic_signal,
        recovery_checks_required=recovery_checks_required,
        path_key=path_key,
        now_fn=_utc_now,
    )
    _WATCHDOG_TRAFFIC_FAILURE_CANDIDATE = get_traffic_failure_candidate()
    return result


def _watchdog_failover_cooldown_status() -> dict[str, Any]:
    return failover_cooldown_status(now_fn=_utc_now, parse_timestamp=_parse_timestamp)


def _record_watchdog_successful_failover(
    *,
    path_key: str | None,
    previous_target_id: str | None,
    selected_target_id: str | None,
    cooldown_seconds: int,
) -> dict[str, Any]:
    return record_successful_failover(
        path_key=path_key,
        previous_target_id=previous_target_id,
        selected_target_id=selected_target_id,
        cooldown_seconds=cooldown_seconds,
        now_fn=_utc_now,
    )


def detect_recent_vpn_traffic_attempts(
    *,
    window_seconds: int | None = None,
) -> dict[str, Any]:
    return _detect_recent_vpn_traffic_attempts(window_seconds=window_seconds, now_fn=_utc_now)


_paused_result = paused_result
_write_watchdog_operational_event = write_watchdog_operational_event


def _should_write_watchdog_issue_log(fingerprint: str) -> bool:
    global _WATCHDOG_LAST_FAILURE_FINGERPRINT, _WATCHDOG_LAST_FAILURE_LOGGED_AT
    state = {
        "last_failure_fingerprint": _WATCHDOG_LAST_FAILURE_FINGERPRINT,
        "last_failure_logged_at": _WATCHDOG_LAST_FAILURE_LOGGED_AT,
        "issue_logged_at_by_fingerprint": _WATCHDOG_ISSUE_LOGGED_AT_BY_FINGERPRINT,
    }
    allowed = should_write_watchdog_issue_log(
        fingerprint,
        now_fn=_utc_now,
        lock=_WATCHDOG_FAILURE_LOG_LOCK,
        state=state,
        suppression_seconds=WATCHDOG_FAILURE_LOG_SUPPRESSION_SECONDS,
    )
    _WATCHDOG_LAST_FAILURE_FINGERPRINT = state.get("last_failure_fingerprint")
    _WATCHDOG_LAST_FAILURE_LOGGED_AT = state.get("last_failure_logged_at")
    return allowed


def _write_watchdog_decision_log(
    *,
    level: str,
    event_type: str,
    message: str,
    result: dict[str, Any],
    error_code: str | None = None,
) -> None:
    write_watchdog_decision_log(
        level=level,
        event_type=event_type,
        message=message,
        result=result,
        timestamp=_utc_timestamp(),
        should_write=_should_write_watchdog_issue_log,
        error_code=error_code,
    )


def _watchdog_flow_deps() -> WatchdogFlowDeps:
    return WatchdogFlowDeps(
        active_watchdog_vpn_adapter=_active_watchdog_vpn_adapter,
        active_quality_degraded_confirmation=_watchdog_active_quality_degraded_confirmation,
        active_quality_degraded=_watchdog_active_quality_degraded,
        active_quality_recovery_confirmation=_watchdog_active_quality_recovery_confirmation,
        cooldown_fields=_watchdog_cooldown_fields,
        degraded_active_check=_watchdog_degraded_active_check,
        detect_recent_vpn_traffic_attempts=detect_recent_vpn_traffic_attempts,
        failover_cooldown_status=_watchdog_failover_cooldown_status,
        get_last_runtime_convergence_status=get_last_runtime_convergence_status,
        get_settings=get_settings,
        get_vpn_runtime_controller=get_vpn_runtime_controller,
        has_scoped_vpn_subjects=_has_scoped_vpn_subjects,
        is_core_bypass_enabled=is_core_bypass_enabled,
        load_routing_state=_load_routing_state,
        load_watchdog_module=_load_watchdog_module,
        paused_result=_paused_result,
        recent_successful_active_check=_recent_successful_active_check,
        record_successful_failover=_record_watchdog_successful_failover,
        reset_stalled_traffic_failure_candidate=_reset_watchdog_stalled_traffic_failure_candidate,
        reset_traffic_failure_candidate=_reset_watchdog_traffic_failure_candidate,
        routing_mode=_routing_mode,
        runtime_response_fields=_watchdog_runtime_response_fields,
        set_global_mode=set_global_mode,
        traffic_failure_confirmation=_watchdog_traffic_failure_confirmation,
        update_watchdog_module=_update_watchdog_module,
        watchdog_adapter_subject=_watchdog_adapter_subject,
        write_watchdog_decision_log=_write_watchdog_decision_log,
        write_watchdog_operational_event=_write_watchdog_operational_event,
    )


def run_vpn_watchdog_check(
    *,
    traffic_attempts_observed: bool = False,
    allow_switch: bool = False,
    update_ping_state: bool = True,
    timeout_ms: int = DEFAULT_WATCHDOG_TIMEOUT_MS,
    candidate_limit: int = DEFAULT_WATCHDOG_CANDIDATE_LIMIT,
    reason: str = "manual_watchdog_check",
    log_events: bool = False,
) -> dict[str, Any]:
    return _run_vpn_watchdog_check(
        _watchdog_flow_deps(),
        traffic_attempts_observed=traffic_attempts_observed,
        allow_switch=allow_switch,
        update_ping_state=update_ping_state,
        timeout_ms=timeout_ms,
        candidate_limit=candidate_limit,
        reason=reason,
        log_events=log_events,
    )


def run_vpn_watchdog_auto_check(
    *,
    allow_switch: bool = True,
    update_ping_state: bool = True,
    timeout_ms: int = DEFAULT_WATCHDOG_TIMEOUT_MS,
    candidate_limit: int = DEFAULT_WATCHDOG_CANDIDATE_LIMIT,
    traffic_window_seconds: int | None = None,
    reason: str = "auto_watchdog_check",
    log_events: bool = False,
) -> dict[str, Any]:
    return _run_vpn_watchdog_auto_check(
        _watchdog_flow_deps(),
        allow_switch=allow_switch,
        update_ping_state=update_ping_state,
        timeout_ms=timeout_ms,
        candidate_limit=candidate_limit,
        traffic_window_seconds=traffic_window_seconds,
        reason=reason,
        log_events=log_events,
    )


def run_watchdog_scheduler_tick() -> dict[str, Any]:
    """Run one safe scheduler tick and convert exceptions into diagnostics."""

    return run_scheduler_tick(
        auto_check=run_vpn_watchdog_auto_check,
        update_module=_update_watchdog_module,
        should_log_issue=_should_write_watchdog_issue_log,
        write_technical=write_technical_log,
        timestamp=_utc_timestamp,
        runtime_failed_state=WATCHDOG_RUNTIME_FAILED,
        timeout_ms=DEFAULT_WATCHDOG_TIMEOUT_MS,
        candidate_limit=DEFAULT_WATCHDOG_CANDIDATE_LIMIT,
    )


def start_watchdog_scheduler() -> bool:
    def disabled() -> None:
        _update_watchdog_module(
            runtime_state=WATCHDOG_RUNTIME_STOPPED,
            status_text="Watchdog scheduler is disabled by config.",
            error_code="WATCHDOG_DISABLED_BY_CONFIG",
            error_message="FWROUTER_WATCHDOG_SCHEDULER_ENABLED is false.",
        )

    return start_scheduler(tick=run_watchdog_scheduler_tick, disabled=disabled)


def stop_watchdog_scheduler(*, timeout_seconds: float = 2.0) -> bool:
    return stop_scheduler(timeout_seconds=timeout_seconds)
