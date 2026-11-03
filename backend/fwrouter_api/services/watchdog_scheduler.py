from __future__ import annotations

from threading import Thread
from typing import Any, Callable

from fwrouter_api.core.config import get_settings
from fwrouter_api.services.worker_lifecycle import WorkerLifecycle


_WORKER = WorkerLifecycle()


def run_scheduler_tick(
    *,
    auto_check: Callable[..., dict[str, Any]],
    update_module: Callable[..., dict[str, Any] | None],
    should_log_issue: Callable[[str], bool],
    write_technical: Callable[..., None],
    timestamp: Callable[[], str],
    runtime_failed_state: str,
    timeout_ms: int,
    candidate_limit: int,
) -> dict[str, Any]:
    settings = get_settings()

    try:
        return auto_check(
            allow_switch=True,
            update_ping_state=True,
            timeout_ms=timeout_ms,
            candidate_limit=candidate_limit,
            traffic_window_seconds=settings.watchdog_traffic_window_seconds,
            reason="scheduler_watchdog_check",
            log_events=settings.watchdog_scheduler_log_events,
        )
    except Exception as exc:  # pragma: no cover - defensive background safety
        updated_module = update_module(
            runtime_state=runtime_failed_state,
            status_text="Watchdog scheduler tick failed.",
            error_code="WATCHDOG_SCHEDULER_FAILED",
            error_message=str(exc),
        )
        details = {
            "error_code": "WATCHDOG_SCHEDULER_FAILED",
            "error_message": str(exc),
            "timestamp": timestamp(),
        }
        if should_log_issue(str(exc)):
            write_technical(
                component="watchdog",
                level="error",
                event_type="watchdog_scheduler_failed",
                message="Watchdog scheduler tick failed.",
                details=details,
            )
        return {
            "ok": False,
            "automated": True,
            "status": "scheduler_failed",
            "reason": "scheduler_watchdog_check",
            "traffic_attempts_observed": False,
            "allow_switch": True,
            "active_server_id": None,
            "active_check": None,
            "selector": None,
            "action": "none",
            "message": "Watchdog scheduler tick failed.",
            "module": updated_module,
            "error_code": "WATCHDOG_SCHEDULER_FAILED",
            "error_message": str(exc),
        }


def _next_interval_seconds(result: dict[str, Any], *, default_interval: int, suspicious_interval: int) -> int:
    requested = result.get("next_check_delay_seconds") if isinstance(result, dict) else None
    if requested is None:
        return default_interval
    try:
        requested_interval = int(requested)
    except (TypeError, ValueError):
        return default_interval
    if requested_interval <= 0:
        return default_interval
    return max(5, min(default_interval, requested_interval, suspicious_interval))


def scheduler_loop(tick: Callable[[], dict[str, Any]]) -> None:
    settings = get_settings()
    interval = settings.watchdog_auto_interval_seconds
    suspicious_interval = settings.watchdog_suspicious_interval_seconds

    while not _WORKER.stop_event.is_set():
        result = tick()
        wait_seconds = _next_interval_seconds(
            result,
            default_interval=interval,
            suspicious_interval=suspicious_interval,
        )
        if _WORKER.stop_event.wait(wait_seconds):
            break


def start_scheduler(
    *,
    tick: Callable[[], dict[str, Any]],
    disabled: Callable[[], None],
) -> bool:
    settings = get_settings()
    if not settings.watchdog_scheduler_enabled:
        disabled()
        return False

    return _WORKER.start(
        lambda: scheduler_loop(tick),
        name="fwrouter-watchdog",
        thread_factory=Thread,
    )


def stop_scheduler(*, timeout_seconds: float = 2.0) -> bool:
    return _WORKER.stop(timeout_seconds=timeout_seconds)
