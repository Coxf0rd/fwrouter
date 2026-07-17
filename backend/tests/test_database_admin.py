from __future__ import annotations
from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import initialize_database


import json
import sqlite3
from pathlib import Path

from fwrouter_api.db.connection import db_session, get_db_path, initialize_database
from fwrouter_api.services.control_plane_transfer import export_control_plane_snapshot
from fwrouter_api.services.database_admin import (
    cleanup_runtime_state,
    get_database_schema_state,
    rebuild_control_plane_database,
)


def _configure_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FWROUTER_STATE_DIR", str(tmp_path / "state"))
    get_settings.cache_clear()


def test_initialize_database_reports_schema_drift_for_legacy_subjects_table(monkeypatch, tmp_path: Path) -> None:
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
        INSERT INTO schema_meta (key, value) VALUES ('schema_version', '7');

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

    assert schema_state["ok"] is False
    assert "subjects" in [
        table_name
        for table_name, table_state in schema_state["tables"].items()
        if not table_state["ok"]
    ]


def test_rebuild_control_plane_database_restores_snapshot(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

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
            VALUES (?, 'tailscale_node', ?, ?, 'global', 'active', 1)
            """,
            ("tailscale-node:test-peer", "tailscale-node:test-peer", "test-peer"),
        )
        connection.execute(
            """
            INSERT INTO subject_tailscale (
                subject_id,
                node_id,
                tailscale_ip,
                hostname,
                user_name,
                online
            )
            VALUES (?, 'peer-1', '100.64.0.44', 'test-peer', 'pytest', 1)
            """,
            ("tailscale-node:test-peer",),
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
    assert subject["subject_type"] == "tailscale_node"
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
