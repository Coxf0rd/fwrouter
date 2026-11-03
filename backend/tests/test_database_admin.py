from __future__ import annotations
from _test_support import configure_test_state_dir as _configure_env, offline_maintenance_test_lease

import json
import sqlite3
from pathlib import Path

import pytest

from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import db_session, get_db_path, initialize_database
from fwrouter_api.services.external_connections_registry import upsert_external_connection_record
from fwrouter_api.services.control_plane_transfer import export_control_plane_snapshot
from fwrouter_api.services.database_admin import (
    backup_database_file,
    cleanup_runtime_state,
    get_database_schema_state,
    rebuild_control_plane_database,
)
from fwrouter_api.services import database_admin as database_admin_module





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
    assert (get_db_path().parent.stat().st_mode & 0o777) == 0o700
    assert (get_db_path().stat().st_mode & 0o777) == 0o600


def test_database_backup_is_private(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    result = backup_database_file()
    backup_path = Path(result["backup_path"])

    assert result["created"] is True
    assert (backup_path.parent.stat().st_mode & 0o777) == 0o700
    assert (backup_path.stat().st_mode & 0o777) == 0o600


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
def test_rebuild_control_plane_database_restores_snapshot(
    monkeypatch,
    tmp_path: Path,
    isolated_host_observations,
) -> None:
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

    with offline_maintenance_test_lease(monkeypatch):
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


def test_online_database_rebuild_refuses_existing_selection_fence_without_mutation(monkeypatch, tmp_path: Path) -> None:
    from fwrouter_api.db.connection import get_db_path

    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    with db_session() as connection:
        connection.execute(
            "INSERT INTO settings (key, value_json) VALUES ('routing.auto_selection_revision', '0')"
        )
    exported = export_control_plane_snapshot(include_secrets=False, write_file=False)
    db_path = get_db_path()
    before = db_path.read_bytes()
    backups_before = set((tmp_path / "state" / "backups").glob("*"))

    with offline_maintenance_test_lease(monkeypatch):
        result = rebuild_control_plane_database(snapshot=exported["snapshot"], requested_by="pytest")

    assert result["ok"] is False
    assert result["error"]["code"] == "DATABASE_REBUILD_SELECTION_FENCE_REQUIRED"
    assert db_path.read_bytes() == before
    assert set((tmp_path / "state" / "backups").glob("*")) == backups_before


@pytest.mark.no_database_autoinit
def test_unqualified_rebuild_denies_before_resolution_lock_backup_or_database_change(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from fwrouter_api.core.config import get_settings
    from fwrouter_api.db.connection import get_db_path

    _configure_env(monkeypatch, tmp_path)
    settings = get_settings()
    db_path = get_db_path()
    monkeypatch.setattr(
        database_admin_module, "resolve_control_plane_snapshot_source",
        lambda **_kwargs: pytest.fail("snapshot resolution must follow offline admission"),
    )
    monkeypatch.setattr(
        database_admin_module, "backup_database_file",
        lambda: pytest.fail("backup must follow offline admission"),
    )

    result = rebuild_control_plane_database(file_path="missing.json")

    assert result["ok"] is False
    assert result["stage"] == "maintenance_admission"
    assert result["error"]["code"] == "DATABASE_REBUILD_MAINTENANCE_CONTEXT_UNQUALIFIED"
    assert not db_path.exists()
    assert not settings.paths.run_dir.exists()
    assert not (settings.paths.state_dir / "backups").exists()


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


def test_cleanup_runtime_state_fences_eligible_test_server_removal(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    with db_session() as connection:
        connection.execute(
            "INSERT INTO servers (server_id, server_name, provider_name, inventory_state) VALUES ('test', 'test', 'pytest', 'active')"
        )
        connection.execute("INSERT INTO server_preferences (server_id, vpn_auto, global_list) VALUES ('test', 1, 1)")
        connection.execute("DELETE FROM settings WHERE key='routing.auto_selection_revision'")

    result = cleanup_runtime_state(requested_by="pytest")

    assert result["deleted_server_rows"] == 1
    with db_session() as connection:
        revision = connection.execute(
            "SELECT value_json FROM settings WHERE key='routing.auto_selection_revision'"
        ).fetchone()
    assert revision is not None and json.loads(revision["value_json"]) == 1
