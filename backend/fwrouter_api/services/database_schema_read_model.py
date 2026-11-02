from __future__ import annotations

import sqlite3
import stat
from typing import Any

from fwrouter_api.db.connection import get_db_path, inspect_existing_database_schema
from fwrouter_api.services.live_probe_cache import get_live_probe_cache


def _schema_cache_key() -> str:
    """Key the read projection to file replacement and SQLite DDL generation."""
    path = get_db_path()
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise sqlite3.OperationalError("FWROUTER_DATABASE_MISSING") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise sqlite3.OperationalError("FWROUTER_DATABASE_PATH_INVALID")
    connection = sqlite3.connect(
        f"{path.absolute().as_uri()}?mode=ro", uri=True, timeout=30.0,
    )
    try:
        schema_cookie = int(connection.execute("PRAGMA schema_version").fetchone()[0])
        row = connection.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()
        schema_version = str(row[0]) if row is not None else "missing"
    finally:
        connection.close()
    return (f"db.schema_state.{path.absolute()}.{info.st_dev}.{info.st_ino}."
            f"{info.st_ctime_ns}.{schema_cookie}.{schema_version}")


def get_cached_schema_state(*, ttl_seconds: float = 30.0) -> dict[str, Any]:
    """Cache the health/read projection outside the low-level DB package."""
    key = _schema_cache_key()
    return get_live_probe_cache(
        key,
        ttl_seconds=ttl_seconds,
        loader=inspect_existing_database_schema,
    )
