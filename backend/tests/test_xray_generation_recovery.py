from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from fwrouter_api.adapters.xray_common import XrayApplyResult
from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import initialize_database
from fwrouter_api.services import mihomo_config, xray_materialize, xray_bindings
from fwrouter_api.adapters import mihomo
from fwrouter_api.services import xray_subscription_service as service


IDENTITY = ("11111111-1111-4111-8111-111111111111", "committed@example.test")


class _Adapter:
    def __init__(self, root: Path) -> None:
        self.config_path = root / "xray" / "config.json"
        self.config_path.parent.mkdir(parents=True)
        self.config_path.write_text(json.dumps({
            "api": {"tag": "fwrouter-api", "services": ["StatsService"]},
            "inbounds": [
                {"tag": "vless-ws", "protocol": "vless", "settings": {"clients": [{"id": IDENTITY[0], "email": IDENTITY[1]}]}},
                self._managed_api_inbound(),
            ],
            "outbounds": [], "routing": {"rules": []},
        }), encoding="utf-8")
        self._candidate = self.config_path.with_suffix(".candidate")
        self.incarnation = "old-running"
        self.reload_count = 0
        self.reload_ok = True
        self.loaded = [IDENTITY]

    @staticmethod
    def _managed_api_inbound():
        return {"tag": "fwrouter-api", "listen": "127.0.0.1", "port": 10085,
                "protocol": "dokodemo-door", "settings": {"address": "127.0.0.1"}}

    def _load_clients_and_config(self):
        payload = json.loads(self.config_path.read_text())
        return payload, next(item for item in payload["inbounds"] if item["tag"] == "vless-ws"), []

    def _materialize_client_binding_metadata(self, *, raw_clients, bindings):
        return raw_clients, len(bindings)

    def _ensure_runtime_stats(self, payload):
        payload["api"] = {"tag": "fwrouter-api", "services": ["StatsService", "HandlerService"]}

    def _materialize_managed_egress(self, *, payload, bindings, client_modes):
        payload["routing"]["rules"] = [{"type": "field", "inboundTag": ["vless-ws"], "user": [IDENTITY[1]], "outboundTag": "handoff"}]
        return 1, {"egress_count": 1}

    def _candidate_path(self):
        return self._candidate

    def test_config(self, _path):
        return XrayApplyResult(ok=True, message="native test ok")

    def get_runtime_incarnation(self):
        return self.incarnation

    def get_runtime_config_sha256(self):
        return hashlib.sha256(self.config_path.read_bytes()).hexdigest()

    def reload(self):
        if self.reload_ok:
            self.reload_count += 1
            self.incarnation = f"running-{self.reload_count}"
        return XrayApplyResult(ok=self.reload_ok, message="reload")

    def list_loaded_client_identities(self):
        return self.loaded


def _setup(monkeypatch, tmp_path: Path, *, fail: str | None = None):
    monkeypatch.setenv("FWROUTER_STATE_DIR", str(tmp_path / "state"))
    get_settings.cache_clear()
    initialize_database()
    adapter = _Adapter(tmp_path)
    mihomo_path = tmp_path / "mihomo.yaml"
    mihomo_path.write_text("mihomo-current", encoding="utf-8")
    monkeypatch.setattr(mihomo_config, "_resolved_base_config_path", lambda: mihomo_path)
    monkeypatch.setattr(service, "get_mihomo_runtime_incarnation", lambda: "mihomo-running", raising=False)
    import fwrouter_api.services.mihomo_runtime as mihomo_runtime
    monkeypatch.setattr(mihomo_runtime, "get_mihomo_runtime_incarnation", lambda: "mihomo-running")
    monkeypatch.setattr(service, "_current_xray_projection_snapshot", lambda: {
        "identities": [IDENTITY], "snapshots": [], "published_snapshots": [],
        "fence": {"revision": 4, "decision_id": "current", "active_server_id": "server"},
    })
    fingerprint_calls = {"count": 0}
    def projection_fingerprint(_snapshot):
        fingerprint_calls["count"] += 1
        if fail == "terminal_drift" and fingerprint_calls["count"] >= 4:
            return "changed"
        if fail == "preapply_drift" and fingerprint_calls["count"] >= 2:
            return "changed"
        return "projection-stable"
    monkeypatch.setattr(service, "_current_projection_fingerprint", projection_fingerprint)
    monkeypatch.setattr(service, "_current_bindings_artifact_parity", lambda *_a: (True, "bindings-stable"))
    monkeypatch.setattr(xray_bindings, "get_xray_handoff_listeners", lambda _bindings=None: [{"port": 53123}])
    monkeypatch.setattr(mihomo.DEFAULT_MIHOMO_ADAPTER, "check_port", lambda *_a, **_k: fail != "handoff_unready")
    monkeypatch.setattr(service, "_generation_source_fingerprint", lambda: "source-stable")
    monkeypatch.setattr(
        service, "_verify_current_public_projection",
        lambda _snapshot, *, supersession=None: fail != "public_mismatch",
    )
    monkeypatch.setattr(service, "collect_xray_runtime_bindings", lambda **_kwargs: [{
        "client_uuid": IDENTITY[0], "client_email": IDENTITY[1], "selected_server_id": "server",
    }])
    monkeypatch.setattr(service, "collect_xray_client_mode_directives", lambda **_kwargs: [])
    monkeypatch.setattr(service, "_capture_generation_auto_selection", lambda: {
        "routing": {"server_mode": "auto", "active_auto_server_id": "server"},
    })
    monkeypatch.setattr(service, "_verify_generation_selection_readback", lambda _selection: {"ok": True, "mode": "auto", "logical_server_id": "server"})
    import fwrouter_api.services.selector as selector
    monkeypatch.setattr(selector, "get_vpn_auto_state", lambda **_kwargs: {
        "active_auto_target_valid": True, "config_consistent": True, "active_auto_server_id": "server",
    })
    monkeypatch.setattr(xray_materialize, "_verify_active_config_bindings", lambda *_a, **_k: {
        "ok": fail != "binding_readback", "verified_bindings_count": 1,
    })
    monkeypatch.setattr(xray_materialize, "_verify_active_config_client_modes", lambda *_a, **_k: {
        "ok": True, "verified_client_modes_count": 0,
    })
    if fail == "unsafe_api":
        payload = json.loads(adapter.config_path.read_text())
        payload["inbounds"][1]["listen"] = "0.0.0.0"
        adapter.config_path.write_text(json.dumps(payload))
    if fail == "reload":
        adapter.reload_ok = False
    if fail == "loaded_mismatch":
        adapter.loaded = []
    if fail == "incarnation_unchanged":
        adapter.reload = lambda: XrayApplyResult(ok=True, message="reload")
    if fail == "native":
        adapter.test_config = lambda _path: XrayApplyResult(ok=False, message="native failure")
    if fail == "selection_readback":
        monkeypatch.setattr(service, "_verify_generation_selection_readback", lambda _selection: {"ok": False})
    checkpoint = adapter.config_path.parent / ".generation" / "generation-checkpoint.json"
    checkpoint.parent.mkdir(mode=0o700)
    checkpoint.write_text(json.dumps({"generation_id": "old-gen", "phase": "restore_failed"}), encoding="utf-8")
    checkpoint.chmod(0o600)
    return adapter, checkpoint


