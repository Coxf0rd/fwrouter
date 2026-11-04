from __future__ import annotations

import hashlib
import os
import re
import sqlite3
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest

from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import (
    connect, get_schema_path, initialize_database, inspect_existing_database_schema,
)
from fwrouter_api.db import connection as db_connection
from fwrouter_api.db.schema_state import EXPECTED_SCHEMA_VERSION, inspect_database_schema


def _retarget(monkeypatch: pytest.MonkeyPatch, root: Path) -> Path:
    monkeypatch.setenv("FWROUTER_STATE_DIR", str(root))
    get_settings.cache_clear()
    return get_settings().paths.db_path


def _install_known_legacy_schema27_profile(
    path: Path, *, origin: int | str, tables_override: tuple[str, ...] | None = None,
) -> None:
    """Build the reviewed baseline 7/10/11→27 DDL profile without migrations."""
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=OFF")
        tables = list(tables_override or ("routing_global_state", "server_preferences"))
        if tables_override is None and origin == 7:
            tables.extend(("modules", "server_custom_https_proxy"))
        for table in tables:
            sql = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,),
            ).fetchone()[0]
            if table == "routing_global_state":
                for column in (
                    "desired_fixed_server_id", "applied_fixed_server_id", "active_auto_server_id",
                ):
                    sql = re.sub(
                        rf",\s*FOREIGN KEY\s*\(\s*{column}\s*\)"
                        r"\s*REFERENCES\s*servers\s*\(\s*server_id\s*\)"
                        r"\s*ON DELETE SET NULL",
                        "", sql, flags=re.IGNORECASE,
                    )
            if table == "server_preferences":
                sql = re.sub(
                    r",\s*CHECK\s*\(\s*vpn_auto_priority_origin\s+IN\s*"
                    r"\(\s*'auto'\s*,\s*'manual'\s*,\s*'legacy'\s*\)\s*\)",
                    "", sql, flags=re.IGNORECASE,
                )
                sql = re.sub(
                    r"(vpn_auto_priority_origin\s+TEXT\s+NOT NULL\s+DEFAULT\s+'legacy')",
                    r"\1 CHECK (vpn_auto_priority_origin IN ('auto', 'manual', 'legacy'))",
                    sql, flags=re.IGNORECASE,
                )
                if origin == 7:
                    sql = re.sub(
                        r",\s*CHECK\s*\(\s*vpn_auto_priority\s*>=\s*-1\s*"
                        r"AND\s*vpn_auto_priority\s*<=\s*5\s*\)",
                        "", sql, flags=re.IGNORECASE,
                    )
                    sql = re.sub(
                        r"(vpn_auto_priority\s+INTEGER\s+NOT NULL\s+DEFAULT\s+0)",
                        r"\1 CHECK (vpn_auto_priority >= -1 AND vpn_auto_priority <= 5)",
                        sql, flags=re.IGNORECASE,
                    )
            if origin == 7 and table == "modules":
                sql = re.sub(
                    r",\s*CHECK\s*\(\s*lifecycle_mode\s+IN\s*"
                    r"\(\s*'none'\s*,\s*'managed'\s*,\s*'external'\s*\)\s*\)",
                    "", sql, flags=re.IGNORECASE,
                )
                sql = re.sub(
                    r"(lifecycle_mode\s+TEXT\s+NOT NULL\s+DEFAULT\s+'none')",
                    r"\1 CHECK (lifecycle_mode IN ('none', 'managed', 'external'))",
                    sql, flags=re.IGNORECASE,
                )
            if origin == 7 and table == "server_custom_https_proxy":
                sql = re.sub(
                    r",\s*CHECK\s*\(\s*proxy_type\s+IN\s*"
                    r"\(\s*'http'\s*,\s*'socks5'\s*\)\s*\)",
                    "", sql, flags=re.IGNORECASE,
                )
                sql = re.sub(
                    r"(proxy_type\s+TEXT\s+NOT NULL\s+DEFAULT\s+'http')",
                    r"\1 CHECK (proxy_type IN ('http', 'socks5'))",
                    sql, flags=re.IGNORECASE,
                )
            if origin == "deployment" and table == "server_ping_state":
                sql = re.sub(
                    r",\s*CHECK\s*\(\s*manual_status\s+IS\s+NULL\s+OR\s+"
                    r"manual_status\s+IN\s*\(\s*'unknown'\s*,\s*'success'\s*,\s*"
                    r"'failed'\s*,\s*'skipped'\s*\)\s*\)",
                    "", sql, flags=re.IGNORECASE,
                )
                assert "CHECK (manual_status" not in sql
            legacy_name = f"{table}_legacy_profile"
            sql = re.sub(rf"^CREATE TABLE\s+{table}\b", f"CREATE TABLE {legacy_name}",
                         sql, flags=re.IGNORECASE)
            columns = [str(row["name"]) for row in connection.execute(f'PRAGMA table_info("{table}")')]
            quoted_columns = ", ".join('"' + name.replace('"', '""') + '"' for name in columns)
            connection.execute(sql)
            connection.execute(
                f'INSERT INTO "{legacy_name}" ({quoted_columns}) '
                f'SELECT {quoted_columns} FROM "{table}"'
            )
            connection.execute(f'DROP TABLE "{table}"')
            connection.execute(f'ALTER TABLE "{legacy_name}" RENAME TO "{table}"')
        connection.commit()
        connection.executescript(get_schema_path().read_text(encoding="utf-8"))


