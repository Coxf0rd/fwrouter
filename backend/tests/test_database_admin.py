from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import db_session, get_db_path, initialize_database
from fwrouter_api.services.external_connections_registry import upsert_external_connection_record
from fwrouter_api.services.control_plane_transfer import export_control_plane_snapshot
from fwrouter_api.services.database_admin import (
    cleanup_runtime_state,
    get_database_schema_state,
    rebuild_control_plane_database,
)


def _configure_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FWROUTER_STATE_DIR", str(tmp_path / "state"))
    get_settings.cache_clear()


def test_clean_database_does_not_seed_provider_specific_instances(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)

    schema_state = initialize_database()

    assert schema_state["ok"] is True
    with db_session() as connection:
        modules = {
            row["module_name"]
            for row in connection.execute("SELECT module_name FROM modules").fetchall()
        }
        external_connections_count = connection.execute(
            "SELECT COUNT(*) AS count FROM external_connections"
        ).fetchone()["count"]
        generated_state_count = connection.execute(
            "SELECT COUNT(*) AS count FROM external_connection_generated_state"
        ).fetchone()["count"]
        detail_tables = {
            row["name"]
            for row in connection.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'table'
                  AND name IN ('subject_tailscale', 'subject_xray')
                """
            ).fetchall()
        }
        subjects_schema = connection.execute(
            """
            SELECT sql
            FROM sqlite_master
            WHERE type = 'table'
              AND name = 'subjects'
            """
        ).fetchone()["sql"]

    assert {"core", "vpn", "watchdog", "selector", "subscription"}.issubset(modules)
    assert "tailscale" not in modules
    assert "xray" not in modules
    assert external_connections_count == 0
    assert generated_state_count == 0
    assert detail_tables == set()
    assert "subject_type IN" not in subjects_schema


def test_initialize_database_prunes_legacy_default_provider_module_bootstrap(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    with db_session() as connection:
        connection.executemany(
            """
            INSERT INTO modules (
                module_name, desired_state, lifecycle_mode, runtime_state,
                apply_state, status_text
            )
            VALUES (?, 'enabled', ?, 'not_configured', 'clean', ?)
            """,
            [
                (
                    "tailscale",
                    "external",
                    "External ingress module is externally managed.",
                ),
                (
                    "xray",
                    "managed",
                    "Xray module is not initialized yet.",
                ),
            ],
        )

    schema_state = initialize_database()

    assert schema_state["ok"] is True
    with db_session() as connection:
        modules = {
            row["module_name"]
            for row in connection.execute("SELECT module_name FROM modules").fetchall()
        }
    assert "tailscale" not in modules
    assert "xray" not in modules


@pytest.mark.no_database_autoinit
def test_initialize_database_migrates_legacy_subject_type_constraint(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    db_path = get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    for path in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
        path.unlink(missing_ok=True)
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        CREATE TABLE schema_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        INSERT INTO schema_meta (key, value) VALUES ('schema_version', '10');

        CREATE TABLE subjects (
            subject_id TEXT PRIMARY KEY,
            subject_type TEXT NOT NULL,
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
            CHECK (subject_type IN ('lan', 'tailscale', 'tailscale_node', 'xray', 'host', 'docker', 'fwrouter'))
        );
        INSERT INTO subjects (
            subject_id, subject_type, stable_key, display_name, desired_mode, runtime_state, is_active
        )
        VALUES ('legacy-ts-1', 'tailscale', 'legacy-ts-1', 'Legacy TS', 'global', 'active', 1);
        """
    )
    connection.commit()
    connection.close()

    schema_state = initialize_database()

    assert schema_state["ok"] is True
    with db_session() as connection:
        subjects_schema = connection.execute(
            """
            SELECT sql
            FROM sqlite_master
            WHERE type = 'table'
              AND name = 'subjects'
            """
        ).fetchone()["sql"]
        subject = connection.execute(
            """
            SELECT subject_type, subject_role, implementation_kind
            FROM subjects
            WHERE subject_id = 'legacy-ts-1'
            """
        ).fetchone()
        foreign_key_problems = connection.execute("PRAGMA foreign_key_check").fetchall()

    assert "subject_type IN" not in subjects_schema
    assert subject["subject_type"] == "external_network_client"
    assert subject["subject_role"] == "external_network_source"
    assert subject["implementation_kind"] == "tailscale"
    assert foreign_key_problems == []


