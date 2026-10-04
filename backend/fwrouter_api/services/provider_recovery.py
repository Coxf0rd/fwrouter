"""Provider recovery uses the existing durable watchdog incident and apply pipeline."""
from __future__ import annotations

from copy import deepcopy
from contextvars import ContextVar

from typing import Any
import time

from fwrouter_api.db.connection import db_session
from fwrouter_api.services.watchdog_failure_state import get_recovery_pending, set_recovery_pending

_REENTRY = ContextVar("provider_verified_reentry", default=False)


def _automatic_member_switch_allowed(source_ref: str) -> bool:
    from fwrouter_api.services.provider_managed import automatic_member_switch_allowed
    return automatic_member_switch_allowed(source_ref)


def _recovery_policy_disabled_result(phase: int) -> dict[str, Any]:
    return {
        "ok": True, "status": "provider_auto_switch_disabled",
        "outcome": "policy_disabled", "action": "none",
        "error_code": "provider_auto_switch_disabled",
        "reason": "provider_auto_switch_disabled",
        "switch_attempted": False, "last_good_retained": True,
        "provider_confirmation": phase, "provider_recovery": True,
    }


def _record_policy_disabled(pending: dict[str, Any], phase: int) -> dict[str, Any]:
    from fwrouter_api.adapters.xray_common import xray_writer_guard
    updated = {**pending, "last_outcome": "policy_disabled",
               "error_code": "provider_auto_switch_disabled",
               "switch_attempted": False}
    with xray_writer_guard(timeout_seconds=5.0):
        if not _cas_pending(pending, updated):
            return {"ok": False, "status": "provider_recovery_stale", "outcome": "deferred",
                    "action": "none", "error_code": "provider_recovery_stale",
                    "last_good_retained": True}
    result = _recovery_policy_disabled_result(phase)
    result["effective_override"] = "emergency_direct" if updated.get("emergency_direct") else None
    return result


def _runtime_incarnation() -> str | None:
    """Read the active selector runtime generation without probing connectivity."""
    try:
        from fwrouter_api.services.selector import _active_selector_runtime
        _adapter, operations = _active_selector_runtime()
        reader = getattr(operations, "runtime_incarnation", None)
        if callable(reader):
            incarnation = str(reader(timeout_seconds=2.0) or "")
        else:
            incarnation = ""
        if not incarnation:
            from fwrouter_api.core.config import get_settings
            if get_settings().environment.strip().lower() != "test":
                return None
        return incarnation or None
    except Exception:
        return None


def _capture_recovery_context(controller: Any, pending: dict[str, Any]) -> dict[str, Any] | None:
    """Capture persistent Core fences before performing any network probe."""
    try:
        from fwrouter_api.services.vpn_auto_selection_state import read_selection_fence, selection_pool_signature
        state = controller.get_state()
        if str(state.get("active_target_id") or "") != str(pending.get("logical_server_id") or ""):
            return None
        with db_session() as connection:
            fence = read_selection_fence(connection)
            pool = selection_pool_signature(connection)
            routing = connection.execute("SELECT desired_mode FROM routing_global_state WHERE id=1").fetchone()
            exclusive = connection.execute(
                "SELECT value_json FROM settings WHERE key='vpn_auto_exclusive_source_ref'"
            ).fetchone()
            from fwrouter_api.services.auto_eligibility import auto_eligible_sql
            eligible = connection.execute(
                f"""SELECT 1 FROM servers s LEFT JOIN server_preferences p ON p.server_id=s.server_id
                    WHERE s.server_id=? AND {auto_eligible_sql(server_alias='s', preferences_alias='p')} LIMIT 1""",
                (str(pending.get("logical_server_id") or ""),),
            ).fetchone()
            binding = connection.execute(
                """SELECT source_ref, binding_revision, provider_id, resource_id, logical_server_id,
                          enabled, protocol, current_member_id, current_location_id, observed_protocol,
                          observed_at, applied_member_id, applied_protocol, applied_revision,
                          allow_automatic_member_switch
                   FROM provider_bindings WHERE source_ref=?""",
                (str(pending.get("source_ref") or ""),),
            ).fetchone()
            credential = connection.execute(
                "SELECT api_key FROM provider_credentials WHERE source_ref=?",
                (str(pending.get("source_ref") or ""),),
            ).fetchone()
        runtime_incarnation = _runtime_incarnation()
        if runtime_incarnation is None:
            return None
        if eligible is None:
            return None
        import hashlib
        credential_fingerprint = hashlib.sha256(str(credential["api_key"] or "").encode()).hexdigest() if credential else None
        return {
            "pending": dict(pending), "selection_fence": fence, "pool": pool,
            "desired_mode": str(routing["desired_mode"] or "") if routing else "",
            "exclusive": str(exclusive["value_json"] or "") if exclusive else "",
            "binding": tuple(binding) if binding else None,
            "eligible": True,
            "credential_fingerprint": credential_fingerprint,
            "runtime_incarnation": runtime_incarnation,
            "active_target_id": str(state.get("active_target_id") or ""),
            "path_key": str(state.get("path_key") or ""),
        }
    except Exception:
        return None


