from __future__ import annotations
from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import initialize_database


import json
from pathlib import Path

from fastapi.testclient import TestClient

from fwrouter_api.db.connection import db_session, initialize_database
from fwrouter_api.main import create_app
from fwrouter_api.services.rules import get_manual_rules_texts, save_manual_draft
from fwrouter_api.services.subscription import save_subscription_url


def _configure_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FWROUTER_STATE_DIR", str(tmp_path / "state"))
    get_settings.cache_clear()


def _client() -> TestClient:
    return TestClient(create_app(enable_startup_tasks=False))


def _seed_server(server_id: str) -> None:
    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO servers (
                server_id,
                server_name,
                provider_name,
                inventory_state,
                raw_json
            )
            VALUES (?, ?, 'provider', 'active', '{}')
            """,
            (server_id, server_id),
        )
        connection.execute(
            """
            INSERT INTO server_preferences (
                server_id,
                vpn_auto,
                global_list
            )
            VALUES (?, 1, 1)
            """,
            (server_id,),
        )
        connection.execute(
            "INSERT OR IGNORE INTO server_ping_state (server_id, status) VALUES (?, 'success')",
            (server_id,),
        )


def _seed_subject(subject_id: str, *, desired_mode: str = "vpn") -> None:
    with db_session() as connection:
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
            VALUES (?, 'lan', ?, ?, ?, ?, 'active', 1)
            """,
            (subject_id, subject_id, subject_id, desired_mode, desired_mode),
        )
        connection.execute(
            """
            INSERT INTO subject_lan (
                subject_id,
                mac_address,
                ip_address,
                hostname,
                dhcp_hostname
            )
            VALUES (?, 'aa:bb:cc:dd:ee:ff', '192.168.77.9', ?, 'pytest')
            """,
            (subject_id, subject_id),
        )