def _prepare_verified_committed_checkpoint(
    monkeypatch, tmp_path: Path, *, fail: str | None = None,
    phase: str = "snapshots_published",
):
    """Model crash after durable inventory promotion, before checkpoint unlink."""
    adapter, checkpoint = _setup(monkeypatch, tmp_path, fail=fail)
    from fwrouter_api.services import subscription
    from fwrouter_api.db.connection import db_session

    with db_session() as connection:
        connection.execute(
            "INSERT INTO servers (server_id, server_name, provider_name, raw_json, inventory_state) "
            "VALUES ('synthetic-committed', 'Synthetic committed', 'subscription', '{}', 'active')"
        )
        connection.execute(
            "INSERT INTO subscription_server_memberships "
            "(source_id, server_id, source_url, entry_identity_hash, parser_format, is_active) "
            "VALUES ('src:committed', 'synthetic-committed', 'http://127.0.0.1:9/committed', 'synthetic', 'fixture', 1)"
        )
        subscription._write_subscription_inventory_candidate(connection)
    inventory = subscription.get_subscription_native_generation_state()
    assert inventory["status"] == "pending"
    # Recreate the exact settings row committed by successful promotion. The
    # checkpoint remains on disk, representing a process crash before unlink.
    with db_session() as connection:
        connection.execute(
            "UPDATE settings SET value_json=? WHERE key=?",
            (json.dumps({
                "schema": 1,
                "status": "verified",
                "candidate_digest": inventory["candidate_digest"],
                "candidate_revision": inventory["candidate_revision"],
                "verified_digest": inventory["candidate_digest"],
                "verified_revision": inventory["candidate_revision"],
                "reason": None,
                "inventory_count": inventory["inventory_count"],
            }, sort_keys=True, separators=(",", ":")), subscription.SUBSCRIPTION_NATIVE_GENERATION_KEY),
        )
    inventory = subscription.get_subscription_native_generation_state()
    assert inventory["status"] == "verified"

    checkpoint_data = json.loads(checkpoint.read_text(encoding="utf-8"))
    checkpoint_data["phase"] = phase
    checkpoint_data["subscription_inventory_generation"] = {
        "digest": inventory["candidate_digest"],
        "revision": inventory["candidate_revision"],
    }
    mihomo_path = Path(mihomo_config._resolved_base_config_path())
    checkpoint_data["staged_generation"] = {
        "mihomo_final_sha256": hashlib.sha256(mihomo_path.read_bytes()).hexdigest(),
    }
    checkpoint.write_text(json.dumps(checkpoint_data, sort_keys=True), encoding="utf-8")
    checkpoint.chmod(0o600)
    return adapter, checkpoint, inventory


def _stub_verified_mihomo_recovery_receipt(monkeypatch, *, proof_ok: bool):
    from fwrouter_api.services import mihomo_reconcile, mihomo_reconcile_fingerprint

    proof = {"ok": proof_ok, "evidence": "synthetic-native-readback"}
    proof_calls = []
    receipt_calls = []
    monkeypatch.setattr(mihomo_reconcile, "_native_udp_policy_proof", lambda: proof_calls.append(proof) or proof)
    monkeypatch.setattr(mihomo_reconcile_fingerprint, "current_mihomo_input_fingerprint", lambda: "synthetic-fingerprint")

    def write_receipt(**kwargs):
        receipt_calls.append(kwargs)
        return True

    monkeypatch.setattr(mihomo_reconcile_fingerprint, "write_mihomo_reconcile_fingerprint_state", write_receipt)
    return proof_calls, receipt_calls


def _write_supersession_candidate_proof(adapter: _Adapter, checkpoint: Path) -> dict[str, Any]:
    """Build internally consistent persisted proof fields for the ownership predicate unit test."""
    mihomo_path = Path(mihomo_config._resolved_base_config_path())
    xray_sha = hashlib.sha256(adapter.config_path.read_bytes()).hexdigest()
    mihomo_sha = hashlib.sha256(mihomo_path.read_bytes()).hexdigest()
    transition_sha = hashlib.sha256(b"isolated transition candidate").hexdigest()
    transition_receipt = hashlib.sha256(b"isolated transition receipt").hexdigest()
    final_receipt = hashlib.sha256(b"isolated final receipt").hexdigest()
    checkpoint_data = {
        "generation_id": "current-candidate-generation",
        "phase": "runtime_applied",
        "source_fingerprint": "source-stable",
        "derived_source_fingerprint": "source-stable",
        "selection_revision": 4,
        "xray_runtime_incarnation_after": adapter.get_runtime_incarnation(),
        "mihomo_runtime_incarnation": "mihomo-running",
        "auto_selection_after": {"selection_revision": 4, "selection_decision_id": "current"},
        "supersession": {"rollback_preimage_projection_matches": False},
        "staged_generation": {
            "xray_candidate_sha256": xray_sha,
            "mihomo_transition_sha256": transition_sha,
            "mihomo_final_sha256": mihomo_sha,
        },
        "native_validation_proof": {
            "xray": {"ok": True, "candidate_sha256": xray_sha},
            "mihomo": {
                "transition": {
                    "ok": True, "candidate_sha256": transition_sha,
                    "receipt_sha256": transition_receipt,
                },
                "final": {
                    "ok": True, "candidate_sha256": mihomo_sha,
                    "receipt_sha256": final_receipt,
                },
            },
        },
    }
    checkpoint.write_text(json.dumps(checkpoint_data, sort_keys=True), encoding="utf-8")
    checkpoint.chmod(0o600)
    return checkpoint_data