def _install_deployment_legacy_schema27_profile(path: Path) -> None:
    """Compose the observed 7→8 and 13→15 ALTER-addition DDL on a canonical fixture."""
    _install_known_legacy_schema27_profile(
        path, origin=10, tables_override=("server_preferences",),
    )
    _install_known_legacy_schema27_profile(
        path, origin=7, tables_override=("modules", "server_custom_https_proxy"),
    )
    _install_known_legacy_schema27_profile(
        path, origin="deployment", tables_override=("server_ping_state",),
    )


@pytest.mark.no_database_autoinit
@pytest.mark.parametrize("mutation", ["isolated_check", "hybrid", "unique_index", "trigger", "foreign_key"])
def test_additive_manual_ping_profile_rejects_unreviewed_near_misses(monkeypatch, tmp_path, mutation):
    path = _retarget(monkeypatch, tmp_path / "state")
    assert initialize_database()["ok"]
    if mutation == "isolated_check":
        _install_known_legacy_schema27_profile(
            path, origin="deployment", tables_override=("server_ping_state",),
        )
    else:
        _install_deployment_legacy_schema27_profile(path)
        assert inspect_existing_database_schema()["ok"]
        with sqlite3.connect(path) as connection:
            connection.execute("PRAGMA foreign_keys=OFF")
            if mutation == "unique_index":
                connection.execute("CREATE UNIQUE INDEX extra_unique ON server_preferences(updated_at)")
            elif mutation == "trigger":
                connection.execute("CREATE TRIGGER extra_trigger AFTER INSERT ON server_ping_state BEGIN SELECT 1; END")
            else:
                with sqlite3.connect(":memory:") as canonical:
                    canonical.executescript(get_schema_path().read_text())
                    ddl = canonical.execute(
                        "SELECT sql FROM sqlite_master WHERE name='modules'"
                    ).fetchone()[0]
                table = "modules"
                if mutation == "foreign_key":
                    table = "server_ping_state"
                    ddl = connection.execute("SELECT sql FROM sqlite_master WHERE name=?", (table,)).fetchone()[0]
                    ddl = re.sub(r",\s*FOREIGN KEY\s*\(server_id\)\s*REFERENCES servers\(server_id\) ON DELETE CASCADE", "", ddl, flags=re.I)
                connection.execute(f'DROP TABLE "{table}"')
                connection.execute(ddl)
    before = path.read_bytes()
    assert inspect_existing_database_schema()["ok"] is False
    assert initialize_database()["ok"] is False
    with pytest.raises(sqlite3.DatabaseError, match="^FWROUTER_DATABASE_SCHEMA_UNSUPPORTED$"):
        with connect():
            pytest.fail("unsupported schema must not admit a writer")
    assert path.read_bytes() == before


