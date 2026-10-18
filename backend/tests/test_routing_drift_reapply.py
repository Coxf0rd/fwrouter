from __future__ import annotations
from _test_support import configure_test_state_dir_and_clear_live_probe_cache as _configure_env

import hashlib
import json
import threading
from pathlib import Path

from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import db_session, initialize_database
from fwrouter_api.services import apply_orchestrator as orchestrator
from fwrouter_api.services.modules import get_module_state, set_module_desired_state
from fwrouter_api.services.servers import ensure_routing_global_state




def _seed_selective_routing() -> None:
    ensure_routing_global_state()
    with db_session() as connection:
        connection.execute(
            """
            UPDATE routing_global_state
            SET
                desired_mode = 'selective',
                applied_mode = 'selective',
                selective_default = 'direct',
                apply_state = 'clean',
                error_code = NULL,
                error_message = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = 1
            """
        )


def _mock_owned_apply(kwargs: dict[str, object], apply_id: str) -> dict[str, object]:
    """Persist the receipt that a successful native pipeline would own."""
    revision = kwargs.get("core_intent_revision")
    with db_session() as connection:
        connection.execute(
            "INSERT INTO apply_versions(apply_id, manifest_path, status, core_intent_revision) "
            "VALUES (?, ?, 'applied', ?)",
            (apply_id, f"/synthetic/{apply_id}.json", revision),
        )
    return {
        "ok": True,
        "apply_id": apply_id,
        "dataplane": {"message": "ok", "error_code": None, "error_message": None},
    }


def _proof_manifest(apply_id: str) -> dict[str, object]:
    with db_session() as connection:
        row = connection.execute(
            "SELECT desired_mode, applied_mode, selective_default, server_mode, "
            "desired_fixed_server_id, applied_fixed_server_id, fixed_server_until, "
            "active_auto_server_id FROM routing_global_state WHERE id=1",
        ).fetchone()
    return {"plan_id": apply_id, "routing_global_state": dict(row)}


