from __future__ import annotations

from fwrouter_api.services.watchdog_auto_flow import run_vpn_watchdog_auto_check
from fwrouter_api.services.watchdog_flow_deps import (
    DEFAULT_WATCHDOG_CANDIDATE_LIMIT,
    DEFAULT_WATCHDOG_TIMEOUT_MS,
    WATCHDOG_RUNTIME_DEGRADED,
    WATCHDOG_RUNTIME_PAUSED,
    WATCHDOG_RUNTIME_RUNNING,
    WatchdogFlowDeps,
)
from fwrouter_api.services.watchdog_manual_flow import run_vpn_watchdog_check

__all__ = [
    "DEFAULT_WATCHDOG_CANDIDATE_LIMIT",
    "DEFAULT_WATCHDOG_TIMEOUT_MS",
    "WATCHDOG_RUNTIME_DEGRADED",
    "WATCHDOG_RUNTIME_PAUSED",
    "WATCHDOG_RUNTIME_RUNNING",
    "WatchdogFlowDeps",
    "run_vpn_watchdog_auto_check",
    "run_vpn_watchdog_check",
]
