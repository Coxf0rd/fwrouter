from __future__ import annotations

from pathlib import Path

from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import db_session, initialize_database
from fwrouter_api.services import apply_orchestrator as orchestrator
from fwrouter_api.services.live_probe_cache import clear_live_probe_cache
from fwrouter_api.services.servers import ensure_routing_global_state


def _configure_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FWROUTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("FWROUTER_DATABASE_URL", f"sqlite:///{tmp_path}/fwrouter.db")
    get_settings.cache_clear()
    clear_live_probe_cache()


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
        lambda **kwargs: pipeline_calls.append(kwargs) or {
            "ok": True,
            "apply_id": "apply-1",
            "dataplane": {"message": "ok", "error_code": None, "error_message": None},
        },
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
        lambda **kwargs: pipeline_calls.append(kwargs) or {
            "ok": True,
            "apply_id": "apply-marker-drift",
            "dataplane": {"message": "ok", "error_code": None, "error_message": None},
        },
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
        lambda **kwargs: pipeline_calls.append(kwargs) or {
            "ok": True,
            "apply_id": "apply-direct",
            "dataplane": {"message": "ok", "error_code": None, "error_message": None},
        },
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
        lambda **kwargs: pipeline_calls.append(kwargs) or {
            "ok": True,
            "apply_id": "apply-precompiled",
            "dataplane": {"message": "ok", "error_code": None, "error_message": None},
        },
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
        lambda **kwargs: {
            "ok": True,
            "apply_id": "apply-runtime-matches",
            "dataplane": {"message": "ok", "error_code": None, "error_message": None},
        },
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
        lambda **kwargs: pipeline_calls.append(kwargs) or {
            "ok": True,
            "apply_id": "apply-3",
            "dataplane": {"message": "ok", "error_code": None, "error_message": None},
        },
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
