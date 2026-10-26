from __future__ import annotations

from contextvars import ContextVar
import sqlite3
import time
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from fwrouter_api.core.config import get_settings
from fwrouter_api.db.migrations import database_has_user_tables, run_missing_migrations
from fwrouter_api.db.schema_state import inspect_database_schema
from fwrouter_api.services.live_probe_cache import get_live_probe_cache


_READ_ONLY_DB_SESSIONS: ContextVar[bool] = ContextVar("fwrouter_read_only_db_sessions", default=False)
_OFFLINE_CANDIDATE_DB_PATH: ContextVar[Path | None] = ContextVar(
    "fwrouter_offline_candidate_database_path", default=None,
)


def get_db_path() -> Path:
    candidate = _OFFLINE_CANDIDATE_DB_PATH.get()
    if candidate is not None:
        from fwrouter_api.services.maintenance_lifecycle_admission import offline_rebuild_is_held

        if not offline_rebuild_is_held():
            raise RuntimeError("offline candidate database path outlived its maintenance lease")
        return candidate
    settings = get_settings()
    return settings.paths.db_path


@contextmanager
def offline_candidate_database_path(path: Path) -> Iterator[None]:
    """Bind canonical DB helpers to a private candidate during held rebuild.

    This process-local override does not change environment or global settings
    and is revoked on every exit path. The candidate directory must be an
    immediate, private child of the configured state root.
    """
    from fwrouter_api.services.maintenance_lifecycle_admission import offline_rebuild_is_held

    if not offline_rebuild_is_held():
        raise RuntimeError("offline candidate database requires held maintenance admission")
    from fwrouter_api.services.maintenance_lifecycle_admission import active_offline_rebuild_state_root

    state_root = Path(os.path.abspath(get_settings().paths.state_dir))
    admitted_state_root = Path(os.path.abspath(active_offline_rebuild_state_root()))
    if state_root != admitted_state_root:
        raise ValueError("offline candidate state root differs from held maintenance root")
    candidate = Path(os.path.abspath(path))
    parent = candidate.parent
    if parent.parent != state_root:
        raise ValueError("offline candidate database must be in a private state-root child")
    root_info = state_root.lstat()
    if (not stat.S_ISDIR(root_info.st_mode) or stat.S_ISLNK(root_info.st_mode)
            or root_info.st_uid != os.geteuid() or stat.S_IMODE(root_info.st_mode) & 0o077):
        raise ValueError("offline candidate state root is not private")
    parent_info = parent.lstat()
    if (not stat.S_ISDIR(parent_info.st_mode) or parent_info.st_uid != os.geteuid()
            or stat.S_IMODE(parent_info.st_mode) != 0o700):
        raise ValueError("offline candidate database directory is not private")
    for component in (parent, candidate):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            raise ValueError("offline candidate database path contains a symlink")
    if candidate.exists():
        info = candidate.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            raise ValueError("offline candidate database file is not owned and regular")
    token = _OFFLINE_CANDIDATE_DB_PATH.set(candidate)
    try:
        yield
    finally:
        _OFFLINE_CANDIDATE_DB_PATH.reset(token)


def get_schema_path() -> Path:
    return Path(__file__).with_name("schema.sql")


@contextmanager
def readonly_db_session_scope() -> Iterator[None]:
    """Make nested `connect()`/`db_session()` calls read-only in this context."""
    token = _READ_ONLY_DB_SESSIONS.set(True)
    try:
        yield
    finally:
        _READ_ONLY_DB_SESSIONS.reset(token)


def connect() -> sqlite3.Connection:
    db_path = get_db_path()
    if _READ_ONLY_DB_SESSIONS.get():
        connection = sqlite3.connect(
            f"{db_path.resolve().as_uri()}?mode=ro", uri=True, timeout=30.0,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON;")
        connection.execute("PRAGMA foreign_keys = ON;")
        connection.execute("PRAGMA busy_timeout = 30000;")
        connection.execute("PRAGMA temp_store = MEMORY;")
        return connection

    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.parent.chmod(0o700)
    if db_path.exists():
        db_path.chmod(0o600)
    else:
        try:
            descriptor = os.open(db_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            # Another process may have initialized the same fresh state root.
            db_path.chmod(0o600)
        else:
            os.close(descriptor)

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
    # SQLite may create these while enabling WAL. Restrict both an existing
    # database and any sidecars even when connect() is used outside systemd.
    db_path.chmod(0o600)
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{db_path}{suffix}")
        if sidecar.exists():
            sidecar.chmod(0o600)
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
        loader=inspect_existing_database_schema,
    )


def inspect_existing_database_schema() -> dict[str, Any]:
    """Inspect the current database without creating or modifying state.

    Initialization and migrations belong to the explicit startup/installer
    path. Health and runtime reads only report the schema they can observe.
    SQLite errors intentionally propagate so API callers retain their existing
    unavailable-database behavior.
    """

    db_path = get_db_path().resolve()
    connection = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True, timeout=30.0)
    connection.row_factory = sqlite3.Row
    try:
        return inspect_database_schema(connection)
    finally:
        connection.close()
