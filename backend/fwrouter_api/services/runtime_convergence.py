from __future__ import annotations

from datetime import datetime, timezone
from threading import Lock
from typing import Any

from fwrouter_api.db.connection import db_session
from fwrouter_api.services.apply_orchestrator import reconcile_current_routing_if_drift
from fwrouter_api.services.dnsmasq import reconcile_dnsmasq_rules
from fwrouter_api.services.live_probe_cache import get_live_probe_cache
from fwrouter_api.services.logs import write_operational_log, write_technical_log
from fwrouter_api.services.servers import ensure_routing_global_state
from fwrouter_api.services.subject_policy import list_subjects_with_effective_state


RUNTIME_CONVERGENCE_CACHE_TTL_SECONDS = 60
_LAST_RESULT_LOCK = Lock()
_LAST_RESULT: dict[str, Any] | None = None


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    return str(state.get("desired_mode") or state.get("applied_mode") or "direct").strip().lower()


def _compute_has_scoped_vpn_subjects() -> bool:
    subjects = list_subjects_with_effective_state(
        is_active=True,
        include_deleted=False,
        limit=1000,
    )
    for subject in subjects:
        subject_type = str(subject.get("subject_type") or "").strip().lower()
        if subject_type not in {"lan", "tailscale_node"}:
            continue
        effective_state = subject.get("effective_state")
        if not isinstance(effective_state, dict):
            continue
        effective_mode = str(effective_state.get("effective_mode") or "").strip().lower()
        dataplane_path = str(effective_state.get("dataplane_path") or "").strip().lower()
        if effective_mode in {"vpn", "selective"} or dataplane_path in {"vpn", "selective"}:
            return True
    return False


def _needs_convergence(mode: str, scoped_vpn_subjects: bool) -> bool:
    return mode in {"vpn", "selective"} or scoped_vpn_subjects


def _store_last_result(result: dict[str, Any]) -> dict[str, Any]:
    with _LAST_RESULT_LOCK:
        global _LAST_RESULT
        _LAST_RESULT = dict(result)
    return result


def get_last_runtime_convergence_status(
    *,
    mode: str,
    scoped_vpn_subjects: bool,
) -> dict[str, Any]:
    normalized_mode = str(mode or "direct").strip().lower()
    if not _needs_convergence(normalized_mode, scoped_vpn_subjects):
        return {
            "ok": True,
            "status": "skipped",
            "reason": "no_vpn_or_selective_scope",
            "checked": False,
            "repaired": False,
            "dnsmasq": None,
            "dataplane": None,
        }

    with _LAST_RESULT_LOCK:
        result = dict(_LAST_RESULT) if _LAST_RESULT is not None else None

    if result is None:
        return {
            "ok": True,
            "status": "not_checked",
            "reason": "runtime_convergence_scheduler_has_not_reported_yet",
            "checked": False,
            "repaired": False,
            "dnsmasq": None,
            "dataplane": None,
        }
    return result


def _write_operational_event(
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


def _run_runtime_convergence(*, requested_by: str, log_events: bool) -> dict[str, Any]:
    routing = _load_routing_state()
    mode = _routing_mode(routing)
    scoped_vpn_subjects = _compute_has_scoped_vpn_subjects()

    if not _needs_convergence(mode, scoped_vpn_subjects):
        return _store_last_result(
            {
                "ok": True,
                "status": "skipped",
                "reason": "no_vpn_or_selective_scope",
                "checked": True,
                "checked_at": _utc_timestamp(),
                "requested_by": requested_by,
                "mode": mode,
                "scoped_vpn_subjects": scoped_vpn_subjects,
                "repaired": False,
                "dnsmasq": None,
                "dataplane": None,
                "error_code": None,
                "error_message": None,
            }
        )

    dnsmasq = reconcile_dnsmasq_rules()
    dataplane = reconcile_current_routing_if_drift(
        requested_by=requested_by,
    )

    ok = bool(dnsmasq.get("ok")) and bool(dataplane.get("ok"))
    repaired = (
        bool(dnsmasq.get("restart_required"))
        or dataplane.get("action") == "reapply_global_mode"
    )
    result = {
        "ok": ok,
        "status": "ok" if ok else "failed",
        "checked": True,
        "checked_at": _utc_timestamp(),
        "requested_by": requested_by,
        "mode": mode,
        "scoped_vpn_subjects": scoped_vpn_subjects,
        "repaired": repaired,
        "dnsmasq": dnsmasq,
        "dataplane": dataplane,
        "error_code": None,
        "error_message": None,
    }
    if not ok:
        if not bool(dnsmasq.get("ok")):
            result["error_code"] = dnsmasq.get("error_code") or "DNSMASQ_SELECTIVE_CONVERGENCE_FAILED"
            result["error_message"] = dnsmasq.get("message") or "Dnsmasq selective convergence failed."
        else:
            result["error_code"] = dataplane.get("error_code") or "DATAPLANE_DRIFT_CONVERGENCE_FAILED"
            result["error_message"] = dataplane.get("error_message") or dataplane.get("message")

    if log_events and (repaired or not ok):
        level = "info" if ok else "error"
        event_type = (
            "runtime_convergence_repaired"
            if ok
            else "runtime_convergence_failed"
        )
        message = (
            "Runtime convergence repaired selective/VPN path."
            if ok
            else "Runtime convergence failed to repair selective/VPN path."
        )
        write_technical_log(
            component="runtime-convergence",
            level=level,
            event_type=event_type,
            message=message,
            details=result,
        )
        _write_operational_event(
            event_type=event_type,
            level=level,
            message=message,
            details=result,
        )

    return _store_last_result(result)


def run_runtime_convergence_check(
    *,
    requested_by: str = "runtime_convergence_scheduler",
    log_events: bool = True,
    force: bool = False,
) -> dict[str, Any]:
    if force:
        return _run_runtime_convergence(requested_by=requested_by, log_events=log_events)

    return get_live_probe_cache(
        "runtime_convergence.check",
        ttl_seconds=RUNTIME_CONVERGENCE_CACHE_TTL_SECONDS,
        loader=lambda: _run_runtime_convergence(
            requested_by=requested_by,
            log_events=log_events,
        ),
    )


def _reset_runtime_convergence_state_for_tests() -> None:
    with _LAST_RESULT_LOCK:
        global _LAST_RESULT
        _LAST_RESULT = None