def _recovery_context_matches(controller: Any, snapshot: dict[str, Any], *,
                              expected_pending: dict[str, Any] | None = None,
                              expected_target: str | None = None) -> bool:
    """Revalidate Core intent, selection, runtime and exact pending ownership."""
    from fwrouter_api.adapters.xray_common import xray_writer_guard_is_held
    if not xray_writer_guard_is_held():
        return False
    if get_recovery_pending() != (expected_pending if expected_pending is not None else snapshot["pending"]):
        return False
    try:
        from fwrouter_api.services.vpn_auto_selection_state import read_selection_fence, selection_pool_signature
        with db_session() as connection:
            fence = read_selection_fence(connection)
            pool = selection_pool_signature(connection)
            routing = connection.execute("SELECT desired_mode FROM routing_global_state WHERE id=1").fetchone()
            exclusive = connection.execute(
                "SELECT value_json FROM settings WHERE key='vpn_auto_exclusive_source_ref'"
            ).fetchone()
            from fwrouter_api.services.auto_eligibility import auto_eligible_sql
            eligible = connection.execute(
                f"""SELECT 1 FROM servers s LEFT JOIN server_preferences p ON p.server_id=s.server_id
                    WHERE s.server_id=? AND {auto_eligible_sql(server_alias='s', preferences_alias='p')} LIMIT 1""",
                (str(snapshot["pending"].get("logical_server_id") or ""),),
            ).fetchone()
            binding = connection.execute(
                """SELECT source_ref, binding_revision, provider_id, resource_id, logical_server_id,
                          enabled, protocol, current_member_id, current_location_id, observed_protocol,
                          observed_at, applied_member_id, applied_protocol, applied_revision,
                          allow_automatic_member_switch
                   FROM provider_bindings WHERE source_ref=?""",
                (str(snapshot["pending"].get("source_ref") or ""),),
            ).fetchone()
            credential = connection.execute(
                "SELECT api_key FROM provider_credentials WHERE source_ref=?",
                (str(snapshot["pending"].get("source_ref") or ""),),
            ).fetchone()
        from fwrouter_api.services.logical_topology import get_logical_runtime_name
        from fwrouter_api.services.selector import _active_selector_runtime
        _adapter, operations = _active_selector_runtime()
        reader = getattr(operations, "get_recovery_selection_snapshot", None)
        if callable(reader):
            binding_values = snapshot.get("binding")
            logical_id = str(binding_values[4]) if binding_values else str(snapshot["pending"].get("logical_server_id") or "")
            runtime_target = get_logical_runtime_name(logical_id)
            runtime_readback = reader(runtime_target, timeout_seconds=1.5)
            active_runtime_target = str(runtime_readback.get("active_target") or "")
            state = {"active_target_id": logical_id if active_runtime_target == runtime_target else active_runtime_target}
        else:
            from fwrouter_api.core.config import get_settings
            if get_settings().environment.strip().lower() != "test":
                return False
            # Lightweight test controllers do not expose the production adapter.
            state = controller.get_state()
            runtime_readback = None
    except Exception as exc:
        return False
    expected_fence = snapshot["selection_fence"]
    if fence != expected_fence or pool != snapshot["pool"]:
        return False
    if (str(routing["desired_mode"] or "") if routing else "") != snapshot["desired_mode"]:
        return False
    if (str(exclusive["value_json"] or "") if exclusive else "") != snapshot["exclusive"]:
        return False
    if eligible is None:
        return False
    import hashlib
    credential_fingerprint = hashlib.sha256(str(credential["api_key"] or "").encode()).hexdigest() if credential else None
    if (tuple(binding) if binding else None) != snapshot["binding"] or credential_fingerprint != snapshot["credential_fingerprint"]:
        return False
    if not snapshot["runtime_incarnation"] or _runtime_incarnation() != snapshot["runtime_incarnation"]:
        return False
    target = str(state.get("active_target_id") or "")
    target_matches = (target == str(expected_target) if expected_target is not None
                      else target == snapshot["active_target_id"])
    if not target_matches:
        return False
    if runtime_readback is not None:
        effective = str(runtime_readback.get("effective_member_runtime_identity") or "")
        binding_values = snapshot.get("binding")
        if not runtime_readback.get("ok") or not effective or not binding_values:
            return False
        from fwrouter_api.services.provider_managed import provider_runtime_member_id
        binding_for_id = {"provider_id": binding_values[2], "source_ref": binding_values[0]}
        applied_member = binding_values[11] or binding_values[7]
        applied_protocol = binding_values[12] or binding_values[9] or binding_values[6]
        canonical_member = provider_runtime_member_id(binding_for_id, applied_member, applied_protocol)
        with db_session() as conn:
            member = conn.execute(
                "SELECT member_runtime_name FROM logical_server_members WHERE logical_server_id=? AND member_id=? AND is_active=1",
                (str(binding_values[4]), canonical_member),
            ).fetchone()
        if member is None or str(member["member_runtime_name"] or "") != effective:
            return False
        return True
    return str(state.get("path_key") or "") == snapshot["path_key"]


def _provider_down_evidence_is_fresh(observed_at: float, *, now: float | None = None) -> bool:
    current = time.time() if now is None else float(now)
    age = current - float(observed_at)
    return 0 <= age <= 15.0


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


def _cas_pending(expected: dict[str, Any] | None, value: dict[str, Any] | None) -> bool:
    from fwrouter_api.services.watchdog_failure_state import compare_and_set_recovery_pending
    return compare_and_set_recovery_pending(expected, value)


