from __future__ import annotations

from fwrouter_api.db.connection import db_session
from fwrouter_api.services.state_projection import build_routing_state_projection


def test_projection_routing_drift_exposes_selective_and_dataplane_context(monkeypatch) -> None:
    monkeypatch.setattr("fwrouter_api.services.state_projection.read_live_dataplane_payload", lambda: {"ok": True})
    monkeypatch.setattr("fwrouter_api.services.state_projection.read_applied_manifest", lambda: {"ok": True})
    monkeypatch.setattr(
        "fwrouter_api.services.state_projection.build_runtime_enforcement_state",
        lambda **_: {
            "traffic_enforcement_guaranteed": True,
            "enforcement_level": "global_direct_enforced",
            "active_mode_matches_intent": False,
            "live_global_mode": "direct",
            "live_selective_default": "direct",
            "supported_modes": {"direct": True, "selective": True, "vpn": True},
            "selective_rules": {"rules_count": 7, "vpn_rules_count": 3, "direct_rules_count": 4},
            "profile": {"protected_ipv4": ["192.168.0.0/16"], "protected_ipv6": []},
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

    routing = build_routing_state_projection()["routing"]

    assert routing["reconcile"]["state"] == "runtime_drift"
    assert routing["effective"]["global_mode"] == "direct"
    assert routing["intent"]["details"]["global_mode"] == "selective"
    assert routing["observation"]["evidence"]["selective_rules"]["vpn_rules_count"] == 3
    assert routing["observation"]["evidence"]["direct_exceptions"]["protected_ipv4_count"] == 1
    assert routing["projection"]["state"] == "error"
