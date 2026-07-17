from __future__ import annotations
from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import initialize_database


import json
from pathlib import Path
from types import SimpleNamespace

from fwrouter_api.db.connection import db_session, initialize_database
from fwrouter_api.services.bootstrap import (
    bootstrap_backend,
    recover_startup_mihomo_selector,
    recover_startup_live_routing_from_persisted_mode,
    recover_startup_routing_to_direct,
    recover_startup_scoped_subject_routing,
)
from fwrouter_api.services.servers import ensure_routing_global_state
from fwrouter_api.services.subjects import get_subject, list_subjects


def _configure_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FWROUTER_STATE_DIR", str(tmp_path / "state"))
    get_settings.cache_clear()


def _stub_bootstrap_recovery(monkeypatch) -> None:
    monkeypatch.setattr(
        "fwrouter_api.services.bootstrap.recover_startup_live_routing_from_persisted_mode",
        lambda: {"recovery_required": False, "recovered": False},
    )
    monkeypatch.setattr(
        "fwrouter_api.services.bootstrap.recover_startup_mihomo_selector",
        lambda: {"restored": False},
    )
    monkeypatch.setattr(
        "fwrouter_api.services.bootstrap.recover_startup_intended_routing",
        lambda: {"reapply_required": False, "reapplied": False},
    )
    monkeypatch.setattr(
        "fwrouter_api.services.bootstrap.recover_startup_scoped_subject_routing",
        lambda: {"reapply_required": False, "reapplied": False},
    )


def test_bootstrap_normalizes_legacy_tailscale_subjects(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    _stub_bootstrap_recovery(monkeypatch)
    initialize_database()

    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO servers (
                server_id,
                server_name,
                provider_name,
                inventory_state
            )
            VALUES ('server-1', 'server-1', 'pytest', 'active')
            """
        )
        connection.execute(
            """
            INSERT INTO subjects (
                subject_id,
                subject_type,
                stable_key,
                display_name,
                desired_mode,
                runtime_state,
                is_active
            )
            VALUES ('legacy-ts-1', 'tailscale', 'legacy-ts-1', 'legacy-ts-1', 'global', 'active', 1)
            """
        )

    result = bootstrap_backend()
    subject = get_subject("legacy-ts-1")

    assert result["subject_taxonomy"]["normalized_tailscale_node_count"] == 1
    assert subject is not None
    assert subject["subject_type"] == "tailscale_node"
    assert subject["stored_subject_type"] == "tailscale_node"


def test_list_subjects_accepts_tailscale_alias_filter(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    _stub_bootstrap_recovery(monkeypatch)
    initialize_database()

    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO subjects (
                subject_id,
                subject_type,
                stable_key,
                display_name,
                desired_mode,
                runtime_state,
                is_active
            )
            VALUES ('ts-node-2', 'tailscale_node', 'ts-node-2', 'ts-node-2', 'global', 'active', 1)
            """
        )

    subjects = list_subjects(subject_type="tailscale")

    assert len(subjects) == 1
    assert subjects[0]["subject_type"] == "tailscale_node"


def test_bootstrap_reapplies_intended_non_direct_mode_when_live_drifts(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "fwrouter_api.services.bootstrap.recover_startup_live_routing_from_persisted_mode",
        lambda: {"recovery_required": False, "recovered": False},
    )
    monkeypatch.setattr(
        "fwrouter_api.services.bootstrap.recover_startup_mihomo_selector",
        lambda: {"restored": True},
    )
    initialize_database()
    ensure_routing_global_state()

    with db_session() as connection:
        connection.execute(
            """
            UPDATE routing_global_state
            SET desired_mode = 'selective',
                applied_mode = 'selective',
                selective_default = 'direct',
                updated_at = CURRENT_TIMESTAMP
            """
        )

    monkeypatch.setattr(
        "fwrouter_api.services.bootstrap.probe_live_global_mode",
        lambda: {"ok": True, "table_exists": True, "mode": "direct", "selective_default": "direct"},
    )
    monkeypatch.setattr(
        "fwrouter_api.adapters.mihomo.DEFAULT_MIHOMO_ADAPTER",
        SimpleNamespace(
            health=lambda: SimpleNamespace(runtime_state="running", message="ok", details={})
        ),
    )
    applied: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "fwrouter_api.services.apply_orchestrator.apply_global_mode_immediately",
        lambda mode, requested_by="api": applied.append((mode, requested_by)) or {"ok": True, "mode": mode},
    )

    result = bootstrap_backend()

    assert applied == [("selective", "startup-intended-recovery")]
    recovery = result["startup_intended_routing_recovery"]
    assert recovery["reapply_required"] is True
    assert recovery["reapplied"] is True
    assert recovery["intended_mode"] == "selective"


