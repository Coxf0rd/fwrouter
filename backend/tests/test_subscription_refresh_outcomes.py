from __future__ import annotations

from pathlib import Path

from fwrouter_api.services import mihomo_config as _mihomo_config  # initialize reconcile exports before test imports
from fwrouter_api.services import mihomo_reconcile, subscription_pipeline, subscription_refresh_job
from fwrouter_api.services.events import CORE_EVENT_CODE_CATALOG
from fwrouter_api.services.ui_state_logs import UI_OPERATIONAL_EVENT_MESSAGES
from fwrouter_api.services.ui_text import _ui_text_title


def test_mihomo_generation_checkpoint_restores_exact_previous_file_and_runtime(monkeypatch, tmp_path: Path) -> None:
    base = tmp_path / "active.yaml"
    candidate = tmp_path / "candidate.yaml"
    last_good = tmp_path / "last-good"
    base.write_text("proxy-groups: [last-good]\n", encoding="utf-8")
    candidate.write_text("proxy-groups: [candidate]\n", encoding="utf-8")
    monkeypatch.setattr(mihomo_reconcile.config, "_resolved_last_good_mihomo_dir", lambda: last_good)
    restarts: list[str] = []
    monkeypatch.setattr(
        "fwrouter_api.services.mihomo_runtime.restart_mihomo_container",
        lambda action: restarts.append(action) or {"ok": True, "action": action},
    )

    checkpoint = mihomo_reconcile._capture_mihomo_reconcile_checkpoint(str(base), str(candidate))
    base.write_bytes(candidate.read_bytes())
    result = mihomo_reconcile.restore_mihomo_reconcile_checkpoint(checkpoint)

    assert result["ok"] is True
    assert base.read_text(encoding="utf-8") == "proxy-groups: [last-good]\n"
    assert restarts == ["force_recreate"]


def test_mihomo_generation_restore_refuses_external_active_config_change(monkeypatch, tmp_path: Path) -> None:
    base = tmp_path / "active.yaml"
    candidate = tmp_path / "candidate.yaml"
    monkeypatch.setattr(mihomo_reconcile.config, "_resolved_last_good_mihomo_dir", lambda: tmp_path / "last-good")
    base.write_text("old", encoding="utf-8")
    candidate.write_text("candidate", encoding="utf-8")
    checkpoint = mihomo_reconcile._capture_mihomo_reconcile_checkpoint(str(base), str(candidate))
    base.write_text("external-change", encoding="utf-8")

    result = mihomo_reconcile.restore_mihomo_reconcile_checkpoint(checkpoint)

    assert result["ok"] is False
    assert base.read_text(encoding="utf-8") == "external-change"


def test_invalid_source_job_does_not_claim_saved_intent_or_last_good(monkeypatch) -> None:
    result = subscription_refresh_job._subscription_refresh_failure(
        job_id="invalid-ref",
        stage="validate",
        result={"error": {"code": "SUBSCRIPTION_SOURCE_REF_INVALID", "message": "Invalid source reference."}},
    )
    assert result["outcome"] == "failed"
    assert result["intent_saved"] is False
    assert result["last_good_retained"] is False


def test_terminal_partial_event_is_warning_with_typed_source_errors(monkeypatch) -> None:
    events = []
    monkeypatch.setattr(subscription_pipeline, "write_operational_log", lambda **kwargs: events.append(kwargs))
    subscription_pipeline._write_subscription_terminal_event({
        "ok": True,
        "outcome": "partial",
        "runtime_verified": True,
        "intent_saved": True,
        "last_good_retained": False,
        "source_outcomes": [{"source_ref": "src:" + "a" * 64, "outcome": "failed", "error_code": "FETCH", "error_message": "provider unavailable", "retained": False}],
    })
    assert len(events) == 1
    assert events[0]["event_type"] == "subscription_refresh_partial"
    assert events[0]["level"] == "warning"
    assert events[0]["details"]["runtime_verified"] is True
    assert events[0]["details"]["source_outcomes"][0]["error_code"] == "FETCH"


