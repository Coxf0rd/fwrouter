from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from fwrouter_api.core.config import get_settings
from fwrouter_api.db import migrations
from fwrouter_api.db.connection import connect, initialize_database
from fwrouter_api.services.live_probe_cache import clear_live_probe_cache
from fwrouter_api.services.bootstrap import bootstrap_backend


def _configure_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FWROUTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("FWROUTER_STARTUP_RECOVERY_ENABLED", "false")
    get_settings.cache_clear()
    clear_live_probe_cache()


def _connect_raw() -> sqlite3.Connection:
    connection = connect()
    connection.row_factory = sqlite3.Row
    return connection


def _install_legacy_common_schema(connection: sqlite3.Connection, version: int) -> None:
    lifecycle_column = (
        "lifecycle_mode TEXT NOT NULL DEFAULT 'none',"
        if version >= 8
        else ""
    )
    lifecycle_check = (
        "CHECK (lifecycle_mode IN ('none', 'managed', 'external')),"
        if version >= 8
        else ""
    )
    fixed_server_until_column = "fixed_server_until TEXT," if version >= 8 else ""
    subject_role_columns = (
        """
        subject_role TEXT NOT NULL DEFAULT 'unknown',
        implementation_kind TEXT NOT NULL DEFAULT 'unknown',
        """
        if version >= 9
        else ""
    )
    subject_role_check = (
        "CHECK (subject_role IN ('unknown', 'lan_client', 'external_network_source', 'vless_client', 'docker_runtime', 'host_runtime', 'router_core')),"
        if version >= 9
        else ""
    )
    server_priority_column = (
        "vpn_auto_priority INTEGER NOT NULL DEFAULT 0,"
        if version >= 8
        else ""
    )
    server_priority_check = (
        "CHECK (vpn_auto_priority >= -1 AND vpn_auto_priority <= 5),"
        if version >= 8
        else ""
    )
    proxy_type_column = (
        "proxy_type TEXT NOT NULL DEFAULT 'http',"
        if version >= 8
        else ""
    )
    proxy_type_check = "CHECK (proxy_type IN ('http', 'socks5'))," if version >= 8 else ""
    watchdog_state = (
        """
        CREATE TABLE watchdog_state (
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
        );
        """
        if version >= 10
        else ""
    )
    external_registry = (
        """
        CREATE TABLE external_connections (
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
        CREATE INDEX idx_external_connections_type
        ON external_connections (connection_type, runtime_type);
        CREATE INDEX idx_external_connections_updated
        ON external_connections (updated_at DESC);
        CREATE TABLE external_connection_generated_state (
            connection_id TEXT PRIMARY KEY,
            state_json TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (connection_id) REFERENCES external_connections(connection_id) ON DELETE CASCADE
        );
        CREATE TABLE external_connection_migrations (
            migration_key TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """
        if version >= 11
        else ""
    )

    connection.executescript(
        f"""
        PRAGMA foreign_keys = ON;

        CREATE TABLE schema_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        INSERT INTO schema_meta (key, value) VALUES ('schema_version', '{version}');

        CREATE TABLE settings (
            key TEXT PRIMARY KEY,
            value_json TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        {external_registry}

        CREATE TABLE modules (
            module_name TEXT PRIMARY KEY,
            desired_state TEXT NOT NULL,
            {lifecycle_column}
            runtime_state TEXT NOT NULL,
            apply_state TEXT NOT NULL DEFAULT 'clean',
            status_text TEXT,
            error_code TEXT,
            error_message TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CHECK (desired_state IN ('enabled', 'disabled')),
            {lifecycle_check}
            CHECK (runtime_state IN ('not_configured', 'running', 'stopped', 'failed', 'degraded', 'paused')),
            CHECK (apply_state IN ('clean', 'pending', 'applying', 'failed'))
        );

        {watchdog_state}

        CREATE TABLE subjects (
            subject_id TEXT PRIMARY KEY,
            subject_type TEXT NOT NULL,
            {subject_role_columns}
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
            CHECK (subject_type IN ('lan', 'tailscale', 'tailscale_node', 'xray', 'host', 'docker', 'fwrouter')),
            {subject_role_check}
            CHECK (desired_mode IN ('global', 'direct', 'selective', 'vpn', 'disabled', 'enabled', 'forced_vpn')),
            CHECK (applied_mode IS NULL OR applied_mode IN ('global', 'direct', 'selective', 'vpn', 'disabled', 'enabled', 'forced_vpn')),
            CHECK (apply_state IN ('clean', 'pending', 'applying', 'failed')),
            CHECK (runtime_state IN ('not_configured', 'active', 'inactive', 'missing', 'running', 'stopped', 'failed', 'degraded', 'paused')),
            CHECK (is_active IN (0, 1)),
            CHECK (is_deleted IN (0, 1))
        );
        CREATE UNIQUE INDEX idx_subjects_active_stable_key
        ON subjects (subject_type, stable_key)
        WHERE is_deleted = 0;
        CREATE INDEX idx_subjects_type_active
        ON subjects (subject_type, is_active, last_seen_at DESC);
        CREATE INDEX idx_subjects_type_deleted
        ON subjects (subject_type, is_deleted, deleted_at);

        CREATE TABLE subject_tailscale (
            subject_id TEXT PRIMARY KEY,
            node_id TEXT,
            tailscale_ip TEXT,
            hostname TEXT,
            user_name TEXT,
            online INTEGER NOT NULL DEFAULT 0,
            source_json TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CHECK (online IN (0, 1)),
            FOREIGN KEY (subject_id) REFERENCES subjects(subject_id) ON DELETE CASCADE
        );

        CREATE TABLE subject_xray (
            subject_id TEXT PRIMARY KEY,
            client_id TEXT NOT NULL,
            client_uuid TEXT,
            email TEXT,
            subscription_path TEXT,
            last_subscription_at TEXT,
            enabled INTEGER NOT NULL DEFAULT 1,
            source_json TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CHECK (enabled IN (0, 1)),
            FOREIGN KEY (subject_id) REFERENCES subjects(subject_id) ON DELETE CASCADE
        );

        CREATE TABLE servers (
            server_id TEXT PRIMARY KEY,
            server_name TEXT NOT NULL,
            provider_name TEXT,
            country_code TEXT,
            region TEXT,
            raw_json TEXT,
            inventory_state TEXT NOT NULL DEFAULT 'active',
            first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            missing_since TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CHECK (inventory_state IN ('active', 'missing', 'deleted'))
        );

        CREATE TABLE server_preferences (
            server_id TEXT PRIMARY KEY,
            vpn_auto INTEGER NOT NULL DEFAULT 0,
            {server_priority_column}
            global_list INTEGER NOT NULL DEFAULT 1,
            remembered_until TEXT,
            manually_deleted_at TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CHECK (vpn_auto IN (0, 1)),
            {server_priority_check}
            CHECK (global_list IN (0, 1)),
            FOREIGN KEY (server_id) REFERENCES servers(server_id) ON DELETE CASCADE
        );

        CREATE TABLE server_custom_https_proxy (
            server_id TEXT PRIMARY KEY,
            {proxy_type_column}
            host TEXT NOT NULL,
            port INTEGER NOT NULL,
            username TEXT,
            password TEXT,
            tls INTEGER NOT NULL DEFAULT 1,
            sni TEXT,
            skip_cert_verify INTEGER NOT NULL DEFAULT 0,
            path TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            {proxy_type_check}
            CHECK (port >= 1 AND port <= 65535),
            CHECK (tls IN (0, 1)),
            CHECK (skip_cert_verify IN (0, 1)),
            FOREIGN KEY (server_id) REFERENCES servers(server_id) ON DELETE CASCADE
        );

        CREATE TABLE routing_global_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            desired_mode TEXT NOT NULL DEFAULT 'direct',
            applied_mode TEXT,
            selective_default TEXT NOT NULL DEFAULT 'direct',
            server_mode TEXT NOT NULL DEFAULT 'auto',
            desired_fixed_server_id TEXT,
            applied_fixed_server_id TEXT,
            {fixed_server_until_column}
            active_auto_server_id TEXT,
            apply_state TEXT NOT NULL DEFAULT 'clean',
            error_code TEXT,
            error_message TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CHECK (desired_mode IN ('direct', 'selective', 'vpn')),
            CHECK (applied_mode IS NULL OR applied_mode IN ('direct', 'selective', 'vpn')),
            CHECK (selective_default IN ('direct', 'vpn')),
            CHECK (server_mode IN ('auto', 'fixed')),
            CHECK (apply_state IN ('clean', 'pending', 'applying', 'failed'))
        );

        CREATE TABLE jobs (
            job_id TEXT PRIMARY KEY,
            job_type TEXT NOT NULL,
            status TEXT NOT NULL,
            lock_key TEXT,
            requested_by TEXT,
            input_json TEXT,
            result_json TEXT,
            error_code TEXT,
            error_message TEXT,
            artifact_dir TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            started_at TEXT,
            finished_at TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CHECK (status IN ('queued', 'running', 'success', 'failed', 'cancelled'))
        );
        """
    )
    connection.commit()