def test_bootstrap_skips_intended_reapply_when_live_matches(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "fwrouter_api.services.bootstrap.recover_startup_live_routing_from_persisted_mode",
        lambda: {"recovery_required": False, "recovered": False},
    )
    monkeypatch.setattr(
        "fwrouter_api.services.bootstrap.recover_startup_mihomo_selector",
        lambda: {"restored": True},
    )
    initialize_database()
    ensure_routing_global_state()

    with db_session() as connection:
        connection.execute(
            """
            UPDATE routing_global_state
            SET desired_mode = 'selective',
                applied_mode = 'selective',
                selective_default = 'direct',
                updated_at = CURRENT_TIMESTAMP
            """
        )

    monkeypatch.setattr(
        "fwrouter_api.services.bootstrap.probe_live_global_mode",
        lambda: {"ok": True, "table_exists": True, "mode": "selective", "selective_default": "direct"},
    )
    monkeypatch.setattr(
        "fwrouter_api.adapters.mihomo.DEFAULT_MIHOMO_ADAPTER",
        SimpleNamespace(
            health=lambda: SimpleNamespace(runtime_state="running", message="ok", details={})
        ),
    )
    monkeypatch.setattr(
        "fwrouter_api.services.apply_orchestrator.apply_global_mode_immediately",
        lambda mode, requested_by="api": (_ for _ in ()).throw(AssertionError("unexpected reapply")),
    )

    result = bootstrap_backend()

    recovery = result["startup_intended_routing_recovery"]
    assert recovery["reapply_required"] is False
    assert recovery["reapplied"] is False


def test_recover_startup_scoped_subject_routing_reapplies_missing_live_rules(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO subjects (
                subject_id,
                subject_type,
                stable_key,
                display_name,
                desired_mode,
                applied_mode,
                apply_state,
                runtime_state,
                is_active
            )
            VALUES (
                'lan:fc-41-16-df-f3-5e',
                'lan',
                'lan:fc-41-16-df-f3-5e',
                'Pixel-9',
                'selective',
                'selective',
                'clean',
                'active',
                1
            )
            """
        )

    monkeypatch.setattr(
        "fwrouter_api.services.bootstrap._read_live_classify_chain",
        lambda: {
            "ok": True,
            "raw_chain": 'chain fwrouter_classify { goto fwrouter_direct comment "global direct v1" }',
            "error_code": None,
            "error_message": None,
        },
    )
    applied: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        "fwrouter_api.services.apply_orchestrator.set_subject_admin_mode",
        lambda subject_id, mode, requested_by="api": applied.append((subject_id, mode, requested_by))
        or {"ok": True, "subject_id": subject_id, "mode": mode},
    )

    result = recover_startup_scoped_subject_routing()

    assert result["reapply_required"] is True
    assert result["reapplied"] is True
    assert result["missing_subject_ids"] == ["lan:fc-41-16-df-f3-5e"]
    assert applied == [
        ("lan:fc-41-16-df-f3-5e", "selective", "startup-scoped-subject-recovery")
    ]


def test_recover_startup_scoped_subject_routing_skips_when_live_rules_exist(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO subjects (
                subject_id,
                subject_type,
                stable_key,
                display_name,
                desired_mode,
                applied_mode,
                apply_state,
                runtime_state,
                is_active
            )
            VALUES (
                'lan:fc-41-16-df-f3-5e',
                'lan',
                'lan:fc-41-16-df-f3-5e',
                'Pixel-9',
                'selective',
                'selective',
                'clean',
                'active',
                1
            )
            """
        )

    monkeypatch.setattr(
        "fwrouter_api.services.bootstrap._read_live_classify_chain",
        lambda: {
            "ok": True,
            "raw_chain": 'ip saddr 192.168.0.71 goto fwrouter_direct comment "scoped selective default direct: lan:fc-41-16-df-f3-5e"',
            "error_code": None,
            "error_message": None,
        },
    )
    monkeypatch.setattr(
        "fwrouter_api.services.apply_orchestrator.set_subject_admin_mode",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected reapply")),
    )

    result = recover_startup_scoped_subject_routing()

    assert result["reapply_required"] is False
    assert result["reapplied"] is False
    assert result["missing_subject_ids"] == []


