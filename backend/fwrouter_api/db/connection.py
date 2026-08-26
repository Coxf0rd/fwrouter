from __future__ import annotations

import sqlite3
import time
import json
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


def _modules_needs_lifecycle_mode_column(connection: sqlite3.Connection) -> bool:
    columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(modules)").fetchall()
    }
    return "lifecycle_mode" not in columns


def _subjects_columns(connection: sqlite3.Connection) -> set[str]:
    return {
        row["name"]
        for row in connection.execute("PRAGMA table_info(subjects)").fetchall()
    }


def _backfill_subject_roles(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        UPDATE subjects
        SET
            subject_role = CASE subject_type
                WHEN 'lan' THEN 'lan_client'
                WHEN 'tailscale' THEN 'external_network_source'
                WHEN 'tailscale_node' THEN 'external_network_source'
                WHEN 'xray' THEN 'vless_client'
                WHEN 'docker' THEN 'docker_runtime'
                WHEN 'host' THEN 'host_runtime'
                WHEN 'fwrouter' THEN 'router_core'
                ELSE 'unknown'
            END,
            implementation_kind = CASE
                WHEN subject_type IS NOT NULL AND subject_type <> '' THEN subject_type
                ELSE 'unknown'
            END,
            updated_at = CURRENT_TIMESTAMP
        WHERE subject_role = 'unknown'
           OR implementation_kind = 'unknown'
           OR subject_role IS NULL
           OR implementation_kind IS NULL
        """
    )


def _apply_default_module_lifecycle_modes(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        UPDATE modules
        SET lifecycle_mode = CASE module_name
            WHEN 'tailscale' THEN 'external'
            WHEN 'core' THEN 'managed'
            WHEN 'vpn' THEN 'managed'
            WHEN 'xray' THEN 'managed'
            WHEN 'watchdog' THEN 'managed'
            WHEN 'selector' THEN 'managed'
            WHEN 'subscription' THEN 'managed'
            ELSE lifecycle_mode
        END
        WHERE lifecycle_mode = 'none'
          AND module_name IN ('core', 'vpn', 'xray', 'tailscale', 'watchdog', 'selector', 'subscription')
        """
    )


