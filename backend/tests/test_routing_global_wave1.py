from __future__ import annotations
from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import initialize_database

from pathlib import Path
import json
from typing import Any
from fastapi.testclient import TestClient
from fwrouter_api.adapters.dataplane import DataplaneOperation, DataplaneResult
from fwrouter_api.db.connection import db_session, initialize_database
from fwrouter_api.jobs.manager import get_default_job_manager
from fwrouter_api.main import create_app
from fwrouter_api.services.apply_orchestrator import (
    set_global_mode,
    submit_apply_mutation,
    INTENT_SET_GLOBAL_MODE,
    repair_global_direct_runtime_sync,
)
from fwrouter_api.services.servers import (
    ensure_routing_global_state,
    get_routing_global_state,
)

def _configure_env(monkeypatch, tmp_path: Path) -> None:
    get_default_job_manager().wait_for_idle()
    monkeypatch.setenv("FWROUTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("FWROUTER_DATABASE_URL", f"sqlite:///{tmp_path}/fwrouter.db")
    get_settings.cache_clear()

def _seed_subject(subject_id: str, desired_mode: str = "global") -> None:
    with db_session() as connection:
        connection.execute(
            "INSERT INTO subjects (subject_id, subject_type, stable_key, display_name, desired_mode, runtime_state, is_active) VALUES (?, 'lan', ?, ?, ?, 'active', 1)",
            (subject_id, subject_id, subject_id, desired_mode),
        )

def _seed_user_override(subject_id: str, mode: str = "vpn") -> None:
    with db_session() as connection:
        connection.execute(
            "INSERT INTO subject_user_overrides (subject_id, override_mode) VALUES (?, ?)",
            (subject_id, mode),
        )

def _patch_successful_dataplane(monkeypatch):
    class FakeAdapter:
        def check(self, *args, **kwargs):
            return DataplaneResult(
                ok=True,
                operation=DataplaneOperation.CHECK,
                message="check ok",
                details={"stage": "check", "table_exists": True, "required_chains": {}},
            )
        def apply(self, *args, **kwargs):
            return DataplaneResult(
                ok=True,
                operation=DataplaneOperation.APPLY,
                message="apply ok",
                details={"stage": "commit", "table_exists": True, "required_chains": {}},
            )
        def rollback(self, *args, **kwargs):
            return DataplaneResult(
                ok=True,
                operation=DataplaneOperation.ROLLBACK,
                message="rollback ok",
                details={"stage": "rollback"},
            )
    monkeypatch.setattr("fwrouter_api.services.apply.DEFAULT_DATAPLANE_ADAPTER", FakeAdapter())
    monkeypatch.setattr(
        "fwrouter_api.services.apply._apply_global_mode_hot_swap",
        lambda **kwargs: DataplaneResult(
            ok=True,
            operation=DataplaneOperation.APPLY,
            message="hot swap ok",
            details={"stage": "commit", "hot_swap": True},
        ),
    )
    return FakeAdapter()

def _patch_failing_dataplane(monkeypatch):
    class FailingAdapter:
        def __init__(self):
            self.apply_calls = 0
            self.rollback_calls = 0
        def check(self, *args, **kwargs):
            return DataplaneResult(
                ok=True,
                operation=DataplaneOperation.CHECK,
                message="check ok",
                details={"stage": "check", "table_exists": True, "required_chains": {}},
            )
        def apply(self, *args, **kwargs):
            self.apply_calls += 1
            return DataplaneResult(
                ok=False,
                operation=DataplaneOperation.APPLY,
                message="apply failed",
                details={"stage": "apply"},
                error_code="FORCED_APPLY_FAILURE",
                error_message="forced apply failure",
            )
        def rollback(self, *args, **kwargs):
            self.rollback_calls += 1
            return DataplaneResult(
                ok=True,
                operation=DataplaneOperation.ROLLBACK,
                message="rollback ok",
                details={"stage": "rollback"},
            )
    adapter = FailingAdapter()
    monkeypatch.setattr("fwrouter_api.services.apply.DEFAULT_DATAPLANE_ADAPTER", adapter)
    return adapter

def test_global_mode_success_commit_contract(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _seed_subject("lan-follow")
    _seed_subject("lan-user")
    _seed_user_override("lan-user", mode="vpn")
    _patch_successful_dataplane(monkeypatch)
    monkeypatch.setattr(
        "fwrouter_api.services.apply.probe_live_global_mode",
        lambda: {"ok": True, "mode": "direct", "selective_default": "direct"},
    )
    monkeypatch.setattr(
        "fwrouter_api.services.apply.live_mode_matches_intent",
        lambda **kwargs: True,
    )
    monkeypatch.setattr(
        "fwrouter_api.services.apply_orchestrator._current_routing_drift",
        lambda **kwargs: {
            "detected": False,
            "code": None,
            "routing": kwargs.get("routing"),
            "expected_mode": "direct",
            "expected_selective_default": "direct",
            "live_probe": {"ok": True, "mode": "direct", "selective_default": "direct"},
            "live_mode": "direct",
            "live_selective_default": "direct",
        },
    )
    monkeypatch.setattr(
        "fwrouter_api.services.apply_orchestrator._applied_manifest_routing_drift",
        lambda **kwargs: {
            "detected": False,
            "code": None,
            "routing": kwargs.get("routing"),
            "applied_manifest_routing": None,
            "mismatches": {},
        },
    )
    
    result = set_global_mode("direct", requested_by="pytest")
    assert result["ok"] is True
    assert result["stage"] == "commit"
    assert "lan-follow" in result.get("affected_subject_ids", [])


def test_repair_global_direct_runtime_clears_stale_routing_error(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    ensure_routing_global_state()
    _seed_subject("lan-follow")
    _patch_successful_dataplane(monkeypatch)
    monkeypatch.setattr(
        "fwrouter_api.services.apply.probe_live_global_mode",
        lambda: {"ok": True, "mode": "direct", "selective_default": "direct"},
    )
    monkeypatch.setattr(
        "fwrouter_api.services.apply.live_mode_matches_intent",
        lambda **kwargs: True,
    )

    with db_session() as connection:
        connection.execute(
            """
            UPDATE routing_global_state
            SET
                desired_mode = 'direct',
                applied_mode = 'direct',
                apply_state = 'failed',
                error_code = 'NFT_VPN_CONTRACT_MISSING',
                error_message = 'Candidate is missing VPN contract marker.'
            WHERE id = 1
            """
        )

    result = repair_global_direct_runtime_sync(requested_by="pytest")

    assert result["status"] == "success"
    routing = get_routing_global_state()
    assert routing is not None
    assert routing["apply_state"] == "clean"
    assert routing["error_code"] is None
    assert routing["error_message"] is None