def _seed_routing() -> None:
    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO routing_global_state (
                id,
                desired_mode,
                applied_mode,
                selective_default,
                server_mode,
                desired_fixed_server_id,
                applied_fixed_server_id,
                active_auto_server_id,
                apply_state
            )
            VALUES (1, 'vpn', 'vpn', 'direct', 'fixed', 'server-1', 'server-1', 'server-1', 'clean')
            ON CONFLICT(id) DO UPDATE SET
                desired_mode = 'vpn',
                applied_mode = 'vpn',
                selective_default = 'direct',
                server_mode = 'fixed',
                desired_fixed_server_id = 'server-1',
                applied_fixed_server_id = 'server-1',
                active_auto_server_id = 'server-1',
                apply_state = 'clean'
            """
        )


def test_control_plane_export_redacts_subscription_url_by_default(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    save_subscription_url("https://secret.example/subscription", metadata={"source": "pytest"})

    with _client() as client:
        response = client.get("/api/v2/transfer/control-plane/export")

    assert response.status_code == 200
    payload = response.json()["data"]
    snapshot = payload["snapshot"]
    assert snapshot["state"]["subscription_state"]["url"] is None
    assert snapshot["state"]["subscription_state"]["url_redacted"] is True
    assert "subscription_url_redacted" in snapshot["warnings"]
    assert payload["file_path"] is not None

    with _client() as client:
        files_response = client.get("/api/v2/transfer/control-plane/files")

    assert files_response.status_code == 200
    files_payload = files_response.json()["data"]
    assert files_payload["snapshots"]
    assert files_payload["snapshots"][0]["file_path"] == payload["file_path"]


def test_control_plane_plan_and_import_from_file(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _seed_server("server-1")
    _seed_subject("lan-transfer")
    _seed_routing()
    save_manual_draft("DIRECT plan.example\n")

    with _client() as client:
        export_response = client.get(
            "/api/v2/transfer/control-plane/export",
            params={"include_secrets": "false", "write_file": "true"},
        )
        export_payload = export_response.json()["data"]
        snapshot_path = export_payload["file_path"]

        plan_response = client.post(
            "/api/v2/transfer/control-plane/plan",
            json={"file_path": snapshot_path, "normalize_runtime_state": True},
        )

        validate_response = client.post(
            "/api/v2/transfer/control-plane/validate",
            json={"file_path": snapshot_path},
        )

        with db_session() as connection:
            connection.execute("DELETE FROM subject_lan")
            connection.execute("DELETE FROM subjects")

        import_response = client.post(
            "/api/v2/transfer/control-plane/import",
            json={"file_path": snapshot_path, "normalize_runtime_state": True},
        )

    assert plan_response.status_code == 200
    plan_payload = plan_response.json()["data"]
    assert plan_payload["source"]["kind"] == "file"
    assert plan_payload["plan"]["ok"] is True
    assert plan_payload["plan"]["post_import_expectations"]["routing_apply_required"] is True
    assert plan_payload["plan"]["scoped_egress"]["readiness"]["state"] in {"ready", "degraded", "blocked"}

    assert validate_response.status_code == 200
    assert validate_response.json()["data"]["source"]["kind"] == "file"
    assert validate_response.json()["ok"] is True

    assert import_response.status_code == 200
    assert import_response.json()["data"]["source"]["kind"] == "file"
    with db_session() as connection:
        restored_subject = connection.execute(
            "SELECT subject_id, desired_mode, applied_mode FROM subjects WHERE subject_id = ?",
            ("lan-transfer",),
        ).fetchone()
    assert restored_subject is not None
    assert restored_subject["desired_mode"] == "vpn"
    assert restored_subject["applied_mode"] is None


def test_control_plane_snapshot_file_path_must_stay_in_transfer_dir(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    outside_file = tmp_path / "outside-snapshot.json"
    outside_file.write_text("{}", encoding="utf-8")

    with _client() as client:
        response = client.post(
            "/api/v2/transfer/control-plane/plan",
            json={"file_path": str(outside_file), "normalize_runtime_state": True},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is False
    assert payload["error"]["code"] == "CONTROL_PLANE_SNAPSHOT_FILE_PATH_INVALID"


def test_control_plane_import_restores_snapshot_with_runtime_normalization(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _seed_server("server-1")
    _seed_subject("lan-transfer")
    _seed_routing()
    save_subscription_url("https://secret.example/subscription", metadata={"source": "pytest"})
    save_manual_draft("DIRECT import.example\n")
    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO subject_server_overrides (
                subject_id,
                selected_server_id,
                selected_until,
                apply_state
            )
            VALUES (?, 'server-1', datetime('now', '+24 hours'), 'clean')
            """,
            ("lan-transfer",),
        )
        connection.execute(
            """
            INSERT INTO settings (key, value_json)
            VALUES ('pytest.transfer.flag', '{"enabled": true}')
            ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json
            """
        )

    with _client() as client:
        export_response = client.get(
            "/api/v2/transfer/control-plane/export",
            params={"include_secrets": "true", "write_file": "false"},
        )
        snapshot = export_response.json()["data"]["snapshot"]

        with db_session() as connection:
            connection.execute("DELETE FROM subject_server_overrides")
            connection.execute("DELETE FROM subject_lan")
            connection.execute("DELETE FROM subjects")
            connection.execute("UPDATE modules SET desired_state = 'disabled', runtime_state = 'stopped'")
            connection.execute(
                """
                UPDATE routing_global_state
                SET desired_mode = 'direct',
                    applied_mode = 'direct',
                    apply_state = 'clean'
                WHERE id = 1
                """
            )

        validate_response = client.post(
            "/api/v2/transfer/control-plane/validate",
            json={"snapshot": snapshot},
        )
        assert validate_response.status_code == 200
        assert validate_response.json()["ok"] is True

        import_response = client.post(
            "/api/v2/transfer/control-plane/import",
            json={"snapshot": snapshot, "normalize_runtime_state": True},
        )

    assert import_response.status_code == 200
    imported = import_response.json()["data"]["import"]
    assert imported["ok"] is True
    assert imported["normalize_runtime_state"] is True
    assert imported["post_import"]["scoped_egress"]["readiness"]["state"] in {
        "ready",
        "degraded",
        "blocked",
    }

    with db_session() as connection:
        subject_row = connection.execute(
            "SELECT desired_mode, applied_mode, apply_state FROM subjects WHERE subject_id = ?",
            ("lan-transfer",),
        ).fetchone()
        routing_row = connection.execute(
            "SELECT desired_mode, applied_mode, apply_state FROM routing_global_state WHERE id = 1"
        ).fetchone()
        setting_row = connection.execute(
            "SELECT value_json FROM settings WHERE key = 'pytest.transfer.flag'"
        ).fetchone()
        subscription_row = connection.execute(
            "SELECT url FROM subscription_state WHERE id = 1"
        ).fetchone()

    assert subject_row is not None
    assert subject_row["desired_mode"] == "vpn"
    assert subject_row["applied_mode"] is None
    assert subject_row["apply_state"] == "pending"

    assert routing_row is not None
    assert routing_row["desired_mode"] == "vpn"
    assert routing_row["applied_mode"] is None
    assert routing_row["apply_state"] == "pending"

    assert setting_row is not None
    assert json.loads(setting_row["value_json"]) == {"enabled": True}
    assert subscription_row is not None
    assert subscription_row["url"] == "https://secret.example/subscription"

    rules_texts = get_manual_rules_texts()
    assert rules_texts["draft_text"] == "DIRECT import.example\n"


