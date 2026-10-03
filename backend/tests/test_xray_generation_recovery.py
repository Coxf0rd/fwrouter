from __future__ import annotations

import hashlib
import json
from pathlib import Path

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
    monkeypatch.setattr(service, "_verify_current_public_projection", lambda _snapshot: fail != "public_mismatch")
    monkeypatch.setattr(service, "collect_xray_runtime_bindings", lambda: [{
        "client_uuid": IDENTITY[0], "client_email": IDENTITY[1], "selected_server_id": "server",
    }])
    monkeypatch.setattr(service, "collect_xray_client_mode_directives", lambda: [])
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
    monkeypatch.setattr(service, "collect_xray_client_mode_directives", lambda: [{
        "client_uuid": direct[0], "client_email": direct[1], "effective_mode": "direct",
    }])
    monkeypatch.setattr(xray_materialize, "_verify_active_config_client_modes", lambda *_a, **_k: {
        "ok": True, "verified_client_modes_count": 1,
    })

    result = service._recover_checkpoint_from_current_projection(adapter, checkpoint)

    assert result["ok"] is True, result
    assert result["loaded_identity_count"] == 2
    assert result["modes_verified"] == 1


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
    checkpoint.write_text(json.dumps({"generation_id": "old-gen", "phase": phase}), encoding="utf-8")
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
