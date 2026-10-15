"""Joined persistence/migration checks, executed only in qualified hosted Compose."""
from __future__ import annotations

import hashlib
import sqlite3

from .http_support import http_json
from .xray_support import loaded_identities


def _owned_database(stack):
    from fwrouter_api.core.config import get_settings
    path = get_settings().paths.db_path.resolve(strict=True)
    assert path.is_relative_to(stack["state"].resolve(strict=True))
    return path


def _database_rows(connection, table_names):
    """Return deterministic row snapshots for the selected existing tables."""
    snapshots = {}
    for table_name in sorted(table_names):
        quoted = '"' + table_name.replace('"', '""') + '"'
        columns = [row[1] for row in connection.execute(f"PRAGMA table_info({quoted})")]
        if not columns:
            continue
        projection = ", ".join('"' + name.replace('"', '""') + '"' for name in columns)
        snapshots[table_name] = (
            tuple(columns),
            connection.execute(
                f"SELECT {projection} FROM {quoted} ORDER BY {projection}"
            ).fetchall(),
        )
    return snapshots


def _rows_digest(rows):
    """Compare complete row values without putting persisted values in CI output."""
    return hashlib.sha256(repr(rows).encode("utf-8")).hexdigest()


def _assert_existing_database_rows_preserved(before, after):
    """Compare every pre-migration row on columns that existed before upgrade."""
    assert set(before) <= set(after)
    for table_name, (old_columns, old_rows) in before.items():
        new_columns, new_rows = after[table_name]
        assert set(old_columns) <= set(new_columns), f"{table_name} existing columns were removed"
        old_positions = tuple(range(len(old_columns)))
        new_positions = tuple(new_columns.index(name) for name in old_columns)
        old_projection = sorted(
            repr(tuple(row[index] for index in old_positions)) for row in old_rows
        )
        new_projection = sorted(
            repr(tuple(row[index] for index in new_positions)) for row in new_rows
        )
        assert _rows_digest(old_projection) == _rows_digest(new_projection), (
            f"{table_name} existing-row digest changed"
        )


def test_provider_v23_shape_initialization_reaches_schema25_and_preserves_state(acceptance_stack):
    """Probe the v23 provider-column contract against the current schema-25 app DB.

    The acceptance worker is provisioned with the current schema. This test removes
    the v24 provider column and uses the v23 marker to exercise the supported
    provider migration path; it is not a complete historical-v23 database fixture.
    The genuine schema-24-to-25 preimage and downgrade refusal are covered by the
    isolated migration/CLI contract tests.
    """
    stack = acceptance_stack
    from fwrouter_api.db.connection import initialize_database
    before_native = loaded_identities(stack["native"])
    path = _owned_database(stack)
    stack["worker"].terminate()
    stack["worker"].wait(timeout=5)
    source = "acceptance:migration-source"
    with sqlite3.connect(path) as connection:
        connection.execute("INSERT INTO provider_bindings (source_ref,provider_id,resource_id,logical_server_id,protocol,enabled,current_member_id,updated_at) VALUES (?,?,?,?,?,1,?,0)",
                           (source, "stealthsurf", "1", "migration-logical", "hysteria2", "migration-member"))
        connection.execute("INSERT INTO provider_credentials (source_ref,api_key) VALUES (?,?)", (source, "synthetic-not-a-production-secret"))
        connection.execute("ALTER TABLE provider_bindings DROP COLUMN allow_automatic_member_switch")
        connection.execute("UPDATE schema_meta SET value='23' WHERE key='schema_version'")
        preexisting_tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name <> 'schema_meta'"
            )
        }
        before_rows = _database_rows(connection, preexisting_tables)
    first = initialize_database()
    assert first["ok"] is True and first["actual_schema_version"] == "25", first
    with sqlite3.connect(path) as connection:
        binding = connection.execute("SELECT enabled,current_member_id,allow_automatic_member_switch FROM provider_bindings WHERE source_ref=?", (source,)).fetchone()
        secret = connection.execute("SELECT api_key FROM provider_credentials WHERE source_ref=?", (source,)).fetchone()
        assert binding == (1, "migration-member", 0), binding
        assert secret == ("synthetic-not-a-production-secret",)
        identity_tables = {
            "subscription_node_identities",
            "subscription_node_aliases",
            "subscription_node_credentials",
            "subscription_node_credential_history",
            "subscription_node_identity_transitions",
        }
        existing_tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        assert identity_tables <= existing_tables
        after_first_rows = _database_rows(connection, existing_tables - {"schema_meta"})
        _assert_existing_database_rows_preserved(before_rows, after_first_rows)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    second = initialize_database()
    assert second["ok"] is True and second["actual_schema_version"] == "25", second
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone() == ("25",)
        after_second_rows = _database_rows(connection, existing_tables - {"schema_meta"})
        assert set(after_second_rows) == set(after_first_rows)
        for table_name in sorted(after_first_rows):
            first_columns, first_rows = after_first_rows[table_name]
            second_columns, second_rows = after_second_rows[table_name]
            assert first_columns == second_columns, f"{table_name} columns changed on repeated initialization"
            assert _rows_digest(first_rows) == _rows_digest(second_rows), (
                f"{table_name} row digest changed on repeated initialization"
            )
    stack["worker"], stack["api"] = stack["start_worker"]()
    code, response = http_json(stack["api"] + "/xray/clients")
    assert code == 200 and response.get("ok") is True, response
    assert loaded_identities(stack["native"]) == before_native
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT enabled,current_member_id,allow_automatic_member_switch FROM provider_bindings WHERE source_ref=?", (source,)).fetchone() == binding
        assert connection.execute("SELECT api_key FROM provider_credentials WHERE source_ref=?", (source,)).fetchone() == secret


def test_application_database_transaction_rollback_is_visible_after_restart(acceptance_stack):
    """The product transaction context must roll back intent; API/native state stays unchanged."""
    stack = acceptance_stack
    from fwrouter_api.db.connection import db_session
    before_native = loaded_identities(stack["native"])
    path = _owned_database(stack)
    source = "acceptance:rolled-back-binding"
    try:
        with db_session() as connection:
            connection.execute("INSERT INTO provider_bindings (source_ref,provider_id,resource_id,logical_server_id,protocol,updated_at) VALUES (?,?,?,?,?,0)",
                               (source, "stealthsurf", "2", "rollback-logical", "hysteria2"))
            assert connection.execute("SELECT source_ref FROM provider_bindings WHERE source_ref=?", (source,)).fetchone() is not None
            raise RuntimeError("intentional isolated transaction abort")
    except RuntimeError as exc:
        assert str(exc) == "intentional isolated transaction abort"
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT source_ref FROM provider_bindings WHERE source_ref=?", (source,)).fetchone() is None
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    stack["worker"].terminate()
    stack["worker"].wait(timeout=5)
    stack["worker"], stack["api"] = stack["start_worker"]()
    code, response = http_json(stack["api"] + "/xray/clients")
    assert code == 200 and response.get("ok") is True, response
    assert loaded_identities(stack["native"]) == before_native
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT source_ref FROM provider_bindings WHERE source_ref=?", (source,)).fetchone() is None
