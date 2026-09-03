from __future__ import annotations

from fwrouter_api.db.connection import db_session
from fwrouter_api.services.reconcile import VpnReconciler


def _seed_vpn_intent() -> None:
    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO servers (server_id, server_name, inventory_state)
            VALUES ('server-1', 'Server 1', 'active')
            """
        )
        connection.execute(
            """
            INSERT INTO routing_global_state (
                id, desired_mode, applied_mode, selective_default, server_mode,
                active_auto_server_id, apply_state
            )
            VALUES (1, 'vpn', 'vpn', 'direct', 'auto', 'server-1', 'clean')
            """
        )


def test_vpn_enabled_runtime_active_is_in_sync() -> None:
    _seed_vpn_intent()
    reconciler = VpnReconciler(
        health_loader=lambda: {"runtime_state": "running", "active_server_id": "server-1"},
        projection_loader=lambda: {"vpn": {}},
    )

    result = reconciler.check()

    assert result.reconcile_state == "in_sync"


def test_vpn_enabled_runtime_missing_is_drift() -> None:
    _seed_vpn_intent()
    reconciler = VpnReconciler(
        health_loader=lambda: {"runtime_state": "stopped", "active_server_id": None},
        projection_loader=lambda: {"vpn": {}},
    )

    result = reconciler.check()

    assert result.reconcile_state == "drift"
    assert result.reason == "adapter_unavailable"
