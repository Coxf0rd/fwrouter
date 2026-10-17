import hashlib

from fwrouter_api.services import apply_orchestrator as orchestrator
from fwrouter_api.services import apply_orchestrator_server_handlers as server_handlers
from fwrouter_api.services import rules, rules_state
from fwrouter_api.services.apply_orchestrator import _commit_manual_rules_apply
from fwrouter_api.services.apply_orchestrator_rules_handlers import _execute_apply_manual_rules
from fwrouter_api.services.rules import validate_manual_rules


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