def test_supersession_recovery_allows_owned_current_candidate_past_preimage_gate(
    monkeypatch, tmp_path: Path,
):
    """A mismatched old preimage is admissible only when the saved current proof owns runtime."""
    adapter, checkpoint = _setup(monkeypatch, tmp_path, fail="public_mismatch")
    checkpoint_data = _write_supersession_candidate_proof(adapter, checkpoint)
    projection = service._current_xray_projection_snapshot()

    # Exercise the real ownership predicate over persisted hashes, native
    # incarnation/readback, source fingerprint, and Core revision/decision.
    assert service._supersession_current_candidate_owned(adapter, checkpoint_data, projection) is True

    # The recovery flow must get past the formerly unconditional preimage
    # refusal. It then correctly stops at the next, independently failed
    # committed-public-projection proof.
    result = service._recover_checkpoint_from_current_projection(adapter, checkpoint)
    assert result == {
        "ok": False,
        "recovered": "current_projection_unproven",
        "reason": "committed_projection_mismatch",
    }


def test_supersession_recovery_refuses_unowned_candidate_when_preimage_differs(
    monkeypatch, tmp_path: Path,
):
    adapter, checkpoint = _setup(monkeypatch, tmp_path)
    checkpoint_data = {
        "generation_id": "unowned-candidate-generation",
        "phase": "restore_failed",
        "supersession": {"rollback_preimage_projection_matches": False},
        "staged_generation": {
            "xray_candidate_sha256": "0" * 64,
            "mihomo_final_sha256": "0" * 64,
        },
    }
    checkpoint.write_text(json.dumps(checkpoint_data, sort_keys=True), encoding="utf-8")
    checkpoint.chmod(0o600)
    checkpoint_before = checkpoint.read_bytes()
    config_before = adapter.config_path.read_bytes()
    loaded_before = list(adapter.list_loaded_client_identities())
    checkpoint_digest = hashlib.sha256(checkpoint_before).hexdigest()

    assert service._supersession_current_candidate_owned(
        adapter, checkpoint_data, service._current_xray_projection_snapshot(),
    ) is False
    result = service._recover_checkpoint_from_current_projection(adapter, checkpoint)

    assert result == {
        "ok": False,
        "recovered": "current_projection_unproven",
        "reason": "supersession_current_candidate_unowned",
    }
    assert checkpoint.read_bytes() == checkpoint_before
    assert adapter.config_path.read_bytes() == config_before
    assert adapter.reload_count == 0
    assert adapter.list_loaded_client_identities() == loaded_before
    assert not service._generation_recovery_attempt_path(checkpoint, checkpoint_digest).exists()


def test_current_projection_recovery_reloads_and_closes_checkpoint(monkeypatch, tmp_path: Path):
    adapter, checkpoint = _setup(monkeypatch, tmp_path)
    old_digest = hashlib.sha256(checkpoint.read_bytes()).hexdigest()

    result = service._recover_checkpoint_from_current_projection(adapter, checkpoint)

    assert result["ok"] is True, result
    assert not checkpoint.exists()
    attempt = json.loads(service._generation_recovery_attempt_path(checkpoint, old_digest).read_text())
    assert attempt["phase"] == "superseded_verified"
    assert attempt["checkpoint_sha256"] == old_digest
    assert attempt["expected_identity_count"] == 1
    assert attempt["operation_id"]
    assert set(attempt).isdisjoint({"uuid", "email", "stdout", "stderr", "config"})


def test_verified_promotion_crash_uses_current_projection_proof_and_retains_on_failure(
    monkeypatch, tmp_path: Path,
):
    adapter, checkpoint, inventory = _prepare_verified_committed_checkpoint(
        monkeypatch, tmp_path, fail="public_mismatch",
    )
    checkpoint_before = checkpoint.read_bytes()
    config_before = adapter.config_path.read_bytes()
    loaded_before = list(adapter.list_loaded_client_identities())
    calls_before = list(adapter._candidate.read_bytes() for _ in [0] if adapter._candidate.exists())
    reloads_before = adapter.reload_count
    restore_calls = []

    def forbidden_preimage_restore(_adapter, _checkpoint_path):
        restore_calls.append(True)
        return {"ok": True, "recovered": "restored_preimage"}

    monkeypatch.setattr(service, "_restore_xray_generation_checkpoint", forbidden_preimage_restore)
    result = service._resolve_existing_xray_generation_checkpoint(adapter, checkpoint)

    assert result["ok"] is False
    assert result["error_code"] == "XRAY_GENERATION_RECOVERY_FAILED"
    assert result["details"].get("reason") == "committed_projection_mismatch"
    assert restore_calls == []
    assert checkpoint.read_bytes() == checkpoint_before
    assert adapter.config_path.read_bytes() == config_before
    assert adapter.list_loaded_client_identities() == loaded_before
    assert adapter.reload_count == reloads_before
    assert list(adapter._candidate.read_bytes() for _ in [0] if adapter._candidate.exists()) == calls_before
    from fwrouter_api.services.subscription import get_subscription_native_generation_state
    assert get_subscription_native_generation_state() == inventory


