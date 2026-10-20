import hashlib

import pytest

from fwrouter_api.services import apply_orchestrator as orchestrator
from fwrouter_api.services import apply_orchestrator_public as public_api
from fwrouter_api.services import apply_orchestrator_server_handlers as server_handlers
from fwrouter_api.services import rules, rules_state
from fwrouter_api.services.apply_orchestrator import _commit_manual_rules_apply
from fwrouter_api.services.apply_orchestrator_rules_handlers import _execute_apply_manual_rules
from fwrouter_api.services.rules import validate_manual_rules


def test_apply_orchestrator_preserves_explicit_facade_exports():
    expected_private_runtime_attrs = {
        "INTENT_APPLY_MANUAL_RULES",
        "INTENT_CLEAR_SUBJECT_SERVER_OVERRIDE",
        "INTENT_CLEAR_SUBJECT_USER_MODE",
        "INTENT_REPAIR_GLOBAL_DIRECT_RUNTIME",
        "INTENT_SET_GLOBAL_MODE",
        "INTENT_SET_GLOBAL_SERVER_MODE",
        "INTENT_SET_SELECTIVE_DEFAULT",
        "INTENT_SET_SUBJECT_ADMIN_MODE",
        "INTENT_SET_SUBJECT_SERVER_OVERRIDE",
        "INTENT_SET_SUBJECT_USER_MODE",
        "JOB_TYPE_APPLY_MUTATION",
        "LOCK_APPLY",
        "LOCK_RULES",
        "LOCK_RULES_APPLY",
        "GLOBAL_ROUTING_DRIFT_CODE",
        "GLOBAL_ARTIFACT_DRIFT_CODE",
        "LIVE_DATAPLANE_ARTIFACT_DRIFT_CODE",
        "DEFAULT_MIHOMO_ADAPTER",
        "_build_failure_result",
        "_current_routing_drift",
        "_run_pipeline_for_state",
        "external_vpn_mihomo_reconcile_skip",
        "get_default_job_manager",
        "reconcile_mihomo_runtime",
        "write_operational_log",
        "write_technical_log",
    }
    expected_public_star_exports = {
        "ADMIN_MODES_BY_SUBJECT_TYPE", "Any", "ApplyMode", "ApplyOrchestrator",
        "DEFAULT_MIHOMO_ADAPTER", "GLOBAL_ARTIFACT_DRIFT_CODE", "GLOBAL_ROUTING_DRIFT_CODE",
        "INTENT_APPLY_MANUAL_RULES", "INTENT_CLEAR_SUBJECT_SERVER_OVERRIDE",
        "INTENT_CLEAR_SUBJECT_USER_MODE", "INTENT_REPAIR_GLOBAL_DIRECT_RUNTIME",
        "INTENT_SET_GLOBAL_MODE", "INTENT_SET_GLOBAL_SERVER_MODE", "INTENT_SET_SELECTIVE_DEFAULT",
        "INTENT_SET_SUBJECT_ADMIN_MODE", "INTENT_SET_SUBJECT_SERVER_OVERRIDE",
        "INTENT_SET_SUBJECT_USER_MODE", "JOB_TYPE_APPLY_MUTATION", "JobLockConflictError",
        "LIVE_DATAPLANE_ARTIFACT_DRIFT_CODE", "LOCK_APPLY", "LOCK_RULES", "LOCK_RULES_APPLY",
        "SERVER_OVERRIDE_SUBJECT_TYPES", "USER_MODES", "USER_OVERRIDE_TTL_DAYS",
        "VIRTUAL_XRAY_VPN_AUTO_SERVER_ID", "applied_nft_markers_match_live",
        "apply_global_mode_immediately", "apply_manual_rules", "build_runtime_enforcement_state",
        "clear_subject_server_override", "clear_subject_user_mode", "effective_rules_with_selective_default",
        "enrich_subject_with_effective_state", "ensure_routing_global_state", "execute_apply_mutation",
        "explicit_external_client_allows_virtual_vpn_auto", "explicit_external_client_runtime_binding",
        "external_vpn_mihomo_reconcile_skip", "finalize_manual_rules_apply", "get_core_bypass_state",
        "get_dataplane_capability", "get_default_job_manager", "get_job", "get_manual_rules_texts",
        "get_routing_global_state", "get_routing_snapshot", "get_server", "get_subject",
        "get_subject_server_override", "get_subject_with_effective_state",
        "is_explicit_external_client_subject_type", "list_subjects", "load_precompiled_global_mode_profile",
        "mark_rules_job_failed", "mark_rules_job_running",
        "materialize_explicit_external_client_runtime_bindings", "materialize_precompiled_manifest",
        "materialize_xray_runtime_bindings", "mihomo_runtime_satisfies_routing",
        "prepare_manual_rules_candidate", "probe_live_global_mode", "read_applied_manifest",
        "read_effective_rules_artifact", "reconcile_current_routing_if_drift", "reconcile_mihomo_runtime",
        "reconcile_mihomo_selective_default_fast", "repair_global_direct_runtime",
        "repair_global_direct_runtime_sync", "run_apply_mutation", "run_apply_pipeline", "set_global_mode",
        "set_selective_default", "set_subject_admin_mode", "set_subject_mode",
        "set_subject_server_override", "set_subject_user_mode", "subject_follows_global_mode",
        "subject_selector_name", "submit_apply_mutation", "sync_active_selective_default", "touch_job_running",
        "update_subject_server_override_apply_status", "validate_global_mode_request", "validate_manual_rules",
        "write_operational_log", "write_technical_log",
    }

    namespace: dict[str, object] = {}
    exec("from fwrouter_api.services.apply_orchestrator import *", namespace)

    assert set(orchestrator.__all__) == expected_public_star_exports
    assert set(namespace) - {"__builtins__"} == expected_public_star_exports
    assert all(hasattr(orchestrator, name) for name in expected_private_runtime_attrs)
    assert orchestrator.ApplyOrchestrator is public_api.ApplyOrchestrator
    assert orchestrator.set_global_mode is public_api.set_global_mode


