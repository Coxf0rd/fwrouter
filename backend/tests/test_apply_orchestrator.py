import hashlib

from fwrouter_api.services import apply_orchestrator as orchestrator
from fwrouter_api.services.apply_orchestrator import _commit_manual_rules_apply
from fwrouter_api.services.apply_orchestrator_rules_handlers import _execute_apply_manual_rules
from fwrouter_api.services.rules import validate_manual_rules


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