def _insert_legacy_data(connection: sqlite3.Connection, version: int) -> None:
    module_columns = (
        "module_name, desired_state, lifecycle_mode, runtime_state, status_text"
        if version >= 8
        else "module_name, desired_state, runtime_state, status_text"
    )
    module_values = (
        "'core', 'enabled', 'none', 'not_configured', 'core seed'"
        if version >= 8
        else "'core', 'enabled', 'not_configured', 'core seed'"
    )
    subject_columns = (
        "subject_id, subject_type, subject_role, implementation_kind, stable_key, display_name, desired_mode, is_active, metadata_json"
        if version >= 9
        else "subject_id, subject_type, stable_key, display_name, desired_mode, is_active, metadata_json"
    )
    lan_values = (
        "'lan:aa', 'lan', 'unknown', 'unknown', 'aa', 'Laptop', 'vpn', 1, '{\"keep\":\"yes\"}'"
        if version >= 9
        else "'lan:aa', 'lan', 'aa', 'Laptop', 'vpn', 1, '{\"keep\":\"yes\"}'"
    )
    tailscale_values = (
        "'tailscale:node1', 'tailscale_node', 'external_network_source', 'tailscale_node', 'node1', 'TS node', 'vpn', 1, NULL"
        if version >= 9
        else "'tailscale:node1', 'tailscale_node', 'node1', 'TS node', 'vpn', 1, NULL"
    )

    connection.executescript(
        f"""
        INSERT INTO modules ({module_columns}) VALUES ({module_values});
        INSERT INTO modules ({module_columns}) VALUES (
            {'"xray", "enabled", "managed", "not_configured", "Xray module is not initialized yet."' if version >= 8 else '"xray", "enabled", "not_configured", "Xray module is not initialized yet."'}
        );
        INSERT INTO modules ({module_columns}) VALUES (
            {'"tailscale", "enabled", "external", "not_configured", "External ingress module is externally managed."' if version >= 8 else '"tailscale", "enabled", "not_configured", "External ingress module is externally managed."'}
        );

        INSERT INTO subjects ({subject_columns}) VALUES ({lan_values});
        INSERT INTO subjects ({subject_columns}) VALUES ({tailscale_values});
        INSERT INTO subject_tailscale (
            subject_id, node_id, tailscale_ip, hostname, user_name, online, source_json
        )
        VALUES (
            'tailscale:node1', 'node-1', '100.64.0.1', 'ts-host', 'user@example', 1, '{{"raw":true}}'
        );

        INSERT INTO servers (server_id, server_name, provider_name)
        VALUES ('srv-1', 'Server One', 'pytest');
        INSERT INTO server_preferences (server_id, vpn_auto, global_list)
        VALUES ('srv-1', 1, 1);
        INSERT INTO server_custom_https_proxy (server_id, host, port)
        VALUES ('srv-1', 'proxy.example', 443);
        INSERT INTO routing_global_state (id, desired_mode, applied_mode)
        VALUES (1, 'vpn', 'selective');
        INSERT INTO jobs (job_id, job_type, status, lock_key)
        VALUES ('job-1', 'apply_mutation', 'success', 'apply');
        """
    )

    if version >= 10:
        connection.execute(
            """
            INSERT INTO settings (key, value_json)
            VALUES ('ui.admin_client_display.v1', json(?))
            """,
            (
                json.dumps(
                    {
                        "custom_external_systems": [
                            {
                                "connection_id": "custom-api",
                                "system_id": "custom-api",
                                "label": "Custom API",
                                "connection_type": "external_management",
                                "integration_mode": "api_push",
                            }
                        ]
                    },
                    sort_keys=True,
                ),
            ),
        )

    if version >= 11:
        connection.execute(
            """
            INSERT INTO external_connections (
                connection_id, system_id, label, connection_type, runtime_type,
                location, integration_mode, refresh_mode, enabled, value_json
            )
            VALUES (
                'conn-1', 'shared-system', 'Conn 1', 'external_management', '',
                'manual', 'api_push', 'on_change', 1, '{}'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO external_connection_generated_state (connection_id, state_json)
            VALUES ('conn-1', '{"state":true}')
            """
        )
    connection.commit()