@pytest.mark.no_database_autoinit
def test_explicit_initialization_publishes_complete_current_schema(monkeypatch, tmp_path: Path) -> None:
    path = _retarget(monkeypatch, tmp_path / "state")
    assert not path.exists()

    state = initialize_database()

    assert state["ok"] is True
    assert state["actual_schema_version"] == EXPECTED_SCHEMA_VERSION
    assert path.is_file()
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        assert len(tables) == 46, sorted(tables)
        assert "external_connection_migrations" in tables


@pytest.mark.no_database_autoinit
def test_existing_current_database_is_observed_without_reseeding_or_rewriting(monkeypatch, tmp_path: Path) -> None:
    path = _retarget(monkeypatch, tmp_path / "state")
    assert initialize_database()["ok"] is True
    with sqlite3.connect(path) as connection:
        connection.execute("DELETE FROM settings WHERE key='vpn_auto_priority'")
        connection.execute("INSERT INTO settings(key, value_json) VALUES ('test-owned-row', '{\"v\":1}')")
        connection.commit()
    before = hashlib.sha256(path.read_bytes()).hexdigest()

    state = initialize_database()

    assert state["ok"] is True
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT 1 FROM settings WHERE key='vpn_auto_priority'").fetchone() is None
        assert connection.execute("SELECT value_json FROM settings WHERE key='test-owned-row'").fetchone()[0] == '{"v":1}'


@pytest.mark.no_database_autoinit
@pytest.mark.parametrize(
    ("origin", "profile"),
    [
        (7, "legacy_migrated_schema27_from_7"),
        (10, "legacy_migrated_schema27_from_10_or_11"),
        ("deployment", "legacy_migrated_schema27_manual_ping_additive"),
    ],
)
def test_known_historical_schema27_profiles_are_existing_only_and_fail_closed_for_unknown_hybrids(
    monkeypatch, tmp_path: Path, origin: int | str, profile: str, capsys,
) -> None:
    path = _retarget(monkeypatch, tmp_path / "state")
    assert initialize_database()["ok"] is True
    if origin == "deployment":
        _install_deployment_legacy_schema27_profile(path)
    else:
        _install_known_legacy_schema27_profile(path, origin=origin)

    compatible = inspect_existing_database_schema()
    assert compatible["ok"] is True
    assert compatible["status"] == "ok"
    assert compatible["compatibility_profile"] == profile
    assert compatible["compatibility_notes"]
    from fwrouter_api.db.schema_state import summarize_schema_state
    assert summarize_schema_state(compatible)["status"] == "ok"
    assert summarize_schema_state(compatible)["compatibility_profile"] == profile
    from fwrouter_api.routes.system import health
    health_result = health()
    assert health_result.ok is True
    assert health_result.data["database"]["schema"]["status"] == "ok"
    assert health_result.data["database"]["schema"]["compatibility_profile"] == profile
    from fwrouter_api.services.diagnostics import _check_database
    diagnostic_database, _diagnostic_problems = _check_database()
    assert diagnostic_database["status"] == "healthy"
    assert diagnostic_database["schema"]["status"] == "ok"
    assert diagnostic_database["schema"]["compatibility_profile"] == profile
    from fwrouter_api.services.database_admin import (
        _require_canonical_rebuild_schema, _validate_database_file,
    )
    assert _validate_database_file(path)["compatibility_profile"] == profile
    from fwrouter_api.services.database_backup import create_sqlite_backup
    backup_dir = tmp_path / "backups"
    receipt = create_sqlite_backup(path, backup_dir, prefix="historical-profile.")
    backup_path = Path(receipt["backup_path"])
    assert receipt["source_snapshot"] == "sqlite_read_transaction"
    assert _validate_database_file(backup_path)["compatibility_profile"] == profile
    with pytest.raises(ValueError, match="DATABASE_REBUILD_DATABASE_VALIDATION_FAILED"):
        _validate_database_file(path, allow_legacy_schema27_profiles=False)
    canonical_candidate = tmp_path / "canonical-candidate.sqlite"
    with sqlite3.connect(canonical_candidate) as candidate:
        candidate.executescript(get_schema_path().read_text(encoding="utf-8"))
    canonical_candidate.chmod(0o600)
    with pytest.raises(ValueError, match="DATABASE_REBUILD_SCHEMA_OBJECTS_UNSUPPORTED"):
        _require_canonical_rebuild_schema(path, canonical_candidate)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE INDEX idx_profile_nonunique_test ON server_preferences(updated_at)"
        )
    assert inspect_existing_database_schema()["compatibility_profile"] == profile
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        assert inspect_database_schema(
            connection, allow_legacy_schema27_profiles=False,
        )["ok"] is False
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    assert initialize_database()["ok"] is True
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    with connect() as connection:
        assert connection.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()[0] == "27"
    from fwrouter_api.cli import main
    before_cli = hashlib.sha256(path.read_bytes()).hexdigest()
    assert main(["database", "upgrade", "--expected-schema", "27"]) == 1
    assert "DATABASE_SCHEMA_PREFLIGHT_FAILED" in capsys.readouterr().out
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before_cli

    if origin == 7:
        # Restoring only one canonical table creates a hybrid not observed in
        # any baseline migration route; exact mismatch-set matching rejects it.
        with sqlite3.connect(path) as connection:
            connection.row_factory = sqlite3.Row
            with sqlite3.connect(":memory:") as canonical_connection:
                canonical_connection.executescript(get_schema_path().read_text(encoding="utf-8"))
                canonical = canonical_connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name='routing_global_state'"
                ).fetchone()[0]
            columns = [row["name"] for row in connection.execute(
                'PRAGMA table_info("routing_global_state")'
            )]
            connection.execute("CREATE TABLE routing_global_state_replacement AS SELECT * FROM routing_global_state")
            connection.execute("DROP TABLE routing_global_state")
            connection.execute(canonical)
            quoted = ", ".join('"' + name + '"' for name in columns)
            connection.execute(
                f"INSERT INTO routing_global_state ({quoted}) "
                f"SELECT {quoted} FROM routing_global_state_replacement"
            )
            connection.execute("DROP TABLE routing_global_state_replacement")
            connection.executescript(get_schema_path().read_text(encoding="utf-8"))
        assert inspect_existing_database_schema()["ok"] is False