def _slugify_external_connection_id(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    result: list[str] = []
    previous_dash = False
    for char in normalized:
        if char.isalnum():
            result.append(char)
            previous_dash = False
        elif char == "_":
            result.append("_")
            previous_dash = False
        elif not previous_dash:
            result.append("-")
            previous_dash = True
    return "".join(result).strip("-")[:64]


def _external_connection_row_from_value(
    value: dict[str, Any],
    *,
    fallback_id: str = "",
) -> dict[str, Any] | None:
    raw_id = (
        value.get("connection_id")
        or value.get("system_id")
        or value.get("id")
        or fallback_id
        or value.get("label")
    )
    connection_id = _slugify_external_connection_id(raw_id)
    if not connection_id:
        return None
    connection_type = str(value.get("connection_type") or "external_management").strip().lower()
    if connection_type not in {
        "external_management",
        "external_vpn_module",
        "external_network_source",
        "display_only",
    }:
        connection_type = "external_management"
    system_id = _slugify_external_connection_id(value.get("system_id") or connection_id)
    if not system_id:
        system_id = connection_id
    label = str(value.get("label") or value.get("name") or system_id).strip()[:80] or system_id
    location = str(value.get("location") or "manual").strip().lower()
    if location not in {"docker", "host", "ip", "manual"}:
        location = "manual"
    integration_mode = str(value.get("integration_mode") or "api_push").strip().lower()
    if integration_mode not in {"api_push", "http_poll", "command_probe", "file_read"}:
        integration_mode = "api_push"
    refresh_default = "on_change" if integration_mode == "api_push" else "manual"
    refresh_mode = str(value.get("refresh_mode") or refresh_default).strip().lower()
    if refresh_mode not in {"on_change", "manual", "interval"}:
        refresh_mode = "on_change" if integration_mode == "api_push" else "manual"
    if integration_mode == "api_push":
        refresh_mode = "on_change"
    stored = dict(value)
    stored["connection_id"] = connection_id
    stored["system_id"] = system_id
    stored["label"] = label
    stored["connection_type"] = connection_type
    stored["location"] = location
    stored["integration_mode"] = integration_mode
    stored["refresh_mode"] = refresh_mode
    stored["custom"] = True
    return {
        "connection_id": connection_id,
        "system_id": system_id,
        "label": label,
        "connection_type": connection_type,
        "runtime_type": str(value.get("runtime_type") or "").strip().lower()[:80],
        "replacement_target": str(
            value.get("replacement_target") or value.get("replaces") or ""
        ).strip().lower()[:80],
        "location": location,
        "address": str(value.get("address") or "").strip()[:160],
        "integration_mode": integration_mode,
        "refresh_mode": refresh_mode,
        "enabled": 1 if value.get("enabled", True) is not False else 0,
        "value_json": json.dumps(stored, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
    }


def _insert_external_connection_if_missing(
    connection: sqlite3.Connection,
    value: dict[str, Any],
    *,
    fallback_id: str = "",
) -> None:
    row = _external_connection_row_from_value(value, fallback_id=fallback_id)
    if row is None:
        return
    connection.execute(
        """
        INSERT OR IGNORE INTO external_connections (
            connection_id, system_id, label, connection_type, runtime_type,
            replacement_target, location, address, integration_mode, refresh_mode,
            enabled, value_json, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """,
        (
            row["connection_id"],
            row["system_id"],
            row["label"],
            row["connection_type"],
            row["runtime_type"],
            row["replacement_target"],
            row["location"],
            row["address"],
            row["integration_mode"],
            row["refresh_mode"],
            row["enabled"],
            row["value_json"],
        ),
    )
    connection.execute(
        """
        INSERT OR IGNORE INTO external_connection_generated_state (connection_id, state_json, updated_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
        """,
        (
            row["connection_id"],
            json.dumps(
                {
                    "connection_id": row["connection_id"],
                    "system_id": row["system_id"],
                    "connection_type": row["connection_type"],
                    "runtime_type": row["runtime_type"],
                    "replacement_target": row["replacement_target"],
                    "integration_mode": row["integration_mode"],
                    "refresh_mode": row["refresh_mode"],
                    "collector": f"external_connection:{row['connection_id']}",
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        ),
    )


def _migrate_external_connections(connection: sqlite3.Connection) -> None:
    row = connection.execute(
        "SELECT value_json FROM settings WHERE key = 'ui.admin_client_display.v1'"
    ).fetchone()
    settings: dict[str, Any] = {}
    if row is not None:
        try:
            loaded = json.loads(row["value_json"])
            if isinstance(loaded, dict):
                settings = loaded
        except json.JSONDecodeError:
            settings = {}

    legacy_systems = settings.get("custom_external_systems")
    for item in legacy_systems if isinstance(legacy_systems, list) else []:
        if isinstance(item, dict):
            _insert_external_connection_if_missing(connection, item)

    raw_visibility = settings.get("system_visibility")
    visibility = raw_visibility if isinstance(raw_visibility, dict) else {}
    for system_id, visible in visibility.items():
        normalized = _slugify_external_connection_id(system_id)
        if not visible or not normalized.startswith("external-management-"):
            continue
        label = (
            normalized.removeprefix("external-management-").replace("-", " ").strip().title()
            or normalized
        )
        _insert_external_connection_if_missing(
            connection,
            {
                "connection_id": normalized,
                "system_id": normalized,
                "label": label,
                "connection_type": "external_management",
                "location": "manual",
                "integration_mode": "api_push",
                "refresh_mode": "on_change",
            },
            fallback_id=normalized,
        )

    tailscale_migrated = connection.execute(
        """
        SELECT 1
        FROM external_connection_migrations
        WHERE migration_key = 'bootstrap_discovered_tailscale_v1'
        """
    ).fetchone()
    tailscale_count = connection.execute(
        """
        SELECT COUNT(*)
        FROM subjects
        WHERE is_deleted = 0
          AND subject_role = 'external_network_source'
          AND subject_type = 'tailscale_node'
        """
    ).fetchone()[0]
    if tailscale_count and tailscale_migrated is None:
        _insert_external_connection_if_missing(
            connection,
            {
                "connection_id": "external-network-tailscale",
                "system_id": "external-network-tailscale",
                "label": "Tailscale",
                "connection_type": "external_network_source",
                "location": "host",
                "runtime_type": "tailscale",
                "integration_mode": "command_probe",
                "refresh_mode": "interval",
                "capabilities": {"supports_client_inventory": True},
                "collector_config": {
                    "script_id": "tailscale_status",
                    "interval_seconds": 3600,
                    "timeout_seconds": 20,
                    "apply_traffic": False,
                    "trigger": "poll_interval",
                },
            },
            fallback_id="external-network-tailscale",
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO external_connection_migrations (migration_key, applied_at)
            VALUES ('bootstrap_discovered_tailscale_v1', CURRENT_TIMESTAMP)
            """
        )


def connect() -> sqlite3.Connection:
    db_path = get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)

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
        if _modules_needs_lifecycle_mode_column(connection):
            connection.execute(
                """
                ALTER TABLE modules
                ADD COLUMN lifecycle_mode TEXT NOT NULL DEFAULT 'none'
                CHECK (lifecycle_mode IN ('none', 'managed', 'external'))
                """
            )
        subject_columns = _subjects_columns(connection)
        if "subject_role" not in subject_columns:
            connection.execute(
                """
                ALTER TABLE subjects
                ADD COLUMN subject_role TEXT NOT NULL DEFAULT 'unknown'
                CHECK (subject_role IN ('unknown', 'lan_client', 'external_network_source', 'vless_client', 'docker_runtime', 'host_runtime', 'router_core'))
                """
            )
        if "implementation_kind" not in subject_columns:
            connection.execute(
                """
                ALTER TABLE subjects
                ADD COLUMN implementation_kind TEXT NOT NULL DEFAULT 'unknown'
                """
            )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_subjects_role_active
            ON subjects (subject_role, is_active, last_seen_at DESC)
            """
        )
        _backfill_subject_roles(connection)
        _apply_default_module_lifecycle_modes(connection)
        _migrate_external_connections(connection)
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