def _confirm_unconfirmed_switch(binding: dict[str, Any], pending: dict[str, Any],
                                controller: Any) -> dict[str, Any]:
    """Perform one bounded read-only confirmation after an ambiguous PATCH."""
    if pending.get("switch_confirmation_attempted"):
        return {"ok": False, "status": "switch_outcome_unconfirmed", "outcome": "unconfirmed",
                "action": "none", "error_code": "switch_outcome_unconfirmed", "last_good_retained": True}
    snapshot = _capture_recovery_context(controller, pending)
    if snapshot is None:
        return {"ok": False, "status": "provider_recovery_deferred", "outcome": "deferred",
                "action": "none", "error_code": "provider_recovery_fence_unavailable"}
    from fwrouter_api.services.provider_managed import provider_operation_reservation
    from fwrouter_api.services.provider_adapters import provider_adapter, RequestBudget, ProviderError
    from fwrouter_api.services.provider_managed import execute_provider_operation
    reservation = provider_operation_reservation()
    try:
        reservation.__enter__()
    except ProviderError as exc:
        return {"ok": False, "status": "provider_recovery_deferred", "outcome": "deferred",
                "action": "none", "error_code": "provider_operation_busy",
                "provider_error_code": exc.code, "last_good_retained": True}
    try:
        attempted = {**pending, "switch_confirmation_attempted": True}
        from fwrouter_api.adapters.xray_common import xray_writer_guard
        with xray_writer_guard(timeout_seconds=5.0):
            if not _recovery_context_matches(controller, snapshot) or not _cas_pending(pending, attempted):
                return {"ok": False, "status": "provider_recovery_stale", "outcome": "deferred",
                        "action": "none", "error_code": "provider_recovery_stale"}
        adapter = None
        budget = RequestBudget(2, 15, operation="ambiguous_switch_confirmation")
        result = None
        try:
            with provider_operation_reservation():
                adapter = provider_adapter(binding["provider_id"],
                                           f"{binding['source_ref']}:{binding['binding_revision']}",
                                           source_ref=binding["source_ref"])
                configs = adapter.get_configs(int(binding["resource_id"]), budget=budget, max_age_s=0)
                if len(configs) != 1:
                    raise ProviderError("PROVIDER_INVALID_RESPONSE")
                actual = configs[0]
                target = str(pending.get("switch_requested_member_id") or "")
                previous = str(pending.get("switch_previous_member_id") or "")
                actual_id = str(actual.get("server_id") or "")
                try:
                    actual_location = int(actual.get("location_id"))
                    actual_resource = int(actual.get("id"))
                except (TypeError, ValueError):
                    actual_location = actual_resource = -1
                if (actual_id not in {target, previous} or not actual_id
                        or actual_location != int(binding["current_location_id"])
                        or actual_resource != int(binding["resource_id"])
                        or str(actual.get("protocol") or "") != str(binding["protocol"])):
                    raise ProviderError("PROVIDER_INVALID_RESPONSE")
                if actual_id == target:
                    result = execute_provider_operation(
                        binding["source_ref"], "recovery_refresh",
                        expected_member_id=target,
                        expected_revision=binding["binding_revision"],
                        expected_selection_revision=int(snapshot["selection_fence"]["revision"]),
                        expected_selection_pool_signature=snapshot["pool"],
                        expected_runtime_incarnation=snapshot["runtime_incarnation"],
                        _adapter=adapter, _budget=budget,
                    )
        except ProviderError as exc:
            from fwrouter_api.adapters.provider_base import recovery_evidence_code
            return {"ok": False, "status": "switch_outcome_unconfirmed", "outcome": "unconfirmed",
                    "action": "none", "error_code": recovery_evidence_code(exc),
                    "provider_error_code": exc.code, "last_good_retained": True}
        finally:
            if adapter is not None:
                adapter.close()
        if actual_id == target:
            if not result or not result.get("ok"):
                return {**(result or {}), "status": "provider_local_apply_failed", "outcome": "failed",
                        "action": "none", "error_code": result.get("error_code") if result else "local_apply_failed",
                        "evidence_code": "local_apply_failed", "last_good_retained": True}
            owned = _capture_recovery_context(controller, attempted)
            before_binding = snapshot.get("binding")
            after_binding = owned.get("binding") if owned else None
            receipt = result.get("_owned_receipt") if isinstance(result, dict) else None
            identity_preserved = bool(
                owned and before_binding and after_binding
                and isinstance(receipt, dict)
                and receipt.get("operation_id") == result.get("operation_id")
                and receipt.get("source_ref") == binding["source_ref"]
                and int(receipt.get("binding_revision") or 0) == int(binding["binding_revision"])
                and str(receipt.get("member_id") or "") == target
                and str(receipt.get("location_id") or "") == str(actual_location)
                and str(receipt.get("protocol") or "") == str(binding["protocol"])
                and tuple(before_binding[:7]) == tuple(after_binding[:7])
                and str(after_binding[7] or "") == target
                and str(after_binding[8] or "") == str(actual_location)
                and str(after_binding[9] or "") == str(binding["protocol"])
                and str(after_binding[11] or "") == target
                and str(after_binding[12] or "") == str(binding["protocol"])
            )
            if (not identity_preserved or int(owned["selection_fence"]["revision"]) != int(receipt.get("selection_revision", -1))
                    or owned["pool"] != receipt.get("pool_signature")
                    or owned["runtime_incarnation"] != snapshot["runtime_incarnation"]
                    or receipt.get("runtime_incarnation") != snapshot["runtime_incarnation"]
                    or owned["desired_mode"] != snapshot["desired_mode"]
                    or owned["exclusive"] != snapshot["exclusive"]
                    or owned["credential_fingerprint"] != snapshot["credential_fingerprint"]):
                return {"ok": False, "status": "provider_recovery_stale", "outcome": "deferred",
                        "action": "none", "error_code": "provider_recovery_stale", "last_good_retained": True}
            with xray_writer_guard(timeout_seconds=5.0):
                if not _recovery_context_matches(controller, owned, expected_pending=attempted):
                    return {"ok": False, "status": "provider_recovery_stale", "outcome": "deferred",
                            "action": "none", "error_code": "provider_recovery_stale"}
                confirmed = {key: value for key, value in attempted.items()
                             if key not in {"switch_outcome_unconfirmed", "switch_confirmation_attempted",
                                            "switch_requested_member_id", "switch_previous_member_id"}}
                confirmed["switch_confirmation_result"] = "requested_member"
                if not _cas_pending(attempted, confirmed):
                    return {"ok": False, "status": "provider_recovery_stale", "outcome": "deferred",
                            "action": "none", "error_code": "provider_recovery_stale"}
            return {**result, "status": "provider_switch_confirmed", "evidence_code": "switch_outcome_confirmed"}
        with xray_writer_guard(timeout_seconds=5.0):
            if not _recovery_context_matches(controller, snapshot, expected_pending=attempted):
                return {"ok": False, "status": "provider_recovery_stale", "outcome": "deferred",
                        "action": "none", "error_code": "provider_recovery_stale"}
        return {"ok": False, "status": "switch_outcome_unconfirmed", "outcome": "unconfirmed",
                "action": "none", "error_code": "switch_outcome_unconfirmed",
                "evidence_code": "switch_current_member_unchanged", "last_good_retained": True}
    finally:
        import sys
        reservation.__exit__(*sys.exc_info())

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


