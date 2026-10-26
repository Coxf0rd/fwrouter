from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
from typing import Any
import uuid

from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import (
    connect,
    get_db_path,
    initialize_database,
    offline_candidate_database_path,
)
from fwrouter_api.db.schema_state import inspect_database_schema, summarize_schema_state
from fwrouter_api.services.control_plane_transfer import (
    import_control_plane_snapshot,
    resolve_control_plane_snapshot_source,
)
from fwrouter_api.services.logs import write_operational_log
from fwrouter_api.services.external_connections_registry import list_external_connections
from fwrouter_api.services.system_subjects import ensure_builtin_system_subjects
from fwrouter_api.services.subject_inventory import sync_subject_inventory
from fwrouter_api.adapters.xray_common import xray_writer_guard
from fwrouter_api.services.vpn_auto_selection_state import advance_selection_revision, selection_pool_signature
from fwrouter_api.services.database_backup import create_sqlite_backup
from fwrouter_api.services.control_plane_import_files import RECEIPT_PREFIX


class _OfflineMaintenanceContextUnqualified(RuntimeError):
    pass


def _require_qualified_offline_maintenance_context() -> None:
    """Require the controlled lifecycle's live exclusive maintenance lease."""
    from fwrouter_api.services.maintenance_lifecycle_admission import offline_rebuild_is_held

    if not offline_rebuild_is_held():
        raise _OfflineMaintenanceContextUnqualified


def _offline_phase(phase: str) -> None:
    from fwrouter_api.services.maintenance_lifecycle_admission import record_offline_rebuild_phase

    record_offline_rebuild_phase(phase)


def _read_import_receipts(path: Path) -> list[tuple[str, str, str | None]]:
    """Read only well-formed, locally-owned replay receipts from a trusted DB."""
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600):
        raise ValueError("DATABASE_REBUILD_SOURCE_DATABASE_UNSAFE")
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        tables = {str(row[0]) for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "settings" not in tables:
            return []
        rows = connection.execute(
            "SELECT key, value_json, updated_at FROM settings WHERE key GLOB ? ORDER BY key",
            (RECEIPT_PREFIX + "*",),
        ).fetchall()
        result: list[tuple[str, str, str | None]] = []
        for row in rows:
            key = str(row["key"])
            operation_id = key.removeprefix(RECEIPT_PREFIX)
            if not re.fullmatch(r"(?:[0-9a-f]{32}|[0-9a-f]{64})", operation_id):
                raise ValueError("DATABASE_REBUILD_IMPORT_RECEIPT_INVALID")
            try:
                value = json.loads(str(row["value_json"]))
            except (TypeError, json.JSONDecodeError) as exc:
                raise ValueError("DATABASE_REBUILD_IMPORT_RECEIPT_INVALID") from exc
            if (not isinstance(value, dict) or set(value) != {"fingerprint", "result"}
                    or not isinstance(value["fingerprint"], str)
                    or not re.fullmatch(r"[0-9a-f]{64}", value["fingerprint"])
                    or not isinstance(value["result"], dict)
                    or value["result"].get("operation_id") != operation_id):
                raise ValueError("DATABASE_REBUILD_IMPORT_RECEIPT_INVALID")
            result.append((key, json.dumps(value, ensure_ascii=False, sort_keys=True,
                                           separators=(",", ":")), row["updated_at"]))
        return result
    finally:
        connection.close()


def _database_receipt(path: Path, operation_id: str) -> dict[str, Any] | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600):
        raise ValueError("DATABASE_REBUILD_SOURCE_DATABASE_UNSAFE")
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=30)
    try:
        row = connection.execute("SELECT value_json FROM settings WHERE key=?",
                                 (RECEIPT_PREFIX + operation_id,)).fetchone()
    except sqlite3.Error:
        return None
    finally:
        connection.close()
    if row is None:
        return None
    value = json.loads(str(row[0]))
    if (not isinstance(value, dict) or set(value) != {"fingerprint", "result"}
            or not isinstance(value.get("fingerprint"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", value["fingerprint"])
            or not isinstance(value.get("result"), dict)
            or value["result"].get("operation_id") != operation_id):
        raise ValueError("DATABASE_REBUILD_IMPORT_RECEIPT_INVALID")
    return value


def _import_fingerprint(snapshot: dict[str, Any], normalize_runtime_state: bool) -> str:
    payload = json.dumps(
        {"snapshot": snapshot, "normalize_runtime_state": normalize_runtime_state},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _verified_replayed_import(
    path: Path, operation_id: str, snapshot: dict[str, Any], normalize_runtime_state: bool,
) -> dict[str, Any]:
    receipt = _database_receipt(path, operation_id)
    expected = _import_fingerprint(snapshot, normalize_runtime_state)
    if receipt is None or receipt.get("fingerprint") != expected:
        raise ValueError("DATABASE_REBUILD_IMPORT_REPLAY_CONFLICT")
    return {**receipt["result"], "replayed": True}


def _validate_database_file(path: Path) -> dict[str, Any]:
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600):
        raise ValueError("DATABASE_REBUILD_DATABASE_FILE_UNSAFE")
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        integrity = [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        schema = inspect_database_schema(connection)
        if integrity != ["ok"] or foreign_keys or not schema.get("ok"):
            raise ValueError("DATABASE_REBUILD_DATABASE_VALIDATION_FAILED")
        return schema
    finally:
        connection.close()


def _logical_database_digest(path: Path) -> str:
    from fwrouter_api.services.database_backup import _logical_digest

    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=30)
    try:
        return _logical_digest(connection)
    finally:
        connection.close()


def _schema_objects(path: Path) -> list[tuple[str, str, str, str | None]]:
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=30)
    try:
        return [
            (str(row[0]), str(row[1]), str(row[2]), None if row[3] is None else str(row[3]))
            for row in connection.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_stat%' ORDER BY type,name,tbl_name",
            )
        ]
    finally:
        connection.close()


