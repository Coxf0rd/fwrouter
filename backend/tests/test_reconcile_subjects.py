from __future__ import annotations

from fwrouter_api.services.reconcile import SubjectReconciler


def test_subject_enabled_runtime_missing_is_drift() -> None:
    reconciler = SubjectReconciler(projection_loader=lambda **_: {"subject": {}})

    result = reconciler.check(
        {
            "subject_id": "lan:phone",
            "desired_mode": "vpn",
            "applied_mode": "vpn",
            "apply_state": "clean",
            "runtime_state": "missing",
            "is_active": 1,
            "implementation_kind": "lan",
        }
    )

    assert result.reconcile_state == "drift"
    assert result.reason == "runtime_missing"


def test_subject_active_runtime_active_is_in_sync() -> None:
    reconciler = SubjectReconciler(
        projection_loader=lambda **_: {
            "subject": {
                "reconcile": {"state": "in_sync"},
                "projection": {"state": "healthy"},
            }
        }
    )

    result = reconciler.check(
        {
            "subject_id": "lan:laptop",
            "desired_mode": "vpn",
            "applied_mode": "vpn",
            "apply_state": "clean",
            "runtime_state": "active",
            "is_active": 1,
            "implementation_kind": "lan",
        }
    )

    assert result.reconcile_state == "in_sync"
