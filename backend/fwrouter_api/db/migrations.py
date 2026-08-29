from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fwrouter_api.db.schema_state import EXPECTED_SCHEMA_VERSION


CURRENT_SCHEMA_VERSION = int(EXPECTED_SCHEMA_VERSION)


@dataclass(frozen=True)
class SchemaMigration:
    from_version: int
    to_version: int
    apply: Callable[[sqlite3.Connection], None]


def _table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    row = connection.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table'
          AND name = ?
        """,
        (table_name,),
    ).fetchone()
    return row is not None


def _columns(connection: sqlite3.Connection, table_name: str) -> set[str]:
    if not _table_exists(connection, table_name):
        return set()
    return {
        str(row["name"])
        for row in connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    }


def _table_sql(connection: sqlite3.Connection, table_name: str) -> str:
    row = connection.execute(
        """
        SELECT sql
        FROM sqlite_master
        WHERE type = 'table'
          AND name = ?
        """,
        (table_name,),
    ).fetchone()
    return str((row["sql"] if row is not None else "") or "")


def _schema_version(connection: sqlite3.Connection) -> int | None:
    if not _table_exists(connection, "schema_meta"):
        return None
    row = connection.execute(
        "SELECT value FROM schema_meta WHERE key = 'schema_version'"
    ).fetchone()
    if row is None:
        return None
    try:
        return int(str(row["value"]))
    except ValueError as exc:
        raise RuntimeError(f"Unsupported schema_version value: {row['value']!r}") from exc


def _set_schema_version(connection: sqlite3.Connection, version: int) -> None:
    connection.execute(
        """
        INSERT INTO schema_meta (key, value, updated_at)
        VALUES ('schema_version', ?, CURRENT_TIMESTAMP)
        ON CONFLICT(key) DO UPDATE SET
            value = excluded.value,
            updated_at = excluded.updated_at
        """,
        (str(version),),
    )


def _server_preferences_needs_priority_range_rebuild(connection: sqlite3.Connection) -> bool:
    return "vpn_auto_priority >= 0" in _table_sql(connection, "server_preferences")


def _rebuild_server_preferences_priority_range(connection: sqlite3.Connection) -> None:
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


def _apply_default_module_lifecycle_modes(connection: sqlite3.Connection) -> None:
    if "lifecycle_mode" not in _columns(connection, "modules"):
        return
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


def _json_object(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        loaded = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _json_detail_source(value: str | None) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _merge_subject_detail_metadata(
    connection: sqlite3.Connection,
    subject_id: str,
    detail: dict[str, Any],
) -> None:
    row = connection.execute(
        "SELECT metadata_json FROM subjects WHERE subject_id = ?",
        (subject_id,),
    ).fetchone()
    if row is None:
        return
    metadata = _json_object(row["metadata_json"])
    current_detail = metadata.get("detail") if isinstance(metadata.get("detail"), dict) else {}
    metadata["detail"] = {
        **current_detail,
        **{key: value for key, value in detail.items() if value is not None},
    }
    connection.execute(
        """
        UPDATE subjects
        SET metadata_json = json(?), updated_at = CURRENT_TIMESTAMP
        WHERE subject_id = ?
        """,
        (json.dumps(metadata, ensure_ascii=False, sort_keys=True), subject_id),
    )


def _migrate_provider_subject_details_to_metadata(connection: sqlite3.Connection) -> None:
    if _table_exists(connection, "subject_tailscale"):
        rows = connection.execute(
            """
            SELECT subject_id, node_id, tailscale_ip, hostname, user_name, online, source_json
            FROM subject_tailscale
            """
        ).fetchall()
        for row in rows:
            _merge_subject_detail_metadata(
                connection,
                str(row["subject_id"]),
                {
                    "node_id": row["node_id"],
                    "provider_node_id": row["node_id"],
                    "tailscale_ip": row["tailscale_ip"],
                    "ip_address": row["tailscale_ip"],
                    "hostname": row["hostname"],
                    "user_name": row["user_name"],
                    "online": bool(row["online"]),
                    "source": _json_detail_source(row["source_json"]),
                },
            )
        connection.execute("DROP TABLE subject_tailscale")

    if _table_exists(connection, "subject_xray"):
        rows = connection.execute(
            """
            SELECT
                subject_id,
                client_id,
                client_uuid,
                email,
                subscription_path,
                last_subscription_at,
                enabled,
                source_json
            FROM subject_xray
            """
        ).fetchall()
        for row in rows:
            _merge_subject_detail_metadata(
                connection,
                str(row["subject_id"]),
                {
                    "client_id": row["client_id"],
                    "client_uuid": row["client_uuid"],
                    "email": row["email"],
                    "subscription_path": row["subscription_path"],
                    "last_subscription_at": row["last_subscription_at"],
                    "enabled": bool(row["enabled"]),
                    "source": _json_detail_source(row["source_json"]),
                },
            )
        connection.execute("DROP TABLE subject_xray")


def _normalize_provider_subject_types(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        UPDATE subjects
        SET
            subject_type = 'external_network_client',
            subject_role = 'external_network_source',
            implementation_kind = CASE
                WHEN implementation_kind IS NULL
                  OR implementation_kind = ''
                  OR implementation_kind IN ('unknown', 'tailscale', 'tailscale_node')
                THEN 'tailscale'
                ELSE implementation_kind
            END,
            updated_at = CURRENT_TIMESTAMP
        WHERE subject_type IN ('tailscale', 'tailscale_node')
        """
    )
    connection.execute(
        """
        UPDATE subjects
        SET
            subject_type = 'explicit_external_client',
            subject_role = 'vless_client',
            implementation_kind = CASE
                WHEN implementation_kind IS NULL
                  OR implementation_kind = ''
                  OR implementation_kind IN ('unknown', 'xray')
                THEN 'xray'
                ELSE implementation_kind
            END,
            updated_at = CURRENT_TIMESTAMP
        WHERE subject_type = 'xray'
        """
    )


