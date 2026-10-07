#!/usr/bin/env python3
"""Replay 28 selection revision fences against a disposable SQLite WAL database."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path


REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "backend"))

from fwrouter_api.services.vpn_auto_selection_state import advance_selection_revision  # noqa: E402


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="fwrouter-revision-replay-") as temp_dir:
        database = Path(temp_dir) / "isolated.db"
        connection = sqlite3.connect(database)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute(
            "CREATE TABLE settings (key TEXT PRIMARY KEY, value_json TEXT, updated_at TEXT)"
        )
        connection.commit()

        counts = {"begin_immediate": 0, "settings_upserts": 0}

        def count_statements(statement: str) -> None:
            normalized = statement.lstrip().upper()
            if normalized.startswith("BEGIN IMMEDIATE"):
                counts["begin_immediate"] += 1
            if normalized.startswith("INSERT INTO SETTINGS"):
                counts["settings_upserts"] += 1

        connection.set_trace_callback(count_statements)
        started = time.perf_counter()
        for _ in range(28):
            advance_selection_revision(connection)
            connection.commit()
        elapsed_ms = (time.perf_counter() - started) * 1000

        revision = connection.execute(
            "SELECT value_json FROM settings WHERE key='routing.auto_selection_revision'"
        ).fetchone()[0]
        wal = Path(f"{database}-wal")
        result = {
            "increments": 28,
            "final_revision": int(revision),
            "begin_immediate": counts["begin_immediate"],
            "settings_upserts": counts["settings_upserts"],
            "commits": 28,
            "elapsed_ms": round(elapsed_ms, 3),
            "database_bytes": os.stat(database).st_size,
            "uncheckpointed_wal_bytes": os.stat(wal).st_size if wal.exists() else 0,
            "page_size": connection.execute("PRAGMA page_size").fetchone()[0],
            "provider_calls": 0,
            "runtime_calls": 0,
            "scope": "disposable temporary SQLite database; no production path opened",
        }
        connection.close()
        print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
