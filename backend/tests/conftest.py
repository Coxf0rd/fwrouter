from __future__ import annotations

import atexit
import os
import shutil
from pathlib import Path

import pytest

from fwrouter_api.adapters.dataplane import DataplaneOperation, DataplaneResult
from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import initialize_database
from fwrouter_api.services.live_probe_cache import clear_live_probe_cache


_PROJECT_ROOT = Path(__file__).resolve().parents[1]


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
            "candidate_path": plan.generated_path,
            "manifest_path": plan.manifest_path,
            "artifact_paths": plan.artifact_paths,
            "dataplane_capability": "nft_owned_table",
            "enforcement_level": "owned_table_ready",
            "traffic_enforcement_guaranteed": True,
            "missing_runtime_requirements": [],
        }
        return DataplaneResult(
            ok=True,
            operation=operation,
            message=f"pytest dataplane {operation.value} ok",
            details=details,
        )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "live_dataplane: allow a test to touch live nftables")
    config.addinivalue_line(
        "markers",
        "no_database_autoinit: allow a test to create its own SQLite schema",
    )


def _cleanup_path(path: Path) -> None:
    try:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        elif path.exists():
            path.unlink()
    except OSError:
        return


def _cleanup_pytest_artifacts() -> None:
    if os.environ.get("FWROUTER_PYTEST_KEEP_ARTIFACTS") == "1":
        return

    _cleanup_path(_PROJECT_ROOT / ".pytest_cache")
    _cleanup_path(Path("/tmp/fwrouter-pytest-cache"))
    _cleanup_path(Path("/tmp/fwrouter-pytest-tmp"))

    for pycache in _PROJECT_ROOT.rglob("__pycache__"):
        _cleanup_path(pycache)


atexit.register(_cleanup_pytest_artifacts)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    _cleanup_pytest_artifacts()


def pytest_unconfigure(config: pytest.Config) -> None:
    _cleanup_pytest_artifacts()


@pytest.fixture(autouse=True)
def isolate_fwrouter_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, request: pytest.FixtureRequest):
    if "live_dataplane" not in request.keywords:
        monkeypatch.setenv("FWROUTER_STATE_DIR", str(tmp_path / "state"))
        monkeypatch.setenv("FWROUTER_ENVIRONMENT", "test")
        get_settings.cache_clear()
        clear_live_probe_cache()
        if "no_database_autoinit" not in request.keywords:
            initialize_database()

        adapter = _TestDataplaneAdapter()
        monkeypatch.setattr("fwrouter_api.services.apply.DEFAULT_DATAPLANE_ADAPTER", adapter)
        monkeypatch.setattr("fwrouter_api.services.runtime.DEFAULT_DATAPLANE_ADAPTER", adapter)
        monkeypatch.setattr(
            "fwrouter_api.services.apply.probe_live_global_mode",
            lambda: {
                "ok": True,
                "mode": "direct",
                "selective_default": "direct",
                "error_code": None,
                "error_message": None,
                "raw_chain": None,
                "pytest_isolated": True,
            },
        )

    yield

    get_settings.cache_clear()
    clear_live_probe_cache()