def test_control_plane_import_normalizes_fwrouter_subject_and_override(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    snapshot = {
        "snapshot_version": "2026-05-14.control-plane-transfer.v2",
        "meta": {"exported_by": "pytest"},
        "state": {
            "modules": [],
            "routing_global_state": {
                "id": 1,
                "desired_mode": "direct",
                "applied_mode": "direct",
                "selective_default": "direct",
                "server_mode": "auto",
                "desired_fixed_server_id": None,
                "applied_fixed_server_id": None,
                "active_auto_server_id": None,
                "apply_state": "clean",
                "error_code": None,
                "error_message": None,
            },
            "subjects": [
                {
                    "subject_id": "fwrouter:global",
                    "subject_type": "fwrouter",
                    "stable_key": "fwrouter:global",
                    "display_name": "FWRouter global traffic",
                    "desired_mode": "vpn",
                    "applied_mode": "vpn",
                    "apply_state": "clean",
                    "runtime_state": "running",
                    "is_active": 1,
                    "is_deleted": 0,
                    "first_seen_at": "2026-06-29 00:00:00",
                    "last_seen_at": "2026-06-29 00:00:00",
                    "metadata": {},
                    "created_at": "2026-06-29 00:00:00",
                    "updated_at": "2026-06-29 00:00:00",
                    "detail": {
                        "component_name": "global",
                        "source": {},
                        "updated_at": "2026-06-29 00:00:00",
                    },
                }
            ],
            "subject_server_overrides": [
                {
                    "subject_id": "fwrouter:global",
                    "selected_server_id": "server-1",
                    "selected_until": "2099-01-01 00:00:00",
                    "apply_state": "clean",
                    "error_code": None,
                    "error_message": None,
                }
            ],
            "settings": [],
            "subscription_state": {"id": 1, "url": None, "status": "not_configured"},
            "rules": {"content": {}, "metadata": {}},
            "rules_state": {},
            "rules_files": {},
            "servers": [],
            "server_preferences": [],
            "server_ping_state": [],
            "known_devices": [],
            "traffic_monthly": [],
            "jobs": [],
            "operational_logs": [],
            "xray_clients": [],
            "xray_client_history": [],
        },
        "warnings": [],
    }

    with _client() as client:
        import_response = client.post(
            "/api/v2/transfer/control-plane/import",
            json={"snapshot": snapshot, "normalize_runtime_state": True},
        )

    assert import_response.status_code == 200
    with db_session() as connection:
        subject_row = connection.execute(
            "SELECT desired_mode, applied_mode, apply_state FROM subjects WHERE subject_id = 'fwrouter:global'"
        ).fetchone()
        override_row = connection.execute(
            "SELECT selected_server_id FROM subject_server_overrides WHERE subject_id = 'fwrouter:global'"
        ).fetchone()

    assert subject_row is not None
    assert subject_row["desired_mode"] == "direct"
    assert subject_row["applied_mode"] is None
    assert subject_row["apply_state"] == "pending"
    assert override_row is None
