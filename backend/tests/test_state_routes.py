from __future__ import annotations

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
