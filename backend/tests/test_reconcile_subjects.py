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


def test_external_unknown_projection_is_not_reclassified_as_runtime_missing() -> None:
    reconciler = SubjectReconciler(projection_loader=lambda **_: {
        "subject": {
            "reconcile": {"state": "unknown", "reason_code": "EXTERNAL_SOURCE_OBSERVATION_UNCONFIRMED"},
            "projection": {"state": "unknown"},
            "observation": {"source": "external_source_observation+database"},
        }
    })

    result = reconciler.check({
        "subject_id": "source:test", "desired_mode": "vpn", "apply_state": "clean",
        "runtime_state": "offline", "is_active": 1, "implementation_kind": "provider_a",
    })

    assert result.reconcile_state == "unknown"
    assert result.reason == "EXTERNAL_SOURCE_OBSERVATION_UNCONFIRMED"
