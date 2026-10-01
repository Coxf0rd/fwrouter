"""Provider recovery uses the existing durable watchdog incident and apply pipeline."""
from __future__ import annotations

from copy import deepcopy
from contextvars import ContextVar

from typing import Any
import time

from fwrouter_api.db.connection import db_session
from fwrouter_api.services.watchdog_failure_state import get_recovery_pending, set_recovery_pending

_REENTRY = ContextVar("provider_verified_reentry", default=False)


def _transition_event(code: str, pending: dict[str, Any]) -> None:
    from fwrouter_api.services.events import write_operational_event
    write_operational_event(
        event_type=code, event_code=code, message="Provider recovery transition.",
        severity="warning" if code == "provider_emergency_direct" else "info",
        entity_type="subscription", entity_id=pending.get("source_ref"),
        details={"source_ref": pending.get("source_ref"), "logical_server_id": pending.get("logical_server_id"),
                 "phase": "emergency_direct" if code == "provider_emergency_direct" else "verified_vpn_reentry"},
    )


def emergency_override() -> dict[str, Any] | None:
    if _REENTRY.get():
        return None
    pending = get_recovery_pending()
    if pending and pending.get("provider_managed") and pending.get("emergency_direct"):
        return pending
    return None


def override_manifest_state(routing: dict[str, Any], subjects: list[dict[str, Any]], *, reentry: bool = False) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Change only execution input; SQLite routing and subject intent stay untouched."""
    if reentry or not emergency_override():
        return routing, subjects
    effective_routing = {**routing, "desired_mode": "direct", "applied_mode": "direct"}
    effective_subjects = deepcopy(subjects)
    for subject in effective_subjects:
        state = subject.get("effective_state")
        if isinstance(state, dict) and state.get("effective_mode", state.get("mode")) in {"vpn", "forced_vpn", "selective"}:
            state.update(mode="direct", effective_mode="direct", dataplane_path="direct", mode_source="emergency_direct")
        if subject.get("dataplane_path") in {"vpn", "forced_vpn", "selective"}:
            subject["dataplane_path"] = "direct"
        if subject.get("effective_mode") in {"vpn", "forced_vpn", "selective"}:
            subject["effective_mode"] = "direct"
    return effective_routing, effective_subjects


def _apply_override(*, reentry: bool) -> dict[str, Any]:
    token = _REENTRY.set(reentry)
    try:
        return _apply_override_under_policy(reentry=reentry)
    finally:
        _REENTRY.reset(token)


def _apply_override_under_policy(*, reentry: bool) -> dict[str, Any]:
    from fwrouter_api.services import apply_orchestrator as apply
    from fwrouter_api.services.jobs import create_job, mark_job_running, mark_job_failed, mark_job_success
    from fwrouter_api.services.apply_orchestrator_constants import LOCK_APPLY
    from fwrouter_api.services.jobs import JobLockConflictError
    try:
        job = create_job("provider_emergency_apply", lock_key=LOCK_APPLY, requested_by="watchdog", input_data={"reentry": reentry})
    except JobLockConflictError:
        return {"ok": False, "outcome": "busy", "error_code": "APPLY_IN_PROGRESS"}
    mark_job_running(job["job_id"])
    try:
        routing = apply.get_routing_snapshot()
        subjects = apply._load_subjects_with_overrides(
            routing=routing, user_overrides=apply._load_user_override_map(),
            server_overrides=apply._load_server_override_map(),
        )
        from fwrouter_api.services.xray_runtime_state import _module_state
        module = _module_state("xray") or {}
        if module.get("desired_state") == "enabled" and module.get("lifecycle_mode") == "managed":
            from fwrouter_api.services.xray import materialize_xray_runtime_bindings
            xray = materialize_xray_runtime_bindings(requested_by="watchdog.provider_recovery", prepare_mihomo_handoff=False)
            if not xray.get("ok"):
                raise RuntimeError("XRAY_EMERGENCY_READBACK_FAILED")
        result = apply._run_pipeline_for_state(
            job_id=job["job_id"], reason="provider_vpn_reentry" if reentry else "provider_emergency_direct",
            input_data={"reentry": reentry}, routing=routing, subjects=subjects,
            extra={"provider_vpn_reentry": reentry},
        )
    except Exception:
        mark_job_failed(job["job_id"], error_code="PROVIDER_EMERGENCY_APPLY_FAILED", error_message="Provider emergency apply failed.")
        return {"ok": False, "outcome": "failed", "error_code": "PROVIDER_EMERGENCY_APPLY_FAILED"}
    safe = {"ok": bool(result.get("ok")), "outcome": "verified" if result.get("ok") else "failed"}
    if safe["ok"]:
        mark_job_success(job["job_id"], result=safe)
    else:
        mark_job_failed(job["job_id"], error_code="PROVIDER_EMERGENCY_READBACK_FAILED", error_message="Emergency apply/readback failed.", result=safe)
    return safe


def enter_emergency_direct(pending: dict[str, Any]) -> dict[str, Any]:
    from fwrouter_api.adapters.xray_common import xray_writer_guard
    with xray_writer_guard():
        # Durable marker precedes execution: a crash cannot silently reapply VPN.
        set_recovery_pending({**pending, "provider_managed": True, "emergency_direct": True, "phase": "emergency_direct_pending"})
        result = _apply_override(reentry=False)
        if result.get("ok"):
            set_recovery_pending({**pending, "provider_managed": True, "emergency_direct": True, "phase": "emergency_direct"})
            _transition_event("provider_emergency_direct", pending)
        else:
            # Either dataplane may already have changed. Retain the durable
            # override until the shared reconcile path verifies Direct.
            set_recovery_pending({**pending, "provider_managed": True, "emergency_direct": True, "phase": "emergency_direct_unconfirmed"})
        return {**result, "action": "emergency_direct" if result.get("ok") else "none", "effective_override": "emergency_direct" if result.get("ok") else None}


def try_verified_reentry(controller: Any, *, timeout_ms: int, reason: str) -> dict[str, Any]:
    from fwrouter_api.adapters.xray_common import xray_writer_guard
    from fwrouter_api.services.watchdog_failure_state import reset_traffic_failure_candidate
    with xray_writer_guard():
        pending = emergency_override()
        if pending is None:
            return {"ok": True, "action": "none"}
        from fwrouter_api.services.vpn_auto_exclusive import get_vpn_auto_exclusive_source_ref
        from fwrouter_api.services.auto_eligibility import server_is_in_exclusive_pool
        exclusive_ref = get_vpn_auto_exclusive_source_ref()
        if exclusive_ref:
            with db_session() as connection:
                allowed = server_is_in_exclusive_pool(
                    connection,
                    str(pending.get("logical_server_id") or ""),
                    exclusive_ref,
                )
            if not allowed:
                return {
                    "ok": True,
                    "status": "provider_reentry_suppressed_by_exclusive_source",
                    "action": "none",
                    "effective_override": "emergency_direct",
                    "pending_preserved": True,
                }
        if pending.get("phase") == "emergency_direct_unconfirmed":
            repaired = _apply_override(reentry=False)
            if not repaired.get("ok"):
                return {"ok": False, "status": "emergency_direct_unconfirmed", "action": "none", "effective_override": "emergency_direct", "fallback_verified": False}
            pending = {**pending, "phase": "emergency_direct"}
            set_recovery_pending(pending)
        # A local client-path probe of the saved VPN group works while LAN is Direct.
        probe = controller.probe(update_ping_state=True, timeout_ms=timeout_ms, reason=reason)
        if not probe or not probe.get("ok"):
            return {"ok": False, "status": "emergency_direct", "action": "none", "effective_override": "emergency_direct"}
        result = _apply_override(reentry=True)
        state = controller.get_state()
        exact_target = str(state.get("active_target_id") or "") == str(pending.get("logical_server_id") or "")
        post_probe = controller.probe(update_ping_state=True, timeout_ms=timeout_ms, reason=reason) if result.get("ok") and exact_target else None
        if result.get("ok") and exact_target and post_probe and post_probe.get("ok"):
            set_recovery_pending(None)
            reset_traffic_failure_candidate()
            _transition_event("provider_vpn_reentry", pending)
            return {"ok": True, "status": "provider_vpn_recovered", "action": "verified_vpn_reentry", "effective_override": None}
        # Failed connectivity/readback after VPN apply restores verified Direct.
        fallback = _apply_override(reentry=False)
        if not fallback.get("ok"):
            set_recovery_pending({**pending, "phase": "emergency_direct_unconfirmed"})
        return {"ok": False, "status": "emergency_direct" if fallback.get("ok") else "emergency_direct_unconfirmed", "action": "none", "effective_override": "emergency_direct", "fallback_verified": bool(fallback.get("ok"))}


def confirmed_provider_recovery(*, logical_server_id: str | None, path_key: str | None,
                                decision_id: str | None, controller: Any, timeout_ms: int,
                                allow_switch: bool) -> dict[str, Any] | None:
    from fwrouter_api.services.provider_managed import binding_for_logical, execute_provider_operation, provider_candidates
    binding = binding_for_logical(logical_server_id)
    if not binding:
        return None
    from fwrouter_api.services.vpn_auto_exclusive import get_vpn_auto_exclusive_source_ref
    from fwrouter_api.services.auto_eligibility import server_is_in_exclusive_pool
    exclusive_ref = get_vpn_auto_exclusive_source_ref()
    if exclusive_ref:
        with db_session() as connection:
            allowed = server_is_in_exclusive_pool(connection, str(logical_server_id or ""), exclusive_ref)
        if not allowed:
            pending = get_recovery_pending() or {}
            return {
                "ok": True,
                "status": "provider_recovery_suppressed_by_exclusive_source",
                "action": "none",
                "effective_override": "emergency_direct" if pending.get("emergency_direct") else None,
                "pending_preserved": bool(pending),
            }
    if not allow_switch:
        return {"ok": True, "status": "provider_recovery_suppressed", "action": "none"}
    pending = get_recovery_pending() or {}
    if pending.get("source_ref") != binding["source_ref"] or pending.get("binding_revision") != binding["binding_revision"]:
        pending = {}
    if not decision_id or decision_id == pending.get("traffic_decision_id"):
        return {"ok": True, "status": "provider_recovery_pending", "action": "none"}
    if float(pending.get("not_before") or 0) > time.time():
        return {"ok": False, "status": "provider_recovery_pending", "outcome": "deferred", "action": "none", "not_before": pending["not_before"]}
    phase = min(3, int(pending.get("provider_confirmation", 0)) + 1)
    pending = {"provider_managed": True, "source_ref": binding["source_ref"], "binding_revision": binding["binding_revision"],
               "logical_server_id": logical_server_id, "path_key": path_key, "phase": "provider_recovery",
               "provider_confirmation": phase, "traffic_decision_id": decision_id}
    set_recovery_pending(pending)
    if phase == 1:
        result = execute_provider_operation(binding["source_ref"], "recovery_refresh", expected_revision=binding["binding_revision"])
    elif phase == 2:
        from fwrouter_api.services.provider_adapters import provider_adapter, RequestBudget, ProviderError
        from fwrouter_api.services.provider_managed import store
        adapter = None
        budget = RequestBudget(4, 30, operation="recovery_confirmation_2")
        try:
            adapter = provider_adapter(binding["provider_id"], f"{binding['source_ref']}:{binding['binding_revision']}", source_ref=binding["source_ref"])
            stats = adapter.get_server_stats(int(binding["resource_id"]), budget=budget)
            with db_session() as conn:
                store.update_observation(conn, binding["source_ref"], kind="recovery_status", safe_data={**stats, "member_id": binding["current_member_id"], "protocol": binding["observed_protocol"]},
                                         scope_key=f"{binding['current_member_id']}:{binding['observed_protocol']}",
                                         revision=binding["binding_revision"], expected_binding_revision=binding["binding_revision"])
            if stats.get("status") in {"down", "unavailable"}:
                candidates = provider_candidates(binding["source_ref"])
                if not candidates:
                    members = adapter.discover(int(binding["current_location_id"]), binding["protocol"], budget=budget)
                    with db_session() as conn:
                        store.record_discovery(conn, binding["source_ref"], binding["binding_revision"], binding["current_location_id"], binding["protocol"], members,
                                               expected_binding_revision=binding["binding_revision"])
                    candidates = provider_candidates(binding["source_ref"])
                from fwrouter_api.services.selector import _select_candidate_with_priority
                selected, _ = _select_candidate_with_priority(candidates)
                result = execute_provider_operation(binding["source_ref"], "switch", member_id=selected["member_id"],
                                                     expected_revision=binding["binding_revision"], _adapter=adapter, _budget=budget) if selected else {"ok": False, "outcome": "no_candidate"}
            elif stats.get("status") == "up":
                result = execute_provider_operation(binding["source_ref"], "recovery_refresh", expected_revision=binding["binding_revision"], _adapter=adapter, _budget=budget)
            else:
                result = {"ok": False, "outcome": "unknown"}
        except ProviderError as exc:
            result = {"ok": False, "outcome": "unknown", "error_code": exc.code, "not_before": time.time()+exc.retry_after_seconds if exc.retry_after_seconds is not None else None}
        finally:
            if adapter is not None:
                adapter.close()
    else:
        # Third normal confirmation alone does not prove current connectivity absence.
        probe = controller.probe(update_ping_state=True, timeout_ms=timeout_ms, reason="provider_emergency_preflight")
        if probe and probe.get("ok"):
            set_recovery_pending(None)
            from fwrouter_api.services.watchdog_failure_state import reset_traffic_failure_candidate
            reset_traffic_failure_candidate()
            return {"ok": True, "status": "provider_vpn_recovered", "action": "observe_recovery"}
        result = enter_emergency_direct(pending)
    if phase < 3:
        set_recovery_pending({**pending, "last_outcome": result.get("outcome"), "error_code": result.get("error_code"), "not_before": result.get("not_before")})
    return {**result, "status": "emergency_direct" if result.get("effective_override") else "provider_recovery_pending",
            "provider_confirmation": phase, "action": result.get("action", "provider_recovery"), "provider_recovery": True}
