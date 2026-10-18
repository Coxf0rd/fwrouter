from __future__ import annotations
from _test_support import configure_test_state_dir as _configure_env
from fwrouter_api.db.connection import db_session, initialize_database


import hashlib
import json
import threading
from pathlib import Path

from fastapi.testclient import TestClient

from fwrouter_api.core.config import get_settings
import fwrouter_api.services.apply as apply_service
from fwrouter_api.services import dataplane_status as dataplane_status_service
from fwrouter_api.services import core_bypass as core_bypass_service
import fwrouter_api.services.routing_manifest as routing_manifest_service
import fwrouter_api.services.subject_policy as subject_policy_service
from fwrouter_api.adapters.dataplane import DataplaneOperation, DataplaneResult
from fwrouter_api.jobs.extended_handlers import register_extended_handlers
from fwrouter_api.jobs.manager import get_default_job_manager
from fwrouter_api.main import create_app
from fwrouter_api.services.core_bypass import get_core_bypass_state, submit_core_bypass_job
from fwrouter_api.services.modules import get_module_state, set_module_desired_state
from fwrouter_api.services.servers import ensure_routing_global_state
from fwrouter_api.services.runtime import get_runtime_summary
from fwrouter_api.services.system_summary import build_system_summary
from fwrouter_api.services.watchdog import run_vpn_watchdog_auto_check




def _client() -> TestClient:
    return TestClient(create_app(enable_startup_tasks=False))


def _core_bypass_job_failure_context(job: dict) -> dict[str, str | None]:
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    apply_result = result.get("apply") if isinstance(result.get("apply"), dict) else {}
    dataplane = apply_result.get("dataplane") if isinstance(apply_result.get("dataplane"), dict) else {}

    def _bounded(value) -> str | None:  # noqa: ANN001
        return str(value)[:240] if value is not None else None

    return {
        "status": _bounded(job.get("status")),
        "error_code": _bounded(job.get("error_code")),
        "error_message": _bounded(job.get("error_message")),
        "result_stage": _bounded(result.get("stage") or apply_result.get("stage")),
        "dataplane_error_code": _bounded(dataplane.get("error_code")),
        "dataplane_error_message": _bounded(dataplane.get("error_message") or dataplane.get("message")),
    }


