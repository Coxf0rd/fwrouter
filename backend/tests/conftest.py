from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

if any(name == "fwrouter_api" or name.startswith("fwrouter_api.") for name in sys.modules):
    raise RuntimeError("FWRouter application modules loaded before the test safety bootstrap")
sys.dont_write_bytecode = True

_BOOTSTRAP_SPEC = importlib.util.spec_from_file_location(
    "_fwrouter_test_isolation_bootstrap",
    Path(__file__).with_name("_isolation_bootstrap.py"),
)
assert _BOOTSTRAP_SPEC is not None and _BOOTSTRAP_SPEC.loader is not None
_ISOLATION_BOOTSTRAP = importlib.util.module_from_spec(_BOOTSTRAP_SPEC)
sys.modules[_BOOTSTRAP_SPEC.name] = _ISOLATION_BOOTSTRAP
_BOOTSTRAP_SPEC.loader.exec_module(_ISOLATION_BOOTSTRAP)
_TEST_RUN_ROOT, _TEST_STATE_ROOT = _ISOLATION_BOOTSTRAP.configure_test_process()

import pytest

from fwrouter_api.core.config import Settings, get_settings

# The Pydantic settings class otherwise resolves the deployed absolute dotenv
# file. Set this before importing adapters/facades that instantiate Settings.
Settings.model_config["env_file"] = None
_bootstrap_paths = Settings().paths
assert Settings.model_config.get("env_file") is None
assert _bootstrap_paths.state_dir.resolve() == _TEST_STATE_ROOT.resolve()
assert _bootstrap_paths.run_dir.resolve() == (_TEST_STATE_ROOT / "run").resolve()
assert not _ISOLATION_BOOTSTRAP.path_is_protected(_bootstrap_paths.db_path)
assert not _ISOLATION_BOOTSTRAP.path_is_protected(_bootstrap_paths.run_dir / "xray-writer.lock")

from fwrouter_api.adapters.dataplane import DataplaneOperation, DataplaneResult
from fwrouter_api.adapters.mihomo import MihomoApplyResult, MihomoHealth, MihomoRuntimeState
from fwrouter_api.adapters.xray import NoopXrayAdapter
from fwrouter_api.db.connection import initialize_database
from fwrouter_api.jobs.manager import get_default_job_manager
from fwrouter_api.services.live_probe_cache import clear_live_probe_cache


class _TestDataplaneAdapter:
    def check(self, plan):  # noqa: ANN001
        return self._result(plan, operation=DataplaneOperation.CHECK, stage="check")

    def apply(self, plan):  # noqa: ANN001
        return self._result(plan, operation=DataplaneOperation.APPLY, stage="apply")

    def rollback(self, plan):  # noqa: ANN001
        return self._result(plan, operation=DataplaneOperation.ROLLBACK, stage="rollback")

    @staticmethod
    def _result(plan, *, operation: DataplaneOperation, stage: str) -> DataplaneResult:  # noqa: ANN001
        details = {
            "stage": stage,
            "adapter": "pytest-dataplane",
            "owned_table": "inet fwrouter_v2",
            "table_exists": False,
            "required_chains": {
                "prerouting": False,
                "input": False,
                "output": False,
                "forward": False,
                "postrouting": False,
                "fwrouter_classify": False,
                "fwrouter_direct": False,
                "fwrouter_vpn": False,
            },
            "candidate_path": plan.generated_path,
            "manifest_path": plan.manifest_path,
            "artifact_paths": plan.artifact_paths,
            "dataplane_capability": "unavailable",
            "enforcement_level": "not_configured",
            "traffic_enforcement_guaranteed": False,
            "missing_runtime_requirements": ["pytest_live_dataplane_disabled"],
        }
        return DataplaneResult(
            ok=False,
            operation=operation,
            message=f"pytest isolated runtime did not perform dataplane {operation.value}",
            details=details,
        )


def _fake_mihomo_restart(
    *,
    action: str = "restart",
    heartbeat=None,  # noqa: ANN001
) -> dict[str, object]:
    if heartbeat is not None:
        heartbeat()
    return {
        "ok": False,
        "pytest_isolated": True,
        "compose_file": "/tmp/fwrouter-pytest/mihomo/docker-compose.yml",
        "service": "mihomo",
        "action": action,
        "restart": {"ok": False, "returncode": None},
        "status": {"ok": False, "returncode": None},
        "controller_wait": {"ok": False, "attempts": 0},
        "selector_restore": {"ok": False, "pytest_isolated": True},
        "error_code": "PYTEST_RUNTIME_DISABLED",
    }


