from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from fwrouter_api.core.config import get_settings
from fwrouter_api.db.migrations import database_has_user_tables, run_missing_migrations
from fwrouter_api.db.schema_state import inspect_database_schema
from fwrouter_api.services.live_probe_cache import get_live_probe_cache


def get_db_path() -> Path:
    settings = get_settings()
    return settings.paths.db_path


def get_schema_path() -> Path:
    return Path(__file__).with_name("schema.sql")


def connect() -> sqlite3.Connection:
    db_path = get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(db_path, timeout=30.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON;")
    connection.execute("PRAGMA busy_timeout = 30000;")
    for attempt in range(6):
        try:
            connection.execute("PRAGMA journal_mode = WAL;")
            break
        except sqlite3.OperationalError as exc:
            if "database is locked" not in str(exc).lower() or attempt == 5:
                raise
            time.sleep(0.2)
    connection.execute("PRAGMA synchronous = NORMAL;")
    connection.execute("PRAGMA temp_store = MEMORY;")
    return connection


@contextmanager
def db_session() -> Iterator[sqlite3.Connection]:
    connection = connect()
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def initialize_database() -> dict[str, Any]:
    schema_path = get_schema_path()
    schema_sql = schema_path.read_text(encoding="utf-8")
    schema_state: dict[str, Any] | None = None

    with db_session() as connection:
        if database_has_user_tables(connection):
            run_missing_migrations(connection)
        connection.executescript(schema_sql)
        schema_state = inspect_database_schema(connection)

    return schema_state or {
        "ok": False,
        "status": "drift",
        "expected_schema_version": None,
        "actual_schema_version": None,
        "rebuild_required": True,
        "problems": [
            {
                "code": "DATABASE_SCHEMA_INSPECTION_FAILED",
            }
        ],
        "tables": {},
    }


def get_cached_schema_state(*, ttl_seconds: float = 30.0) -> dict[str, Any]:
    return get_live_probe_cache(
        "db.schema_state",
        ttl_seconds=ttl_seconds,
        loader=initialize_database,
    )