@pytest.mark.no_database_autoinit
def test_initialize_database_migrates_provider_detail_tables_to_subject_metadata(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    db_path = get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    for path in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
        path.unlink(missing_ok=True)
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        CREATE TABLE schema_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        INSERT INTO schema_meta (key, value) VALUES ('schema_version', '11');

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
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        INSERT INTO subjects (
            subject_id, subject_type, subject_role, implementation_kind,
            stable_key, display_name, desired_mode, runtime_state, is_active, metadata_json
        )
        VALUES
            ('ts:peer-1', 'tailscale_node', 'external_network_source', 'tailscale_node', 'ts:peer-1', 'Peer 1', 'global', 'active', 1, json('{"connection_id":"connection-a"}')),
            ('xray:client-1', 'xray', 'vless_client', 'xray', 'xray:client-1', 'Client 1', 'enabled', 'active', 1, json('{"connection_id":"xray-a"}'));

        CREATE TABLE subject_tailscale (
            subject_id TEXT PRIMARY KEY,
            node_id TEXT,
            tailscale_ip TEXT,
            hostname TEXT,
            user_name TEXT,
            online INTEGER NOT NULL DEFAULT 0,
            source_json TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        INSERT INTO subject_tailscale (
            subject_id, node_id, tailscale_ip, hostname, user_name, online, source_json
        )
        VALUES ('ts:peer-1', 'node-1', '100.64.0.10', 'phone', 'user-a', 1, '{"raw":true}');

        CREATE TABLE subject_xray (
            subject_id TEXT PRIMARY KEY,
            client_id TEXT NOT NULL,
            client_uuid TEXT,
            email TEXT,
            subscription_path TEXT,
            last_subscription_at TEXT,
            enabled INTEGER NOT NULL DEFAULT 1,
            source_json TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        INSERT INTO subject_xray (
            subject_id, client_id, client_uuid, email, subscription_path,
            last_subscription_at, enabled, source_json
        )
        VALUES (
            'xray:client-1', 'client-1', 'uuid-1', 'client@fwrouter.local',
            '/sub/client-1', '2026-08-01T00:00:00Z', 1, '{"raw":true}'
        );
        """
    )
    connection.commit()
    connection.close()

    schema_state = initialize_database()

    assert schema_state["ok"] is True
    with db_session() as connection:
        leftover_detail_tables = {
            row["name"]
            for row in connection.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'table'
                  AND name IN ('subject_tailscale', 'subject_xray')
                """
            ).fetchall()
        }
        subjects = {
            row["subject_id"]: dict(row)
            for row in connection.execute(
                """
                SELECT subject_id, subject_type, subject_role, implementation_kind, metadata_json
                FROM subjects
                ORDER BY subject_id
                """
            ).fetchall()
        }

    ts_metadata = json.loads(subjects["ts:peer-1"]["metadata_json"])
    xray_metadata = json.loads(subjects["xray:client-1"]["metadata_json"])
    assert leftover_detail_tables == set()
    assert subjects["ts:peer-1"]["subject_type"] == "external_network_client"
    assert subjects["ts:peer-1"]["implementation_kind"] == "tailscale"
    assert ts_metadata["connection_id"] == "connection-a"
    assert ts_metadata["detail"]["ip_address"] == "100.64.0.10"
    assert ts_metadata["detail"]["node_id"] == "node-1"
    assert ts_metadata["detail"]["online"] is True
    assert subjects["xray:client-1"]["subject_type"] == "explicit_external_client"
    assert subjects["xray:client-1"]["implementation_kind"] == "xray"
    assert xray_metadata["connection_id"] == "xray-a"
    assert xray_metadata["detail"]["client_id"] == "client-1"
    assert xray_metadata["detail"]["client_uuid"] == "uuid-1"
    assert xray_metadata["detail"]["email"] == "client@fwrouter.local"
    assert xray_metadata["detail"]["enabled"] is True


def test_initialize_database_preserves_existing_provider_user_state_and_connections(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    upsert_external_connection_record(
        {
            "connection_id": "connection-a",
            "system_id": "connection-a",
            "label": "Connection A",
            "connection_type": "external_network_source",
            "runtime_type": "tailscale",
            "integration_mode": "command_probe",
            "refresh_mode": "manual",
            "collector_config": {"script_id": "tailscale_status"},
        }
    )
    with db_session() as connection:
        connection.executemany(
            """
            INSERT INTO modules (
                module_name, desired_state, lifecycle_mode, runtime_state,
                apply_state, status_text
            )
            VALUES (?, ?, ?, ?, 'clean', ?)
            """,
            [
                (
                    "tailscale",
                    "enabled",
                    "external",
                    "not_configured",
                    "External ingress module is externally managed.",
                ),
                (
                    "xray",
                    "disabled",
                    "managed",
                    "not_configured",
                    "Module xray desired state set to disabled.",
                ),
            ],
        )

    schema_state = initialize_database()

    assert schema_state["ok"] is True
    with db_session() as connection:
        modules = {
            row["module_name"]: dict(row)
            for row in connection.execute("SELECT * FROM modules WHERE module_name IN ('tailscale', 'xray')").fetchall()
        }
        external_connection = connection.execute(
            "SELECT connection_id FROM external_connections WHERE connection_id = 'connection-a'"
        ).fetchone()
        generated_state = connection.execute(
            "SELECT connection_id FROM external_connection_generated_state WHERE connection_id = 'connection-a'"
        ).fetchone()

    assert modules["tailscale"]["lifecycle_mode"] == "external"
    assert modules["xray"]["desired_state"] == "disabled"
    assert external_connection is not None
    assert generated_state is not None


@pytest.mark.no_database_autoinit
def test_initialize_database_repairs_legacy_subjects_table(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    db_path = get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        CREATE TABLE schema_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        INSERT INTO schema_meta (key, value) VALUES ('schema_version', '8');

        CREATE TABLE subjects (
            subject_id TEXT PRIMARY KEY,
            subject_type TEXT NOT NULL,
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
            CHECK (subject_type IN ('lan', 'tailscale', 'xray', 'host', 'docker', 'fwrouter'))
        );
        """
    )
    connection.commit()
    connection.close()

    schema_state = get_database_schema_state()

    assert schema_state["ok"] is True
    assert schema_state["tables"]["subjects"]["ok"] is True


def test_rebuild_control_plane_database_restores_snapshot(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO subjects (
                subject_id,
                subject_type,
                subject_role,
                implementation_kind,
                stable_key,
                display_name,
                desired_mode,
                runtime_state,
                is_active
            )
            VALUES (?, 'external_network_client', 'external_network_source', 'tailscale', ?, ?, 'global', 'active', 1)
            """,
            ("tailscale-node:test-peer", "tailscale-node:test-peer", "test-peer"),
        )
        connection.execute(
            """
            UPDATE subjects
            SET metadata_json = json(?)
            WHERE subject_id = ?
            """,
            (
                json.dumps(
                    {
                        "provider": "tailscale",
                        "connection_id": "connection-a",
                        "detail": {
                            "node_id": "peer-1",
                            "tailscale_ip": "100.64.0.44",
                            "ip_address": "100.64.0.44",
                            "hostname": "test-peer",
                            "user_name": "pytest",
                            "online": True,
                        },
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                "tailscale-node:test-peer",
            ),
        )

    exported = export_control_plane_snapshot(include_secrets=False, write_file=True)
    snapshot_path = exported["file_path"]

    monkeypatch.setattr(
        "fwrouter_api.services.subject_inventory._run_script",
        lambda script_id, extra_args=None: type(
            "_Result",
            (),
            {
                "script_id": script_id,
                "stdout": "[]" if script_id == "host_services" else "",
                "stderr": "",
                "returncode": 0,
                "ok": True,
                "to_dict": lambda self: {"script_id": script_id, "ok": True},
            },
        )(),
    )
    monkeypatch.setattr(
        "fwrouter_api.services.subject_inventory.DEFAULT_XRAY_ADAPTER.list_clients",
        lambda: [],
    )

    result = rebuild_control_plane_database(
        file_path=snapshot_path,
        normalize_runtime_state=True,
        requested_by="pytest",
    )

    assert result["ok"] is True
    assert result["schema"]["summary"]["ok"] is True
    assert result["backup"]["db_path"].endswith("fwrouter.db")

    with db_session() as connection:
        subject = connection.execute(
            "SELECT subject_id, subject_type, applied_mode FROM subjects WHERE subject_id = ?",
            ("tailscale-node:test-peer",),
        ).fetchone()

    assert subject is not None
    assert subject["subject_type"] == "external_network_client"
    assert subject["applied_mode"] is None


def test_get_database_schema_state_includes_summary(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    state = get_database_schema_state()

    assert state["ok"] is True
    assert state["summary"]["ok"] is True
    assert state["summary"]["drifted_tables"] == []


def test_cleanup_runtime_state_removes_empty_duplicate_dbs_and_test_rows(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    state_dir = get_settings().paths.state_dir
    duplicate_paths = [
        state_dir / "db.sqlite",
        state_dir / "state.db",
        state_dir / "state" / "fwrouter.db",
    ]
    for path in duplicate_paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")

    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO subjects (
                subject_id,
                subject_type,
                stable_key,
                display_name,
                desired_mode,
                runtime_state,
                is_active
            )
            VALUES (?, 'lan', ?, ?, 'global', 'inactive', 0)
            """,
            ("lan-1", "lan-1", "lan-1"),
        )
        connection.execute(
            """
            INSERT INTO servers (
                server_id,
                server_name,
                provider_name,
                inventory_state
            )
            VALUES ('Test', 'Test', 'pytest', 'missing')
            """
        )
        connection.execute(
            """
            INSERT INTO server_preferences (
                server_id,
                vpn_auto,
                global_list
            )
            VALUES ('Test', 0, 0)
            """
        )
    raw = sqlite3.connect(get_db_path())
    raw.execute("PRAGMA foreign_keys = OFF")
    raw.execute(
        """
        INSERT INTO traffic_counter_snapshots (
            counter_key,
            subject_id,
            path,
            rx_bytes,
            tx_bytes,
            metadata_json
        )
        VALUES ('orphan:test', 'missing-subject', 'vpn', 1, 2, '{}')
        """
    )
    raw.commit()
    raw.close()

    result = cleanup_runtime_state(requested_by="pytest")

    assert result["ok"] is True
    assert result["deleted_subject_rows"] >= 1
    assert result["deleted_server_rows"] == 1
    assert result["deleted_server_preference_rows"] == 1
    assert result["deleted_snapshot_rows"] == 1
    for path in duplicate_paths:
        assert not path.exists()