def test_bootstrap_skips_intended_reapply_when_mihomo_is_unreachable(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "fwrouter_api.services.bootstrap.recover_startup_live_routing_from_persisted_mode",
        lambda: {"recovery_required": False, "recovered": False},
    )
    monkeypatch.setattr(
        "fwrouter_api.services.bootstrap.recover_startup_mihomo_selector",
        lambda: {"restored": False},
    )
    initialize_database()
    ensure_routing_global_state()

    with db_session() as connection:
        connection.execute(
            """
            UPDATE routing_global_state
            SET desired_mode = 'selective',
                applied_mode = 'selective',
                selective_default = 'direct',
                updated_at = CURRENT_TIMESTAMP
            """
        )

    monkeypatch.setattr(
        "fwrouter_api.services.bootstrap.probe_live_global_mode",
        lambda: {"ok": True, "table_exists": True, "mode": "direct", "selective_default": "direct"},
    )
    monkeypatch.setattr(
        "fwrouter_api.adapters.mihomo.DEFAULT_MIHOMO_ADAPTER",
        SimpleNamespace(
            health=lambda: SimpleNamespace(
                runtime_state="degraded",
                message="controller unreachable",
                details={"error": "boom"},
            )
        ),
    )
    monkeypatch.setattr(
        "fwrouter_api.services.apply_orchestrator.apply_global_mode_immediately",
        lambda mode, requested_by="api": (_ for _ in ()).throw(AssertionError("unexpected reapply")),
    )

    result = bootstrap_backend()

    recovery = result["startup_intended_routing_recovery"]
    assert recovery["reapply_required"] is False
    assert recovery["reapplied"] is False


def test_recover_startup_mihomo_selector_restores_active_auto_target(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    ensure_routing_global_state()

    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO servers (
                server_id,
                server_name,
                provider_name,
                inventory_state
            )
            VALUES ('srv-norway', 'srv-norway', 'pytest', 'active')
            """
        )
        connection.execute(
            """
            UPDATE routing_global_state
            SET
                desired_mode = 'selective',
                applied_mode = 'selective',
                server_mode = 'auto',
                active_auto_server_id = 'srv-norway',
                updated_at = CURRENT_TIMESTAMP
            WHERE id = 1
            """
        )

    monkeypatch.setattr(
        "fwrouter_api.services.selector.DEFAULT_MIHOMO_ADAPTER",
        SimpleNamespace(
            health=lambda: SimpleNamespace(runtime_state="running"),
            list_servers=lambda: [SimpleNamespace(server_id="srv-norway")],
            apply_server_to_selector=lambda selector_name, server_id: SimpleNamespace(
                ok=True,
                active_server_id=server_id,
                to_dict=lambda: {
                    "ok": True,
                    "selector_name": selector_name,
                    "active_server_id": server_id,
                },
            ),
        ),
    )

    result = recover_startup_mihomo_selector()

    assert result["restored"] is True
    assert result["vpn_auto_restore"]["active_server_id"] == "srv-norway"
    assert result["vpn_global_restore"]["active_server_id"] == "vpn-auto"


def test_bootstrap_immediately_cleans_stale_running_jobs(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    _stub_bootstrap_recovery(monkeypatch)
    initialize_database()

    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO jobs (
                job_id,
                job_type,
                status,
                lock_key,
                requested_by,
                created_at,
                started_at,
                updated_at
            )
            VALUES (
                'stale-running-job',
                'apply_mutation',
                'running',
                'apply',
                'pytest',
                CURRENT_TIMESTAMP,
                CURRENT_TIMESTAMP,
                CURRENT_TIMESTAMP
            )
            """
        )

    result = bootstrap_backend()

    cleaned = result["stale_jobs_cleaned"]
    assert len(cleaned) == 1
    assert cleaned[0]["job_id"] == "stale-running-job"

    with db_session() as connection:
        row = connection.execute(
            "SELECT status, error_code FROM jobs WHERE job_id = 'stale-running-job'"
        ).fetchone()

    assert row["status"] == "failed"
    assert row["error_code"] == "JOB_STALE_TIMEOUT"


