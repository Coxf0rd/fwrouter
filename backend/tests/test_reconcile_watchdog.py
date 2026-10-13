from __future__ import annotations

from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import db_session
from fwrouter_api.services.reconcile import WatchdogReconciler


def _enable_watchdog_runtime_projection(monkeypatch) -> None:
    # These unit cases inspect the watchdog's active runtime state. Keep the
    # default-disabled scheduler override from masking their database fixture.
    monkeypatch.setenv("FWROUTER_WATCHDOG_SCHEDULER_ENABLED", "true")
    get_settings.cache_clear()


def test_watchdog_enabled_without_successful_check_is_stale(monkeypatch) -> None:
    _enable_watchdog_runtime_projection(monkeypatch)
    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO modules (
                module_name, desired_state, lifecycle_mode, runtime_state, apply_state
            )
            VALUES ('watchdog', 'enabled', 'managed', 'running', 'clean')
            ON CONFLICT(module_name) DO UPDATE SET
                desired_state = excluded.desired_state,
                lifecycle_mode = excluded.lifecycle_mode,
                runtime_state = excluded.runtime_state,
                apply_state = excluded.apply_state,
                error_code = NULL,
                error_message = NULL
            """
        )
        connection.execute("DELETE FROM watchdog_state WHERE id = 1")
    reconciler = WatchdogReconciler(projection_loader=lambda: {"watchdog": {}})

    result = reconciler.check()

    assert result.reconcile_state == "stale"
    assert result.reason == "last_successful_check_missing"


def test_watchdog_failed_module_is_failed(monkeypatch) -> None:
    _enable_watchdog_runtime_projection(monkeypatch)
    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO modules (
                module_name, desired_state, lifecycle_mode, runtime_state, apply_state, error_code
            )
            VALUES ('watchdog', 'enabled', 'managed', 'failed', 'failed', 'WATCHDOG_FAILED')
            ON CONFLICT(module_name) DO UPDATE SET
                desired_state = excluded.desired_state,
                lifecycle_mode = excluded.lifecycle_mode,
                runtime_state = excluded.runtime_state,
                apply_state = excluded.apply_state,
                error_code = excluded.error_code
            """
        )
    reconciler = WatchdogReconciler(projection_loader=lambda: {"watchdog": {}})

    result = reconciler.check()

    assert result.reconcile_state == "failed"
    assert result.reason == "WATCHDOG_FAILED"
