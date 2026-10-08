"""Joined persistence/migration checks, executed only in qualified hosted Compose."""
from __future__ import annotations

import sqlite3

from .http_support import http_json
from .xray_support import loaded_identities


def _owned_database(stack):
    from fwrouter_api.core.config import get_settings
    path = get_settings().paths.db_path.resolve(strict=True)
    assert path.is_relative_to(stack["state"].resolve(strict=True))
    return path


def test_schema23_upgrade_preserves_binding_secret_and_native_clients(acceptance_stack):
    """Run the real initializer on an actual v23-shaped provider table, then HTTP readback."""
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
    first = initialize_database()
    assert first["ok"] is True and first["actual_schema_version"] == "24", first
    with sqlite3.connect(path) as connection:
        binding = connection.execute("SELECT enabled,current_member_id,allow_automatic_member_switch FROM provider_bindings WHERE source_ref=?", (source,)).fetchone()
        secret = connection.execute("SELECT api_key FROM provider_credentials WHERE source_ref=?", (source,)).fetchone()
        assert binding == (1, "migration-member", 0), binding
        assert secret == ("synthetic-not-a-production-secret",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    second = initialize_database()
    assert second["ok"] is True and second["actual_schema_version"] == "24", second
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
