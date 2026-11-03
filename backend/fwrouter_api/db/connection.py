from __future__ import annotations

from contextvars import ContextVar
from collections import OrderedDict
from hashlib import sha256
from functools import lru_cache
import json
import sqlite3
import time
import os
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from typing import Any

from fwrouter_api.core.config import get_settings
from fwrouter_api.db.schema_state import EXPECTED_SCHEMA_VERSION, inspect_database_schema


_READ_ONLY_DB_SESSIONS: ContextVar[bool] = ContextVar("fwrouter_read_only_db_sessions", default=False)
_OFFLINE_CANDIDATE_DB_PATH: ContextVar[Path | None] = ContextVar(
    "fwrouter_offline_candidate_database_path", default=None,
)
_SCHEMA_ADMISSION_CACHE_MAX = 32
_SCHEMA_ADMISSION_LOCK = RLock()
_SCHEMA_ADMISSION_CACHE: OrderedDict[tuple[str, str, str], bool] = OrderedDict()


@lru_cache(maxsize=1)
def _canonical_schema_identity() -> str:
    source = get_schema_path().read_bytes()
    return sha256(source + b"\0" + sqlite3.sqlite_version.encode("ascii")).hexdigest()


def _schema_content_key(connection: sqlite3.Connection) -> tuple[str, str, str]:
    metadata = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_meta'"
    ).fetchone()
    version_row = (connection.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    ).fetchone() if metadata is not None else None)
    version = str(version_row[0]) if version_row is not None else "<missing>"
    rows = connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master "
        "ORDER BY type, name, tbl_name, sql"
    ).fetchall()
    encoded = json.dumps(
        [tuple(row) for row in rows], ensure_ascii=True, separators=(",", ":"),
    ).encode("utf-8")
    return _canonical_schema_identity(), version, sha256(encoded).hexdigest()


def _schema_admission_cached(key: tuple[str, str, str]) -> bool:
    with _SCHEMA_ADMISSION_LOCK:
        admitted = _SCHEMA_ADMISSION_CACHE.get(key)
        if admitted is not None:
            _SCHEMA_ADMISSION_CACHE.move_to_end(key)
            return admitted
    return False


def _remember_schema_admission(key: tuple[str, str, str]) -> None:
    with _SCHEMA_ADMISSION_LOCK:
        _SCHEMA_ADMISSION_CACHE[key] = True
        _SCHEMA_ADMISSION_CACHE.move_to_end(key)
        while len(_SCHEMA_ADMISSION_CACHE) > _SCHEMA_ADMISSION_CACHE_MAX:
            _SCHEMA_ADMISSION_CACHE.popitem(last=False)


def _clear_schema_admission_cache() -> None:
    """Clear process-local positive admission keys (used by isolated tests)."""
    with _SCHEMA_ADMISSION_LOCK:
        _SCHEMA_ADMISSION_CACHE.clear()


def _schema_admitted_on_connection(connection: sqlite3.Connection) -> bool:
    key = _schema_content_key(connection)
    if key[1] != EXPECTED_SCHEMA_VERSION:
        return False
    if _schema_admission_cached(key):
        return True
    state = inspect_database_schema(connection)
    if not state["ok"]:
        return False
    _remember_schema_admission(key)
    return True


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
    try:
        info = db_path.lstat()
    except FileNotFoundError as exc:
        raise sqlite3.OperationalError("FWROUTER_DATABASE_MISSING") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise sqlite3.OperationalError("FWROUTER_DATABASE_PATH_INVALID")

    readonly = _READ_ONLY_DB_SESSIONS.get()
    mode = "ro" if readonly else "rw"
    connection = sqlite3.connect(
        f"{db_path.absolute().as_uri()}?mode={mode}", uri=True, timeout=30.0,
    )
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON;")
        connection.execute("PRAGMA busy_timeout = 30000;")
        connection.execute("PRAGMA query_only = ON;")
        connection.execute("BEGIN")
        if not _schema_admitted_on_connection(connection):
            raise sqlite3.DatabaseError("FWROUTER_DATABASE_SCHEMA_UNSUPPORTED")
        connection.commit()
        if readonly:
            connection.execute("PRAGMA temp_store = MEMORY;")
            return connection

        # `mode=rw` above cannot create a missing file. Only a verified current
        # schema reaches permission hardening and WAL setup.
        connection.execute("PRAGMA query_only = OFF;")
        db_path.parent.chmod(0o700)
        db_path.chmod(0o600)
        connection.execute("PRAGMA synchronous = NORMAL;")
        connection.execute("PRAGMA temp_store = MEMORY;")
        for attempt in range(6):
            try:
                connection.execute("PRAGMA journal_mode = WAL;")
                break
            except sqlite3.OperationalError as exc:
                if "database is locked" not in str(exc).lower() or attempt == 5:
                    raise
                time.sleep(0.2)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{db_path}{suffix}")
            if sidecar.exists():
                sidecar.chmod(0o600)
        return connection
    except Exception:
        connection.close()
        raise


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


def _unavailable_schema_state(code: str) -> dict[str, Any]:
    return {
        "ok": False,
        "status": "drift",
        "expected_schema_version": "27",
        "actual_schema_version": None,
        "rebuild_required": True,
        "problems": [{"code": code}],
        "tables": {},
    }