def enter_emergency_direct(pending: dict[str, Any], *, controller: Any | None = None,
                           snapshot_context: dict[str, Any] | None = None) -> dict[str, Any]:
    from fwrouter_api.adapters.xray_common import xray_writer_guard, xray_writer_guard_is_held
    if xray_writer_guard_is_held():
        return {"ok": False, "outcome": "deferred", "error_code": "provider_recovery_writer_busy",
                "action": "none", "effective_override": None}
    if controller is None or snapshot_context is None:
        return {"ok": False, "outcome": "deferred", "error_code": "provider_recovery_fence_unavailable",
                "action": "none", "effective_override": None}
    current = get_recovery_pending()
    snapshot = current
    marker = {**pending, "provider_managed": True, "emergency_direct": True, "phase": "emergency_direct_pending"}
    with xray_writer_guard(timeout_seconds=5.0):
        # The exact phase token prevents a stale watchdog decision from taking
        # ownership after another intent/recovery operation has superseded it.
        if (get_recovery_pending() != snapshot
                or snapshot != snapshot_context.get("pending")
                or not _recovery_context_matches(controller, snapshot_context)
                or not _cas_pending(snapshot, marker)):
            return {"ok": False, "outcome": "deferred", "error_code": "provider_recovery_stale",
                    "action": "none", "effective_override": None}
        # Marker precedes Core apply so a crash cannot silently reapply VPN.
        result = _apply_override(reentry=False)
        if get_recovery_pending() != marker:
            return {**result, "outcome": "stale", "error_code": "provider_recovery_stale",
                    "action": "none", "effective_override": None}
        if result.get("ok"):
            terminal = {**marker, "phase": "emergency_direct"}
            if (not _recovery_context_matches(controller, snapshot_context, expected_pending=marker)
                    or not _cas_pending(marker, terminal)):
                return {**result, "outcome": "stale", "error_code": "provider_recovery_stale",
                        "action": "none", "effective_override": None}
            _transition_event("provider_emergency_direct", pending)
        else:
            # Either dataplane may already have changed. Retain the durable
            # override until the shared reconcile path verifies Direct.
            _cas_pending(marker, {**marker, "phase": "emergency_direct_unconfirmed"})
        return {**result, "action": "emergency_direct" if result.get("ok") else "none", "effective_override": "emergency_direct" if result.get("ok") else None}


