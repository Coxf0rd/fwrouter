from fwrouter_api.services.apply_orchestrator import _commit_manual_rules_apply


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
    }
