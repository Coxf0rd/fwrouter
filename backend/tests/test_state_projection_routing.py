from __future__ import annotations

from fwrouter_api.db.connection import db_session
from fwrouter_api.services.state_projection import build_routing_state_projection


def test_routing_projection_marks_matching_live_runtime_healthy(monkeypatch) -> None:
    monkeypatch.setattr("fwrouter_api.services.state_projection.read_live_dataplane_payload", lambda: {"ok": True})
    monkeypatch.setattr("fwrouter_api.services.state_projection.read_applied_manifest", lambda: {"ok": True})
    monkeypatch.setattr(
        "fwrouter_api.services.state_projection.build_runtime_enforcement_state",
        lambda **_: {
            "traffic_enforcement_guaranteed": True,
            "enforcement_level": "global_selective_enforced",
            "active_mode_matches_intent": True,
            "live_global_mode": "selective",
            "live_selective_default": "direct",
        },
    )
    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO routing_global_state (id, desired_mode, applied_mode, selective_default, apply_state)
            VALUES (1, 'selective', 'selective', 'direct', 'clean')
            ON CONFLICT(id) DO UPDATE SET
                desired_mode = excluded.desired_mode,
                applied_mode = excluded.applied_mode,
                selective_default = excluded.selective_default,
                apply_state = excluded.apply_state
            """
        )

    projection = build_routing_state_projection()["routing"]

    assert projection["intent"]["mode"] == "selective"
    assert projection["execution"]["state"] == "idle"
    assert projection["observation"]["state"] == "running"
    assert projection["reconcile"]["state"] == "in_sync"
    assert projection["projection"]["state"] == "healthy"


def test_routing_projection_does_not_treat_clean_as_runtime_applied(monkeypatch) -> None:
    monkeypatch.setattr("fwrouter_api.services.state_projection.read_live_dataplane_payload", lambda: {"ok": False})
    monkeypatch.setattr("fwrouter_api.services.state_projection.read_applied_manifest", lambda: None)
    monkeypatch.setattr(
        "fwrouter_api.services.state_projection.build_runtime_enforcement_state",
        lambda **_: {
            "traffic_enforcement_guaranteed": False,
            "enforcement_level": "owned_table_missing",
            "active_mode_matches_intent": False,
            "live_global_mode": None,
            "live_selective_default": None,
        },
    )
    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO routing_global_state (id, desired_mode, applied_mode, selective_default, apply_state)
            VALUES (1, 'vpn', 'vpn', 'direct', 'clean')
            ON CONFLICT(id) DO UPDATE SET
                desired_mode = excluded.desired_mode,
                applied_mode = excluded.applied_mode,
                apply_state = excluded.apply_state,
                error_code = NULL,
                error_message = NULL
            """
        )

    projection = build_routing_state_projection()["routing"]

    assert projection["execution"]["state"] == "idle"
    assert projection["reconcile"]["state"] == "runtime_drift"
    assert projection["projection"]["state"] == "error"