def _schema_version() -> str:
    with _connect_raw() as connection:
        return str(
            connection.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()["value"]
        )


def _table_rows(connection: sqlite3.Connection, table_name: str) -> list[dict[str, object]]:
    columns = [str(row["name"]) for row in connection.execute(f"PRAGMA table_info({table_name})")]
    order_by = ", ".join(columns)
    return [
        dict(row)
        for row in connection.execute(f"SELECT * FROM {table_name} ORDER BY {order_by}").fetchall()
    ]


def _table_snapshot(connection: sqlite3.Connection, table_names: list[str]) -> dict[str, list[dict[str, object]]]:
    return {table_name: _table_rows(connection, table_name) for table_name in table_names}


@pytest.mark.no_database_autoinit
def test_fresh_database_starts_at_current_schema(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)

    schema_state = initialize_database()

    assert schema_state["ok"] is True
    assert schema_state["actual_schema_version"] == "12"
    with _connect_raw() as connection:
        rows = {
            row["module_name"]: row["lifecycle_mode"]
            for row in connection.execute(
                "SELECT module_name, lifecycle_mode FROM modules"
            ).fetchall()
        }
    assert rows == {
        "core": "managed",
        "vpn": "managed",
        "watchdog": "managed",
        "selector": "managed",
        "subscription": "managed",
    }


