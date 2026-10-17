from __future__ import annotations

import json

import pytest

from fwrouter_api.adapters.xray_common import xray_writer_guard
from fwrouter_api.db.connection import db_session
from fwrouter_api.services import subscription
from fwrouter_api.adapters.subscription import SubscriptionServer


def _seed_inventory(server_id: str = "synthetic-node") -> None:
    with db_session() as connection:
        connection.execute(
            """INSERT INTO servers (server_id, server_name, provider_name, raw_json, inventory_state)
               VALUES (?, ?, 'subscription', '{}', 'active')""",
            (server_id, server_id),
        )
        connection.execute(
            """INSERT INTO subscription_server_memberships
               (source_id, server_id, source_url, entry_identity_hash, parser_format, is_active)
               VALUES ('src:synthetic', ?, 'http://127.0.0.1:9/synthetic', 'synthetic-hash', 'fixture', 1)""",
            (server_id,),
        )


def test_legacy_nonempty_inventory_is_unknown_and_cannot_reconcile() -> None:
    _seed_inventory()

    state = subscription.get_subscription_native_generation_state()
    gate = subscription._subscription_inventory_generation_gate()

    assert state["status"] == "unknown"
    assert state["candidate_digest"] is None
    assert gate["ok"] is False
    assert gate["reason"] == "verified_generation_missing"


def test_truly_empty_clean_bootstrap_is_admitted() -> None:
    state = subscription.get_subscription_native_generation_state()
    gate = subscription._subscription_inventory_generation_gate()

    assert state["status"] == "empty"
    assert gate["ok"] is True


def test_inventory_upsert_atomically_records_pending_candidate() -> None:
    server = SubscriptionServer(
        server_id="synthetic-vless-a",
        server_name="Synthetic A",
        provider_name="subscription",
        country_code=None,
        region=None,
        raw={"protocol": "vless", "host": "127.0.0.1", "port": 443},
        protocol="vless",
        host="127.0.0.1",
        port=443,
        raw_identity="synthetic-identity-a",
        parser_format="fixture",
        source_format="test",
        runtime_name="synthetic-a",
    )

    result = subscription._upsert_subscription_servers(
        [server],
        servers_by_url={"http://127.0.0.1:9/synthetic": [server]},
    )

    state = subscription.get_subscription_native_generation_state()
    assert result["seen_count"] == 1
    assert state["status"] == "pending"
    assert state["candidate_revision"] == 1
    assert len(state["candidate_digest"]) == 64
    assert state["verified_digest"] is None
    assert subscription._subscription_inventory_generation_gate()["ok"] is False


def test_inventory_upsert_refuses_unresolved_checkpoint_before_database_mutation(monkeypatch) -> None:
    from fwrouter_api.adapters.xray_common import xray_writer_guard_is_held
    from fwrouter_api.services.subscription import SubscriptionInventoryGenerationBlocked

    guard_observations: list[bool] = []
    monkeypatch.setattr(
        "fwrouter_api.services.xray_subscription_service.subscription_inventory_checkpoint_write_gate",
        lambda: guard_observations.append(xray_writer_guard_is_held()) or {
            "ok": False, "error_code": "XRAY_GENERATION_CHECKPOINT_RECOVERY_REQUIRED",
        },
    )
    before = subscription.get_subscription_native_generation_state()
    server = SubscriptionServer(
        server_id="blocked-candidate", server_name="Blocked", provider_name="subscription",
        raw={"protocol": "vless", "host": "127.0.0.1", "port": 443},
        raw_identity="synthetic-blocked-candidate", parser_format="fixture", source_format="test",
    )

    with pytest.raises(SubscriptionInventoryGenerationBlocked) as blocked:
        subscription._upsert_subscription_servers(
            [server], servers_by_url={"http://127.0.0.1:9/synthetic": [server]},
        )

    assert guard_observations == [True]
    assert blocked.value.result["error_code"] == "XRAY_GENERATION_CHECKPOINT_RECOVERY_REQUIRED"
    assert blocked.value.result["last_good_retained"] is True
    assert subscription.get_subscription_native_generation_state() == before
    with db_session() as connection:
        assert connection.execute("SELECT 1 FROM servers WHERE server_id='blocked-candidate'").fetchone() is None


