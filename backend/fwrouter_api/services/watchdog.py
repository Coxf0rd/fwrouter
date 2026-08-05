from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from threading import Event, Lock, Thread
from typing import Any

from fwrouter_api.adapters.mihomo import DEFAULT_MIHOMO_ADAPTER
from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import db_session
from fwrouter_api.services.core_bypass import is_core_bypass_enabled
from fwrouter_api.services.live_probe_cache import get_live_probe_cache
from fwrouter_api.services.logs import write_operational_log, write_technical_log
from fwrouter_api.services.runtime_convergence import get_last_runtime_convergence_status
from fwrouter_api.services.selector import get_vpn_auto_state, select_vpn_auto_server
from fwrouter_api.services.server_ping import check_active_server_delay
from fwrouter_api.services.servers import (
    ensure_routing_global_state,
    set_global_mode,
)
from fwrouter_api.services.subject_policy import list_subjects_with_effective_state
from fwrouter_api.services.subject_taxonomy import TRANSPARENT_INGRESS_CLIENT_SUBJECT_TYPES


DEFAULT_WATCHDOG_TIMEOUT_MS = 10000
DEFAULT_WATCHDOG_CANDIDATE_LIMIT = 4
DEFAULT_WATCHDOG_ACTIVE_CHECK_TTL_SECONDS = 60
SCOPED_VPN_SUBJECTS_CACHE_TTL_SECONDS = 30
VPN_AUTO_STATE_CACHE_TTL_SECONDS = 45

WATCHDOG_RUNTIME_RUNNING = "running"
WATCHDOG_RUNTIME_PAUSED = "paused"
WATCHDOG_RUNTIME_DEGRADED = "degraded"
WATCHDOG_RUNTIME_STOPPED = "stopped"
WATCHDOG_RUNTIME_FAILED = "failed"

_WATCHDOG_THREAD: Thread | None = None
_WATCHDOG_STOP_EVENT = Event()
_WATCHDOG_LOCK = Lock()
_WATCHDOG_FAILURE_LOG_LOCK = Lock()
_WATCHDOG_LAST_FAILURE_FINGERPRINT: str | None = None
_WATCHDOG_LAST_FAILURE_LOGGED_AT: datetime | None = None
WATCHDOG_FAILURE_LOG_SUPPRESSION_SECONDS = 300


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_timestamp() -> str:
    return _utc_now().isoformat()


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _load_watchdog_module() -> dict[str, Any] | None:
    with db_session() as connection:
        row = connection.execute(
            """
            SELECT
                module_name,
                desired_state,
                runtime_state,
                apply_state,
                status_text,
                error_code,
                error_message,
                updated_at
            FROM modules
            WHERE module_name = 'watchdog'
            """
        ).fetchone()

    return dict(row) if row is not None else None


def _update_watchdog_module(
    *,
    runtime_state: str,
    status_text: str,
    error_code: str | None = None,
    error_message: str | None = None,
) -> dict[str, Any] | None:
    with db_session() as connection:
        connection.execute(
            """
            UPDATE modules
            SET
                runtime_state = ?,
                apply_state = 'clean',
                status_text = ?,
                error_code = ?,
                error_message = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE module_name = 'watchdog'
            """,
            (runtime_state, status_text, error_code, error_message),
        )

    return _load_watchdog_module()


def _load_routing_state() -> dict[str, Any] | None:
    with db_session() as connection:
        row = connection.execute(
            """
            SELECT
                desired_mode,
                applied_mode,
                server_mode,
                active_auto_server_id
            FROM routing_global_state
            WHERE id = 1
            """
        ).fetchone()

    if row is None:
        return ensure_routing_global_state()
    return dict(row)


def _routing_mode(routing: dict[str, Any] | None) -> str:
    state = routing or {}
    # Watchdog must be proactive and check the DESIRED mode, not the APPLIED one.
    return str(state.get("desired_mode") or state.get("applied_mode") or "direct")


def _compute_has_scoped_vpn_subjects() -> bool:
    subjects = list_subjects_with_effective_state(
        is_active=True,
        include_deleted=False,
        limit=1000,
    )
    for subject in subjects:
        subject_type = str(subject.get("subject_type") or "").strip().lower()
        if subject_type not in TRANSPARENT_INGRESS_CLIENT_SUBJECT_TYPES:
            continue
        effective_state = subject.get("effective_state")
        if not isinstance(effective_state, dict):
            continue
        effective_mode = str(effective_state.get("effective_mode") or "").strip().lower()
        dataplane_path = str(effective_state.get("dataplane_path") or "").strip().lower()
        if effective_mode in {"vpn", "selective"} or dataplane_path in {"vpn", "selective"}:
            return True
    return False