@pytest.mark.no_database_autoinit
@pytest.mark.parametrize("version", [7, 10, 11])
def test_supported_legacy_versions_upgrade_to_current(monkeypatch, tmp_path: Path, version: int) -> None:
    _configure_env(monkeypatch, tmp_path)
    with _connect_raw() as connection:
        _install_legacy_common_schema(connection, version)
        _insert_legacy_data(connection, version)

    schema_state = initialize_database()

    assert schema_state["ok"] is True
    assert schema_state["actual_schema_version"] == "12"
    with _connect_raw() as connection:
        lan = connection.execute(
            """
            SELECT subject_type, subject_role, implementation_kind, desired_mode, metadata_json
            FROM subjects
            WHERE subject_id = 'lan:aa'
            """
        ).fetchone()
        ts = connection.execute(
            """
            SELECT subject_type, subject_role, implementation_kind, metadata_json
            FROM subjects
            WHERE subject_id = 'tailscale:node1'
            """
        ).fetchone()
        server = connection.execute(
            """
            SELECT sp.vpn_auto, sp.vpn_auto_priority, sp.global_list, sc.proxy_type, sc.host
            FROM server_preferences AS sp
            JOIN server_custom_https_proxy AS sc ON sc.server_id = sp.server_id
            WHERE sp.server_id = 'srv-1'
            """
        ).fetchone()

    assert dict(lan) == {
        "subject_type": "lan",
        "subject_role": "lan_client",
        "implementation_kind": "lan",
        "desired_mode": "vpn",
        "metadata_json": '{"keep":"yes"}',
    }
    assert ts["subject_type"] == "external_network_client"
    assert ts["subject_role"] == "external_network_source"
    assert ts["implementation_kind"] == "tailscale"
    assert json.loads(ts["metadata_json"])["detail"]["tailscale_ip"] == "100.64.0.1"
    assert dict(server) == {
        "vpn_auto": 1,
        "vpn_auto_priority": 0,
        "global_list": 1,
        "proxy_type": "http",
        "host": "proxy.example",
    }