def _write_test_checkpoint(path, *, binding: dict | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schema": 1,
        "phase": "prepared",
        "subscription_inventory_generation": binding,
    }), encoding="utf-8")


def _verified_test_inventory(*, status: str = "verified") -> tuple[str, int, int]:
    _seed_inventory("synthetic-checkpoint-bound")
    with db_session() as connection:
        subscription._write_subscription_inventory_candidate(connection)
        digest, count = subscription._subscription_inventory_digest(connection)
        connection.execute(
            "UPDATE settings SET value_json=? WHERE key=?",
            (json.dumps({
                "schema": 1, "status": status, "candidate_digest": digest,
                "candidate_revision": 1,
                "verified_digest": digest if status == "verified" else None,
                "verified_revision": 1 if status == "verified" else None,
                "reason": None if status == "verified" else "synthetic_pending",
                "inventory_count": count,
            }, sort_keys=True, separators=(",", ":")),
             subscription.SUBSCRIPTION_NATIVE_GENERATION_KEY),
        )
    return digest, count, 1


def test_verified_bound_checkpoint_refuses_discovery_until_recovery(monkeypatch, tmp_path) -> None:
    from fwrouter_api.services import xray_subscription_service

    digest, _count, revision = _verified_test_inventory()
    checkpoint_path = tmp_path / "generation-checkpoint.json"
    _write_test_checkpoint(checkpoint_path, binding={"digest": digest, "revision": revision})
    monkeypatch.setattr(xray_subscription_service, "_xray_adapter", lambda: object())
    monkeypatch.setattr(xray_subscription_service, "_xray_generation_checkpoint_path", lambda _adapter: checkpoint_path)
    server = SubscriptionServer(
        server_id="synthetic-next-candidate", server_name="Synthetic next",
        provider_name="subscription", raw={"protocol": "vless", "host": "127.0.0.1", "port": 443},
        raw_identity="synthetic-next-identity", parser_format="fixture", source_format="test",
    )

    before = subscription.get_subscription_native_generation_state()
    with pytest.raises(subscription.SubscriptionInventoryGenerationBlocked):
        subscription._upsert_subscription_servers(
            [server], servers_by_url={"http://127.0.0.1:9/synthetic": [server]},
        )

    assert subscription.get_subscription_native_generation_state() == before
    with db_session() as connection:
        assert connection.execute(
            "SELECT 1 FROM servers WHERE server_id='synthetic-next-candidate'"
        ).fetchone() is None
    assert digest and revision == 1


def test_clean_empty_bound_checkpoint_refuses_discovery_until_recovery(monkeypatch, tmp_path) -> None:
    from fwrouter_api.services import xray_subscription_service

    checkpoint_path = tmp_path / "generation-checkpoint.json"
    _write_test_checkpoint(checkpoint_path, binding={"digest": None, "revision": None})
    monkeypatch.setattr(xray_subscription_service, "_xray_adapter", lambda: object())
    monkeypatch.setattr(xray_subscription_service, "_xray_generation_checkpoint_path", lambda _adapter: checkpoint_path)
    assert subscription.get_subscription_native_generation_state()["status"] == "empty"
    server = SubscriptionServer(
        server_id="synthetic-from-empty", server_name="Synthetic from empty",
        provider_name="subscription", raw={"protocol": "vless", "host": "127.0.0.1", "port": 443},
        raw_identity="synthetic-from-empty-identity", parser_format="fixture", source_format="test",
    )

    before = subscription.get_subscription_native_generation_state()
    with pytest.raises(subscription.SubscriptionInventoryGenerationBlocked):
        subscription._upsert_subscription_servers(
            [server], servers_by_url={"http://127.0.0.1:9/synthetic": [server]},
        )

    assert subscription.get_subscription_native_generation_state() == before
    assert before["status"] == "empty"
    with db_session() as connection:
        assert connection.execute(
            "SELECT 1 FROM servers WHERE server_id='synthetic-from-empty'"
        ).fetchone() is None