def _proof_manifest_hash(manifest: dict[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8"),
    ).hexdigest()


def test_same_global_mode_runs_apply_when_core_enable_is_pending(
    monkeypatch, tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    ensure_routing_global_state()
    set_module_desired_state("core", "disabled", requested_by="pytest", run_now=False)
    set_module_desired_state("core", "enabled", requested_by="pytest", run_now=False)
    with db_session() as connection:
        connection.execute(
            "UPDATE routing_global_state SET desired_mode='direct', applied_mode='direct', "
            "apply_state='clean' WHERE id=1"
        )
    monkeypatch.setattr(
        orchestrator,
        "get_routing_snapshot",
        lambda: {
            "desired_mode": "direct", "applied_mode": "direct", "apply_state": "clean",
            "server_mode": "fixed", "desired_fixed_server_id": None,
            "applied_fixed_server_id": None,
        },
    )

    pipeline_calls: list[dict[str, object]] = []
    from fwrouter_api.adapters.xray_common import xray_writer_guard_is_held

    monkeypatch.setattr(orchestrator, "_current_routing_drift", lambda **_kwargs: {"detected": False})
    monkeypatch.setattr(orchestrator, "_applied_manifest_routing_drift", lambda **_kwargs: {"detected": False})
    def _successful_owned_apply(**kwargs):
        assert xray_writer_guard_is_held()
        pipeline_calls.append(kwargs)
        with db_session() as connection:
            connection.execute(
                "INSERT INTO apply_versions(apply_id, manifest_path, status, core_intent_revision) "
                "VALUES('fresh-core-enable-apply', '/synthetic/fresh.json', 'applied', ?)",
                (kwargs["core_intent_revision"],),
            )
        return {
            "ok": True,
            "apply_id": "fresh-core-enable-apply",
            "dataplane": {"message": "ok", "error_code": None, "error_message": None},
        }
    monkeypatch.setattr(orchestrator, "_run_pipeline_for_state", _successful_owned_apply)
    monkeypatch.setattr(
        "fwrouter_api.services.xray_subscription_service._collect_legacy_admission_core_native_proof",
        lambda: {
            "ok": True, "apply_id": "fresh-core-enable-apply",
            "manifest_hash": "synthetic-proof-hash",
            "traffic_enforcement_guaranteed": True,
            "active_mode_matches_intent": True,
            "enforcement_level": "global_direct_enforced",
            "live_global_mode": "direct",
            "runtime_context": {"boot_id": "00000000-0000-0000-0000-000000000001", "network_namespace_inode": "123"},
        },
    )
    monkeypatch.setattr(
        "fwrouter_api.services.core_intent.read_core_runtime_context",
        lambda: {"boot_id": "00000000-0000-0000-0000-000000000001", "network_namespace_inode": "123"},
    )
    monkeypatch.setattr(orchestrator, "validate_global_mode_request", lambda *_args, **_kwargs: {"ok": True})
    monkeypatch.setattr(orchestrator, "touch_job_running", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(orchestrator, "load_precompiled_global_mode_profile", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(orchestrator, "_load_user_override_map", lambda: {})
    monkeypatch.setattr(orchestrator, "_load_server_override_map", lambda: {})
    monkeypatch.setattr(orchestrator, "_load_subjects_with_overrides", lambda **_kwargs: [])
    monkeypatch.setattr(orchestrator, "_sync_subject_server_override_statuses", lambda *_args: None)
    proof_manifest = _proof_manifest("fresh-core-enable-apply")
    monkeypatch.setattr(
        "fwrouter_api.services.dataplane_global._read_applied_manifest_uncached",
        lambda: proof_manifest,
    )
    setter_started = threading.Event()
    setter_finished = threading.Event()
    setter_errors: list[BaseException] = []
    setter_threads: list[threading.Thread] = []

    def _proof_during_owned_global_operation():
        assert xray_writer_guard_is_held(), "global operation released shared writer exclusion before proof"

        def change_intent() -> None:
            setter_started.set()
            try:
                set_module_desired_state("core", "disabled", requested_by="concurrent-test", run_now=False)
            except BaseException as exc:  # surfaced in the owning test thread
                setter_errors.append(exc)
            finally:
                setter_finished.set()

        writer = threading.Thread(target=change_intent)
        setter_threads.append(writer)
        writer.start()
        assert setter_started.wait(timeout=2)
        assert not setter_finished.wait(timeout=0.1), "Core intent writer crossed global native/proof window"
        return {
            "ok": True, "apply_id": "fresh-core-enable-apply",
            "manifest_hash": _proof_manifest_hash(proof_manifest),
            "traffic_enforcement_guaranteed": True, "active_mode_matches_intent": True,
            "enforcement_level": "global_direct_enforced", "live_global_mode": "direct",
            "runtime_context": {"boot_id": "00000000-0000-0000-0000-000000000001", "network_namespace_inode": "123"},
        }

    monkeypatch.setattr(
        "fwrouter_api.services.xray_subscription_service._collect_legacy_admission_core_native_proof",
        _proof_during_owned_global_operation,
    )

    result = orchestrator._execute_set_global_mode(
        {"job_id": "job-core-enable", "requested_by": "pytest"}, {"mode": "direct"},
    )
    # The writer resumes only after the global handler has settled the proof.
    assert setter_finished.wait(timeout=3)
    for writer in setter_threads:
        writer.join(timeout=2)
        assert not writer.is_alive()
    assert not setter_errors, setter_errors

    assert result["ok"] is True
    assert len(pipeline_calls) == 1
    assert pipeline_calls[0]["core_intent_revision"] is not None
    assert get_module_state("core")["desired_state"] == "disabled"


def test_core_settlement_rejects_changed_runtime_context(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    ensure_routing_global_state()
    set_module_desired_state("core", "enabled", requested_by="pytest", run_now=False)
    with db_session() as connection:
        connection.execute(
            "UPDATE routing_global_state SET desired_mode='direct', applied_mode='direct', "
            "apply_state='clean' WHERE id=1"
        )
        connection.execute(
            "INSERT INTO apply_versions(apply_id, manifest_path, status, core_intent_revision) "
            "SELECT 'runtime-context-generation', '/synthetic/context.json', 'applied', "
            "CAST(json_extract(value_json, '$') AS INTEGER) FROM settings WHERE key='core.intent_revision'"
        )
        from fwrouter_api.services.core_intent import read_core_intent_revision
        revision = read_core_intent_revision(connection)
    expected_context = {"boot_id": "00000000-0000-0000-0000-000000000001", "network_namespace_inode": "123"}
    changed_context = {**expected_context, "network_namespace_inode": "456"}
    monkeypatch.setattr(
        "fwrouter_api.services.core_intent.read_core_runtime_context", lambda: changed_context,
    )
    from fwrouter_api.services.core_intent import settle_core_enable_after_proof
    proof = {
        "ok": True, "apply_id": "runtime-context-generation", "manifest_hash": "synthetic",
        "traffic_enforcement_guaranteed": True, "active_mode_matches_intent": True,
        "enforcement_level": "global_direct_enforced", "live_global_mode": "direct",
        "runtime_context": expected_context,
    }
    assert not settle_core_enable_after_proof(
        expected_revision=revision, apply_id="runtime-context-generation", proof=proof,
        expected_runtime_context=expected_context,
    )
    assert get_module_state("core")["apply_state"] == "pending"


def test_core_settlement_holds_write_fence_across_check_and_update(monkeypatch, tmp_path: Path) -> None:
    import sqlite3

    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    ensure_routing_global_state()
    set_module_desired_state("core", "enabled", requested_by="pytest", run_now=False)
    expected_context = {"boot_id": "00000000-0000-0000-0000-000000000001", "network_namespace_inode": "123"}
    monkeypatch.setattr(
        "fwrouter_api.services.core_intent.read_core_runtime_context", lambda: expected_context,
    )
    with db_session() as connection:
        connection.execute(
            "UPDATE routing_global_state SET desired_mode='direct', applied_mode='direct', "
            "apply_state='clean' WHERE id=1"
        )
        connection.execute(
            "INSERT INTO apply_versions(apply_id, manifest_path, status, created_at, core_intent_revision) "
            "SELECT 'settlement-lock-generation', '/synthetic/lock.json', 'applied', "
            "datetime('now', '+1 day'), "
            "CAST(value_json AS INTEGER) FROM settings WHERE key='core.intent_revision'"
        )
        from fwrouter_api.services.core_intent import read_core_intent_revision
        revision = read_core_intent_revision(connection)
        latest_receipt = connection.execute(
            "SELECT apply_id, status, core_intent_revision FROM apply_versions "
            "ORDER BY created_at DESC, apply_id DESC LIMIT 1",
        ).fetchone()
        assert tuple(latest_receipt) == ("settlement-lock-generation", "applied", revision)
        routing_receipt = connection.execute(
            "SELECT desired_mode, applied_mode, apply_state FROM routing_global_state WHERE id=1",
        ).fetchone()
        assert tuple(routing_receipt) == ("direct", "direct", "clean")
        assert connection.execute(
            "SELECT apply_id FROM apply_versions ORDER BY created_at DESC, apply_id DESC LIMIT 1",
        ).fetchone()["apply_id"] == "settlement-lock-generation"
        from fwrouter_api.services.core_intent import assert_core_intent_current
        assert assert_core_intent_current(connection, revision)

    from fwrouter_api.services import core_intent
    original_assert = core_intent.assert_core_intent_current
    competing_writer_blocked: list[bool] = []
    proof_manifest = _proof_manifest("settlement-lock-generation")
    monkeypatch.setattr(
        "fwrouter_api.services.dataplane_global._read_applied_manifest_uncached",
        lambda: proof_manifest,
    )

    def assert_with_competing_writer(connection, expected_revision, *, require_pending=True):
        contender = sqlite3.connect(get_settings().paths.db_path, timeout=0)
        try:
            contender.execute("PRAGMA busy_timeout=0")
            try:
                contender.execute(
                    "UPDATE settings SET value_json='999' WHERE key='core.intent_revision'",
                )
                contender.commit()
                competing_writer_blocked.append(False)
            except sqlite3.OperationalError as exc:
                assert "locked" in str(exc).lower()
                competing_writer_blocked.append(True)
        finally:
            contender.close()
        return original_assert(connection, expected_revision, require_pending=require_pending)

    monkeypatch.setattr(core_intent, "assert_core_intent_current", assert_with_competing_writer)
    proof = {
        "ok": True, "apply_id": "settlement-lock-generation",
        "manifest_hash": _proof_manifest_hash(proof_manifest),
        "traffic_enforcement_guaranteed": True, "active_mode_matches_intent": True,
        "enforcement_level": "global_direct_enforced", "live_global_mode": "direct",
        "runtime_context": expected_context,
    }
    assert core_intent.settle_core_enable_after_proof(
        expected_revision=revision, apply_id="settlement-lock-generation", proof=proof,
        expected_runtime_context=expected_context,
    )
    assert competing_writer_blocked == [True]
    assert get_module_state("core")["apply_state"] == "clean"


def test_core_enable_apply_rejects_disable_reenable_aba_before_route_commit(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    set_module_desired_state("core", "disabled", requested_by="pytest", run_now=False)
    set_module_desired_state("core", "enabled", requested_by="pytest", run_now=False)
    with db_session() as connection:
        connection.execute(
            "UPDATE routing_global_state SET desired_mode='direct', applied_mode='direct', "
            "apply_state='clean' WHERE id=1"
        )
    monkeypatch.setattr(orchestrator, "get_routing_snapshot", lambda: {
        "desired_mode": "direct", "applied_mode": "direct", "apply_state": "clean",
        "server_mode": "fixed", "desired_fixed_server_id": None, "applied_fixed_server_id": None,
    })
    monkeypatch.setattr(orchestrator, "_current_routing_drift", lambda **_kwargs: {"detected": False})
    monkeypatch.setattr(orchestrator, "_applied_manifest_routing_drift", lambda **_kwargs: {"detected": False})
    monkeypatch.setattr(orchestrator, "validate_global_mode_request", lambda *_args, **_kwargs: {"ok": True})
    monkeypatch.setattr(orchestrator, "touch_job_running", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(orchestrator, "load_precompiled_global_mode_profile", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(orchestrator, "_load_user_override_map", lambda: {})
    monkeypatch.setattr(orchestrator, "_load_server_override_map", lambda: {})
    monkeypatch.setattr(orchestrator, "_load_subjects_with_overrides", lambda **_kwargs: [])
    route_commits: list[dict[str, object]] = []
    monkeypatch.setattr(orchestrator, "_commit_global_mode", lambda **kwargs: route_commits.append(kwargs))

    def stale_apply(**kwargs):
        with db_session() as connection:
            connection.execute(
                "INSERT INTO apply_versions(apply_id, manifest_path, status, core_intent_revision) "
                "VALUES('stale-core-generation', '/synthetic/stale.json', 'applied', ?)",
                (kwargs["core_intent_revision"],),
            )
        set_module_desired_state("core", "disabled", requested_by="race", run_now=False)
        set_module_desired_state("core", "enabled", requested_by="race", run_now=False)
        return {
            "ok": True, "apply_id": "stale-core-generation",
            "dataplane": {"message": "ok", "error_code": None, "error_message": None},
        }
    monkeypatch.setattr(orchestrator, "_run_pipeline_for_state", stale_apply)

    result = orchestrator._execute_set_global_mode(
        {"job_id": "job-core-aba", "requested_by": "pytest"}, {"mode": "direct"},
    )
    assert result["ok"] is False
    assert result["code"] == "CORE_INTENT_STALE"
    assert route_commits == []
    assert get_module_state("core")["apply_state"] == "pending"


def test_set_global_mode_reapplies_when_live_dataplane_drift_is_detected(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _seed_selective_routing()

    pipeline_calls: list[dict[str, object]] = []
    operational_events: list[dict[str, object]] = []
    technical_events: list[dict[str, object]] = []

    monkeypatch.setattr(
        orchestrator,
        "probe_live_global_mode",
        lambda: {"ok": True, "mode": "direct", "selective_default": "direct"},
    )
    monkeypatch.setattr(
        orchestrator,
        "validate_global_mode_request",
        lambda mode, routing=None: {"ok": True, "mode": mode, "preflight": {}},
    )
    monkeypatch.setattr(orchestrator, "reconcile_mihomo_runtime", lambda **kwargs: {"ok": True})
    monkeypatch.setattr(orchestrator, "_load_user_override_map", lambda: {})
    monkeypatch.setattr(orchestrator, "_load_server_override_map", lambda: {})
    monkeypatch.setattr(orchestrator, "_load_subjects_with_overrides", lambda **kwargs: [])
    monkeypatch.setattr(orchestrator, "_sync_subject_server_override_statuses", lambda subjects: None)
    monkeypatch.setattr(
        orchestrator,
        "_run_pipeline_for_state",
        lambda **kwargs: pipeline_calls.append(kwargs) or _mock_owned_apply(kwargs, "apply-1"),
    )
    monkeypatch.setattr(
        orchestrator,
        "write_operational_log",
        lambda **kwargs: operational_events.append(kwargs) or kwargs,
    )
    monkeypatch.setattr(
        orchestrator,
        "write_technical_log",
        lambda **kwargs: technical_events.append(kwargs) or kwargs,
    )

    result = orchestrator._execute_set_global_mode(
        {"job_id": "job-1", "requested_by": "pytest"},
        {"mode": "selective"},
    )

    assert result["ok"] is True
    assert result["stage"] == "commit"
    assert len(pipeline_calls) == 1
    assert operational_events[-1]["event_type"] == "routing_live_drift_detected"
    assert technical_events[-1]["event_type"] == "routing_live_drift_detected"


def test_set_global_mode_reapplies_when_live_applied_nft_markers_are_stale(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _seed_selective_routing()

    pipeline_calls: list[dict[str, object]] = []
    operational_events: list[dict[str, object]] = []

    monkeypatch.setattr(
        orchestrator,
        "probe_live_global_mode",
        lambda: {"ok": True, "mode": "selective", "selective_default": "direct"},
    )
    monkeypatch.setattr(
        orchestrator,
        "_live_applied_nft_artifact_consistency",
        lambda: {
            "detected": True,
            "checked": True,
            "ok": False,
            "reason": "live_table_missing_applied_markers",
            "missing_markers": ["scoped selective vpn IPv4: lan:1"],
            "missing_markers_count": 1,
        },
    )
    monkeypatch.setattr(
        orchestrator,
        "validate_global_mode_request",
        lambda mode, routing=None: {"ok": True, "mode": mode, "preflight": {}},
    )
    monkeypatch.setattr(orchestrator, "reconcile_mihomo_runtime", lambda **kwargs: {"ok": True})
    monkeypatch.setattr(orchestrator, "_load_user_override_map", lambda: {})
    monkeypatch.setattr(orchestrator, "_load_server_override_map", lambda: {})
    monkeypatch.setattr(orchestrator, "_load_subjects_with_overrides", lambda **kwargs: [])
    monkeypatch.setattr(orchestrator, "_sync_subject_server_override_statuses", lambda subjects: None)
    monkeypatch.setattr(
        orchestrator,
        "_run_pipeline_for_state",
        lambda **kwargs: pipeline_calls.append(kwargs) or _mock_owned_apply(kwargs, "apply-marker-drift"),
    )
    monkeypatch.setattr(
        orchestrator,
        "write_operational_log",
        lambda **kwargs: operational_events.append(kwargs) or kwargs,
    )

    result = orchestrator._execute_set_global_mode(
        {"job_id": "job-marker-drift", "requested_by": "pytest"},
        {"mode": "selective"},
    )

    assert result["ok"] is True
    assert result["stage"] == "commit"
    assert len(pipeline_calls) == 1
    assert operational_events[-1]["details"]["code"] == "LIVE_DATAPLANE_ARTIFACT_DRIFT"
    assert operational_events[-1]["details"]["live_artifact_consistency"]["missing_markers_count"] == 1


def test_set_selective_default_reapplies_when_live_dataplane_drift_is_detected(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _seed_selective_routing()

    pipeline_calls: list[dict[str, object]] = []
    fast_reconcile_calls: list[dict[str, object]] = []
    full_reconcile_calls: list[dict[str, object]] = []

    monkeypatch.setattr(
        orchestrator,
        "probe_live_global_mode",
        lambda: {"ok": True, "mode": "direct", "selective_default": "direct"},
    )
    monkeypatch.setattr(
        orchestrator,
        "validate_global_mode_request",
        lambda mode, routing=None: {"ok": True, "mode": mode, "preflight": {}},
    )
    monkeypatch.setattr(
        orchestrator,
        "mihomo_runtime_satisfies_routing",
        lambda routing: {"ok": False, "reason": "pytest_drift"},
    )
    monkeypatch.setattr(
        orchestrator,
        "reconcile_mihomo_selective_default_fast",
        lambda **kwargs: fast_reconcile_calls.append(kwargs) or {
            "ok": False,
            "fast_path": True,
            "fallback_required": True,
            "reconcile_reason": "verified_publication_requires_full_reconcile",
        },
    )
    monkeypatch.setattr(
        orchestrator,
        "reconcile_mihomo_runtime",
        lambda **kwargs: full_reconcile_calls.append(kwargs) or {"ok": True},
    )
    monkeypatch.setattr(orchestrator, "_load_user_override_map", lambda: {})
    monkeypatch.setattr(orchestrator, "_load_server_override_map", lambda: {})
    monkeypatch.setattr(orchestrator, "_load_subjects_with_overrides", lambda **kwargs: [])
    monkeypatch.setattr(orchestrator, "_sync_subject_server_override_statuses", lambda subjects: None)
    monkeypatch.setattr(
        orchestrator,
        "_run_pipeline_for_state",
        lambda **kwargs: pipeline_calls.append(kwargs) or {
            "ok": True,
            "apply_id": "apply-2",
            "dataplane": {"message": "ok", "error_code": None, "error_message": None},
        },
    )

    result = orchestrator._execute_set_selective_default(
        {"job_id": "job-2", "requested_by": "pytest"},
        {"selective_default": "direct"},
    )

    assert result["ok"] is True
    assert result["stage"] == "commit"
    assert len(pipeline_calls) == 1
    assert len(fast_reconcile_calls) == 1
    assert len(full_reconcile_calls) == 1
    assert pipeline_calls[0]["extra"]["rules_effective"]["selective_default"] == "direct"
    assert pipeline_calls[0]["extra"]["rules_effective"]["default_action"] == "DIRECT"


def test_set_selective_default_does_not_retry_unmarked_fast_failure(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _seed_selective_routing()

    full_reconcile_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        orchestrator,
        "probe_live_global_mode",
        lambda: {"ok": True, "mode": "direct", "selective_default": "direct"},
    )
    monkeypatch.setattr(
        orchestrator,
        "_applied_manifest_routing_drift",
        lambda routing=None: {"detected": True, "mismatches": {"selective_default": True}},
    )
    monkeypatch.setattr(
        orchestrator,
        "mihomo_runtime_satisfies_routing",
        lambda routing: {"ok": False, "reason": "pytest_drift"},
    )
    monkeypatch.setattr(
        orchestrator,
        "reconcile_mihomo_selective_default_fast",
        lambda **kwargs: {
            "ok": False,
            "fast_path": True,
            "fallback_required": False,
            "reconcile_reason": "runtime_mutation_failed",
        },
    )
    monkeypatch.setattr(
        orchestrator,
        "reconcile_mihomo_runtime",
        lambda **kwargs: full_reconcile_calls.append(kwargs) or {"ok": True},
    )

    result = orchestrator._execute_set_selective_default(
        {"job_id": "job-selective-default-no-retry", "requested_by": "pytest"},
        {"selective_default": "direct"},
    )

    assert result["ok"] is False
    assert result["code"] == "MIHOMO_RECONCILE_FAILED"
    assert full_reconcile_calls == []


def test_set_selective_default_skips_full_mihomo_reconcile_when_runtime_matches(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _seed_selective_routing()

    pipeline_calls: list[dict[str, object]] = []

    monkeypatch.setattr(
        orchestrator,
        "probe_live_global_mode",
        lambda: {"ok": True, "mode": "direct", "selective_default": "direct"},
    )
    monkeypatch.setattr(orchestrator, "_applied_manifest_routing_drift", lambda routing=None: {"detected": False})
    monkeypatch.setattr(
        orchestrator,
        "mihomo_runtime_satisfies_routing",
        lambda routing: {"ok": True, "reason": "pytest_runtime_matches"},
    )
    monkeypatch.setattr(
        orchestrator,
        "reconcile_mihomo_runtime",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("full Mihomo reconcile must be skipped")),
    )
    monkeypatch.setattr(orchestrator, "_load_user_override_map", lambda: {})
    monkeypatch.setattr(orchestrator, "_load_server_override_map", lambda: {})
    monkeypatch.setattr(orchestrator, "_load_subjects_with_overrides", lambda **kwargs: [])
    monkeypatch.setattr(orchestrator, "_sync_subject_server_override_statuses", lambda subjects: None)
    monkeypatch.setattr(
        orchestrator,
        "_run_pipeline_for_state",
        lambda **kwargs: pipeline_calls.append(kwargs) or {
            "ok": True,
            "apply_id": "apply-selective-default-runtime-matches",
            "dataplane": {"message": "ok", "error_code": None, "error_message": None},
        },
    )

    result = orchestrator._execute_set_selective_default(
        {"job_id": "job-selective-default-runtime-matches", "requested_by": "pytest"},
        {"selective_default": "vpn"},
    )

    assert result["ok"] is True
    assert result["stage"] == "commit"
    assert len(pipeline_calls) == 1


def test_set_selective_default_skips_pipeline_when_global_direct_is_clean(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO routing_global_state (
                id, desired_mode, applied_mode, selective_default, server_mode, apply_state
            )
            VALUES (1, 'direct', 'direct', 'direct', 'auto', 'clean')
            """
        )

    monkeypatch.setattr(
        orchestrator,
        "probe_live_global_mode",
        lambda: {"ok": True, "mode": "direct", "selective_default": "direct"},
    )
    monkeypatch.setattr(
        orchestrator,
        "_applied_manifest_routing_drift",
        lambda routing=None: {"detected": False},
    )
    monkeypatch.setattr(
        orchestrator,
        "reconcile_mihomo_runtime",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("selective_default in global direct should not reconcile mihomo")),
    )
    monkeypatch.setattr(
        orchestrator,
        "_run_pipeline_for_state",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("selective_default in global direct should not run full pipeline")),
    )

    result = orchestrator._execute_set_selective_default(
        {"job_id": "job-fast-selective-default", "requested_by": "pytest"},
        {"selective_default": "vpn"},
    )

    assert result["ok"] is True
    assert result["stage"] == "commit"
    assert result["runtime_state_unchanged"] is True
    assert result["routing"]["selective_default"] == "vpn"
    with db_session() as connection:
        rules_state = connection.execute("SELECT selective_default FROM rules_state WHERE id = 1").fetchone()
        metadata = connection.execute(
            "SELECT metadata_json FROM rules_metadata WHERE ruleset_id = 'effective'"
        ).fetchone()
    assert rules_state["selective_default"] == "vpn"
    assert '"selective_default": "vpn"' in metadata["metadata_json"]


def test_set_selective_default_skips_selective_default_only_artifact_drift_in_global_direct(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO routing_global_state (
                id, desired_mode, applied_mode, selective_default, server_mode, apply_state
            )
            VALUES (1, 'direct', 'direct', 'direct', 'auto', 'clean')
            """
        )

    monkeypatch.setattr(
        orchestrator,
        "probe_live_global_mode",
        lambda: {"ok": True, "mode": "direct", "selective_default": "direct"},
    )
    monkeypatch.setattr(
        orchestrator,
        "_applied_manifest_routing_drift",
        lambda routing=None: {
            "detected": True,
            "code": "APPLIED_MANIFEST_ROUTING_MISMATCH",
            "mismatches": {
                "selective_default": {
                    "routing": "direct",
                    "applied_manifest": "vpn",
                },
            },
        },
    )
    monkeypatch.setattr(
        orchestrator,
        "reconcile_mihomo_runtime",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("ignorable artifact drift should not reconcile mihomo")),
    )
    monkeypatch.setattr(
        orchestrator,
        "_run_pipeline_for_state",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("ignorable artifact drift should not run full pipeline")),
    )

    result = orchestrator._execute_set_selective_default(
        {"job_id": "job-fast-selective-default-artifact", "requested_by": "pytest"},
        {"selective_default": "vpn"},
    )

    assert result["ok"] is True
    assert result["runtime_state_unchanged"] is True
    assert result["routing"]["selective_default"] == "vpn"


def test_set_global_direct_skips_mihomo_reconcile(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _seed_selective_routing()

    pipeline_calls: list[dict[str, object]] = []

    monkeypatch.setattr(
        orchestrator,
        "probe_live_global_mode",
        lambda: {"ok": True, "mode": "selective", "selective_default": "direct"},
    )
    monkeypatch.setattr(
        orchestrator,
        "validate_global_mode_request",
        lambda mode, routing=None: {"ok": True, "mode": mode, "preflight": {}},
    )
    monkeypatch.setattr(
        orchestrator,
        "reconcile_mihomo_runtime",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("direct mode should not reconcile mihomo")),
    )
    monkeypatch.setattr(orchestrator, "_load_user_override_map", lambda: {})
    monkeypatch.setattr(orchestrator, "_load_server_override_map", lambda: {})
    monkeypatch.setattr(orchestrator, "_load_subjects_with_overrides", lambda **kwargs: [])
    monkeypatch.setattr(orchestrator, "_sync_subject_server_override_statuses", lambda subjects: None)
    monkeypatch.setattr(
        orchestrator,
        "_run_pipeline_for_state",
        lambda **kwargs: pipeline_calls.append(kwargs) or _mock_owned_apply(kwargs, "apply-direct"),
    )

    result = orchestrator._execute_set_global_mode(
        {"job_id": "job-direct", "requested_by": "pytest"},
        {"mode": "direct"},
    )

    assert result["ok"] is True
    assert result["stage"] == "commit"
    assert len(pipeline_calls) == 1


def test_set_global_mode_uses_precompiled_profile_when_available(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _seed_selective_routing()

    pipeline_calls: list[dict[str, object]] = []

    monkeypatch.setattr(
        orchestrator,
        "probe_live_global_mode",
        lambda: {"ok": True, "mode": "direct", "selective_default": "direct"},
    )
    monkeypatch.setattr(
        orchestrator,
        "validate_global_mode_request",
        lambda mode, routing=None: {"ok": True, "mode": mode, "preflight": {}},
    )
    monkeypatch.setattr(orchestrator, "reconcile_mihomo_runtime", lambda **kwargs: {"ok": True})
    monkeypatch.setattr(
        orchestrator,
        "load_precompiled_global_mode_profile",
        lambda mode, routing=None: {
            "affected_subject_ids": ["lan:1"],
            "subject_runtime_statuses": [{"subject_id": "lan:1", "scoped_runtime_status": "applied"}],
            "manifest": {
                "routing_global_state": {"desired_mode": mode, "applied_mode": mode},
                "summary": {"subjects_count": 1, "path_counts": {"selective": 1}, "extra_keys": []},
                "runtime_enforcement": {
                    "dataplane_capability": "nft_owned_table",
                    "enforcement_level": "owned_table_ready",
                    "traffic_enforcement_guaranteed": False,
                    "supported_modes": {"direct": True},
                    "missing_runtime_requirements": [],
                },
                "scoped_egress": {},
                "extra": {},
                "global_preflight": {"missing": [], "profile": {"profile": "global_v1"}},
                "contract_version": "v1",
                "owned_table": "inet fwrouter_v2",
                "required_chains": [],
                "generated_at": "2026-01-01T00:00:00Z",
                "subjects": [],
            },
        },
    )
    monkeypatch.setattr(
        orchestrator,
        "_run_pipeline_for_manifest",
        lambda **kwargs: pipeline_calls.append(kwargs) or _mock_owned_apply(kwargs, "apply-precompiled"),
    )
    monkeypatch.setattr(
        orchestrator,
        "_load_subjects_with_overrides",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("fallback path must not load subjects")),
    )

    result = orchestrator._execute_set_global_mode(
        {"job_id": "job-precompiled", "requested_by": "pytest"},
        {"mode": "selective"},
    )

    assert result["ok"] is True
    assert result["stage"] == "commit"
    assert len(pipeline_calls) == 1


def test_set_global_mode_skips_mihomo_reconcile_when_runtime_already_matches(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _seed_selective_routing()

    monkeypatch.setattr(
        orchestrator,
        "probe_live_global_mode",
        lambda: {"ok": True, "mode": "direct", "selective_default": "direct"},
    )
    monkeypatch.setattr(
        orchestrator,
        "validate_global_mode_request",
        lambda mode, routing=None: {"ok": True, "mode": mode, "preflight": {}},
    )
    monkeypatch.setattr(
        orchestrator,
        "mihomo_runtime_satisfies_routing",
        lambda routing: {"ok": True, "reason": "pytest_runtime_matches"},
    )
    monkeypatch.setattr(
        orchestrator,
        "reconcile_mihomo_runtime",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("full reconcile must be skipped")),
    )
    monkeypatch.setattr(
        orchestrator,
        "load_precompiled_global_mode_profile",
        lambda mode, routing=None: {
            "affected_subject_ids": [],
            "subject_runtime_statuses": [],
            "manifest": {
                "routing_global_state": {"desired_mode": mode, "applied_mode": mode},
                "summary": {"subjects_count": 0, "path_counts": {}, "extra_keys": []},
                "runtime_enforcement": {
                    "dataplane_capability": "nft_owned_table",
                    "enforcement_level": "owned_table_ready",
                    "traffic_enforcement_guaranteed": False,
                    "supported_modes": {"direct": True},
                    "missing_runtime_requirements": [],
                },
                "scoped_egress": {},
                "extra": {},
                "global_preflight": {"missing": [], "profile": {"profile": "global_v1"}},
                "contract_version": "v1",
                "owned_table": "inet fwrouter_v2",
                "required_chains": [],
                "generated_at": "2026-01-01T00:00:00Z",
                "subjects": [],
            },
        },
    )
    monkeypatch.setattr(
        orchestrator,
        "_run_pipeline_for_manifest",
        lambda **kwargs: _mock_owned_apply(kwargs, "apply-runtime-matches"),
    )
    monkeypatch.setattr(orchestrator, "_sync_subject_server_override_statuses", lambda subjects: None)

    result = orchestrator._execute_set_global_mode(
        {"job_id": "job-runtime-matches", "requested_by": "pytest"},
        {"mode": "selective"},
    )

    assert result["ok"] is True
    assert result["stage"] == "commit"


def test_set_global_mode_reapplies_when_applied_manifest_routing_is_stale(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _seed_selective_routing()

    settings = get_settings()
    dataplane_dir = settings.paths.generated_dir / "dataplane"
    dataplane_dir.mkdir(parents=True, exist_ok=True)
    (dataplane_dir / "applied-manifest.json").write_text(
        """
        {
          "routing_global_state": {
            "desired_mode": "selective",
            "applied_mode": "direct",
            "selective_default": "direct",
            "server_mode": "auto",
            "desired_fixed_server_id": null,
            "applied_fixed_server_id": null
          }
        }
        """.strip(),
        encoding="utf-8",
    )

    pipeline_calls: list[dict[str, object]] = []
    operational_events: list[dict[str, object]] = []

    monkeypatch.setattr(
        orchestrator,
        "probe_live_global_mode",
        lambda: {"ok": True, "mode": "selective", "selective_default": "direct"},
    )
    monkeypatch.setattr(
        orchestrator,
        "validate_global_mode_request",
        lambda mode, routing=None: {"ok": True, "mode": mode, "preflight": {}},
    )
    monkeypatch.setattr(orchestrator, "reconcile_mihomo_runtime", lambda **kwargs: {"ok": True})
    monkeypatch.setattr(orchestrator, "_load_user_override_map", lambda: {})
    monkeypatch.setattr(orchestrator, "_load_server_override_map", lambda: {})
    monkeypatch.setattr(orchestrator, "_load_subjects_with_overrides", lambda **kwargs: [])
    monkeypatch.setattr(orchestrator, "_sync_subject_server_override_statuses", lambda subjects: None)
    monkeypatch.setattr(
        orchestrator,
        "_run_pipeline_for_state",
        lambda **kwargs: pipeline_calls.append(kwargs) or _mock_owned_apply(kwargs, "apply-3"),
    )
    monkeypatch.setattr(
        orchestrator,
        "write_operational_log",
        lambda **kwargs: operational_events.append(kwargs) or kwargs,
    )

    result = orchestrator._execute_set_global_mode(
        {"job_id": "job-3", "requested_by": "pytest"},
        {"mode": "selective"},
    )

    assert result["ok"] is True
    assert result["stage"] == "commit"
    assert len(pipeline_calls) == 1
    assert operational_events[-1]["event_type"] == "routing_artifact_drift_detected"