def _require_canonical_rebuild_schema(source_path: Path, candidate_path: Path) -> None:
    # The candidate is initialized by this exact backend before the owner import.
    # Reject source-only triggers/views/indexes/table definitions rather than
    # silently dropping schema behavior outside the transfer contract.
    if _schema_objects(source_path) != _schema_objects(candidate_path):
        raise ValueError("DATABASE_REBUILD_SCHEMA_OBJECTS_UNSUPPORTED")


def _backup_pin_path(state_root: Path, operation_id: str, filename: str) -> Path:
    expected_prefix = f"fwrouter.db.rebuild-{operation_id}."
    if (not filename or filename != Path(filename).name or "/" in filename or "\\" in filename
            or not filename.startswith(expected_prefix) or not filename.endswith(".bak")):
        raise ValueError("DATABASE_REBUILD_BACKUP_PIN_INVALID")
    return state_root / "backups" / filename


def _validate_pinned_backup(
    pin: dict[str, str], operation_id: str, state_root: Path,
) -> dict[str, Any]:
    if set(pin) != {"filename", "sha256"} or not re.fullmatch(r"[0-9a-f]{64}", pin["sha256"]):
        raise ValueError("DATABASE_REBUILD_BACKUP_PIN_INVALID")
    backup_path = _backup_pin_path(state_root, operation_id, pin["filename"])
    backup_dir = backup_path.parent
    try:
        directory_info = backup_dir.lstat()
    except FileNotFoundError as exc:
        raise ValueError("DATABASE_REBUILD_BACKUP_MISSING") from exc
    if (not stat.S_ISDIR(directory_info.st_mode) or stat.S_ISLNK(directory_info.st_mode)
            or directory_info.st_uid != os.geteuid() or stat.S_IMODE(directory_info.st_mode) != 0o700):
        raise ValueError("DATABASE_REBUILD_BACKUP_DIRECTORY_UNSAFE")
    try:
        info = backup_path.lstat()
    except FileNotFoundError as exc:
        raise ValueError("DATABASE_REBUILD_BACKUP_MISSING") from exc
    if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
        raise ValueError("DATABASE_REBUILD_BACKUP_UNSAFE")
    try:
        _validate_database_file(backup_path)
    except (sqlite3.Error, ValueError) as exc:
        raise ValueError("DATABASE_REBUILD_BACKUP_INVALID") from exc
    # Hash after SQLite validation so a publication-boundary caller can make
    # this the last read of the pinned bytes before its atomic rename.
    digest = _hash_regular_file(backup_path)
    if digest != pin["sha256"]:
        raise ValueError("DATABASE_REBUILD_BACKUP_PIN_MISMATCH")
    return {"ok": True, "created": False, "backup_path": str(backup_path),
            "sha256": digest, "db_path": str(get_db_path())}


def _validate_active_operation_backup(
    backup: dict[str, Any], operation_id: str, state_root: Path,
) -> dict[str, Any]:
    """Re-read lifecycle authority and verify the exact pinned file on disk."""
    from fwrouter_api.services.maintenance_lifecycle_admission import offline_rebuild_backup_pin

    pin = offline_rebuild_backup_pin()
    if pin is None:
        raise ValueError("DATABASE_REBUILD_BACKUP_PIN_MISSING")
    validated = _validate_pinned_backup(pin, operation_id, state_root)
    if (Path(str(backup.get("backup_path") or "")) != Path(validated["backup_path"])
            or backup.get("sha256") != pin["sha256"]):
        raise ValueError("DATABASE_REBUILD_BACKUP_PIN_MISMATCH")
    return validated


def _candidate_path_if_present(state_root: Path, operation_id: str) -> Path | None:
    directory = state_root / f".maintenance-rebuild-{operation_id}"
    try:
        info = directory.lstat()
    except FileNotFoundError:
        return None
    if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700):
        raise ValueError("DATABASE_REBUILD_CANDIDATE_DIRECTORY_UNSAFE")
    allowed = {"candidate.sqlite", "candidate.sqlite-wal", "candidate.sqlite-shm"}
    names = set()
    for item in directory.iterdir():
        child = item.lstat()
        if (item.name not in allowed or stat.S_ISLNK(child.st_mode)
                or not stat.S_ISREG(child.st_mode) or child.st_uid != os.geteuid()):
            raise ValueError("DATABASE_REBUILD_CANDIDATE_CONTENT_UNSAFE")
        names.add(item.name)
    return directory / "candidate.sqlite" if names else None