@pytest.mark.parametrize("checkpoint_binding", [
    {"digest": "f" * 64, "revision": 1},
    {"digest": "not-a-digest", "revision": 1},
    None,
])
def test_unverified_or_malformed_checkpoint_cannot_admit_inventory_write(
    monkeypatch, tmp_path, checkpoint_binding,
) -> None:
    from fwrouter_api.services import xray_subscription_service
    from fwrouter_api.services.subscription import SubscriptionInventoryGenerationBlocked

    digest, _count, revision = _verified_test_inventory()
    checkpoint_path = tmp_path / "generation-checkpoint.json"
    _write_test_checkpoint(checkpoint_path, binding=checkpoint_binding)
    monkeypatch.setattr(xray_subscription_service, "_xray_adapter", lambda: object())
    monkeypatch.setattr(xray_subscription_service, "_xray_generation_checkpoint_path", lambda _adapter: checkpoint_path)
    before = subscription.get_subscription_native_generation_state()
    server = SubscriptionServer(
        server_id="synthetic-blocked-by-checkpoint", server_name="Synthetic blocked",
        provider_name="subscription", raw={"protocol": "vless", "host": "127.0.0.1", "port": 443},
        raw_identity="synthetic-blocked-identity", parser_format="fixture", source_format="test",
    )

    with pytest.raises(SubscriptionInventoryGenerationBlocked):
        subscription._upsert_subscription_servers(
            [server], servers_by_url={"http://127.0.0.1:9/synthetic": [server]},
        )

    assert subscription.get_subscription_native_generation_state() == before
    with db_session() as connection:
        assert connection.execute(
            "SELECT 1 FROM servers WHERE server_id='synthetic-blocked-by-checkpoint'"
        ).fetchone() is None
    assert digest and revision == 1


def test_pending_bound_checkpoint_refuses_inventory_write(monkeypatch, tmp_path) -> None:
    from fwrouter_api.services import xray_subscription_service
    from fwrouter_api.services.subscription import SubscriptionInventoryGenerationBlocked

    digest, _count, revision = _verified_test_inventory(status="pending")
    checkpoint_path = tmp_path / "generation-checkpoint.json"
    _write_test_checkpoint(checkpoint_path, binding={"digest": digest, "revision": revision})
    monkeypatch.setattr(xray_subscription_service, "_xray_adapter", lambda: object())
    monkeypatch.setattr(xray_subscription_service, "_xray_generation_checkpoint_path", lambda _adapter: checkpoint_path)
    before = subscription.get_subscription_native_generation_state()
    server = SubscriptionServer(
        server_id="synthetic-pending-checkpoint", server_name="Synthetic pending",
        provider_name="subscription", raw={"protocol": "vless", "host": "127.0.0.1", "port": 443},
        raw_identity="synthetic-pending-identity", parser_format="fixture", source_format="test",
    )

    with pytest.raises(SubscriptionInventoryGenerationBlocked):
        subscription._upsert_subscription_servers(
            [server], servers_by_url={"http://127.0.0.1:9/synthetic": [server]},
        )

    assert subscription.get_subscription_native_generation_state() == before
    with db_session() as connection:
        assert connection.execute(
            "SELECT 1 FROM servers WHERE server_id='synthetic-pending-checkpoint'"
        ).fetchone() is None


def test_unknown_inventory_marker_with_checkpoint_refuses_inventory_write(monkeypatch, tmp_path) -> None:
    from fwrouter_api.services import xray_subscription_service
    from fwrouter_api.services.subscription import SubscriptionInventoryGenerationBlocked

    digest, _count, revision = _verified_test_inventory()
    checkpoint_path = tmp_path / "generation-checkpoint.json"
    _write_test_checkpoint(checkpoint_path, binding={"digest": digest, "revision": revision})
    monkeypatch.setattr(xray_subscription_service, "_xray_adapter", lambda: object())
    monkeypatch.setattr(xray_subscription_service, "_xray_generation_checkpoint_path", lambda _adapter: checkpoint_path)
    with db_session() as connection:
        connection.execute(
            "UPDATE settings SET value_json=? WHERE key=?",
            ("{malformed", subscription.SUBSCRIPTION_NATIVE_GENERATION_KEY),
        )
    before = subscription.get_subscription_native_generation_state()
    assert before["status"] == "unknown"
    server = SubscriptionServer(
        server_id="synthetic-unknown-checkpoint", server_name="Synthetic unknown",
        provider_name="subscription", raw={"protocol": "vless", "host": "127.0.0.1", "port": 443},
        raw_identity="synthetic-unknown-identity", parser_format="fixture", source_format="test",
    )

    with pytest.raises(SubscriptionInventoryGenerationBlocked):
        subscription._upsert_subscription_servers(
            [server], servers_by_url={"http://127.0.0.1:9/synthetic": [server]},
        )

    assert subscription.get_subscription_native_generation_state() == before
    with db_session() as connection:
        assert connection.execute(
            "SELECT 1 FROM servers WHERE server_id='synthetic-unknown-checkpoint'"
        ).fetchone() is None


