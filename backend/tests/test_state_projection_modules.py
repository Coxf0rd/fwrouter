from __future__ import annotations

from fwrouter_api.db.connection import db_session
from fwrouter_api.services.state_projection import build_module_state_projection


def test_module_projection_separates_legacy_disabled_from_live_projection() -> None:
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
    assert core["projection"]["state"] == "warning"


def test_module_projection_reports_enabled_running_as_healthy() -> None:
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

    modules = {
        item["entity"]["id"]: item
        for item in build_module_state_projection()["items"]
    }

    assert modules["watchdog"]["reconcile"]["state"] == "in_sync"
    assert modules["watchdog"]["projection"]["state"] == "healthy"
