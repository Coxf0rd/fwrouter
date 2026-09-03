from __future__ import annotations

from fwrouter_api.db.connection import db_session
from fwrouter_api.services.reconcile import XrayReconciler


def _seed_xray_subject(*, apply_state: str = "clean", runtime_state: str = "active") -> None:
    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO subjects (
                subject_id, subject_type, subject_role, implementation_kind, stable_key,
                display_name, desired_mode, applied_mode, apply_state, runtime_state, is_active
            )
            VALUES ('xray:alice', 'explicit_external_client', 'vless_client', 'xray',
                    'xray:alice', 'Alice', 'vpn', 'vpn', ?, ?, 1)
            """,
            (apply_state, runtime_state),
        )
        connection.execute(
            """
            INSERT INTO subject_server_overrides (
                subject_id, selected_server_id, selected_until, apply_state
            )
            VALUES ('xray:alice', NULL, datetime('now', '+1 day'), ?)
            """,
            (apply_state,),
        )


def test_xray_pending_db_binding_applied_is_in_sync() -> None:
    _seed_xray_subject(apply_state="pending")
    reconciler = XrayReconciler(
        bindings_loader=lambda: {
            "bindings": [{"subject_id": "xray:alice", "status": "applied"}],
            "bindings_count": 1,
            "applied_count": 1,
        },
        projection_loader=lambda: {"xray": {}},
        health_loader=lambda: {"runtime_state": "running"},
    )

    result = reconciler.check()

    assert result.reconcile_state == "in_sync"
    assert result.reason == "runtime_confirmed"


def test_xray_active_client_missing_runtime_binding_is_drift() -> None:
    _seed_xray_subject()
    reconciler = XrayReconciler(
        bindings_loader=lambda: {"bindings": [], "bindings_count": 0, "applied_count": 0},
        projection_loader=lambda: {"xray": {}},
        health_loader=lambda: {"runtime_state": "running"},
    )

    result = reconciler.check()

    assert result.reconcile_state == "drift"
    assert result.reason == "binding_missing"
    assert result.details["missing_subject_ids"] == ["xray:alice"]


def test_xray_runtime_unavailable_is_failed() -> None:
    _seed_xray_subject()
    reconciler = XrayReconciler(
        bindings_loader=lambda: {
            "bindings": [{"subject_id": "xray:alice", "status": "applied"}],
        },
        projection_loader=lambda: {"xray": {}},
        health_loader=lambda: {"runtime_state": "failed"},
    )

    result = reconciler.check()

    assert result.reconcile_state == "failed"
    assert result.reason == "runtime_unavailable"