def test_nonempty_unknown_inventory_cannot_use_empty_checkpoint_binding(monkeypatch, tmp_path) -> None:
    from fwrouter_api.services import xray_subscription_service
    from fwrouter_api.services.subscription import SubscriptionInventoryGenerationBlocked

    _seed_inventory("synthetic-nonempty-unknown")
    checkpoint_path = tmp_path / "generation-checkpoint.json"
    _write_test_checkpoint(checkpoint_path, binding={"digest": None, "revision": None})
    monkeypatch.setattr(xray_subscription_service, "_xray_adapter", lambda: object())
    monkeypatch.setattr(xray_subscription_service, "_xray_generation_checkpoint_path", lambda _adapter: checkpoint_path)
    before = subscription.get_subscription_native_generation_state()
    assert before["status"] == "unknown"
    server = SubscriptionServer(
        server_id="synthetic-unverified-inventory", server_name="Synthetic unverified",
        provider_name="subscription", raw={"protocol": "vless", "host": "127.0.0.1", "port": 443},
        raw_identity="synthetic-unverified-identity", parser_format="fixture", source_format="test",
    )

    with pytest.raises(SubscriptionInventoryGenerationBlocked):
        subscription._upsert_subscription_servers(
            [server], servers_by_url={"http://127.0.0.1:9/synthetic": [server]},
        )

    assert subscription.get_subscription_native_generation_state() == before


def test_malformed_checkpoint_refuses_inventory_write_without_mutation(monkeypatch, tmp_path) -> None:
    from fwrouter_api.services import xray_subscription_service
    from fwrouter_api.services.subscription import SubscriptionInventoryGenerationBlocked

    _verified_test_inventory()
    checkpoint_path = tmp_path / "generation-checkpoint.json"
    checkpoint_path.write_text("{malformed", encoding="utf-8")
    monkeypatch.setattr(xray_subscription_service, "_xray_adapter", lambda: object())
    monkeypatch.setattr(xray_subscription_service, "_xray_generation_checkpoint_path", lambda _adapter: checkpoint_path)
    before = subscription.get_subscription_native_generation_state()
    server = SubscriptionServer(
        server_id="synthetic-malformed-checkpoint", server_name="Synthetic malformed",
        provider_name="subscription", raw={"protocol": "vless", "host": "127.0.0.1", "port": 443},
        raw_identity="synthetic-malformed-identity", parser_format="fixture", source_format="test",
    )

    with pytest.raises(SubscriptionInventoryGenerationBlocked):
        subscription._upsert_subscription_servers(
            [server], servers_by_url={"http://127.0.0.1:9/synthetic": [server]},
        )

    assert subscription.get_subscription_native_generation_state() == before
    with db_session() as connection:
        assert connection.execute(
            "SELECT 1 FROM servers WHERE server_id='synthetic-malformed-checkpoint'"
        ).fetchone() is None


def test_reviewed_migration_forces_pending_without_emitting_conflicting_verified_proof() -> None:
    _seed_inventory("synthetic-reviewed-migration")
    with db_session() as connection:
        subscription._write_subscription_inventory_candidate(connection)
        digest, count = subscription._subscription_inventory_digest(connection)
        connection.execute(
            """UPDATE settings SET value_json=? WHERE key=?""",
            (json.dumps({
                "schema": 1, "status": "verified", "candidate_digest": digest,
                "candidate_revision": 4, "verified_digest": digest,
                "verified_revision": 4, "reason": None, "inventory_count": count,
            }, sort_keys=True, separators=(",", ":")),
             subscription.SUBSCRIPTION_NATIVE_GENERATION_KEY),
        )

    with db_session() as connection:
        subscription._write_subscription_inventory_candidate(connection, force_pending=True)

    state = subscription.get_subscription_native_generation_state()
    assert state["status"] == "pending"
    assert state["candidate_revision"] == 5
    assert state["candidate_digest"] == digest
    # The migration intent requires fresh native proof even when the inventory
    # bytes return to an older digest; that same-digest proof cannot be carried
    # as a distinct last-good generation in this marker schema.
    assert state["verified_digest"] is None
    assert state["verified_revision"] is None


