from __future__ import annotations

from fwrouter_api.db.connection import db_session
from fwrouter_api.services.state_projection import build_watchdog_state_projection


def test_watchdog_projection_reports_degraded_as_runtime_drift() -> None:
    with db_session() as connection:
        connection.execute(
            """
            UPDATE modules
            SET desired_state = 'enabled',
                runtime_state = 'degraded',
                apply_state = 'clean',
                error_code = 'WATCHDOG_SIGNAL_UNAVAILABLE'
            WHERE module_name = 'watchdog'
            """
        )

    watchdog = build_watchdog_state_projection()["watchdog"]

    assert watchdog["intent"]["state"] == "enabled"
    assert watchdog["reconcile"]["state"] == "runtime_drift"
    assert watchdog["projection"]["state"] == "error"


def test_watchdog_projection_reports_disabled_as_not_applicable() -> None:
    with db_session() as connection:
        connection.execute(
            """
            UPDATE modules
            SET desired_state = 'disabled',
                runtime_state = 'paused',
                apply_state = 'clean',
                error_code = NULL
            WHERE module_name = 'watchdog'
            """
        )

    watchdog = build_watchdog_state_projection()["watchdog"]

    assert watchdog["reconcile"]["state"] == "not_applicable"
    assert watchdog["projection"]["state"] == "disabled"