def _operation_backup(operation_id: str, db_path: Path, state_root: Path) -> dict[str, Any]:
    from fwrouter_api.services.maintenance_lifecycle_admission import (
        offline_rebuild_backup_pin, offline_rebuild_phase, record_offline_rebuild_backup_pin,
    )

    phase = offline_rebuild_phase()
    candidate = _candidate_path_if_present(state_root, operation_id)
    published = _database_receipt(db_path, operation_id) is not None
    pin = offline_rebuild_backup_pin()
    if pin is not None:
        backup = _validate_pinned_backup(pin, operation_id, state_root)
        if not published and _logical_database_digest(db_path) != _logical_database_digest(
            Path(str(backup["backup_path"])),
        ):
            raise ValueError("DATABASE_REBUILD_SOURCE_CHANGED_AFTER_BACKUP")
        return backup
    if phase != "pending" or candidate is not None or published:
        raise ValueError("DATABASE_REBUILD_BACKUP_PIN_MISSING")

    backup_dir = state_root / "backups"
    prefix = f"{db_path.name}.rebuild-{operation_id}."
    try:
        backup_dir_info = backup_dir.lstat()
    except FileNotFoundError:
        backup_dir_info = None
    if backup_dir_info is not None and (
            not stat.S_ISDIR(backup_dir_info.st_mode) or stat.S_ISLNK(backup_dir_info.st_mode)
            or backup_dir_info.st_uid != os.geteuid() or stat.S_IMODE(backup_dir_info.st_mode) != 0o700):
        raise ValueError("DATABASE_REBUILD_BACKUP_DIRECTORY_UNSAFE")
    existing = sorted(backup_dir.glob(f"{prefix}*.bak")) if backup_dir_info is not None else []
    if len(existing) > 1:
        raise ValueError("DATABASE_REBUILD_BACKUP_AMBIGUOUS")
    if existing:
        path = existing[0]
        if (path.parent != backup_dir or not path.name.startswith(prefix)
                or not path.name.endswith(".bak")):
            raise ValueError("DATABASE_REBUILD_BACKUP_UNSAFE")
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1):
            raise ValueError("DATABASE_REBUILD_BACKUP_UNSAFE")
        _validate_database_file(path)
        backup = {"ok": True, "created": False, "backup_path": str(path),
                  "sha256": _hash_regular_file(path), "db_path": str(db_path)}
    else:
        backup = backup_database_file(operation_id=operation_id)
        if not backup.get("ok") or not backup.get("created"):
            raise ValueError("DATABASE_REBUILD_BACKUP_CREATE_FAILED")
    backup_path = Path(str(backup.get("backup_path") or ""))
    # A crash after file publication but before checkpoint pinning may only be
    # adopted when its complete snapshot still matches the unchanged source.
    if backup_path.parent != backup_dir or _logical_database_digest(db_path) != _logical_database_digest(backup_path):
        raise ValueError("DATABASE_REBUILD_UNPINNED_BACKUP_AMBIGUOUS")
    digest = _hash_regular_file(backup_path)
    pin_value = {"filename": backup_path.name, "sha256": digest}
    record_offline_rebuild_backup_pin(**pin_value)
    return _validate_pinned_backup(pin_value, operation_id, state_root)


def _hash_regular_file(path: Path) -> str:
    """Hash one stable regular file without following a substituted symlink."""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        named = path.lstat()
        if (not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(named.st_mode)
                or (before.st_dev, before.st_ino) != (named.st_dev, named.st_ino)
                or before.st_uid != os.geteuid() or stat.S_IMODE(before.st_mode) != 0o600):
            raise ValueError("DATABASE_REBUILD_BACKUP_UNSAFE")
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        after = os.fstat(descriptor)
        if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
            raise ValueError("DATABASE_REBUILD_BACKUP_CHANGED_WHILE_HASHING")
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def _copy_source_import_receipts(source_path: Path, candidate_path: Path) -> None:
    receipts = _read_import_receipts(source_path)
    source = sqlite3.connect(f"{source_path.resolve().as_uri()}?mode=ro", uri=True, timeout=30)
    try:
        row = source.execute(
            "SELECT value_json, updated_at FROM settings WHERE key='core.intent_revision'",
        ).fetchone()
    finally:
        source.close()
    if row is None:
        raise ValueError("DATABASE_REBUILD_CORE_INTENT_REVISION_MISSING")
    try:
        revision = json.loads(str(row[0]))
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("DATABASE_REBUILD_CORE_INTENT_REVISION_INVALID") from exc
    if type(revision) is not int or revision < 0:
        raise ValueError("DATABASE_REBUILD_CORE_INTENT_REVISION_INVALID")
    sequence_source = sqlite3.connect(
        f"{source_path.resolve().as_uri()}?mode=ro", uri=True, timeout=30,
    )
    try:
        source_tables = {str(item[0]) for item in sequence_source.execute(
            "SELECT name FROM sqlite_master WHERE type='table'",
        )}
        sequences = sequence_source.execute(
            "SELECT name, seq FROM sqlite_sequence ORDER BY name",
        ).fetchall() if "sqlite_sequence" in source_tables else []
    finally:
        sequence_source.close()
    from fwrouter_api.services.control_plane_transfer_common import CONTROL_PLANE_TABLES

    transferred = set(CONTROL_PLANE_TABLES)
    sequence_rows: list[tuple[str, int]] = []
    for table, seq in sequences:
        name = str(table)
        if name not in transferred or type(seq) is not int or seq < 0:
            raise ValueError("DATABASE_REBUILD_UNSUPPORTED_SQLITE_SEQUENCE")
        sequence_rows.append((name, seq))
    connection = sqlite3.connect(candidate_path, timeout=30)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO settings(key,value_json,updated_at) VALUES ('core.intent_revision',?,?) "
            "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at",
            (json.dumps(revision), row[1]),
        )
        if sequence_rows:
            if "sqlite_sequence" not in {
                str(item[0]) for item in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'",
                )
            }:
                raise ValueError("DATABASE_REBUILD_CANDIDATE_SEQUENCE_TABLE_MISSING")
            for name, seq in sequence_rows:
                current = connection.execute(
                    "SELECT seq FROM sqlite_sequence WHERE name=?", (name,),
                ).fetchone()
                if current is None:
                    connection.execute("INSERT INTO sqlite_sequence(name,seq) VALUES (?,?)", (name, seq))
                elif type(current[0]) is not int or current[0] < seq:
                    connection.execute("UPDATE sqlite_sequence SET seq=? WHERE name=?", (seq, name))
        if receipts:
            connection.executemany(
                "INSERT INTO settings(key, value_json, updated_at) VALUES (?, ?, ?)", receipts,
            )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _candidate_directory(operation_id: str) -> tuple[Path, Path]:
    state_root = get_settings().paths.state_dir.absolute()
    directory = state_root / f".maintenance-rebuild-{operation_id}"
    try:
        directory.mkdir(mode=0o700)
    except FileExistsError:
        pass
    info = directory.lstat()
    if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700):
        raise ValueError("DATABASE_REBUILD_CANDIDATE_DIRECTORY_UNSAFE")
    return directory, directory / "candidate.sqlite"