def test_verified_promotion_crash_requires_fresh_native_udp_proof_before_closing(
    monkeypatch, tmp_path: Path,
):
    adapter, checkpoint, inventory = _prepare_verified_committed_checkpoint(monkeypatch, tmp_path)
    proof_calls, receipt_calls = _stub_verified_mihomo_recovery_receipt(monkeypatch, proof_ok=False)
    restore_calls = []
    monkeypatch.setattr(
        service, "_restore_xray_generation_checkpoint",
        lambda *_args: restore_calls.append(True) or {"ok": True, "recovered": "restored_preimage"},
    )
    checkpoint_before = checkpoint.read_bytes()
    reloads_before = adapter.reload_count

    result = service._resolve_existing_xray_generation_checkpoint(adapter, checkpoint)

    assert result["ok"] is False
    assert (result.get("details") or {}).get("reason") == "inventory_native_receipt_unconfirmed"
    assert len(proof_calls) == 1
    assert receipt_calls == []
    assert restore_calls == []
    assert checkpoint.read_bytes() == checkpoint_before
    assert adapter.reload_count == reloads_before + 1
    from fwrouter_api.services.subscription import get_subscription_native_generation_state
    assert get_subscription_native_generation_state() == inventory


@pytest.mark.parametrize("phase", ["snapshots_published", "projections_cleaned"])
def test_verified_promotion_crash_closes_only_after_current_projection_proof(
    monkeypatch, tmp_path: Path, phase: str,
):
    adapter, checkpoint, inventory = _prepare_verified_committed_checkpoint(
        monkeypatch, tmp_path, phase=phase,
    )
    proof_calls, receipt_calls = _stub_verified_mihomo_recovery_receipt(monkeypatch, proof_ok=True)
    old_digest = hashlib.sha256(checkpoint.read_bytes()).hexdigest()
    reloads_before = adapter.reload_count

    result = service._resolve_existing_xray_generation_checkpoint(adapter, checkpoint)

    assert result["ok"] is True, result
    assert result.get("recovered") == "current_projection_verified"
    assert not checkpoint.exists()
    assert adapter.reload_count == reloads_before + 1
    assert set(adapter.list_loaded_client_identities()) == {IDENTITY}
    from fwrouter_api.services.subscription import get_subscription_native_generation_state
    assert get_subscription_native_generation_state() == inventory
    assert len(proof_calls) == 1
    assert len(receipt_calls) == 1
    assert receipt_calls[0].get("native_proof") == proof_calls[0]
    assert receipt_calls[0].get("result") == {
        "ok": True, "reconcile_reason": "xray_checkpoint_recovery",
    }
    attempt_path = service._generation_recovery_attempt_path(checkpoint, old_digest)
    attempt = json.loads(attempt_path.read_text(encoding="utf-8"))
    assert attempt.get("phase") == "superseded_verified"
    assert attempt.get("checkpoint_sha256") == old_digest


def test_verified_promotion_crash_still_requires_explicit_migration_supersession(
    monkeypatch, tmp_path: Path,
):
    adapter, checkpoint, inventory = _prepare_verified_committed_checkpoint(monkeypatch, tmp_path)
    from fwrouter_api.db.connection import db_session

    checkpoint_digest = hashlib.sha256(checkpoint.read_bytes()).hexdigest()
    with db_session() as connection:
        connection.execute(
            "INSERT INTO subscription_node_identities "
            "(identity_id, origin_server_id, current_server_id, revision, provenance_sha256) "
            "VALUES ('synthetic-migration', 'origin', 'current', 1, ?)",
            ("a" * 64,),
        )
        connection.execute(
            "INSERT INTO subscription_node_identity_transitions "
            "(identity_id, from_server_id, to_server_id, from_revision, to_revision, "
            "before_provenance_sha256, after_provenance_sha256, source_ownership_sha256, checkpoint_sha256) "
            "VALUES ('synthetic-migration', 'origin', 'current', 1, 2, ?, ?, ?, ?)",
            ("a" * 64, "b" * 64, "c" * 64, checkpoint_digest),
        )
    calls = []
    monkeypatch.setattr(service, "_recover_checkpoint_from_current_projection", lambda *_a: calls.append("current"))
    monkeypatch.setattr(service, "_restore_xray_generation_checkpoint", lambda *_a: calls.append("restore"))
    checkpoint_before = checkpoint.read_bytes()
    config_before = adapter.config_path.read_bytes()
    reloads_before = adapter.reload_count

    result = service._resolve_existing_xray_generation_checkpoint(adapter, checkpoint)

    assert result["ok"] is False
    assert result["stage"] == "generation_supersession_required"
    assert result["error_code"] == "XRAY_GENERATION_SUPERSESSION_REQUIRED"
    assert calls == []
    assert checkpoint.read_bytes() == checkpoint_before
    assert adapter.config_path.read_bytes() == config_before
    assert adapter.reload_count == reloads_before
    from fwrouter_api.services.subscription import get_subscription_native_generation_state
    assert get_subscription_native_generation_state() == inventory


def _handoff_parity_state(handoffs):
    bindings = [
        {"subject_id": "subject-a", "client_id": "client-a", "client_uuid": "uuid-a",
         "client_email": "a@example.test", "selected_server_id": "server-a",
         "selected_server_source": "fixed", "handoff_proxy_name": "proxy-a",
         "server_name": "A", "server_runtime_name": "proxy-a", "match_key": "a", "status": "applied"},
        {"subject_id": "subject-b", "client_id": "client-b", "client_uuid": "uuid-b",
         "client_email": "b@example.test", "selected_server_id": "server-b",
         "selected_server_source": "fixed", "handoff_proxy_name": "proxy-b",
         "server_name": "B", "server_runtime_name": "proxy-b", "match_key": "b", "status": "applied"},
    ]
    return bindings, {
        "bindings_version": 1,
        "bindings_count": 2,
        "applied_count": 2,
        "bindings": bindings,
        "handoff_count": len(handoffs),
        "handoff_listeners": handoffs,
        "client_modes_count": 0,
        "client_modes": [],
    }