@pytest.mark.no_database_autoinit
def test_upgrade_runs_sequential_migrations(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    with _connect_raw() as connection:
        _install_legacy_common_schema(connection, 7)
        _insert_legacy_data(connection, 7)
        applied = migrations.run_missing_migrations(connection)
        connection.executescript(Path(migrations.__file__).with_name("schema.sql").read_text())
        schema_state = initialize_database()

    assert [(item.from_version, item.to_version) for item in applied] == [
        (7, 8),
        (8, 9),
        (9, 10),
        (10, 11),
        (11, 12),
    ]
    assert schema_state["ok"] is True
    assert _schema_version() == "12"


@pytest.mark.no_database_autoinit
def test_repeated_startup_after_upgrade_does_not_rerun_migrations(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    with _connect_raw() as connection:
        _install_legacy_common_schema(connection, 11)
        _insert_legacy_data(connection, 11)

    first = initialize_database()
    with _connect_raw() as connection:
        before = connection.execute(
            """
            SELECT updated_at
            FROM schema_meta
            WHERE key = 'schema_version'
            """
        ).fetchone()["updated_at"]

    second = initialize_database()
    with _connect_raw() as connection:
        after = connection.execute(
            """
            SELECT updated_at
            FROM schema_meta
            WHERE key = 'schema_version'
            """
        ).fetchone()["updated_at"]
        subject_count = connection.execute("SELECT COUNT(*) FROM subjects").fetchone()[0]

    assert first["ok"] is True
    assert second["ok"] is True
    assert after == before
    assert subject_count == 2


@pytest.mark.no_database_autoinit
def test_schema_10_upgrade_to_current_preserves_intent_and_is_bootstrap_idempotent(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    with _connect_raw() as connection:
        _install_legacy_common_schema(connection, 10)
        _insert_legacy_data(connection, 10)

    schema_state = initialize_database()

    assert schema_state["ok"] is True
    assert schema_state["actual_schema_version"] == "12"
    with _connect_raw() as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert _schema_version() == "12"

        setting = connection.execute(
            "SELECT value_json FROM settings WHERE key = 'ui.admin_client_display.v1'"
        ).fetchone()
        routing = connection.execute(
            """
            SELECT desired_mode, applied_mode, server_mode
            FROM routing_global_state
            WHERE id = 1
            """
        ).fetchone()
        server = connection.execute(
            """
            SELECT s.server_id, s.server_name, p.vpn_auto, p.global_list, c.host, c.port
            FROM servers AS s
            JOIN server_preferences AS p ON p.server_id = s.server_id
            JOIN server_custom_https_proxy AS c ON c.server_id = s.server_id
            WHERE s.server_id = 'srv-1'
            """
        ).fetchone()
        lan = connection.execute(
            """
            SELECT subject_type, subject_role, implementation_kind, stable_key, desired_mode, metadata_json
            FROM subjects
            WHERE subject_id = 'lan:aa'
            """
        ).fetchone()
        tailscale = connection.execute(
            """
            SELECT subject_type, subject_role, implementation_kind, stable_key, desired_mode, metadata_json
            FROM subjects
            WHERE subject_id = 'tailscale:node1'
            """
        ).fetchone()
        external = connection.execute(
            """
            SELECT connection_id, system_id, label, connection_type, integration_mode, enabled
            FROM external_connections
            WHERE connection_id = 'custom-api'
            """
        ).fetchone()
        subscriptions_count = connection.execute(
            "SELECT COUNT(*) FROM subscription_clients"
        ).fetchone()[0]

    assert setting is not None
    assert dict(routing) == {
        "desired_mode": "vpn",
        "applied_mode": "selective",
        "server_mode": "auto",
    }
    assert dict(server) == {
        "server_id": "srv-1",
        "server_name": "Server One",
        "vpn_auto": 1,
        "global_list": 1,
        "host": "proxy.example",
        "port": 443,
    }
    assert dict(lan) == {
        "subject_type": "lan",
        "subject_role": "lan_client",
        "implementation_kind": "lan",
        "stable_key": "aa",
        "desired_mode": "vpn",
        "metadata_json": '{"keep":"yes"}',
    }
    assert tailscale["subject_type"] == "external_network_client"
    assert tailscale["subject_role"] == "external_network_source"
    assert tailscale["implementation_kind"] == "tailscale"
    assert tailscale["stable_key"] == "node1"
    assert tailscale["desired_mode"] == "vpn"
    assert json.loads(tailscale["metadata_json"])["detail"]["tailscale_ip"] == "100.64.0.1"
    assert dict(external) == {
        "connection_id": "custom-api",
        "system_id": "custom-api",
        "label": "Custom API",
        "connection_type": "external_management",
        "integration_mode": "api_push",
        "enabled": 1,
    }
    assert subscriptions_count == 0

    first_bootstrap = bootstrap_backend()
    assert first_bootstrap["database_schema"]["ok"] is True
    assert first_bootstrap["database_schema"]["actual_schema_version"] == "12"
    assert first_bootstrap["startup_recovery_enabled"] is False

    tracked_tables = [
        "settings",
        "routing_global_state",
        "rules_state",
        "rules_metadata",
        "servers",
        "server_preferences",
        "server_custom_https_proxy",
        "external_connections",
        "external_connection_generated_state",
        "subjects",
        "subject_server_overrides",
        "subject_user_overrides",
        "subscription_accounts",
        "subscription_clients",
        "subscription_state",
    ]
    with _connect_raw() as connection:
        after_first_bootstrap = _table_snapshot(connection, tracked_tables)

    second_bootstrap = bootstrap_backend()
    assert second_bootstrap["database_schema"]["ok"] is True
    assert second_bootstrap["database_schema"]["actual_schema_version"] == "12"
    assert second_bootstrap["startup_recovery_enabled"] is False

    with _connect_raw() as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert _table_snapshot(connection, tracked_tables) == after_first_bootstrap
