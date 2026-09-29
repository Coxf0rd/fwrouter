from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from fwrouter_api.db.connection import db_session
from fwrouter_api.services import state_projection
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


def _project_lan_inventory_observation(
    *,
    age_seconds: int,
    source: str = "dnsmasq_leases",
    apply_state: str = "clean",
    desired_mode: str = "global",
) -> dict[str, object]:
    observed_at = datetime.now(UTC).replace(microsecond=0) - timedelta(seconds=age_seconds)
    return state_projection._project_subject(
        {
            "subject_id": "lan:inventory-test",
            "subject_type": "lan",
            "subject_role": "lan_client",
            "implementation_kind": "lan",
            "stable_key": "lan:inventory-test",
            "display_name": "LAN inventory test",
            "desired_mode": desired_mode,
            "applied_mode": "global",
            "apply_state": apply_state,
            "runtime_state": "active",
            "is_active": True,
            "is_deleted": False,
            "last_seen_at": observed_at.isoformat(),
            "updated_at": observed_at.isoformat(),
            "metadata": {"source": source},
            "effective_state": {},
        }
    ).model_dump(mode="json")


def test_lan_inventory_observation_uses_scheduler_interval_plus_grace(monkeypatch) -> None:
    monkeypatch.setattr(state_projection, "get_settings", lambda: SimpleNamespace(subject_inventory_interval_seconds=3600))

    fresh = _project_lan_inventory_observation(age_seconds=3500)
    stale = _project_lan_inventory_observation(age_seconds=4000)
    other_lan_source = _project_lan_inventory_observation(age_seconds=4000, source="direct_probe")

    assert fresh["observation"]["stale"] is False
    assert stale["observation"]["stale"] is True
    assert stale["observation"]["state"] == "unknown"
    assert stale["observation"]["evidence"]["inventory_observation_source"] == "dnsmasq_leases"
    assert stale["reconcile"]["state"] == "in_sync"
    assert stale["projection"]["state"] == "unknown"
    assert other_lan_source["observation"]["stale"] is True
    assert other_lan_source["projection"]["state"] == "warning"


def test_stale_lan_inventory_does_not_mask_confirmed_failure(monkeypatch) -> None:
    monkeypatch.setattr(state_projection, "get_settings", lambda: SimpleNamespace(subject_inventory_interval_seconds=3600))

    failed = _project_lan_inventory_observation(age_seconds=4000, apply_state="failed")

    assert failed["observation"]["stale"] is True
    assert failed["projection"]["state"] == "failed"


def test_stale_lan_inventory_does_not_override_disabled_projection(monkeypatch) -> None:
    monkeypatch.setattr(state_projection, "get_settings", lambda: SimpleNamespace(subject_inventory_interval_seconds=3600))

    disabled = _project_lan_inventory_observation(age_seconds=4000, desired_mode="disabled")

    assert disabled["observation"]["stale"] is True
    assert disabled["projection"]["state"] == "disabled"


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
    monkeypatch.setattr("fwrouter_api.services.state_projection.cached_external_source_observations", lambda provider: _provider_observations(online=["external-source:18"]))

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
    monkeypatch.setattr("fwrouter_api.services.state_projection.cached_external_source_observations", lambda provider: _provider_observations(online=["external-source:18"]))

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
    monkeypatch.setattr("fwrouter_api.services.state_projection.cached_external_source_observations", lambda provider: _provider_observations(online=[], offline=["external-source:30"]))

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
    monkeypatch.setattr("fwrouter_api.services.state_projection.cached_external_source_observations", lambda provider: _provider_observations(online=["external-source:22"]))

    subject = build_subject_state_projection(subject_id="external-source:22")["subject"]

    assert subject["observation"]["state"] == "inactive"
    assert subject["observation"]["evidence"]["external_source_observation"]["presence"] == "online"
    assert subject["reconcile"]["state"] == "not_applicable"
    assert subject["projection"]["state"] == "inactive"


def _project_external_observation(*, presence: str, age_seconds: int = 0, apply_state: str = "clean", scoped_status: str | None = None) -> dict[str, object]:
    observed_at = (datetime.now(UTC) - timedelta(seconds=age_seconds)).replace(microsecond=0).isoformat()
    effective_state: dict[str, object] = {}
    if scoped_status:
        effective_state["scoped_runtime"] = {"status": scoped_status}
    return state_projection._project_subject({
        "subject_id": "external-source:projection-test",
        "subject_type": "external_network_client",
        "subject_role": "external_network_source",
        "implementation_kind": "provider_a",
        "stable_key": "external-source:projection-test",
        "display_name": "Projection test source",
        "desired_mode": "global",
        "applied_mode": "global",
        "apply_state": apply_state,
        "runtime_state": "active",
        "is_active": True,
        "is_deleted": False,
        "last_seen_at": observed_at,
        "updated_at": observed_at,
        "effective_state": effective_state,
        "_external_source_observation": {
            "presence": presence,
            "runtime_state": presence,
            "observed_at": observed_at,
            "provider": "provider_a",
        },
    }).model_dump(mode="json")


def test_expired_and_unknown_external_presence_are_unknown_without_impact() -> None:
    expired_offline = _project_external_observation(presence="offline", age_seconds=90)
    fresh_unknown = _project_external_observation(presence="unknown")

    for subject in (expired_offline, fresh_unknown):
        assert subject["observation"]["state"] == "unknown"
        assert subject["reconcile"]["state"] == "unknown"
        assert subject["reconcile"]["reason_code"] == "EXTERNAL_SOURCE_OBSERVATION_UNCONFIRMED"
        assert subject["projection"]["state"] == "unknown"


def test_external_unknown_does_not_mask_independent_apply_or_scoped_failure() -> None:
    apply_failed = _project_external_observation(presence="unknown", apply_state="failed")
    scoped_failed = _project_external_observation(presence="unknown", scoped_status="failed")
    scoped_drift = _project_external_observation(presence="unknown", scoped_status="drift")

    assert apply_failed["projection"]["state"] == "failed"
    assert apply_failed["reconcile"]["reason_code"] == "SUBJECT_APPLY_FAILED"
    assert scoped_failed["reconcile"]["state"] == "failed"
    assert scoped_drift["reconcile"]["state"] == "runtime_drift"


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
        "fwrouter_api.services.state_projection.cached_external_source_observations",
        lambda provider: _provider_observations(online=["external-source:18", "external-source:22"]),
    )

    projection = build_subject_state_projection(limit=100)
    by_id = {item["entity"]["id"]: item for item in projection["items"]}

    assert by_id["external-source:18"]["projection"]["state"] == "healthy"
    assert by_id["external-source:22"]["projection"]["state"] == "inactive"
    assert by_id["external-source:23"]["projection"]["state"] == "warning"
    assert by_id["external-source:23"]["reconcile"]["reason_code"] == "EXTERNAL_SOURCE_MISSING"
