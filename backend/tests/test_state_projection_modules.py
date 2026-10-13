from __future__ import annotations

from _test_support import configure_test_state_dir as _configure_env
from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import db_session, initialize_database
from fwrouter_api.services.modules import fetch_modules
from fwrouter_api.services.watchdog_runtime_state import update_watchdog_runtime_state
from fwrouter_api.services.state_projection import build_module_state_projection


def test_module_projection_separates_legacy_disabled_from_live_projection(monkeypatch) -> None:
    monkeypatch.setattr(
        "fwrouter_api.services.state_projection._module_runtime_context",
        lambda: {},
    )
    with db_session() as connection:
        connection.execute(
            """
            UPDATE modules
            SET desired_state = 'disabled',
                runtime_state = 'running',
                apply_state = 'clean'
            WHERE module_name = 'core'
            """
        )

    modules = {
        item["entity"]["id"]: item
        for item in build_module_state_projection()["items"]
    }

    core = modules["core"]
    assert core["intent"]["state"] == "disabled"
    assert core["observation"]["state"] == "running"
    assert core["reconcile"]["state"] == "legacy_ambiguous"
    assert core["projection"]["state"] == "disabled"


def test_module_projection_reports_enabled_running_as_healthy(monkeypatch, tmp_path) -> None:
    _configure_env(monkeypatch, tmp_path)
    monkeypatch.setenv("FWROUTER_WATCHDOG_SCHEDULER_ENABLED", "true")
    get_settings.cache_clear()
    initialize_database()
    watchdog_runtime = update_watchdog_runtime_state(path_key="pytest-watchdog")
    assert watchdog_runtime["path_key"] == "pytest-watchdog"
    monkeypatch.setattr(
        "fwrouter_api.services.state_projection.build_runtime_enforcement_state",
        lambda: {"traffic_enforcement_guaranteed": False, "enforcement_level": "owned_table_missing"},
    )
    monkeypatch.setattr(
        "fwrouter_api.services.state_projection._safe_health",
        lambda _adapter: {"runtime_state": "stopped", "checked_at": None},
    )
    with db_session() as connection:
        connection.execute(
            """
            UPDATE modules
            SET desired_state = 'enabled',
                runtime_state = 'running',
                apply_state = 'clean',
                error_code = NULL
            WHERE module_name = 'watchdog'
            """
        )
    watchdog_module = next(
        module for module in fetch_modules() if module["module_name"] == "watchdog"
    )
    assert watchdog_module["desired_state"] == "enabled"
    assert watchdog_module["runtime_state"] == "running"
    assert watchdog_module["error_code"] is None

    modules = {
        item["entity"]["id"]: item
        for item in build_module_state_projection()["items"]
    }

    assert modules["watchdog"]["reconcile"]["state"] == "in_sync"
    assert modules["watchdog"]["projection"]["state"] == "healthy"