def _install_no_live_runtime_guards(monkeypatch: pytest.MonkeyPatch) -> None:
    from fwrouter_api.adapters import mihomo as mihomo_adapter

    # The module singleton is created with production absolute config paths at
    # import time. Retarget its file paths and external runtime methods for the
    # ordinary unit-test process; adapter-specific tests instantiate their own
    # adapter and provide an explicit fake transport or temp paths.
    mihomo = mihomo_adapter.DEFAULT_MIHOMO_ADAPTER
    generated = get_settings().paths.generated_dir / "mihomo"
    monkeypatch.setattr(mihomo, "config_path", generated / "config.yaml")
    monkeypatch.setattr(mihomo, "contours_path", generated / "contours.json")
    monkeypatch.setattr(
        mihomo,
        "health",
        lambda: MihomoHealth(
            runtime_state=MihomoRuntimeState.NOT_CONFIGURED,
            message="pytest isolated runtime",
            details={"adapter": "pytest-isolated"},
        ),
    )
    monkeypatch.setattr(mihomo, "list_servers", lambda: [])
    monkeypatch.setattr(mihomo, "get_active_server_id", lambda: None)
    monkeypatch.setattr(mihomo, "check_port", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        mihomo,
        "apply_server_to_selector",
        lambda selector_name, server_id: MihomoApplyResult(
            ok=False,
            message="pytest isolated runtime does not apply selectors",
            error_code="PYTEST_RUNTIME_DISABLED",
        ),
    )

    xray_adapter = NoopXrayAdapter()
    monkeypatch.setattr("fwrouter_api.adapters.xray.DEFAULT_XRAY_ADAPTER", xray_adapter)
    monkeypatch.setattr("fwrouter_api.services.xray.DEFAULT_XRAY_ADAPTER", xray_adapter)
    monkeypatch.setattr("fwrouter_api.services.subject_inventory.DEFAULT_XRAY_ADAPTER", xray_adapter)
    monkeypatch.setattr("fwrouter_api.services.runtime.DEFAULT_XRAY_ADAPTER", xray_adapter)
    monkeypatch.setattr("fwrouter_api.services.xray_status.DEFAULT_XRAY_ADAPTER", xray_adapter)
    monkeypatch.setattr("fwrouter_api.services.xray_runtime_state.DEFAULT_XRAY_ADAPTER", xray_adapter)

    monkeypatch.setattr(
        "fwrouter_api.services.mihomo_runtime.restart_mihomo_container",
        _fake_mihomo_restart,
    )
    monkeypatch.setattr(
        "fwrouter_api.services.mihomo_config.restart_mihomo_container",
        _fake_mihomo_restart,
    )
    monkeypatch.setattr(
        "fwrouter_api.routes.mihomo.restart_mihomo_container",
        _fake_mihomo_restart,
    )


def pytest_configure(config: pytest.Config) -> None:
    configured_basetemp = Path(config.option.basetemp or (_TEST_RUN_ROOT / "pytest-tmp"))
    if not _ISOLATION_BOOTSTRAP.path_is_owned(configured_basetemp):
        raise pytest.UsageError("pytest basetemp must stay under the owned FWRouter test root")
    config.option.basetemp = str(configured_basetemp)
    config.addinivalue_line("markers", "live_dataplane: allow a test to touch live nftables")
    config.addinivalue_line("markers", "destructive: destructive test, disposable staging only")
    config.addinivalue_line("markers", "live: live system test, excluded from routine execution")
    config.addinivalue_line(
        "markers",
        "no_database_autoinit: allow a test to create its own SQLite schema",
    )


def _cleanup_pytest_artifacts() -> None:
    _ISOLATION_BOOTSTRAP.cleanup_owned_root()


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    blocked = [
        item.nodeid
        for item in items
        if item.get_closest_marker("live_dataplane")
        or item.get_closest_marker("live")
        or item.get_closest_marker("destructive")
    ]
    if blocked:
        raise pytest.UsageError(
            "live/destructive tests are denied; no verified disposable-staging attestation runner is installed: "
            + ", ".join(blocked)
        )


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    _cleanup_pytest_artifacts()


def pytest_unconfigure(config: pytest.Config) -> None:
    _cleanup_pytest_artifacts()


@pytest.fixture(autouse=True)
def isolate_fwrouter_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, request: pytest.FixtureRequest):
    if "live_dataplane" not in request.keywords:
        if not get_default_job_manager().wait_for_idle():
            pytest.fail("prior FWRouter test job manager did not become idle before isolation")
        monkeypatch.setenv("FWROUTER_STATE_DIR", str(tmp_path / "state"))
        monkeypatch.setenv("FWROUTER_ENVIRONMENT", "test")
        get_settings.cache_clear()
        clear_live_probe_cache()
        if "no_database_autoinit" not in request.keywords:
            initialize_database()

        _install_no_live_runtime_guards(monkeypatch)
        adapter = _TestDataplaneAdapter()
        monkeypatch.setattr("fwrouter_api.services.apply.DEFAULT_DATAPLANE_ADAPTER", adapter)
        monkeypatch.setattr("fwrouter_api.services.runtime.DEFAULT_DATAPLANE_ADAPTER", adapter)
        monkeypatch.setattr(
        "fwrouter_api.services.apply.probe_live_global_mode",
        lambda: {
                "ok": False,
                "mode": "unknown",
                "selective_default": None,
                "error_code": "PYTEST_RUNTIME_DISABLED",
                "error_message": "pytest isolated runtime does not inspect live dataplane state",
                "raw_chain": None,
                "pytest_isolated": True,
            },
        )

    yield

    if not get_default_job_manager().wait_for_idle():
        pytest.fail("FWRouter test job manager did not become idle during teardown")
    get_settings.cache_clear()
    clear_live_probe_cache()