def _stub_core_native_proof(monkeypatch) -> None:
    """Keep local bypass tests synthetic; never inspect this host's runtime context."""
    runtime_context = {
        "boot_id": "00000000-0000-0000-0000-000000000001",
        "network_namespace_inode": "123",
    }
    ensure_routing_global_state()
    with db_session() as connection:
        connection.execute(
            "UPDATE routing_global_state SET desired_mode='direct', applied_mode='direct', "
            "apply_state='clean' WHERE id=1",
        )
    monkeypatch.setattr(
        "fwrouter_api.services.core_intent.read_core_runtime_context", lambda: runtime_context,
    )

    def applied_manifest() -> dict:
        with db_session() as connection:
            apply = connection.execute(
                "SELECT apply_id FROM apply_versions ORDER BY created_at DESC, apply_id DESC LIMIT 1",
            ).fetchone()
            routing = connection.execute(
                "SELECT desired_mode, applied_mode, selective_default, server_mode, "
                "desired_fixed_server_id, applied_fixed_server_id, fixed_server_until, "
                "active_auto_server_id FROM routing_global_state WHERE id=1",
            ).fetchone()
        return {
            "plan_id": apply["apply_id"] if apply is not None else "",
            "routing_global_state": dict(routing) if routing is not None else {},
        }

    def manifest_hash(manifest: dict) -> str:
        return hashlib.sha256(
            json.dumps(manifest, sort_keys=True, separators=(",", ":"), default=str).encode(),
        ).hexdigest()

    monkeypatch.setattr(
        "fwrouter_api.services.dataplane_global._read_applied_manifest_uncached", applied_manifest,
    )

    def collect_proof() -> dict:
        manifest = applied_manifest()
        mode = str(manifest["routing_global_state"]["applied_mode"])
        return {
            "ok": True,
            "apply_id": manifest["plan_id"],
            "manifest_hash": manifest_hash(manifest),
            "traffic_enforcement_guaranteed": True,
            "active_mode_matches_intent": True,
            "enforcement_level": {
                "direct": "global_direct_enforced",
                "selective": "global_selective_enforced",
                "vpn": "global_vpn_enforced",
            }[mode],
            "live_global_mode": mode,
            "runtime_context": runtime_context,
        }

    monkeypatch.setattr(
        "fwrouter_api.services.xray_subscription_service._collect_legacy_admission_core_native_proof",
        collect_proof,
    )


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
    """Successful unit apply with an explicit, test-owned mode observation."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.observed_mode = "unknown"
        self.mode_before_apply = "unknown"
        self.readbacks: list[str] = []
        self.observed_selective_default: str | None = None
        self.selective_default_before_apply: str | None = None
        self.observed_bypass = False
        self.bypass_before_apply = False

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
        try:
            manifest = json.loads(Path(plan.manifest_path or "").read_text(encoding="utf-8"))
        except (OSError, TypeError, json.JSONDecodeError):
            manifest = None
        extra = manifest.get("extra") if isinstance(manifest, dict) else None
        core_bypass = extra.get("core_bypass") if isinstance(extra, dict) else None
        routing = manifest.get("routing_global_state") if isinstance(manifest, dict) else None
        if not isinstance(core_bypass, dict) or not isinstance(core_bypass.get("enabled"), bool):
            return DataplaneResult(
                ok=False,
                operation=DataplaneOperation.APPLY,
                message="unit adapter requires explicit core bypass manifest state",
                details={"stage": "manifest_readback"},
                error_code="PYTEST_CORE_BYPASS_MANIFEST_MISSING",
                error_message="generated apply manifest has no explicit core bypass state",
            )
        if not isinstance(routing, dict):
            return DataplaneResult(
                ok=False,
                operation=DataplaneOperation.APPLY,
                message="unit adapter requires explicit routing mode in apply manifest",
                details={"stage": "manifest_readback"},
                error_code="PYTEST_ROUTING_MODE_MISSING",
                error_message="generated apply manifest has no routing mode",
            )
        target_mode = str(routing.get("desired_mode") or routing.get("applied_mode") or "direct").lower()
        target_selective_default = (
            str(routing.get("selective_default") or "direct").lower()
            if target_mode == "selective"
            else None
        )
        result = DataplaneResult(
            ok=True,
            operation=DataplaneOperation.APPLY,
            message="unit-owned-table apply ok",
            details={
                "stage": "verify",
                "owned_table": "inet fwrouter_v2",
                "table_exists": True,
                "routing_mode": target_mode,
            },
        )
        if result.ok:
            self.mode_before_apply = self.observed_mode
            self.selective_default_before_apply = self.observed_selective_default
            self.bypass_before_apply = self.observed_bypass
            self.observed_mode = target_mode
            self.observed_selective_default = target_selective_default
            self.observed_bypass = core_bypass["enabled"]
        return result

    def rollback(self, plan):  # noqa: ANN001
        self.calls.append("rollback")
        result = DataplaneResult(
            ok=True,
            operation=DataplaneOperation.ROLLBACK,
            message="unit-owned-table rollback ok",
            details={"stage": "rollback"},
        )
        if result.ok:
            self.observed_mode = self.mode_before_apply
            self.observed_selective_default = self.selective_default_before_apply
            self.observed_bypass = self.bypass_before_apply
        return result

    def probe_live_mode(self) -> dict[str, object]:
        self.readbacks.append(self.observed_mode)
        return {
            "ok": self.observed_mode == "direct",
            "mode": self.observed_mode,
            "selective_default": self.observed_selective_default,
            "error_code": None if self.observed_mode == "direct" else "PYTEST_MODE_NOT_APPLIED",
            "error_message": None if self.observed_mode == "direct" else "unit adapter has not applied direct mode",
            "raw_chain": None,
            "pytest_unit_owned": True,
        }


def _install_successful_unit_apply(monkeypatch) -> _SuccessfulUnitApplyAdapter:
    adapter = _SuccessfulUnitApplyAdapter()
    monkeypatch.setattr(core_bypass_service, "_live_core_bypass_active", lambda: adapter.observed_bypass)
    monkeypatch.setattr(apply_service, "DEFAULT_DATAPLANE_ADAPTER", adapter)
    monkeypatch.setattr(apply_service, "probe_live_global_mode", adapter.probe_live_mode)
    monkeypatch.setattr(dataplane_status_service, "_read_live_dataplane_payload", lambda: None)
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
    _stub_core_native_proof(monkeypatch)
    adapter = _install_successful_unit_apply(monkeypatch)
    register_extended_handlers(get_default_job_manager())
    set_module_desired_state("watchdog", "enabled", run_now=False)
    enable_job = submit_core_bypass_job(
        action="enable",
        requested_by="pytest",
        run_now=True,
    )

    assert enable_job["status"] == "success", enable_job
    assert adapter.calls == ["check", "apply"]
    assert adapter.observed_bypass is True
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

    assert disable_job["status"] == "success", _core_bypass_job_failure_context(disable_job)
    assert adapter.calls == ["check", "apply", "check", "apply"]
    assert adapter.readbacks == ["direct", "direct"]
    assert adapter.observed_bypass is False
    assert get_core_bypass_state()["enabled"] is False
    assert get_module_state("core")["runtime_state"] == "running"
    assert get_module_state("watchdog")["desired_state"] == "enabled"
    assert get_module_state("watchdog")["runtime_state"] == "paused"


def test_bypass_restore_serializes_dependent_intent_and_does_not_resurrect_snapshot(
    monkeypatch, tmp_path: Path, isolated_host_observations,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _stub_core_native_proof(monkeypatch)
    adapter = _install_successful_unit_apply(monkeypatch)
    register_extended_handlers(get_default_job_manager())
    set_module_desired_state("watchdog", "enabled", requested_by="pytest", run_now=False)
    enabled = submit_core_bypass_job(action="enable", requested_by="pytest", run_now=True)
    assert enabled["status"] == "success", _core_bypass_job_failure_context(enabled)

    native_started = threading.Event()
    release_native = threading.Event()
    dependent_write_finished = threading.Event()
    thread_errors: list[BaseException] = []
    original_apply = adapter.apply
    apply_count = 0

    def blocking_apply(plan):  # noqa: ANN001
        nonlocal apply_count
        apply_count += 1
        result = original_apply(plan)
        if apply_count == 1:
            native_started.set()
            assert release_native.wait(timeout=5), "test did not release bypass apply"
        return result

    monkeypatch.setattr(adapter, "apply", blocking_apply)
    disable_results: list[dict] = []

    def disable_bypass() -> None:
        try:
            disable_results.append(submit_core_bypass_job(
                action="disable", requested_by="pytest", run_now=True,
            ))
        except BaseException as exc:  # surfaced in the owning test thread
            thread_errors.append(exc)

    def disable_watchdog() -> None:
        try:
            set_module_desired_state("watchdog", "disabled", requested_by="concurrent-api", run_now=False)
        except BaseException as exc:  # surfaced in the owning test thread
            thread_errors.append(exc)
        finally:
            dependent_write_finished.set()

    bypass_thread = threading.Thread(target=disable_bypass)
    bypass_thread.start()
    assert native_started.wait(timeout=5), "bypass did not reach native apply"
    writer_thread = threading.Thread(target=disable_watchdog)
    writer_thread.start()
    assert not dependent_write_finished.wait(timeout=0.1), "dependent intent crossed bypass apply"
    release_native.set()
    bypass_thread.join(timeout=10)
    writer_thread.join(timeout=10)

    assert not bypass_thread.is_alive() and not writer_thread.is_alive()
    assert not thread_errors, thread_errors
    assert len(disable_results) == 1 and disable_results[0]["status"] == "success"
    watchdog = get_module_state("watchdog")
    assert watchdog["desired_state"] == "disabled"
    assert watchdog["runtime_state"] == "stopped"


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
    assert adapter.observed_bypass is True

    monkeypatch.setattr(apply_service, "DEFAULT_DATAPLANE_ADAPTER", _FailingApplyAdapter())

    disable_job = submit_core_bypass_job(
        action="disable",
        requested_by="pytest",
        run_now=True,
    )

    assert disable_job["status"] == "failed", _core_bypass_job_failure_context(disable_job)
    assert get_core_bypass_state()["enabled"] is True
    assert adapter.observed_bypass is True
    core_module = get_module_state("core")
    assert core_module["apply_state"] == "failed", {
        "job": _core_bypass_job_failure_context(disable_job),
        "core": {
            key: str(core_module.get(key))[:240]
            for key in ("runtime_state", "apply_state", "error_code", "error_message")
        },
    }
    assert get_module_state("core")["runtime_state"] == "paused"


def test_core_bypass_completion_cannot_resurrect_newer_module_intent(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    ensure_routing_global_state()
    set_module_desired_state("core", "enabled", run_now=False)
    monkeypatch.setattr(core_bypass_service, "_live_core_bypass_active", lambda: True)
    monkeypatch.setattr(
        "fwrouter_api.services.core_intent.read_core_runtime_context",
        lambda: {"boot_id": "00000000-0000-0000-0000-000000000001", "network_namespace_inode": "123"},
    )
    with db_session() as connection:
        connection.execute(
            "INSERT INTO settings(key, value_json) VALUES('core.bypass', '{\"enabled\":true}') "
            "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json",
        )

    def stale_apply(**kwargs) -> dict:
        with db_session() as connection:
            connection.execute(
                "INSERT INTO apply_versions(apply_id, manifest_path, status, created_at, core_intent_revision) "
                "VALUES('stale-bypass-apply', '/synthetic/stale-bypass.json', 'applied', "
                "datetime('now', '+1 day'), ?)",
                (kwargs["core_intent_revision"],),
            )
        set_module_desired_state("core", "disabled", requested_by="concurrent-api", run_now=False)
        return {
            "ok": True, "apply_id": "stale-bypass-apply",
            "dataplane": {"message": "applied", "error_code": None, "error_message": None},
        }

    monkeypatch.setattr(core_bypass_service, "_run_core_bypass_apply", stale_apply)
    result = core_bypass_service.disable_core_bypass(job_id="stale-bypass-job", requested_by="pytest")

    assert result["job_status"] == "failed"
    assert result["error_code"] == "CORE_INTENT_STALE"
    assert get_core_bypass_state()["enabled"] is True
    assert get_module_state("core")["desired_state"] == "disabled"


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
