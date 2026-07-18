from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from fwrouter_api.core.config import get_settings
from fwrouter_api.db.schema_state import inspect_database_schema
from fwrouter_api.services.live_probe_cache import get_live_probe_cache


def get_db_path() -> Path:
    settings = get_settings()
    return settings.paths.db_path


def get_schema_path() -> Path:
    return Path(__file__).with_name("schema.sql")


def _server_preferences_needs_priority_migration(connection: sqlite3.Connection) -> bool:
    row = connection.execute(
        """
        SELECT sql
        FROM sqlite_master
        WHERE type = 'table' AND name = 'server_preferences'
        """
    ).fetchone()
    table_sql = str((row["sql"] if row is not None else "") or "")
    return "vpn_auto_priority >= 0" in table_sql


def _migrate_server_preferences_priority_range(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE server_preferences_new (
            server_id TEXT PRIMARY KEY,
            vpn_auto INTEGER NOT NULL DEFAULT 0,
            vpn_auto_priority INTEGER NOT NULL DEFAULT 0,
            global_list INTEGER NOT NULL DEFAULT 1,
            remembered_until TEXT,
            manually_deleted_at TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CHECK (vpn_auto IN (0, 1)),
            CHECK (vpn_auto_priority >= -1 AND vpn_auto_priority <= 5),
            CHECK (global_list IN (0, 1)),
            FOREIGN KEY (server_id) REFERENCES servers(server_id) ON DELETE CASCADE
        );

        INSERT INTO server_preferences_new (
            server_id,
            vpn_auto,
            vpn_auto_priority,
            global_list,
            remembered_until,
            manually_deleted_at,
            updated_at
        )
        SELECT
            server_id,
            vpn_auto,
            vpn_auto_priority,
            global_list,
            remembered_until,
            manually_deleted_at,
            updated_at
        FROM server_preferences;

        DROP TABLE server_preferences;
        ALTER TABLE server_preferences_new RENAME TO server_preferences;

        CREATE INDEX IF NOT EXISTS idx_server_preferences_vpn_auto
        ON server_preferences (vpn_auto);

        CREATE INDEX IF NOT EXISTS idx_server_preferences_global_list
        ON server_preferences (global_list);
        """
    )


def _server_custom_https_proxy_needs_protocol_column(connection: sqlite3.Connection) -> bool:
    columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(server_custom_https_proxy)").fetchall()
    }
    return "proxy_type" not in columns


def connect() -> sqlite3.Connection:
    db_path = get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(db_path, timeout=30.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON;")
    connection.execute("PRAGMA journal_mode = WAL;")
    connection.execute("PRAGMA synchronous = NORMAL;")
    connection.execute("PRAGMA busy_timeout = 30000;")
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
        connection.executescript(schema_sql)
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(server_preferences)").fetchall()
        }
        if "vpn_auto_priority" not in columns:
            connection.execute(
                """
                ALTER TABLE server_preferences
                ADD COLUMN vpn_auto_priority INTEGER NOT NULL DEFAULT 0
                CHECK (vpn_auto_priority >= -1 AND vpn_auto_priority <= 5)
                """
            )
        routing_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(routing_global_state)").fetchall()
        }
        if "fixed_server_until" not in routing_columns:
            connection.execute(
                """
                ALTER TABLE routing_global_state
                ADD COLUMN fixed_server_until TEXT
                """
            )
        if _server_preferences_needs_priority_migration(connection):
            _migrate_server_preferences_priority_range(connection)
        if _server_custom_https_proxy_needs_protocol_column(connection):
            connection.execute(
                """
                ALTER TABLE server_custom_https_proxy
                ADD COLUMN proxy_type TEXT NOT NULL DEFAULT 'http'
                CHECK (proxy_type IN ('http', 'socks5'))
                """
            )
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