def test_subject_server_override_reconciles_missing_selector(monkeypatch):
    subject_id = "subject-1"
    server_id = "server-1"
    subject = {"subject_id": subject_id, "subject_type": "lan_client", "desired_mode": "vpn"}
    switches = iter((
        {"ok": False, "error_code": "MIHOMO_SELECTOR_NOT_FOUND"},
        {"ok": True},
    ))
    reconciles: list[tuple[dict[str, object], str]] = []

    monkeypatch.setattr(orchestrator, "get_subject_server_override", lambda _subject_id: None)
    monkeypatch.setattr(orchestrator, "get_subject", lambda _subject_id: subject)
    monkeypatch.setattr(orchestrator, "_validate_subject_server_override_request", lambda *_args: None)
    monkeypatch.setattr(orchestrator, "set_subject_server_override", lambda *_args, **_kwargs: {"ok": True})
    monkeypatch.setattr(orchestrator, "get_routing_snapshot", lambda: {"desired_mode": "vpn"})
    monkeypatch.setattr(server_handlers, "_subject_needs_mihomo_selector_from_committed", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(server_handlers, "_switch_subject_mihomo_selector", lambda *_args: next(switches))
    monkeypatch.setattr(orchestrator, "external_vpn_mihomo_reconcile_skip", lambda: None)

    def reconcile(*, routing: dict[str, object], job_id: str) -> dict[str, object]:
        reconciles.append((routing, job_id))
        return {"ok": True, "reconcile_id": "reconcile-1"}

    monkeypatch.setattr(orchestrator, "reconcile_mihomo_runtime", reconcile)
    monkeypatch.setattr(orchestrator, "update_subject_server_override_apply_status", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(orchestrator, "get_subject_with_effective_state", lambda _subject_id: subject)
    monkeypatch.setattr(orchestrator, "_build_success_result", lambda **kwargs: {"ok": True, **kwargs})

    result = server_handlers._execute_set_subject_server_override(
        {"job_id": "job-1", "requested_by": "test"},
        {"subject_id": subject_id, "server_id": server_id, "actor_scope": "admin"},
    )

    assert result["ok"] is True
    assert reconciles == [({"desired_mode": "vpn"}, "job-1")]
    assert result["details"]["mihomo_reconcile"]["reconcile_id"] == "reconcile-1"


def test_commit_manual_rules_apply_passes_manual_active_text(monkeypatch):
    captured: dict[str, object] = {}

    def fake_finalize_manual_rules_apply(**kwargs):
        captured.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(
        "fwrouter_api.services.apply_orchestrator.finalize_manual_rules_apply",
        fake_finalize_manual_rules_apply,
    )

    result = _commit_manual_rules_apply(
        job_id="job-1",
        draft_text="VPN .facebook.com\n",
        effective_artifact={"rules": []},
        runtime_enforcement={"enforcement_level": "global_selective_enforced"},
    )

    assert result == {"ok": True}
    assert captured == {
        "job_id": "job-1",
        "manual_active_text": "VPN .facebook.com\n",
        "effective_artifact": {"rules": []},
        "runtime_enforcement": {"enforcement_level": "global_selective_enforced"},
        "audit_change": None,
    }


def test_manual_rules_apply_audit_counts_prior_rules(monkeypatch):
    previous_active_text = "DIRECT old.example\n"
    manual_validation = validate_manual_rules("VPN new.example\n")
    captured: dict[str, object] = {}

    monkeypatch.setattr(orchestrator, "mark_rules_job_running", lambda **_kwargs: None)
    monkeypatch.setattr(orchestrator, "get_manual_rules_texts",
                        lambda: {"active_text": previous_active_text})
    monkeypatch.setattr(orchestrator, "prepare_manual_rules_candidate", lambda **_kwargs: {
        "manual_validation": manual_validation,
        "effective_artifact": {"rules": []},
    })
    monkeypatch.setattr(orchestrator, "get_routing_snapshot", lambda: {})
    monkeypatch.setattr(orchestrator, "_load_subjects_with_overrides", lambda **_kwargs: [])
    monkeypatch.setattr(orchestrator, "_load_user_override_map", lambda: {})
    monkeypatch.setattr(orchestrator, "_load_server_override_map", lambda: {})
    monkeypatch.setattr(orchestrator, "_run_pipeline_for_state", lambda **_kwargs: {
        "ok": True,
        "dataplane_capability": "nft_owned_table",
        "enforcement_level": "owned_table_ready",
        "traffic_enforcement_guaranteed": True,
        "supported_modes": {"direct": True, "vpn": True},
        "missing_runtime_requirements": [],
        "apply_id": "apply-1",
    })

    def capture_commit(**kwargs):
        captured.update(kwargs)
        return {"state": "active", "active_text": kwargs["draft_text"]}

    monkeypatch.setattr(orchestrator, "_commit_manual_rules_apply", capture_commit)
    monkeypatch.setattr(orchestrator, "_sync_subject_server_override_statuses", lambda _subjects: None)
    monkeypatch.setattr(orchestrator, "_build_success_result", lambda **kwargs: {
        "ok": True,
        "details": kwargs["details"],
    })

    result = _execute_apply_manual_rules(
        {"job_id": "job-1", "requested_by": "pytest:manual-apply"},
        {},
    )

    assert result["ok"] is True
    assert orchestrator.validate_manual_rules(previous_active_text)["valid"] is True
    audit = captured["audit_change"]
    assert audit["requested_by"] == "pytest:manual-apply"
    assert audit["previous_value"] == {
        "sha256": hashlib.sha256(previous_active_text.encode("utf-8")).hexdigest(),
        "rule_count": 1,
    }
    assert audit["new_value"]["rule_count"] == 1


def test_mark_rules_job_success_forwards_audit_change_without_mutating_it(monkeypatch):
    captured: list[dict[str, object]] = []
    audit_change = {
        "changed": True,
        "requested_by": "pytest:manual-apply",
        "previous_value": {"sha256": "prior-hash", "rule_count": 1},
        "new_value": {"sha256": "next-hash", "rule_count": 1},
    }

    def fake_mark_rules_job_success(**kwargs):
        captured.append(kwargs)
        return {"status": "success"}

    monkeypatch.setattr(rules_state, "mark_rules_job_success", fake_mark_rules_job_success)
    assert rules.mark_rules_job_success(
        job_id="job-1", update_type="manual_apply", audit_change=audit_change,
    ) == {"status": "success"}
    assert rules.mark_rules_job_success(job_id="job-2", update_type="manual_apply") == {
        "status": "success",
    }
    assert captured == [
        {"job_id": "job-1", "update_type": "manual_apply", "audit_change": audit_change},
        {"job_id": "job-2", "update_type": "manual_apply", "audit_change": None},
    ]
    assert captured[0]["audit_change"] is audit_change


def test_generic_server_mode_holds_shared_owner_across_native_and_commit(monkeypatch):
    """Generic mode apply must not interleave the fixed owner between native effect and commit."""
    from fwrouter_api.adapters.xray_common import xray_writer_guard_is_held
    from fwrouter_api.db.connection import db_session
    from fwrouter_api.services import apply_orchestrator_global_handlers as handlers
    from fwrouter_api.services.server_state import ensure_routing_global_state

    ensure_routing_global_state()
    phases = []

    def owned_phase(name, value):
        assert xray_writer_guard_is_held(), name
        phases.append(name)
        return value

    monkeypatch.setattr(orchestrator, "_current_routing_drift", lambda **kwargs: {"detected": False})
    monkeypatch.setattr(orchestrator, "_applied_manifest_routing_drift", lambda **kwargs: {"detected": False})
    monkeypatch.setattr(orchestrator, "validate_global_mode_request", lambda *args, **kwargs: {"ok": True})
    monkeypatch.setattr(orchestrator, "touch_job_running", lambda *args: None)
    monkeypatch.setattr(handlers, "_reconcile_vpn_runtime_for_apply",
                        lambda **kwargs: owned_phase("generation", {"ok": True}))
    monkeypatch.setattr(orchestrator, "_load_user_override_map", lambda: {})
    monkeypatch.setattr(orchestrator, "_load_server_override_map", lambda: {})
    monkeypatch.setattr(orchestrator, "_load_subjects_with_overrides", lambda **kwargs: [])
    monkeypatch.setattr(orchestrator, "_run_pipeline_for_state",
                        lambda **kwargs: owned_phase("native_effect", {"ok": True, "apply_id": None}))
    real_commit = orchestrator._commit_global_server_mode

    def commit(**kwargs):
        owned_phase("commit", None)
        return real_commit(**kwargs)

    monkeypatch.setattr(orchestrator, "_commit_global_server_mode", commit)
    monkeypatch.setattr(orchestrator, "_build_success_result", lambda **kwargs: {"ok": True, **kwargs})
    result = handlers._execute_set_global_server_mode(
        {"job_id": "synthetic-generic-fixed", "requested_by": "pytest"}, {"server_mode": "fixed"},
    )
    assert result["ok"] is True
    assert phases == ["generation", "native_effect", "commit"]
    assert xray_writer_guard_is_held() is False
    with db_session() as connection:
        row = connection.execute("SELECT server_mode FROM routing_global_state WHERE id=1").fetchone()
    assert row["server_mode"] == "fixed"


@pytest.mark.parametrize("routing_state", ["clean", "pending", "failed", "different_mode"])
def test_core_pending_same_mode_reads_settled_fixed_intent_not_apply_projection(monkeypatch, routing_state):
    from fwrouter_api.db.connection import db_session
    from fwrouter_api.services import core_intent
    from fwrouter_api.services import apply_orchestrator_global_handlers as handlers
    from fwrouter_api.services.server_state import ensure_routing_global_state, get_routing_global_state
    from fwrouter_api.services.mihomo_config_status import _fixed_intent_is_clean_and_unexpired

    ensure_routing_global_state()
    with db_session() as connection:
        connection.execute("INSERT INTO servers(server_id, server_name, provider_name, inventory_state, raw_json) VALUES ('synthetic-fixed','Synthetic Fixed','pytest','active','{}')")
        connection.execute("UPDATE modules SET desired_state='enabled', lifecycle_mode='managed', apply_state='pending' WHERE module_name='core'")
        connection.execute("""UPDATE routing_global_state SET desired_mode='selective', applied_mode='selective',
                           server_mode='fixed', desired_fixed_server_id='synthetic-fixed',
                           applied_fixed_server_id='synthetic-fixed', fixed_server_until=datetime('now','+1 day'),
                           apply_state='clean', error_code=NULL, error_message=NULL WHERE id=1""")
        if routing_state == "different_mode":
            connection.execute("UPDATE routing_global_state SET applied_mode='vpn' WHERE id=1")
        elif routing_state != "clean":
            connection.execute("UPDATE routing_global_state SET apply_state=? WHERE id=1", (routing_state,))
    routing = get_routing_global_state(expire_ttl=False)
    monkeypatch.setattr(core_intent, 'read_core_runtime_context', lambda: {'boot_id': 'test', 'network_namespace_inode': '1'})
    monkeypatch.setattr(orchestrator, 'get_routing_snapshot', lambda: dict(routing))
    monkeypatch.setattr(orchestrator, '_current_routing_drift', lambda **kwargs: {'detected': False})
    monkeypatch.setattr(orchestrator, '_applied_manifest_routing_drift', lambda **kwargs: {'detected': False})
    monkeypatch.setattr(orchestrator, 'validate_global_mode_request', lambda *args, **kwargs: {'ok': True})
    monkeypatch.setattr(orchestrator, 'touch_job_running', lambda *args: None)

    def fresh_native_proof(snapshot):
        if routing_state == "clean":
            assert snapshot['apply_state'] == 'clean'
            assert _fixed_intent_is_clean_and_unexpired(snapshot)
        else:
            assert snapshot['apply_state'] == 'applying'
            assert not _fixed_intent_is_clean_and_unexpired(snapshot)
        raise RuntimeError('SETTLED_FIXED_PROOF_REACHED')

    monkeypatch.setattr(orchestrator, 'mihomo_runtime_satisfies_routing', fresh_native_proof)
    with pytest.raises(RuntimeError, match='SETTLED_FIXED_PROOF_REACHED'):
        handlers._execute_set_global_mode({'job_id': 'synthetic', 'requested_by': 'pytest'}, {'mode': 'selective'})
