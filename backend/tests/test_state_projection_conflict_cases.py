from __future__ import annotations

from fwrouter_api.db.connection import db_session
from fwrouter_api.services.state_projection import build_subject_state_projection, compute_reconcile_state


def test_projection_conflict_cases_helper_marks_mode_mismatch_as_drift() -> None:
    reconcile = compute_reconcile_state(
        intent_mode="selective",
        live_mode="direct",
        execution_state="idle",
        observation_state="running",
        drift_reason="ROUTING_LIVE_MODE_MISMATCH",
        details={"desired_mode": "selective", "live_mode": "direct"},
    )

    assert reconcile.state == "runtime_drift"
    assert reconcile.reason_code == "ROUTING_LIVE_MODE_MISMATCH"
    assert reconcile.details["desired_mode"] == "selective"


def test_projection_conflict_cases_subject_exposes_identity_effective_and_reason(monkeypatch) -> None:
    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO subjects (
                subject_id, subject_type, subject_role, implementation_kind, stable_key,
                display_name, desired_mode, applied_mode, apply_state, runtime_state, is_active
            )
            VALUES (
                'host:test', 'host', 'host_runtime', 'host', 'host:test',
                'Host process', 'direct', 'direct', 'clean', 'active', 1
            )
            """
        )
    monkeypatch.setattr(
        "fwrouter_api.services.state_projection.build_runtime_enforcement_state",
        lambda **_: {
            "supported_modes": {"direct": True, "selective": True, "vpn": True},
            "traffic_enforcement_guaranteed": True,
            "enforcement_level": "global_selective_enforced",
            "active_mode_matches_intent": True,
        },
    )

    subject = build_subject_state_projection(subject_id="host:test")["subject"]

    assert subject["identity"] == {
        "subject_id": "host:test",
        "stable_key": "host:test",
        "display_name": "Host process",
    }
    assert subject["intent"]["mode"] == "direct"
    assert subject["effective"]["mode"] == "direct"
    assert subject["effective"]["dataplane_path"] == "direct"
    assert subject["reason"]["mode_source"] in {"subject", "admin_locked"}
