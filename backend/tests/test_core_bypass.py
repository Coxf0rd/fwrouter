from __future__ import annotations
from _test_support import configure_test_state_dir as _configure_env
from fwrouter_api.db.connection import initialize_database


import json
from pathlib import Path

from fastapi.testclient import TestClient

from fwrouter_api.core.config import get_settings
import fwrouter_api.services.apply as apply_service
import fwrouter_api.services.routing_manifest as routing_manifest_service
import fwrouter_api.services.subject_policy as subject_policy_service
from fwrouter_api.adapters.dataplane import DataplaneOperation, DataplaneResult
from fwrouter_api.jobs.extended_handlers import register_extended_handlers
from fwrouter_api.jobs.manager import get_default_job_manager
from fwrouter_api.main import create_app
from fwrouter_api.services.core_bypass import get_core_bypass_state, submit_core_bypass_job
from fwrouter_api.services.modules import get_module_state, set_module_desired_state
from fwrouter_api.services.runtime import get_runtime_summary
from fwrouter_api.services.system_summary import build_system_summary
from fwrouter_api.services.watchdog import run_vpn_watchdog_auto_check




def _client() -> TestClient:
    return TestClient(create_app(enable_startup_tasks=False))


class _FailingApplyAdapter:
    def check(self, plan):  # noqa: ANN001
        return DataplaneResult(
            ok=True,
            operation=DataplaneOperation.CHECK,
            message="check ok",
            details={
                "stage": "check",
                "owned_table": "inet fwrouter_v2",
                "required_chains": {
                    "prerouting": True,
                    "output": True,
                    "forward": True,
                    "postrouting": True,
                    "fwrouter_classify": True,
                    "fwrouter_direct": True,
                    "fwrouter_vpn": True,
                },
                "table_exists": True,
            },
        )

    def apply(self, plan):  # noqa: ANN001
        return DataplaneResult(
            ok=False,
            operation=DataplaneOperation.APPLY,
            message="apply failed",
            details={"stage": "apply"},
            error_code="NFT_APPLY_FAILED",
            error_message="apply failed",
        )

    def rollback(self, plan):  # noqa: ANN001
        return DataplaneResult(
            ok=True,
            operation=DataplaneOperation.ROLLBACK,
            message="rollback ok",
            details={"stage": "rollback"},
        )