@contextmanager
def existing_database_readonly_connection(
    db_path: Path | None = None,
) -> Iterator[sqlite3.Connection]:
    """Open an existing SQLite file for raw observation without admission or writes."""
    path = Path(os.path.abspath(db_path if db_path is not None else get_db_path()))
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise sqlite3.OperationalError("FWROUTER_DATABASE_MISSING") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise sqlite3.OperationalError("FWROUTER_DATABASE_PATH_INVALID")
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=30.0)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only = ON;")
        yield connection
    finally:
        connection.close()


def _inspect_existing_database_for_initialization(db_path: Path) -> dict[str, Any]:
    """Read-only admission for an existing DB, including data integrity checks."""
    db_path = Path(os.path.abspath(db_path))
    try:
        info = db_path.lstat()
    except FileNotFoundError:
        return _unavailable_schema_state("FWROUTER_DATABASE_MISSING")
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        return _unavailable_schema_state("FWROUTER_DATABASE_PATH_INVALID")

    try:
        with existing_database_readonly_connection(db_path) as connection:
            state = inspect_database_schema(connection)
            if not state["ok"]:
                return state
            integrity = [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]
            foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
            if integrity != ["ok"] or foreign_keys:
                problems = list(state["problems"])
                if integrity != ["ok"]:
                    problems.append({"code": "DATABASE_INTEGRITY_CHECK_FAILED"})
                if foreign_keys:
                    problems.append({"code": "DATABASE_FOREIGN_KEY_CHECK_FAILED",
                                     "violation_count": len(foreign_keys)})
                return {**state, "ok": False, "status": "drift", "rebuild_required": True,
                        "problems": problems}
            return state
    except sqlite3.Error:
        return _unavailable_schema_state("DATABASE_SCHEMA_INSPECTION_FAILED")


def validate_existing_database_for_initialization() -> dict[str, Any]:
    """Read-only full admission for an existing database; never creates one."""
    return _inspect_existing_database_for_initialization(get_db_path())


def _create_fresh_database(db_path: Path) -> dict[str, Any]:
    """Build schema 27 in a private sibling and publish it atomically, no-replace."""
    db_path = Path(os.path.abspath(db_path))
    db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    parent_info = db_path.parent.lstat()
    if (stat.S_ISLNK(parent_info.st_mode) or not stat.S_ISDIR(parent_info.st_mode)
            or parent_info.st_uid != os.geteuid()):
        raise sqlite3.OperationalError("FWROUTER_DATABASE_DIRECTORY_INVALID")
    db_path.parent.chmod(0o700)
    descriptor, candidate_name = tempfile.mkstemp(
        prefix=f".{db_path.name}.fresh-", suffix=".tmp", dir=db_path.parent,
    )
    os.close(descriptor)
    candidate = Path(candidate_name)
    published = False
    try:
        connection = sqlite3.connect(
            f"{candidate.absolute().as_uri()}?mode=rw", uri=True, timeout=30.0,
        )
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON;")
            connection.execute("PRAGMA busy_timeout = 30000;")
            schema_sql = get_schema_path().read_text(encoding="utf-8")
            try:
                connection.executescript("BEGIN IMMEDIATE;\n" + schema_sql + "\nCOMMIT;")
            except Exception:
                connection.rollback()
                raise
            state = inspect_database_schema(connection, allow_legacy_schema27_profiles=False)
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
            foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
            if not state["ok"] or integrity is None or integrity[0] != "ok" or foreign_keys:
                raise sqlite3.DatabaseError("FWROUTER_FRESH_DATABASE_VALIDATION_FAILED")
        finally:
            connection.close()

        candidate.chmod(0o600)
        with candidate.open("rb") as stream:
            os.fsync(stream.fileno())
        for suffix in ("-journal", "-wal", "-shm"):
            if Path(f"{candidate}{suffix}").exists():
                raise sqlite3.DatabaseError("FWROUTER_FRESH_DATABASE_SIDECAR_REMAINS")
        try:
            os.link(candidate, db_path, follow_symlinks=False)
            published = True
        except FileExistsError:
            # Another explicit initializer won the no-replace race. Inspect its
            # published structure and data integrity without replaying seeds or
            # replacing any data.
            return _inspect_existing_database_for_initialization(db_path)
        candidate.unlink()
        directory_fd = os.open(db_path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return state
    finally:
        # Only remove the private sibling created here. Never remove the final
        # path on error, including after successful hard-link publication.
        candidate.unlink(missing_ok=True)
        if not published:
            for suffix in ("-journal", "-wal", "-shm"):
                Path(f"{candidate}{suffix}").unlink(missing_ok=True)


def initialize_database() -> dict[str, Any]:
    """Create schema 27 only when absent; never migrate, seed or repair a file."""
    db_path = get_db_path()
    try:
        db_path.lstat()
    except FileNotFoundError:
        try:
            return _create_fresh_database(db_path)
        except (OSError, sqlite3.Error) as exc:
            code = str(exc) if str(exc).startswith("FWROUTER_") else "DATABASE_INITIALIZATION_FAILED"
            return _unavailable_schema_state(code)
    return _inspect_existing_database_for_initialization(db_path)


def inspect_existing_database_schema() -> dict[str, Any]:
    """Inspect the current database without creating or modifying state.

    Initialization and migrations belong to the explicit startup/installer
    path. Health and runtime reads only report the schema they can observe.
    SQLite errors intentionally propagate so API callers retain their existing
    unavailable-database behavior.
    """

    with existing_database_readonly_connection() as connection:
        return inspect_database_schema(connection)