def _try_verified_reentry(controller: Any, *, timeout_ms: int, reason: str) -> dict[str, Any]:
    from fwrouter_api.adapters.xray_common import xray_writer_guard
    from fwrouter_api.adapters.xray_common import xray_writer_guard_is_held
    if xray_writer_guard_is_held():
        return {"ok": False, "status": "provider_recovery_deferred", "action": "none",
                "effective_override": "emergency_direct", "error_code": "provider_recovery_writer_busy"}
    pending = emergency_override()
    if pending is None:
        return {"ok": True, "action": "none"}
    if pending.get("switch_outcome_unconfirmed"):
        return {"ok": False, "status": "provider_switch_outcome_unconfirmed", "action": "none",
                "effective_override": "emergency_direct", "error_code": "switch_outcome_unconfirmed"}
    if pending.get("phase") == "reentry_verification":
        started = float(pending.get("reentry_started_at") or 0)
        if started > 0 and time.time() - started < max(120.0, timeout_ms / 1000.0 * 4):
            return {"ok": False, "status": "provider_reentry_in_progress", "action": "none",
                    "effective_override": "emergency_direct", "outcome": "deferred"}
        # A timed-out claim may belong to a process that died after the VPN
        # apply. Re-establish Direct from the original intent before probing.
        stale_claim = {**pending, "phase": "emergency_direct_unconfirmed"}
        stale_claim.pop("reentry_operation_id", None)
        stale_claim.pop("reentry_started_at", None)
        with xray_writer_guard(timeout_seconds=5.0):
            if not _cas_pending(pending, stale_claim):
                return {"ok": False, "status": "provider_recovery_stale", "action": "none",
                        "effective_override": "emergency_direct", "error_code": "provider_recovery_stale"}
        pending = stale_claim
    from fwrouter_api.services.vpn_auto_exclusive import get_vpn_auto_exclusive_source_ref
    from fwrouter_api.services.auto_eligibility import server_is_in_exclusive_pool
    exclusive_ref = get_vpn_auto_exclusive_source_ref()
    if exclusive_ref:
        with db_session() as connection:
            allowed = server_is_in_exclusive_pool(connection, str(pending.get("logical_server_id") or ""), exclusive_ref)
        if not allowed:
            return {"ok": True, "status": "provider_reentry_suppressed_by_exclusive_source", "action": "none",
                    "effective_override": "emergency_direct", "pending_preserved": True}
    snapshot = _capture_recovery_context(controller, pending)
    if snapshot is None:
        return {"ok": False, "status": "emergency_direct", "action": "none",
                "effective_override": "emergency_direct", "error_code": "provider_recovery_fence_unavailable"}
    if str(snapshot.get("active_target_id") or "") != str(pending.get("logical_server_id") or ""):
        return {"ok": False, "status": "provider_recovery_stale", "action": "none",
                "effective_override": "emergency_direct", "error_code": "provider_recovery_stale"}
    if pending.get("phase") == "emergency_direct_unconfirmed":
        with xray_writer_guard(timeout_seconds=5.0):
            if not _recovery_context_matches(controller, snapshot):
                return {"ok": False, "status": "provider_recovery_stale", "action": "none",
                        "effective_override": "emergency_direct", "error_code": "provider_recovery_stale"}
            repaired = _apply_override(reentry=False)
            if not repaired.get("ok"):
                return {"ok": False, "status": "emergency_direct_unconfirmed", "action": "none",
                        "effective_override": "emergency_direct", "fallback_verified": False}
            if not _recovery_context_matches(controller, snapshot, expected_pending=pending,
                                             expected_target=str(pending.get("logical_server_id") or "")):
                _cas_pending(pending, {**pending, "phase": "emergency_direct_unconfirmed"})
                return {"ok": False, "status": "provider_recovery_stale", "action": "none",
                        "effective_override": "emergency_direct", "fallback_verified": False,
                        "error_code": "provider_recovery_stale"}
            updated = {**pending, "phase": "emergency_direct"}
            if not _cas_pending(pending, updated):
                return {"ok": False, "status": "provider_recovery_stale", "action": "none",
                        "effective_override": "emergency_direct", "error_code": "provider_recovery_stale"}
            pending = updated
        snapshot = _capture_recovery_context(controller, pending)
        if snapshot is None:
            return {"ok": False, "status": "emergency_direct", "action": "none",
                    "effective_override": "emergency_direct", "error_code": "provider_recovery_fence_unavailable"}

    # The preflight is a potentially slow network/runtime probe and always runs
    # before entering the shared writer guard.
    probe = controller.probe(update_ping_state=True, timeout_ms=timeout_ms, reason=reason)
    if not probe or not probe.get("ok"):
        return {"ok": False, "status": "emergency_direct", "action": "none", "effective_override": "emergency_direct"}

    # Revalidate all ownership immediately before Core apply. Keep the guard
    # through apply/readback so a concurrent intent cannot be overwritten.
    import uuid
    claimed = {**pending, "phase": "reentry_verification",
               "reentry_operation_id": str(uuid.uuid4()), "reentry_started_at": time.time()}
    with xray_writer_guard(timeout_seconds=5.0):
        if not _recovery_context_matches(controller, snapshot):
            return {"ok": False, "status": "provider_recovery_stale", "action": "none",
                    "effective_override": "emergency_direct", "error_code": "provider_recovery_stale"}
        if not _cas_pending(pending, claimed):
            return {"ok": False, "status": "provider_reentry_in_progress", "action": "none",
                    "effective_override": "emergency_direct", "outcome": "deferred"}
        snapshot = {**snapshot, "pending": claimed}
        result = _apply_override(reentry=True)
        # The fenced native selection readback is deliberately minimal; full
        # controller health aggregation can take several seconds under guard.
        exact_target = _recovery_context_matches(
            controller, snapshot, expected_pending=claimed,
            expected_target=str(pending.get("logical_server_id") or ""),
        )
        if not exact_target:
            # The claim is still ours, but the applied target no longer matches
            # the captured Core target. Keep the override and require a fresh
            # Direct repair before another verification attempt.
            _cas_pending(claimed, {**pending, "phase": "emergency_direct_unconfirmed"})
            return {"ok": False, "status": "provider_recovery_stale", "action": "none",
                    "effective_override": "emergency_direct", "error_code": "provider_recovery_stale"}

    # Post-apply connectivity is also outside the guard. A newer operation may
    # win during this probe, so terminal/fallback actions are fenced again.
    post_probe = controller.probe(update_ping_state=True, timeout_ms=timeout_ms, reason=reason) if result.get("ok") and exact_target else None
    if result.get("ok") and exact_target and post_probe and post_probe.get("ok"):
        with xray_writer_guard(timeout_seconds=5.0):
            if not _recovery_context_matches(controller, snapshot, expected_pending=claimed,
                                             expected_target=str(pending.get("logical_server_id") or "")):
                return {"ok": False, "status": "provider_recovery_stale", "action": "none",
                        "effective_override": "emergency_direct", "error_code": "provider_recovery_stale"}
            from fwrouter_api.services.watchdog_failure_state import complete_recovery_pending
            if not complete_recovery_pending(claimed):
                return {"ok": False, "status": "provider_recovery_stale", "action": "none",
                        "effective_override": "emergency_direct", "error_code": "provider_recovery_stale"}
            _transition_event("provider_vpn_reentry", pending)
        return {"ok": True, "status": "provider_vpn_recovered", "action": "verified_vpn_reentry", "effective_override": None}

    # Restore Direct only while the exact same selection, runtime and incident
    # still own the operation; otherwise leave the newer operation untouched.
    with xray_writer_guard(timeout_seconds=5.0):
        fallback_target = (str(pending.get("logical_server_id") or "")
                           if result.get("ok") and exact_target else snapshot["active_target_id"])
        if not _recovery_context_matches(controller, snapshot, expected_pending=claimed,
                                         expected_target=fallback_target):
            return {"ok": False, "status": "provider_recovery_stale", "action": "none",
                    "effective_override": "emergency_direct", "error_code": "provider_recovery_stale"}
        fallback = _apply_override(reentry=False)
        fallback_owned = bool(
            fallback.get("ok")
            and _recovery_context_matches(
                controller, snapshot, expected_pending=claimed,
                expected_target=str(snapshot.get("active_target_id") or ""),
            )
        )
        if not fallback_owned:
            _cas_pending(claimed, {**pending, "phase": "emergency_direct_unconfirmed"})
        else:
            _cas_pending(claimed, pending)
    return {"ok": False, "status": "emergency_direct" if fallback_owned else "emergency_direct_unconfirmed",
            "action": "none", "effective_override": "emergency_direct", "fallback_verified": fallback_owned,
            **({"error_code": "provider_recovery_stale"} if fallback.get("ok") and not fallback_owned else {})}