def _discard_incomplete_candidate(directory: Path) -> None:
    for path in directory.iterdir():
        info = path.lstat()
        if (stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()):
            raise ValueError("DATABASE_REBUILD_CANDIDATE_CONTENT_UNSAFE")
        if path.name not in {"candidate.sqlite", "candidate.sqlite-wal", "candidate.sqlite-shm"}:
            raise ValueError("DATABASE_REBUILD_CANDIDATE_CONTENT_UNKNOWN")
        path.unlink()


def _checkpoint_source_for_publication(db_path: Path) -> None:
    """Use SQLite's own crash-safe WAL transition; never unlink sidecars manually."""
    _checkpoint_database_for_publication(db_path, "SOURCE")


def _checkpoint_database_for_publication(db_path: Path, label: str) -> None:
    connection = sqlite3.connect(db_path, timeout=30)
    try:
        checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if checkpoint is not None and int(checkpoint[0]) != 0:
            raise ValueError(f"DATABASE_REBUILD_{label}_WAL_BUSY")
        journal_mode = str(connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0]).lower()
        if journal_mode != "delete":
            raise ValueError(f"DATABASE_REBUILD_{label}_JOURNAL_MODE_UNCHANGED")
        integrity = [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]
        if integrity != ["ok"] or connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError(f"DATABASE_REBUILD_{label}_VALIDATION_FAILED")
    finally:
        connection.close()
    for suffix in ("-wal", "-shm"):
        if Path(f"{db_path}{suffix}").exists():
            raise ValueError(f"DATABASE_REBUILD_{label}_SIDECAR_REMAINS")