def _has_scoped_vpn_subjects() -> bool:
    return bool(
        get_live_probe_cache(
            "watchdog.has_scoped_vpn_subjects",
            ttl_seconds=SCOPED_VPN_SUBJECTS_CACHE_TTL_SECONDS,
            loader=_compute_has_scoped_vpn_subjects,
        )
    )


def _watchdog_vpn_auto_state() -> dict[str, Any]:
    return get_live_probe_cache(
        "watchdog.vpn_auto_state",
        ttl_seconds=VPN_AUTO_STATE_CACHE_TTL_SECONDS,
        loader=get_vpn_auto_state,
    )


def _recent_successful_active_check(
    *,
    server_id: str | None,
    ttl_seconds: int = DEFAULT_WATCHDOG_ACTIVE_CHECK_TTL_SECONDS,
    checked_by: str,
    timeout_ms: int,
) -> dict[str, Any] | None:
    normalized_server_id = str(server_id or "").strip()
    if not normalized_server_id:
        return None
    cutoff_modifier = f"-{max(1, int(ttl_seconds))} seconds"
    with db_session() as connection:
        row = connection.execute(
            """
            SELECT status, last_ping_ms, checked_at, error_code, error_message
            FROM server_ping_state
            WHERE server_id = ?
              AND status = 'success'
              AND checked_at >= datetime('now', ?)
            LIMIT 1
            """,
            (normalized_server_id, cutoff_modifier),
        ).fetchone()
    if row is None:
        return None
    last_ping_ms = row["last_ping_ms"]
    return {
        "ok": True,
        "server_id": normalized_server_id,
        "status": "success",
        "last_ping_ms": last_ping_ms,
        "latency_label": f"{last_ping_ms} ms" if last_ping_ms is not None else "n/a",
        "checked_by": checked_by,
        "test_url": "cached_server_ping_state",
        "timeout_ms": timeout_ms,
        "error_code": None,
        "error_message": None,
        "updated_state": False,
        "cached": True,
        "cache_ttl_seconds": ttl_seconds,
        "checked_at": row["checked_at"],
    }


def detect_recent_vpn_traffic_attempts(
    *,
    window_seconds: int | None = None,
) -> dict[str, Any]:
    settings = get_settings()
    resolved_window = window_seconds or settings.watchdog_traffic_window_seconds
    cutoff_dt = _utc_now() - timedelta(seconds=resolved_window)
    cutoff = cutoff_dt.isoformat()

    with db_session() as connection:
        rows = connection.execute(
            """
            SELECT
                counter_key,
                subject_id,
                path,
                rx_bytes,
                tx_bytes,
                collected_at,
                metadata_json
            FROM traffic_counter_snapshots
            WHERE path = 'vpn'
              AND collected_at >= ?
            ORDER BY collected_at DESC
            LIMIT 200
            """,
            (cutoff,),
        ).fetchall()

    samples: list[dict[str, Any]] = []
    active_count = 0
    for row in rows:
        metadata = json.loads(row["metadata_json"]) if row["metadata_json"] else {}
        rx_delta = int(metadata.get("rx_delta") or 0)
        tx_delta = int(metadata.get("tx_delta") or 0)
        activity_observed = bool(metadata.get("activity_observed")) or rx_delta > 0 or tx_delta > 0
        if activity_observed:
            active_count += 1
        samples.append(
            {
                "counter_key": row["counter_key"],
                "subject_id": row["subject_id"],
                "collected_at": row["collected_at"],
                "rx_delta": rx_delta,
                "tx_delta": tx_delta,
                "activity_observed": activity_observed,
                "metadata": metadata,
            }
        )

    last_collected_at = samples[0]["collected_at"] if samples else None
    last_collected_age_seconds = None
    last_collected_dt = _parse_timestamp(last_collected_at)
    if last_collected_dt is not None:
        last_collected_age_seconds = max(
            0,
            int((_utc_now() - last_collected_dt).total_seconds()),
        )

    settings = get_settings()
    signal_stale = (
        last_collected_age_seconds is None
        or last_collected_age_seconds > max(settings.watchdog_traffic_window_seconds, resolved_window)
    )

    return {
        "observed": active_count > 0,
        "window_seconds": resolved_window,
        "source": "traffic_counter_snapshots",
        "checked_samples_count": len(samples),
        "active_samples_count": active_count,
        "last_collected_at": last_collected_at,
        "last_collected_age_seconds": last_collected_age_seconds,
        "fresh": not signal_stale,
        "authoritative": not signal_stale,
        "signal_authority": "authoritative" if not signal_stale else "unavailable",
        "safe_for_watchdog_auto": not signal_stale,
        "samples": samples,
    }