def test_subscription_terminal_events_are_registered_and_localized() -> None:
    for event_type in (
        "subscription_refresh_partial",
        "subscription_refresh_failed",
        "subscription_refresh_unconfirmed",
        "server_selection_noop",
    ):
        assert CORE_EVENT_CODE_CATALOG[event_type] == "operational"
        assert UI_OPERATIONAL_EVENT_MESSAGES[event_type]["ru"]
        assert UI_OPERATIONAL_EVENT_MESSAGES[event_type]["en"]
        assert _ui_text_title("log.event", event_type, locale="ru")
        assert _ui_text_title("log.event", event_type, locale="en")


def test_mihomo_reconcile_rolls_back_when_postapply_selection_readback_fails(monkeypatch, tmp_path: Path) -> None:
    config = mihomo_reconcile.config
    base = tmp_path / "active.yaml"
    candidate = tmp_path / "candidate.yaml"
    last_good = tmp_path / "last-good"
    base.write_text("config: last-good\n", encoding="utf-8")
    monkeypatch.setattr(config, "_resolved_base_config_path", lambda: str(base))
    monkeypatch.setattr(config, "_resolved_candidate_config_path", lambda: str(candidate))
    monkeypatch.setattr(config, "_resolved_last_good_mihomo_dir", lambda: last_good)
    monkeypatch.setattr(config, "managed_runtime_operation_blocked", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(mihomo_reconcile, "current_mihomo_input_fingerprint", lambda *_args: {"hash": "new"})
    monkeypatch.setattr(mihomo_reconcile, "mihomo_input_unchanged", lambda _fingerprint: False)
    def write_candidate(*_args, **_kwargs):
        candidate.write_text("config: candidate\n", encoding="utf-8")
        return {"candidate_path": str(candidate), "rules_count": 0}
    monkeypatch.setattr(config, "write_mihomo_candidate_config", write_candidate)
    monkeypatch.setattr(config, "validate_mihomo_candidate_config", lambda *_args, **_kwargs: {"ok": True})
    monkeypatch.setattr(config, "_write_mihomo_reconcile_logs", lambda **_kwargs: None)
    monkeypatch.setattr(mihomo_reconcile, "promote_mihomo_candidate_config", lambda: (base.write_bytes(candidate.read_bytes()) and {"ok": True, "promoted": True}))
    monkeypatch.setattr(config, "restart_mihomo_container", lambda **_kwargs: {"ok": True, "action": "force_recreate"})
    restarts: list[str] = []
    monkeypatch.setattr(
        "fwrouter_api.services.mihomo_runtime.restart_mihomo_container",
        lambda action: restarts.append(action) or {"ok": True, "action": action},
    )

    result = mihomo_reconcile.reconcile_mihomo_runtime(
        verification_callback=lambda: {"ok": False, "error_code": "READBACK_MISMATCH"},
    )

    assert result["ok"] is False
    assert result["last_good_retained"] is True
    assert result["verification_callback_result"]["error_code"] == "READBACK_MISMATCH"
    assert base.read_text(encoding="utf-8") == "config: last-good\n"
    assert restarts == ["force_recreate"]


def test_job_projects_partial_noop_and_all_failed_outcomes_without_overclaiming(monkeypatch) -> None:
    monkeypatch.setattr(subscription_refresh_job, "update_job_running_result", lambda *_args, **_kwargs: None)
    source = {"source_ref": "src:" + "b" * 64, "display_label": "https://provider.example/…", "outcome": "failed", "error_code": "FETCH", "error_message": "offline", "retained": False}

    monkeypatch.setattr(subscription_refresh_job, "apply_subscription_refresh", lambda: {
        "ok": True, "stage": "verify", "outcome": "partial", "runtime_verified": True,
        "intent_saved": True, "last_good_retained": False, "source_outcomes": [source],
    })
    partial = subscription_refresh_job.run_subscription_refresh_job({"job_id": "partial", "input": {}})
    assert partial["job_status"] == "failed"
    assert partial["outcome"] == "partial"
    assert partial["runtime_verified"] is True
    assert partial["last_good_retained"] is False
    assert partial["source_outcomes"][0]["error_code"] == "FETCH"

    monkeypatch.setattr(subscription_refresh_job, "apply_subscription_refresh", lambda: {
        "ok": True, "stage": "already_current", "outcome": "no_op", "runtime_verified": True,
        "intent_saved": True, "last_good_retained": False, "applied": False, "source_outcomes": [],
    })
    noop = subscription_refresh_job.run_subscription_refresh_job({"job_id": "noop", "input": {}})
    assert noop["job_status"] == "success"
    assert noop["outcome"] == "no_op"
    assert noop["runtime_verified"] is True
    assert noop["applied"] is False

    monkeypatch.setattr(subscription_refresh_job, "apply_subscription_refresh", lambda: {
        "ok": False, "stage": "download_parse", "outcome": "failed", "runtime_verified": False,
        "intent_saved": True, "last_good_retained": False, "source_outcomes": [source],
        "error": {"code": "FETCH", "message": "offline"},
    })
    failed = subscription_refresh_job.run_subscription_refresh_job({"job_id": "failed", "input": {}})
    assert failed["job_status"] == "failed"
    assert failed["outcome"] == "failed"
    assert failed["intent_saved"] is True
    assert failed["last_good_retained"] is False


def test_failed_readback_uses_confirmed_reconcile_restore_once(monkeypatch, tmp_path: Path) -> None:
    from fwrouter_api.core.config import get_settings
    from fwrouter_api.db.connection import initialize_database
    from fwrouter_api.services import xray_runtime_state

    monkeypatch.setenv("FWROUTER_STATE_DIR", str(tmp_path / "state"))
    get_settings.cache_clear()
    initialize_database()
    monkeypatch.setattr(xray_runtime_state, "_module_state", lambda _name: {"desired_state": "enabled", "lifecycle_mode": "external"})
    monkeypatch.setattr(subscription_pipeline, "_subscription_transition_preflight", lambda: {"ok": True})
    monkeypatch.setattr(subscription_pipeline, "_maybe_select_vpn_auto_after_refresh", lambda: {"ok": False, "error_code": "READBACK_MISMATCH"})
    snapshots = iter([{"state": "before"}, {"state": "after"}])
    monkeypatch.setattr("fwrouter_api.services.xray_subscription_service._capture_generation_auto_selection", lambda: next(snapshots))
    restored: list[bool] = []
    monkeypatch.setattr(
        "fwrouter_api.services.xray_subscription_service._restore_generation_auto_selection",
        lambda _connection, **_kwargs: restored.append(True) or True,
    )
    monkeypatch.setattr(
        "fwrouter_api.services.xray_subscription_service._verify_generation_selection_readback",
        lambda _selection: {"ok": False, "error_code": "SELECTION_RESTORE_READBACK_UNCONFIRMED"},
    )

    def reconcile(*, verification_callback=None, **_kwargs):
        verification = verification_callback() if verification_callback else {"ok": True}
        return {
            "ok": False,
            "stage": "verification",
            "verification_callback_result": verification,
            "generation_recovery": {"ok": True, "recovered": True},
            "last_good_retained": True,
            "promoted": {"ok": True, "promoted": True},
            "container": {"ok": True, "action": "restart"},
        }
    monkeypatch.setattr(subscription_pipeline, "reconcile_mihomo_runtime", reconcile)
    monkeypatch.setattr(subscription_pipeline, "write_operational_log", lambda **_kwargs: None)
    monkeypatch.setattr(subscription_pipeline, "write_technical_log", lambda **_kwargs: None)

    result = subscription_pipeline.apply_prepared_subscription_refresh({
        "ok": True,
        "stage": "candidate_validated",
        "refresh": {"ok": True, "state": {"metadata": {"subscriptions": {"items": []}}}, "batch": {"items": [], "errors": 0}},
        "candidate": {},
    })

    assert result["outcome"] == "unconfirmed"
    assert result["last_good_retained"] is False
    assert result["runtime_verified"] is False
    assert restored == [True]