def test_reviewed_migration_preserves_a_distinct_last_good_proof() -> None:
    _seed_inventory("synthetic-reviewed-migration-last-good")
    with db_session() as connection:
        digest, count = subscription._subscription_inventory_digest(connection)
        connection.execute(
            """INSERT INTO settings (key, value_json, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)""",
            (subscription.SUBSCRIPTION_NATIVE_GENERATION_KEY, json.dumps({
                "schema": 1, "status": "pending", "candidate_digest": digest,
                "candidate_revision": 4, "verified_digest": "a" * 64,
                "verified_revision": 3, "reason": "inventory_changed_requires_native_publication",
                "inventory_count": count,
            }, sort_keys=True, separators=(",", ":"))),
        )

    with db_session() as connection:
        subscription._write_subscription_inventory_candidate(connection, force_pending=True)

    state = subscription.get_subscription_native_generation_state()
    assert state["status"] == "pending"
    assert state["candidate_revision"] == 5
    assert state["candidate_digest"] == digest
    assert state["verified_digest"] == "a" * 64
    assert state["verified_revision"] == 3


def test_failed_candidate_marker_write_rolls_back_inventory_transaction(monkeypatch) -> None:
    server = SubscriptionServer(
        server_id="synthetic-rollback",
        server_name="Synthetic rollback",
        provider_name="subscription",
        country_code=None,
        region=None,
        raw={"protocol": "vless", "host": "127.0.0.1", "port": 443},
        protocol="vless",
        host="127.0.0.1",
        port=443,
        raw_identity="synthetic-identity-rollback",
        parser_format="fixture",
        source_format="test",
        runtime_name="synthetic-rollback",
    )

    def fail_marker(_connection) -> None:
        raise RuntimeError("synthetic marker write failure")

    monkeypatch.setattr(subscription, "_write_subscription_inventory_candidate", fail_marker)
    try:
        subscription._upsert_subscription_servers(
            [server],
            servers_by_url={"http://127.0.0.1:9/synthetic": [server]},
        )
    except RuntimeError as exc:
        assert str(exc) == "synthetic marker write failure"
    else:
        raise AssertionError("inventory upsert should fail when its generation marker cannot persist")

    with db_session() as connection:
        assert connection.execute(
            "SELECT 1 FROM servers WHERE server_id='synthetic-rollback'"
        ).fetchone() is None
    assert subscription.get_subscription_native_generation_state()["status"] == "empty"


def test_verified_marker_with_mismatched_digest_is_unknown() -> None:
    _seed_inventory()
    with db_session() as connection:
        connection.execute(
            """INSERT INTO settings (key, value_json, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)""",
            (subscription.SUBSCRIPTION_NATIVE_GENERATION_KEY, json.dumps({
                "schema": 1, "status": "verified", "candidate_digest": "0" * 64,
                "candidate_revision": 1, "verified_digest": "0" * 64,
                "verified_revision": 1,
            })),
        )

    state = subscription.get_subscription_native_generation_state()
    assert state["status"] == "unknown"
    assert state["reason"] == "inventory_digest_mismatch"
    assert subscription._subscription_inventory_generation_gate()["ok"] is False


@pytest.mark.parametrize(("marker", "reason"), [
    ({"schema": True, "status": "pending", "candidate_revision": 1}, "generation_marker_invalid"),
    ({"schema": 1, "status": "pending", "candidate_revision": True}, "generation_revision_invalid"),
])
def test_boolean_schema_or_revision_is_not_accepted_as_integer(marker: dict, reason: str) -> None:
    _seed_inventory()
    with db_session() as connection:
        marker.setdefault("candidate_digest", subscription._subscription_inventory_digest(connection)[0])
        connection.execute(
            "INSERT INTO settings (key, value_json, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
            (subscription.SUBSCRIPTION_NATIVE_GENERATION_KEY, json.dumps(marker)),
        )

    state = subscription.get_subscription_native_generation_state()

    assert state["status"] == "unknown"
    assert state["reason"] == reason