@pytest.mark.no_database_autoinit
def test_initialization_rejects_existing_foreign_key_violation_without_repair(monkeypatch, tmp_path: Path) -> None:
    path = _retarget(monkeypatch, tmp_path / "state")
    assert initialize_database()["ok"] is True
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            "INSERT INTO subscription_profile_snapshots(token, nodes_json) VALUES ('missing-client', '[]')"
        )
        connection.commit()
    before = hashlib.sha256(path.read_bytes()).hexdigest()

    state = initialize_database()

    assert state["ok"] is False
    assert any(problem["code"] == "DATABASE_FOREIGN_KEY_CHECK_FAILED" for problem in state["problems"])
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


@pytest.mark.no_database_autoinit
@pytest.mark.parametrize("foreign_key_violation", [False, True])
def test_fresh_initialization_race_validates_winner_without_replacing_it(
    monkeypatch, tmp_path: Path, foreign_key_violation: bool,
) -> None:
    winner_root = tmp_path / "winner"
    winner_path = _retarget(monkeypatch, winner_root)
    assert initialize_database()["ok"] is True
    with sqlite3.connect(winner_path) as winner:
        winner.execute("INSERT INTO settings(key, value_json) VALUES ('race-winner', '{\"v\":7}')")
        if foreign_key_violation:
            winner.execute("PRAGMA foreign_keys=OFF")
            winner.execute(
                "INSERT INTO subscription_profile_snapshots(token, nodes_json) "
                "VALUES ('missing-client', '[]')"
            )
        winner.commit()
    winner_bytes = winner_path.read_bytes()
    winner_hash = hashlib.sha256(winner_bytes).hexdigest()

    target_path = _retarget(monkeypatch, tmp_path / "target")
    real_link = db_connection.os.link

    def publish_winner(_candidate, destination, **kwargs):
        Path(destination).write_bytes(winner_bytes)
        raise FileExistsError

    monkeypatch.setattr(db_connection.os, "link", publish_winner)
    state = initialize_database()
    monkeypatch.setattr(db_connection.os, "link", real_link)

    assert state["ok"] is (not foreign_key_violation)
    assert hashlib.sha256(target_path.read_bytes()).hexdigest() == winner_hash
    with sqlite3.connect(target_path) as winner:
        assert winner.execute(
            "SELECT value_json FROM settings WHERE key='race-winner'"
        ).fetchone() == ('{"v":7}',)