def _handoff_rows():
    return [
        {"selected_server_id": "server-a", "digest": "digest-a", "tag": "tag-a",
         "listener_name": "listener-a", "listen": "172.18.0.1", "proxy": "proxy-a",
         "subject_ids": ["subject-a", "subject-a2"],
         "client_emails": ["a@example.test", "a2@example.test"], "port": 53101,
         "bindings_count": 2},
        {"selected_server_id": "server-b", "digest": "digest-b", "tag": "tag-b",
         "listener_name": "listener-b", "listen": "172.18.0.1", "proxy": "proxy-b",
         "subject_ids": ["subject-b"], "client_emails": ["b@example.test"], "port": 53102,
         "bindings_count": 1},
    ]


def test_bindings_artifact_parity_ignores_only_handoff_association_order(monkeypatch, tmp_path: Path):
    path = tmp_path / "bindings.json"
    monkeypatch.setattr("fwrouter_api.services.xray_runtime_state._xray_bindings_path", lambda: path)
    expected_handoffs = _handoff_rows()
    bindings, state = _handoff_parity_state(expected_handoffs)
    state["handoff_listeners"] = [
        {**expected_handoffs[1]},
        {**expected_handoffs[0],
         "subject_ids": list(reversed(expected_handoffs[0]["subject_ids"])),
         "client_emails": list(reversed(expected_handoffs[0]["client_emails"]))},
    ]
    path.write_text(json.dumps(state), encoding="utf-8")
    monkeypatch.setattr("fwrouter_api.services.xray_bindings._bindings_for_state", lambda values: values)
    monkeypatch.setattr("fwrouter_api.services.xray_bindings.get_xray_handoff_listeners", lambda _values: expected_handoffs)

    parity, _digest = service._current_bindings_artifact_parity(bindings, [])

    assert parity is True


@pytest.mark.parametrize("mutation", ["member", "type", "duplicate", "endpoint", "port", "target", "malformed_actual", "malformed_expected"])
def test_bindings_artifact_parity_rejects_handoff_membership_and_routing_changes(
    monkeypatch, tmp_path: Path, mutation: str,
):
    path = tmp_path / "bindings.json"
    monkeypatch.setattr("fwrouter_api.services.xray_runtime_state._xray_bindings_path", lambda: path)
    expected_handoffs = _handoff_rows()
    bindings, state = _handoff_parity_state(expected_handoffs)
    actual_handoffs = json.loads(json.dumps(expected_handoffs))
    if mutation == "member":
        actual_handoffs[0]["subject_ids"][0] = "foreign-subject"
    elif mutation == "type":
        actual_handoffs[0]["subject_ids"][0] = 7
    elif mutation == "duplicate":
        actual_handoffs[0]["client_emails"].append("a@example.test")
    elif mutation == "endpoint":
        actual_handoffs[0]["listen"] = "0.0.0.0"
    elif mutation == "port":
        actual_handoffs[0]["port"] += 1
    elif mutation == "target":
        actual_handoffs[0]["selected_server_id"] = "foreign-server"
    state["handoff_listeners"] = None if mutation == "malformed_actual" else actual_handoffs
    path.write_text(json.dumps(state), encoding="utf-8")
    monkeypatch.setattr("fwrouter_api.services.xray_bindings._bindings_for_state", lambda values: values)
    monkeypatch.setattr(
        "fwrouter_api.services.xray_bindings.get_xray_handoff_listeners",
        lambda _values: None if mutation == "malformed_expected" else expected_handoffs,
    )

    parity, _digest = service._current_bindings_artifact_parity(bindings, [])

    assert parity is False


def test_current_projection_recovery_proves_mixed_binding_and_direct_clients(monkeypatch, tmp_path: Path):
    adapter, checkpoint = _setup(monkeypatch, tmp_path)
    direct = ("22222222-2222-4222-8222-222222222222", "direct@example.test")
    payload = json.loads(adapter.config_path.read_text())
    inbound = next(item for item in payload["inbounds"] if item["tag"] == "vless-ws")
    inbound["settings"]["clients"].append({"id": direct[0], "email": direct[1]})
    adapter.config_path.write_text(json.dumps(payload), encoding="utf-8")
    adapter.loaded = [IDENTITY, direct]
    monkeypatch.setattr(service, "_current_xray_projection_snapshot", lambda: {
        "identities": [IDENTITY, direct], "snapshots": [], "published_snapshots": [],
        "fence": {"revision": 4, "decision_id": "current", "active_server_id": "server"},
    })
    monkeypatch.setattr(service, "collect_xray_client_mode_directives", lambda **_kwargs: [{
        "client_uuid": direct[0], "client_email": direct[1], "effective_mode": "direct",
    }])
    monkeypatch.setattr(xray_materialize, "_verify_active_config_client_modes", lambda *_a, **_k: {
        "ok": True, "verified_client_modes_count": 1,
    })

    result = service._recover_checkpoint_from_current_projection(adapter, checkpoint)

    assert result["ok"] is True, result
    assert result["loaded_identity_count"] == 2
    assert result["modes_verified"] == 1


def _direct_selection_snapshot(*, desired_mode: str = "direct", applied_mode: str = "direct") -> dict[str, Any]:
    return {
        "routing": {
            "server_mode": "fixed",
            "desired_fixed_server_id": None,
            "applied_fixed_server_id": None,
            "active_auto_server_id": None,
            "desired_mode": desired_mode,
            "applied_mode": applied_mode,
            "apply_state": "clean",
            "updated_at": "2026-10-17 00:00:00",
        },
        "selection_revision": 4,
        "selection_decision_id": "current",
    }