def test_promotion_rejects_admission_with_unissued_capability() -> None:
    from fwrouter_api.adapters.xray_common import xray_writer_guard

    forged = subscription._SubscriptionInventoryAdmission(
        digest="0" * 64, revision=1, issuer=object(),
    )
    with xray_writer_guard():
        result = subscription._promote_subscription_inventory_generation(forged)

    assert result == {"ok": False, "reason": "inventory_generation_admission_missing"}


def test_disabled_xray_keeps_the_subscription_publication_gate_adapter_neutral(monkeypatch) -> None:
    from fwrouter_api.services import subscription_pipeline

    admission = object()
    promoted: list[object] = []
    monkeypatch.setattr(subscription_pipeline, "_claim_subscription_inventory_admission", lambda: admission)
    monkeypatch.setattr(
        subscription_pipeline,
        "_apply_prepared_subscription_refresh_under_xray_guard",
        lambda _prepared, *, inventory_admission: {
            "ok": True,
            "runtime_verified": True,
            "xray_vpn_auto_reconcile": {"ok": True, "status": "skipped", "reason": "xray_module_disabled"},
            "xray_profile_reconcile": {"ok": True, "status": "skipped", "reason": "xray_module_disabled"},
            "public_profile_promote": None,
        },
    )
    monkeypatch.setattr(
        subscription_pipeline,
        "_promote_subscription_inventory_generation",
        lambda capability: promoted.append(capability) or {"ok": True},
    )

    result = subscription_pipeline.apply_prepared_subscription_refresh({"ok": True})

    assert result["ok"] is True
    assert result["inventory_generation_promotion"]["ok"] is True
    assert promoted == [admission]


def test_stale_admission_cannot_apply_after_candidate_revision_changes() -> None:
    server = SubscriptionServer(
        server_id="synthetic-stale",
        server_name="Synthetic stale",
        provider_name="subscription",
        country_code=None,
        region=None,
        raw={"protocol": "vless", "host": "127.0.0.1", "port": 443},
        protocol="vless",
        host="127.0.0.1",
        port=443,
        raw_identity="synthetic-identity-stale",
        parser_format="fixture",
        source_format="test",
        runtime_name="synthetic-stale",
    )
    subscription._upsert_subscription_servers(
        [server], servers_by_url={"http://127.0.0.1:9/synthetic": [server]},
    )
    with xray_writer_guard():
        admission = subscription._claim_subscription_inventory_admission()
    assert admission is not None

    with db_session() as connection:
        marker = json.loads(connection.execute(
            "SELECT value_json FROM settings WHERE key=?",
            (subscription.SUBSCRIPTION_NATIVE_GENERATION_KEY,),
        ).fetchone()["value_json"])
        marker["candidate_revision"] += 1
        connection.execute(
            "UPDATE settings SET value_json=? WHERE key=?",
            (json.dumps(marker), subscription.SUBSCRIPTION_NATIVE_GENERATION_KEY),
        )

    gate = subscription._subscription_inventory_generation_gate(admission)
    assert gate["ok"] is False
    assert gate["reason"] == "inventory_generation_stale"


def test_native_promotion_requires_the_claimed_pending_candidate() -> None:
    from fwrouter_api.services import mihomo_reconcile

    server = SubscriptionServer(
        server_id="synthetic-promotion",
        server_name="Synthetic promotion",
        provider_name="subscription",
        country_code=None,
        region=None,
        raw={"protocol": "vless", "host": "127.0.0.1", "port": 443},
        protocol="vless",
        host="127.0.0.1",
        port=443,
        raw_identity="synthetic-identity-promotion",
        parser_format="fixture",
        source_format="test",
        runtime_name="synthetic-promotion",
    )
    subscription._upsert_subscription_servers(
        [server], servers_by_url={"http://127.0.0.1:9/synthetic": [server]},
    )

    denied = mihomo_reconcile._inventory_candidate_promotion_blocked(None)
    assert denied is not None
    assert denied["error_code"] == "SUBSCRIPTION_INVENTORY_GENERATION_UNVERIFIED"

    with xray_writer_guard():
        admission = subscription._claim_subscription_inventory_admission()
        assert admission is not None
        assert mihomo_reconcile._inventory_candidate_promotion_blocked(admission) is None