def _paused_result(
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


def _write_watchdog_operational_event(
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


def _should_write_scheduler_failure_log(error_message: str) -> bool:
    global _WATCHDOG_LAST_FAILURE_FINGERPRINT, _WATCHDOG_LAST_FAILURE_LOGGED_AT

    now = _utc_now()
    with _WATCHDOG_FAILURE_LOG_LOCK:
        if (
            _WATCHDOG_LAST_FAILURE_FINGERPRINT == error_message
            and _WATCHDOG_LAST_FAILURE_LOGGED_AT is not None
            and (now - _WATCHDOG_LAST_FAILURE_LOGGED_AT).total_seconds()
            < WATCHDOG_FAILURE_LOG_SUPPRESSION_SECONDS
        ):
            return False

        _WATCHDOG_LAST_FAILURE_FINGERPRINT = error_message
        _WATCHDOG_LAST_FAILURE_LOGGED_AT = now
        return True


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
    """Evaluate VPN watchdog state.

    This function intentionally does not treat "no traffic" as failure.
    A failure can only be evaluated when the caller tells us that attempts
    through vpn-auto were observed.

    With allow_switch=False it never changes Mihomo runtime.
    With allow_switch=True it may call selector apply after active check fails.
    """

    health = DEFAULT_MIHOMO_ADAPTER.health()
    active_server_id = health.active_server_id

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
            "message": "No VPN-auto traffic attempts observed; watchdog does not treat idle state as failure.",
        }
        if log_events:
            _write_watchdog_operational_event(
                event_type="vpn_watchdog_no_traffic",
                level="info",
                message=result["message"],
                details=result,
            )
        return result

    checked_by = f"watchdog_active_check:{reason}"
    active_check = _recent_successful_active_check(
        server_id=active_server_id,
        checked_by=checked_by,
        timeout_ms=timeout_ms,
    )
    if active_check is None:
        active_check = check_active_server_delay(
            update_state=update_ping_state,
            checked_by=checked_by,
            timeout_ms=timeout_ms,
        )

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
            "message": "VPN-auto traffic attempts observed and active server check succeeded.",
        }

        if log_events:
            _write_watchdog_operational_event(
                event_type="vpn_watchdog_healthy",
                level="info",
                message=result["message"],
                details=result,
            )

        return result

    selector = select_vpn_auto_server(
        apply=allow_switch,
        reason=f"watchdog_failover:{reason}",
        check_on_demand=True,
        update_ping_state=update_ping_state,
        on_demand_limit=candidate_limit,
        timeout_ms=timeout_ms,
        exclude_active=True,
        post_check=True,
    )

    if selector["ok"]:
        # After a successful switch, we must trigger a dataplane reconciliation
        # to ensure routing rules are updated for the new reality.
        if allow_switch:
            current_mode = _routing_mode(_load_routing_state())
            if current_mode in {"vpn", "selective"}:
                set_global_mode(current_mode, requested_by="watchdog_failover")

        result = {
            "ok": True,
            "status": "failover_applied" if allow_switch else "failover_candidate_found",
            "reason": reason,
            "traffic_attempts_observed": True,
            "allow_switch": allow_switch,
            "active_server_id": active_server_id,
            "active_check": active_check,
            "selector": selector,
            "action": "switch_vpn_auto" if allow_switch else "dry_run_only",
            "message": (
                "VPN-auto active check failed; failover candidate was applied."
                if allow_switch
                else "VPN-auto active check failed; failover candidate found in dry-run."
            ),
        }

        if log_events:
            _write_watchdog_operational_event(
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
        "action": "fail_open_direct_recommended",
        "message": "VPN-auto active check failed and no working failover candidate was found.",
    }

    if log_events:
        _write_watchdog_operational_event(
            event_type="vpn_watchdog_fail_open_direct",
            level="error",
            message=result["message"],
            details=result,
        )

    return result


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
    """Run watchdog with backend-owned traffic signal and module state updates."""

    module = _load_watchdog_module()
    routing = _load_routing_state()

    if module is None:
        return _paused_result(
            status="watchdog_module_missing",
            reason=reason,
            message="Watchdog module row is missing.",
            module=None,
            routing=routing,
        )

    if module["desired_state"] != "enabled":
        return _paused_result(
            status="watchdog_disabled",
            reason=reason,
            message="Watchdog automation is disabled.",
            module=module,
            routing=routing,
        )

    if is_core_bypass_enabled():
        updated_module = _update_watchdog_module(
            runtime_state=WATCHDOG_RUNTIME_PAUSED,
            status_text="Watchdog paused because FWRouter core bypass is active.",
        )
        return _paused_result(
            status="paused_core_bypass",
            reason=reason,
            message="Watchdog paused because FWRouter core bypass is active.",
            module=updated_module,
            routing=routing,
        )

    mode = _routing_mode(routing)
    scoped_vpn_subjects = _has_scoped_vpn_subjects()
    if mode not in {"vpn", "selective"} and not scoped_vpn_subjects:
        updated_module = _update_watchdog_module(
            runtime_state=WATCHDOG_RUNTIME_PAUSED,
            status_text=f"Watchdog paused because global mode is {mode}.",
        )
        return _paused_result(
            status="paused_not_vpn",
            reason=reason,
            message=f"Watchdog paused because global mode is {mode}.",
            module=updated_module,
            routing=routing,
        )

    runtime_convergence = get_last_runtime_convergence_status(
        mode=mode,
        scoped_vpn_subjects=scoped_vpn_subjects,
    )
    if not bool(runtime_convergence.get("ok")):
        updated_module = _update_watchdog_module(
            runtime_state=WATCHDOG_RUNTIME_DEGRADED,
            status_text="Watchdog could not repair selective/VPN runtime convergence.",
            error_code=runtime_convergence.get("error_code") or "WATCHDOG_RUNTIME_CONVERGENCE_FAILED",
            error_message=runtime_convergence.get("error_message")
            or "Selective/VPN runtime convergence failed.",
        )
        return {
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

    server_mode = str((routing or {}).get("server_mode") or "auto")
    if server_mode != "auto":
        updated_module = _update_watchdog_module(
            runtime_state=WATCHDOG_RUNTIME_PAUSED,
            status_text=f"Watchdog paused because server_mode is {server_mode}.",
        )
        return _paused_result(
            status="paused_not_auto_selector",
            reason=reason,
            message=f"Watchdog paused because server_mode is {server_mode}.",
            module=updated_module,
            routing=routing,
        )

    vpn_auto_state = _watchdog_vpn_auto_state()
    if not bool(vpn_auto_state.get("active_auto_server_valid")):
        if allow_switch:
            selector = select_vpn_auto_server(
                apply=True,
                reason=f"watchdog_initial_select:{reason}",
                check_on_demand=True,
                update_ping_state=update_ping_state,
                on_demand_limit=candidate_limit,
                timeout_ms=timeout_ms,
                exclude_active=bool(vpn_auto_state.get("active_auto_server_id")),
                post_check=True,
            )
            if selector["ok"]:
                updated_module = _update_watchdog_module(
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
                    "active_server_id": selector.get("active_after"),
                    "active_check": None,
                    "selector": selector,
                    "action": "switch_vpn_auto",
                    "message": "Watchdog bootstrap selected a valid vpn-auto server without waiting for traffic attempts.",
                    "traffic_signal": None,
                    "safe_for_watchdog_auto": False,
                    "module": updated_module,
                    "routing": routing,
                    "vpn_auto_state": get_vpn_auto_state(),
                    "runtime_convergence": runtime_convergence,
                }

        updated_module = _update_watchdog_module(
            runtime_state=WATCHDOG_RUNTIME_DEGRADED,
            status_text="VPN-auto is missing a valid active server and needs initial selection.",
            error_code="WATCHDOG_INITIAL_AUTO_SELECTION_REQUIRED",
            error_message="VPN-auto has no valid active server selected.",
        )
        return {
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
        }

    traffic_signal = detect_recent_vpn_traffic_attempts(
        window_seconds=traffic_window_seconds,
    )
    if not bool(traffic_signal.get("authoritative")):
        updated_module = _update_watchdog_module(
            runtime_state=WATCHDOG_RUNTIME_DEGRADED,
            status_text="Watchdog traffic signal is stale or unavailable; automatic switching is suppressed.",
            error_code="WATCHDOG_SIGNAL_UNAVAILABLE",
            error_message="Fresh traffic counter snapshots are required for authoritative watchdog decisions.",
        )
        return {
            "ok": True,
            "automated": True,
            "status": "paused_signal_unavailable",
            "reason": reason,
            "traffic_attempts_observed": False,
            "allow_switch": False,
            "active_server_id": (routing or {}).get("active_auto_server_id"),
            "active_check": None,
            "selector": None,
            "action": "none",
            "message": "Watchdog traffic signal is stale or unavailable; automatic switching is suppressed.",
            "traffic_signal": traffic_signal,
            "safe_for_watchdog_auto": False,
            "module": updated_module,
            "routing": routing,
            "runtime_convergence": runtime_convergence,
        }

    result = run_vpn_watchdog_check(
        traffic_attempts_observed=traffic_signal["observed"],
        allow_switch=allow_switch,
        update_ping_state=update_ping_state,
        timeout_ms=timeout_ms,
        candidate_limit=candidate_limit,
        reason=reason,
        log_events=log_events,
    )

    if result["status"] == "no_failure_no_traffic":
        updated_module = _update_watchdog_module(
            runtime_state=WATCHDOG_RUNTIME_RUNNING,
            status_text="Watchdog enabled and waiting for VPN-auto traffic activity.",
        )
    elif result["status"] in {"healthy", "failover_applied"}:
        updated_module = _update_watchdog_module(
            runtime_state=WATCHDOG_RUNTIME_RUNNING,
            status_text=result["message"],
        )
    elif result["status"] == "failover_candidate_found":
        updated_module = _update_watchdog_module(
            runtime_state=WATCHDOG_RUNTIME_DEGRADED,
            status_text=result["message"],
        )
    else:
        updated_module = _update_watchdog_module(
            runtime_state=WATCHDOG_RUNTIME_DEGRADED,
            status_text=result["message"],
            error_code="WATCHDOG_FAIL_OPEN_DIRECT_RECOMMENDED",
            error_message=result["message"],
        )

    return {
        **result,
        "automated": True,
        "traffic_signal": traffic_signal,
        "safe_for_watchdog_auto": bool((traffic_signal or {}).get("safe_for_watchdog_auto")),
        "module": updated_module,
        "routing": routing,
        "runtime_convergence": runtime_convergence,
    }


def run_watchdog_scheduler_tick() -> dict[str, Any]:
    """Run one safe scheduler tick and convert exceptions into diagnostics."""

    settings = get_settings()

    try:
        return run_vpn_watchdog_auto_check(
            allow_switch=True,
            update_ping_state=True,
            timeout_ms=DEFAULT_WATCHDOG_TIMEOUT_MS,
            candidate_limit=DEFAULT_WATCHDOG_CANDIDATE_LIMIT,
            traffic_window_seconds=settings.watchdog_traffic_window_seconds,
            reason="scheduler_watchdog_check",
            log_events=settings.watchdog_scheduler_log_events,
        )
    except Exception as exc:  # pragma: no cover - defensive background safety
        updated_module = _update_watchdog_module(
            runtime_state=WATCHDOG_RUNTIME_FAILED,
            status_text="Watchdog scheduler tick failed.",
            error_code="WATCHDOG_SCHEDULER_FAILED",
            error_message=str(exc),
        )
        details = {
            "error_code": "WATCHDOG_SCHEDULER_FAILED",
            "error_message": str(exc),
            "timestamp": _utc_timestamp(),
        }
        if _should_write_scheduler_failure_log(str(exc)):
            write_technical_log(
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


def _watchdog_scheduler_loop() -> None:
    settings = get_settings()
    interval = settings.watchdog_auto_interval_seconds

    while not _WATCHDOG_STOP_EVENT.is_set():
        run_watchdog_scheduler_tick()
        if _WATCHDOG_STOP_EVENT.wait(interval):
            break


def start_watchdog_scheduler() -> bool:
    settings = get_settings()
    if not settings.watchdog_scheduler_enabled:
        _update_watchdog_module(
            runtime_state=WATCHDOG_RUNTIME_STOPPED,
            status_text="Watchdog scheduler is disabled by config.",
            error_code="WATCHDOG_DISABLED_BY_CONFIG",
            error_message="FWROUTER_WATCHDOG_SCHEDULER_ENABLED is false.",
        )
        return False

    global _WATCHDOG_THREAD
    with _WATCHDOG_LOCK:
        if _WATCHDOG_THREAD is not None and _WATCHDOG_THREAD.is_alive():
            return False

        _WATCHDOG_STOP_EVENT.clear()
        _WATCHDOG_THREAD = Thread(
            target=_watchdog_scheduler_loop,
            name="fwrouter-watchdog",
            daemon=True,
        )
        _WATCHDOG_THREAD.start()
        return True


def stop_watchdog_scheduler(*, timeout_seconds: float = 2.0) -> bool:
    global _WATCHDOG_THREAD
    with _WATCHDOG_LOCK:
        if _WATCHDOG_THREAD is None:
            return False

        _WATCHDOG_STOP_EVENT.set()
        _WATCHDOG_THREAD.join(timeout=timeout_seconds)
        stopped = not _WATCHDOG_THREAD.is_alive()
        _WATCHDOG_THREAD = None
        return stopped