@pytest.mark.parametrize("origin_state", ["missing", "foreign", "owned"])
def test_fixed_generation_recovery_rejects_missing_or_foreign_origin_without_mutation(
    monkeypatch, tmp_path: Path, origin_state: str,
) -> None:
    from fwrouter_api.db.connection import db_session
    from fwrouter_api.services.server_global_selection import set_global_fixed_server
    from fwrouter_api.services.server_state import get_routing_global_state
    from fwrouter_api.services.vpn_auto_selection_state import read_selection_revision
    from fwrouter_api.services.xray_vpn_auto_pending import _load_row

    adapter, _unused = _setup(monkeypatch, tmp_path)
    server_id = f"fixed-recovery-origin-{origin_state}"
    with db_session() as connection:
        connection.execute(
            "UPDATE modules SET desired_state='enabled', lifecycle_mode='managed', apply_state='clean' "
            "WHERE module_name='core'",
        )
        connection.execute(
            "INSERT INTO servers(server_id, server_name, provider_name, inventory_state, raw_json) "
            "VALUES (?, ?, 'recovery-test', 'active', ?)",
            (server_id, server_id, json.dumps({"name": server_id, "type": "socks5"}),),
        )
        connection.execute(
            "INSERT INTO server_preferences(server_id, vpn_auto, vpn_auto_priority, global_list) "
            "VALUES (?, 0, 0, 0)", (server_id,),
        )
    fixed = set_global_fixed_server(server_id, requested_by="pytest")
    assert fixed["ok"] is True
    with db_session() as connection:
        connection.execute(
            "UPDATE routing_global_state SET applied_fixed_server_id=?, apply_state='clean' WHERE id=1",
            (server_id,),
        )
        selection_revision = read_selection_revision(connection)
        pending = _load_row(connection)
    route = get_routing_global_state(expire_ttl=False)
    checkpoint = service._xray_generation_checkpoint_path(adapter)
    checkpoint.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    checkpoint.write_text(json.dumps({
        "version": 1,
        "generation_id": "fixed-recovery-operation",
        "selection_operation_id": "fixed-recovery-operation",
        "phase": "transition_applied",
        "fixed_workflow_revision": fixed["pending_revision"],
        "fixed_origin_selection_revision": (
            None if origin_state == "missing"
            else selection_revision if origin_state == "owned"
            else selection_revision - 1
        ),
        "selection_revision": selection_revision,
        "fixed_routing_snapshot": service._fixed_xray_routing_projection(route),
    }), encoding="utf-8")
    checkpoint_before = checkpoint.read_bytes()
    xray_before = adapter.config_path.read_bytes()

    result = service._resolve_existing_xray_generation_checkpoint(adapter, checkpoint)

    assert result["ok"] is False
    assert result["status"] == "pending"
    assert result["error_code"] == "FIXED_XRAY_GENERATION_RECOVERY_UNPROVEN"
    assert result["runtime_verified"] is False
    assert "last_good_retained" not in result
    assert checkpoint.read_bytes() == checkpoint_before
    assert adapter.config_path.read_bytes() == xray_before
    assert adapter.reload_count == 0
    with db_session() as connection:
        current = _load_row(connection)
    assert current["pending"] is True
    assert current["revision"] == pending["revision"]


def _install_direct_readback_fakes(
    monkeypatch, *, native_ok: bool = True, current_routing_override: dict[str, Any] | None = None,
    decision_id: str | None = "current",
) -> None:
    from fwrouter_api.services import dataplane_global, dataplane_live, dataplane_status, servers

    routing = current_routing_override or _direct_selection_snapshot()["routing"]
    monkeypatch.setattr(servers, "get_routing_global_state", lambda **_kwargs: dict(routing))
    from fwrouter_api.services import vpn_auto_selection_state
    monkeypatch.setattr(vpn_auto_selection_state, "read_selection_fence", lambda _connection: {
        "revision": 4, "decision_id": decision_id,
    })
    monkeypatch.setattr(
        dataplane_status, "_read_live_dataplane_payload",
        lambda: {"ok": native_ok, "table_exists": native_ok,
                 "required_chains": {"fwrouter_classify": native_ok},
                 "artifact_consistency": {"ok": native_ok}},
    )
    monkeypatch.setattr(dataplane_status, "_live_owned_table_ready", lambda payload: bool(payload.get("ok")))
    monkeypatch.setattr(
        dataplane_global, "_read_applied_manifest_uncached",
        lambda: {"routing_global_state": dict(routing), "extra": {"rules_effective": {}}},
    )
    monkeypatch.setattr(
        dataplane_live, "_probe_live_global_mode_uncached",
        lambda: {"ok": native_ok, "mode": "direct" if native_ok else "unknown"},
    )
    monkeypatch.setattr(
        dataplane_global, "build_global_preflight",
        lambda **_kwargs: {"can_enforce_global_direct": native_ok},
    )
    monkeypatch.setattr(
        dataplane_status, "build_global_preflight",
        lambda **_kwargs: {"can_enforce_global_direct": native_ok},
    )
    monkeypatch.setattr(
        dataplane_global, "build_applied_runtime_enforcement",
        lambda **_kwargs: {
            "enforcement_level": "global_direct_enforced" if native_ok else "global_direct_only",
            "traffic_enforcement_guaranteed": native_ok,
            "active_mode_matches_intent": native_ok,
            "live_global_mode": "direct" if native_ok else "unknown",
        },
    )
    monkeypatch.setattr(
        dataplane_status, "build_applied_runtime_enforcement",
        lambda **_kwargs: {
            "enforcement_level": "global_direct_enforced" if native_ok else "global_direct_only",
            "traffic_enforcement_guaranteed": native_ok,
            "active_mode_matches_intent": native_ok,
            "live_global_mode": "direct" if native_ok else "unknown",
        },
    )