def test_inventory_equivalent_refresh_preserves_verified_generation() -> None:
    server = SubscriptionServer(
        server_id="synthetic-stable",
        server_name="Synthetic stable",
        provider_name="subscription",
        country_code=None,
        region=None,
        raw={"protocol": "vless", "host": "127.0.0.1", "port": 443},
        protocol="vless",
        host="127.0.0.1",
        port=443,
        raw_identity="synthetic-identity-stable",
        parser_format="fixture",
        source_format="test",
        runtime_name="synthetic-stable",
    )
    source = "http://127.0.0.1:9/synthetic"
    subscription._upsert_subscription_servers([server], servers_by_url={source: [server]})
    with db_session() as connection:
        marker = json.loads(connection.execute(
            "SELECT value_json FROM settings WHERE key=?",
            (subscription.SUBSCRIPTION_NATIVE_GENERATION_KEY,),
        ).fetchone()["value_json"])
        marker.update({
            "status": "verified",
            "verified_digest": marker["candidate_digest"],
            "verified_revision": marker["candidate_revision"],
            "reason": None,
        })
        connection.execute(
            "UPDATE settings SET value_json=? WHERE key=?",
            (json.dumps(marker), subscription.SUBSCRIPTION_NATIVE_GENERATION_KEY),
        )
    verified_digest = marker["candidate_digest"]

    subscription._upsert_subscription_servers([server], servers_by_url={source: [server]})

    state = subscription.get_subscription_native_generation_state()
    assert state["status"] == "verified"
    assert state["candidate_digest"] == verified_digest
    assert state["verified_digest"] == verified_digest
    assert state["candidate_revision"] == marker["candidate_revision"] + 1
    assert state["verified_revision"] == state["candidate_revision"]


def test_source_removal_invalidates_verified_inventory() -> None:
    server = SubscriptionServer(
        server_id="synthetic-removed",
        server_name="Synthetic removed",
        provider_name="subscription",
        country_code=None,
        region=None,
        raw={"protocol": "vless", "host": "127.0.0.1", "port": 443},
        protocol="vless",
        host="127.0.0.1",
        port=443,
        raw_identity="synthetic-identity-removed",
        parser_format="fixture",
        source_format="test",
        runtime_name="synthetic-removed",
    )
    source = "http://127.0.0.1:9/synthetic"
    subscription._upsert_subscription_servers([server], servers_by_url={source: [server]})
    with db_session() as connection:
        marker = json.loads(connection.execute(
            "SELECT value_json FROM settings WHERE key=?",
            (subscription.SUBSCRIPTION_NATIVE_GENERATION_KEY,),
        ).fetchone()["value_json"])
        marker.update({
            "status": "verified",
            "verified_digest": marker["candidate_digest"],
            "verified_revision": marker["candidate_revision"],
            "reason": None,
        })
        connection.execute(
            "UPDATE settings SET value_json=? WHERE key=?",
            (json.dumps(marker), subscription.SUBSCRIPTION_NATIVE_GENERATION_KEY),
        )

    subscription._upsert_subscription_servers([], servers_by_url={source: []})

    state = subscription.get_subscription_native_generation_state()
    assert state["status"] == "pending"
    assert state["candidate_digest"] != marker["candidate_digest"]
    assert state["verified_digest"] == marker["candidate_digest"]
    assert subscription._subscription_inventory_generation_gate()["ok"] is False


@pytest.mark.parametrize("marker_updates", [
    {"status": "unknown"},
    {"status": "verified", "verified_revision": True},
    {"status": "verified", "verified_revision": 2},
    {"status": "verified", "verified_digest": "not-a-sha256"},
    {"status": "verified", "verified_revision": None},
])
def test_inventory_write_does_not_retrust_malformed_prior_proof(marker_updates: dict) -> None:
    _seed_inventory("synthetic-malformed-marker")
    with db_session() as connection:
        current_digest, _ = subscription._subscription_inventory_digest(connection)
        marker = {
            "schema": 1,
            "status": "verified",
            "candidate_digest": current_digest,
            "candidate_revision": 1,
            "verified_digest": current_digest,
            "verified_revision": 1,
        }
        marker.update(marker_updates)
        connection.execute(
            "INSERT INTO settings (key, value_json, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
            (subscription.SUBSCRIPTION_NATIVE_GENERATION_KEY, json.dumps(marker)),
        )
        subscription._write_subscription_inventory_candidate(connection)

    state = subscription.get_subscription_native_generation_state()
    assert state["status"] == "pending"
    assert state["candidate_revision"] == 2
    assert state["verified_digest"] is None
    assert state["verified_revision"] is None