@pytest.mark.no_database_autoinit
def test_initialization_rejects_corrupt_existing_file_without_replacing_it(monkeypatch, tmp_path: Path) -> None:
    path = _retarget(monkeypatch, tmp_path / "state")
    path.parent.mkdir(parents=True)
    contents = b"not a SQLite database"
    path.write_bytes(contents)

    state = initialize_database()

    assert state["ok"] is False
    assert path.read_bytes() == contents


@pytest.mark.no_database_autoinit
def test_observation_and_writer_open_do_not_create_missing_database(monkeypatch, tmp_path: Path) -> None:
    path = _retarget(monkeypatch, tmp_path / "missing-state")

    with pytest.raises(sqlite3.OperationalError, match="FWROUTER_DATABASE_MISSING"):
        inspect_existing_database_schema()
    with pytest.raises(sqlite3.OperationalError, match="FWROUTER_DATABASE_MISSING"):
        connect()

    assert not path.exists()


@pytest.mark.no_database_autoinit
def test_actual_schema11_shape_is_rejected_intact(monkeypatch, tmp_path: Path) -> None:
    path = _retarget(monkeypatch, tmp_path / "state")
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO schema_meta VALUES ('schema_version', '11');
            CREATE TABLE modules (module_name TEXT PRIMARY KEY, desired_state TEXT NOT NULL);
            INSERT INTO modules VALUES ('legacy-provider', 'enabled');
        """)
    before = hashlib.sha256(path.read_bytes()).hexdigest()

    state = initialize_database()

    assert state["ok"] is False
    assert state["actual_schema_version"] == "11"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    with sqlite3.connect(f"{path.absolute().as_uri()}?mode=ro", uri=True) as connection:
        assert connection.execute("SELECT * FROM modules").fetchone() == ("legacy-provider", "enabled")


@pytest.mark.no_database_autoinit
def test_health_reports_missing_database_without_creating_it(monkeypatch, tmp_path: Path) -> None:
    _retarget(monkeypatch, tmp_path / "missing-state")
    path = get_settings().paths.db_path
    path.unlink(missing_ok=True)
    Path(f"{path}-wal").unlink(missing_ok=True)
    Path(f"{path}-shm").unlink(missing_ok=True)
    from fwrouter_api.routes.system import health

    response = health()

    assert response.ok is False
    assert response.error is not None
    assert response.error["code"] == "DATABASE_UNAVAILABLE"
    assert not path.exists()


@pytest.mark.no_database_autoinit
@pytest.mark.parametrize("schema_version", ["8", "10", "11", "23", "24", "25", "26"])
def test_unsupported_version_is_rejected_intact(monkeypatch, tmp_path: Path, schema_version: str) -> None:
    path = _retarget(monkeypatch, tmp_path / "state")
    assert initialize_database()["ok"] is True
    db_connection._clear_schema_admission_cache()
    with connect():
        pass
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE schema_meta SET value=? WHERE key='schema_version'", (schema_version,))
        connection.commit()
    before = hashlib.sha256(path.read_bytes()).hexdigest()

    state = initialize_database()

    assert state["ok"] is False
    assert state["actual_schema_version"] == schema_version
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    with pytest.raises(sqlite3.DatabaseError, match="FWROUTER_DATABASE_SCHEMA_UNSUPPORTED"):
        connect()


@pytest.mark.no_database_autoinit
def test_schema26_is_rejected_intact_without_rewriting_identity_records(monkeypatch, tmp_path: Path) -> None:
    path = _retarget(monkeypatch, tmp_path / "state")
    assert initialize_database()["ok"] is True
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO subscription_node_identities(identity_id, origin_server_id, current_server_id, revision, provenance_sha256) "
            "VALUES ('identity-fixture', 'server-origin', 'server-current', 3, ?)",
            ("a" * 64,),
        )
        connection.execute("UPDATE schema_meta SET value='26' WHERE key='schema_version'")
        connection.commit()
    before = hashlib.sha256(path.read_bytes()).hexdigest()

    state = initialize_database()

    assert state["ok"] is False
    assert state["actual_schema_version"] == "26"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    with sqlite3.connect(f"{path.absolute().as_uri()}?mode=ro", uri=True) as connection:
        assert connection.execute(
            "SELECT origin_server_id, current_server_id, revision FROM subscription_node_identities "
            "WHERE identity_id='identity-fixture'"
        ).fetchone() == ("server-origin", "server-current", 3)


@pytest.mark.no_database_autoinit
def test_schema27_identity_rows_survive_repeated_initialization(monkeypatch, tmp_path: Path) -> None:
    path = _retarget(monkeypatch, tmp_path / "state")
    assert initialize_database()["ok"] is True
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO subscription_node_identities(identity_id, origin_server_id, current_server_id, revision, provenance_sha256) "
            "VALUES ('identity-fixture', 'server-origin', 'server-current', 3, ?)",
            ("a" * 64,),
        )
        connection.execute(
            "INSERT INTO subscription_node_aliases(server_id, identity_id) VALUES ('server-alias', 'identity-fixture')"
        )
        connection.execute(
            "INSERT INTO subscription_node_credentials(scope, identity_id, client_uuid, client_email, epoch) "
            "VALUES ('account', 'identity-fixture', 'uuid-fixture', 'fixture@example.test', 4)"
        )
        connection.commit()

    assert initialize_database()["ok"] is True

    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT origin_server_id, current_server_id, revision FROM subscription_node_identities "
            "WHERE identity_id='identity-fixture'"
        ).fetchone() == ("server-origin", "server-current", 3)
        assert connection.execute(
            "SELECT identity_id FROM subscription_node_aliases WHERE server_id='server-alias'"
        ).fetchone() == ("identity-fixture",)
        assert connection.execute(
            "SELECT client_uuid, client_email, epoch FROM subscription_node_credentials "
            "WHERE identity_id='identity-fixture'"
        ).fetchone() == ("uuid-fixture", "fixture@example.test", 4)


@pytest.mark.no_database_autoinit
def test_database_upgrade_cli_verifies_current_schema_without_mutation(monkeypatch, tmp_path: Path, capsys) -> None:
    from fwrouter_api.cli import main

    path = _retarget(monkeypatch, tmp_path / "state")
    assert initialize_database()["ok"] is True
    before = hashlib.sha256(path.read_bytes()).hexdigest()

    assert main(["database", "upgrade", "--expected-schema", "27"]) == 0

    assert '"status": "already_current"' in capsys.readouterr().out
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_database_upgrade_cli_rejects_retired_historical_transitions(capsys) -> None:
    from fwrouter_api.cli import main

    assert main(["database", "upgrade", "--expected-schema", "26", "--apply", "--confirm-offline"]) == 2

    output = capsys.readouterr().out
    assert "DATABASE_SCHEMA_MIGRATION_RETIRED" in output
    assert "supported_schema" in output


@pytest.mark.no_database_autoinit
def test_database_upgrade_current_schema_rejects_ancestor_symlink(monkeypatch, tmp_path: Path, capsys) -> None:
    from fwrouter_api.cli import main

    real_root = tmp_path / "real-state"
    _retarget(monkeypatch, real_root)
    assert initialize_database()["ok"] is True
    link_root = tmp_path / "linked-state"
    link_root.symlink_to(real_root, target_is_directory=True)
    monkeypatch.setenv("FWROUTER_STATE_DIR", str(link_root))
    get_settings.cache_clear()

    assert main(["database", "upgrade", "--expected-schema", "27"]) == 1

    assert "DATABASE_PATH_SYMLINK_REJECTED" in capsys.readouterr().out


def test_database_upgrade_cli_rejects_unexpected_arguments() -> None:
    from fwrouter_api.cli import main

    with pytest.raises(SystemExit) as exc:
        main(["database", "upgrade", "--expected-schema", "27", "--unexpected"])

    assert exc.value.code == 2


@pytest.mark.no_database_autoinit
def test_schema_signature_positive_cache_avoids_repeated_metadata_validation(
    monkeypatch, tmp_path: Path,
) -> None:
    _retarget(monkeypatch, tmp_path / "state")
    assert initialize_database()["ok"] is True
    db_connection._clear_schema_admission_cache()
    calls = 0
    original = db_connection.inspect_database_schema

    def counted(connection):
        nonlocal calls
        calls += 1
        return original(connection)

    monkeypatch.setattr(db_connection, "inspect_database_schema", counted)
    with connect():
        pass
    with connect():
        pass

    assert calls == 1
    assert all(db_connection._SCHEMA_ADMISSION_CACHE.values())
    assert len(db_connection._SCHEMA_ADMISSION_CACHE) <= db_connection._SCHEMA_ADMISSION_CACHE_MAX


@pytest.mark.no_database_autoinit
def test_schema_content_replacement_with_same_version_and_cookie_fails_closed(
    monkeypatch, tmp_path: Path,
) -> None:
    path = _retarget(monkeypatch, tmp_path / "state")
    assert initialize_database()["ok"] is True
    db_connection._clear_schema_admission_cache()
    with connect():
        pass
    with sqlite3.connect(path) as connection:
        schema_cookie = connection.execute("PRAGMA schema_version").fetchone()[0]
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.execute("PRAGMA journal_mode=DELETE")
    replacement = path.with_name("replacement.db")
    replacement.write_bytes(path.read_bytes())
    with sqlite3.connect(replacement) as connection:
        connection.execute("PRAGMA writable_schema=ON")
        sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='subject_server_overrides'"
        ).fetchone()[0]
        connection.execute(
            "UPDATE sqlite_master SET sql=? WHERE type='table' AND name='subject_server_overrides'",
            (sql.replace("'server'", "'SERVER'"),),
        )
        connection.execute(f"PRAGMA schema_version={int(schema_cookie)}")
        connection.commit()
    os.replace(replacement, path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA schema_version").fetchone()[0] == schema_cookie
        assert connection.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()[0] == EXPECTED_SCHEMA_VERSION

    with pytest.raises(sqlite3.DatabaseError, match="FWROUTER_DATABASE_SCHEMA_UNSUPPORTED"):
        connect()

    assert len(db_connection._SCHEMA_ADMISSION_CACHE) == 1


@pytest.mark.no_database_autoinit
def test_schema_admission_cache_is_bounded_and_thread_safe(monkeypatch, tmp_path: Path) -> None:
    _retarget(monkeypatch, tmp_path / "state")
    assert initialize_database()["ok"] is True
    db_connection._clear_schema_admission_cache()

    def open_and_close(_index: int) -> bool:
        with connect() as connection:
            return connection.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0] == "27"

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(open_and_close, range(18)))
    assert all(results)
    for index in range(db_connection._SCHEMA_ADMISSION_CACHE_MAX + 8):
        db_connection._remember_schema_admission(("canonical", "27", f"schema-{index}"))
    assert len(db_connection._SCHEMA_ADMISSION_CACHE) == db_connection._SCHEMA_ADMISSION_CACHE_MAX
    assert ("canonical", "27", "schema-0") not in db_connection._SCHEMA_ADMISSION_CACHE
    db_connection._clear_schema_admission_cache()


@pytest.mark.no_database_autoinit
@pytest.mark.parametrize("damage", ["table", "index", "trigger", "check"])
def test_partial_schema27_fails_closed(monkeypatch, tmp_path: Path, damage: str) -> None:
    path = _retarget(monkeypatch, tmp_path / "state")
    assert initialize_database()["ok"] is True
    with sqlite3.connect(path) as connection:
        if damage == "table":
            connection.execute("DROP TABLE traffic_counter_snapshots")
        elif damage == "index":
            connection.execute("DROP INDEX idx_servers_inventory_state")
        elif damage == "trigger":
            connection.execute("CREATE TRIGGER unexpected_trigger AFTER INSERT ON settings BEGIN SELECT 1; END")
        else:
            connection.execute("ALTER TABLE modules RENAME TO modules_saved")
            connection.execute("CREATE TABLE modules(module_name TEXT PRIMARY KEY, desired_state TEXT NOT NULL)")
            connection.execute("INSERT INTO modules(module_name, desired_state) SELECT module_name, desired_state FROM modules_saved")
            connection.execute("DROP TABLE modules_saved")
        connection.commit()

    state = inspect_existing_database_schema()

    assert state["ok"] is False
    with pytest.raises(sqlite3.DatabaseError, match="FWROUTER_DATABASE_SCHEMA_UNSUPPORTED"):
        connect()


@pytest.mark.no_database_autoinit
@pytest.mark.parametrize(
    ("table_name", "old", "new"),
    [
        ("subject_server_overrides", "'server'", "'SERVER'"),
        ("subscription_accounts", "AUTOINCREMENT", ""),
    ],
)
def test_schema_admission_preserves_string_and_autoincrement_semantics(
    monkeypatch, tmp_path: Path, table_name: str, old: str, new: str,
) -> None:
    path = _retarget(monkeypatch, tmp_path / "state")
    assert initialize_database()["ok"] is True
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA writable_schema=ON")
        sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table_name,)
        ).fetchone()[0]
        assert old in sql
        connection.execute("UPDATE sqlite_master SET sql=? WHERE type='table' AND name=?",
                           (sql.replace(old, new), table_name))
        connection.execute("PRAGMA schema_version=999")
        connection.commit()

    assert inspect_existing_database_schema()["ok"] is False


@pytest.mark.no_database_autoinit
def test_additive_non_unique_index_remains_supported(monkeypatch, tmp_path: Path) -> None:
    path = _retarget(monkeypatch, tmp_path / "state")
    assert initialize_database()["ok"] is True
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE INDEX operator_read_index ON settings(value_json)")
        connection.commit()

    state = inspect_existing_database_schema()
    assert state["ok"] is True, state["problems"]


@pytest.mark.no_database_autoinit
def test_additive_unique_index_is_rejected(monkeypatch, tmp_path: Path) -> None:
    path = _retarget(monkeypatch, tmp_path / "state")
    assert initialize_database()["ok"] is True
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE UNIQUE INDEX operator_unique_index ON settings(key)")
        connection.commit()

    assert inspect_existing_database_schema()["ok"] is False


@pytest.mark.no_database_autoinit
def test_supported_schema27_alter_added_column_layout_is_admitted(monkeypatch, tmp_path: Path) -> None:
    path = _retarget(monkeypatch, tmp_path / "state")
    assert initialize_database()["ok"] is True
    with sqlite3.connect(path) as connection:
        connection.execute("ALTER TABLE subject_server_overrides RENAME TO subject_server_overrides_saved")
        connection.execute("""CREATE TABLE subject_server_overrides (
            subject_id TEXT PRIMARY KEY,
            selected_server_id TEXT,
            selected_until TEXT,
            apply_state TEXT NOT NULL DEFAULT 'clean',
            error_code TEXT,
            error_message TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CHECK (apply_state IN ('clean', 'pending', 'applying', 'failed')),
            FOREIGN KEY (subject_id) REFERENCES subjects(subject_id) ON DELETE CASCADE,
            FOREIGN KEY (selected_server_id) REFERENCES servers(server_id) ON DELETE SET NULL
        )""")
        connection.execute("""INSERT INTO subject_server_overrides(
            subject_id, selected_server_id, selected_until, apply_state, error_code, error_message, updated_at)
            SELECT subject_id, selected_server_id, selected_until, apply_state, error_code, error_message, updated_at
            FROM subject_server_overrides_saved""")
        connection.execute("DROP TABLE subject_server_overrides_saved")
        connection.execute("""ALTER TABLE subject_server_overrides ADD COLUMN selected_target_kind TEXT NOT NULL DEFAULT 'server'
            CHECK (selected_target_kind IN ('server', 'vpn_auto')
                   AND (selected_target_kind = 'server' OR selected_server_id IS NULL))""")
        connection.execute("CREATE INDEX idx_subject_server_overrides_until ON subject_server_overrides(selected_until)")
        connection.commit()

    state = inspect_existing_database_schema()
    from fwrouter_api.db.schema_state import _canonical_schema_contract, _table_semantics
    expected = _canonical_schema_contract()[1]["subject_server_overrides"]
    with sqlite3.connect(f"{path.absolute().as_uri()}?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        actual = _table_semantics(connection, "subject_server_overrides")
    assert state["ok"] is True, (state["problems"], actual, expected)