class _SuccessfulUnitApplyAdapter:
    """Explicit component-level success response; no host/native probe is performed."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def check(self, plan):  # noqa: ANN001
        self.calls.append("check")
        return DataplaneResult(
            ok=True,
            operation=DataplaneOperation.CHECK,
            message="unit-owned-table check ok",
            details={
                "stage": "check",
                "owned_table": "inet fwrouter_v2",
                "table_exists": True,
                "required_chains": {
                    "prerouting": True,
                    "input": True,
                    "output": True,
                    "forward": True,
                    "postrouting": True,
                    "fwrouter_classify": True,
                    "fwrouter_direct": True,
                    "fwrouter_vpn": True,
                },
            },
        )

    def apply(self, plan):  # noqa: ANN001
        self.calls.append("apply")
        return DataplaneResult(
            ok=True,
            operation=DataplaneOperation.APPLY,
            message="unit-owned-table apply ok",
            details={
                "stage": "verify",
                "owned_table": "inet fwrouter_v2",
                "table_exists": True,
                "routing_mode": "direct",
            },
        )

    def rollback(self, plan):  # noqa: ANN001
        self.calls.append("rollback")
        return DataplaneResult(
            ok=True,
            operation=DataplaneOperation.ROLLBACK,
            message="unit-owned-table rollback ok",
            details={"stage": "rollback"},
        )


def _install_successful_unit_apply(monkeypatch) -> _SuccessfulUnitApplyAdapter:
    adapter = _SuccessfulUnitApplyAdapter()
    monkeypatch.setattr(apply_service, "DEFAULT_DATAPLANE_ADAPTER", adapter)
    monkeypatch.setattr(apply_service, "build_global_preflight", _unit_bypass_preflight)
    monkeypatch.setattr(routing_manifest_service, "build_global_preflight", _unit_bypass_preflight)
    monkeypatch.setattr(subject_policy_service, "build_global_preflight", _unit_bypass_preflight)
    return adapter


def _unit_bypass_preflight(**_kwargs) -> dict:
    """Unit-owned policy input for bypass transitions; makes no live health claim."""
    return {
        "profile": {"profile": "pytest-unit-owned-table"},
        "can_enforce_global_direct": True,
        "can_enforce_global_selective": False,
        "can_enforce_global_vpn": False,
        "missing": [],
        "selective_rules": {"requires_vpn_runtime": False, "path_kind": "ip_only"},
        "vpn_contour": None,
    }


def test_core_bypass_enable_and_disable_roundtrip(
    monkeypatch, tmp_path: Path, isolated_host_observations,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    monkeypatch.setenv("FWROUTER_WATCHDOG_SCHEDULER_ENABLED", "true")
    get_settings.cache_clear()
    initialize_database()
    adapter = _install_successful_unit_apply(monkeypatch)
    register_extended_handlers(get_default_job_manager())
    set_module_desired_state("watchdog", "enabled", run_now=False)

    enable_job = submit_core_bypass_job(
        action="enable",
        requested_by="pytest",
        run_now=True,
    )

    assert enable_job["status"] == "success"
    assert adapter.calls == ["check", "apply"]
    assert get_core_bypass_state()["enabled"] is True
    assert get_module_state("core")["runtime_state"] == "paused"
    assert get_module_state("watchdog")["desired_state"] == "enabled"
    assert get_module_state("watchdog")["runtime_state"] == "paused"

    summary = build_system_summary()
    runtime = get_runtime_summary()
    assert summary["core"]["bypass"]["enabled"] is True
    assert runtime["dataplane"]["enforcement_level"] == "bypass_direct_safe"
    assert runtime["dataplane"]["traffic_enforcement_guaranteed"] is False

    disable_job = submit_core_bypass_job(
        action="disable",
        requested_by="pytest",
        run_now=True,
    )

    assert disable_job["status"] == "success"
    assert adapter.calls == ["check", "apply", "check", "apply"]
    assert get_core_bypass_state()["enabled"] is False
    assert get_module_state("core")["runtime_state"] == "running"
    assert get_module_state("watchdog")["desired_state"] == "enabled"
    assert get_module_state("watchdog")["runtime_state"] == "paused"


def test_core_bypass_failed_disable_keeps_bypass_enabled(
    monkeypatch, tmp_path: Path, isolated_host_observations,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    adapter = _install_successful_unit_apply(monkeypatch)
    register_extended_handlers(get_default_job_manager())

    enable_job = submit_core_bypass_job(
        action="enable",
        requested_by="pytest",
        run_now=True,
    )
    assert enable_job["status"] == "success"
    assert adapter.calls == ["check", "apply"]

    monkeypatch.setattr(apply_service, "DEFAULT_DATAPLANE_ADAPTER", _FailingApplyAdapter())

    disable_job = submit_core_bypass_job(
        action="disable",
        requested_by="pytest",
        run_now=True,
    )

    assert disable_job["status"] == "failed"
    assert get_core_bypass_state()["enabled"] is True
    assert get_module_state("core")["apply_state"] == "failed"
    assert get_module_state("core")["runtime_state"] == "paused"


def test_core_bypass_api_contract(
    monkeypatch, tmp_path: Path, isolated_host_observations,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    adapter = _install_successful_unit_apply(monkeypatch)
    register_extended_handlers(get_default_job_manager())

    with _client() as client:
        response = client.get("/api/v2/core/bypass")
        assert response.status_code == 200
        assert response.json()["data"]["bypass"]["enabled"] is False

        denied = client.post("/api/v2/core/bypass/enable", json={"confirm_apply": False})
        assert denied.status_code == 200
        assert denied.json()["ok"] is False
        assert denied.json()["error"]["code"] == "CORE_BYPASS_CONFIRMATION_REQUIRED"

        enabled = client.post(
            "/api/v2/core/bypass/enable",
            json={"confirm_apply": True, "requested_by": "pytest"},
        )
        assert enabled.status_code == 200
        assert enabled.json()["ok"] is True
        assert adapter.calls == ["check", "apply"]

        summary = client.get("/api/v2/system/summary").json()["data"]
        assert summary["core"]["bypass"]["enabled"] is True
        assert any(
            warning["code"] == "FWROUTER_CORE_BYPASS_ACTIVE"
            for warning in summary["warnings"]
        )


def test_watchdog_auto_check_pauses_when_core_bypass_is_active(
    monkeypatch,
    tmp_path: Path,
    isolated_host_observations,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    adapter = _install_successful_unit_apply(monkeypatch)
    register_extended_handlers(get_default_job_manager())
    set_module_desired_state("watchdog", "enabled", run_now=False)
    submit_core_bypass_job(
        action="enable",
        requested_by="pytest",
        run_now=True,
    )
    assert adapter.calls == ["check", "apply"]

    result = run_vpn_watchdog_auto_check()

    assert result["ok"] is True
    assert result["status"] == "paused_core_bypass"
    assert result["module"]["runtime_state"] == "paused"