def test_recovery_selection_readback_accepts_unselected_only_with_checkpointed_native_direct_proof(
    monkeypatch, tmp_path: Path,
):
    adapter, _checkpoint = _setup(monkeypatch, tmp_path)
    selection = _direct_selection_snapshot()
    selection["selection_decision_id"] = None
    monkeypatch.setattr(service, "_verify_generation_selection_readback", lambda _selection: {
        "ok": False, "error_code": "SELECTION_BASELINE_UNSELECTED",
    })
    monkeypatch.setattr(service, "_current_xray_projection_snapshot", lambda: {
        "identities": [IDENTITY], "snapshots": [], "published_snapshots": [],
        "fence": {"revision": 4, "decision_id": None, "active_server_id": None,
                  "provenance_server_id": None},
    })
    _install_direct_readback_fakes(monkeypatch, decision_id=None)

    result = service._verify_generation_recovery_selection_readback(selection)

    assert result["ok"] is True, result
    assert result["mode"] == "direct"
    assert result["native_enforcement_level"] == "global_direct_enforced"


def test_recovery_selection_uses_checkpoint_intent_for_unselected_and_never_synthesizes_legacy_direct(
    monkeypatch, tmp_path: Path,
):
    _adapter, _checkpoint = _setup(monkeypatch, tmp_path)
    current_direct = _direct_selection_snapshot()
    assert service._checkpoint_selection_for_recovery(
        {"auto_selection_after": current_direct}, current_direct,
    ) == current_direct
    assert service._checkpoint_selection_for_recovery({}, current_direct) is None

    current_vpn = {
        "routing": {"server_mode": "auto", "active_auto_server_id": "vpn-target"},
        "selection_revision": 9, "selection_decision_id": "current-vpn",
    }
    assert service._checkpoint_selection_for_recovery({}, current_vpn) is current_vpn


@pytest.mark.parametrize("mutation", [
    "native_missing", "revision", "decision", "desired_vpn", "current_desired_vpn",
    "decision_none_vs_current", "applied_pending", "legacy_checkpoint",
])
def test_recovery_selection_readback_rejects_direct_without_exact_fenced_native_proof(
    monkeypatch, tmp_path: Path, mutation: str,
):
    _adapter, _checkpoint = _setup(monkeypatch, tmp_path)
    selection = _direct_selection_snapshot()
    projection_fence = {"revision": 4, "decision_id": "current"}
    native_ok = True
    current_routing_override = None
    if mutation == "revision":
        projection_fence["revision"] = 5
    elif mutation == "decision":
        projection_fence["decision_id"] = "new-decision"
    elif mutation == "decision_none_vs_current":
        selection["selection_decision_id"] = None
    elif mutation == "desired_vpn":
        selection["routing"]["desired_mode"] = "vpn"
    elif mutation == "current_desired_vpn":
        current_routing_override = _direct_selection_snapshot(desired_mode="vpn", applied_mode="vpn")["routing"]
    elif mutation == "applied_pending":
        selection["routing"]["apply_state"] = "pending"
    elif mutation == "legacy_checkpoint":
        del selection["routing"]["desired_mode"]
        del selection["routing"]["applied_mode"]
        del selection["routing"]["apply_state"]
    elif mutation == "native_missing":
        native_ok = False
    monkeypatch.setattr(service, "_verify_generation_selection_readback", lambda _selection: {
        "ok": False, "error_code": "SELECTION_BASELINE_UNSELECTED",
    })
    monkeypatch.setattr(service, "_current_xray_projection_snapshot", lambda: {
        "identities": [IDENTITY], "snapshots": [], "published_snapshots": [],
        "fence": projection_fence,
    })
    _install_direct_readback_fakes(
        monkeypatch, native_ok=native_ok, current_routing_override=current_routing_override,
    )

    result = service._verify_generation_recovery_selection_readback(selection)

    assert result["ok"] is False, result


def test_recovery_selection_readback_does_not_treat_emergency_direct_as_desired_direct(
    monkeypatch, tmp_path: Path,
):
    _adapter, _checkpoint = _setup(monkeypatch, tmp_path)
    selection = _direct_selection_snapshot(desired_mode="vpn", applied_mode="vpn")
    monkeypatch.setattr(service, "_verify_generation_selection_readback", lambda _selection: {
        "ok": False, "error_code": "SELECTION_BASELINE_UNSELECTED",
    })
    monkeypatch.setattr(service, "_current_xray_projection_snapshot", lambda: {
        "identities": [IDENTITY], "snapshots": [], "published_snapshots": [],
        "fence": {"revision": 4, "decision_id": "current"},
    })
    _install_direct_readback_fakes(monkeypatch)

    result = service._verify_generation_recovery_selection_readback(selection)

    assert result["ok"] is False, result


def test_runtime_incarnation_change_during_native_validation_prevents_reload(monkeypatch, tmp_path: Path):
    adapter, checkpoint = _setup(monkeypatch, tmp_path)
    original_config = adapter.config_path.read_bytes()

    def external_restart_during_validation(_path):
        adapter.incarnation = "external-new-incarnation"
        return XrayApplyResult(ok=True, message="native validation")

    adapter.test_config = external_restart_during_validation
    result = service._recover_checkpoint_from_current_projection(adapter, checkpoint)

    assert result["ok"] is False
    assert result["reason"] == "preapply_revalidation_failed"
    assert adapter.reload_count == 0
    assert adapter.config_path.read_bytes() == original_config
    assert checkpoint.exists()


def test_recovery_rechecks_durable_receipt_after_crash_before_checkpoint_unlink(monkeypatch, tmp_path: Path):
    adapter, checkpoint = _setup(monkeypatch, tmp_path)
    real_fsync = service._fsync_directory
    calls = {"count": 0}
    def fail_receipt_sync(path):
        calls["count"] += 1
        if calls["count"] == 3:
            raise OSError("isolated crash window")
        return real_fsync(path)
    monkeypatch.setattr(service, "_fsync_directory", fail_receipt_sync)

    first = service._recover_checkpoint_from_current_projection(adapter, checkpoint)
    assert first["ok"] is False
    assert checkpoint.exists()
    attempt_path = service._generation_recovery_attempt_path(checkpoint, hashlib.sha256(checkpoint.read_bytes()).hexdigest())
    receipt = json.loads(attempt_path.read_text())
    assert receipt["phase"] == "superseded_verified"
    first_operation = receipt["operation_id"]

    monkeypatch.setattr(service, "_fsync_directory", real_fsync)
    second = service._recover_checkpoint_from_current_projection(adapter, checkpoint)

    assert second["ok"] is True, second
    assert second["operation_id"] == first_operation
    assert not checkpoint.exists()


