from __future__ import annotations

from fwrouter_api.db.connection import db_session
from fwrouter_api.services.reconcile import WatchdogReconciler


def test_watchdog_enabled_without_successful_check_is_stale() -> None:
    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO modules (
                module_name, desired_state, lifecycle_mode, runtime_state, apply_state
            )
            VALUES ('watchdog', 'enabled', 'managed', 'running', 'clean')
            ON CONFLICT(module_name) DO NOTHING
            """
        )
    reconciler = WatchdogReconciler(projection_loader=lambda: {"watchdog": {}})

    result = reconciler.check()

    assert result.reconcile_state == "stale"
    assert result.reason == "last_successful_check_missing"


def test_watchdog_failed_module_is_failed() -> None:
    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO modules (
                module_name, desired_state, lifecycle_mode, runtime_state, apply_state, error_code
            )
            VALUES ('watchdog', 'enabled', 'managed', 'failed', 'failed', 'WATCHDOG_FAILED')
            ON CONFLICT(module_name) DO UPDATE SET
                runtime_state = excluded.runtime_state,
                apply_state = excluded.apply_state,
                error_code = excluded.error_code
            """
        )
    reconciler = WatchdogReconciler(projection_loader=lambda: {"watchdog": {}})

    result = reconciler.check()

    assert result.reconcile_state == "failed"
    assert result.reason == "WATCHDOG_FAILED"