def try_verified_reentry(controller: Any, *, timeout_ms: int, reason: str) -> dict[str, Any]:
    try:
        return _try_verified_reentry(controller, timeout_ms=timeout_ms, reason=reason)
    except TimeoutError:
        return {"ok": False, "status": "provider_recovery_deferred", "action": "none",
                "effective_override": "emergency_direct", "error_code": "provider_recovery_writer_busy"}


def _confirmed_provider_recovery(*, logical_server_id: str | None, path_key: str | None,
                                decision_id: str | None, controller: Any, timeout_ms: int,
                                allow_switch: bool) -> dict[str, Any] | None:
    from fwrouter_api.services.provider_managed import binding_for_logical, execute_provider_operation, provider_candidates
    from fwrouter_api.adapters.xray_common import xray_writer_guard, xray_writer_guard_is_held
    if xray_writer_guard_is_held():
        return {"ok": False, "status": "provider_recovery_deferred", "outcome": "deferred",
                "action": "none", "error_code": "provider_recovery_writer_busy"}
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
    previous_pending = get_recovery_pending()
    pending = previous_pending or {}
    if (pending.get("switch_outcome_unconfirmed")
            and pending.get("source_ref") == binding["source_ref"]
            and pending.get("binding_revision") != binding["binding_revision"]):
        return {"ok": False, "status": "switch_outcome_unconfirmed", "outcome": "unconfirmed",
                "action": "none", "error_code": "switch_outcome_unconfirmed",
                "effective_override": "emergency_direct" if pending.get("emergency_direct") else None,
                "last_good_retained": True}
    if pending.get("source_ref") != binding["source_ref"] or pending.get("binding_revision") != binding["binding_revision"]:
        pending = {}
    if pending.get("switch_outcome_unconfirmed"):
        if not pending.get("switch_confirmation_attempted"):
            return _confirm_unconfirmed_switch(binding, pending, controller)
        if not decision_id or decision_id == pending.get("traffic_decision_id"):
            return {"ok": False, "status": "switch_outcome_unconfirmed", "outcome": "unconfirmed",
                    "action": "none", "error_code": "switch_outcome_unconfirmed",
                    "effective_override": "emergency_direct" if pending.get("emergency_direct") else None,
                    "last_good_retained": True}
        # The one bounded read-only check was exhausted. A distinct confirmed
        # watchdog decision may advance to local connectivity confirmation and
        # Emergency Direct, while the remote mutation marker remains durable.
    if not decision_id or decision_id == pending.get("traffic_decision_id"):
        return {"ok": True, "status": "provider_recovery_pending", "action": "none"}
    if float(pending.get("not_before") or 0) > time.time():
        return {"ok": False, "status": "provider_recovery_pending", "outcome": "deferred", "action": "none", "not_before": pending["not_before"]}
    phase = min(3, int(pending.get("provider_confirmation", 0)) + 1)
    pending = {**pending, "provider_managed": True, "source_ref": binding["source_ref"], "binding_revision": binding["binding_revision"],
               "logical_server_id": logical_server_id, "path_key": path_key, "phase": "provider_recovery",
               "provider_confirmation": phase, "traffic_decision_id": decision_id}
    with xray_writer_guard(timeout_seconds=5.0):
        if get_recovery_pending() != previous_pending or not _cas_pending(previous_pending, pending):
            return {"ok": False, "status": "provider_recovery_stale", "outcome": "deferred",
                    "action": "none", "error_code": "provider_recovery_stale"}
    if phase == 1:
        phase_snapshot = _capture_recovery_context(controller, pending)
        if phase_snapshot is None:
            result = {"ok": False, "outcome": "deferred", "error_code": "provider_recovery_fence_unavailable"}
        else:
            result = execute_provider_operation(binding["source_ref"], "recovery_refresh",
                expected_member_id=str(binding.get("current_member_id") or ""),
                expected_revision=binding["binding_revision"],
                expected_selection_revision=int(phase_snapshot["selection_fence"]["revision"]),
                expected_selection_pool_signature=phase_snapshot["pool"],
                expected_runtime_incarnation=phase_snapshot["runtime_incarnation"])
    elif phase == 2:
        if not _automatic_member_switch_allowed(binding["source_ref"]):
            # The confirmed traffic failure remains in the existing recovery
            # state machine. Skip provider status, candidate selection,
            # discovery and PATCH; the next distinct confirmation reaches the
            # existing local probe / Emergency Direct phase.
            return _record_policy_disabled(pending, phase)
        from fwrouter_api.services.provider_adapters import provider_adapter, RequestBudget, ProviderError
        from fwrouter_api.services.provider_managed import store, provider_operation_reservation
        adapter = None
        budget = RequestBudget(4, 30, operation="recovery_confirmation_2")
        reservation = provider_operation_reservation()
        reservation_owned = False
        phase_snapshot = _capture_recovery_context(controller, pending)
        if phase_snapshot is None:
            result = {"ok": False, "outcome": "deferred", "error_code": "provider_recovery_fence_unavailable"}
            adapter = None
        else:
            result = None
        try:
            if phase_snapshot is None:
                raise StopIteration
            reservation.__enter__()
            reservation_owned = True
            adapter = provider_adapter(binding["provider_id"], f"{binding['source_ref']}:{binding['binding_revision']}", source_ref=binding["source_ref"])
            stats = adapter.get_server_stats(int(binding["resource_id"]), budget=budget, max_age_s=0)
            stats_observed_at = time.time()
            with xray_writer_guard(timeout_seconds=5.0):
                if not _recovery_context_matches(controller, phase_snapshot):
                    result = {"ok": False, "outcome": "deferred", "error_code": "provider_recovery_stale"}
                else:
                    with db_session() as conn:
                        store.update_observation(conn, binding["source_ref"], kind="recovery_status", safe_data={**stats, "member_id": binding["current_member_id"], "protocol": binding["observed_protocol"]},
                                                 scope_key=f"{binding['current_member_id']}:{binding['observed_protocol']}",
                                                 revision=binding["binding_revision"], expected_binding_revision=binding["binding_revision"])
            if result is not None:
                raise StopIteration
            if stats.get("status") in {"down", "unavailable"}:
                confirmed_down_code = ("provider_member_confirmed_down" if stats.get("status") == "down"
                                       else "provider_member_explicitly_unavailable")
                if not _automatic_member_switch_allowed(binding["source_ref"]):
                    return _record_policy_disabled(pending, phase)
                candidates = provider_candidates(binding["source_ref"])
                owned_fence = phase_snapshot["selection_fence"]
                owned_pool = phase_snapshot["pool"]
                if not candidates:
                    if not _automatic_member_switch_allowed(binding["source_ref"]):
                        return _record_policy_disabled(pending, phase)
                    members = adapter.discover(int(binding["current_location_id"]), binding["protocol"], budget=budget)
                    with xray_writer_guard(timeout_seconds=5.0):
                        if not _recovery_context_matches(controller, phase_snapshot):
                            result = {"ok": False, "outcome": "deferred", "error_code": "provider_recovery_stale"}
                            raise StopIteration
                        with db_session() as conn:
                            from fwrouter_api.services.provider_managed import _record_discovery_revisioned
                            _record_discovery_revisioned(conn, binding["source_ref"], binding["binding_revision"],
                                binding["current_location_id"], binding["protocol"], members,
                                expected_binding_revision=binding["binding_revision"])
                            from fwrouter_api.services.vpn_auto_selection_state import read_selection_fence, selection_pool_signature
                            owned_fence = read_selection_fence(conn)
                            owned_pool = selection_pool_signature(conn)
                    phase_snapshot = {**phase_snapshot, "selection_fence": owned_fence, "pool": owned_pool}
                    candidates = provider_candidates(binding["source_ref"])
                from fwrouter_api.services.selector import _select_candidate_with_priority
                selected, _ = _select_candidate_with_priority(candidates)
                with xray_writer_guard(timeout_seconds=5.0):
                    if not _recovery_context_matches(controller, phase_snapshot):
                        result = {"ok": False, "outcome": "deferred", "error_code": "provider_recovery_stale"}
                        raise StopIteration
                if not _provider_down_evidence_is_fresh(stats_observed_at):
                    result = {"ok": False, "outcome": "deferred", "error_code": "provider_evidence_stale",
                              "evidence_code": "switch_not_attempted", "switch_attempted": False,
                              "last_good_retained": True}
                    raise StopIteration
                if not _automatic_member_switch_allowed(binding["source_ref"]):
                    return _record_policy_disabled(pending, phase)
                result = execute_provider_operation(binding["source_ref"], "switch", member_id=selected["member_id"],
                                                     expected_revision=binding["binding_revision"],
                                                     expected_selection_revision=int(phase_snapshot["selection_fence"]["revision"]),
                                                     expected_selection_pool_signature=phase_snapshot["pool"],
                                                     expected_runtime_incarnation=phase_snapshot["runtime_incarnation"],
                                                     _adapter=adapter, _budget=budget,
                                                     automatic_switch=True) if selected else {"ok": False, "outcome": "no_candidate"}
                result.setdefault("evidence_code", confirmed_down_code)
                result["switch_attempted"] = bool(result.get("mutation_attempted"))
            elif stats.get("status") == "up":
                result = execute_provider_operation(
                    binding["source_ref"], "recovery_refresh",
                    expected_member_id=str(binding.get("current_member_id") or ""),
                    expected_revision=binding["binding_revision"],
                    expected_selection_revision=int(phase_snapshot["selection_fence"]["revision"]),
                    expected_selection_pool_signature=phase_snapshot["pool"],
                    expected_runtime_incarnation=phase_snapshot["runtime_incarnation"],
                    _adapter=adapter, _budget=budget,
                )
            else:
                result = {"ok": False, "outcome": "unknown", "error_code": "provider_response_unknown",
                          "switch_attempted": False, "last_good_retained": True}
        except StopIteration:
            pass
        except ProviderError as exc:
            if exc.code == "PROVIDER_OPERATION_BUSY":
                result = {"ok": False, "outcome": "deferred", "error_code": "provider_operation_busy"}
            else:
                from fwrouter_api.adapters.provider_base import recovery_evidence_code
                result = {"ok": False, "outcome": "unknown", "error_code": recovery_evidence_code(exc),
                          "provider_error_code": exc.code,
                          "switch_attempted": False, "last_good_retained": True,
                          "not_before": time.time()+exc.retry_after_seconds if exc.retry_after_seconds is not None else None}
        finally:
            try:
                if adapter is not None:
                    adapter.close()
            finally:
                if reservation_owned:
                    import sys
                    reservation.__exit__(*sys.exc_info())
    else:
        # Third normal confirmation alone does not prove current connectivity absence.
        snapshot_context = _capture_recovery_context(controller, pending)
        if snapshot_context is None:
            result = {"ok": False, "outcome": "deferred", "error_code": "provider_recovery_fence_unavailable",
                      "action": "none"}
        else:
            # Probe with no writer guard, then CAS terminal completion or Direct
            # apply against this exact incident and runtime generation.
            probe = controller.probe(update_ping_state=True, timeout_ms=timeout_ms, reason="provider_emergency_preflight")
            if probe and probe.get("ok"):
                if pending.get("switch_outcome_unconfirmed"):
                    return {"ok": False, "status": "local_connectivity_restored_but_mutation_unconfirmed", "outcome": "unconfirmed",
                            "action": "none", "error_code": "switch_outcome_unconfirmed",
                            "effective_override": ("emergency_direct" if pending.get("emergency_direct") else None),
                            "local_connectivity_restored": True, "last_good_retained": True}
                with xray_writer_guard(timeout_seconds=5.0):
                    if not _recovery_context_matches(controller, snapshot_context):
                        return {"ok": False, "status": "provider_recovery_stale", "outcome": "deferred",
                                "action": "none", "error_code": "provider_recovery_stale"}
                    from fwrouter_api.services.watchdog_failure_state import complete_recovery_pending
                    if not complete_recovery_pending(pending):
                        return {"ok": False, "status": "provider_recovery_stale", "outcome": "deferred",
                                "action": "none", "error_code": "provider_recovery_stale"}
                return {"ok": True, "status": "provider_vpn_recovered", "action": "observe_recovery"}
            result = enter_emergency_direct(pending, controller=controller, snapshot_context=snapshot_context)
    if phase < 3:
        raw_error = str(result.get("error_code") or "")
        if raw_error in {"PROVIDER_BINDING_REVISION_CONFLICT", "VPN_AUTO_SELECTION_STALE_STATE",
                         "VPN_AUTO_SELECTION_STALE_RUNTIME_EVIDENCE"}:
            result = {**result, "error_code": "provider_recovery_stale", "fence_error_code": raw_error}
        elif raw_error and raw_error.startswith("PROVIDER_") and raw_error not in {
            "PROVIDER_LOCAL_VERIFICATION_FAILED", "PROVIDER_EFFECTIVE_TARGET_UNCONFIRMED",
            "PROVIDER_MEMBER_READBACK_UNCONFIRMED", "PROVIDER_CONNECTIVITY_UNCONFIRMED",
        }:
            from fwrouter_api.adapters.provider_base import ProviderError, recovery_evidence_code
            result = {**result, "provider_error_code": raw_error,
                      "error_code": recovery_evidence_code(ProviderError(raw_error,
                          status_code=result.get("provider_status_code")))}
        elif raw_error in {"PROVIDER_LOCAL_VERIFICATION_FAILED", "PROVIDER_EFFECTIVE_TARGET_UNCONFIRMED",
                           "PROVIDER_MEMBER_READBACK_UNCONFIRMED"}:
            result = {**result, "error_code": "readback_failed"}
        elif raw_error == "PROVIDER_CONNECTIVITY_UNCONFIRMED":
            result = {**result, "error_code": "local_apply_failed"}
        outcome_pending = {**pending, "last_outcome": result.get("outcome"),
                           "error_code": result.get("error_code") or result.get("evidence_code"),
                           "not_before": result.get("not_before")}
        if (result.get("outcome") == "unconfirmed" and phase == 2
                and bool(result.get("mutation_attempted"))):
            switch_reason = result.get("error_code")
            outcome_pending.update({"switch_outcome_unconfirmed": True,
                                    "error_code": "switch_outcome_unconfirmed",
                                    "switch_confirmation_reason": switch_reason,
                                    "switch_requested_member_id": result.get("requested_member_id"),
                                    "switch_previous_member_id": binding.get("current_member_id")})
            result = {**result, "error_code": "switch_outcome_unconfirmed",
                      "switch_confirmation_reason": switch_reason}
            if str(switch_reason or "").startswith("provider_api_") or switch_reason == "provider_response_unknown":
                outcome_pending["switch_api_error_code"] = switch_reason
                result["provider_api_error_code"] = switch_reason
        with xray_writer_guard(timeout_seconds=5.0):
            if not _cas_pending(pending, outcome_pending):
                return {"ok": False, "status": "provider_recovery_stale", "outcome": "deferred",
                        "action": "none", "error_code": "provider_recovery_stale"}
    status = ("emergency_direct" if result.get("effective_override") else
              result.get("status") if result.get("status") == "provider_auto_switch_disabled" else
              "provider_recovery_pending")
    return {**result, "status": status,
            "provider_confirmation": phase, "action": result.get("action", "provider_recovery"), "provider_recovery": True}


def confirmed_provider_recovery(*, logical_server_id: str | None, path_key: str | None,
                                decision_id: str | None, controller: Any, timeout_ms: int,
                                allow_switch: bool) -> dict[str, Any] | None:
    try:
        return _confirmed_provider_recovery(logical_server_id=logical_server_id, path_key=path_key,
            decision_id=decision_id, controller=controller, timeout_ms=timeout_ms, allow_switch=allow_switch)
    except TimeoutError:
        return {"ok": False, "status": "provider_recovery_deferred", "outcome": "deferred",
                "action": "none", "error_code": "provider_recovery_writer_busy",
                "last_good_retained": True}
