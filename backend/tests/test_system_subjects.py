from __future__ import annotations
from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import initialize_database


import json
from pathlib import Path

from fastapi.testclient import TestClient

from fwrouter_api.db.connection import db_session
from fwrouter_api.main import create_app


def _configure_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FWROUTER_STATE_DIR", str(tmp_path / "state"))
    get_settings.cache_clear()


def _client() -> TestClient:
    return TestClient(create_app(enable_startup_tasks=False))


def test_system_subjects_endpoint_exposes_builtin_fwrouter_subject(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    with _client() as client:
        response = client.get("/api/v2/system-subjects")

    assert response.status_code == 200
    subjects = response.json()["data"]["subjects"]
    fwrouter_global = next(subject for subject in subjects if subject["subject_id"] == "fwrouter:global")
    assert fwrouter_global["subject_type"] == "fwrouter"
    assert fwrouter_global["visibility"] == "active"
    assert fwrouter_global["can_delete"] is False
    ssh_host = next(subject for subject in subjects if subject["subject_id"] == "host:ssh")
    assert ssh_host["subject_type"] == "host"
    assert ssh_host["visibility"] == "active"
    assert ssh_host["can_delete"] is False


def test_system_subjects_delete_tombstones_missing_host_subject(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    def _fake_run(script_id: str, extra_args=None):
        if script_id == "host_services":
            class _Result:
                stdout = ""
                stderr = ""
                returncode = 0
                script_id = "host_services"

                @property
                def ok(self):
                    return True

                def to_dict(self):
                    return {"script_id": "host_services", "ok": True}

            return _Result()
        raise AssertionError(script_id)

    monkeypatch.setattr("fwrouter_api.services.subject_inventory._run_script", _fake_run)

    with _client() as client:
        sync = client.post("/api/v2/subjects/sync", json={"discover_docker": False, "host_services": [
            {
                "systemd_unit": "app.service",
                "process_name": "App Service",
                "runtime_state": "running",
                "is_active": True,
            }
        ]})
        assert sync.status_code == 200

        sync_missing = client.post("/api/v2/system-subjects/sync", json={"discover_docker": False, "discover_host": True})
        assert sync_missing.status_code == 200

        subjects = client.get("/api/v2/system-subjects").json()["data"]["subjects"]
        host_subject = next(subject for subject in subjects if subject["subject_id"] == "host:app-service")
        assert host_subject["visibility"] == "missing"

        deleted = client.delete(f"/api/v2/system-subjects/{host_subject['subject_id']}")
        assert deleted.status_code == 200
        assert deleted.json()["data"]["subject"]["is_deleted"] is True


def test_system_subjects_force_fwrouter_global_direct_and_clear_override(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO servers (
                server_id,
                server_name,
                provider_name,
                inventory_state
            )
            VALUES ('server-1', 'server-1', 'pytest', 'active')
            """
        )
        connection.execute(
            """
            INSERT INTO subjects (
                subject_id,
                subject_type,
                stable_key,
                display_name,
                desired_mode,
                applied_mode,
                runtime_state,
                is_active
            )
            VALUES ('fwrouter:global', 'fwrouter', 'fwrouter:global', 'FWRouter global traffic', 'direct', 'vpn', 'running', 1)
            ON CONFLICT(subject_id) DO UPDATE SET applied_mode = 'vpn'
            """
        )
        connection.execute(
            """
            INSERT INTO subject_server_overrides (
                subject_id,
                selected_server_id,
                selected_until,
                apply_state
            )
            VALUES ('fwrouter:global', 'server-1', datetime('now', '+24 hours'), 'clean')
            """
        )

    with _client() as client:
        response = client.get("/api/v2/system-subjects")

    assert response.status_code == 200
    with db_session() as connection:
        row = connection.execute(
            "SELECT desired_mode, applied_mode FROM subjects WHERE subject_id = 'fwrouter:global'"
        ).fetchone()
        override = connection.execute(
            "SELECT selected_server_id FROM subject_server_overrides WHERE subject_id = 'fwrouter:global'"
        ).fetchone()

    assert row is not None
    assert row["desired_mode"] == "direct"
    assert row["applied_mode"] == "direct"
    assert override is None