def _backfill_subject_roles(connection: sqlite3.Connection) -> None:
    if not {"subject_role", "implementation_kind"}.issubset(_columns(connection, "subjects")):
        return
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


def _subjects_needs_open_type_rebuild(connection: sqlite3.Connection) -> bool:
    table_sql = _table_sql(connection, "subjects").lower()
    return (
        "check (subject_type in" in table_sql
        or "subject_role in ('unknown', 'lan_client', 'external_network_source', 'vless_client', 'docker_runtime', 'host_runtime', 'router_core')" not in table_sql
        or "desired_mode in ('global', 'direct', 'selective', 'vpn', 'disabled', 'enabled', 'forced_vpn')" not in table_sql
    )


def _rebuild_subjects_to_open_subject_type(connection: sqlite3.Connection) -> None:
    connection.commit()
    connection.execute("PRAGMA foreign_keys = OFF;")
    connection.execute("PRAGMA legacy_alter_table = ON;")
    try:
        connection.executescript(
            """
            ALTER TABLE subjects RENAME TO subjects_old;

            CREATE TABLE subjects (
                subject_id TEXT PRIMARY KEY,
                subject_type TEXT NOT NULL,
                subject_role TEXT NOT NULL DEFAULT 'unknown',
                implementation_kind TEXT NOT NULL DEFAULT 'unknown',
                stable_key TEXT NOT NULL,
                display_name TEXT,
                alias TEXT,
                desired_mode TEXT NOT NULL,
                applied_mode TEXT,
                apply_state TEXT NOT NULL DEFAULT 'clean',
                runtime_state TEXT NOT NULL DEFAULT 'not_configured',
                is_active INTEGER NOT NULL DEFAULT 0,
                is_deleted INTEGER NOT NULL DEFAULT 0,
                first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_seen_at TEXT,
                last_traffic_at TEXT,
                inactive_since TEXT,
                deleted_at TEXT,
                metadata_json TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CHECK (subject_role IN ('unknown', 'lan_client', 'external_network_source', 'vless_client', 'docker_runtime', 'host_runtime', 'router_core')),
                CHECK (desired_mode IN ('global', 'direct', 'selective', 'vpn', 'disabled', 'enabled', 'forced_vpn')),
                CHECK (applied_mode IS NULL OR applied_mode IN ('global', 'direct', 'selective', 'vpn', 'disabled', 'enabled', 'forced_vpn')),
                CHECK (apply_state IN ('clean', 'pending', 'applying', 'failed')),
                CHECK (runtime_state IN ('not_configured', 'active', 'inactive', 'missing', 'running', 'stopped', 'failed', 'degraded', 'paused')),
                CHECK (is_active IN (0, 1)),
                CHECK (is_deleted IN (0, 1))
            );

            INSERT INTO subjects (
                subject_id,
                subject_type,
                subject_role,
                implementation_kind,
                stable_key,
                display_name,
                alias,
                desired_mode,
                applied_mode,
                apply_state,
                runtime_state,
                is_active,
                is_deleted,
                first_seen_at,
                last_seen_at,
                last_traffic_at,
                inactive_since,
                deleted_at,
                metadata_json,
                created_at,
                updated_at
            )
            SELECT
                subject_id,
                CASE WHEN subject_type = 'tailscale' THEN 'tailscale_node' ELSE subject_type END,
                subject_role,
                CASE
                    WHEN implementation_kind = 'tailscale' THEN 'tailscale_node'
                    ELSE implementation_kind
                END,
                stable_key,
                display_name,
                alias,
                desired_mode,
                applied_mode,
                apply_state,
                runtime_state,
                is_active,
                is_deleted,
                first_seen_at,
                last_seen_at,
                last_traffic_at,
                inactive_since,
                deleted_at,
                metadata_json,
                created_at,
                updated_at
            FROM subjects_old;

            DROP TABLE subjects_old;

            CREATE UNIQUE INDEX IF NOT EXISTS idx_subjects_active_stable_key
            ON subjects (subject_type, stable_key)
            WHERE is_deleted = 0;

            CREATE INDEX IF NOT EXISTS idx_subjects_type_active
            ON subjects (subject_type, is_active, last_seen_at DESC);

            CREATE INDEX IF NOT EXISTS idx_subjects_type_deleted
            ON subjects (subject_type, is_deleted, deleted_at);
            """
        )
    finally:
        connection.execute("PRAGMA legacy_alter_table = OFF;")
        connection.execute("PRAGMA foreign_keys = ON;")


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