def _atomic_publish_candidate(
    candidate: Path, db_path: Path, backup: dict[str, Any], *,
    operation_id: str, state_root: Path,
) -> None:
    backup_path = Path(str(backup.get("backup_path") or ""))
    if any(Path(f"{db_path}{suffix}").exists() for suffix in ("-wal", "-shm")):
        raise ValueError("DATABASE_REBUILD_SOURCE_SIDECAR_REMAINS")
    if any(Path(f"{candidate}{suffix}").exists() for suffix in ("-wal", "-shm")):
        raise ValueError("DATABASE_REBUILD_CANDIDATE_SIDECAR_REMAINS")
    source_info = db_path.lstat()
    candidate_info = candidate.lstat()
    parent_info = db_path.parent.lstat()
    if (not stat.S_ISREG(source_info.st_mode) or stat.S_ISLNK(source_info.st_mode)
            or source_info.st_uid != os.geteuid() or stat.S_IMODE(source_info.st_mode) != 0o600
            or not stat.S_ISREG(candidate_info.st_mode) or stat.S_ISLNK(candidate_info.st_mode)
            or candidate_info.st_uid != os.geteuid() or stat.S_IMODE(candidate_info.st_mode) != 0o600
            or not stat.S_ISDIR(parent_info.st_mode) or stat.S_ISLNK(parent_info.st_mode)
            or parent_info.st_uid != os.geteuid() or stat.S_IMODE(parent_info.st_mode) != 0o700
            or candidate_info.st_dev != parent_info.st_dev):
        raise ValueError("DATABASE_REBUILD_PUBLICATION_PATH_UNSAFE")
    if _logical_database_digest(db_path) != _logical_database_digest(backup_path):
        raise ValueError("DATABASE_REBUILD_SOURCE_CHANGED_BEFORE_PUBLICATION")
    # Re-read both lifecycle pin and file bytes immediately before the
    # irreversible rename. Logical equality alone does not preserve the
    # original backup identity if a different SQLite image has the same rows.
    _validate_active_operation_backup(backup, operation_id, state_root)
    os.replace(candidate, db_path)
    descriptor = os.open(db_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    directory_fd = os.open(db_path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _selection_fence_state(db_path: Path) -> dict[str, Any] | None:
    try:
        source_info = db_path.lstat()
    except FileNotFoundError:
        return None
    parent_info = db_path.parent.lstat()
    if (not stat.S_ISREG(source_info.st_mode) or stat.S_ISLNK(source_info.st_mode)
            or source_info.st_uid != os.geteuid() or stat.S_IMODE(source_info.st_mode) != 0o600
            or not stat.S_ISDIR(parent_info.st_mode) or stat.S_ISLNK(parent_info.st_mode)
            or parent_info.st_uid != os.geteuid() or stat.S_IMODE(parent_info.st_mode) != 0o700):
        return {"stage": "source_database", "code": "DATABASE_REBUILD_SOURCE_DATABASE_UNSAFE"}
    connection = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True, timeout=30)
    try:
        tables = {str(row[0]) for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "settings" in tables:
            if connection.execute(
                "SELECT 1 FROM settings WHERE key IN ('routing.auto_selection_revision', "
                "'routing.auto_selection_provenance', 'subscriptions.native_generation.v1') LIMIT 1"
            ).fetchone():
                return {"stage": "selection_fence_preflight",
                        "code": "DATABASE_REBUILD_SELECTION_FENCE_REQUIRED"}
        if "routing_global_state" in tables and connection.execute(
            "SELECT 1 FROM routing_global_state WHERE id=1 AND active_auto_server_id IS NOT NULL LIMIT 1"
        ).fetchone():
            return {"stage": "selection_fence_preflight",
                    "code": "DATABASE_REBUILD_SELECTION_FENCE_REQUIRED"}
    finally:
        connection.close()

    # Snapshot v2 deliberately transfers only the tables declared by its
    # existing owner. A candidate rebuild would otherwise silently discard
    # provider bindings/credentials, subscription identities, telemetry,
    # jobs, logs, or future tables unknown to that contract. Until each such
    # domain owner supplies an import hook, reject nonempty omitted tables
    # before creating the backup or candidate. Return names only; never expose
    # row contents (which may include secrets).
    from fwrouter_api.services.control_plane_transfer_common import CONTROL_PLANE_TABLES

    transferred = set(CONTROL_PLANE_TABLES)
    connection = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True, timeout=30)
    try:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name",
        ).fetchall()
        unsupported: list[str] = []
        for row in rows:
            table = str(row[0])
            if (table in {"schema_meta", "sqlite_sequence"} or table in transferred
                    or table.startswith("sqlite_stat")):
                continue
            quoted = '"' + table.replace('"', '""') + '"'
            if connection.execute(f"SELECT 1 FROM {quoted} LIMIT 1").fetchone() is not None:
                unsupported.append(table)
        if "schema_meta" in tables:
            schema_rows = connection.execute("SELECT key,value FROM schema_meta").fetchall()
            if len(schema_rows) != 1 or tuple(schema_rows[0]) != ("schema_version", "27"):
                unsupported.append("schema_meta")
        if "sqlite_sequence" in tables:
            invalid_sequences = connection.execute(
                "SELECT 1 FROM sqlite_sequence WHERE name NOT IN (%s) LIMIT 1"
                % ",".join("?" for _ in transferred),
                tuple(sorted(transferred)),
            ).fetchone()
            if invalid_sequences is not None:
                unsupported.append("sqlite_sequence")
        if unsupported:
            return {"stage": "restore_eligibility",
                    "code": "DATABASE_REBUILD_UNSUPPORTED_DURABLE_TABLES",
                    "tables": sorted(set(unsupported))}
    finally:
        connection.close()
    from fwrouter_api.adapters.xray_common import _default_xray_config_path

    checkpoint = Path(_default_xray_config_path()).parent / ".generation" / "generation-checkpoint.json"
    try:
        checkpoint.lstat()
    except FileNotFoundError:
        return None
    return {"stage": "native_generation_preflight",
            "code": "DATABASE_REBUILD_NATIVE_GENERATION_STATE_PRESENT"}


