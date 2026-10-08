"""Small shared fixtures for backend tests with identical setup contracts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def configure_test_state_dir(monkeypatch: Any, tmp_path: Path) -> None:
    """Point a test at its private state root and clear cached settings."""
    monkeypatch.setenv("FWROUTER_STATE_DIR", str(tmp_path / "state"))
    from fwrouter_api.core.config import get_settings

    get_settings.cache_clear()


def configure_test_state_dir_and_clear_live_probe_cache(monkeypatch: Any, tmp_path: Path) -> None:
    """Configure private test state and clear the process-local probe cache."""
    configure_test_state_dir(monkeypatch, tmp_path)
    from fwrouter_api.services.live_probe_cache import clear_live_probe_cache

    clear_live_probe_cache()


def configure_test_state_dir_without_schedulers(monkeypatch: Any, tmp_path: Path) -> None:
    """Use isolated state while disabling the three test-sensitive schedulers."""
    monkeypatch.setenv("FWROUTER_STATE_DIR", str(tmp_path / "state"))
    for name in (
        "FWROUTER_MAINTENANCE_SCHEDULER_ENABLED",
        "FWROUTER_WATCHDOG_SCHEDULER_ENABLED",
        "FWROUTER_RUNTIME_CONVERGENCE_SCHEDULER_ENABLED",
    ):
        monkeypatch.setenv(name, "false")
    from fwrouter_api.core.config import get_settings

    get_settings.cache_clear()


def database_table_snapshot() -> dict[str, list[dict[str, object]]]:
    """Capture every application table for read-only endpoint contracts."""
    from fwrouter_api.db.connection import db_session

    with db_session() as connection:
        names = [
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        ]
        return {
            table: sorted(
                (dict(row) for row in connection.execute(f'SELECT * FROM "{table}"').fetchall()),
                key=lambda row: json.dumps(row, ensure_ascii=False, sort_keys=True, default=str),
            )
            for table in names
        }


def database_table_diff(
    before: dict[str, list[dict[str, object]]],
    after: dict[str, list[dict[str, object]]],
) -> dict[str, dict[str, list[dict[str, object]]]]:
    """Return exact row additions/removals by table for assertion diagnostics."""
    changed: dict[str, dict[str, list[dict[str, object]]]] = {}
    for table in sorted(before.keys() | after.keys()):
        old_rows, new_rows = before.get(table, []), after.get(table, [])
        if old_rows == new_rows:
            continue
        old_index = {
            json.dumps(row, ensure_ascii=False, sort_keys=True, default=str): row
            for row in old_rows
        }
        new_index = {
            json.dumps(row, ensure_ascii=False, sort_keys=True, default=str): row
            for row in new_rows
        }
        changed[table] = {
            "removed": [old_index[key] for key in sorted(old_index.keys() - new_index.keys())],
            "added": [new_index[key] for key in sorted(new_index.keys() - old_index.keys())],
        }
    return changed
