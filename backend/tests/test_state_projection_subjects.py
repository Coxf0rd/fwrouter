from __future__ import annotations

from fwrouter_api.db.connection import db_session
from fwrouter_api.services.state_projection import build_subject_state_projection


def _seed_lan_subject(*, active: bool, applied_mode: str | None = "global") -> None:
    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO subjects (
                subject_id, subject_type, subject_role, implementation_kind, stable_key,
                display_name, desired_mode, applied_mode, apply_state, runtime_state, is_active
            )
            VALUES ('lan:test', 'lan', 'lan_client', 'lan', 'lan:test',
                    'LAN test', 'global', ?, 'clean', ?, ?)
            """,
            (applied_mode, "active" if active else "inactive", 1 if active else 0),
        )
        connection.execute(
            """
            INSERT INTO subject_lan (subject_id, ip_address, mac_address, hostname)
            VALUES ('lan:test', '192.168.50.10', 'aa:bb:cc:dd:ee:ff', 'desktop')
            """
        )


def test_subject_projection_marks_inactive_as_inactive_not_degraded(monkeypatch) -> None:
    _seed_lan_subject(active=False)
    monkeypatch.setattr(
        "fwrouter_api.services.subject_policy.build_runtime_enforcement_state",
        lambda **_: {"supported_modes": {"direct": True, "selective": True, "vpn": True}},
    )

    subject = build_subject_state_projection(subject_id="lan:test")["subject"]

    assert subject["observation"]["state"] == "inactive"
    assert subject["reconcile"]["state"] == "not_applicable"
    assert subject["projection"]["state"] == "inactive"


def test_subject_projection_keeps_legacy_applied_mode_ambiguity_visible(monkeypatch) -> None:
    _seed_lan_subject(active=True, applied_mode=None)
    monkeypatch.setattr(
        "fwrouter_api.services.state_projection.build_runtime_enforcement_state",
        lambda **_: {
            "supported_modes": {"direct": True, "selective": True, "vpn": True},
            "traffic_enforcement_guaranteed": True,
            "enforcement_level": "global_selective_enforced",
            "active_mode_matches_intent": True,
        },
    )

    subject = build_subject_state_projection(subject_id="lan:test")["subject"]

    assert subject["execution"]["legacy_apply_state"] == "clean"
    assert subject["execution"]["applied_mode"] is None
    assert subject["reconcile"]["state"] == "legacy_ambiguous"
    assert subject["projection"]["state"] == "warning"
