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
        lambda action, **_kwargs: restarts.append(action) or {"ok": True, "action": action},
    )

    checkpoint = mihomo_reconcile._capture_mihomo_reconcile_checkpoint(str(base), str(candidate))
    checkpoint.update({
        "expected_selection_revision": 0,
        "operation_id": "test-operation",
        "runtime_incarnation_after": "container-a|started-a",
    })
    base.write_bytes(candidate.read_bytes())
    identities = iter(["container-a|started-a", "container-b|started-b"])
    monkeypatch.setattr(mihomo_reconcile, "_mihomo_incarnation", lambda: next(identities))
    result = mihomo_reconcile.restore_mihomo_reconcile_checkpoint(
        checkpoint, expected_revision=0, operation_id="test-operation",
    )

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

    checkpoint.update({"expected_selection_revision": 0, "operation_id": "test-operation", "runtime_incarnation_after": "container-a|started-a"})
    monkeypatch.setattr(mihomo_reconcile, "_mihomo_incarnation", lambda: "container-a|started-a")
    result = mihomo_reconcile.restore_mihomo_reconcile_checkpoint(
        checkpoint, expected_revision=0, operation_id="test-operation",
    )

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
    identities = iter(["container-a|started-a", "container-a|started-a", "container-b|started-b", "container-b|started-b", "container-c|started-c"])
    monkeypatch.setattr(mihomo_reconcile, "_mihomo_incarnation", lambda: next(identities))
    monkeypatch.setattr(mihomo_reconcile, "mihomo_input_unchanged", lambda _fingerprint: False)
    def write_candidate(*_args, **_kwargs):
        candidate.write_text("config: candidate\n", encoding="utf-8")
        return {"candidate_path": str(candidate), "rules_count": 0}
    monkeypatch.setattr(config, "write_mihomo_candidate_config", write_candidate)
    monkeypatch.setattr(config, "validate_mihomo_candidate_config", lambda *_args, **_kwargs: {"ok": True})
    monkeypatch.setattr(config, "_write_mihomo_reconcile_logs", lambda **_kwargs: None)
    monkeypatch.setattr(mihomo_reconcile, "promote_mihomo_candidate_config", lambda **_kwargs: (base.write_bytes(candidate.read_bytes()) and {"ok": True, "promoted": True}))
    monkeypatch.setattr(config, "restart_mihomo_container", lambda **_kwargs: {"ok": True, "action": "force_recreate"})
    restarts: list[str] = []
    monkeypatch.setattr(
        "fwrouter_api.services.mihomo_runtime.restart_mihomo_container",
        lambda action, **_kwargs: restarts.append(action) or {"ok": True, "action": action},
    )

    result = mihomo_reconcile.reconcile_mihomo_runtime(
        verification_callback=lambda: {"ok": False, "error_code": "READBACK_MISMATCH"},
    )

    assert result["ok"] is False
    assert result["last_good_retained"] is True
    assert result["verification_callback_result"]["error_code"] == "READBACK_MISMATCH"
    assert base.read_text(encoding="utf-8") == "config: last-good\n"
    assert restarts == ["force_recreate"]