def test_inventory_write_fails_closed_when_revision_high_water_is_unusable() -> None:
    _seed_inventory("synthetic-invalid-revision-counter")
    with db_session() as connection:
        current_digest, _ = subscription._subscription_inventory_digest(connection)
        marker = {
            "schema": 1,
            "status": "pending",
            "candidate_digest": current_digest,
            "candidate_revision": True,
            "verified_digest": None,
            "verified_revision": None,
        }
        connection.execute(
            "INSERT INTO settings (key, value_json, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
            (subscription.SUBSCRIPTION_NATIVE_GENERATION_KEY, json.dumps(marker)),
        )
        subscription._write_subscription_inventory_candidate(connection)

    state = subscription.get_subscription_native_generation_state()
    assert state["status"] == "unknown"
    assert state["candidate_revision"] is None
    assert state["verified_digest"] is None
    assert subscription._subscription_inventory_generation_gate()["ok"] is False


def test_inventory_write_preserves_valid_historical_generation_a_while_b_is_pending() -> None:
    _seed_inventory("synthetic-historical-generation")
    with db_session() as connection:
        digest_a, _ = subscription._subscription_inventory_digest(connection)
        marker_a = {
            "schema": 1,
            "status": "verified",
            "candidate_digest": digest_a,
            "candidate_revision": 4,
            "verified_digest": digest_a,
            "verified_revision": 4,
            "reason": None,
        }
        connection.execute(
            "INSERT INTO settings (key, value_json, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
            (subscription.SUBSCRIPTION_NATIVE_GENERATION_KEY, json.dumps(marker_a)),
        )
        connection.execute(
            "UPDATE servers SET server_name='synthetic generation B' WHERE server_id=?",
            ("synthetic-historical-generation",),
        )
        subscription._write_subscription_inventory_candidate(connection)

    state = subscription.get_subscription_native_generation_state()
    assert state["status"] == "pending"
    assert state["candidate_digest"] != digest_a
    assert state["candidate_revision"] == 5
    assert state["verified_digest"] == digest_a
    assert state["verified_revision"] == 4


def test_malformed_status_advances_revision_and_fences_previous_admission() -> None:
    _seed_inventory("synthetic-stale-admission-after-corruption")
    with db_session() as connection:
        digest, _ = subscription._subscription_inventory_digest(connection)
        connection.execute(
            "INSERT INTO settings (key, value_json, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
            (subscription.SUBSCRIPTION_NATIVE_GENERATION_KEY, json.dumps({
                "schema": 1,
                "status": "pending",
                "candidate_digest": digest,
                "candidate_revision": 7,
                "verified_digest": None,
                "verified_revision": None,
            })),
        )
    with xray_writer_guard():
        stale_admission = subscription._claim_subscription_inventory_admission()
    assert stale_admission is not None

    with db_session() as connection:
        marker = json.loads(connection.execute(
            "SELECT value_json FROM settings WHERE key=?",
            (subscription.SUBSCRIPTION_NATIVE_GENERATION_KEY,),
        ).fetchone()["value_json"])
        marker["status"] = "unknown"
        connection.execute(
            "UPDATE settings SET value_json=? WHERE key=?",
            (json.dumps(marker), subscription.SUBSCRIPTION_NATIVE_GENERATION_KEY),
        )
        subscription._write_subscription_inventory_candidate(connection)

    state = subscription.get_subscription_native_generation_state()
    assert state["status"] == "pending"
    assert state["candidate_digest"] == stale_admission.digest
    assert state["candidate_revision"] == stale_admission.revision + 1
    assert subscription._subscription_inventory_generation_gate(stale_admission)["ok"] is False
