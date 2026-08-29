from __future__ import annotations

from datetime import datetime, timezone
from threading import Lock
from typing import Any

from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import db_session
from fwrouter_api.services.apply_orchestrator import reconcile_current_routing_if_drift
from fwrouter_api.services.dnsmasq import inspect_dnsmasq_selective_status, reconcile_dnsmasq_rules
from fwrouter_api.services.live_probe_cache import get_live_probe_cache
from fwrouter_api.services.logs import write_operational_log, write_technical_log
from fwrouter_api.services.servers import ensure_routing_global_state, expire_global_fixed_server
from fwrouter_api.services.subject_policy import list_subjects_with_effective_state
from fwrouter_api.services.subject_taxonomy import TRANSPARENT_INGRESS_CLIENT_SUBJECT_TYPES


RUNTIME_CONVERGENCE_CACHE_TTL_SECONDS = 60
_LAST_RESULT_LOCK = Lock()
_LAST_RESULT: dict[str, Any] | None = None
_FAILURE_STATE_LOCK = Lock()
_FAILURE_STATE: dict[str, Any] = {
    "fingerprint": None,
    "count": 0,
    "cooldown_until": None,
}


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


def _needs_convergence(mode: str, scoped_vpn_subjects: bool) -> bool:
    return mode in {"vpn", "selective"} or scoped_vpn_subjects


def _store_last_result(result: dict[str, Any]) -> dict[str, Any]:
    with _LAST_RESULT_LOCK:
        global _LAST_RESULT
        _LAST_RESULT = dict(result)
    return result


def _failure_fingerprint(result: dict[str, Any]) -> str:
    dnsmasq = result.get("dnsmasq") if isinstance(result.get("dnsmasq"), dict) else {}
    dataplane = result.get("dataplane") if isinstance(result.get("dataplane"), dict) else {}
    return "|".join(
        [
            str(result.get("mode") or ""),
            str(result.get("error_code") or ""),
            str(dnsmasq.get("error_code") or ""),
            str(dnsmasq.get("preflight_action") or ""),
            ",".join(str(item) for item in (dnsmasq.get("missing") or [])),
            str(dataplane.get("error_code") or ""),
            str(dataplane.get("action") or ""),
        ]
    )


def _cooldown_until_timestamp(*, now: datetime, seconds: int) -> str:
    return datetime.fromtimestamp(now.timestamp() + seconds, tz=timezone.utc).isoformat()


def _runtime_convergence_cooldown_result(*, requested_by: str) -> dict[str, Any] | None:
    now = datetime.now(timezone.utc)
    with _FAILURE_STATE_LOCK:
        cooldown_until = _FAILURE_STATE.get("cooldown_until")
        if not isinstance(cooldown_until, str):
            return None
        try:
            cooldown_dt = datetime.fromisoformat(cooldown_until)
        except ValueError:
            _FAILURE_STATE["cooldown_until"] = None
            return None
        if cooldown_dt <= now:
            _FAILURE_STATE["cooldown_until"] = None
            _FAILURE_STATE["count"] = 0
            return None

        last = dict(_LAST_RESULT) if _LAST_RESULT is not None else {}
        result = {
            **last,
            "ok": False,
            "status": "cooldown",
            "checked": False,
            "checked_at": _utc_timestamp(),
            "requested_by": requested_by,
            "repaired": False,
            "suppressed": True,
            "cooldown_until": cooldown_until,
            "cooldown_failure_count": int(_FAILURE_STATE.get("count") or 0),
            "error_code": last.get("error_code") or "RUNTIME_CONVERGENCE_COOLDOWN_ACTIVE",
            "error_message": (
                last.get("error_message")
                or "Runtime convergence repair is temporarily suppressed after repeated failures."
            ),
        }
    return result


def _record_runtime_convergence_result(result: dict[str, Any], *, log_events: bool) -> dict[str, Any]:
    if bool(result.get("ok")):
        with _FAILURE_STATE_LOCK:
            _FAILURE_STATE["fingerprint"] = None
            _FAILURE_STATE["count"] = 0
            _FAILURE_STATE["cooldown_until"] = None
        result["cooldown_failure_count"] = 0
        result["cooldown_until"] = None
        return result

    settings = get_settings()
    fingerprint = _failure_fingerprint(result)
    entered_cooldown = False
    with _FAILURE_STATE_LOCK:
        if _FAILURE_STATE.get("fingerprint") == fingerprint:
            count = int(_FAILURE_STATE.get("count") or 0) + 1
        else:
            count = 1
        _FAILURE_STATE["fingerprint"] = fingerprint
        _FAILURE_STATE["count"] = count
        if count >= settings.runtime_convergence_failure_limit:
            previous_cooldown_until = _FAILURE_STATE.get("cooldown_until")
            cooldown_until = _cooldown_until_timestamp(
                now=datetime.now(timezone.utc),
                seconds=settings.runtime_convergence_cooldown_seconds,
            )
            entered_cooldown = not isinstance(previous_cooldown_until, str)
            _FAILURE_STATE["cooldown_until"] = cooldown_until
        else:
            cooldown_until = None
            _FAILURE_STATE["cooldown_until"] = None

    result["cooldown_failure_count"] = count
    result["cooldown_until"] = cooldown_until
    if entered_cooldown and log_events:
        details = {
            "failure_count": count,
            "cooldown_until": cooldown_until,
            "error_code": result.get("error_code"),
            "error_message": result.get("error_message"),
        }
        write_technical_log(
            component="runtime-convergence",
            level="warning",
            event_type="runtime_convergence_cooldown_entered",
            message="Runtime convergence repair entered cooldown after repeated failures.",
            details=details,
        )
        _write_operational_event(
            event_type="runtime_convergence_cooldown_entered",
            level="warning",
            message="Runtime convergence repair entered cooldown after repeated failures.",
            details=details,
        )
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
    dedupe_key: str | None = None,
    cooldown_seconds: int | None = None,
) -> None:
    write_operational_log(
        event_type=event_type,
        level=level,
        subject_id=None,
        message=message,
        details=details,
        dedupe_key=dedupe_key,
        cooldown_seconds=cooldown_seconds,
    )