def _finish_candidate_rebuild(
    *, source_snapshot: dict[str, Any], db_path: Path, operation_id: str,
    normalize_runtime_state: bool, backup: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    operation_id_hex = uuid.UUID(operation_id).hex
    directory, candidate = _candidate_directory(operation_id_hex)
    backup_path = Path(str(backup["backup_path"]))
    if backup.get("created") is not True and not backup_path.is_file():
        raise ValueError("DATABASE_REBUILD_BACKUP_MISSING")
    _validate_database_file(backup_path)
    if _logical_database_digest(db_path) != _logical_database_digest(backup_path):
        raise ValueError("DATABASE_REBUILD_SOURCE_CHANGED_AFTER_BACKUP")

    candidate_receipt = _database_receipt(candidate, operation_id_hex) if candidate.exists() else None
    if candidate_receipt is not None:
        # This is a fully committed candidate from an earlier attempt. Verify
        # operation identity/payload through the owner before reusing it.
        replay = _verified_replayed_import(
            candidate, operation_id_hex, source_snapshot, normalize_runtime_state,
        )
        if not replay.get("ok") or replay.get("operation_id") != operation_id_hex:
            raise ValueError("DATABASE_REBUILD_CANDIDATE_REPLAY_CONFLICT")
        _checkpoint_database_for_publication(candidate, "CANDIDATE")
        _validate_database_file(candidate)
        from fwrouter_api.services.maintenance_lifecycle_admission import offline_rebuild_phase
        if offline_rebuild_phase() == "pending":
            _offline_phase("candidate_ready")
        return replay, backup

    _discard_incomplete_candidate(directory)
    with offline_candidate_database_path(candidate):
        schema_state = initialize_database()
        if not schema_state.get("ok"):
            raise ValueError("DATABASE_REBUILD_CANDIDATE_SCHEMA_INVALID")
        _require_canonical_rebuild_schema(backup_path, candidate)
        _copy_source_import_receipts(backup_path, candidate)
        imported = import_control_plane_snapshot(
            source_snapshot, normalize_runtime_state=normalize_runtime_state,
            operation_id=operation_id_hex,
        )
        if (not imported.get("ok") or imported.get("operation_id") != operation_id_hex
                or imported.get("replayed") is True):
            raise ValueError("DATABASE_REBUILD_CANDIDATE_IMPORT_FAILED")

    connection = sqlite3.connect(candidate, timeout=30)
    try:
        checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if checkpoint is not None and int(checkpoint[0]) != 0:
            raise ValueError("DATABASE_REBUILD_CANDIDATE_WAL_BUSY")
        mode = str(connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0]).lower()
        if mode != "delete":
            raise ValueError("DATABASE_REBUILD_CANDIDATE_JOURNAL_MODE_UNCHANGED")
        connection.commit()
    finally:
        connection.close()
    for suffix in ("-wal", "-shm"):
        if Path(f"{candidate}{suffix}").exists():
            raise ValueError("DATABASE_REBUILD_CANDIDATE_SIDECAR_REMAINS")
    os.chmod(candidate, 0o600, follow_symlinks=False)
    _validate_database_file(candidate)
    file_fd = os.open(candidate, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fsync(file_fd)
    finally:
        os.close(file_fd)
    dir_fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
    from fwrouter_api.services.maintenance_lifecycle_admission import offline_rebuild_phase
    if offline_rebuild_phase() == "pending":
        _offline_phase("candidate_ready")
    return imported, backup


def get_database_schema_state() -> dict[str, Any]:
    # Preserve the established repair/initialize contract for normal callers.
    initialize_database()
    with connect() as connection:
        schema_state = inspect_database_schema(connection)
    return {
        **schema_state,
        "summary": summarize_schema_state(schema_state),
        "db_path": str(get_db_path()),
    }


def _read_database_schema_state(path: Path) -> dict[str, Any]:
    """Validate/report a published file without changing its SQLite journal mode."""
    schema_state = _validate_database_file(path)
    return {
        **schema_state,
        "summary": summarize_schema_state(schema_state),
        "db_path": str(path),
    }


def _utc_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _backup_dir() -> Path:
    return get_settings().paths.state_dir / "backups"


def backup_database_file(*, operation_id: str | None = None) -> dict[str, Any]:
    db_path = get_db_path()
    try:
        db_path.lstat()
    except FileNotFoundError:
        return {
            "ok": True,
            "created": False,
            "backup_path": None,
            "db_path": str(db_path),
        }

    suffix = f"rebuild-{operation_id}." if operation_id is not None else ""
    if operation_id is not None and not re.fullmatch(r"[0-9a-f]{32}", operation_id):
        raise ValueError("DATABASE_BACKUP_OPERATION_ID_INVALID")
    receipt = create_sqlite_backup(
        db_path,
        _backup_dir(),
        prefix=f"{db_path.name}.{suffix}{_utc_stamp()}.",
    )
    return {
        "ok": True,
        "created": True,
        **receipt,
        "db_path": str(db_path),
    }


def reconcile_control_plane_runtime(*, requested_by: str = "database_reconcile") -> dict[str, Any]:
    ensure_builtin_system_subjects()
    external_ingress_providers = [
        str(connection.get("runtime_type") or "").strip()
        for connection in list_external_connections(enabled_only=True)
        if str(connection.get("connection_type") or "") == "external_network_source"
        and str(connection.get("runtime_type") or "").strip()
    ]
    sync_result = sync_subject_inventory(
        requested_by=requested_by,
        discover_docker=True,
        discover_host=True,
        discover_external_ingress_providers=external_ingress_providers,
        discover_xray=True,
        include_all_external_ingress_peers=False,
        lan_clients=[],
        tailscale_nodes=[],
        host_services=[],
    )
    return {
        "builtin_system_subjects_ensured": True,
        "subject_inventory": sync_result,
    }


def rebuild_control_plane_database(
    *,
    snapshot: dict[str, Any] | None = None,
    file_path: str | None = None,
    normalize_runtime_state: bool = True,
    requested_by: str = "database_rebuild",
    operation_id: str | None = None,
) -> dict[str, Any]:
    try:
        _require_qualified_offline_maintenance_context()
    except _OfflineMaintenanceContextUnqualified:
        return {
            "ok": False,
            "stage": "maintenance_admission",
            "error": {
                "code": "DATABASE_REBUILD_MAINTENANCE_CONTEXT_UNQUALIFIED",
                "message": "Offline rebuild is blocked until lifecycle exclusion and quiescence are qualified.",
            },
        }

    from fwrouter_api.services.maintenance_lifecycle_admission import (
        active_offline_rebuild_operation_id,
        active_offline_rebuild_state_root,
    )
    try:
        admitted_operation_id = uuid.UUID(active_offline_rebuild_operation_id()).hex
    except Exception:
        return {
            "ok": False,
            "stage": "maintenance_admission",
            "error": {"code": "DATABASE_REBUILD_MAINTENANCE_CONTEXT_UNQUALIFIED"},
        }
    if operation_id is None:
        operation_id = admitted_operation_id
    try:
        operation_id = uuid.UUID(operation_id).hex
    except (ValueError, TypeError, AttributeError):
        return {"ok": False, "stage": "maintenance_admission",
                "error": {"code": "DATABASE_REBUILD_OPERATION_ID_INVALID"}}
    if operation_id != admitted_operation_id:
        return {"ok": False, "stage": "maintenance_admission",
                "error": {"code": "DATABASE_REBUILD_OPERATION_ID_MISMATCH"}}

    # The cooperative lease is deliberately fixed to the installed host state
    # root. Refuse environment/config redirects before resolving snapshots,
    # creating transfer directories, or opening any database/backup paths.
    admitted_root = Path(os.path.abspath(active_offline_rebuild_state_root()))
    configured_root = Path(os.path.abspath(get_settings().paths.state_dir))
    configured_db_path = Path(os.path.abspath(get_db_path()))
    if (configured_root != admitted_root
            or configured_db_path != admitted_root / "fwrouter.db"):
        return {
            "ok": False,
            "stage": "maintenance_admission",
            "error": {"code": "DATABASE_REBUILD_STATE_ROOT_MISMATCH"},
        }

    source = resolve_control_plane_snapshot_source(snapshot=snapshot, file_path=file_path)
    if not source["ok"]:
        return {
            "ok": False,
            "stage": "resolve_snapshot",
            "error": source["error"],
        }
    db_path = get_db_path()
    # A completed publication may be retried after a process interruption
    # before reconciliation/checkpoint completion. The durable owner receipt
    # is the forward-completion proof; it is checked before eligibility guards.
    if db_path.exists() and _database_receipt(db_path, operation_id) is not None:
        replay = _verified_replayed_import(
            db_path, operation_id, source["snapshot"], normalize_runtime_state,
        )
        if not replay.get("ok") or replay.get("operation_id") != operation_id:
            return {"ok": False, "stage": "import_replay",
                    "error": {"code": replay.get("error_code", "DATABASE_REBUILD_REPLAY_CONFLICT")},
                    "import": replay}
        backup = _operation_backup(operation_id, db_path, admitted_root)
        _offline_phase("published")
        reconciliation = reconcile_control_plane_runtime(requested_by=requested_by)
        inventory = reconciliation.get("subject_inventory") if isinstance(reconciliation, dict) else None
        final_schema_state = _read_database_schema_state(db_path)
        result = {"ok": isinstance(inventory, dict) and inventory.get("ok") is True,
                "stage": None if isinstance(inventory, dict) and inventory.get("ok") is True
                else "runtime_reconcile",
                "persistence_status": "published",
                "inventory_reconcile_status": "complete"
                if isinstance(inventory, dict) and inventory.get("ok") is True else "incomplete",
                "native_proof_status": "not_provided",
                "backup": backup, "source": source["source"],
                "normalize_runtime_state": normalize_runtime_state, "import": replay,
                "reconciliation": reconciliation, "schema": final_schema_state,
                "operation_id": operation_id}
        _checkpoint_database_for_publication(db_path, "PUBLISHED")
        _validate_active_operation_backup(backup, operation_id, admitted_root)
        return result

    with xray_writer_guard(timeout_seconds=30.0):
        fence = _selection_fence_state(db_path)
        if fence is not None:
            return {"ok": False, "stage": fence["stage"],
                    "error": {"code": fence["code"],
                              "tables": fence.get("tables", []),
                              "message": "Database rebuild is disabled while selection, native generation, or unsupported durable state exists."}}
        if not db_path.exists():
            return {"ok": False, "stage": "source_database",
                    "error": {"code": "DATABASE_REBUILD_SOURCE_DATABASE_MISSING"}}
        backup = _operation_backup(operation_id, db_path, admitted_root)
        imported, backup = _finish_candidate_rebuild(
            source_snapshot=source["snapshot"], db_path=db_path,
            operation_id=operation_id, normalize_runtime_state=normalize_runtime_state,
            backup=backup,
        )
        _checkpoint_source_for_publication(db_path)
        _offline_phase("source_quiesced")
        _atomic_publish_candidate(
            get_settings().paths.state_dir / f".maintenance-rebuild-{operation_id}" / "candidate.sqlite",
            db_path, backup, operation_id=operation_id, state_root=admitted_root,
        )
        _offline_phase("published")
        _validate_database_file(db_path)
        if _database_receipt(db_path, operation_id) is None:
            raise ValueError("DATABASE_REBUILD_PUBLISHED_RECEIPT_MISSING")
    reconciliation = reconcile_control_plane_runtime(requested_by=requested_by)
    inventory = reconciliation.get("subject_inventory") if isinstance(reconciliation, dict) else None
    final_schema_state = _read_database_schema_state(db_path)
    write_operational_log(
        event_type="control_plane_database_rebuilt",
        message="Control-plane SQLite database was rebuilt from snapshot.",
        details={
            "requested_by": requested_by,
            "normalize_runtime_state": normalize_runtime_state,
            "snapshot_source": source["source"],
            "backup": backup,
        "schema": final_schema_state["summary"],
        "operation_id": operation_id,
        },
    )
    result = {
        "ok": isinstance(inventory, dict) and inventory.get("ok") is True,
        "stage": None if isinstance(inventory, dict) and inventory.get("ok") is True
        else "runtime_reconcile",
        "persistence_status": "published",
        "inventory_reconcile_status": "complete"
        if isinstance(inventory, dict) and inventory.get("ok") is True else "incomplete",
        "native_proof_status": "not_provided",
        "backup": backup,
        "source": source["source"],
        "normalize_runtime_state": normalize_runtime_state,
        "import": imported,
        "reconciliation": reconciliation,
        "schema": final_schema_state,
        "operation_id": operation_id,
    }
    # SQLite connect() re-enables WAL for operational writes (inventory and
    # logs). Finish with SQLite's own checkpoint/journal transition; never
    # unlink sidecars manually. A failed checkpoint leaves honest published
    # state plus the lifecycle checkpoint pending for a forward retry.
    _checkpoint_database_for_publication(db_path, "PUBLISHED")
    _validate_active_operation_backup(backup, operation_id, admitted_root)
    return result


def cleanup_runtime_state(*, requested_by: str = "database_cleanup") -> dict[str, Any]:
    state_dir = get_settings().paths.state_dir
    duplicate_db_paths = [
        state_dir / "db.sqlite",
        state_dir / "state.db",
        state_dir / "state" / "fwrouter.db",
        Path("/opt/fwrouter-api/fwrouter.db"),
    ]
    removed_files: list[str] = []
    kept_files: list[str] = []

    for path in duplicate_db_paths:
        if not path.exists():
            continue
        if path == get_db_path():
            kept_files.append(str(path))
            continue
        if path.stat().st_size == 0:
            path.unlink()
            removed_files.append(str(path))
        else:
            kept_files.append(str(path))

    deleted_subject_rows = 0
    deleted_server_rows = 0
    deleted_snapshot_rows = 0
    deleted_server_pref_rows = 0

    test_subject_ids = {
        "lan-1",
        "lan-2",
        "lan-fail",
        "lan-fail-user",
        "lan-follow",
        "lan-state",
        "lan-traffic",
        "lan-transfer",
        "lan-user",
        "lan-vpn",
        "legacy-ts-1",
        "ts-node-2",
        "tailscale-node:peer-1",
        "tailscale-node:peer-a",
        "tailscale-node:test-peer",
        "xray:uuid-list",
    }

    with xray_writer_guard(timeout_seconds=30.0), connect() as connection:
        before_pool = selection_pool_signature(connection)
        placeholders = ", ".join("?" for _ in test_subject_ids)
        deleted_snapshot_rows = connection.execute(
            """
            DELETE FROM traffic_counter_snapshots
            WHERE subject_id IS NOT NULL
              AND subject_id NOT IN (SELECT subject_id FROM subjects)
            """
        ).rowcount
        deleted_server_pref_rows = connection.execute(
            """
            DELETE FROM server_preferences
            WHERE server_id IN (
                SELECT p.server_id
                FROM server_preferences AS p
                JOIN servers AS s ON s.server_id = p.server_id
                WHERE s.inventory_state != 'active'
                  AND lower(s.server_id) = 'test'
            )
            """
        ).rowcount
        deleted_subject_rows = connection.execute(
            f"""
            DELETE FROM subjects
            WHERE subject_id IN ({placeholders})
            """,
            tuple(sorted(test_subject_ids)),
        ).rowcount
        deleted_server_rows = connection.execute(
            """
            DELETE FROM servers
            WHERE lower(server_id) = 'test'
            """
        ).rowcount
        after_pool = selection_pool_signature(connection)
        if after_pool != before_pool:
            advanced = advance_selection_revision(connection)
            if advanced is None:
                raise RuntimeError("VPN-auto selection fence could not be advanced during runtime cleanup.")
        connection.commit()

    result = {
        "ok": True,
        "requested_by": requested_by,
        "removed_files": removed_files,
        "kept_files": kept_files,
        "deleted_subject_rows": deleted_subject_rows,
        "deleted_server_rows": deleted_server_rows,
        "deleted_snapshot_rows": deleted_snapshot_rows,
        "deleted_server_preference_rows": deleted_server_pref_rows,
    }
    write_operational_log(
        event_type="runtime_state_cleanup_completed",
        message="Runtime state cleanup completed.",
        details=result,
    )
    return result