def test_distinct_generation_checkpoint_gets_distinct_recovery_attempt(monkeypatch, tmp_path: Path):
    adapter, checkpoint = _setup(monkeypatch, tmp_path)
    first_digest = hashlib.sha256(checkpoint.read_bytes()).hexdigest()
    first = service._recover_checkpoint_from_current_projection(adapter, checkpoint)
    assert first["ok"] is True
    first_attempt_path = service._generation_recovery_attempt_path(checkpoint, first_digest)
    assert first_attempt_path.exists()

    checkpoint.write_text(json.dumps({"generation_id": "next-gen", "phase": "restore_failed"}), encoding="utf-8")
    checkpoint.chmod(0o600)
    second_digest = hashlib.sha256(checkpoint.read_bytes()).hexdigest()
    second = service._recover_checkpoint_from_current_projection(adapter, checkpoint)

    assert second["ok"] is True, second
    assert service._generation_recovery_attempt_path(checkpoint, second_digest).exists()
    assert service._generation_recovery_attempt_path(checkpoint, second_digest) != first_attempt_path


@pytest.mark.parametrize("phase,restore_declines", [("restore_failed", False), ("transition_applied", True)])
def test_stale_checkpoint_uses_current_intent_recovery_before_normal_generation(
    monkeypatch, tmp_path: Path, phase, restore_declines,
):
    adapter, checkpoint = _setup(monkeypatch, tmp_path)
    checkpoint.write_text(json.dumps({
        "generation_id": "old-gen",
        "phase": phase,
        # This fixture intentionally models a checkpoint bound to a clean,
        # truly empty inventory. Unbound legacy checkpoints must not adopt
        # current projection state after the generation-admission change.
        "subscription_inventory_generation": {"digest": None, "revision": None},
    }), encoding="utf-8")
    monkeypatch.setattr(service, "_xray_adapter", lambda: adapter)
    calls = {"restore": 0, "current": 0}
    def restore(*_args):
        calls["restore"] += 1
        return {"ok": False, "recovered": "stale_checkpoint_declined"}
    def current(*_args):
        calls["current"] += 1
        return {"ok": False, "reason": "fixture_stop", "recovered": "current_projection_unproven"}
    monkeypatch.setattr(service, "_restore_xray_generation_checkpoint", restore)
    monkeypatch.setattr(service, "_recover_checkpoint_from_current_projection", current)

    result = service._reconcile_xray_subscription_profile_nodes_guarded()

    assert result["status"] == "pending"
    assert result["details"]["reason"] == "fixture_stop"
    assert calls == {"restore": int(restore_declines), "current": 1}
    assert checkpoint.exists()


@pytest.mark.parametrize("failure,reason", [
    ("public_mismatch", "committed_projection_mismatch"),
    ("unsafe_api", "api_listener_unsafe"),
    ("native", "native_candidate_invalid"),
    ("preapply_drift", "preapply_revalidation_failed"),
    ("reload", "xray_reload_failed"),
    ("incarnation_unchanged", "runtime_revalidation_failed"),
    ("loaded_mismatch", "loaded_identity_mismatch"),
    ("binding_readback", "runtime_projection_readback_failed"),
    ("selection_readback", "preapply_revalidation_failed"),
    ("handoff_unready", "handoff_listener_unready"),
    ("terminal_drift", "preterminal_revalidation_failed"),
])
def test_current_projection_recovery_fails_closed_and_retains_checkpoint(monkeypatch, tmp_path: Path, failure, reason):
    adapter, checkpoint = _setup(monkeypatch, tmp_path, fail=failure)
    old_digest = hashlib.sha256(checkpoint.read_bytes()).hexdigest()

    result = service._recover_checkpoint_from_current_projection(adapter, checkpoint)

    assert result == {"ok": False, "recovered": "current_projection_unproven", "reason": reason}
    assert checkpoint.exists()
    assert hashlib.sha256(checkpoint.read_bytes()).hexdigest() == old_digest


def test_ordinary_fixed_checkpoint_recovery_stays_blocked_without_owned_preimage(
    monkeypatch, tmp_path: Path,
) -> None:
    adapter, checkpoint = _setup(monkeypatch, tmp_path)
    checkpoint.write_text(json.dumps({
        "version": 1,
        "generation_id": "ordinary-fixed-recovery-operation",
        "phase": "runtime_applied",
        "selection_revision": 4,
        "fixed_routing_snapshot": {
            "server_mode": "fixed",
            "desired_fixed_server_id": "fixed-target",
            "applied_fixed_server_id": "fixed-target",
            "fixed_server_until": "2099-12-31 23:59:59",
            "desired_mode": "direct",
            "applied_mode": "direct",
            "selective_default": "direct",
            "apply_state": "clean",
        },
    }, sort_keys=True), encoding="utf-8")
    checkpoint.chmod(0o600)
    before = checkpoint.read_bytes()
    xray_before = adapter.config_path.read_bytes()

    result = service._resolve_existing_xray_generation_checkpoint(adapter, checkpoint)
    restored = service._restore_xray_generation_checkpoint(adapter, checkpoint)

    for blocked in (result, restored):
        assert blocked["ok"] is False
        assert blocked["runtime_verified"] is False
        assert "last_good_retained" not in blocked
    assert result["error_code"] == "FIXED_XRAY_GENERATION_RECOVERY_UNPROVEN"
    assert restored["recovered"] == "fixed_generation_recovery_unproven"
    assert checkpoint.read_bytes() == before
    assert adapter.config_path.read_bytes() == xray_before
    assert adapter.reload_count == 0