def _is_dnsmasq_nftset_probe_transient(selective_status: dict[str, Any]) -> bool:
    nftset_probe_status = selective_status.get("nftset_probe_status")
    if not isinstance(nftset_probe_status, dict):
        return False
    missing = nftset_probe_status.get("missing")
    if not isinstance(missing, list) or not missing:
        return False
    allowed_prefixes = (
        "dnsmasq_nftset_probe_materialization_missing:",
        "dnsmasq_nftset_probe_resolve_failed:",
    )
    if not all(str(item).startswith(allowed_prefixes) for item in missing):
        return False
    if any(str(item).startswith("dnsmasq_nftset_probe_resolve_failed:") for item in missing):
        probes = nftset_probe_status.get("probes")
        return isinstance(probes, list) and any(
            isinstance(probe, dict) and bool(probe.get("ok"))
            for probe in probes
        )
    return True


def _converge_dnsmasq_selective_contract() -> dict[str, Any]:
    try:
        selective_status = inspect_dnsmasq_selective_status()
    except Exception as exc:
        dnsmasq = reconcile_dnsmasq_rules()
        dnsmasq["preflight_status"] = {
            "ok": False,
            "missing": ["dnsmasq_selective_status_failed"],
            "error": str(exc),
        }
        dnsmasq["preflight_action"] = "reconcile_after_status_error"
        return dnsmasq

    if bool(selective_status.get("ok")):
        return {
            "ok": True,
            "skipped": True,
            "restart_required": False,
            "restart_reason": None,
            "preflight_action": "skip_reconcile_status_ok",
            "message": "Dnsmasq selective contract is healthy; reconcile skipped.",
            "selective_status": selective_status,
        }

    if _is_dnsmasq_nftset_probe_transient(selective_status):
        return {
            "ok": True,
            "skipped": True,
            "restart_required": False,
            "restart_reason": None,
            "preflight_action": "skip_reconcile_nftset_probe_transient",
            "message": "Dnsmasq nftset active probe is transient; reconcile skipped.",
            "selective_status": selective_status,
        }

    dnsmasq = reconcile_dnsmasq_rules()
    dnsmasq["preflight_status"] = selective_status
    dnsmasq["preflight_action"] = "reconcile_after_status_unhealthy"
    return dnsmasq


def _skip_dnsmasq_after_dataplane_failure(dataplane: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": True,
        "skipped": True,
        "restart_required": False,
        "restart_reason": None,
        "preflight_action": "skip_after_dataplane_repair_failed",
        "message": "Dnsmasq convergence skipped because dataplane drift repair failed.",
        "dataplane_error_code": dataplane.get("error_code"),
        "dataplane_error_message": dataplane.get("error_message") or dataplane.get("message"),
    }


def _run_runtime_convergence(*, requested_by: str, log_events: bool) -> dict[str, Any]:
    global_fixed_server_expiry = expire_global_fixed_server(
        dry_run=False,
        apply_runtime=True,
    )
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
                "global_fixed_server_expiry": global_fixed_server_expiry,
                "repaired": False,
                "dnsmasq": None,
                "dataplane": None,
                "error_code": None,
                "error_message": None,
            }
        )

    dataplane = reconcile_current_routing_if_drift(
        requested_by=requested_by,
    )
    dnsmasq = (
        _converge_dnsmasq_selective_contract()
        if bool(dataplane.get("ok"))
        else _skip_dnsmasq_after_dataplane_failure(dataplane)
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
        "global_fixed_server_expiry": global_fixed_server_expiry,
        "repaired": repaired,
        "dnsmasq": dnsmasq,
        "dataplane": dataplane,
        "error_code": None,
        "error_message": None,
    }
    if not ok:
        if not bool(dataplane.get("ok")):
            result["error_code"] = dataplane.get("error_code") or "DATAPLANE_DRIFT_CONVERGENCE_FAILED"
            result["error_message"] = dataplane.get("error_message") or dataplane.get("message")
        elif not bool(dnsmasq.get("ok")):
            result["error_code"] = dnsmasq.get("error_code") or "DNSMASQ_SELECTIVE_CONVERGENCE_FAILED"
            result["error_message"] = dnsmasq.get("message") or "Dnsmasq selective convergence failed."
    result = _record_runtime_convergence_result(result, log_events=log_events)

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
            dedupe_key=f"{event_type}:{mode}:{result.get('error_code') or 'ok'}",
            cooldown_seconds=300,
        )
        _write_operational_event(
            event_type=event_type,
            level=level,
            message=message,
            details=result,
            dedupe_key=f"{event_type}:{mode}:{result.get('error_code') or 'ok'}",
            cooldown_seconds=300,
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

    cooldown_result = _runtime_convergence_cooldown_result(requested_by=requested_by)
    if cooldown_result is not None:
        return _store_last_result(cooldown_result)

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
    with _FAILURE_STATE_LOCK:
        _FAILURE_STATE["fingerprint"] = None
        _FAILURE_STATE["count"] = 0
        _FAILURE_STATE["cooldown_until"] = None