def _migrate_external_connections_from_settings(connection: sqlite3.Connection) -> None:
    if not _table_exists(connection, "settings"):
        return
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


def _external_connections_needs_system_id_rebuild(connection: sqlite3.Connection) -> bool:
    return "system_id text not null unique" in _table_sql(connection, "external_connections").lower()


def _rebuild_external_connections_system_id_not_unique(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TEMP TABLE external_connection_generated_state_backup AS
        SELECT connection_id, state_json, updated_at
        FROM external_connection_generated_state;

        CREATE TABLE external_connections_new (
            connection_id TEXT PRIMARY KEY,
            system_id TEXT NOT NULL,
            label TEXT NOT NULL,
            connection_type TEXT NOT NULL,
            runtime_type TEXT,
            replacement_target TEXT,
            location TEXT NOT NULL DEFAULT 'manual',
            address TEXT,
            integration_mode TEXT NOT NULL DEFAULT 'api_push',
            refresh_mode TEXT NOT NULL DEFAULT 'on_change',
            enabled INTEGER NOT NULL DEFAULT 1,
            value_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_seen_at TEXT,
            CHECK (connection_type IN ('external_management', 'external_vpn_module', 'external_network_source', 'display_only')),
            CHECK (location IN ('docker', 'host', 'ip', 'manual')),
            CHECK (integration_mode IN ('api_push', 'http_poll', 'command_probe', 'file_read')),
            CHECK (refresh_mode IN ('on_change', 'manual', 'interval')),
            CHECK (enabled IN (0, 1))
        );

        INSERT INTO external_connections_new (
            connection_id,
            system_id,
            label,
            connection_type,
            runtime_type,
            replacement_target,
            location,
            address,
            integration_mode,
            refresh_mode,
            enabled,
            value_json,
            created_at,
            updated_at,
            last_seen_at
        )
        SELECT
            connection_id,
            system_id,
            label,
            connection_type,
            runtime_type,
            replacement_target,
            location,
            address,
            integration_mode,
            refresh_mode,
            enabled,
            value_json,
            created_at,
            updated_at,
            last_seen_at
        FROM external_connections;

        DROP TABLE external_connections;
        ALTER TABLE external_connections_new RENAME TO external_connections;

        CREATE INDEX IF NOT EXISTS idx_external_connections_type
        ON external_connections (connection_type, runtime_type);

        CREATE INDEX IF NOT EXISTS idx_external_connections_updated
        ON external_connections (updated_at DESC);

        INSERT OR REPLACE INTO external_connection_generated_state (connection_id, state_json, updated_at)
        SELECT backup.connection_id, backup.state_json, backup.updated_at
        FROM external_connection_generated_state_backup AS backup
        JOIN external_connections AS registry
          ON registry.connection_id = backup.connection_id;

        DROP TABLE external_connection_generated_state_backup;
        """
    )


def _prune_default_provider_module_bootstrap(connection: sqlite3.Connection) -> None:
    if not _table_exists(connection, "modules"):
        return
    if not _table_exists(connection, "external_connections"):
        return
    connection.execute(
        """
        DELETE FROM modules
        WHERE module_name = 'tailscale'
          AND desired_state = 'enabled'
          AND lifecycle_mode = 'external'
          AND runtime_state = 'not_configured'
          AND apply_state = 'clean'
          AND status_text = 'External ingress module is externally managed.'
          AND error_code IS NULL
          AND error_message IS NULL
          AND NOT EXISTS (
              SELECT 1
              FROM external_connections
              WHERE connection_type = 'external_network_source'
                AND runtime_type = 'tailscale'
          )
        """
    )
    connection.execute(
        """
        DELETE FROM modules
        WHERE module_name = 'xray'
          AND desired_state = 'enabled'
          AND lifecycle_mode = 'managed'
          AND runtime_state = 'not_configured'
          AND apply_state = 'clean'
          AND status_text = 'Xray module is not initialized yet.'
          AND error_code IS NULL
          AND error_message IS NULL
          AND NOT EXISTS (
              SELECT 1
              FROM subjects
              WHERE implementation_kind = 'xray'
          )
        """
    )


def _migrate_7_to_8(connection: sqlite3.Connection) -> None:
    if (
        _table_exists(connection, "server_preferences")
        and "vpn_auto_priority" not in _columns(connection, "server_preferences")
    ):
        connection.execute(
            """
            ALTER TABLE server_preferences
            ADD COLUMN vpn_auto_priority INTEGER NOT NULL DEFAULT 0
            CHECK (vpn_auto_priority >= -1 AND vpn_auto_priority <= 5)
            """
        )
    if _server_preferences_needs_priority_range_rebuild(connection):
        _rebuild_server_preferences_priority_range(connection)
    if (
        _table_exists(connection, "server_custom_https_proxy")
        and "proxy_type" not in _columns(connection, "server_custom_https_proxy")
    ):
        connection.execute(
            """
            ALTER TABLE server_custom_https_proxy
            ADD COLUMN proxy_type TEXT NOT NULL DEFAULT 'http'
            CHECK (proxy_type IN ('http', 'socks5'))
            """
        )
    if _table_exists(connection, "modules") and "lifecycle_mode" not in _columns(connection, "modules"):
        connection.execute(
            """
            ALTER TABLE modules
            ADD COLUMN lifecycle_mode TEXT NOT NULL DEFAULT 'none'
            CHECK (lifecycle_mode IN ('none', 'managed', 'external'))
            """
        )
    if (
        _table_exists(connection, "routing_global_state")
        and "fixed_server_until" not in _columns(connection, "routing_global_state")
    ):
        connection.execute("ALTER TABLE routing_global_state ADD COLUMN fixed_server_until TEXT")
    if _table_exists(connection, "jobs"):
        connection.executescript(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_active_lock_unique
            ON jobs (lock_key)
            WHERE lock_key IS NOT NULL
              AND status IN ('queued', 'running');
            """
        )
    _apply_default_module_lifecycle_modes(connection)


