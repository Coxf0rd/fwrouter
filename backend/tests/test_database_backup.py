from __future__ import annotations

import errno
import hashlib
import os
from pathlib import Path
import sqlite3
import threading
import time

import pytest

from _test_support import configure_test_state_dir, offline_maintenance_test_lease
from fwrouter_api.db.connection import get_db_path
from fwrouter_api.services import database_backup
from fwrouter_api.services import database_admin
from fwrouter_api.services.database_admin import backup_database_file


def _configure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    configure_test_state_dir(monkeypatch, tmp_path)
    return get_db_path()


def _open(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA wal_autocheckpoint=0")
    return connection


def test_backup_includes_committed_wal_rows_and_full_logical_content(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    db_path = _configure(monkeypatch, tmp_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    writer = _open(db_path)
    writer.executescript(
        """
        PRAGMA user_version=27;
        PRAGMA application_id=1179996756;
        CREATE TABLE stable_rowids(payload TEXT);
        INSERT INTO stable_rowids(rowid, payload) VALUES (91, 'stable-row');
        CREATE TABLE credentials(id INTEGER PRIMARY KEY, secret TEXT NOT NULL);
        CREATE TABLE overrides(id INTEGER PRIMARY KEY, credential_id INTEGER NOT NULL
            REFERENCES credentials(id), payload TEXT NOT NULL);
        CREATE TABLE opaque_values(id INTEGER PRIMARY KEY, text_value TEXT, blob_value BLOB);
        CREATE TABLE without_rowid_values(key_a TEXT, key_b INTEGER, payload BLOB,
            PRIMARY KEY(key_a, key_b)) WITHOUT ROWID;
        INSERT INTO credentials VALUES (1, 'fixture-secret');
        INSERT INTO overrides VALUES (1, 1, 'first');
        INSERT INTO overrides VALUES (2, 1, 'second');
        INSERT INTO opaque_values VALUES (1, 'prefix' || char(0) || 'before', x'410042');
        INSERT INTO without_rowid_values VALUES ('a', 1, x'00ff');
        INSERT INTO without_rowid_values VALUES ('b', 2, x'ff00');
        """
    )
    writer.commit()
    source_bytes_before = db_path.read_bytes()
    wal_path = Path(f"{db_path}-wal")
    wal_bytes_before = wal_path.read_bytes()

    # A raw main-file copy does not contain these committed rows while the
    # writer connection holds the uncheckpointed WAL open.
    main_only = tmp_path / "main-only.sqlite"
    main_only.write_bytes(db_path.read_bytes())
    with sqlite3.connect(main_only) as stale:
        assert stale.execute("SELECT name FROM sqlite_master WHERE name='credentials'").fetchone() is None

    result = backup_database_file()
    backup_path = Path(result["backup_path"])
    immutable = sqlite3.connect(
        backup_path.as_uri() + "?mode=ro&immutable=1", uri=True,
    )
    try:
        assert immutable.execute("SELECT COUNT(*) FROM overrides").fetchone() == (2,)
    finally:
        immutable.close()
    assert db_path.read_bytes() == source_bytes_before
    assert wal_path.read_bytes() == wal_bytes_before
    assert result["ok"] is True and result["created"] is True
    assert result["backup_sha256"] == hashlib.sha256(backup_path.read_bytes()).hexdigest()
    assert result["snapshot_sha256"] == database_backup._logical_digest(writer)
    assert result["source_snapshot"] == "sqlite_read_transaction"
    assert backup_path.stat().st_uid == os.geteuid()
    assert (backup_path.parent.stat().st_mode & 0o777) == 0o700
    assert (backup_path.stat().st_mode & 0o777) == 0o600
    with sqlite3.connect(backup_path) as backup:
        assert backup.execute("PRAGMA user_version").fetchone() == (27,)
        assert backup.execute("PRAGMA application_id").fetchone() == (1179996756,)
        assert backup.execute("SELECT rowid, payload FROM stable_rowids").fetchall() == [(91, "stable-row")]
        assert backup.execute("SELECT secret FROM credentials").fetchall() == [("fixture-secret",)]
        assert backup.execute("SELECT payload FROM overrides ORDER BY id").fetchall() == [
            ("first",), ("second",),
        ]
        assert backup.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert backup.execute("PRAGMA foreign_key_check").fetchall() == []
        assert backup.execute(
            "SELECT key_a, key_b, payload FROM without_rowid_values ORDER BY key_a, key_b"
        ).fetchall() == [("a", 1, b"\x00\xff"), ("b", 2, b"\xff\x00")]
        assert backup.execute("SELECT text_value, blob_value FROM opaque_values").fetchone() == (
            "prefix\x00before", b"A\x00B",
        )
    writer.close()


def test_backup_is_pinned_before_a_concurrent_commit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    db_path = _configure(monkeypatch, tmp_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    writer = _open(db_path)
    writer.execute("CREATE TABLE records(value TEXT PRIMARY KEY)")
    writer.execute("INSERT INTO records VALUES ('snapshot')")
    writer.commit()

    original_digest = database_backup._logical_digest
    committed = False

    def digest_with_commit(connection: sqlite3.Connection) -> str:
        nonlocal committed
        result = original_digest(connection)
        if not committed:
            committed = True
            concurrent = sqlite3.connect(db_path, timeout=2)
            try:
                concurrent.execute("INSERT INTO records VALUES ('later')")
                concurrent.commit()
            finally:
                concurrent.close()
        return result

    monkeypatch.setattr(database_backup, "_logical_digest", digest_with_commit)
    result = backup_database_file()
    with sqlite3.connect(result["backup_path"]) as backup:
        assert backup.execute("SELECT value FROM records ORDER BY value").fetchall() == [("snapshot",)]
    with sqlite3.connect(db_path) as live:
        assert live.execute("SELECT value FROM records ORDER BY value").fetchall() == [
            ("later",), ("snapshot",),
        ]
    writer.close()


def test_backup_snapshot_excludes_commit_during_page_copy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    db_path = _configure(monkeypatch, tmp_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    writer = _open(db_path)
    writer.execute("CREATE TABLE bulk_records(id INTEGER PRIMARY KEY, payload BLOB)")
    payload = b"z" * 4096
    writer.executemany(
        "INSERT INTO bulk_records(payload) VALUES (?)", ((payload,) for _ in range(9000)),
    )
    writer.commit()
    later_commit_done = threading.Event()
    errors: list[BaseException] = []
    expected_size = 9000 * 4096

    def commit_after_copy_starts() -> None:
        deadline = time.monotonic() + 10
        try:
            while time.monotonic() < deadline:
                candidates = list((db_path.parent / "backups").glob(
                    ".backup-tmp-*/database.sqlite"
                ))
                if candidates and candidates[0].stat().st_size >= 256 * 1024:
                    # The published snapshot still has most bulk pages left to
                    # copy when this independent WAL writer commits.
                    if candidates[0].stat().st_size >= expected_size // 2:
                        raise AssertionError("copy watcher observed only its final half")
                    with sqlite3.connect(db_path, timeout=2) as concurrent:
                        concurrent.execute(
                            "INSERT INTO bulk_records(payload) VALUES (?)", (b"later",),
                        )
                    later_commit_done.set()
                    return
                time.sleep(0.001)
            raise TimeoutError("backup copy did not expose a growing temporary file")
        except BaseException as exc:
            errors.append(exc)

    watcher = threading.Thread(target=commit_after_copy_starts, daemon=True)
    watcher.start()
    result = backup_database_file()
    watcher.join(timeout=10)
    writer.close()
    assert not watcher.is_alive()
    assert errors == []
    assert later_commit_done.is_set()
    with sqlite3.connect(result["backup_path"]) as backup:
        assert backup.execute("SELECT COUNT(*) FROM bulk_records").fetchone() == (9000,)
    with sqlite3.connect(db_path) as current:
        assert current.execute("SELECT COUNT(*) FROM bulk_records").fetchone() == (9001,)


def test_backup_rejects_foreign_key_violations_and_cleans_temp(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    db_path = _configure(monkeypatch, tmp_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            PRAGMA foreign_keys=OFF;
            CREATE TABLE parent(id INTEGER PRIMARY KEY);
            CREATE TABLE child(parent_id INTEGER REFERENCES parent(id));
            INSERT INTO child VALUES (99);
            """
        )
    with pytest.raises(ValueError, match="DATABASE_BACKUP_FOREIGN_KEY_CHECK_FAILED"):
        backup_database_file()
    backup_dir = db_path.parent / "backups"
    assert list(backup_dir.glob("*.bak")) == []
    assert list(backup_dir.glob(".backup-tmp-*")) == []


def test_backup_rejects_unsafe_backup_directory_without_chmodding_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    db_path = _configure(monkeypatch, tmp_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE sample(value TEXT)")
    backup_dir = db_path.parent / "backups"
    backup_dir.mkdir(mode=0o755)
    backup_dir.chmod(0o755)
    with pytest.raises(ValueError, match="DATABASE_BACKUP_PATH_INVALID|DATABASE_BACKUP_DIRECTORY_INVALID"):
        backup_database_file()
    assert (backup_dir.stat().st_mode & 0o777) == 0o755
    assert list(backup_dir.iterdir()) == []


def test_repeated_backups_publish_distinct_files_and_preserve_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    db_path = _configure(monkeypatch, tmp_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE sample(value TEXT)")
        connection.execute("INSERT INTO sample VALUES ('unchanged')")
    before = db_path.stat()
    first = backup_database_file()
    second = backup_database_file()
    assert first["backup_path"] != second["backup_path"]
    assert Path(first["backup_path"]).exists() and Path(second["backup_path"]).exists()
    after = db_path.stat()
    assert (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) == (
        after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
    )


@pytest.mark.parametrize("tamper_sql", [
    "UPDATE values_table SET value='same' || char(0) || 'right'",
    "UPDATE values_table SET rowid=17",
    "PRAGMA user_version=99",
    "PRAGMA application_id=99",
])
def test_valid_but_altered_backup_fails_full_content_comparison(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tamper_sql: str,
) -> None:
    db_path = _configure(monkeypatch, tmp_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE values_table(value TEXT)")
        connection.execute("INSERT INTO values_table VALUES (?)", ("same\x00left",))

    validate = database_backup._validate_database

    def validate_then_alter(destination: sqlite3.Connection) -> None:
        validate(destination)
        destination.execute(tamper_sql)
        destination.commit()

    monkeypatch.setattr(database_backup, "_validate_database", validate_then_alter)
    with pytest.raises(ValueError, match="DATABASE_BACKUP_CONTENT_MISMATCH"):
        backup_database_file()
    backup_dir = db_path.parent / "backups"
    assert list(backup_dir.glob("*.bak")) == []
    assert list(backup_dir.glob(".backup-tmp-*")) == []


def test_truncated_online_backup_is_rejected_and_cleaned(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    db_path = _configure(monkeypatch, tmp_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE large_payload(value BLOB)")
        connection.executemany(
            "INSERT INTO large_payload VALUES (?)", ((b"q" * 2048,) for _ in range(128)),
        )

    original_connect = sqlite3.connect

    class TruncatingConnection(sqlite3.Connection):
        def backup(self, destination: sqlite3.Connection, **kwargs: object) -> None:
            super().backup(destination, **kwargs)
            filename = destination.execute("PRAGMA database_list").fetchone()[2]
            destination.execute("PRAGMA shrink_memory")
            Path(filename).write_bytes(Path(filename).read_bytes()[:100])
            destination.execute("PRAGMA shrink_memory")

    def instrumented_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        kwargs.setdefault("factory", TruncatingConnection)
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(database_backup.sqlite3, "connect", instrumented_connect)
    with pytest.raises((sqlite3.DatabaseError, ValueError)):
        backup_database_file()
    backup_dir = db_path.parent / "backups"
    assert list(backup_dir.glob("*.bak")) == []
    assert list(backup_dir.glob(".backup-tmp-*")) == []


def test_backup_failure_aborts_rebuild_before_source_unlink_or_import(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from fwrouter_api.db.connection import initialize_database

    db_path = _configure(monkeypatch, tmp_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    initialize_database()
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            "INSERT INTO settings(key,value_json,updated_at) VALUES ('test.restore.marker','\"preserve-me\"','test')",
        )
        connection.commit()
    finally:
        connection.close()
    source = {"ok": True, "snapshot": {"fixture": True}, "source": {"kind": "test"}}
    monkeypatch.setattr(database_admin, "resolve_control_plane_snapshot_source", lambda **_kwargs: source)

    def fail_backup(**_kwargs) -> dict[str, object]:
        raise OSError("injected backup I/O failure")

    monkeypatch.setattr(database_admin, "backup_database_file", fail_backup)
    monkeypatch.setattr(database_admin, "initialize_database", lambda: pytest.fail("database recreated"))
    monkeypatch.setattr(database_admin, "import_control_plane_snapshot", lambda *_args, **_kwargs: pytest.fail("snapshot imported"))
    with offline_maintenance_test_lease(monkeypatch):
        with pytest.raises(OSError, match="injected backup I/O failure"):
            database_admin.rebuild_control_plane_database()
    assert db_path.exists()
    connection = sqlite3.connect(db_path)
    try:
        assert connection.execute(
            "SELECT value_json FROM settings WHERE key='test.restore.marker'",
        ).fetchall() == [("\"preserve-me\"",)]
    finally:
        connection.close()


@pytest.mark.no_database_autoinit
def test_source_and_backup_directory_symlinks_are_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    db_path = _configure(monkeypatch, tmp_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    real_db = tmp_path / "real.sqlite"
    with sqlite3.connect(real_db) as connection:
        connection.execute("CREATE TABLE sample(value TEXT)")
    db_path.symlink_to(real_db)
    with pytest.raises(ValueError, match="DATABASE_PATH_INVALID"):
        backup_database_file()
    db_path.unlink()
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE sample(value TEXT)")
    backup_dir = db_path.parent / "backups"
    target_dir = tmp_path / "target-backups"
    target_dir.mkdir(mode=0o700)
    backup_dir.symlink_to(target_dir, target_is_directory=True)
    with pytest.raises(ValueError, match="DATABASE_BACKUP_PATH_INVALID"):
        backup_database_file()
    assert list(target_dir.iterdir()) == []


@pytest.mark.no_database_autoinit
def test_replacement_of_source_path_after_snapshot_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    db_path = _configure(monkeypatch, tmp_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE original(value TEXT)")
        connection.execute("INSERT INTO original VALUES ('preserved')")
    original_digest = database_backup._logical_digest
    replaced = False

    def replace_after_pin(connection: sqlite3.Connection) -> str:
        nonlocal replaced
        result = original_digest(connection)
        if not replaced:
            replaced = True
            db_path.rename(db_path.with_suffix(".original"))
            with sqlite3.connect(db_path) as replacement:
                replacement.execute("CREATE TABLE substituted(value TEXT)")
        return result

    monkeypatch.setattr(database_backup, "_logical_digest", replace_after_pin)
    with pytest.raises(ValueError, match="DATABASE_PATH_CHANGED_DURING_BACKUP"):
        backup_database_file()
    backup_dir = db_path.parent / "backups"
    assert list(backup_dir.glob("*.bak")) == []
    assert list(backup_dir.glob(".backup-tmp-*")) == []


@pytest.mark.parametrize(
    ("failure_stage", "error"),
    [
        ("publish", PermissionError(errno.EACCES, "injected EACCES")),
        ("file_fsync", OSError(errno.ENOSPC, "injected ENOSPC")),
    ],
)
def test_permission_and_space_failures_leave_no_published_backup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure_stage: str,
    error: OSError,
) -> None:
    db_path = _configure(monkeypatch, tmp_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE sample(value TEXT)")
    if failure_stage == "publish":
        monkeypatch.setattr(database_backup.os, "link", lambda *_args, **_kwargs: (_ for _ in ()).throw(error))
    else:
        monkeypatch.setattr(database_backup.os, "fsync", lambda *_args, **_kwargs: (_ for _ in ()).throw(error))
    with pytest.raises(type(error), match="injected"):
        backup_database_file()
    backup_dir = db_path.parent / "backups"
    assert list(backup_dir.glob("*.bak")) == []
    assert list(backup_dir.glob(".backup-tmp-*")) == []


def test_temp_file_creation_permission_failure_cleans_private_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    db_path = _configure(monkeypatch, tmp_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE sample(value TEXT)")
    original_open = database_backup.os.open

    def deny_temp_file(path: object, *args: object, **kwargs: object) -> int:
        if Path(path).name == "database.sqlite":
            raise PermissionError(errno.EACCES, "injected EACCES during temp creation")
        return original_open(path, *args, **kwargs)

    with monkeypatch.context() as patcher:
        patcher.setattr(database_backup.os, "open", deny_temp_file)
        with pytest.raises(PermissionError, match="injected EACCES"):
            backup_database_file()
    backup_dir = db_path.parent / "backups"
    assert list(backup_dir.glob("*.bak")) == []
    assert list(backup_dir.glob(".backup-tmp-*")) == []


def test_no_replace_collision_preserves_existing_backup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    db_path = _configure(monkeypatch, tmp_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE sample(value TEXT)")
        connection.execute("INSERT INTO sample VALUES ('original')")
    fixed_stamp = "20261028T120000Z"
    monkeypatch.setattr(database_admin, "_utc_stamp", lambda: fixed_stamp)
    tokens = iter(("same-final", "first-temp", "same-final", "second-temp"))
    monkeypatch.setattr(database_backup.secrets, "token_hex", lambda _length: next(tokens))
    first = backup_database_file()
    first_path = Path(first["backup_path"])
    original_bytes = first_path.read_bytes()
    original_identity = first_path.stat().st_dev, first_path.stat().st_ino
    expected_path = db_path.parent / "backups" / f"{db_path.name}.{fixed_stamp}.same-final.bak"
    assert first_path == expected_path
    with pytest.raises(FileExistsError):
        backup_database_file()
    assert first_path.read_bytes() == original_bytes
    first_after = first_path.stat()
    assert (first_after.st_dev, first_after.st_ino) == original_identity
    assert list((db_path.parent / "backups").glob(".backup-tmp-*")) == []


def test_temp_directory_name_collision_preserves_preexisting_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    db_path = _configure(monkeypatch, tmp_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE sample(value TEXT)")
    backup_dir = db_path.parent / "backups"
    backup_dir.mkdir(mode=0o700)
    collision_dir = backup_dir / ".backup-tmp-collision"
    collision_dir.mkdir(mode=0o700)
    sentinel = collision_dir / "sentinel"
    sentinel.write_text("keep", encoding="utf-8")
    tokens = iter(("final-collision", "collision"))
    monkeypatch.setattr(database_backup.secrets, "token_hex", lambda _length: next(tokens))

    with pytest.raises(FileExistsError):
        backup_database_file()

    assert sentinel.read_text(encoding="utf-8") == "keep"


@pytest.mark.no_database_autoinit
def test_sqlite_source_busy_failure_respects_configured_bounded_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    db_path = _configure(monkeypatch, tmp_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as setup:
        setup.execute("PRAGMA journal_mode=DELETE")
        setup.execute("CREATE TABLE sample(value TEXT)")
    lock = sqlite3.connect(db_path, timeout=1)
    lock.execute("BEGIN EXCLUSIVE")
    lock.execute("INSERT INTO sample VALUES ('locked')")
    monkeypatch.setattr(database_backup, "_BUSY_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(database_backup, "_BUSY_TIMEOUT_MILLISECONDS", 50)
    started = time.monotonic()
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            backup_database_file()
    finally:
        lock.rollback()
        lock.close()
    assert time.monotonic() - started < 1
    backup_dir = db_path.parent / "backups"
    assert list(backup_dir.glob("*.bak")) == []
    assert list(backup_dir.glob(".backup-tmp-*")) == []


def test_interruption_and_publication_fsync_failure_remove_owned_artifacts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    db_path = _configure(monkeypatch, tmp_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE sample(value TEXT)")

    def interrupt(_connection: sqlite3.Connection) -> None:
        raise KeyboardInterrupt

    with monkeypatch.context() as patcher:
        patcher.setattr(database_backup, "_validate_database", interrupt)
        with pytest.raises(KeyboardInterrupt):
            backup_database_file()
    backup_dir = db_path.parent / "backups"
    assert list(backup_dir.glob("*.bak")) == []
    assert list(backup_dir.glob(".backup-tmp-*")) == []

    with monkeypatch.context() as patcher:
        patcher.setattr(
            database_backup,
            "_fsync_directory",
            lambda _path: (_ for _ in ()).throw(OSError("injected fsync failure")),
        )
        with pytest.raises(OSError, match="injected fsync failure"):
            backup_database_file()
    assert list(backup_dir.glob("*.bak")) == []
    assert list(backup_dir.glob(".backup-tmp-*")) == []


@pytest.mark.no_database_autoinit
def test_missing_database_keeps_original_success_contract(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    db_path = _configure(monkeypatch, tmp_path)
    result = backup_database_file()
    assert result == {
        "ok": True,
        "created": False,
        "backup_path": None,
        "db_path": str(db_path),
    }


def test_typed_logical_digest_detects_same_row_count_nul_and_blob_changes(
    tmp_path: Path,
) -> None:
    first_path = tmp_path / "typed-first.sqlite"
    second_path = tmp_path / "typed-second.sqlite"
    for path, text_value, blob_value in (
        (first_path, "same\x00left", b"x\x00left"),
        (second_path, "same\x00right", b"x\x00right"),
    ):
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE values_table(value TEXT, payload BLOB)")
            connection.execute(
                "INSERT INTO values_table VALUES (?, ?)", (text_value, blob_value),
            )
    with sqlite3.connect(first_path) as first, sqlite3.connect(second_path) as second:
        assert first.execute("SELECT COUNT(*) FROM values_table").fetchone() == (1,)
        assert second.execute("SELECT COUNT(*) FROM values_table").fetchone() == (1,)
        assert database_backup._logical_digest(first) != database_backup._logical_digest(second)


def test_parent_traversal_is_rejected_before_filesystem_changes(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE stable(value TEXT)")
    branch = tmp_path / "branch"
    leaf = branch / "leaf"
    leaf.mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(leaf, target_is_directory=True)
    destination = tmp_path / "alias" / ".." / "backups"
    with pytest.raises(ValueError, match="DATABASE_BACKUP_PATH_INVALID"):
        database_backup.create_sqlite_backup(source, destination, prefix="fixture.")
    assert not (tmp_path / "backups").exists()
    assert not (branch / "backups").exists()