def test_mihomo_reconcile_callback_cannot_publish_after_newer_generation_wins(monkeypatch, tmp_path: Path) -> None:
    """A successful selection callback is not final until guarded generation C."""
    config = mihomo_reconcile.config
    base = tmp_path / "active.yaml"
    candidate = tmp_path / "candidate.yaml"
    base.write_text("old", encoding="utf-8")
    monkeypatch.setattr(config, "_resolved_base_config_path", lambda: str(base))
    monkeypatch.setattr(config, "_resolved_candidate_config_path", lambda: str(candidate))
    monkeypatch.setattr(config, "_resolved_last_good_mihomo_dir", lambda: tmp_path / "last-good")
    monkeypatch.setattr(config, "managed_runtime_operation_blocked", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(mihomo_reconcile, "current_mihomo_input_fingerprint", lambda *_args: {"hash": "inputs-a"})
    monkeypatch.setattr(mihomo_reconcile, "mihomo_input_unchanged", lambda _fingerprint: False)
    revisions = [0]
    monkeypatch.setattr(mihomo_reconcile, "read_selection_revision", lambda _connection: revisions[0])
    monkeypatch.setattr(mihomo_reconcile, "advance_selection_revision", lambda _connection, expected_revision=None: (revisions.__setitem__(0, revisions[0] + 1) or revisions[0]) if revisions[0] == expected_revision else None)
    identities = iter(["runtime-a", "runtime-a", "runtime-b", "runtime-c"])
    latest_identity = ["runtime-a"]
    def get_identity():
        try:
            latest_identity[0] = next(identities)
        except StopIteration:
            pass
        return latest_identity[0]
    monkeypatch.setattr(mihomo_reconcile, "_mihomo_incarnation", get_identity)
    def write_candidate(*_args, **_kwargs):
        candidate.write_text("new", encoding="utf-8")
        return {"candidate_path": str(candidate), "rules_count": 0}
    monkeypatch.setattr(config, "write_mihomo_candidate_config", write_candidate)
    monkeypatch.setattr(config, "validate_mihomo_candidate_config", lambda *_args, **_kwargs: {"ok": True})
    monkeypatch.setattr(config, "_write_mihomo_reconcile_logs", lambda **_kwargs: None)
    monkeypatch.setattr(mihomo_reconcile, "promote_mihomo_candidate_config", lambda **_kwargs: (base.write_bytes(candidate.read_bytes()) and {"ok": True, "promoted": True}))
    monkeypatch.setattr(config, "restart_mihomo_container", lambda **_kwargs: {"ok": True, "action": "force_recreate"})
    fingerprints: list[dict[str, object]] = []
    monkeypatch.setattr(mihomo_reconcile, "write_mihomo_reconcile_fingerprint_state", lambda **kwargs: fingerprints.append(kwargs))

    def verify(*, operation_id: str, expected_selection_revision: int):
        # Callback commits its expected transition, then a competing runtime
        # generation wins before the outer reconcile can perform phase C.
        revisions[0] = expected_selection_revision + 1
        return {"ok": True, "operation_id": operation_id, "selection_revision": expected_selection_revision}

    result = mihomo_reconcile.reconcile_mihomo_runtime(verification_callback=verify)
    assert result["ok"] is False
    assert result["error_code"] == "MIHOMO_GENERATION_STALE_BEFORE_PUBLICATION"
    assert fingerprints == []


def test_mihomo_same_config_restart_changes_generation_identity(monkeypatch) -> None:
    from fwrouter_api.adapters.xray_common import xray_writer_guard

    monkeypatch.setattr(mihomo_reconcile, "current_mihomo_input_fingerprint", lambda _routing: {"hash": "same-input"})
    monkeypatch.setattr(mihomo_reconcile, "_mihomo_incarnation", lambda: "container-id|new-started-at")
    monkeypatch.setattr(mihomo_reconcile, "_file_hash", lambda _path: "same-config-digest")
    monkeypatch.setattr(mihomo_reconcile, "read_selection_revision", lambda _connection: 8)
    with xray_writer_guard(timeout_seconds=1):
        assert not mihomo_reconcile._selection_publication_still_owned(
            operation_id="op-old", expected_revision=8,
            expected_incarnation="container-id|old-started-at",
            expected_input_hash="same-input", expected_active_hash="same-config-digest",
            routing={}, verification=None,
        )


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
    monkeypatch.setattr(subscription_pipeline, "_maybe_select_vpn_auto_after_refresh", lambda **_kwargs: {"ok": False, "error_code": "READBACK_MISMATCH", "operation_id": "test-operation", "selection_revision": 0})
    snapshots = iter([{"state": "before"}, {"state": "after"}])
    monkeypatch.setattr("fwrouter_api.services.xray_subscription_service._capture_generation_auto_selection", lambda: next(snapshots))
    restored: list[bool] = []
    monkeypatch.setattr(
        "fwrouter_api.services.xray_subscription_service._restore_generation_auto_selection",
        lambda **_kwargs: restored.append(True) or True,
    )
    monkeypatch.setattr(
        "fwrouter_api.services.xray_subscription_service._verify_generation_selection_readback",
        lambda _selection: {"ok": False, "error_code": "SELECTION_RESTORE_READBACK_UNCONFIRMED"},
    )

    def reconcile(*, verification_callback=None, **_kwargs):
        verification = verification_callback(operation_id="test-operation", expected_selection_revision=0) if verification_callback else {"ok": True}
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


def test_staged_profile_verification_runs_outside_writer_guard(monkeypatch, tmp_path: Path) -> None:
    import hashlib
    import json
    from fwrouter_api.adapters.xray_common import xray_writer_guard_is_held
    from fwrouter_api.services import xray_subscription_service as profile_service

    active = tmp_path / "active.yaml"
    active.write_text("proxy-groups: []\n", encoding="utf-8")
    digest = hashlib.sha256(active.read_bytes()).hexdigest()
    checkpoint = tmp_path / "checkpoint.json"
    checkpoint.write_text(json.dumps({
        "generation_id": "op-1", "selection_revision": 4,
        "source_fingerprint": "pre-inventory-source", "mihomo_runtime_incarnation": "c1|t1",
        "derived_source_fingerprint": "post-inventory-source",
        "staged_generation": {"mihomo_final_sha256": digest},
    }), encoding="utf-8")
    callback_states: list[bool] = []
    def verify(**context):
        callback_states.append(xray_writer_guard_is_held())
        return {"ok": True, "operation_id": context["operation_id"], "selection_revision": 5}
    monkeypatch.setattr(profile_service, "_reconcile_xray_subscription_profile_nodes_guarded", lambda **kwargs: {
        "_pending_generation": {
            "verification_callback": verify, "checkpoint_path": str(checkpoint),
            "checkpoint_generation_id": "op-1", "checkpoint_selection_revision": 4,
            "checkpoint_runtime_incarnation": "c1|t1", "checkpoint_source_fingerprint": "post-inventory-source",
            "subscription_nodes": [], "affected_profile_tokens": [],
            "materialize": True, "promote_public_profile": True,
            "result_values": {},
        }
    })
    monkeypatch.setattr("fwrouter_api.services.mihomo_runtime.get_mihomo_runtime_incarnation", lambda: "c1|t1")
    monkeypatch.setattr("fwrouter_api.services.vpn_auto_selection_state.read_selection_fence", lambda _conn: {"revision": 5})
    monkeypatch.setattr(profile_service, "_generation_source_fingerprint", lambda: "post-inventory-source")
    monkeypatch.setattr("fwrouter_api.services.mihomo_config._resolved_base_config_path", lambda: active)
    monkeypatch.setattr(profile_service, "_mark_generation_selection_verification_required", lambda _path: None)
    monkeypatch.setattr(profile_service, "_record_xray_generation_derived_rows", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(profile_service, "_finalize_xray_profile_publication", lambda _pending, result: {"ok": True, "pre_publication_verification": result})

    result = profile_service.reconcile_xray_subscription_profile_nodes(verification_callback=verify)
    assert callback_states == [False]
    assert result["ok"] is True


def test_staged_profile_failed_terminal_verification_keeps_own_core_revision_for_rollback(monkeypatch, tmp_path: Path) -> None:
    import hashlib
    import json
    from fwrouter_api.services import xray_subscription_service as profile_service

    active = tmp_path / "active.yaml"
    active.write_text("candidate", encoding="utf-8")
    digest = hashlib.sha256(active.read_bytes()).hexdigest()
    checkpoint = tmp_path / "checkpoint.json"
    checkpoint.write_text(json.dumps({"generation_id": "op-2", "selection_revision": 7,
        "source_fingerprint": "source-2", "derived_source_fingerprint": "source-2",
        "mihomo_runtime_incarnation": "c2|t2",
        "staged_generation": {"mihomo_final_sha256": digest}}), encoding="utf-8")
    verify = lambda **_ctx: {"ok": False, "error_code": "PROVIDER_READBACK_FAILED",
                             "operation_id": "op-2", "selection_revision": 8}
    monkeypatch.setattr(profile_service, "_reconcile_xray_subscription_profile_nodes_guarded", lambda **_kwargs: {
        "_pending_generation": {"verification_callback": verify,
            "checkpoint_path": str(checkpoint), "checkpoint_generation_id": "op-2",
            "checkpoint_selection_revision": 7, "checkpoint_runtime_incarnation": "c2|t2",
            "checkpoint_source_fingerprint": "source-2", "subscription_nodes": [],
            "affected_profile_tokens": [], "materialize": True, "promote_public_profile": True,
            "result_values": {}}
    })
    monkeypatch.setattr("fwrouter_api.services.mihomo_runtime.get_mihomo_runtime_incarnation", lambda: "c2|t2")
    monkeypatch.setattr("fwrouter_api.services.vpn_auto_selection_state.read_selection_fence", lambda _conn: {"revision": 8})
    monkeypatch.setattr(profile_service, "_generation_source_fingerprint", lambda: "source-2")
    monkeypatch.setattr("fwrouter_api.services.mihomo_config._resolved_base_config_path", lambda: active)
    restored: list[bool] = []
    monkeypatch.setattr(profile_service, "_restore_xray_generation_checkpoint", lambda *_args: restored.append(True) or {"ok": True})

    result = profile_service.reconcile_xray_subscription_profile_nodes(
        verification_callback=verify,
    )
    assert result["stage"] == "selection_verification", result
    assert restored == [True]


def test_nonstaged_profile_selection_callback_runs_outside_writer_guard(monkeypatch, tmp_path: Path) -> None:
    import hashlib
    from fwrouter_api.adapters.xray_common import xray_writer_guard_is_held
    from fwrouter_api.services import xray_subscription_service as profile_service

    active = tmp_path / "active.yaml"
    active.write_text("stable", encoding="utf-8")
    callback_states: list[bool] = []
    def verify(**context):
        callback_states.append(xray_writer_guard_is_held())
        return {"ok": True, "operation_id": context["operation_id"], "selection_revision": 5}
    monkeypatch.setattr(profile_service, "_reconcile_xray_subscription_profile_nodes_guarded", lambda **_kwargs: {
        "_pending_generation": {
            "staged_generation": False, "verification_callback": verify,
            "operation_id": "nonstaged-op", "selection_revision": 4,
            "runtime_incarnation": "container|started", "mihomo_config_sha256": hashlib.sha256(b"stable").hexdigest(),
            "source_fingerprint": "post-inventory-source", "subscription_nodes": [],
            "affected_profile_tokens": [], "materialize": False, "promote_public_profile": False,
            "result_values": {"desired_nodes": [], "created": [], "deleted": [], "recreated": [],
                              "reconcile_details": {}, "materialize_result": None},
        }
    })
    monkeypatch.setattr("fwrouter_api.services.mihomo_runtime.get_mihomo_runtime_incarnation", lambda: "container|started")
    monkeypatch.setattr("fwrouter_api.services.vpn_auto_selection_state.read_selection_fence", lambda _conn: {"revision": 5})
    monkeypatch.setattr(profile_service, "_generation_source_fingerprint", lambda: "post-inventory-source")
    monkeypatch.setattr("fwrouter_api.services.mihomo_config._resolved_base_config_path", lambda: active)
    monkeypatch.setattr(profile_service, "_finalize_nonstaged_profile_publication", lambda _pending, verify: {"ok": True, "verification": verify})

    result = profile_service.reconcile_xray_subscription_profile_nodes(verification_callback=verify)
    assert callback_states == [False]
    assert result["ok"] is True


def test_nonstaged_profile_refuses_publication_after_competing_selection(monkeypatch, tmp_path: Path) -> None:
    import hashlib
    from fwrouter_api.adapters.xray_common import xray_writer_guard_is_held
    from fwrouter_api.services import xray_subscription_service as profile_service

    active = tmp_path / "active.yaml"
    active.write_text("stable", encoding="utf-8")
    state = {"revision": 5}
    published: list[bool] = []
    callback_guard: list[bool] = []
    def verify(**context):
        callback_guard.append(xray_writer_guard_is_held())
        state["revision"] = 6  # OP2 commits while OP1's bounded probe is outside the guard.
        return {"ok": True, "operation_id": context["operation_id"], "selection_revision": 5}
    monkeypatch.setattr(profile_service, "_reconcile_xray_subscription_profile_nodes_guarded", lambda **_kwargs: {
        "_pending_generation": {
            "staged_generation": False, "verification_callback": verify,
            "operation_id": "nonstaged-op", "selection_revision": 4,
            "runtime_incarnation": "container|started", "mihomo_config_sha256": hashlib.sha256(b"stable").hexdigest(),
            "source_fingerprint": "source-op1", "subscription_nodes": [],
            "affected_profile_tokens": [], "materialize": True, "promote_public_profile": True,
            "result_values": {"desired_nodes": [], "created": [], "deleted": [], "recreated": [],
                              "reconcile_details": {}, "materialize_result": None},
        }
    })
    monkeypatch.setattr("fwrouter_api.services.mihomo_runtime.get_mihomo_runtime_incarnation", lambda: "container|started")
    monkeypatch.setattr("fwrouter_api.services.vpn_auto_selection_state.read_selection_fence", lambda _conn: {"revision": state["revision"]})
    monkeypatch.setattr(profile_service, "_generation_source_fingerprint", lambda: "source-op1")
    monkeypatch.setattr("fwrouter_api.services.mihomo_config._resolved_base_config_path", lambda: active)
    monkeypatch.setattr(profile_service, "_finalize_nonstaged_profile_publication", lambda *_args: published.append(True) or {"ok": True})

    result = profile_service.reconcile_xray_subscription_profile_nodes(verification_callback=verify)
    assert callback_guard == [False]
    assert result["status"] == "partial"
    assert result["error_code"] == "XRAY_GENERATION_STALE_BEFORE_PUBLICATION"
    assert published == []


def test_staged_profile_rejects_checkpoint_replaced_by_newer_operation(monkeypatch, tmp_path: Path) -> None:
    import hashlib
    import json
    from fwrouter_api.services import xray_subscription_service as profile_service

    active = tmp_path / "active.yaml"
    active.write_text("candidate", encoding="utf-8")
    digest = hashlib.sha256(active.read_bytes()).hexdigest()
    checkpoint = tmp_path / "checkpoint.json"
    checkpoint.write_text(json.dumps({"generation_id": "op-1", "selection_revision": 4,
        "mihomo_runtime_incarnation": "c1|t1", "source_fingerprint": "source-old",
        "derived_source_fingerprint": "derived-1", "staged_generation": {"mihomo_final_sha256": digest}}), encoding="utf-8")
    def verify(**context):
        checkpoint.write_text(json.dumps({"generation_id": "op-2", "selection_revision": 6,
            "mihomo_runtime_incarnation": "c2|t2", "source_fingerprint": "source-new",
            "derived_source_fingerprint": "derived-2", "staged_generation": {"mihomo_final_sha256": digest}}), encoding="utf-8")
        return {"ok": True, "operation_id": context["operation_id"], "selection_revision": 5}
    monkeypatch.setattr(profile_service, "_reconcile_xray_subscription_profile_nodes_guarded", lambda **_kwargs: {
        "_pending_generation": {"verification_callback": verify, "checkpoint_path": str(checkpoint),
            "checkpoint_generation_id": "op-1", "checkpoint_selection_revision": 4,
            "checkpoint_runtime_incarnation": "c1|t1", "checkpoint_source_fingerprint": "derived-1",
            "subscription_nodes": [], "affected_profile_tokens": [], "materialize": True,
            "promote_public_profile": True, "result_values": {}}
    })
    monkeypatch.setattr("fwrouter_api.services.mihomo_runtime.get_mihomo_runtime_incarnation", lambda: "c1|t1")
    monkeypatch.setattr("fwrouter_api.services.vpn_auto_selection_state.read_selection_fence", lambda _conn: {"revision": 5})
    monkeypatch.setattr(profile_service, "_generation_source_fingerprint", lambda: "derived-1")
    monkeypatch.setattr("fwrouter_api.services.mihomo_config._resolved_base_config_path", lambda: active)
    published: list[bool] = []
    monkeypatch.setattr(profile_service, "_finalize_xray_profile_publication", lambda *_args: published.append(True) or {"ok": True})
    monkeypatch.setattr(profile_service, "_restore_xray_generation_checkpoint", lambda *_args: published.append(True) or {"ok": True})

    result = profile_service.reconcile_xray_subscription_profile_nodes(verification_callback=verify)
    assert result["error_code"] == "XRAY_GENERATION_STALE_BEFORE_PUBLICATION"
    assert published == []