def _migrate_8_to_9(connection: sqlite3.Connection) -> None:
    if not _table_exists(connection, "subjects"):
        return
    subject_columns = _columns(connection, "subjects")
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


def _migrate_9_to_10(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS watchdog_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            path_key TEXT,
            failure_candidate_json TEXT,
            last_processed_decision_id TEXT,
            last_successful_failover_at TEXT,
            failover_path_key TEXT,
            previous_target_id TEXT,
            selected_target_id TEXT,
            cooldown_until TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def _migrate_10_to_11(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS external_connections (
            connection_id TEXT PRIMARY KEY,
            system_id TEXT NOT NULL UNIQUE,
            label TEXT NOT NULL,
            connection_type TEXT NOT NULL,
            runtime_type TEXT,
            replacement_target TEXT,
            location TEXT NOT NULL DEFAULT 'manual',
            address TEXT,
            integration_mode TEXT NOT NULL DEFAULT 'api_push',
            refresh_mode TEXT NOT NULL DEFAULT 'on_change',
            enabled INTEGER NOT NULL DEFAULT 1,
            value_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_seen_at TEXT,
            CHECK (connection_type IN ('external_management', 'external_vpn_module', 'external_network_source', 'display_only')),
            CHECK (location IN ('docker', 'host', 'ip', 'manual')),
            CHECK (integration_mode IN ('api_push', 'http_poll', 'command_probe', 'file_read')),
            CHECK (refresh_mode IN ('on_change', 'manual', 'interval')),
            CHECK (enabled IN (0, 1))
        );

        CREATE INDEX IF NOT EXISTS idx_external_connections_type
        ON external_connections (connection_type, runtime_type);

        CREATE INDEX IF NOT EXISTS idx_external_connections_updated
        ON external_connections (updated_at DESC);

        CREATE TABLE IF NOT EXISTS external_connection_generated_state (
            connection_id TEXT PRIMARY KEY,
            state_json TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (connection_id) REFERENCES external_connections(connection_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS external_connection_migrations (
            migration_key TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """
    )
    _migrate_external_connections_from_settings(connection)


def _migrate_11_to_12(connection: sqlite3.Connection) -> None:
    if _external_connections_needs_system_id_rebuild(connection):
        _rebuild_external_connections_system_id_not_unique(connection)
    if not _table_exists(connection, "subjects"):
        _apply_default_module_lifecycle_modes(connection)
        _prune_default_provider_module_bootstrap(connection)
        return
    subject_columns = _columns(connection, "subjects")
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
    _backfill_subject_roles(connection)
    if _subjects_needs_open_type_rebuild(connection):
        _rebuild_subjects_to_open_subject_type(connection)
    _migrate_provider_subject_details_to_metadata(connection)
    _normalize_provider_subject_types(connection)
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_subjects_role_active
        ON subjects (subject_role, is_active, last_seen_at DESC)
        """
    )
    _apply_default_module_lifecycle_modes(connection)
    _prune_default_provider_module_bootstrap(connection)


MIGRATIONS: tuple[SchemaMigration, ...] = (
    SchemaMigration(7, 8, _migrate_7_to_8),
    SchemaMigration(8, 9, _migrate_8_to_9),
    SchemaMigration(9, 10, _migrate_9_to_10),
    SchemaMigration(10, 11, _migrate_10_to_11),
    SchemaMigration(11, 12, _migrate_11_to_12),
)


def has_schema_metadata(connection: sqlite3.Connection) -> bool:
    return _schema_version(connection) is not None


def database_has_user_tables(connection: sqlite3.Connection) -> bool:
    row = connection.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type IN ('table', 'index', 'trigger', 'view')
          AND name NOT LIKE 'sqlite_%'
        LIMIT 1
        """
    ).fetchone()
    return row is not None


def run_missing_migrations(connection: sqlite3.Connection) -> list[SchemaMigration]:
    version = _schema_version(connection)
    if version is None:
        if database_has_user_tables(connection):
            raise RuntimeError("Existing FWRouter database has no schema_meta.schema_version")
        return []
    if version > CURRENT_SCHEMA_VERSION:
        raise RuntimeError(
            f"Database schema version {version} is newer than supported {CURRENT_SCHEMA_VERSION}"
        )

    by_from_version = {migration.from_version: migration for migration in MIGRATIONS}
    applied: list[SchemaMigration] = []
    while version < CURRENT_SCHEMA_VERSION:
        migration = by_from_version.get(version)
        if migration is None:
            raise RuntimeError(f"No FWRouter schema migration from version {version}")
        migration.apply(connection)
        _set_schema_version(connection, migration.to_version)
        connection.commit()
        applied.append(migration)
        version = migration.to_version

    return applied
