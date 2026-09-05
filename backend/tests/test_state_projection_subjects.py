from __future__ import annotations

from datetime import UTC, datetime

from fwrouter_api.db.connection import db_session
from fwrouter_api.services.state_projection import build_subject_state_projection
from fwrouter_api.services.tailscale_live import parse_tailscale_status_payload


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


def _seed_tailscale_subject(subject_id: str, *, active: bool, runtime_state: str = "active") -> None:
    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO subjects (
                subject_id, subject_type, subject_role, implementation_kind, stable_key,
                display_name, desired_mode, applied_mode, apply_state, runtime_state,
                is_active, last_seen_at, updated_at
            )
            VALUES (?, 'external_network_client', 'external_network_source', 'tailscale',
                    ?, ?, 'global', NULL, 'clean', ?, ?, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
            """,
            (subject_id, subject_id, subject_id, runtime_state, 1 if active else 0),
        )


def _tailscale_payload(*, peer_ids: list[str], self_id: str = "15") -> dict[str, object]:
    return {
        "Self": {
            "ID": self_id,
            "HostName": "minis",
            "DNSName": "minisk.vpn.example",
            "TailscaleIPs": ["100.64.0.12"],
            "Online": True,
        },
        "Peer": {
            peer_id: {
                "ID": peer_id,
                "HostName": f"peer-{peer_id}",
                "DNSName": f"peer-{peer_id}.vpn.example",
                "TailscaleIPs": [f"100.64.0.{peer_id}"],
                "Online": True,
            }
            for peer_id in peer_ids
        },
    }


def test_tailscale_active_persistent_online_is_healthy(monkeypatch) -> None:
    _seed_tailscale_subject("tailscale-node:18", active=True)
    monkeypatch.setattr(
        "fwrouter_api.services.state_projection.build_runtime_enforcement_state",
        lambda **_: {"supported_modes": {"direct": True, "selective": True, "vpn": True}},
    )
    monkeypatch.setattr(
        "fwrouter_api.services.state_projection.read_tailscale_live_state",
        lambda: parse_tailscale_status_payload(
            _tailscale_payload(peer_ids=["18"]),
            observed_at=datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        ),
    )

    subject = build_subject_state_projection(subject_id="tailscale-node:18")["subject"]

    assert subject["observation"]["source"] == "tailscale_status+database"
    assert subject["observation"]["state"] == "active"
    assert subject["observation"]["stale"] is False
    assert subject["reconcile"]["state"] == "in_sync"
    assert subject["projection"]["state"] == "healthy"


def test_tailscale_active_persistent_missing_is_warning(monkeypatch) -> None:
    _seed_tailscale_subject("tailscale-node:23", active=True)
    monkeypatch.setattr(
        "fwrouter_api.services.state_projection.build_runtime_enforcement_state",
        lambda **_: {"supported_modes": {"direct": True, "selective": True, "vpn": True}},
    )
    monkeypatch.setattr(
        "fwrouter_api.services.state_projection.read_tailscale_live_state",
        lambda: parse_tailscale_status_payload(
            _tailscale_payload(peer_ids=["18"]),
            observed_at=datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        ),
    )

    subject = build_subject_state_projection(subject_id="tailscale-node:23")["subject"]

    assert subject["observation"]["state"] == "missing"
    assert subject["reconcile"]["state"] == "observation_stale"
    assert subject["reconcile"]["reason_code"] == "TAILSCALE_NODE_NOT_OBSERVED"
    assert subject["projection"]["state"] == "warning"


def test_tailscale_active_persistent_offline_is_warning(monkeypatch) -> None:
    _seed_tailscale_subject("tailscale-node:30", active=True)
    payload = _tailscale_payload(peer_ids=["30"])
    payload["Peer"]["30"]["Online"] = False
    monkeypatch.setattr(
        "fwrouter_api.services.state_projection.build_runtime_enforcement_state",
        lambda **_: {"supported_modes": {"direct": True, "selective": True, "vpn": True}},
    )
    monkeypatch.setattr(
        "fwrouter_api.services.state_projection.read_tailscale_live_state",
        lambda: parse_tailscale_status_payload(
            payload,
            observed_at=datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        ),
    )

    subject = build_subject_state_projection(subject_id="tailscale-node:30")["subject"]

    assert subject["observation"]["state"] == "offline"
    assert subject["reconcile"]["state"] == "observation_stale"
    assert subject["reconcile"]["reason_code"] == "TAILSCALE_NODE_OFFLINE"
    assert subject["projection"]["state"] == "warning"


def test_tailscale_inactive_persistent_online_has_no_error_health(monkeypatch) -> None:
    _seed_tailscale_subject("tailscale-node:22", active=False, runtime_state="inactive")
    monkeypatch.setattr(
        "fwrouter_api.services.state_projection.build_runtime_enforcement_state",
        lambda **_: {"supported_modes": {"direct": True, "selective": True, "vpn": True}},
    )
    monkeypatch.setattr(
        "fwrouter_api.services.state_projection.read_tailscale_live_state",
        lambda: parse_tailscale_status_payload(
            _tailscale_payload(peer_ids=["22"]),
            observed_at=datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        ),
    )

    subject = build_subject_state_projection(subject_id="tailscale-node:22")["subject"]

    assert subject["observation"]["state"] == "inactive"
    assert subject["observation"]["evidence"]["tailscale_live"]["online"] is True
    assert subject["reconcile"]["state"] == "not_applicable"
    assert subject["projection"]["state"] == "inactive"


def test_tailscale_self_node_is_infrastructure_not_peer_subject() -> None:
    live = parse_tailscale_status_payload(_tailscale_payload(peer_ids=["18"], self_id="15"), observed_at="2026-09-05T13:00:00Z")

    assert live["self"]["subject_id"] == "tailscale-node:15"
    assert live["self"]["classification"] == "infrastructure"
    assert "tailscale-node:15" not in live["peers_by_subject_id"]


def test_tailscale_subject_projection_splits_intent_and_live_observation(monkeypatch) -> None:
    _seed_tailscale_subject("tailscale-node:18", active=True)
    _seed_tailscale_subject("tailscale-node:22", active=False, runtime_state="inactive")
    _seed_tailscale_subject("tailscale-node:23", active=True)
    monkeypatch.setattr(
        "fwrouter_api.services.state_projection.build_runtime_enforcement_state",
        lambda **_: {"supported_modes": {"direct": True, "selective": True, "vpn": True}},
    )
    monkeypatch.setattr(
        "fwrouter_api.services.state_projection.read_tailscale_live_state",
        lambda: parse_tailscale_status_payload(
            _tailscale_payload(peer_ids=["18", "22"]),
            observed_at=datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        ),
    )

    projection = build_subject_state_projection(limit=100)
    by_id = {item["entity"]["id"]: item for item in projection["items"]}

    assert by_id["tailscale-node:18"]["projection"]["state"] == "healthy"
    assert by_id["tailscale-node:22"]["projection"]["state"] == "inactive"
    assert by_id["tailscale-node:23"]["projection"]["state"] == "warning"
    assert by_id["tailscale-node:23"]["reconcile"]["reason_code"] == "TAILSCALE_NODE_NOT_OBSERVED"
