"""Private, WAL-aware SQLite backup publication.

Backups are standalone SQLite databases captured through SQLite's online backup
API. The source is opened read-only and a read transaction is pinned before the
copy. Validation compares a typed, length-framed digest of the schema and every
table value from that same pinned source snapshot and the destination, in
addition to integrity and foreign-key checks. Values are streamed into SHA-256,
so Python does not hold the whole database in memory. A largest cell or schema
object and SQLite's own temporary resources still affect memory use.

Path race protection assumes the normal FWRouter trusted-uid/root boundary; it
does not claim protection from a hostile process running as root. Read-only
SQLite access can still create/update SQLite sidecars depending on SQLite and
filesystem behavior.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import shutil
import sqlite3
import stat
import struct
import time
from pathlib import Path
from typing import Any


_BUSY_TIMEOUT_SECONDS = 30.0
_BUSY_TIMEOUT_MILLISECONDS = int(_BUSY_TIMEOUT_SECONDS * 1000)
_COPY_DEADLINE_SECONDS = 30.0


def _check_directory_chain(path: Path) -> None:
    # Do not lexical-normalize away a symlink/.. traversal before checking it.
    if ".." in path.parts:
        raise ValueError("DATABASE_BACKUP_PATH_INVALID")
    absolute = Path(os.path.abspath(path))
    parts = list(reversed((absolute, *absolute.parents)))
    for item in parts:
        try:
            info = item.lstat()
        except FileNotFoundError:
            raise ValueError("DATABASE_BACKUP_PATH_INVALID") from None
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ValueError("DATABASE_BACKUP_PATH_INVALID")
        mode = stat.S_IMODE(info.st_mode)
        sticky_root = info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX)
        protected_root = info.st_uid == 0 and not (mode & 0o022)
        if info.st_uid != os.geteuid() and not (sticky_root or protected_root):
            raise ValueError("DATABASE_BACKUP_PATH_INVALID")
        if mode & 0o022 and not sticky_root:
            raise ValueError("DATABASE_BACKUP_PATH_INVALID")


def _check_source(source_path: Path) -> os.stat_result:
    _check_directory_chain(source_path.parent)
    try:
        info = source_path.lstat()
    except FileNotFoundError:
        raise ValueError("DATABASE_PATH_INVALID") from None
    if (stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid() or info.st_mode & 0o022):
        raise ValueError("DATABASE_PATH_INVALID")
    return info


def _same_source_inode(source_path: Path, initial: os.stat_result) -> None:
    current = _check_source(source_path)
    if (current.st_dev, current.st_ino) != (initial.st_dev, initial.st_ino):
        raise ValueError("DATABASE_PATH_CHANGED_DURING_BACKUP")


def _logical_digest(connection: sqlite3.Connection) -> str:
    """Hash schema and typed cell values without SQL text conversion.

    `iterdump()` and `quote()` are not suitable proof for arbitrary TEXT: SQL
    quote() truncates at embedded NUL. This stream uses explicit type and length
    framing for every cell, including exact UTF-8 and BLOB bytes.
    """
    digest = hashlib.sha256()
    def add(value: object) -> None:
        if value is None:
            kind, payload = b"N", b""
        elif isinstance(value, bool):
            kind, payload = b"I", (b"1" if value else b"0")
        elif isinstance(value, int):
            kind, payload = b"I", str(value).encode("ascii")
        elif isinstance(value, float):
            kind, payload = b"F", struct.pack("!d", value)
        elif isinstance(value, str):
            kind, payload = b"T", value.encode("utf-8", "surrogatepass")
        elif isinstance(value, bytes):
            kind, payload = b"B", value
        else:
            raise ValueError("DATABASE_BACKUP_UNSUPPORTED_CELL_TYPE")
        digest.update(kind)
        digest.update(struct.pack("!Q", len(payload)))
        digest.update(payload)

    add(connection.execute("PRAGMA user_version").fetchone()[0])
    add(connection.execute("PRAGMA application_id").fetchone()[0])

    def quote_identifier(identifier: str) -> str:
        return '"' + identifier.replace('"', '""') + '"'

    schema_rows = connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master "
        "ORDER BY type, name, tbl_name"
    )
    tables: list[str] = []
    for schema_row in schema_rows:
        object_type, name, table_name, sql = schema_row
        for value in schema_row:
            add(value)
        if object_type == "table":
            if isinstance(sql, str) and sql.lstrip().upper().startswith("CREATE VIRTUAL TABLE"):
                raise ValueError("DATABASE_BACKUP_UNSUPPORTED_VIRTUAL_TABLE")
            tables.append(str(name))

    for table_name in tables:
        add(table_name)
        table_info = connection.execute(
            f"PRAGMA table_xinfo({quote_identifier(table_name)})"
        ).fetchall()
        columns = [str(row[1]) for row in table_info if int(row[6]) != 1]
        if not columns:
            continue
        pk_columns = [
            str(row[1]) for row in sorted(
                (row for row in table_info if int(row[5]) > 0), key=lambda row: int(row[5])
            )
        ]
        table_list_row = next(
            (row for row in connection.execute("PRAGMA table_list") if str(row[1]) == table_name),
            None,
        )
        if table_list_row is None:
            raise ValueError("DATABASE_BACKUP_UNSUPPORTED_TABLE_LAYOUT")
        without_rowid = bool(int(table_list_row[4]))
        rowid_alias = None
        if not without_rowid:
            names = {str(row[1]).casefold() for row in table_info}
            rowid_alias = next((candidate for candidate in ("_rowid_", "rowid", "oid")
                                if candidate.casefold() not in names), None)
            if rowid_alias is None:
                raise ValueError("DATABASE_BACKUP_UNSUPPORTED_ROWID_LAYOUT")
        if without_rowid:
            if not pk_columns:
                raise ValueError("DATABASE_BACKUP_UNSUPPORTED_TABLE_LAYOUT")
            order_columns = pk_columns
        else:
            order_columns = [rowid_alias]
        select_columns = ", ".join(quote_identifier(name) for name in columns)
        ordering = ", ".join(quote_identifier(name) for name in order_columns)
        if rowid_alias:
            select_columns = f"{quote_identifier(rowid_alias)}, {select_columns}"
        query = (f"SELECT {select_columns} FROM {quote_identifier(table_name)} "
                 f"ORDER BY {ordering}")
        add(len(columns) + (1 if rowid_alias else 0))
        for row in connection.execute(query):
            add(len(row))
            for cell in row:
                add(cell)
    return digest.hexdigest()


def _validate_database(connection: sqlite3.Connection) -> None:
    integrity_rows = connection.execute("PRAGMA integrity_check").fetchall()
    if integrity_rows != [("ok",)]:
        raise ValueError("DATABASE_BACKUP_INTEGRITY_CHECK_FAILED")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise ValueError("DATABASE_BACKUP_FOREIGN_KEY_CHECK_FAILED")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                         | getattr(os, "O_CLOEXEC", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def create_sqlite_backup(source_path: Path, backup_dir: Path, *, prefix: str) -> dict[str, Any]:
    """Create, validate and atomically publish one unique standalone backup."""
    if (not prefix or "\x00" in prefix or "/" in prefix or "\\" in prefix
            or Path(prefix).name != prefix):
        raise ValueError("DATABASE_BACKUP_PREFIX_INVALID")
    source_path = Path(source_path)
    backup_dir = Path(backup_dir)
    source_info = _check_source(source_path)

    _check_directory_chain(backup_dir.parent)
    backup_dir_created = False
    try:
        backup_dir.mkdir(mode=0o700)
    except FileExistsError:
        pass
    else:
        backup_dir_created = True
    _check_directory_chain(backup_dir)
    backup_info = backup_dir.lstat()
    if (backup_info.st_uid != os.geteuid()
            or stat.S_IMODE(backup_info.st_mode) != 0o700):
        raise ValueError("DATABASE_BACKUP_DIRECTORY_INVALID")

    backup_identity = (backup_info.st_dev, backup_info.st_ino)
    temp_dir: Path | None = None
    temp_dir_identity: tuple[int, int] | None = None
    temp_path: Path | None = None
    final_path = backup_dir / f"{prefix}{secrets.token_hex(16)}.bak"
    published_identity: tuple[int, int] | None = None
    source: sqlite3.Connection | None = None
    destination: sqlite3.Connection | None = None
    try:
        temp_dir = backup_dir / f".backup-tmp-{secrets.token_hex(16)}"
        os.mkdir(temp_dir, 0o700)
        created_temp_info = temp_dir.lstat()
        temp_dir_identity = (created_temp_info.st_dev, created_temp_info.st_ino)
        temp_path = temp_dir / "database.sqlite"
        descriptor = os.open(
            temp_path,
            os.O_CREAT | os.O_EXCL | os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        os.close(descriptor)
        _check_directory_chain(backup_dir)
        current_backup_info = backup_dir.lstat()
        if (current_backup_info.st_dev, current_backup_info.st_ino) != backup_identity:
            raise ValueError("DATABASE_BACKUP_DIRECTORY_CHANGED")
        _same_source_inode(source_path, source_info)
        source_uri = source_path.absolute().as_uri() + "?mode=ro"
        source = sqlite3.connect(source_uri, uri=True, timeout=_BUSY_TIMEOUT_SECONDS)
        source.execute("PRAGMA query_only=ON")
        source.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MILLISECONDS}")
        # This read establishes the SQLite snapshot before hashing or copying.
        source.execute("BEGIN")
        source.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
        source_digest = _logical_digest(source)

        destination = sqlite3.connect(temp_path, timeout=_BUSY_TIMEOUT_SECONDS)
        destination.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MILLISECONDS}")
        destination.execute("PRAGMA journal_mode=DELETE").fetchone()
        deadline = time.monotonic() + _COPY_DEADLINE_SECONDS

        def progress(status: int, remaining: int, total: int) -> None:
            del status, remaining, total
            if time.monotonic() > deadline:
                raise TimeoutError("DATABASE_BACKUP_DEADLINE_EXCEEDED")

        source.backup(destination, pages=256, progress=progress, sleep=0.05)
        _validate_database(destination)
        if _logical_digest(destination) != source_digest:
            raise ValueError("DATABASE_BACKUP_CONTENT_MISMATCH")
        destination.commit()
        destination.close()
        destination = None
        source.close()
        source = None

        for suffix in ("-wal", "-shm"):
            if Path(f"{temp_path}{suffix}").exists():
                raise ValueError("DATABASE_BACKUP_NOT_STANDALONE")
        standalone = sqlite3.connect(
            temp_path.absolute().as_uri() + "?mode=ro&immutable=1",
            uri=True,
            timeout=_BUSY_TIMEOUT_SECONDS,
        )
        try:
            standalone.execute("PRAGMA query_only=ON")
            _validate_database(standalone)
            if _logical_digest(standalone) != source_digest:
                raise ValueError("DATABASE_BACKUP_CONTENT_MISMATCH")
        finally:
            standalone.close()

        _same_source_inode(source_path, source_info)
        temp_info = temp_path.lstat()
        if (stat.S_ISLNK(temp_info.st_mode) or not stat.S_ISREG(temp_info.st_mode)
                or temp_info.st_uid != os.geteuid()
                or stat.S_IMODE(temp_info.st_mode) != 0o600):
            raise ValueError("DATABASE_BACKUP_TEMP_INVALID")
        with temp_path.open("rb") as stream:
            os.fsync(stream.fileno())

        # hard-link is an atomic NO-REPLACE publish because source/destination
        # are in the same backup filesystem and the name is unique.
        _check_directory_chain(backup_dir)
        current_backup_info = backup_dir.lstat()
        if (current_backup_info.st_dev, current_backup_info.st_ino) != backup_identity:
            raise ValueError("DATABASE_BACKUP_DIRECTORY_CHANGED")
        published_identity = (temp_info.st_dev, temp_info.st_ino)
        os.link(temp_path, final_path, follow_symlinks=False)
        published = final_path.lstat()
        if (published.st_dev, published.st_ino) != (temp_info.st_dev, temp_info.st_ino):
            raise ValueError("DATABASE_BACKUP_PUBLICATION_CHANGED")
        published_identity = (published.st_dev, published.st_ino)
        _fsync_directory(backup_dir)
        if backup_dir_created:
            _fsync_directory(backup_dir.parent)

        digest = hashlib.sha256()
        _check_directory_chain(backup_dir)
        current_backup_info = backup_dir.lstat()
        if (current_backup_info.st_dev, current_backup_info.st_ino) != backup_identity:
            raise ValueError("DATABASE_BACKUP_DIRECTORY_CHANGED")
        descriptor = os.open(
            final_path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            final_info = os.fstat(descriptor)
            if (not stat.S_ISREG(final_info.st_mode) or final_info.st_uid != os.geteuid()
                    or stat.S_IMODE(final_info.st_mode) != 0o600
                    or (final_info.st_dev, final_info.st_ino) != published_identity):
                raise ValueError("DATABASE_BACKUP_PUBLICATION_CHANGED")
            with os.fdopen(os.dup(descriptor), "rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
            final_after = os.fstat(descriptor)
            path_after = final_path.lstat()
            if ((final_after.st_dev, final_after.st_ino) != published_identity
                    or (path_after.st_dev, path_after.st_ino) != published_identity
                    or not stat.S_ISREG(path_after.st_mode)):
                raise ValueError("DATABASE_BACKUP_PUBLICATION_CHANGED")
            _check_directory_chain(backup_dir)
            final_backup_info = backup_dir.lstat()
            if (final_backup_info.st_dev, final_backup_info.st_ino) != backup_identity:
                raise ValueError("DATABASE_BACKUP_DIRECTORY_CHANGED")
        finally:
            os.close(descriptor)
        return {
            "backup_path": str(final_path),
            "backup_sha256": digest.hexdigest(),
            "snapshot_sha256": source_digest,
            "source_snapshot": "sqlite_read_transaction",
        }
    except BaseException:
        if published_identity is not None:
            try:
                info = final_path.lstat()
                if (info.st_dev, info.st_ino) == published_identity:
                    final_path.unlink()
                    _fsync_directory(backup_dir)
                    if backup_dir_created:
                        _fsync_directory(backup_dir.parent)
            except OSError:
                pass
        raise
    finally:
        if destination is not None:
            destination.close()
        if source is not None:
            source.close()
        # This directory is private and uniquely created by this call. Removal
        # is best effort; SIGKILL can leave only an unadvertised private orphan.
        if temp_dir is not None and temp_dir_identity is not None:
            try:
                cleanup_info = temp_dir.lstat()
                if (stat.S_ISDIR(cleanup_info.st_mode)
                        and (cleanup_info.st_dev, cleanup_info.st_ino) == temp_dir_identity):
                    shutil.rmtree(temp_dir)
            except OSError:
                pass
