from __future__ import annotations

from fwrouter_api.db.connection import db_session
from fwrouter_api.services.reconcile import RoutingReconciler


def _seed_routing(*, desired_mode: str = "vpn", apply_state: str = "clean") -> None:
    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO routing_global_state (
                id, desired_mode, applied_mode, selective_default, server_mode, apply_state
            )
            VALUES (1, ?, ?, 'direct', 'auto', ?)
            """,
            (desired_mode, desired_mode, apply_state),
        )


def test_routing_intent_matches_dataplane_observation_is_in_sync() -> None:
    _seed_routing(desired_mode="vpn")
    reconciler = RoutingReconciler(
        runtime_loader=lambda: {
            "traffic_enforcement_guaranteed": True,
            "active_mode_matches_intent": True,
            "live_global_mode": "vpn",
            "enforcement_level": "owned_table_ready",
        },
        live_payload_loader=lambda: {"ok": True},
        projection_loader=lambda: {"routing": {}},
    )

    result = reconciler.check()

    assert result.reconcile_state == "in_sync"


def test_routing_runtime_unavailable_is_failed() -> None:
    _seed_routing(desired_mode="vpn")
    reconciler = RoutingReconciler(
        runtime_loader=lambda: {
            "traffic_enforcement_guaranteed": False,
            "active_mode_matches_intent": False,
        },
        live_payload_loader=lambda: {"ok": False, "error_code": "DATAPLANE_UNAVAILABLE"},
        projection_loader=lambda: {"routing": {}},
    )

    result = reconciler.check()

    assert result.reconcile_state == "failed"
    assert result.reason == "DATAPLANE_UNAVAILABLE"
