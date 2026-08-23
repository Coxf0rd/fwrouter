from __future__ import annotations

from typing import Any

from fwrouter_api.services.logs import write_operational_log


def paused_result(
    *,
    status: str,
    reason: str,
    message: str,
    module: dict[str, Any] | None,
    routing: dict[str, Any] | None,
    traffic_signal: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "ok": True,
        "automated": True,
        "status": status,
        "reason": reason,
        "traffic_attempts_observed": False,
        "allow_switch": False,
        "active_server_id": (routing or {}).get("active_auto_server_id"),
        "active_check": None,
        "selector": None,
        "action": "none",
        "message": message,
        "traffic_signal": traffic_signal,
        "module": module,
        "routing": routing,
    }


def write_watchdog_operational_event(
    *,
    event_type: str,
    level: str,
    message: str,
    details: dict[str, Any],
) -> None:
    write_operational_log(
        event_type=event_type,
        level=level,
        subject_id=None,
        message=message,
        details=details,
    )
