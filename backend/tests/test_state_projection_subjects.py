from __future__ import annotations

from datetime import UTC, datetime

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


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _generic_provider_contract(provider: str) -> dict[str, object] | None:
    if provider != "provider_a":
        return None
    return {
        "provider": "provider_a",
        "module_concept": "provider_a",
        "subject_type": "external_network_client",
        "implementation_kind": "provider_a",
    }


def _seed_external_source_subject(subject_id: str, *, active: bool, runtime_state: str = "active") -> None:
    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO subjects (
                subject_id, subject_type, subject_role, implementation_kind, stable_key,
                display_name, desired_mode, applied_mode, apply_state, runtime_state,
                is_active, last_seen_at, updated_at
            )
            VALUES (?, 'external_network_client', 'external_network_source', 'provider_a',
                    ?, ?, 'global', NULL, 'clean', ?, ?, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
            """,
            (subject_id, subject_id, subject_id, runtime_state, 1 if active else 0),
        )


def _provider_observations(*, online: list[str], offline: list[str] | None = None) -> dict[str, object]:
    observed_at = _now()
    items = [
        {
            "provider": "provider_a",
            "provider_connection_id": None,
            "external_id": subject_id,
            "subject_id": subject_id,
            "presence": "online",
            "runtime_state": "online",
            "observed_at": observed_at,
            "is_local_identity": False,
            "display_name": subject_id,
            "ip_address": None,
            "metadata": {},
        }
        for subject_id in online
    ] + [
        {
            "provider": "provider_a",
            "provider_connection_id": None,
            "external_id": subject_id,
            "subject_id": subject_id,
            "presence": "offline",
            "runtime_state": "offline",
            "observed_at": observed_at,
            "is_local_identity": False,
            "display_name": subject_id,
            "ip_address": None,
            "metadata": {},
        }
        for subject_id in (offline or [])
    ]
    return {
        "ok": True,
        "provider": "provider_a",
        "observed_at": observed_at,
        "items": items,
        "local_identities": [],
        "by_subject_id": {str(item["subject_id"]): item for item in items},
    }


def test_external_source_active_persistent_online_is_healthy(monkeypatch) -> None:
    _seed_external_source_subject("external-source:18", active=True)
    monkeypatch.setattr(
        "fwrouter_api.services.state_projection.build_runtime_enforcement_state",
        lambda **_: {"supported_modes": {"direct": True, "selective": True, "vpn": True}},
    )
    monkeypatch.setattr(
        "fwrouter_api.services.state_projection.external_ingress_contract",
        _generic_provider_contract,
    )
    monkeypatch.setattr("fwrouter_api.services.state_projection.read_external_source_observations", lambda provider: _provider_observations(online=["external-source:18"]))

    subject = build_subject_state_projection(subject_id="external-source:18")["subject"]

    assert subject["observation"]["source"] == "external_source_observation+database"
    assert subject["observation"]["state"] == "active"
    assert subject["observation"]["stale"] is False
    assert subject["reconcile"]["state"] == "in_sync"
    assert subject["projection"]["state"] == "healthy"


def test_external_source_active_persistent_missing_is_warning(monkeypatch) -> None:
    _seed_external_source_subject("external-source:23", active=True)
    monkeypatch.setattr(
        "fwrouter_api.services.state_projection.build_runtime_enforcement_state",
        lambda **_: {"supported_modes": {"direct": True, "selective": True, "vpn": True}},
    )
    monkeypatch.setattr(
        "fwrouter_api.services.state_projection.external_ingress_contract",
        _generic_provider_contract,
    )
    monkeypatch.setattr("fwrouter_api.services.state_projection.read_external_source_observations", lambda provider: _provider_observations(online=["external-source:18"]))

    subject = build_subject_state_projection(subject_id="external-source:23")["subject"]

    assert subject["observation"]["state"] == "missing"
    assert subject["reconcile"]["state"] == "observation_stale"
    assert subject["reconcile"]["reason_code"] == "EXTERNAL_SOURCE_MISSING"
    assert subject["projection"]["state"] == "warning"


def test_external_source_active_persistent_offline_is_warning(monkeypatch) -> None:
    _seed_external_source_subject("external-source:30", active=True)
    monkeypatch.setattr(
        "fwrouter_api.services.state_projection.build_runtime_enforcement_state",
        lambda **_: {"supported_modes": {"direct": True, "selective": True, "vpn": True}},
    )
    monkeypatch.setattr(
        "fwrouter_api.services.state_projection.external_ingress_contract",
        _generic_provider_contract,
    )
    monkeypatch.setattr("fwrouter_api.services.state_projection.read_external_source_observations", lambda provider: _provider_observations(online=[], offline=["external-source:30"]))

    subject = build_subject_state_projection(subject_id="external-source:30")["subject"]

    assert subject["observation"]["state"] == "offline"
    assert subject["reconcile"]["state"] == "observation_stale"
    assert subject["reconcile"]["reason_code"] == "EXTERNAL_SOURCE_OFFLINE"
    assert subject["projection"]["state"] == "warning"


def test_external_source_inactive_persistent_online_has_no_error_health(monkeypatch) -> None:
    _seed_external_source_subject("external-source:22", active=False, runtime_state="inactive")
    monkeypatch.setattr(
        "fwrouter_api.services.state_projection.build_runtime_enforcement_state",
        lambda **_: {"supported_modes": {"direct": True, "selective": True, "vpn": True}},
    )
    monkeypatch.setattr(
        "fwrouter_api.services.state_projection.external_ingress_contract",
        _generic_provider_contract,
    )
    monkeypatch.setattr("fwrouter_api.services.state_projection.read_external_source_observations", lambda provider: _provider_observations(online=["external-source:22"]))

    subject = build_subject_state_projection(subject_id="external-source:22")["subject"]

    assert subject["observation"]["state"] == "inactive"
    assert subject["observation"]["evidence"]["external_source_observation"]["presence"] == "online"
    assert subject["reconcile"]["state"] == "not_applicable"
    assert subject["projection"]["state"] == "inactive"


def test_external_source_projection_splits_intent_and_live_observation(monkeypatch) -> None:
    _seed_external_source_subject("external-source:18", active=True)
    _seed_external_source_subject("external-source:22", active=False, runtime_state="inactive")
    _seed_external_source_subject("external-source:23", active=True)
    monkeypatch.setattr(
        "fwrouter_api.services.state_projection.build_runtime_enforcement_state",
        lambda **_: {"supported_modes": {"direct": True, "selective": True, "vpn": True}},
    )
    monkeypatch.setattr(
        "fwrouter_api.services.state_projection.external_ingress_contract",
        _generic_provider_contract,
    )
    monkeypatch.setattr(
        "fwrouter_api.services.state_projection.read_external_source_observations",
        lambda provider: _provider_observations(online=["external-source:18", "external-source:22"]),
    )

    projection = build_subject_state_projection(limit=100)
    by_id = {item["entity"]["id"]: item for item in projection["items"]}

    assert by_id["external-source:18"]["projection"]["state"] == "healthy"
    assert by_id["external-source:22"]["projection"]["state"] == "inactive"
    assert by_id["external-source:23"]["projection"]["state"] == "warning"
    assert by_id["external-source:23"]["reconcile"]["reason_code"] == "EXTERNAL_SOURCE_MISSING"
