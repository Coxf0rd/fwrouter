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
    active_quality_degraded_confirmation: Callable[..., dict[str, Any]]
    active_quality_degraded: Callable[[dict[str, Any] | None], bool]
    active_quality_recovery_confirmation: Callable[..., dict[str, Any]]
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
    reset_stalled_traffic_failure_candidate: Callable[[], None]
    reset_traffic_failure_candidate: Callable[[], None]
    routing_mode: Callable[[dict[str, Any] | None], str]
    runtime_response_fields: Callable[..., dict[str, Any]]
    set_global_mode: Callable[..., Any]
    traffic_failure_confirmation: Callable[..., dict[str, Any]]
    update_watchdog_module: Callable[..., dict[str, Any] | None]
    watchdog_adapter_subject: Callable[..., str | None]
    write_watchdog_decision_log: Callable[..., None]
    write_watchdog_operational_event: Callable[..., None]