def test_bootstrap_normalizes_fwrouter_subject_to_direct(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    _stub_bootstrap_recovery(monkeypatch)
    initialize_database()

    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO servers (
                server_id,
                server_name,
                provider_name,
                inventory_state
            )
            VALUES ('server-1', 'server-1', 'pytest', 'active')
            """
        )
        connection.execute(
            """
            INSERT INTO subjects (
                subject_id,
                subject_type,
                stable_key,
                display_name,
                desired_mode,
                applied_mode,
                runtime_state,
                is_active
            )
            VALUES ('fwrouter:global', 'fwrouter', 'fwrouter:global', 'FWRouter global traffic', 'vpn', 'vpn', 'running', 1)
            """
        )
        connection.execute(
            """
            INSERT INTO subject_server_overrides (
                subject_id,
                selected_server_id,
                selected_until,
                apply_state
            )
            VALUES ('fwrouter:global', 'server-1', datetime('now', '+24 hours'), 'clean')
            """
        )

    result = bootstrap_backend()

    with db_session() as connection:
        row = connection.execute(
            "SELECT desired_mode, applied_mode FROM subjects WHERE subject_id = 'fwrouter:global'"
        ).fetchone()
        override = connection.execute(
            "SELECT selected_server_id FROM subject_server_overrides WHERE subject_id = 'fwrouter:global'"
        ).fetchone()

    assert result["builtin_system_subjects"]["normalized_count"] >= 0
    assert row is not None
    assert row["desired_mode"] == "direct"
    assert row["applied_mode"] == "direct"
    assert override is None


def test_startup_live_recovery_preserves_intended_selective_mode(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    ensure_routing_global_state()

    with db_session() as connection:
        connection.execute(
            """
            UPDATE routing_global_state
            SET desired_mode = 'selective',
                applied_mode = 'selective',
                selective_default = 'direct',
                updated_at = CURRENT_TIMESTAMP
            """
        )

    monkeypatch.setattr(
        "fwrouter_api.services.bootstrap._read_startup_dataplane_payload",
        lambda: {"ok": False, "table_exists": False, "required_chains": {}},
    )

    applied: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "fwrouter_api.services.apply_orchestrator.apply_global_mode_immediately",
        lambda mode, requested_by="api": applied.append((mode, requested_by)) or {"ok": True, "mode": mode},
    )

    recovery = recover_startup_live_routing_from_persisted_mode()

    assert applied == [("selective", "startup-intended-recovery")]
    assert recovery["recovery_required"] is True
    assert recovery["intended_mode"] == "selective"
    assert recovery["recovery_mode"] == "selective"
    assert recovery["recovered"] is True


def test_startup_live_recovery_legacy_alias_keeps_behavior(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "fwrouter_api.services.bootstrap.recover_startup_live_routing_from_persisted_mode",
        lambda: {"recovery_required": True, "recovered": True, "intended_mode": "direct"},
    )

    recovery = recover_startup_routing_to_direct()

    assert recovery["recovery_required"] is True
    assert recovery["recovered"] is True
