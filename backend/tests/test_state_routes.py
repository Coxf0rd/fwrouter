from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from fwrouter_api.db.connection import db_session
from fwrouter_api.main import create_app


def _table_counts() -> dict[str, int]:
    with db_session() as connection:
        rows = connection.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
              AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        ).fetchall()
        return {
            str(row["name"]): int(
                connection.execute(f"SELECT COUNT(*) AS count FROM {row['name']}").fetchone()["count"]
            )
            for row in rows
        }


def _table_rows(table: str) -> list[dict[str, object]]:
    with db_session() as connection:
        rows = connection.execute(f'SELECT * FROM "{table}"').fetchall()
    return [dict(row) for row in rows]


def test_state_endpoints_are_get_only_and_read_only(monkeypatch) -> None:
    monkeypatch.setattr("fwrouter_api.services.state_projection.read_live_dataplane_payload", lambda: {"ok": True})
    monkeypatch.setattr("fwrouter_api.services.state_projection.read_applied_manifest", lambda: {"ok": True})
    monkeypatch.setattr(
        "fwrouter_api.services.state_projection.build_runtime_enforcement_state",
        lambda **_: {
            "traffic_enforcement_guaranteed": True,
            "enforcement_level": "global_direct_enforced",
            "active_mode_matches_intent": True,
            "live_global_mode": "direct",
            "live_selective_default": "direct",
            "supported_modes": {"direct": True, "selective": True, "vpn": True},
        },
    )
    client = TestClient(create_app(enable_startup_tasks=False))
    before = _table_counts()

    for path in (
        "/api/v2/state/system",
        "/api/v2/state/modules",
        "/api/v2/state/subjects",
        "/api/v2/state/routing",
        "/api/v2/state/watchdog",
        "/api/v2/state/rules",
        "/api/v2/state/xray",
        "/api/v2/state/vpn",
    ):
        response = client.get(path)
        assert response.status_code == 200
        assert response.json()["ok"] is True

    after = _table_counts()
    assert after == before


def test_read_endpoints_do_not_bootstrap_builtin_subjects_or_routing_state(monkeypatch) -> None:
    monkeypatch.setattr("fwrouter_api.services.dataplane_status._read_live_dataplane_payload", lambda: None)
    monkeypatch.setattr("fwrouter_api.services.dataplane_status.read_applied_manifest", lambda: None)
    monkeypatch.setattr("fwrouter_api.services.dataplane_status.probe_live_global_mode", lambda: {"ok": True, "mode": "direct"})
    monkeypatch.setattr("fwrouter_api.services.state_projection.read_live_dataplane_payload", lambda: None)
    monkeypatch.setattr("fwrouter_api.services.state_projection.read_applied_manifest", lambda: None)
    monkeypatch.setattr("fwrouter_api.services.state_snapshot.read_live_dataplane_payload", lambda: None)
    monkeypatch.setattr("fwrouter_api.services.state_snapshot.read_applied_manifest", lambda: None)
    monkeypatch.setattr("fwrouter_api.services.reconcile.read_live_dataplane_payload", lambda: None)

    client = TestClient(create_app(enable_startup_tasks=False))
    before_counts = _table_counts()
    before_subjects = _table_rows("subjects")
    before_routing = _table_rows("routing_global_state")

    for path in (
        "/api/v2/system/summary",
        "/api/v2/state/system",
        "/api/v2/state/subjects",
        "/api/v2/diagnose",
        "/api/v2/reconcile",
    ):
        response = client.get(path)
        assert response.status_code == 200

    assert _table_counts() == before_counts
    assert _table_rows("subjects") == before_subjects
    assert _table_rows("routing_global_state") == before_routing


@pytest.mark.no_database_autoinit
def test_system_summary_get_does_not_initialize_empty_database(monkeypatch) -> None:
    client = TestClient(create_app(enable_startup_tasks=False))

    with db_session() as connection:
        before_tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ).fetchall()

    response = client.get("/api/v2/system/summary")

    with db_session() as connection:
        after_tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ).fetchall()

    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert [row["name"] for row in after_tables] == [row["name"] for row in before_tables]


def test_routing_global_get_does_not_expire_fixed_server_ttl(monkeypatch) -> None:
    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO servers (
                server_id,
                server_name,
                provider_name,
                inventory_state
            )
            VALUES ('server-old', 'server-old', 'pytest', 'active')
            """
        )
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
                fixed_server_until,
                apply_state
            )
            VALUES (
                1,
                'vpn',
                'vpn',
                'direct',
                'fixed',
                'server-old',
                'server-old',
                '2026-01-01 00:00:00',
                'clean'
            )
            """
        )
    before = _table_rows("routing_global_state")
    client = TestClient(create_app(enable_startup_tasks=False))

    response = client.get("/api/v2/routing/global")

    assert response.status_code == 200
    assert response.json()["data"]["routing"]["server_mode"] == "fixed"
    assert _table_rows("routing_global_state") == before


def test_state_subject_endpoint_preserves_legacy_fields(monkeypatch) -> None:
    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO subjects (
                subject_id, subject_type, subject_role, implementation_kind, stable_key,
                display_name, desired_mode, applied_mode, apply_state, runtime_state, is_active
            )
            VALUES ('lan:route-test', 'lan', 'lan_client', 'lan', 'lan:route-test',
                    'LAN route test', 'global', NULL, 'clean', 'active', 1)
            """
        )
        connection.execute(
            """
            INSERT INTO subject_lan (subject_id, ip_address)
            VALUES ('lan:route-test', '192.168.50.20')
            """
        )
    monkeypatch.setattr(
        "fwrouter_api.services.state_projection.build_runtime_enforcement_state",
        lambda **_: {
            "supported_modes": {"direct": True, "selective": True, "vpn": True},
            "traffic_enforcement_guaranteed": True,
            "enforcement_level": "global_direct_enforced",
            "active_mode_matches_intent": True,
        },
    )
    client = TestClient(create_app(enable_startup_tasks=False))

    response = client.get("/api/v2/state/subjects/lan:route-test")

    assert response.status_code == 200
    subject = response.json()["data"]["subject"]
    assert subject["legacy"]["raw"]["desired_mode"] == "global"
    assert subject["legacy"]["raw"]["apply_state"] == "clean"
    assert "intent" in subject
    assert "projection" in subject
