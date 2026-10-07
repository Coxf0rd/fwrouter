from __future__ import annotations
from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import initialize_database


import base64
import hashlib
import io
import json
import os
import time
import threading
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import fwrouter_api.services.apply as apply_service
import fwrouter_api.services.dataplane_global as dataplane_global_service
import fwrouter_api.services.mihomo_config as mihomo_config_service
import fwrouter_api.routes.xray as xray_routes
import fwrouter_api.services.xray_clients as xray_clients_service
import fwrouter_api.services.xray_materialize as xray_materialize_service
import fwrouter_api.services.subject_policy as subject_policy_service
from fwrouter_api.adapters import xray as xray_adapter
from fwrouter_api.adapters import xray_real
from fwrouter_api.adapters.dataplane import DataplaneOperation, DataplaneResult
from fwrouter_api.adapters.mihomo import MihomoHealth, MihomoRuntimeState
from fwrouter_api.adapters.xray import (
    NoopXrayAdapter,
    RealXrayAdapter,
    XrayAdapterError,
    XrayApplyResult,
    XrayClient,
    XrayRuntimeState,
)
from fwrouter_api.adapters.xray_common import xray_writer_guard
from fwrouter_api.adapters.xray_common import XRAY_EXPLICIT_DIRECT_OUTBOUND_TAG, XRAY_FALLBACK_OUTBOUND_TAG
from fwrouter_api.db.connection import db_session, initialize_database
from fwrouter_api.jobs.extended_handlers import register_extended_handlers
from fwrouter_api.jobs.manager import get_default_job_manager
from fwrouter_api.main import create_app
from fwrouter_api.services import runtime as runtime_service
from fwrouter_api.services import subject_inventory as inventory_service
from fwrouter_api.services import xray as xray_service
from fwrouter_api.services import xray_subscription_service
from fwrouter_api.services.xray_handoff import _preferred_handoff_port, build_xray_handoff_assignments
from fwrouter_api.services import xray_runtime_state as xray_runtime_state_service
from fwrouter_api.services import xray_status as xray_status_service
from fwrouter_api.services.live_probe_cache import clear_live_probe_cache
from fwrouter_api.services.subject_policy import (
    get_subject_with_effective_state,
    list_subjects_with_effective_state,
    set_subject_mode,
)
from fwrouter_api.services.subjects import get_subject
from fwrouter_api.services.subscription_profiles import (
    list_desired_subscription_xray_clients,
    promote_runtime_verified_subscription_nodes,
    render_subscription_profile,
)


def _configure_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FWROUTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("FWROUTER_XRAY_PUBLIC_HOST", "xray.example.test")
    get_settings.cache_clear()
    clear_live_probe_cache()


def _xray_paths() -> tuple[Path, Path]:
    settings = get_settings()
    return settings.paths.state_dir / "xray" / "config.json", settings.paths.state_dir / "xray" / "docker-compose.yml"


def _write_xray_config(config_path: Path, clients: list[dict[str, object]] | None = None) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "log": {"loglevel": "warning"},
        "inbounds": [
            {
                "listen": "0.0.0.0",
                "port": 5300,
                "protocol": "vless",
                "settings": {"clients": clients or [], "decryption": "none"},
                "streamSettings": {
                    "network": "ws",
                    "wsSettings": {"path": "/vless"},
                },
            }
        ],
        "outbounds": [{"protocol": "freedom", "tag": "direct"}],
    }
    config_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def test_xray_runtime_identity_requires_uuid_and_email_pair_when_both_expected() -> None:
    inbounds = [{
        "tag": "vless-ws",
        "settings": {"clients": [{"id": "uuid-a", "email": "user-a@example.test"}]},
    }]

    assert xray_materialize_service._client_present_in_vless_inbound(
        inbounds, client_id="uuid-a", email="user-a@example.test"
    )
    assert not xray_materialize_service._client_present_in_vless_inbound(
        inbounds, client_id="uuid-b", email="user-a@example.test"
    )
    assert not xray_materialize_service._client_present_in_vless_inbound(
        inbounds, client_id="uuid-a", email="user-b@example.test"
    )


def test_xray_generation_status_requires_exact_managed_identity_set() -> None:
    bindings = [{"client_uuid": "uuid-a", "client_email": "sub-a@fwrouter.local"}]
    modes = [{"client_uuid": "uuid-b", "client_email": "sub-b@fwrouter.local"}]
    base = {"inbounds": [{"tag": "vless-ws", "settings": {"clients": [
        {"id": "uuid-a", "email": "sub-a@fwrouter.local"},
        {"id": "uuid-b", "email": "sub-b@fwrouter.local"},
        {"id": "standalone", "email": "regular@example.test"},
    ]}}]}
    expected, actual = xray_status_service._managed_identity_sets(bindings, modes, base)
    assert expected == actual

    base["inbounds"][0]["settings"]["clients"].append(
        {"id": "extra", "email": "sub-extra@fwrouter.local"}
    )
    expected, actual = xray_status_service._managed_identity_sets(bindings, modes, base)
    assert expected != actual

    base["inbounds"][0]["settings"]["clients"] = [
        item for item in base["inbounds"][0]["settings"]["clients"]
        if item.get("id") != "uuid-b"
    ]
    expected, actual = xray_status_service._managed_identity_sets(bindings, modes, base)
    assert expected != actual


def test_xray_generation_checkpoint_prevents_false_ready(monkeypatch, tmp_path: Path) -> None:
    from types import SimpleNamespace

    checkpoint_root = tmp_path / "xray"
    bindings_path = checkpoint_root / "fwrouter-bindings.json"
    checkpoint_path = checkpoint_root / ".generation" / "generation-checkpoint.json"
    checkpoint_path.parent.mkdir(parents=True)
    checkpoint_path.write_text('{"phase":"runtime_applied"}\n', encoding="utf-8")
    bindings = [{"client_uuid": "uuid-a", "client_email": "sub-a@fwrouter.local"}]
    modes: list[dict[str, object]] = []
    payload = {"inbounds": [{"tag": "vless-ws", "settings": {"clients": [
        {"id": "uuid-a", "email": "sub-a@fwrouter.local"},
    ]}}]}
    synced: dict[str, object] = {}
    monkeypatch.setattr(xray_status_service, "_xray_bindings_path", lambda: bindings_path)
    monkeypatch.setattr(xray_status_service, "_load_xray_bindings_state", lambda: {
        "bindings": bindings,
        "client_modes": modes,
        "bindings_count": 1,
        "applied_count": 1,
        "handoff_listeners": [{"port": 12345}],
    })
    monkeypatch.setattr(xray_status_service, "_module_state", lambda _name: {"desired_state": "enabled"})
    monkeypatch.setattr(xray_status_service, "_xray_config_egress_summary", lambda: {"traffic_available": True})
    monkeypatch.setattr(xray_status_service.DEFAULT_XRAY_ADAPTER, "health", lambda: SimpleNamespace(
        details={"clients_count": 1},
        runtime_state=SimpleNamespace(value="running"),
        message="ready",
    ))
    monkeypatch.setattr(xray_status_service.DEFAULT_MIHOMO_ADAPTER, "check_port", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(xray_materialize_service, "_verify_active_config_bindings", lambda _items: {
        "ok": True, "verified_bindings_count": 1, "missing_clients": [], "missing_rules": [],
    })
    monkeypatch.setattr(xray_materialize_service, "_verify_active_config_client_modes", lambda _items: {
        "ok": True, "verified_client_modes_count": 0, "missing_clients": [], "missing_rules": [],
    })
    monkeypatch.setattr(xray_materialize_service, "_load_active_config_payload", lambda: (payload, None))
    monkeypatch.setattr(
        xray_status_service,
        "_sync_xray_module_runtime_state",
        lambda **kwargs: synced.update(kwargs) or kwargs["module"],
    )

    result = xray_status_service._get_xray_status_uncached()

    assert result["forced_vpn_ready"] is False
    assert result["details"]["bindings"]["generation"]["pending"] is True
    assert result["details"]["bindings"]["generation"]["phase"] == "runtime_applied"
    assert synced["forced_vpn_ready"] is False


def test_xray_generation_startup_recovers_checkpoint_before_new_stage(monkeypatch, tmp_path: Path) -> None:
    adapter, checkpoint = _seed_current_generation_for_recovery(monkeypatch, tmp_path)
    config_path = adapter.config_path
    runtime_before = adapter.config_path.read_bytes()
    reloads_before = adapter._runner.reload_count
    observed: dict[str, bytes] = {}

    def reject_followup_stage(**_kwargs):
        observed["config"] = config_path.read_bytes()
        return {"ok": False, "stage": "injected_after_recovery", "error_code": "STOP_AFTER_RECOVERY"}

    monkeypatch.setattr(xray_subscription_service, "_stage_profile_native_candidates", reject_followup_stage)
    result = xray_service.reconcile_xray_subscription_profile_nodes(requested_by="pytest")

    assert result["ok"] is False
    assert observed["config"] == runtime_before
    assert adapter._runner.reload_count == reloads_before + 1
    assert not checkpoint.exists()


def test_xray_generation_snapshot_commit_gap_stays_pending_on_restart(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _patch_runtime(monkeypatch)
    _enable_xray_module()
    _seed_subscription_identity(slug="snapshot-gap", token="snapshot-gap")
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [{"id": "last-good", "email": "last-good@example.test"}])
    adapter = _build_adapter(tmp_path, runner=_FakeRunner())
    _patch_xray_adapters(monkeypatch, adapter)
    last_good = config_path.read_bytes()
    checkpoint = xray_subscription_service._write_xray_generation_checkpoint(
        adapter=adapter,
        generation_id="snapshot-gap-fixture",
        tokens={"snapshot-gap"},
        phase="bindings_written",
        source_fingerprint=xray_subscription_service._generation_source_fingerprint(),
        staged_generation={"native_validation": {"xray": {"expected_client_identities": []}}},
        managed_email_prefixes=["sub-"],
    )
    with db_session() as connection:
        connection.execute(
            "INSERT INTO subscription_profile_snapshots (token, nodes_json, runtime_verified_at, updated_at) VALUES (?, '[]', 'test', 'test')",
            ("snapshot-gap",),
        )
    corrupted_runtime = last_good + b" "
    config_path.write_bytes(corrupted_runtime)

    result = xray_service.reconcile_xray_subscription_profile_nodes(requested_by="pytest")

    assert result["ok"] is False
    assert result["status"] == "pending"
    assert result["stage"] == "generation_recovery"
    assert config_path.read_bytes() == corrupted_runtime
    assert checkpoint.exists()
    assert checkpoint.exists()
    with db_session() as connection:
        snapshot = connection.execute(
            "SELECT nodes_json FROM subscription_profile_snapshots WHERE token = ?",
            ("snapshot-gap",),
        ).fetchone()
    assert snapshot["nodes_json"] == "[]"


def _seed_current_generation_for_recovery(monkeypatch, tmp_path: Path, *, extra_account_client: bool = False):
    import fwrouter_api.adapters.mihomo as mihomo_adapter_module
    import fwrouter_api.services.selector as selector_module
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _patch_runtime(monkeypatch)
    monkeypatch.setattr(mihomo_adapter_module, "DEFAULT_MIHOMO_ADAPTER", _ReadyMihomoAdapter())
    monkeypatch.setattr(
        subject_policy_service, "build_runtime_enforcement_state",
        lambda: {"supported_modes": {"direct": True, "selective": False, "vpn": True},
                "enforcement_level": "global_vpn_enforced", "traffic_enforcement_guaranteed": True},
    )
    _enable_xray_module()
    _seed_subscription_identity(slug="recovery-real", token="recovery-real")
    if extra_account_client:
        with db_session() as connection:
            account = connection.execute(
                "SELECT account_id FROM subscription_accounts WHERE slug = ?",
                ("recovery-real",),
            ).fetchone()
            assert account is not None
            connection.execute(
                """
                INSERT INTO subscription_clients (account_id, token, app_type, enabled, display_name)
                VALUES (?, ?, 'auto', 1, ?)
                """,
                (account["account_id"], "recovery-real-extra", "Recovery extra"),
            )
    _seed_server("server-1")
    _seed_routing_state(desired_mode="vpn", active_auto_server_id="server-1")
    from fwrouter_api.services.logical_topology import get_logical_runtime_name
    runtime_target = get_logical_runtime_name("server-1")
    monkeypatch.setattr(selector_module, "get_vpn_auto_state", lambda **_kwargs: {
        "selector_runtime": {"vpn_auto_now": runtime_target, "vpn_global_now": runtime_target},
        "active_auto_server_id": "server-1", "active_auto_target_valid": True,
        "config_consistent": True,
    })
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [])
    adapter = _build_adapter(tmp_path, runner=_FakeRunner())
    _patch_xray_adapters(monkeypatch, adapter)
    applied = xray_service.reconcile_xray_subscription_profile_nodes(requested_by="pytest")
    assert applied["ok"] is True, applied
    checkpoint = xray_subscription_service._xray_generation_checkpoint_path(adapter)
    checkpoint.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    checkpoint.write_text(json.dumps({"generation_id": "isolated-real-projection", "phase": "restore_failed"}), encoding="utf-8")
    checkpoint.chmod(0o600)
    return adapter, checkpoint


def test_current_recovery_uses_real_sql_bindings_modes_and_virtual_public_alias(monkeypatch, tmp_path: Path) -> None:
    adapter, checkpoint = _seed_current_generation_for_recovery(monkeypatch, tmp_path)
    projection = xray_subscription_service._current_xray_projection_snapshot()
    assert projection["identities"]
    assert projection["published_snapshots"]
    assert xray_subscription_service._verify_current_public_projection(projection) is True
    assert any(
        "virtual:xray:vpn-auto" in row["nodes_json"]
        for row in projection["published_snapshots"]
    )

    result = xray_subscription_service._recover_checkpoint_from_current_projection(adapter, checkpoint)

    assert result["ok"] is True, result
    assert result["loaded_identity_count"] == len(projection["identities"])
    assert not checkpoint.exists()


def test_current_recovery_accepts_reordered_persisted_handoff_associations(monkeypatch, tmp_path: Path) -> None:
    adapter, checkpoint = _seed_current_generation_for_recovery(monkeypatch, tmp_path, extra_account_client=True)
    from fwrouter_api.services.xray_bindings import collect_xray_client_mode_directives, collect_xray_runtime_bindings

    bindings_path = xray_runtime_state_service._xray_bindings_path()
    original = json.loads(bindings_path.read_text(encoding="utf-8"))
    multi_associations = [
        item for item in original.get("handoff_listeners", [])
        if len(item.get("subject_ids") or []) > 1 or len(item.get("client_emails") or []) > 1
    ]
    assert multi_associations
    for item in original["handoff_listeners"]:
        for key in ("subject_ids", "client_emails"):
            if len(item.get(key) or []) > 1:
                item[key].reverse()
    original_bytes = json.dumps(original, sort_keys=True).encode("utf-8")
    bindings_path.write_bytes(original_bytes)
    digest_before_recovery = hashlib.sha256(bindings_path.read_bytes()).hexdigest()

    parity, _artifact_digest = xray_subscription_service._current_bindings_artifact_parity(
        collect_xray_runtime_bindings(), collect_xray_client_mode_directives(),
    )
    assert parity is True
    result = xray_subscription_service._recover_checkpoint_from_current_projection(adapter, checkpoint)

    assert result["ok"] is True, result
    assert not checkpoint.exists()
    assert hashlib.sha256(bindings_path.read_bytes()).hexdigest() == digest_before_recovery


def test_public_wrapper_deferred_staged_generation_finalizes_and_is_idempotent(monkeypatch, tmp_path: Path) -> None:
    adapter, checkpoint = _seed_current_generation_for_recovery(monkeypatch, tmp_path)
    callback_contexts: list[tuple[str, int]] = []

    def verify_selection(*, operation_id: str, expected_selection_revision: int):
        callback_contexts.append((operation_id, expected_selection_revision))
        return {
            "ok": True,
            "operation_id": operation_id,
            "selection_revision": expected_selection_revision,
        }

    def reconcile_once():
        return xray_subscription_service.reconcile_xray_subscription_profile_nodes(
            requested_by="pytest-deferred-publication",
            include_vpn_auto=True,
            verification_callback=verify_selection,
        )

    first = reconcile_once()

    assert first["ok"] is True, first
    assert first["status"] == "success"
    assert first["pre_publication_verification"]["ok"] is True
    assert callback_contexts and callback_contexts[0][0]
    assert not checkpoint.exists()
    projection = xray_subscription_service._current_xray_projection_snapshot()
    assert xray_subscription_service._verify_current_public_projection(projection) is True
    active_payload, active_inbound, _ = adapter._load_clients_and_config()
    expected_loaded = sorted(
        (str(client.get("id") or ""), str(client.get("email") or ""))
        for client in (active_inbound.get("settings") or {}).get("clients", [])
        if client.get("id") and client.get("email")
    )
    assert sorted(adapter.list_loaded_client_identities()) == expected_loaded
    first_public_nodes = projection["published_snapshots"]

    second = reconcile_once()

    assert second["ok"] is True, second
    assert second["created_count"] == 0
    assert not checkpoint.exists()
    assert len(callback_contexts) == 2
    assert callback_contexts[1][0]
    assert callback_contexts[1][0] != callback_contexts[0][0]
    second_projection = xray_subscription_service._current_xray_projection_snapshot()
    assert xray_subscription_service._verify_current_public_projection(second_projection) is True
    assert len(second_projection["published_snapshots"]) == len(first_public_nodes)
    second_payload, second_inbound, _ = adapter._load_clients_and_config()
    assert sorted(adapter.list_loaded_client_identities()) == sorted(
        (str(client.get("id") or ""), str(client.get("email") or ""))
        for client in (second_inbound.get("settings") or {}).get("clients", [])
        if client.get("id") and client.get("email")
    )


def test_current_recovery_rejects_persisted_public_node_with_wrong_server(monkeypatch, tmp_path: Path) -> None:
    adapter, checkpoint = _seed_current_generation_for_recovery(monkeypatch, tmp_path)
    previous_calls = list(adapter._runner.calls)
    with db_session() as connection:
        row = connection.execute(
            "SELECT token, nodes_json FROM subscription_profile_snapshots WHERE runtime_verified_at IS NOT NULL LIMIT 1",
        ).fetchone()
        assert row is not None
        nodes = json.loads(row["nodes_json"])
        assert nodes
        nodes[0]["server_id"] = "stale-server-target"
        connection.execute(
            "UPDATE subscription_profile_snapshots SET nodes_json = ? WHERE token = ?",
            (json.dumps(nodes), row["token"]),
        )

    result = xray_subscription_service._recover_checkpoint_from_current_projection(adapter, checkpoint)

    assert result["ok"] is False
    assert result["reason"] == "committed_projection_mismatch"
    assert adapter._runner.calls == previous_calls
    assert checkpoint.exists()


@pytest.mark.parametrize("failure", ["loaded_users", "missing_checkpoint"])
def test_non_deferred_staged_generation_requires_native_user_readback_before_publication(
    monkeypatch, tmp_path: Path, failure: str,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _patch_runtime(monkeypatch)
    monkeypatch.setattr(
        subject_policy_service, "build_runtime_enforcement_state",
        lambda: {"supported_modes": {"direct": True, "selective": False, "vpn": True},
                "enforcement_level": "global_vpn_enforced", "traffic_enforcement_guaranteed": True},
    )
    _enable_xray_module()
    _seed_subscription_identity(slug="no-callback", token="no-callback")
    _seed_server("server-1")
    _seed_routing_state(desired_mode="vpn", active_auto_server_id=None)
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [])
    runner = _FakeRunner()
    if failure == "loaded_users":
        runner.loaded_users_stdout = json.dumps({"users": [{
            "email": "unexpected@example.test",
            "account": {"_TypedMessage_": "xray.proxy.vless.Account", "id": "33333333-3333-4333-8333-333333333333"},
        }]})
    else:
        record = xray_subscription_service._record_xray_generation_derived_rows
        def remove_checkpoint_after_projection(path: Path, *, phase: str):
            record(path, phase=phase)
            if phase == "projections_cleaned":
                path.unlink()
        monkeypatch.setattr(xray_subscription_service, "_record_xray_generation_derived_rows", remove_checkpoint_after_projection)
    adapter = _build_adapter(tmp_path, runner=runner)
    _patch_xray_adapters(monkeypatch, adapter)

    result = xray_service.reconcile_xray_subscription_profile_nodes(requested_by="pytest")

    assert result["ok"] is False
    assert result["status"] == "pending"
    assert result["error_code"] == (
        "XRAY_GENERATION_RUNTIME_READBACK_FAILED" if failure == "loaded_users"
        else "XRAY_GENERATION_CHECKPOINT_UNAVAILABLE"
    )
    checkpoint = xray_subscription_service._xray_generation_checkpoint_path(adapter)
    assert checkpoint.exists() is (failure == "loaded_users")
    with db_session() as connection:
        snapshot = connection.execute(
            "SELECT 1 FROM subscription_profile_snapshots WHERE token = ?",
            ("no-callback",),
        ).fetchone()
    assert snapshot is None


class _FinalizerRuntimeAdapter:
    def __init__(self, config_path: Path, candidate_sha: str, identities, loaded=None) -> None:
        self.config_path = config_path
        self.candidate_sha = candidate_sha
        self.identities = list(identities if loaded is None else loaded)

    def get_runtime_incarnation(self) -> str:
        return "xray-finalized-running"

    def get_runtime_config_sha256(self) -> str:
        return self.candidate_sha

    def list_loaded_client_identities(self):
        return list(self.identities)


def _finalizer_pending(monkeypatch, tmp_path: Path, *, loaded=None):
    identities = [("11111111-1111-4111-8111-111111111111", "generation@example.test")]
    config_path = tmp_path / "xray-config.json"
    config_path.write_text("native-tested-generation", encoding="utf-8")
    candidate_sha = hashlib.sha256(config_path.read_bytes()).hexdigest()
    mihomo_path = tmp_path / "mihomo-final.yaml"
    mihomo_path.write_text("mihomo-finalized", encoding="utf-8")
    from fwrouter_api.services import mihomo_config as mihomo_config_module
    import fwrouter_api.services.mihomo_runtime as mihomo_runtime_module
    from fwrouter_api.services.vpn_auto_selection_state import read_selection_fence
    monkeypatch.setattr(mihomo_config_module, "_resolved_base_config_path", lambda: mihomo_path)
    monkeypatch.setattr(mihomo_runtime_module, "get_mihomo_runtime_incarnation", lambda: "mihomo-finalized-running")
    with db_session() as connection:
        selection_revision = read_selection_fence(connection)["revision"]
    checkpoint = tmp_path / "generation-checkpoint.json"
    checkpoint.write_text(json.dumps({
        "subscription_snapshots": {"generation-test": None},
        "phase": "selection_verified", "xray_runtime_incarnation_after": "xray-finalized-running",
        "mihomo_runtime_incarnation": "mihomo-finalized-running",
        "selection_revision": selection_revision,
        "source_fingerprint": xray_subscription_service._generation_source_fingerprint(),
        "derived_source_fingerprint": xray_subscription_service._generation_source_fingerprint(),
        "staged_generation": {"mihomo_final_sha256": hashlib.sha256(mihomo_path.read_bytes()).hexdigest()},
    }), encoding="utf-8")
    pending = {
        "checkpoint_path": str(checkpoint),
        "adapter": _FinalizerRuntimeAdapter(config_path, candidate_sha, identities, loaded),
        "result_values": {
            "applied_transition": {"ok": True}, "applied_xray": type("Result", (), {"details": {"ok": True}})(),
            "applied_final_mihomo": {"ok": True}, "desired_nodes": [], "created": [], "deleted": [],
            "recreated": [], "reconcile_details": {}, "materialize_result": {"ok": True},
            "staged_generation": {
                "xray_candidate_sha256": candidate_sha,
                "native_validation": {"xray": {"expected_client_identities": identities}},
            },
        },
        "materialize": True, "promote_public_profile": True,
        "subscription_nodes": [], "affected_profile_tokens": ["generation-test"],
    }
    return pending, checkpoint


def test_staged_generation_finalizer_returns_success_and_closes_checkpoint_last(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _seed_subscription_identity(slug="generation-test", token="generation-test")
    pending, checkpoint = _finalizer_pending(monkeypatch, tmp_path)
    result = xray_subscription_service._finalize_xray_profile_publication(
        pending, {"ok": True, "operation_id": "generation-test",
                  "selection_revision": json.loads(checkpoint.read_text())["selection_revision"]},
    )

    assert result["ok"] is True
    assert result["generation_apply"]["public_snapshots_changed"] is True
    assert not checkpoint.exists()
    with db_session() as connection:
        snapshot = connection.execute(
            "SELECT nodes_json FROM subscription_profile_snapshots WHERE token = ?", ("generation-test",),
        ).fetchone()
    assert snapshot["nodes_json"] == "[]"


def test_staged_generation_finalizer_keeps_checkpoint_on_publication_failure(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _seed_subscription_identity(slug="generation-test", token="generation-test")
    pending, checkpoint = _finalizer_pending(monkeypatch, tmp_path)
    monkeypatch.setattr(
        xray_subscription_service, "_xray_generation_snapshots_changed",
        lambda _path: (_ for _ in ()).throw(RuntimeError("injected publication readback failure")),
    )

    try:
        xray_subscription_service._finalize_xray_profile_publication(pending, {"ok": True})
    except RuntimeError as exc:
        assert "injected publication readback failure" in str(exc)
    else:
        raise AssertionError("expected injected publication readback failure")

    assert checkpoint.exists()


def test_staged_generation_finalizer_rejects_wrong_loaded_users_before_publication(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _seed_subscription_identity(slug="generation-test", token="generation-test")
    pending, checkpoint = _finalizer_pending(monkeypatch, tmp_path, loaded=[])

    result = xray_subscription_service._finalize_xray_profile_publication(pending, {"ok": True})

    assert result["ok"] is False
    assert result["error_code"] == "XRAY_GENERATION_RUNTIME_READBACK_FAILED"
    assert checkpoint.exists()
    with db_session() as connection:
        snapshot = connection.execute(
            "SELECT nodes_json FROM subscription_profile_snapshots WHERE token = ?", ("generation-test",),
        ).fetchone()
    assert snapshot is None


def test_xray_handoff_assignment_preserves_applied_port_when_new_target_collides() -> None:
    targets = [f"target-{index}" for index in range(100)]
    pair = next(
        (old, new)
         for old in targets
         for new in targets
         if old != new
         and (_preferred_handoff_port(new), new) < (_preferred_handoff_port(old), old)
    )
    old_target, new_target = pair
    preserved_port = _preferred_handoff_port(new_target)
    assignments = build_xray_handoff_assignments(
        [
            {"selected_server_id": old_target, "handoff_proxy_name": old_target},
            {"selected_server_id": new_target, "handoff_proxy_name": new_target},
        ],
        preserve_assignments=[{
            "selected_server_id": old_target,
            "listener_name": f"listener-{old_target}",
            "listen": "172.18.0.1",
            "port": preserved_port,
            "proxy": "previous-runtime-name",
        }],
    )
    by_target = {item["selected_server_id"]: item for item in assignments}

    assert by_target[old_target]["port"] == preserved_port
    assert by_target[old_target]["listener_name"] == f"listener-{old_target}"
    assert by_target[old_target]["proxy"] == old_target
    assert by_target[new_target]["port"] != preserved_port


def test_xray_generation_stage_is_read_only_and_apply_rejects_tampered_candidate(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [{"id": "uuid-existing", "email": "existing@example.test"}])
    adapter = _build_adapter(tmp_path, runner=_FakeRunner())
    before = config_path.read_bytes()
    staged_path = tmp_path / "xray-generation.candidate.json"

    staged = adapter.stage_subscription_generation(
        desired_clients=[{"client_uuid": "uuid-new", "email": "new@example.test", "alias": "new"}],
        managed_email_prefixes=["sub-"],
        bindings=[],
        client_modes=[],
        candidate_path=staged_path,
    )

    assert staged.ok is True
    assert config_path.read_bytes() == before
    staged_path.write_text(staged_path.read_text(encoding="utf-8") + " ", encoding="utf-8")
    applied = adapter.apply_staged_subscription_generation(
        staged_path,
        expected_sha256=staged.details["candidate_sha256"],
    )

    assert applied.ok is False
    assert applied.error_code == "XRAY_STAGE_CANDIDATE_CHANGED_AFTER_VALIDATION"
    assert config_path.read_bytes() == before


def test_xray_generation_stage_rejects_candidate_modified_by_native_validator(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [])

    class MutatingRunner(_FakeRunner):
        def __call__(self, action: str, payload: dict[str, object]) -> XrayApplyResult:
            result = super().__call__(action, payload)
            if action == "test_config":
                Path(str(payload["path"])).write_text("{}\n", encoding="utf-8")
            return result

    adapter = _build_adapter(tmp_path, runner=MutatingRunner())
    staged = adapter.stage_subscription_generation(
        desired_clients=[],
        managed_email_prefixes=["sub-"],
        bindings=[],
        client_modes=[],
        candidate_path=tmp_path / "mutated.candidate.json",
    )

    assert staged.ok is False
    assert staged.error_code == "XRAY_STAGE_CANDIDATE_CHANGED_DURING_VALIDATION"


def _run_profile_candidate_validation_case(
    monkeypatch,
    tmp_path: Path,
    *,
    old_assignments: list[dict[str, object]],
    assignments: list[dict[str, object]],
    native_result: dict[str, object] | None = None,
    mutate_candidate: bool = False,
    image_id: str | None = "sha256:" + "a" * 64,
):
    from types import SimpleNamespace
    from fwrouter_api.services import subscription_pipeline

    config_path = tmp_path / "xray" / "config.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text("{}\n", encoding="utf-8")
    native_calls: list[str] = []
    local_calls: list[str] = []

    class Adapter:
        def __init__(self):
            self.config_path = config_path

        def stage_subscription_generation(self, *, candidate_path: Path, **kwargs):
            candidate_path.parent.mkdir(parents=True, exist_ok=True)
            candidate_path.write_text("{}\n", encoding="utf-8")
            return SimpleNamespace(ok=True, details={"candidate_sha256": "xray-candidate"})

    def write_candidate(*, candidate_path, xray_handoff_assignments, **kwargs):
        candidate_config = {"handoffs": xray_handoff_assignments}
        candidate_path = Path(candidate_path)
        candidate_path.write_text(json.dumps(candidate_config, sort_keys=True) + "\n", encoding="utf-8")
        return {"candidate_path": str(candidate_path), "_candidate_config": candidate_config}

    def validate_local(*, candidate_path, candidate_config):
        local_calls.append(str(candidate_path))
        return {"ok": True, "structure_checked": True}

    def validate_native(candidate_path, *, image_reference=None):
        native_calls.append(str(candidate_path))
        assert image_reference == image_id
        if mutate_candidate:
            Path(candidate_path).write_text("changed\n", encoding="utf-8")
        return dict(native_result or {"ok": True, "returncode": 0})

    monkeypatch.setattr(xray_subscription_service, "_applied_handoff_assignments", lambda: old_assignments)
    monkeypatch.setattr(mihomo_config_service, "write_mihomo_candidate_config", write_candidate)
    monkeypatch.setattr(mihomo_config_service, "validate_mihomo_candidate_config", validate_local)
    monkeypatch.setattr(subscription_pipeline, "resolve_mihomo_validator_image_id", lambda: image_id)
    monkeypatch.setattr(subscription_pipeline, "validate_mihomo_candidate_config", validate_native)
    result = xray_subscription_service._stage_profile_native_candidates(
        adapter=Adapter(), desired_clients=[], managed_email_prefixes=[], bindings=[],
        client_modes=[], assignments=assignments,
    )
    return result, local_calls, native_calls


def test_profile_generation_reuses_native_validation_for_identical_transition_and_final_candidates(
    monkeypatch,
    tmp_path: Path,
) -> None:
    assignment = {"selected_server_id": "server-a", "port": 53123}
    result, local_calls, native_calls = _run_profile_candidate_validation_case(
        monkeypatch,
        tmp_path,
        old_assignments=[],
        assignments=[assignment],
    )

    assert result["ok"] is True
    assert len(local_calls) == 2
    assert len(native_calls) == 1
    transition = result["native_validation"]["transition"]["validation"]
    final = result["native_validation"]["final"]["validation"]
    assert transition["native_validation_reused"] is False
    assert final["native_validation_reused"] is True


def test_profile_generation_validates_distinct_transition_and_final_candidates_independently(
    monkeypatch,
    tmp_path: Path,
) -> None:
    old_assignment = {"selected_server_id": "server-old", "port": 53123}
    new_assignment = {"selected_server_id": "server-new", "port": 53124}
    result, local_calls, native_calls = _run_profile_candidate_validation_case(
        monkeypatch,
        tmp_path,
        old_assignments=[old_assignment],
        assignments=[new_assignment],
    )

    assert result["ok"] is True
    assert len(local_calls) == 2
    assert len(native_calls) == 2
    assert result["native_validation"]["transition"]["validation"]["native_validation_reused"] is False
    assert result["native_validation"]["final"]["validation"]["native_validation_reused"] is False


def test_profile_generation_does_not_reuse_failed_or_mutated_native_validation(
    monkeypatch,
    tmp_path: Path,
) -> None:
    assignment = {"selected_server_id": "server-a", "port": 53123}
    failed, _local_calls, failed_native_calls = _run_profile_candidate_validation_case(
        monkeypatch,
        tmp_path / "failed",
        old_assignments=[],
        assignments=[assignment],
        native_result={"ok": False, "returncode": 1},
    )
    assert failed["ok"] is False
    assert len(failed_native_calls) == 1

    changed, _local_calls, changed_native_calls = _run_profile_candidate_validation_case(
        monkeypatch,
        tmp_path / "changed",
        old_assignments=[],
        assignments=[assignment],
        mutate_candidate=True,
    )
    assert changed["ok"] is False
    assert changed["error_code"] == "MIHOMO_STAGE_CANDIDATE_CHANGED_DURING_VALIDATION"
    assert len(changed_native_calls) == 1


def test_profile_generation_does_not_reuse_when_native_image_identity_is_unavailable(
    monkeypatch,
    tmp_path: Path,
) -> None:
    assignment = {"selected_server_id": "server-a", "port": 53123}
    result, _local_calls, native_calls = _run_profile_candidate_validation_case(
        monkeypatch,
        tmp_path,
        old_assignments=[],
        assignments=[assignment],
        image_id=None,
    )

    assert result["ok"] is True
    assert len(native_calls) == 2
    assert result["native_validation"]["transition"]["validation"]["native_validation_reused"] is False
    assert result["native_validation"]["final"]["validation"]["native_validation_reused"] is False


def test_profile_generation_revalidates_same_candidate_across_distinct_image_identities(
    monkeypatch,
    tmp_path: Path,
) -> None:
    assignment = {"selected_server_id": "server-a", "port": 53123}
    first, _local_a, native_a = _run_profile_candidate_validation_case(
        monkeypatch,
        tmp_path / "first",
        old_assignments=[],
        assignments=[assignment],
        image_id="sha256:" + "a" * 64,
    )
    second, _local_b, native_b = _run_profile_candidate_validation_case(
        monkeypatch,
        tmp_path / "second",
        old_assignments=[],
        assignments=[assignment],
        image_id="sha256:" + "b" * 64,
    )

    assert first["ok"] is True and second["ok"] is True
    # Memo lifetime is a single generation call; an identical candidate is
    # validated again when the resolved native runtime identity changes.
    assert len(native_a) == 1
    assert len(native_b) == 1


def test_mihomo_validator_resolves_and_uses_immutable_local_image_id(monkeypatch, tmp_path: Path) -> None:
    from types import SimpleNamespace
    from fwrouter_api.services import subscription_pipeline

    image_id = "sha256:" + "c" * 64
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(list(args))
        if args[:3] == ["docker", "image", "inspect"]:
            return SimpleNamespace(returncode=0, stdout=image_id + "\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(subscription_pipeline.subprocess, "run", fake_run)
    assert subscription_pipeline.resolve_mihomo_validator_image_id() == image_id

    candidate = tmp_path / "candidate.yaml"
    candidate.write_text("mixed-port: 1\n", encoding="utf-8")
    result = subscription_pipeline.validate_mihomo_candidate_config(
        str(candidate), image_reference=image_id,
    )

    assert result["ok"] is True
    assert calls[1][0:3] == ["docker", "run", "--rm"]
    assert calls[1][-4] == image_id


def test_mihomo_validator_identity_unavailable_or_invalid_is_not_pinned(monkeypatch) -> None:
    from types import SimpleNamespace
    from fwrouter_api.services import subscription_pipeline

    for result in (
        SimpleNamespace(returncode=1, stdout="", stderr="missing"),
        SimpleNamespace(returncode=0, stdout="not-a-digest\n", stderr=""),
    ):
        monkeypatch.setattr(subscription_pipeline.subprocess, "run", lambda *args, _result=result, **kwargs: _result)
        assert subscription_pipeline.resolve_mihomo_validator_image_id() is None


def test_stale_subscription_snapshot_is_filtered_by_earlier_disabled_mode_rule(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _enable_xray_module()
    config_path, _ = _xray_paths()
    _write_xray_config(
        config_path,
        [{
            "id": "uuid-disabled",
            "email": "disabled@example.test",
            "fwrouterBinding": {"selected_server_id": "server-1"},
        }],
    )
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    payload["outbounds"].extend([
        {"protocol": "blackhole", "tag": XRAY_FALLBACK_OUTBOUND_TAG},
        {"protocol": "socks", "tag": "fwrouter-egress-test", "settings": {"servers": [{"address": "127.0.0.1", "port": 53123}]}},
    ])
    payload["routing"] = {"rules": [
        {"type": "field", "inboundTag": ["vless-ws"], "user": ["disabled@example.test"], "outboundTag": XRAY_FALLBACK_OUTBOUND_TAG},
        {"type": "field", "inboundTag": ["vless-ws"], "user": ["disabled@example.test"], "outboundTag": "fwrouter-egress-test"},
    ]}
    config_path.write_text(json.dumps(payload), encoding="utf-8")

    from fwrouter_api.services.subscription_profiles import filter_runtime_exportable_subscription_nodes
    nodes = [{
        "client_uuid": "uuid-disabled",
        "client_email": "disabled@example.test",
        "server_id": "server-1",
    }]

    assert filter_runtime_exportable_subscription_nodes(nodes) == []


def test_subscription_export_ignores_later_or_destination_scoped_mode_rules(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _enable_xray_module()
    config_path, _ = _xray_paths()
    _write_xray_config(
        config_path,
        [{"id": "uuid-active", "email": "active@example.test", "fwrouterBinding": {"selected_server_id": "server-1"}}],
    )
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    payload["inbounds"][0]["tag"] = "vless-ws"
    payload["outbounds"].append({"protocol": "socks", "tag": "fwrouter-egress-test", "settings": {"servers": [{"address": "127.0.0.1", "port": 53123}]}})
    payload["routing"] = {"rules": [
        {"type": "field", "inboundTag": ["vless-ws"], "user": ["active@example.test"], "outboundTag": "fwrouter-egress-test"},
        {"type": "field", "inboundTag": ["vless-ws"], "user": ["active@example.test"], "domain": ["blocked.example.test"], "outboundTag": XRAY_FALLBACK_OUTBOUND_TAG},
    ]}
    config_path.write_text(json.dumps(payload), encoding="utf-8")
    from fwrouter_api.services.subscription_profiles import filter_runtime_exportable_subscription_nodes

    nodes = [{"client_uuid": "uuid-active", "client_email": "active@example.test", "server_id": "server-1"}]
    assert filter_runtime_exportable_subscription_nodes(nodes) == nodes


class _FakeRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.compose_stdout = '[{"Service":"fwrouter-xray","State":"running"}]'
        self.loaded_users_stdout: str | None = None
        self.runtime_container_id_stdout = "a" * 64
        self.runtime_inspect_stdout = f"{'a' * 64}|true|2026-10-04T12:00:00.000000000Z"
        self.runtime_started_at = "2026-10-04T12:00:00.000000000Z"
        self.runtime_config_archive_override: bytes | None = None
        self.config_path: Path | None = None
        self.reload_count = 0
        self.test_result = XrayApplyResult(ok=True, message="test ok", details={"runner": "fake"})
        self.reload_result = XrayApplyResult(ok=True, message="reload ok", details={"runner": "fake"})

    def __call__(self, action: str, payload: dict[str, object]) -> XrayApplyResult:
        self.calls.append((action, dict(payload)))
        if action == "test_config":
            return self.test_result
        if action == "reload":
            if self.reload_result.ok:
                self.reload_count += 1
                minute = int(self.runtime_started_at[14:16]) + 1
                self.runtime_started_at = self.runtime_started_at[:14] + f"{minute:02d}" + self.runtime_started_at[16:]
                self.runtime_inspect_stdout = f"{'a' * 64}|true|{self.runtime_started_at}"
            return self.reload_result
        if action == "compose_ps":
            return XrayApplyResult(
                ok=True,
                message="compose ps ok",
                details={"stdout": self.compose_stdout, "runner": "fake"},
            )
        if action == "api_inbound_users":
            stdout = self.loaded_users_stdout
            if stdout is None and self.config_path is not None and self.config_path.exists():
                try:
                    config = json.loads(self.config_path.read_text(encoding="utf-8"))
                    clients = next((item.get("settings", {}).get("clients", []) for item in config.get("inbounds", [])
                                    if item.get("tag") == "vless-ws"), [])
                    stdout = json.dumps({"users": [
                        {"email": str(client.get("email") or ""),
                         "account": {"_TypedMessage_": "xray.proxy.vless.Account", "id": str(client.get("id") or "")}}
                        for client in clients if client.get("email") and client.get("id")
                    ]})
                except (OSError, ValueError, AttributeError):
                    stdout = '{"users": []}'
            return XrayApplyResult(
                ok=True, message="loaded users read", details={"stdout": stdout or ""},
            )
        if action == "runtime_container_id":
            return XrayApplyResult(
                ok=True, message="container id read", details={"stdout": self.runtime_container_id_stdout},
            )
        if action == "runtime_inspect":
            return XrayApplyResult(
                ok=True, message="runtime inspected", details={"stdout": self.runtime_inspect_stdout},
            )
        if action == "runtime_config_archive":
            archive = self.runtime_config_archive_override
            if archive is None and self.config_path is not None and self.config_path.exists():
                content = self.config_path.read_bytes()
                buffer = io.BytesIO()
                with tarfile.open(fileobj=buffer, mode="w") as tar:
                    member = tarfile.TarInfo("config.json")
                    member.size = len(content)
                    tar.addfile(member, io.BytesIO(content))
                archive = buffer.getvalue()
            return XrayApplyResult(
                ok=True, message="runtime config archive read", details={"archive_bytes": archive or b""},
            )
        raise AssertionError(action)


def _build_adapter(tmp_path: Path, *, runner: _FakeRunner | None = None) -> RealXrayAdapter:
    config_path, compose_path = _xray_paths()
    runner = runner or _FakeRunner()
    runner.config_path = config_path
    compose_path.parent.mkdir(parents=True, exist_ok=True)
    compose_path.write_text("services:\n  fwrouter-xray:\n    image: teddysun/xray\n", encoding="utf-8")
    return RealXrayAdapter(
        config_path=config_path,
        compose_path=compose_path,
        log_root=tmp_path / "log" / "xray",
        runner=runner,
    )


def _patch_xray_adapters(monkeypatch, adapter: RealXrayAdapter) -> None:
    monkeypatch.setattr(xray_adapter, "DEFAULT_XRAY_ADAPTER", adapter)
    monkeypatch.setattr(xray_service, "DEFAULT_XRAY_ADAPTER", adapter)
    monkeypatch.setattr(inventory_service, "DEFAULT_XRAY_ADAPTER", adapter)
    monkeypatch.setattr(runtime_service, "DEFAULT_XRAY_ADAPTER", adapter)
    monkeypatch.setattr(xray_runtime_state_service, "DEFAULT_XRAY_ADAPTER", adapter)


def _wait_for_job_result(client: TestClient, job_id: str, *, timeout_seconds: float = 3.0) -> dict[str, object]:
    deadline = time.monotonic() + timeout_seconds
    last: dict[str, object] | None = None
    while time.monotonic() < deadline:
        response = client.get(f"/api/v2/jobs/{job_id}")
        assert response.status_code == 200
        last = response.json()["data"]["job"]
        if str(last.get("status")) in {"success", "failed", "stale"}:
            return last
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish: {last}")


class _ReadyMihomoAdapter:
    def check_port(self, port: int, host: str = "127.0.0.1", timeout: float = 1.0) -> bool:
        return 1 <= int(port) <= 65535

    def health(self) -> MihomoHealth:
        return MihomoHealth(
            runtime_state=MihomoRuntimeState.RUNNING,
            message="mihomo ready",
            details={
                "adapter": "fake",
                "config": {
                    "tproxy_port": 5202,
                    "tun_enabled": True,
                },
                "selectors": {
                    "vpn_global_exists": True,
                    "vpn_global_targets_count": 5,
                    "vpn_global_has_vpn_auto": True,
                    "vpn_global_now": "vpn-auto",
                },
            },
        )

    def list_servers(self):  # noqa: ANN001
        return []


class _SuccessfulDataplaneAdapter:
    def check(self, plan):  # noqa: ANN001
        return DataplaneResult(
            ok=True,
            operation=DataplaneOperation.CHECK,
            message="check ok",
            details={
                "stage": "check",
                "owned_table": "inet fwrouter_v2",
                "table_exists": True,
                "required_chains": {
                    "prerouting": True,
                    "output": True,
                    "forward": True,
                    "postrouting": True,
                    "fwrouter_classify": True,
                    "fwrouter_direct": True,
                    "fwrouter_vpn": True,
                },
            },
        )

    def apply(self, plan):  # noqa: ANN001
        return DataplaneResult(
            ok=True,
            operation=DataplaneOperation.APPLY,
            message="apply ok",
            details={
                "stage": "verify",
                "owned_table": "inet fwrouter_v2",
                "table_exists": True,
                "routing_mode": "vpn",
                "vpn_contract_ready": True,
                "vpn_external_path_verified": True,
                "vpn_tproxy_port": 5202,
                "required_chains": {
                    "prerouting": True,
                    "output": True,
                    "forward": True,
                    "postrouting": True,
                    "fwrouter_classify": True,
                    "fwrouter_direct": True,
                    "fwrouter_vpn": True,
                },
            },
        )

    def rollback(self, plan):  # noqa: ANN001
        return DataplaneResult(
            ok=True,
            operation=DataplaneOperation.ROLLBACK,
            message="rollback ok",
            details={"stage": "rollback"},
        )


def _patch_runtime(monkeypatch) -> None:
    import fwrouter_api.adapters.mihomo as mihomo_adapter_module
    adapter = _SuccessfulDataplaneAdapter()
    monkeypatch.setattr(apply_service, "DEFAULT_DATAPLANE_ADAPTER", adapter)
    monkeypatch.setattr(runtime_service, "DEFAULT_DATAPLANE_ADAPTER", adapter)
    monkeypatch.setattr(dataplane_global_service, "DEFAULT_MIHOMO_ADAPTER", _ReadyMihomoAdapter())
    monkeypatch.setattr(mihomo_adapter_module, "DEFAULT_MIHOMO_ADAPTER", _ReadyMihomoAdapter())
    monkeypatch.setattr(runtime_service, "DEFAULT_MIHOMO_ADAPTER", _ReadyMihomoAdapter())
    monkeypatch.setattr(mihomo_config_service, "reconcile_mihomo_runtime", lambda *_args, **_kwargs: {"ok": True})
    monkeypatch.setattr(mihomo_config_service, "validate_mihomo_candidate_config", lambda **_kwargs: {"ok": True})
    import fwrouter_api.services.subscription_pipeline as subscription_pipeline_service
    import fwrouter_api.services.mihomo_runtime as mihomo_runtime_service
    mihomo_incarnation = {"value": 1}
    monkeypatch.setattr(
        mihomo_runtime_service, "get_mihomo_runtime_incarnation",
        lambda: f"pytest-mihomo-container:started-at-{mihomo_incarnation['value']}",
    )
    monkeypatch.setattr(subscription_pipeline_service, "validate_mihomo_candidate_config", lambda *_args, **_kwargs: {"ok": True})
    def fake_fenced_restart(**kwargs):
        from fwrouter_api.services.vpn_auto_selection_state import advance_selection_revision, read_selection_revision
        if kwargs.get("selection_fenced"):
            with db_session() as connection:
                current = read_selection_revision(connection)
            if current != kwargs.get("expected_selection_revision"):
                return {"ok": False, "action": "test", "error_code": "VPN_AUTO_SELECTION_REVISION_CONFLICT"}
            mihomo_incarnation["value"] += 1
            return {"ok": True, "action": "test"}
        with db_session() as connection:
            expected = read_selection_revision(connection)
            owned_revision = advance_selection_revision(connection, expected_revision=expected)
        mihomo_incarnation["value"] += 1
        return {"ok": owned_revision is not None, "action": "test", "selection_revision": owned_revision}

    monkeypatch.setattr(mihomo_runtime_service, "restart_mihomo_container", fake_fenced_restart)


def _seed_server(
    server_id: str,
    *,
    server_name: str | None = None,
    raw: dict[str, Any] | None = None,
) -> None:
    resolved_name = server_name or server_id
    raw_json = json.dumps(raw or {}, ensure_ascii=False, sort_keys=True)
    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO servers (
                server_id,
                server_name,
                provider_name,
                inventory_state,
                raw_json
            )
            VALUES (?, ?, 'provider', 'active', ?)
            """,
            (server_id, resolved_name, raw_json),
        )
        connection.execute(
            """
            INSERT INTO server_preferences (
                server_id,
                vpn_auto,
                global_list
            )
            VALUES (?, 1, 1)
            """,
            (server_id,),
        )
        connection.execute(
            "INSERT OR IGNORE INTO server_ping_state (server_id, status) VALUES (?, 'success')",
            (server_id,),
        )


def _seed_routing_state(*, desired_mode: str, active_auto_server_id: str | None = None) -> None:
    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO routing_global_state (
                id,
                desired_mode,
                applied_mode,
                selective_default,
                server_mode,
                active_auto_server_id,
                apply_state
            )
            VALUES (1, ?, ?, 'direct', 'auto', ?, 'clean')
            ON CONFLICT(id) DO UPDATE SET
                desired_mode = excluded.desired_mode,
                applied_mode = excluded.applied_mode,
                active_auto_server_id = excluded.active_auto_server_id,
                apply_state = 'clean',
                error_code = NULL,
                error_message = NULL,
                updated_at = CURRENT_TIMESTAMP
            """,
            (desired_mode, desired_mode, active_auto_server_id),
        )


def _seed_subscription_identity(*, slug: str, token: str, app_type: str = "auto") -> None:
    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO subscription_accounts (
                slug,
                display_name,
                enabled
            )
            VALUES (?, ?, 1)
            """,
            (slug, slug.title()),
        )
        account = connection.execute(
            "SELECT account_id FROM subscription_accounts WHERE slug = ? LIMIT 1",
            (slug,),
        ).fetchone()
        assert account is not None
        connection.execute(
            """
            INSERT INTO subscription_clients (
                account_id,
                token,
                app_type,
                enabled,
                display_name
            )
            VALUES (?, ?, ?, 1, ?)
            """,
            (account["account_id"], token, app_type, token.title()),
        )


def _enable_xray_module() -> None:
    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO modules (
                module_name,
                desired_state,
                lifecycle_mode,
                runtime_state,
                apply_state,
                status_text
            )
            VALUES ('xray', 'enabled', 'managed', 'running', 'clean', 'pytest xray ready')
            ON CONFLICT(module_name) DO UPDATE SET
                desired_state = 'enabled',
                lifecycle_mode = 'managed',
                runtime_state = 'running',
                apply_state = 'clean',
                error_code = NULL,
                error_message = NULL
            """
        )


def _database_snapshot() -> dict[str, list[dict[str, object]]]:
    with db_session() as connection:
        tables = [
            str(row["name"])
            for row in connection.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'table'
                  AND name NOT LIKE 'sqlite_%'
                ORDER BY name
                """
            ).fetchall()
        ]
        snapshot: dict[str, list[dict[str, object]]] = {}
        for table in tables:
            rows = [dict(row) for row in connection.execute(f'SELECT * FROM "{table}"').fetchall()]
            snapshot[table] = sorted(
                rows,
                key=lambda row: json.dumps(row, ensure_ascii=False, sort_keys=True, default=str),
            )
    return snapshot


def test_noop_xray_health_contract() -> None:
    adapter = NoopXrayAdapter()
    result = adapter.health()

    assert result.runtime_state == XrayRuntimeState.NOT_CONFIGURED
    assert result.details["adapter"] == "noop"


def test_parse_empty_config_clients_returns_empty(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [])
    adapter = _build_adapter(tmp_path)

    assert adapter.list_clients() == []


def test_health_missing_config(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    adapter = _build_adapter(tmp_path)
    result = adapter.health()

    assert result.runtime_state == XrayRuntimeState.NOT_CONFIGURED
    assert result.details["config_path"].replace("\\", "/").endswith("xray/config.json")
    assert result.details["forced_vpn_ready"] is False
    assert result.details["traffic_available"] is False


def test_health_valid_config_reports_forced_vpn_not_ready(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [{"id": "uuid-a", "email": "alice@example.test"}])
    runner = _FakeRunner()
    adapter = _build_adapter(tmp_path, runner=runner)

    result = adapter.health()

    assert result.runtime_state == XrayRuntimeState.RUNNING
    assert result.message == "Xray runtime is up, but forced VPN dataplane is not enabled yet."
    assert result.details["public_host"] == "xray.example.test"
    assert result.details["public_port"] == 443
    assert result.details["public_path"] == "/vless"
    assert result.details["transport"] == "ws"
    assert result.details["clients_count"] == 1
    assert result.details["forced_vpn_ready"] is False
    assert result.details["traffic_available"] is False


def test_default_runner_uses_fwrouter_docker_cli_state(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    config_path, compose_path = _xray_paths()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    compose_path.parent.mkdir(parents=True, exist_ok=True)
    compose_path.write_text("services: {}\n", encoding="utf-8")
    docker_cli_state = tmp_path / "run" / "docker-cli"
    captured: dict[str, object] = {}

    def fake_run(command, **kwargs):  # noqa: ANN001, ANN003
        captured["command"] = command
        captured["env"] = kwargs.get("env")
        return xray_real.subprocess.CompletedProcess(
            command,
            0,
            stdout='[{"Service":"fwrouter-xray","State":"running"}]',
            stderr="",
        )

    monkeypatch.setattr(xray_real, "DOCKER_CLI_STATE_DIR", docker_cli_state)
    monkeypatch.setattr(xray_real.subprocess, "run", fake_run)
    adapter = RealXrayAdapter(config_path=config_path, compose_path=compose_path)

    result = adapter._default_runner("compose_ps", {})

    assert result.ok is True
    assert docker_cli_state.exists()
    env = captured["env"]
    assert isinstance(env, dict)
    assert env["DOCKER_CONFIG"] == str(docker_cli_state)
    assert env["HOME"] == str(docker_cli_state)


def test_create_client_adds_uuid_to_config(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [])
    runner = _FakeRunner()
    adapter = _build_adapter(tmp_path, runner=runner)

    result = adapter.create_client(alias="Alice")
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    clients = payload["inbounds"][0]["settings"]["clients"]

    assert result.ok is True
    assert len(clients) == 1
    assert clients[0]["id"] == result.details["client"]["client_uuid"]
    assert clients[0]["email"] == "alice@fwrouter.local"


def test_xray_create_subscription_identity_is_not_persisted_in_event_logs(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    sentinel = "secret-link-token-2026@example.test"
    monkeypatch.setattr(xray_clients_service, "_xray_managed_runtime_blocked", lambda operation: None)
    monkeypatch.setattr(
        xray_clients_service,
        "_xray_client_create_preflight",
        lambda **kwargs: {"ok": True, "message": "ready", "code": None},
    )

    class _Adapter:
        def create_client(self, *, alias, email):
            return XrayApplyResult(
                ok=True,
                message="Created.",
                details={"client": {"client_id": "client-safe", "client_uuid": "uuid-safe", "email": email, "enabled": True}},
            )

    monkeypatch.setattr(xray_clients_service, "_xray_adapter", lambda: _Adapter())
    monkeypatch.setattr(xray_clients_service, "_sync_xray_inventory", lambda requested_by: None)
    monkeypatch.setattr(xray_clients_service, "_set_local_alias", lambda client_id, alias: None)
    monkeypatch.setattr(xray_clients_service, "ensure_subscription_identity", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "fwrouter_api.services.xray_subscription_service.reconcile_xray_subscription_profile_nodes",
        lambda **kwargs: {"ok": True},
    )
    monkeypatch.setattr(
        xray_clients_service,
        "verify_xray_client_runtime_convergence",
        lambda **kwargs: {"ok": True},
    )
    monkeypatch.setattr(
        "fwrouter_api.services.xray_subscription_service.export_xray_subscription",
        lambda client_id: {"ok": True, "subscription_uri": "vless://private@example.test?token=secret"},
    )

    payload = xray_clients_service.create_xray_client(alias="Sentinel", email=sentinel)

    assert payload["ok"] is True
    assert payload["client"]["email"] == sentinel
    with db_session() as connection:
        rows = connection.execute(
            "SELECT details_json FROM operational_logs WHERE event_type IN ('xray_client_created', 'external_client.created')"
        ).fetchall()
    persisted = "\n".join(row["details_json"] or "" for row in rows)
    persisted += get_settings().paths.operational_events_path.read_text(encoding="utf-8")
    assert sentinel not in persisted
    assert "secret-link-token-2026" not in persisted


def test_create_client_preserves_existing_clients(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    config_path, _ = _xray_paths()
    _write_xray_config(
        config_path,
        [{"id": "uuid-existing", "email": "existing@example.test"}],
    )
    adapter = _build_adapter(tmp_path, runner=_FakeRunner())

    adapter.create_client(alias="Bob")
    clients = json.loads(config_path.read_text(encoding="utf-8"))["inbounds"][0]["settings"]["clients"]

    assert len(clients) == 2
    assert clients[0]["id"] == "uuid-existing"


def test_create_client_writes_atomically(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [])
    runner = _FakeRunner()
    adapter = _build_adapter(tmp_path, runner=runner)
    writes: list[Path] = []
    original_atomic_write = xray_adapter.atomic_write_text

    def _record_atomic_write(path: Path, text: str) -> None:
        writes.append(path)
        original_atomic_write(path, text)

    monkeypatch.setattr(xray_adapter, "atomic_write_text", _record_atomic_write)

    result = adapter.create_client(alias="Atomic")

    assert result.ok is True
    assert config_path in writes
    assert config_path.with_name("config.json.candidate") in writes


def test_duplicate_client_email_is_rejected(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [{"id": "uuid-one", "email": "dup@example.test"}])
    adapter = _build_adapter(tmp_path, runner=_FakeRunner())

    try:
        adapter.create_client(email="dup@example.test")
    except xray_adapter.XrayAdapterError as exc:
        assert exc.code == "XRAY_DUPLICATE_EMAIL"
    else:  # pragma: no cover - safety net
        raise AssertionError("Expected XRAY_DUPLICATE_EMAIL")


def test_delete_client_removes_only_selected(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    config_path, _ = _xray_paths()
    _write_xray_config(
        config_path,
        [
            {"id": "uuid-one", "email": "one@example.test"},
            {"id": "uuid-two", "email": "two@example.test"},
        ],
    )
    adapter = _build_adapter(tmp_path, runner=_FakeRunner())

    result = adapter.delete_client("uuid-one")
    clients = json.loads(config_path.read_text(encoding="utf-8"))["inbounds"][0]["settings"]["clients"]

    assert result.ok is True
    assert [client["id"] for client in clients] == ["uuid-two"]


def test_delete_xray_client_tombstones_stale_local_subject(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO subjects (
                subject_id,
                subject_type,
                subject_role,
                implementation_kind,
                stable_key,
                display_name,
                alias,
                desired_mode,
                runtime_state,
                is_active,
                is_deleted
            )
            VALUES (
                'xray:stale-client',
                'explicit_external_client',
                'vless_client',
                'xray',
                'xray:stale-client',
                'portal',
                'portal',
                'enabled',
                'inactive',
                0,
                0
            )
            """
        )
        connection.execute(
            """
            UPDATE subjects
            SET metadata_json = json(?)
            WHERE subject_id = 'xray:stale-client'
            """,
            (
                json.dumps(
                    {
                        "provider": "xray",
                        "detail": {
                            "client_id": "stale-client",
                            "client_uuid": "stale-client",
                            "email": "portal@fwrouter.local",
                            "enabled": False,
                        },
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            ),
        )
        connection.execute(
            """
            INSERT INTO servers (server_id, server_name, provider_name, inventory_state, raw_json)
            VALUES ('server-1', 'server-1', 'provider', 'active', '{}')
            """
        )
        connection.execute(
            """
            INSERT INTO subject_server_overrides (
                subject_id,
                selected_server_id,
                selected_until,
                apply_state
            )
            VALUES ('xray:stale-client', 'server-1', '2099-12-31 23:59:59', 'clean')
            """
        )

    class _MissingRuntimeXrayAdapter:
        def delete_client(self, client_id: str) -> XrayApplyResult:
            raise XrayAdapterError(
                "XRAY_CLIENT_NOT_FOUND",
                f"Xray client not found: {client_id}",
                details={"client_id": client_id},
            )

        def list_clients(self):
            return []

    adapter = _MissingRuntimeXrayAdapter()
    monkeypatch.setattr(xray_service, "DEFAULT_XRAY_ADAPTER", adapter)
    monkeypatch.setattr(inventory_service, "DEFAULT_XRAY_ADAPTER", adapter)
    monkeypatch.setattr(
        xray_service,
        "materialize_xray_runtime_bindings",
        lambda **_kwargs: {"ok": True},
    )

    result = xray_service.delete_xray_client("stale-client", requested_by="pytest")

    assert result["ok"] is True
    assert result["stage"] == "local_inventory"
    assert result["result"]["details"]["client"]["display_name"] == "portal"

    cleanup = result["cleanup"]
    assert cleanup["subjects_deleted"] == 1
    assert cleanup["server_overrides_deleted"] == 1

    with db_session() as connection:
        row = connection.execute(
            "SELECT subject_id FROM subjects WHERE subject_id = ?",
            ("xray:stale-client",),
        ).fetchone()
        override = connection.execute(
            "SELECT subject_id FROM subject_server_overrides WHERE subject_id = ?",
            ("xray:stale-client",),
        ).fetchone()

    assert row is None
    assert override is None


def test_config_test_failure_returns_structured_error_and_does_not_reload(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    config_path, _ = _xray_paths()
    original_text = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    _write_xray_config(config_path, [])
    original_text = config_path.read_text(encoding="utf-8")
    runner = _FakeRunner()
    runner.test_result = XrayApplyResult(
        ok=False,
        message="test failed",
        error_code="XRAY_CONFIG_TEST_FAILED",
        details={"stderr": "bad config"},
    )
    adapter = _build_adapter(tmp_path, runner=runner)

    result = adapter.create_client(alias="Broken")

    assert result.ok is False
    assert result.error_code == "XRAY_CONFIG_TEST_FAILED"
    assert [call[0] for call in runner.calls] == ["test_config"]
    assert config_path.read_text(encoding="utf-8") == original_text


def test_reload_failure_after_config_save_does_not_rollback(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [])
    runner = _FakeRunner()
    runner.reload_result = XrayApplyResult(
        ok=False,
        message="reload failed",
        error_code="XRAY_RELOAD_FAILED",
        details={"stderr": "compose failed"},
    )
    adapter = _build_adapter(tmp_path, runner=runner)

    result = adapter.create_client(alias="Retry")
    clients = json.loads(config_path.read_text(encoding="utf-8"))["inbounds"][0]["settings"]["clients"]

    assert result.ok is False
    assert result.error_code == "XRAY_RELOAD_FAILED"
    assert len(clients) == 1
    assert clients[0]["email"] == "retry@fwrouter.local"
    assert [call[0] for call in runner.calls] == ["test_config", "reload"]


def test_alias_update_does_not_change_uuid(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [{"id": "uuid-alias", "email": "alias@example.test"}])
    adapter = _build_adapter(tmp_path, runner=_FakeRunner())
    _patch_xray_adapters(monkeypatch, adapter)
    inventory_service.sync_subject_inventory(
        requested_by="pytest",
        discover_docker=False,
        discover_tailscale=False,
        discover_xray=True,
    )

    result = xray_service.update_xray_client_alias("uuid-alias", alias="Display Alias", requested_by="pytest")
    subject = get_subject("xray:uuid-alias")

    assert result["ok"] is True
    assert result["client"]["client_uuid"] == "uuid-alias"
    assert subject is not None
    assert subject["alias"] == "Display Alias"


def test_list_clients_feeds_subject_inventory_discover_xray(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    config_path, _ = _xray_paths()
    _write_xray_config(
        config_path,
        [
            {"id": "uuid-a", "email": "alice@example.test"},
            {"id": "uuid-b", "email": "bob@example.test", "enable": False},
        ],
    )
    adapter = _build_adapter(tmp_path, runner=_FakeRunner())
    _patch_xray_adapters(monkeypatch, adapter)

    result = inventory_service.sync_subject_inventory(
        requested_by="pytest",
        discover_docker=False,
        discover_tailscale=False,
        discover_xray=True,
    )
    alice = get_subject("xray:uuid-a")
    bob = get_subject("xray:uuid-b")

    assert result["sources"]["xray"]["clients_count"] == 2
    assert alice is not None and alice["is_active"] == 1
    assert bob is not None and bob["is_active"] == 0
    assert alice["detail"]["subscription_path"].endswith("/uuid-a/subscription")


def test_subscription_uri_contains_expected_public_parameters(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [{"id": "uuid-sub", "email": "sub@example.test"}])
    adapter = _build_adapter(tmp_path, runner=_FakeRunner())

    result = adapter.export_vless_subscription("uuid-sub")
    uri = result.details["subscription_uri"]

    assert result.ok is True
    assert uri.startswith("vless://uuid-sub@xray.example.test:443")
    assert "encryption=none" in uri
    assert "security=tls" in uri
    assert "sni=xray.example.test" in uri
    assert "type=ws" in uri
    assert "host=xray.example.test" in uri
    assert "path=%2Fvless" in uri
    assert "alpn=http%2F1.1" in uri
    assert "fp=chrome" in uri
    assert "packetEncoding=xudp" in uri


def test_export_xray_subscription_text_contains_alpn_and_fp(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [{"id": "uuid-text", "email": "text@example.test"}])
    adapter = _build_adapter(tmp_path, runner=_FakeRunner())
    _patch_xray_adapters(monkeypatch, adapter)

    exported = xray_service.export_xray_subscription_text("uuid-text", base64_encode=False)

    assert exported["ok"] is True
    assert exported["content"].startswith("vless://uuid-text@xray.example.test:443")
    assert "alpn=http%2F1.1" in exported["content"]
    assert "fp=chrome" in exported["content"]
    assert "packetEncoding=xudp" in exported["content"]


def test_export_xray_subscription_includes_fwrouter_binding_context(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _patch_runtime(monkeypatch)
    monkeypatch.setattr(
        subject_policy_service,
        "build_runtime_enforcement_state",
        lambda: {
            "supported_modes": {"direct": True, "selective": False, "vpn": True},
            "enforcement_level": "global_vpn_enforced",
            "traffic_enforcement_guaranteed": True,
        },
    )
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [{"id": "uuid-export", "email": "export@example.test"}])
    adapter = _build_adapter(tmp_path, runner=_FakeRunner())
    _patch_xray_adapters(monkeypatch, adapter)
    inventory_service.sync_subject_inventory(
        requested_by="pytest",
        discover_docker=False,
        discover_tailscale=False,
        discover_xray=True,
    )
    _seed_server("server-1")
    _seed_routing_state(desired_mode="vpn", active_auto_server_id="server-1")

    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO subject_server_overrides (
                subject_id,
                selected_server_id,
                selected_until,
                apply_state
            )
            VALUES (?, ?, datetime('now', '+24 hours'), 'pending')
            """,
            ("xray:uuid-export", "server-1"),
        )

    exported = xray_service.export_xray_subscription("uuid-export")

    assert exported["ok"] is True
    assert exported["subject_id"] == "xray:uuid-export"


def test_subscription_profile_reconcile_restores_legacy_runtime_bindings(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    register_extended_handlers(get_default_job_manager())
    _patch_runtime(monkeypatch)
    monkeypatch.setattr(
        subject_policy_service,
        "build_runtime_enforcement_state",
        lambda: {
            "supported_modes": {"direct": True, "selective": False, "vpn": True},
            "enforcement_level": "global_vpn_enforced",
            "traffic_enforcement_guaranteed": True,
        },
    )
    _enable_xray_module()
    _seed_subscription_identity(slug="legacy", token="legacy")
    _seed_server("server-1")
    _seed_routing_state(desired_mode="vpn", active_auto_server_id=None)
    desired = list_desired_subscription_xray_clients("legacy")
    server_node = next(node for node in desired if node["server_id"] == "server-1")
    stale_email = "sub-legacy-stale@fwrouter.local"
    config_path, _ = _xray_paths()
    _write_xray_config(
        config_path,
        [
            {
                "id": server_node["client_uuid"],
                "email": server_node["client_email"],
            },
            {
                "id": "stale-runtime-client",
                "email": stale_email,
            },
        ],
    )
    adapter = _build_adapter(tmp_path, runner=_FakeRunner())
    _patch_xray_adapters(monkeypatch, adapter)

    result = xray_service.reconcile_xray_subscription_profile_nodes(requested_by="pytest")

    assert result["ok"] is True
    assert result["nodes_count"] == len(desired)
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    clients = payload["inbounds"][0]["settings"]["clients"]
    emails = [client.get("email") for client in clients]
    assert stale_email not in emails
    assert server_node["client_email"] in emails
    restored_client = next(client for client in clients if client.get("email") == server_node["client_email"])
    assert restored_client["fwrouterBinding"]["subject_id"] == f"xray:{server_node['client_uuid']}"
    rules = payload["routing"]["rules"]
    assert any(
        rule.get("outboundTag", "").startswith("fwrouter-egress-")
        and server_node["client_email"] in rule.get("user", [])
        and "vless-ws" in rule.get("inboundTag", [])
        for rule in rules
    )
    assert not any(
        rule.get("outboundTag") == "fwrouter-api"
        and server_node["client_email"] in rule.get("user", [])
        for rule in rules
    )
    rendered = render_subscription_profile("legacy", user_agent=None, requested_format="raw-vless")
    assert rendered["ok"] is True
    assert rendered["nodes_count"] == len(desired)
    assert server_node["client_uuid"] in rendered["content"]


def test_subscription_profile_reconcile_is_idempotent(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _patch_runtime(monkeypatch)
    monkeypatch.setattr(
        subject_policy_service,
        "build_runtime_enforcement_state",
        lambda: {
            "supported_modes": {"direct": True, "selective": False, "vpn": True},
            "enforcement_level": "global_vpn_enforced",
            "traffic_enforcement_guaranteed": True,
        },
    )
    _enable_xray_module()
    _seed_subscription_identity(slug="repeat", token="repeat")
    _seed_server("server-1")
    _seed_routing_state(desired_mode="vpn", active_auto_server_id=None)
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [])
    adapter = _build_adapter(tmp_path, runner=_FakeRunner())
    _patch_xray_adapters(monkeypatch, adapter)

    first = xray_service.reconcile_xray_subscription_profile_nodes(requested_by="pytest")
    second = xray_service.reconcile_xray_subscription_profile_nodes(requested_by="pytest")

    assert first["ok"] is True
    assert second["ok"] is True
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    clients = payload["inbounds"][0]["settings"]["clients"]
    emails = [client.get("email") for client in clients]
    assert len(emails) == len(set(emails))
    rules = [
        (
            tuple(rule.get("inboundTag", [])),
            tuple(rule.get("user", [])),
            rule.get("outboundTag"),
        )
        for rule in payload["routing"]["rules"]
        if rule.get("user")
    ]
    assert len(rules) == len(set(rules))


def test_failed_new_generation_restores_last_good_runtime_and_public_snapshot(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _patch_runtime(monkeypatch)
    monkeypatch.setattr(
        subject_policy_service,
        "build_runtime_enforcement_state",
        lambda: {
            "supported_modes": {"direct": True, "selective": False, "vpn": True},
            "enforcement_level": "global_vpn_enforced",
            "traffic_enforcement_guaranteed": True,
        },
    )
    _enable_xray_module()
    _seed_subscription_identity(slug="last-good", token="last-good")
    _seed_server("server-1")
    _seed_routing_state(desired_mode="vpn", active_auto_server_id=None)
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [])
    adapter = _build_adapter(tmp_path, runner=_FakeRunner())
    _patch_xray_adapters(monkeypatch, adapter)

    first = xray_service.reconcile_xray_subscription_profile_nodes(requested_by="pytest")
    assert first["ok"] is True
    good_config = config_path.read_bytes()
    from fwrouter_api.services.xray_runtime_state import _xray_bindings_path
    bindings_path = _xray_bindings_path()
    good_bindings = bindings_path.read_bytes()
    with db_session() as connection:
        good_snapshots = [dict(row) for row in connection.execute(
            "SELECT token, nodes_json, runtime_verified_at, updated_at FROM subscription_profile_snapshots ORDER BY token"
        ).fetchall()]

    _seed_subscription_identity(slug="new-generation", token="new-generation")
    apply_candidate = xray_subscription_service._apply_staged_mihomo_candidate
    def fail_final_candidate(candidate_path: str, expected_sha256: str):
        if "final" in Path(candidate_path).name:
            return {"ok": False, "stage": "test_failure", "error_code": "INJECTED_FINAL_APPLY_FAILURE"}
        return apply_candidate(candidate_path, expected_sha256)
    monkeypatch.setattr(xray_subscription_service, "_apply_staged_mihomo_candidate", fail_final_candidate)

    failed = xray_service.reconcile_xray_subscription_profile_nodes(requested_by="pytest")

    assert failed["ok"] is False
    assert failed["stage"] == "mihomo_final_apply"
    assert config_path.read_bytes() == good_config
    assert bindings_path.read_bytes() == good_bindings
    with db_session() as connection:
        restored_snapshots = [dict(row) for row in connection.execute(
            "SELECT token, nodes_json, runtime_verified_at, updated_at FROM subscription_profile_snapshots ORDER BY token"
        ).fetchall()]
    assert restored_snapshots == good_snapshots
    assert not xray_subscription_service._xray_generation_checkpoint_path(adapter).exists(), json.dumps(failed, indent=2)


def test_failed_generation_after_inventory_sync_restores_scoped_derived_rows(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _patch_runtime(monkeypatch)
    monkeypatch.setattr(
        subject_policy_service,
        "build_runtime_enforcement_state",
        lambda: {
            "supported_modes": {"direct": True, "selective": False, "vpn": True},
            "enforcement_level": "global_vpn_enforced",
            "traffic_enforcement_guaranteed": True,
        },
    )
    _enable_xray_module()
    _seed_subscription_identity(slug="derived-base", token="derived-base")
    _seed_server("server-1")
    _seed_routing_state(desired_mode="vpn", active_auto_server_id=None)
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [])
    adapter = _build_adapter(tmp_path, runner=_FakeRunner())
    _patch_xray_adapters(monkeypatch, adapter)
    first = xray_service.reconcile_xray_subscription_profile_nodes(requested_by="pytest")
    assert first["ok"] is True

    original_node = list_desired_subscription_xray_clients("derived-base")[0]
    with db_session() as connection:
        subject = connection.execute(
            "SELECT subject_id FROM subjects WHERE json_extract(metadata_json, '$.detail.client_uuid') = ? LIMIT 1",
            (original_node["client_uuid"],),
        ).fetchone()
        assert subject is not None
        connection.execute(
            "INSERT INTO subject_user_overrides (subject_id, override_mode, created_by) VALUES (?, 'vpn', 'pytest-user-intent')",
            (subject["subject_id"],),
        )
    _seed_subscription_identity(slug="derived-added", token="derived-added")
    with db_session() as connection:
        before_subjects = [dict(row) for row in connection.execute(
            "SELECT * FROM subjects WHERE implementation_kind = 'xray' ORDER BY subject_id"
        ).fetchall()]
        before_overrides = [dict(row) for row in connection.execute(
            "SELECT * FROM subject_server_overrides WHERE subject_id IN (SELECT subject_id FROM subjects WHERE implementation_kind = 'xray') ORDER BY subject_id"
        ).fetchall()]
        before_user_overrides = [dict(row) for row in connection.execute(
            "SELECT * FROM subject_user_overrides WHERE subject_id IN (SELECT subject_id FROM subjects WHERE implementation_kind = 'xray') ORDER BY subject_id"
        ).fetchall()]
    monkeypatch.setattr(
        xray_subscription_service,
        "_materialize_xray_runtime_bindings",
        lambda **_kwargs: {"ok": False, "stage": "injected_readback_failure"},
    )
    failed = xray_service.reconcile_xray_subscription_profile_nodes(requested_by="pytest")

    assert failed["ok"] is False
    assert failed["stage"] == "materialize"
    with db_session() as connection:
        after_subjects = [dict(row) for row in connection.execute(
            "SELECT * FROM subjects WHERE implementation_kind = 'xray' ORDER BY subject_id"
        ).fetchall()]
        after_overrides = [dict(row) for row in connection.execute(
            "SELECT * FROM subject_server_overrides WHERE subject_id IN (SELECT subject_id FROM subjects WHERE implementation_kind = 'xray') ORDER BY subject_id"
        ).fetchall()]
        after_user_overrides = [dict(row) for row in connection.execute(
            "SELECT * FROM subject_user_overrides WHERE subject_id IN (SELECT subject_id FROM subjects WHERE implementation_kind = 'xray') ORDER BY subject_id"
        ).fetchall()]
    assert after_subjects == before_subjects
    assert after_overrides == before_overrides
    assert after_user_overrides == before_user_overrides
    assert not xray_subscription_service._xray_generation_checkpoint_path(adapter).exists()


def test_generation_rollback_accepts_only_unchanged_nullable_auto_state(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    from fwrouter_api.services.servers import ensure_routing_global_state
    ensure_routing_global_state()
    before = xray_subscription_service._capture_generation_auto_selection()
    assert before["routing"]["active_auto_server_id"] is None
    assert xray_subscription_service._restore_generation_auto_selection(
        before=before, after=before, operation_id="pytest-generation",
    ) is True

    with db_session() as connection:
        from fwrouter_api.services.vpn_auto_selection_state import advance_selection_revision
        expected = advance_selection_revision(connection)
    assert expected == before["selection_revision"] + 1
    assert xray_subscription_service._restore_generation_auto_selection(
        before=before, after=before, operation_id="pytest-generation",
    ) is False


def test_stale_generation_checkpoint_declines_before_artifact_write_or_reload(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _patch_runtime(monkeypatch)
    _enable_xray_module()
    adapter = _build_adapter(tmp_path, runner=_FakeRunner())
    _patch_xray_adapters(monkeypatch, adapter)
    xray_path, _ = _xray_paths()
    _write_xray_config(xray_path, [])
    from fwrouter_api.services import mihomo_config
    mihomo_path = Path(mihomo_config._resolved_base_config_path())
    mihomo_path.parent.mkdir(parents=True, exist_ok=True)
    mihomo_path.write_text("old-generation", encoding="utf-8")
    checkpoint = xray_subscription_service._write_xray_generation_checkpoint(
        adapter=adapter, generation_id="stale-fixture", tokens=set(), phase="xray_applied",
        source_fingerprint=xray_subscription_service._generation_source_fingerprint(),
        staged_generation={"native_validation": {"xray": {"expected_client_identities": []}}},
        managed_email_prefixes=["sub-"],
    )
    mihomo_path.write_text("newer-generation", encoding="utf-8")
    with db_session() as connection:
        from fwrouter_api.services.vpn_auto_selection_state import advance_selection_revision
        advance_selection_revision(connection)
    reloads: list[bool] = []
    monkeypatch.setattr(adapter, "reload", lambda: reloads.append(True))
    xray_before = xray_path.read_bytes()
    mihomo_before = mihomo_path.read_bytes()

    restored = xray_subscription_service._restore_xray_generation_checkpoint(adapter, checkpoint)

    assert restored["ok"] is False
    assert restored["recovered"] == "stale_checkpoint_declined"
    assert restored["reason"] == "selection_fence_changed"
    assert xray_path.read_bytes() == xray_before
    assert mihomo_path.read_bytes() == mihomo_before
    assert reloads == []


def test_owned_restore_revision_change_during_native_validation_prevents_active_mutations(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _patch_runtime(monkeypatch)
    _enable_xray_module()
    adapter = _build_adapter(tmp_path, runner=_FakeRunner())
    _patch_xray_adapters(monkeypatch, adapter)
    from fwrouter_api.services import mihomo_config
    from fwrouter_api.services.xray_runtime_state import _xray_bindings_path
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [])
    mihomo_path = Path(mihomo_config._resolved_base_config_path())
    mihomo_path.parent.mkdir(parents=True, exist_ok=True)
    mihomo_path.write_text("current-mihomo", encoding="utf-8")
    bindings_path = _xray_bindings_path()
    bindings_path.parent.mkdir(parents=True, exist_ok=True)
    bindings_path.write_text(json.dumps({"bindings": [], "client_modes": [], "handoff_listeners": []}), encoding="utf-8")
    checkpoint = xray_subscription_service._write_xray_generation_checkpoint(
        adapter=adapter, generation_id="owned-cas-fixture", tokens=set(), phase="prepared",
        source_fingerprint=xray_subscription_service._generation_source_fingerprint(),
        staged_generation={"native_validation": {"xray": {"expected_client_identities": []}}},
        managed_email_prefixes=["sub-"],
    )
    active_before = {
        "xray": config_path.read_bytes(), "mihomo": mihomo_path.read_bytes(),
        "bindings": bindings_path.read_bytes(),
    }
    reloads: list[bool] = []
    original_test = adapter.test_config

    def bump_revision_after_native_validation(path: str):
        result = original_test(path)
        with db_session() as connection:
            from fwrouter_api.services.vpn_auto_selection_state import advance_selection_revision
            advance_selection_revision(connection)
        return result

    monkeypatch.setattr(adapter, "test_config", bump_revision_after_native_validation)
    monkeypatch.setattr(adapter, "reload", lambda: reloads.append(True))

    result = xray_subscription_service._restore_xray_generation_checkpoint(adapter, checkpoint)

    assert result["ok"] is False
    assert result["reason"] == "selection_fence_changed"
    assert config_path.read_bytes() == active_before["xray"]
    assert mihomo_path.read_bytes() == active_before["mihomo"]
    assert bindings_path.read_bytes() == active_before["bindings"]
    assert reloads == []
    assert checkpoint.exists()


@pytest.mark.parametrize("drift", ["snapshot", "fence"])
def test_owned_restore_revalidates_after_reload_before_database_restore(monkeypatch, tmp_path: Path, drift: str) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _patch_runtime(monkeypatch)
    _enable_xray_module()
    _seed_subscription_identity(slug="restore-race", token="restore-race")
    adapter = _build_adapter(tmp_path, runner=_FakeRunner())
    _patch_xray_adapters(monkeypatch, adapter)
    from fwrouter_api.services import mihomo_config
    from fwrouter_api.services.xray_runtime_state import _xray_bindings_path
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [])
    mihomo_path = Path(mihomo_config._resolved_base_config_path())
    mihomo_path.parent.mkdir(parents=True, exist_ok=True)
    mihomo_path.write_text("current-mihomo", encoding="utf-8")
    bindings_path = _xray_bindings_path()
    bindings_path.parent.mkdir(parents=True, exist_ok=True)
    bindings_path.write_text(json.dumps({"bindings": [], "client_modes": [], "handoff_listeners": []}), encoding="utf-8")
    with db_session() as connection:
        connection.execute(
            "INSERT INTO subscription_profile_snapshots (token, nodes_json, runtime_verified_at, updated_at) VALUES (?, '[]', 'verified', 'original')",
            ("restore-race",),
        )
    checkpoint = xray_subscription_service._write_xray_generation_checkpoint(
        adapter=adapter, generation_id=f"restore-race-{drift}", tokens={"restore-race"}, phase="prepared",
        source_fingerprint=xray_subscription_service._generation_source_fingerprint(),
        staged_generation={"native_validation": {"xray": {"expected_client_identities": []}}},
        managed_email_prefixes=["sub-"],
    )
    with db_session() as connection:
        initial_revision = xray_subscription_service._capture_generation_auto_selection()["selection_revision"]
    original_reload = adapter.reload

    def reload_then_external_drift():
        result = original_reload()
        if drift == "snapshot":
            with db_session() as connection:
                connection.execute(
                    "UPDATE subscription_profile_snapshots SET nodes_json = '[{\"foreign\":true}]', updated_at = 'foreign' WHERE token = ?",
                    ("restore-race",),
                )
        else:
            with db_session() as connection:
                from fwrouter_api.services.vpn_auto_selection_state import advance_selection_revision
                advance_selection_revision(connection)
        return result

    monkeypatch.setattr(adapter, "reload", reload_then_external_drift)
    result = xray_subscription_service._restore_xray_generation_checkpoint(adapter, checkpoint)

    assert result["ok"] is False
    assert result["derived_rows_restored"] is False
    assert checkpoint.exists()
    with db_session() as connection:
        current_snapshot = connection.execute(
            "SELECT nodes_json, updated_at FROM subscription_profile_snapshots WHERE token = ?",
            ("restore-race",),
        ).fetchone()
        from fwrouter_api.services.vpn_auto_selection_state import read_selection_revision
        current_revision = read_selection_revision(connection)
    if drift == "snapshot":
        assert current_snapshot["nodes_json"] == '[{"foreign":true}]'
        assert current_snapshot["updated_at"] == "foreign"
    else:
        assert current_revision > initial_revision
        assert current_snapshot["nodes_json"] == "[]"
        assert current_snapshot["updated_at"] == "original"


def test_generation_recovery_keeps_concurrent_user_override_and_checkpoint(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _patch_runtime(monkeypatch)
    monkeypatch.setattr(
        subject_policy_service,
        "build_runtime_enforcement_state",
        lambda: {"supported_modes": {"direct": True, "selective": False, "vpn": True}, "enforcement_level": "global_vpn_enforced", "traffic_enforcement_guaranteed": True},
    )
    _enable_xray_module()
    _seed_subscription_identity(slug="cas-base", token="cas-base")
    _seed_server("server-1")
    _seed_routing_state(desired_mode="vpn", active_auto_server_id=None)
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [])
    adapter = _build_adapter(tmp_path, runner=_FakeRunner())
    _patch_xray_adapters(monkeypatch, adapter)
    assert xray_service.reconcile_xray_subscription_profile_nodes(requested_by="pytest")["ok"]
    node = list_desired_subscription_xray_clients("cas-base")[0]
    with db_session() as connection:
        subject = connection.execute(
            "SELECT subject_id FROM subjects WHERE json_extract(metadata_json, '$.detail.client_uuid') = ? LIMIT 1",
            (node["client_uuid"],),
        ).fetchone()
        assert subject is not None
        subject_id = str(subject["subject_id"])
        connection.execute(
            "INSERT INTO subject_user_overrides (subject_id, override_mode, created_by) VALUES (?, 'vpn', 'pytest-user')",
            (subject_id,),
        )
    _seed_subscription_identity(slug="cas-added", token="cas-added")

    def concurrent_intent_change(**_kwargs):
        with db_session() as connection:
            connection.execute(
                "UPDATE subject_user_overrides SET override_mode = 'direct', updated_at = CURRENT_TIMESTAMP WHERE subject_id = ?",
                (subject_id,),
            )
        return {"ok": False, "stage": "injected_readback_failure"}

    monkeypatch.setattr(xray_subscription_service, "_materialize_xray_runtime_bindings", concurrent_intent_change)
    failed = xray_service.reconcile_xray_subscription_profile_nodes(requested_by="pytest")

    assert failed["ok"] is False
    checkpoint = xray_subscription_service._xray_generation_checkpoint_path(adapter)
    assert checkpoint.exists()
    with db_session() as connection:
        override = connection.execute(
            "SELECT override_mode FROM subject_user_overrides WHERE subject_id = ?",
            (subject_id,),
        ).fetchone()
    assert override["override_mode"] == "direct"


def test_subscription_profile_reconcile_does_not_resurrect_disabled_clients(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _patch_runtime(monkeypatch)
    _enable_xray_module()
    _seed_subscription_identity(slug="disabled", token="disabled")
    _seed_server("server-1")
    with db_session() as connection:
        connection.execute("UPDATE subscription_accounts SET enabled = 0 WHERE slug = 'disabled'")
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [{"id": "disabled-old", "email": "sub-disabled-old@fwrouter.local"}])
    adapter = _build_adapter(tmp_path, runner=_FakeRunner())
    _patch_xray_adapters(monkeypatch, adapter)

    result = xray_service.reconcile_xray_subscription_profile_nodes(requested_by="pytest")

    assert result["ok"] is True
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    assert payload["inbounds"][0]["settings"]["clients"] == []
    rendered = render_subscription_profile("disabled", user_agent=None, requested_format="raw-vless")
    assert rendered["ok"] is False
    assert rendered["error_code"] == "SUBSCRIPTION_CLIENT_DISABLED"


def test_vpn_auto_invalid_stage_keeps_database_and_active_xray_unchanged(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _patch_runtime(monkeypatch)
    _enable_xray_module()
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [{"id": "old-runtime", "email": "vpn-auto-old@fwrouter.local"}])
    adapter = _build_adapter(tmp_path, runner=_FakeRunner())
    _patch_xray_adapters(monkeypatch, adapter)
    before_db = _database_snapshot()
    before_config = config_path.read_bytes()
    staged_inputs: dict[str, object] = {}

    def reject_stage(**kwargs):
        staged_inputs.update(kwargs)
        return {"ok": False, "stage": "mihomo_final_candidate", "error_code": "MIHOMO_CANDIDATE_INVALID"}

    monkeypatch.setattr(xray_subscription_service, "_stage_profile_native_candidates", reject_stage)
    result = xray_service.reconcile_xray_vpn_auto_subscription(requested_by="pytest")

    assert result["ok"] is False
    assert result["error_code"] == "MIHOMO_CANDIDATE_INVALID"
    clients = staged_inputs["desired_clients"]
    assert any(str(client["email"]).startswith("vpn-auto-") for client in clients)
    assert before_config == config_path.read_bytes()
    assert before_db == _database_snapshot()


def test_public_subscription_profile_is_read_only_and_exportable_only(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _enable_xray_module()
    _seed_subscription_identity(slug="readonly", token="readonly")
    _seed_server("server-1")
    desired = list_desired_subscription_xray_clients("readonly")
    assert desired
    before = _database_snapshot()

    rendered = render_subscription_profile("readonly", user_agent=None, requested_format="raw-vless")

    assert rendered["ok"] is True
    assert rendered["nodes_count"] == 0
    assert rendered["content"] == ""
    assert _database_snapshot() == before


def test_public_vless_subscription_contains_verified_manual_custom_proxy(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _seed_subscription_identity(slug="manual-proxy", token="manual-proxy")
    _seed_server("custom-https:manual", server_name="Manual Proxy")
    with db_session() as connection:
        connection.execute(
            "UPDATE server_preferences SET vpn_auto_priority = -1 WHERE server_id = 'custom-https:manual'"
        )
        connection.execute(
            "INSERT INTO server_custom_https_proxy (server_id, proxy_type, host, port) VALUES ('custom-https:manual', 'socks5', 'proxy.example.test', 1080)"
        )

    desired = list_desired_subscription_xray_clients("manual-proxy")
    proxy_node = next(node for node in desired if node["server_id"] == "custom-https:manual")
    promote_runtime_verified_subscription_nodes(desired)
    rendered = render_subscription_profile("manual-proxy", user_agent=None, requested_format="raw-vless")

    assert rendered["ok"] is True
    assert any(node["server_id"] == "custom-https:manual" for node in rendered["nodes"])
    assert proxy_node["client_uuid"] in rendered["content"]


def test_public_subscription_uses_last_verified_snapshot_during_inventory_change(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _patch_runtime(monkeypatch)
    _enable_xray_module()
    _seed_subscription_identity(slug="atomic", token="atomic")
    _seed_server("server-old")
    _seed_routing_state(desired_mode="vpn", active_auto_server_id=None)
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [])
    adapter = _build_adapter(tmp_path, runner=_FakeRunner())
    _patch_xray_adapters(monkeypatch, adapter)

    converged = xray_service.reconcile_xray_subscription_profile_nodes(requested_by="pytest")
    assert converged["ok"] is True
    old_nodes = list_desired_subscription_xray_clients("atomic")
    assert old_nodes

    # Model a persisted refresh candidate before its runtime/profile promote.
    _seed_server("server-new")
    with db_session() as connection:
        connection.execute("UPDATE servers SET inventory_state = 'missing' WHERE server_id = 'server-old'")

    before = _database_snapshot()
    rendered = render_subscription_profile("atomic", user_agent=None, requested_format="raw-vless")

    assert rendered["ok"] is True
    assert rendered["nodes_count"] == len(old_nodes)
    assert old_nodes[0]["client_uuid"] in rendered["content"]
    assert _database_snapshot() == before


def test_failed_profile_materialization_filters_last_public_snapshot_to_active_identity_pairs(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _patch_runtime(monkeypatch)
    _enable_xray_module()
    _seed_subscription_identity(slug="preserved", token="preserved")
    _seed_server("server-old")
    _seed_routing_state(desired_mode="vpn", active_auto_server_id=None)
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [])
    adapter = _build_adapter(tmp_path, runner=_FakeRunner())
    _patch_xray_adapters(monkeypatch, adapter)
    assert xray_service.reconcile_xray_subscription_profile_nodes(requested_by="pytest")["ok"] is True
    old_nodes = list_desired_subscription_xray_clients("preserved")

    _seed_server("server-new")
    with db_session() as connection:
        connection.execute("UPDATE servers SET inventory_state = 'missing' WHERE server_id = 'server-old'")
    monkeypatch.setattr(
        xray_subscription_service,
        "_materialize_xray_runtime_bindings",
        lambda **_kwargs: {"ok": False, "status": "failed", "error": {"code": "XRAY_TEST_FAILURE"}},
    )

    failed = xray_service.reconcile_xray_subscription_profile_nodes(requested_by="pytest")
    from fwrouter_api.services.subscription_profiles import filter_runtime_exportable_subscription_nodes
    runtime_visible = filter_runtime_exportable_subscription_nodes(old_nodes)
    rendered = render_subscription_profile("preserved", user_agent=None, requested_format="raw-vless")

    assert failed["ok"] is False
    assert rendered["nodes_count"] == len(runtime_visible)
    assert all(node["client_uuid"] in rendered["content"] for node in runtime_visible)
    assert all(
        node["client_uuid"] not in rendered["content"]
        for node in old_nodes
        if node not in runtime_visible
    )


def test_xray_binding_reload_failure_restores_active_config(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [{"id": "uuid-old", "email": "old@example.test"}])
    original = config_path.read_text(encoding="utf-8")
    runner = _FakeRunner()
    runner.reload_result = XrayApplyResult(ok=False, message="reload failed", error_code="XRAY_RELOAD_FAILED")
    adapter = _build_adapter(tmp_path, runner=runner)

    result = adapter.materialize_client_bindings(
        [{
            "subject_id": "xray:uuid-old",
            "client_id": "uuid-old",
            "client_uuid": "uuid-old",
            "client_email": "old@example.test",
            "selected_server_id": "vpn-global",
            "selected_server_source": "vpn_auto",
            "status": "pending",
            "match_key": "xray-client-uuid:uuid-old",
        }]
    )

    assert result.ok is False
    assert config_path.read_text(encoding="utf-8") == original
    assert len([call for call in runner.calls if call[0] == "reload"]) == 2


def test_materialize_client_bindings_enables_xray_stats_api(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [{"id": "uuid-stats", "email": "stats@example.test"}])
    adapter = _build_adapter(tmp_path)

    result = adapter.materialize_client_bindings(
        [
            {
                "subject_id": "xray:uuid-stats",
                "client_id": "uuid-stats",
                "client_uuid": "uuid-stats",
                "client_email": "stats@example.test",
                "selected_server_id": "server-1",
                "selected_server_source": "vpn_auto",
                "status": "applied",
                "match_key": "xray-client-uuid:uuid-stats",
                "applied_at": "2026-06-02T00:00:00+00:00",
            }
        ]
    )

    assert result.ok is True

    payload = json.loads(config_path.read_text(encoding="utf-8"))
    assert payload["api"]["tag"] == "fwrouter-api"
    assert payload["api"]["services"] == ["StatsService", "HandlerService"]
    assert payload["stats"] == {}
    assert payload["policy"]["levels"]["0"]["statsUserUplink"] is True
    assert payload["policy"]["levels"]["0"]["statsUserDownlink"] is True
    assert "applied_at" not in payload["inbounds"][0]["settings"]["clients"][0]["fwrouterBinding"]

    api_inbound = next(inbound for inbound in payload["inbounds"] if inbound.get("tag") == "fwrouter-api")
    assert api_inbound["listen"] == "127.0.0.1"
    assert api_inbound["port"] == 10085
    assert api_inbound["protocol"] == "dokodemo-door"

    api_outbound = next(outbound for outbound in payload["outbounds"] if outbound.get("tag") == "fwrouter-api")
    assert api_outbound["protocol"] == "freedom"

    api_rule = next(rule for rule in payload["routing"]["rules"] if rule.get("outboundTag") == "fwrouter-api")
    assert api_rule["inboundTag"] == ["fwrouter-api"]


def test_loaded_xray_users_are_normalized_from_handler_service(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    runner = _FakeRunner()
    runner.loaded_users_stdout = json.dumps({"users": [{
        "email": "fixture@example.test",
        "account": {"_TypedMessage_": "xray.proxy.vless.Account", "id": "11111111-1111-4111-8111-111111111111"},
    }]})
    adapter = _build_adapter(tmp_path, runner=runner)

    assert adapter.list_loaded_client_identities() == [
        ("11111111-1111-4111-8111-111111111111", "fixture@example.test"),
    ]
    assert runner.calls == [("api_inbound_users", {"tag": "vless-ws"})]


def test_loaded_xray_users_readback_errors_do_not_return_native_output(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    runner = _FakeRunner()
    runner.loaded_users_stdout = '{"users":[{"email":"credential-like-secret"}]}'
    adapter = _build_adapter(tmp_path, runner=runner)

    with pytest.raises(XrayAdapterError) as captured:
        adapter.list_loaded_client_identities()

    assert captured.value.code == "XRAY_API_READBACK_INVALID_ACCOUNT"
    assert "credential-like-secret" not in str(captured.value)


def test_runtime_incarnation_requires_running_container_and_valid_started_at(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    runner = _FakeRunner()
    adapter = _build_adapter(tmp_path, runner=runner)

    incarnation = adapter.get_runtime_incarnation()
    assert len(incarnation) == 64
    assert runner.calls == [
        ("runtime_container_id", {}),
        ("runtime_inspect", {"container_id": "a" * 64}),
    ]

    runner.runtime_inspect_stdout = f"{'a' * 64}|false|2026-10-04T12:00:00Z"
    with pytest.raises(XrayAdapterError) as stopped:
        adapter.get_runtime_incarnation()
    assert stopped.value.code == "XRAY_RUNTIME_NOT_RUNNING"

    runner.runtime_inspect_stdout = f"{'a' * 64}|true|0001-01-01T00:00:00Z"
    with pytest.raises(XrayAdapterError) as invalid_started_at:
        adapter.get_runtime_incarnation()
    assert invalid_started_at.value.code == "XRAY_RUNTIME_NOT_RUNNING"


def test_runtime_config_digest_is_parsed_without_exposing_raw_output(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    runner = _FakeRunner()
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [{"id": "digest-user", "email": "digest@example.test"}])
    adapter = _build_adapter(tmp_path, runner=runner)

    assert adapter.get_runtime_config_sha256() == hashlib.sha256(adapter.config_path.read_bytes()).hexdigest()
    runner.runtime_config_archive_override = b"secret-bearing malformed archive"
    with pytest.raises(XrayAdapterError) as invalid:
        adapter.get_runtime_config_sha256()
    assert invalid.value.code == "XRAY_RUNTIME_CONFIG_DIGEST_UNAVAILABLE"
    assert "secret-bearing" not in str(invalid.value)


def test_materialize_client_bindings_skips_reload_when_config_unchanged(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [{"id": "uuid-stable", "email": "stable@example.test"}])
    runner = _FakeRunner()
    adapter = _build_adapter(tmp_path, runner=runner)
    bindings = [
        {
            "subject_id": "xray:uuid-stable",
            "client_id": "uuid-stable",
            "client_uuid": "uuid-stable",
            "client_email": "stable@example.test",
            "selected_server_id": "server-1",
            "selected_server_source": "vpn_auto",
            "status": "applied",
            "match_key": "xray-client-uuid:uuid-stable",
            "applied_at": "2026-06-02T00:00:00+00:00",
        }
    ]

    first = adapter.materialize_client_bindings(bindings)
    runner.calls.clear()
    second = adapter.materialize_client_bindings(bindings)

    assert first.ok is True
    assert second.ok is True
    assert second.details["stage"] == "unchanged"
    assert second.details["config_changed"] is False
    assert second.details["reload"] == {"skipped": True, "reason": "config_unchanged"}
    assert runner.calls == []


def test_materialize_xray_bindings_fails_when_scoped_rule_missing(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _patch_runtime(monkeypatch)
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [{"id": "uuid-missing-rule", "email": "missing@example.test"}])
    adapter = _build_adapter(tmp_path, runner=_FakeRunner())
    _patch_xray_adapters(monkeypatch, adapter)
    _seed_server("server-1")
    _seed_routing_state(desired_mode="vpn", active_auto_server_id="server-1")
    inventory_service.sync_subject_inventory(
        requested_by="pytest",
        discover_docker=False,
        discover_tailscale=False,
        discover_xray=True,
    )
    monkeypatch.setattr(
        adapter,
        "materialize_client_bindings",
        lambda bindings, client_modes=None, force_reload=False: XrayApplyResult(
            ok=True,
            message="claimed ok without changing config",
            details={"stage": "unchanged"},
        ),
    )

    result = xray_service.materialize_xray_runtime_bindings(
        requested_by="pytest",
        prepare_mihomo_handoff=False,
    )

    assert result["ok"] is False
    assert result["stage"] == "runtime_convergence"
    assert result["result"]["error_code"] == "XRAY_BINDINGS_CONVERGENCE_FAILED"
    assert (
        result["convergence"]["missing_outbounds"]
        or result["convergence"]["missing_rules"]
        or result["convergence"]["wrong_api_rules"]
    )


def test_xray_create_job_fails_when_effective_runtime_stays_stale(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _patch_runtime(monkeypatch)
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [])
    adapter = _build_adapter(tmp_path, runner=_FakeRunner())
    _patch_xray_adapters(monkeypatch, adapter)
    _seed_server("server-1")
    _seed_routing_state(desired_mode="vpn", active_auto_server_id="server-1")
    with db_session() as connection:
        connection.execute(
            "UPDATE modules SET desired_state = 'enabled', runtime_state = 'running' WHERE module_name = 'xray'"
        )
    monkeypatch.setattr(
        adapter,
        "materialize_client_bindings",
        lambda bindings, client_modes=None, force_reload=False: XrayApplyResult(
            ok=True,
            message="claimed ok without changing active config",
            details={"stage": "unchanged"},
        ),
    )

    result = xray_clients_service.run_xray_client_create_job(
        {
            "job_id": "job-stale-runtime",
            "requested_by": "pytest",
            "input": {"alias": "Stale", "requested_by": "pytest"},
        }
    )

    assert result["job_status"] == "failed"
    assert result["operation"] == "vless_client_create"
    assert result["stage"] == "runtime_convergence"
    assert result["error_code"] == "XRAY_BINDINGS_CONVERGENCE_FAILED"


def test_materialize_xray_bindings_fails_when_client_rule_points_to_fwrouter_api(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _patch_runtime(monkeypatch)
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [{"id": "uuid-api-rule", "email": "api-rule@example.test"}])
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    payload["inbounds"][0]["tag"] = "vless-ws"
    payload["api"] = {"tag": "fwrouter-api", "services": ["StatsService"]}
    payload["routing"] = {
        "rules": [
            {
                "type": "field",
                "inboundTag": ["vless-ws"],
                "user": ["api-rule@example.test"],
                "outboundTag": "fwrouter-api",
            }
        ]
    }
    payload["outbounds"] = [{"tag": "fwrouter-api", "protocol": "freedom"}]
    config_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    adapter = _build_adapter(tmp_path, runner=_FakeRunner())
    _patch_xray_adapters(monkeypatch, adapter)
    _seed_server("server-1")
    _seed_routing_state(desired_mode="vpn", active_auto_server_id="server-1")
    inventory_service.sync_subject_inventory(
        requested_by="pytest",
        discover_docker=False,
        discover_tailscale=False,
        discover_xray=True,
    )
    monkeypatch.setattr(
        adapter,
        "materialize_client_bindings",
        lambda bindings, client_modes=None, force_reload=False: XrayApplyResult(
            ok=True,
            message="claimed ok without changing active config",
            details={"stage": "unchanged"},
        ),
    )

    result = xray_service.materialize_xray_runtime_bindings(
        requested_by="pytest",
        prepare_mihomo_handoff=False,
    )

    assert result["ok"] is False
    assert result["stage"] == "runtime_convergence"
    assert result["convergence"]["wrong_api_rules"]


def test_xray_create_job_fails_when_mihomo_handoff_prepare_fails(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _patch_runtime(monkeypatch)
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [])
    adapter = _build_adapter(tmp_path, runner=_FakeRunner())
    _patch_xray_adapters(monkeypatch, adapter)
    _seed_server("server-1")
    _seed_routing_state(desired_mode="vpn", active_auto_server_id="server-1")
    with db_session() as connection:
        connection.execute(
            "UPDATE modules SET desired_state = 'enabled', runtime_state = 'running' WHERE module_name = 'xray'"
        )
    monkeypatch.setattr(
        mihomo_config_service,
        "reconcile_mihomo_runtime",
        lambda *_args, **_kwargs: {
            "ok": False,
            "error_code": "MIHOMO_HANDOFF_MISSING",
            "error_message": "missing handoff",
        },
    )

    result = xray_clients_service.run_xray_client_create_job(
        {
            "job_id": "job-mihomo-missing",
            "requested_by": "pytest",
            "input": {"alias": "No Handoff", "requested_by": "pytest"},
        }
    )

    assert result["job_status"] == "failed"
    assert result["stage"] == "mihomo_handoff_prepare"
    assert result["operation"] == "vless_client_create"


def test_xray_client_delete_removes_effective_runtime_binding_and_is_repeatable(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _patch_runtime(monkeypatch)
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [])
    adapter = _build_adapter(tmp_path, runner=_FakeRunner())
    _patch_xray_adapters(monkeypatch, adapter)
    _seed_server("server-1")
    _seed_routing_state(desired_mode="vpn", active_auto_server_id="server-1")
    with db_session() as connection:
        connection.execute(
            "UPDATE modules SET desired_state = 'enabled', runtime_state = 'running' WHERE module_name = 'xray'"
        )

    created = xray_service.create_xray_client(alias="Delete Me", requested_by="pytest")
    assert created["ok"] is True
    client_id = created["client"]["client_id"]
    deleted = xray_service.delete_xray_client(client_id, requested_by="pytest")
    deleted_again = xray_service.delete_xray_client(client_id, requested_by="pytest")

    assert deleted["ok"] is True
    assert deleted["operation"] == "vless_client_delete"
    assert deleted_again["ok"] is True
    assert deleted_again["stage"] == "noop"
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    clients = payload["inbounds"][0]["settings"]["clients"]
    assert all(client.get("id") != client_id for client in clients)
    assert not any(str(outbound.get("tag") or "").startswith("fwrouter-egress-") for outbound in payload["outbounds"])
    assert not any(
        client_id in {str(item) for item in (rule.get("user") or [])}
        for rule in payload.get("routing", {}).get("rules", [])
    )


def test_reconcile_clients_bulk_updates_managed_subscription_nodes(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    config_path, _ = _xray_paths()
    _write_xray_config(
        config_path,
        [
            {"id": "uuid-old", "email": "sub-token-stale@fwrouter.local"},
            {"id": "uuid-keep", "email": "regular@example.test"},
        ],
    )
    runner = _FakeRunner()
    adapter = _build_adapter(tmp_path, runner=runner)

    result = adapter.reconcile_clients(
        desired_clients=[
            {
                "client_uuid": "uuid-a",
                "email": "sub-token-a@fwrouter.local",
                "alias": "Token / A",
            },
            {
                "client_uuid": "uuid-b",
                "email": "sub-token-b@fwrouter.local",
                "alias": "Token / B",
            },
        ],
        managed_email_prefixes=["sub-token-"],
    )

    assert result.ok is True
    assert [call[0] for call in runner.calls] == ["test_config", "reload"]
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    clients = {
        client["email"]: client
        for client in payload["inbounds"][0]["settings"]["clients"]
    }
    assert "regular@example.test" in clients
    assert "sub-token-stale@fwrouter.local" not in clients
    assert clients["sub-token-a@fwrouter.local"]["id"] == "uuid-a"
    assert clients["sub-token-a@fwrouter.local"]["fwrouterAlias"] == "Token / A"
    assert clients["sub-token-b@fwrouter.local"]["id"] == "uuid-b"
    assert result.details["config_changed"] is True
    assert len(result.details["created"]) == 2
    assert len(result.details["deleted"]) == 1


def test_route_smoke_through_testclient(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _patch_runtime(monkeypatch)
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [])
    adapter = _build_adapter(tmp_path, runner=_FakeRunner())
    _patch_xray_adapters(monkeypatch, adapter)
    app = create_app(enable_startup_tasks=False)

    with TestClient(app) as client:
        status = client.get("/api/v2/xray")
        created = client.post(
            "/api/v2/xray/clients",
            json={"alias": "Portal", "requested_by": "pytest"},
        )
        job = _wait_for_job_result(client, created.json()["data"]["job"]["job_id"])
        created_payload = job["result"]["xray_client"]
        clients = client.get("/api/v2/xray/clients")
        subscription = client.get(f"/api/v2/xray/clients/{created_payload['client']['client_id']}/subscription")
        synced = client.post("/api/v2/xray/sync-subjects", json={"requested_by": "pytest"})

    assert status.status_code == 200
    assert status.json()["data"]["xray"]["forced_vpn_ready"] is False
    assert status.json()["data"]["xray"]["module"]["lifecycle_mode"] == "none"
    assert created.status_code == 200
    assert created.json()["data"]["job"]["job_type"] == "xray_client_create"
    assert job["status"] == "success"
    assert created_payload["client"]["client_id"]
    assert clients.status_code == 200
    assert len(clients.json()["data"]["clients"]) == 1
    assert subscription.status_code == 200
    assert "xray.example.test:443" in subscription.json()["data"]["subscription"]["subscription_uri"]
    assert synced.status_code == 200


def test_external_client_create_materializes_subscription_profile_and_delete_hard_deletes_it(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _patch_runtime(monkeypatch)
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [])
    adapter = _build_adapter(tmp_path, runner=_FakeRunner())
    _patch_xray_adapters(monkeypatch, adapter)
    _seed_server("server-1")
    with db_session() as connection:
        connection.execute(
            "UPDATE modules SET desired_state = 'enabled', runtime_state = 'running' WHERE module_name = 'xray'"
        )
    app = create_app(enable_startup_tasks=False)

    with TestClient(app) as client:
        created = client.post(
            "/api/v2/xray/clients",
            json={"alias": "Misha", "email": "misha", "requested_by": "pytest"},
        )
        job = _wait_for_job_result(client, created.json()["data"]["job"]["job_id"])
        created_payload = job["result"]["xray_client"]
        other_created = client.post(
            "/api/v2/xray/clients",
            json={"alias": "Other", "email": "other", "requested_by": "pytest"},
        )
        other_job = _wait_for_job_result(client, other_created.json()["data"]["job"]["job_id"])
        assert other_job["status"] == "success"
        other_prefix = f"sub-{hashlib.sha1(b'other').hexdigest()[:10]}-"
        profile = client.get(
            "/s/misha",
            headers={"X-Forwarded-Host": "xray.example.test", "X-Forwarded-Proto": "https"},
        )
        with db_session() as connection:
            before_delete_subjects = connection.execute(
                """
                SELECT subject_id
                FROM subjects
                WHERE implementation_kind = 'xray'
                  AND subject_type = 'explicit_external_client'
                  AND subject_role = 'vless_client'
                  AND (
                      lower(coalesce(alias, '')) LIKE '%misha%'
                      OR lower(coalesce(display_name, '')) LIKE '%misha%'
                      OR lower(coalesce(metadata_json, '')) LIKE '%misha%'
                  )
                ORDER BY subject_id
                """
            ).fetchall()
            before_delete_overrides = connection.execute(
                """
                SELECT o.subject_id
                FROM subject_server_overrides AS o
                JOIN subjects AS s ON s.subject_id = o.subject_id
                WHERE s.implementation_kind = 'xray'
                  AND s.subject_type = 'explicit_external_client'
                  AND s.subject_role = 'vless_client'
                  AND (
                      lower(coalesce(s.alias, '')) LIKE '%misha%'
                      OR lower(coalesce(s.display_name, '')) LIKE '%misha%'
                      OR lower(coalesce(s.metadata_json, '')) LIKE '%misha%'
                  )
                """
            ).fetchall()
            other_subjects_before = connection.execute(
                "SELECT subject_id FROM subjects WHERE lower(json_extract(metadata_json, '$.detail.email')) LIKE ? ORDER BY subject_id",
                (f"{other_prefix}%",),
            ).fetchall()
            account_row = connection.execute(
                "SELECT account_id FROM subscription_accounts WHERE slug = 'misha' LIMIT 1"
            ).fetchone()
            assert account_row is not None
            assert connection.execute(
                "SELECT 1 FROM subscription_profile_snapshots WHERE token = 'misha'"
            ).fetchone() is not None
            delete_ref = f"subscription-account:{account_row['account_id']}"
        deleted = client.request(
            "DELETE",
            f"/api/v2/xray/subscription-profiles/{delete_ref}",
            json={"requested_by": "pytest"},
        )
        deleted_again = client.request(
            "DELETE",
            f"/api/v2/xray/subscription-profiles/{delete_ref}",
            json={"requested_by": "pytest"},
        )
        after_delete = client.get(
            "/s/misha",
            headers={"X-Forwarded-Host": "xray.example.test", "X-Forwarded-Proto": "https"},
        )

    assert created.status_code == 200
    assert job["status"] == "success"
    assert created_payload["subscription_url"] == "/s/misha"
    assert profile.status_code == 200
    assert "vless://" in profile.text
    assert "xray.example.test:443" in profile.text
    assert len(before_delete_subjects) >= 2
    assert len(before_delete_overrides) >= 1
    assert len(other_subjects_before) >= 1
    assert deleted.status_code == 200
    deleted_payload = deleted.json()["data"]["subscription_profile"]
    assert deleted_payload["deleted_compatibility_clients_count"] == 1
    assert deleted_payload["cleanup"]["subjects_deleted"] == len(before_delete_subjects)
    assert deleted_payload["cleanup"]["server_overrides_deleted"] == len(before_delete_overrides)
    assert deleted_again.status_code == 200
    deleted_again_payload = deleted_again.json()["data"]["subscription_profile"]
    assert deleted_again_payload["ok"] is False
    assert deleted_again_payload["result"]["error_code"] == "SUBSCRIPTION_PROFILE_NOT_FOUND"
    assert after_delete.status_code == 404

    with db_session() as connection:
        account = connection.execute(
            "SELECT account_id FROM subscription_accounts WHERE slug = 'misha'"
        ).fetchone()
        subscription_client = connection.execute(
            "SELECT token FROM subscription_clients WHERE token = 'misha'"
        ).fetchone()
        snapshot = connection.execute(
            "SELECT token FROM subscription_profile_snapshots WHERE token = 'misha'"
        ).fetchone()
        job_result = connection.execute(
            "SELECT result_json FROM jobs WHERE job_type = 'xray_subscription_profile_delete' ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        remaining_subjects = connection.execute(
            """
            SELECT subject_id
            FROM subjects
            WHERE implementation_kind = 'xray'
              AND subject_type = 'explicit_external_client'
              AND subject_role = 'vless_client'
              AND (
                  lower(coalesce(alias, '')) LIKE '%misha%'
                  OR lower(coalesce(display_name, '')) LIKE '%misha%'
                  OR lower(coalesce(metadata_json, '')) LIKE '%misha%'
              )
            """
        ).fetchall()
        other_subjects_after = connection.execute(
            "SELECT subject_id FROM subjects WHERE lower(json_extract(metadata_json, '$.detail.email')) LIKE ? ORDER BY subject_id",
            (f"{other_prefix}%",),
        ).fetchall()
        remaining_overrides = connection.execute(
            """
            SELECT o.subject_id
            FROM subject_server_overrides AS o
            LEFT JOIN subjects AS s ON s.subject_id = o.subject_id
            WHERE lower(coalesce(o.subject_id, '')) LIKE '%misha%'
               OR lower(coalesce(s.alias, '')) LIKE '%misha%'
               OR lower(coalesce(s.display_name, '')) LIKE '%misha%'
               OR lower(coalesce(s.metadata_json, '')) LIKE '%misha%'
            """
        ).fetchall()
        delete_event = connection.execute(
            """
            SELECT event_type, details_json
            FROM operational_logs
            WHERE event_type = 'external_client.deleted'
            ORDER BY created_at
            """
        ).fetchall()
    assert account is None
    assert subscription_client is None
    assert snapshot is None
    serialized_job_result = job_result["result_json"] if job_result else ""
    assert "misha" not in serialized_job_result.lower()
    assert "uuid-" not in serialized_job_result.lower()
    assert "@fwrouter.local" not in serialized_job_result.lower()
    assert remaining_subjects == []
    assert [row["subject_id"] for row in other_subjects_after] == [row["subject_id"] for row in other_subjects_before]
    assert remaining_overrides == []
    assert len(delete_event) == 1
    event_details = json.loads(delete_event[0]["details_json"])
    assert event_details["subscription_ref"].startswith("sub-profile:")
    assert event_details["requested_by"] == "pytest"
    assert event_details["subjects_deleted"] == len(before_delete_subjects)
    assert "misha" not in json.dumps(event_details)

    config_payload = json.loads(config_path.read_text(encoding="utf-8"))
    emails = {
        str(client.get("email") or "")
        for client in config_payload["inbounds"][0]["settings"]["clients"]
    }
    assert not any(email.startswith(f"sub-{hashlib.sha1(b'misha').hexdigest()[:10]}-") for email in emails)
    assert "misha" not in emails
    assert any(email.startswith(other_prefix) for email in emails)


def test_subscription_profile_delete_reference_resolves_exact_account_and_fails_closed(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    with db_session() as connection:
        connection.executemany(
            "INSERT INTO subscription_accounts (account_id, slug, display_name, enabled) VALUES (?, ?, ?, 1)",
            [(71, "target-profile", "Target"), (72, "other-profile", "Other")],
        )
        connection.execute(
            "INSERT INTO subscription_accounts (account_id, slug, display_name, enabled) VALUES (73, 'disabled-profile', 'Disabled', 0)"
        )
        connection.execute(
            "INSERT INTO subscription_accounts (account_id, slug, display_name, enabled) VALUES (74, 'legacy-profile', 'Legacy', 1)"
        )
        connection.executemany(
            "INSERT INTO subscription_clients (account_id, token, enabled) VALUES (?, ?, 1)",
            [(71, "target-profile"), (73, "disabled-profile"), (74, "legacy-profile")],
        )
        connection.execute("INSERT INTO subscription_accounts (account_id, slug, display_name, enabled) VALUES (75, 'multi-profile', 'Multi', 1)")
        connection.executemany(
            "INSERT INTO subscription_clients (account_id, token, enabled) VALUES (75, ?, 1)",
            [("multi-profile-a",), ("multi-profile-b",)],
        )

    class _FakeJobManager:
        def __init__(self) -> None:
            self.created: list[dict[str, object]] = []

        def create(self, job_type: str, **kwargs: object) -> dict[str, object]:
            job = {"job_id": f"job-{len(self.created) + 1}", "job_type": job_type, "status": "queued", **kwargs}
            self.created.append(job)
            return job

        def start_job_and_wait(self, job_id: str, *, timeout_seconds: int) -> dict[str, object] | None:
            return next((job for job in self.created if job["job_id"] == job_id), None)

    manager = _FakeJobManager()
    monkeypatch.setattr(xray_subscription_service, "get_default_job_manager", lambda: manager)

    accepted = xray_subscription_service.submit_xray_subscription_profile_delete(
        "subscription-account:71", requested_by="pytest"
    )
    assert accepted["ok"] is True
    assert manager.created[0]["lock_key"] == "xray-subscription-profile-delete:71"
    assert manager.created[0]["input_data"]["token"] == "target-profile"
    assert manager.created[0]["input_data"]["account_id"] == 71

    disabled = xray_subscription_service.submit_xray_subscription_profile_delete(
        "subscription-account:73", requested_by="pytest"
    )
    assert disabled["ok"] is True
    assert manager.created[1]["lock_key"] == "xray-subscription-profile-delete:73"
    assert manager.created[1]["input_data"]["token"] == "disabled-profile"
    assert manager.created[1]["input_data"]["account_id"] == 73

    unknown = xray_subscription_service.submit_xray_subscription_profile_delete(
        "subscription-account:999", requested_by="pytest"
    )
    malformed = xray_subscription_service.submit_xray_subscription_profile_delete(
        "subscription-account:72oops", requested_by="pytest"
    )
    multiple_clients = xray_subscription_service.submit_xray_subscription_profile_delete(
        "subscription-account:75", requested_by="pytest"
    )
    assert unknown["ok"] is False
    assert unknown["result"]["error_code"] == "SUBSCRIPTION_PROFILE_NOT_FOUND"
    assert malformed["ok"] is False
    assert malformed["result"]["error_code"] == "SUBSCRIPTION_PROFILE_DELETE_REF_INVALID"
    assert multiple_clients["result"]["error_code"] == "SUBSCRIPTION_PROFILE_CLIENT_SET_UNSUPPORTED"
    assert len(manager.created) == 2

    legacy = xray_subscription_service.submit_xray_subscription_profile_delete(
        "legacy-profile", requested_by="pytest"
    )
    assert legacy["ok"] is True
    assert manager.created[2]["input_data"]["token"] == "legacy-profile"
    assert manager.created[2]["input_data"]["account_id"] == 74


def test_subscription_profile_delete_worker_rechecks_client_cardinality(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    with db_session() as connection:
        connection.execute("INSERT INTO subscription_accounts (account_id, slug, enabled) VALUES (76, 'single-profile', 1)")
        connection.execute("INSERT INTO subscription_clients (account_id, token, enabled) VALUES (76, 'single-profile', 1)")

    class _FakeJobManager:
        def create(self, job_type: str, **kwargs: object) -> dict[str, object]:
            return {"job_id": "queued-delete", "job_type": job_type, "status": "queued", **kwargs}

        def start_job_and_wait(self, job_id: str, *, timeout_seconds: int) -> dict[str, object] | None:
            return None

    monkeypatch.setattr(xray_subscription_service, "get_default_job_manager", lambda: _FakeJobManager())
    accepted = xray_subscription_service.submit_xray_subscription_profile_delete("subscription-account:76", requested_by="pytest")
    assert accepted["ok"] is True
    job_input = accepted["job"]["input_data"]
    with db_session() as connection:
        connection.execute("INSERT INTO subscription_clients (account_id, token, enabled) VALUES (76, 'second-token', 1)")
    result = xray_subscription_service.run_xray_subscription_profile_delete_job({"input": job_input, "requested_by": "pytest"})
    assert result["job_status"] == "failed"
    assert result["error_code"] == "SUBSCRIPTION_PROFILE_CLIENT_SET_UNSUPPORTED"
    with db_session() as connection:
        account = connection.execute("SELECT enabled FROM subscription_accounts WHERE account_id = 76").fetchone()
        clients = connection.execute("SELECT enabled FROM subscription_clients WHERE account_id = 76 ORDER BY client_id").fetchall()
    assert account["enabled"] == 1
    assert [row["enabled"] for row in clients] == [1, 1]


def test_stale_subscription_profile_delete_job_cannot_delete_recreated_slug(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    with db_session() as connection:
        connection.execute("INSERT INTO subscription_accounts (account_id, slug, enabled) VALUES (81, 'reusable', 0)")
        connection.execute("DELETE FROM subscription_accounts WHERE account_id = 81")
        connection.execute("INSERT INTO subscription_accounts (slug, enabled) VALUES ('reusable', 1)")
        replacement = connection.execute("SELECT account_id FROM subscription_accounts WHERE slug = 'reusable'").fetchone()
    result = xray_subscription_service.run_xray_subscription_profile_delete_job(
        {"input": {"token": "reusable", "account_id": 81}, "requested_by": "pytest"}
    )
    assert result["job_status"] == "failed"
    assert result["error_code"] == "SUBSCRIPTION_PROFILE_NOT_FOUND"
    with db_session() as connection:
        surviving = connection.execute("SELECT account_id, enabled FROM subscription_accounts WHERE slug = 'reusable'").fetchone()
    assert replacement is not None
    assert surviving is not None
    assert surviving["account_id"] == replacement["account_id"]
    assert surviving["enabled"] == 1


def test_disabled_subscription_profile_hard_delete_preserves_other_account(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    with db_session() as connection:
        connection.execute("INSERT INTO subscription_accounts (account_id, slug, enabled) VALUES (91, 'disabled-target', 0)")
        connection.execute("INSERT INTO subscription_clients (account_id, token, enabled) VALUES (91, 'disabled-target', 0)")
        connection.execute("INSERT INTO subscription_profile_snapshots (token, nodes_json) VALUES ('disabled-target', '[]')")
        connection.execute("INSERT INTO subscription_accounts (account_id, slug, enabled) VALUES (92, 'other-profile', 1)")
        connection.execute("INSERT INTO subscription_clients (account_id, token, enabled) VALUES (92, 'other-profile-token', 1)")
        connection.execute("INSERT INTO subscription_profile_snapshots (token, nodes_json) VALUES ('other-profile-token', '[]')")
    monkeypatch.setattr(xray_subscription_service, "_sync_xray_inventory", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(xray_subscription_service, "reconcile_xray_subscription_profile_nodes", lambda **_kwargs: {"ok": True, "nodes_count": 0})
    result = xray_subscription_service.delete_xray_subscription_profile(
        "disabled-target", account_id=91, requested_by="pytest"
    )
    assert result["ok"] is True
    assert result["stage"] == "completed"
    serialized = json.dumps(result).lower()
    assert "disabled-target" not in serialized
    with db_session() as connection:
        assert connection.execute("SELECT 1 FROM subscription_accounts WHERE account_id = 91").fetchone() is None
        assert connection.execute("SELECT 1 FROM subscription_clients WHERE account_id = 91").fetchone() is None
        assert connection.execute("SELECT 1 FROM subscription_profile_snapshots WHERE token = 'disabled-target'").fetchone() is None
        assert connection.execute("SELECT 1 FROM subscription_accounts WHERE account_id = 92").fetchone() is not None
        assert connection.execute("SELECT 1 FROM subscription_profile_snapshots WHERE token = 'other-profile-token'").fetchone() is not None


def test_subscription_profile_delete_rolls_back_projection_cleanup_on_account_mismatch(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    with db_session() as connection:
        connection.execute("INSERT INTO subscription_accounts (account_id, slug, enabled) VALUES (96, 'rollback-profile', 1)")
        connection.execute("INSERT INTO subscription_clients (account_id, token, enabled) VALUES (96, 'rollback-profile', 1)")
        connection.execute("INSERT INTO subscription_profile_snapshots (token, nodes_json) VALUES ('rollback-profile', '[]')")
    monkeypatch.setattr(xray_subscription_service, "_sync_xray_inventory", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(xray_subscription_service, "_xray_adapter", lambda: type("Adapter", (), {"list_clients": lambda _self: []})())
    monkeypatch.setattr(xray_subscription_service, "reconcile_xray_subscription_profile_nodes", lambda **_kwargs: {"ok": True, "nodes_count": 0})

    def _cleanup_then_remove_account(_token: str, *, connection):
        # Simulate a stale exact target after scoped projection cleanup began.
        connection.execute("DELETE FROM subscription_accounts WHERE account_id = 96")
        return {"subject_ids": [], "subjects_deleted": 1, "server_overrides_deleted": 0, "user_overrides_deleted": 0}

    monkeypatch.setattr(xray_subscription_service, "cleanup_xray_subscription_profile_projection", _cleanup_then_remove_account)
    result = xray_subscription_service.delete_xray_subscription_profile(
        "rollback-profile", account_id=96, requested_by="pytest"
    )
    assert result["ok"] is False
    assert result["stage"] == "delete_account"
    with db_session() as connection:
        account = connection.execute("SELECT account_id, enabled FROM subscription_accounts WHERE account_id = 96").fetchone()
        child = connection.execute("SELECT token, enabled FROM subscription_clients WHERE account_id = 96").fetchone()
        snapshot = connection.execute("SELECT token FROM subscription_profile_snapshots WHERE token = 'rollback-profile'").fetchone()
    assert account is not None and account["enabled"] == 0
    assert child is not None and child["enabled"] == 0
    assert snapshot is not None


def test_subscription_profile_delete_preserves_database_rows_when_materialize_fails(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _patch_runtime(monkeypatch)
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [])
    adapter = _build_adapter(tmp_path, runner=_FakeRunner())
    _patch_xray_adapters(monkeypatch, adapter)
    _seed_server("server-1")
    with db_session() as connection:
        connection.execute(
            "UPDATE modules SET desired_state = 'enabled', runtime_state = 'running' WHERE module_name = 'xray'"
        )
    app = create_app(enable_startup_tasks=False)

    with TestClient(app) as client:
        created = client.post(
            "/api/v2/xray/clients",
            json={"alias": "Misha", "email": "misha", "requested_by": "pytest"},
        )
        assert created.status_code == 200
        with db_session() as connection:
            before_delete_subjects = connection.execute(
                """
                SELECT subject_id
                FROM subjects
                WHERE implementation_kind = 'xray'
                  AND subject_type = 'explicit_external_client'
                  AND subject_role = 'vless_client'
                  AND (
                      lower(coalesce(alias, '')) LIKE '%misha%'
                      OR lower(coalesce(display_name, '')) LIKE '%misha%'
                      OR lower(coalesce(metadata_json, '')) LIKE '%misha%'
                  )
                """
            ).fetchall()
            before_delete_overrides = connection.execute(
                """
                SELECT o.subject_id
                FROM subject_server_overrides AS o
                JOIN subjects AS s ON s.subject_id = o.subject_id
                WHERE s.implementation_kind = 'xray'
                  AND s.subject_type = 'explicit_external_client'
                  AND s.subject_role = 'vless_client'
                  AND (
                      lower(coalesce(s.alias, '')) LIKE '%misha%'
                      OR lower(coalesce(s.display_name, '')) LIKE '%misha%'
                      OR lower(coalesce(s.metadata_json, '')) LIKE '%misha%'
                  )
                """
            ).fetchall()

        monkeypatch.setattr(
            xray_subscription_service,
            "_materialize_xray_runtime_bindings",
            lambda **_kwargs: {
                "ok": False,
                "status": "failed",
                "stage": "materialize",
                "error_code": "XRAY_TEST_MATERIALIZE_FAILED",
            },
        )
        deleted = client.request(
            "DELETE",
            "/api/v2/xray/subscription-profiles/misha",
            json={"requested_by": "pytest"},
        )
        deleted_again = client.request(
            "DELETE",
            "/api/v2/xray/subscription-profiles/misha",
            json={"requested_by": "pytest"},
        )

    assert len(before_delete_subjects) >= 2
    assert len(before_delete_overrides) >= 1
    assert deleted.status_code == 200
    payload = deleted.json()["data"]["subscription_profile"]
    assert payload["ok"] is False
    assert payload["stage"] == "reconcile_subscription_profile_delete"
    assert payload["cleanup"]["subjects_deleted"] == 0
    assert payload["cleanup"]["server_overrides_deleted"] == 0
    assert deleted_again.status_code == 200
    assert deleted_again.json()["data"]["subscription_profile"]["cleanup"]["subjects_deleted"] == 0

    with db_session() as connection:
        remaining_subjects = connection.execute(
            """
            SELECT subject_id
            FROM subjects
            WHERE implementation_kind = 'xray'
              AND subject_type = 'explicit_external_client'
              AND subject_role = 'vless_client'
              AND (
                  lower(coalesce(alias, '')) LIKE '%misha%'
                  OR lower(coalesce(display_name, '')) LIKE '%misha%'
                  OR lower(coalesce(metadata_json, '')) LIKE '%misha%'
              )
            """
        ).fetchall()
        remaining_overrides = connection.execute(
            """
            SELECT o.subject_id
            FROM subject_server_overrides AS o
            LEFT JOIN subjects AS s ON s.subject_id = o.subject_id
            WHERE lower(coalesce(o.subject_id, '')) LIKE '%misha%'
               OR lower(coalesce(s.alias, '')) LIKE '%misha%'
               OR lower(coalesce(s.display_name, '')) LIKE '%misha%'
               OR lower(coalesce(s.metadata_json, '')) LIKE '%misha%'
            """
        ).fetchall()
        failure_event = connection.execute(
            """
            SELECT event_type, details_json
            FROM operational_logs
            WHERE event_type = 'external_client.delete_failed'
            ORDER BY created_at DESC
            LIMIT 1
            """
        ).fetchone()

    assert len(remaining_subjects) == len(before_delete_subjects)
    assert len(remaining_overrides) == len(before_delete_overrides)
    assert failure_event is not None
    details = json.loads(failure_event["details_json"])
    assert details["subjects_deleted"] == 0
    assert "misha" not in json.dumps(details)


def test_subscription_profile_delete_failure_writes_external_client_event(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _patch_runtime(monkeypatch)
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [])
    _seed_server("server-1")
    with db_session() as connection:
        connection.execute(
            "UPDATE modules SET desired_state = 'enabled', runtime_state = 'running' WHERE module_name = 'xray'"
        )
        connection.execute(
            """
            INSERT INTO subscription_accounts (slug, display_name, enabled)
            VALUES ('misha', 'Misha', 1)
            """
        )
        connection.execute(
            """
            INSERT INTO subscription_clients (account_id, token, enabled, display_name)
            SELECT account_id, 'misha', 1, 'Misha'
            FROM subscription_accounts
            WHERE slug = 'misha'
            """
        )

    class _FailingDeleteAdapter:
        def list_clients(self):
            return [
                XrayClient(
                    client_id="uuid-misha",
                    client_uuid="uuid-misha",
                    email="misha",
                    alias="Misha",
                    enabled=True,
                    raw={},
                )
            ]

        def delete_client(self, client_id: str) -> XrayApplyResult:
            return XrayApplyResult(
                ok=False,
                message=f"delete failed for {client_id}",
                error_code="XRAY_DELETE_FAILED",
                details={"client_id": client_id},
            )

    adapter = _FailingDeleteAdapter()
    monkeypatch.setattr(xray_service, "DEFAULT_XRAY_ADAPTER", adapter)
    monkeypatch.setattr(inventory_service, "DEFAULT_XRAY_ADAPTER", adapter)
    monkeypatch.setattr(runtime_service, "DEFAULT_XRAY_ADAPTER", adapter)
    monkeypatch.setattr(xray_runtime_state_service, "DEFAULT_XRAY_ADAPTER", adapter)
    app = create_app(enable_startup_tasks=False)

    with TestClient(app) as client:
        deleted = client.request(
            "DELETE",
            "/api/v2/xray/subscription-profiles/misha",
            json={"requested_by": "pytest"},
        )

    assert deleted.status_code == 200
    payload = deleted.json()["data"]["subscription_profile"]
    assert payload["ok"] is False
    assert payload["stage"] == "delete_compatibility_client"

    with db_session() as connection:
        event = connection.execute(
            """
            SELECT level, event_type, details_json
            FROM operational_logs
            WHERE event_type = 'external_client.delete_failed'
            ORDER BY created_at DESC
            LIMIT 1
            """
        ).fetchone()

    assert event is not None
    assert event["level"] == "warning"
    details = json.loads(event["details_json"])
    assert details["subscription_ref"].startswith("sub-profile:")
    assert "uuid-misha" not in json.dumps(details)
    assert "misha" not in json.dumps(details)
    assert details["error_code"] == "XRAY_DELETE_FAILED"


def test_public_subscription_route_detects_happ(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [])
    adapter = _build_adapter(tmp_path, runner=_FakeRunner())
    _patch_xray_adapters(monkeypatch, adapter)
    _seed_server("server-1")
    _seed_subscription_identity(slug="stepan", token="stepan", app_type="auto")
    app = create_app(enable_startup_tasks=False)

    with TestClient(app) as client:
        response = client.get(
            "/s/stepan",
            headers={"User-Agent": "Happ/3.19.1/Android/test"},
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert not any(name.lower().startswith("x-fwrouter-") for name in response.headers)
    assert response.headers["profile-update-interval"] == "1"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["profile-title"] == "Stepan"
    assert response.headers["subscription-userinfo"] == "upload=0; download=0; total=0; expire=0"
    assert not response.text.startswith("vless://")
    assert "#profile" not in response.text
    decoded = base64.b64decode(response.text).decode("utf-8")
    assert decoded.count("vless://") == 2
    assert "alpn=" not in decoded
    assert "fp=" not in decoded
    assert "packetEncoding=" not in decoded


def test_public_subscription_route_explicit_happ_format_wins(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [])
    adapter = _build_adapter(tmp_path, runner=_FakeRunner())
    _patch_xray_adapters(monkeypatch, adapter)
    _seed_server("server-1")
    _seed_subscription_identity(slug="stepan", token="stepan", app_type="auto")
    app = create_app(enable_startup_tasks=False)

    with TestClient(app) as client:
        response = client.get(
            "/s/stepan?format=happ",
            headers={"X-Forwarded-Host": "xray.example.test", "X-Forwarded-Proto": "https"},
        )

    assert response.status_code == 200
    assert not any(name.lower().startswith("x-fwrouter-") for name in response.headers)
    assert response.headers["profile-update-interval"] == "1"
    assert response.headers["profile-title"] == "Stepan"
    assert not response.text.startswith("vless://")
    assert "#profile" not in response.text
    decoded = base64.b64decode(response.text).decode("utf-8")
    assert decoded.startswith("vless://")
    assert decoded.endswith("\n")
    assert "encryption=none" in decoded
    assert "type=ws" in decoded
    assert "security=tls" in decoded
    assert "sni=xray.example.test" in decoded
    assert "host=xray.example.test" in decoded
    assert "path=%2Fvless" in decoded
    assert "alpn=" not in decoded
    assert "fp=" not in decoded
    assert "packetEncoding=" not in decoded


def test_public_subscription_route_happ_uses_configured_host_for_internal_gateway(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [])
    adapter = _build_adapter(tmp_path, runner=_FakeRunner())
    _patch_xray_adapters(monkeypatch, adapter)
    _seed_server("server-1")
    _seed_subscription_identity(slug="stepan", token="stepan", app_type="auto")
    app = create_app(enable_startup_tasks=False)

    with TestClient(app) as client:
        response = client.get(
            "/s/stepan?format=happ",
            headers={
                "Host": "127.0.0.1:5000",
                "X-Forwarded-Proto": "https",
                "User-Agent": "Happ/4.3.0/Android/test",
            },
        )

    assert response.status_code == 200
    decoded = base64.b64decode(response.text).decode("utf-8")
    assert "@xray.example.test:443" in decoded
    assert "sni=xray.example.test" in decoded
    assert "host=xray.example.test" in decoded
    assert "@127.0.0.1:443" not in decoded
    assert "sni=127.0.0.1" not in decoded
    assert "host=127.0.0.1" not in decoded


def test_public_subscription_route_does_not_reconcile_on_get(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [])
    adapter = _build_adapter(tmp_path, runner=_FakeRunner())
    _patch_xray_adapters(monkeypatch, adapter)
    _seed_server("server-1")
    _seed_subscription_identity(slug="stepan", token="stepan", app_type="auto")
    with db_session() as connection:
        connection.execute(
            """
            UPDATE subscription_clients
            SET last_seen_at = '2026-01-01 00:00:00',
                last_user_agent = 'before-test',
                updated_at = '2026-01-01 00:00:00'
            WHERE token = 'stepan'
            """
        )

    original_service_call = xray_routes.xray_service_call
    called: list[str] = []

    def guarded_service_call(func, *args, **kwargs):
        name = getattr(func, "__name__", str(func))
        called.append(name)
        assert name != "reconcile_xray_subscription_profile_nodes"
        assert name != "materialize_xray_runtime_bindings"
        return original_service_call(func, *args, **kwargs)

    monkeypatch.setattr(xray_routes, "xray_service_call", guarded_service_call)
    app = create_app(enable_startup_tasks=False)
    before = _database_snapshot()

    with TestClient(app) as client:
        response = client.get(
            "/s/stepan",
            headers={"User-Agent": "Happ/4.3.0/Android/test"},
        )

    assert response.status_code == 200
    assert "vless://" in base64.b64decode(response.text).decode("utf-8")
    assert called == ["export_subscription_profile_text"]
    assert _database_snapshot() == before


def test_public_subscription_route_unknown_alias_does_not_create_legacy_identity(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _seed_server("server-1")
    app = create_app(enable_startup_tasks=False)
    before = _database_snapshot()

    with TestClient(app) as client:
        response = client.get("/s/unknown-alias")

    assert response.status_code == 404
    assert _database_snapshot() == before


def test_xray_client_subscription_text_get_is_read_only(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [{"id": "uuid-stepan", "email": "stepan@example.test"}])
    runner = _FakeRunner()
    adapter = _build_adapter(tmp_path, runner=runner)
    _patch_xray_adapters(monkeypatch, adapter)
    before = _database_snapshot()
    app = create_app(enable_startup_tasks=False)

    with TestClient(app) as client:
        response = client.get("/api/v2/xray/clients/uuid-stepan/subscription.txt")

    assert response.status_code == 200
    assert base64.b64decode(response.text).decode("utf-8").startswith("vless://uuid-stepan@")
    assert runner.calls == []
    assert _database_snapshot() == before


def test_xray_client_subscription_json_get_is_read_only(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [{"id": "uuid-stepan", "email": "stepan@example.test"}])
    runner = _FakeRunner()
    adapter = _build_adapter(tmp_path, runner=runner)
    _patch_xray_adapters(monkeypatch, adapter)
    before = _database_snapshot()
    app = create_app(enable_startup_tasks=False)

    with TestClient(app) as client:
        response = client.get("/api/v2/xray/clients/uuid-stepan/subscription")

    assert response.status_code == 200
    payload = response.json()["data"]["subscription"]
    assert payload["subscription_uri"].startswith("vless://uuid-stepan@")
    assert runner.calls == []
    assert _database_snapshot() == before


def test_vpn_auto_subscription_text_get_does_not_create_or_materialize(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [])
    runner = _FakeRunner()
    adapter = _build_adapter(tmp_path, runner=runner)
    _patch_xray_adapters(monkeypatch, adapter)
    _seed_server("server-1")
    before = _database_snapshot()
    app = create_app(enable_startup_tasks=False)

    with TestClient(app) as client:
        response = client.get("/api/v2/xray/clients/vpn-auto/subscription.txt")

    assert response.status_code == 404
    assert "not materialized" in response.text
    assert runner.calls == []
    assert _database_snapshot() == before


def test_legacy_subscription_get_is_read_only(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    before = _database_snapshot()
    app = create_app(enable_startup_tasks=False)

    with TestClient(app) as client:
        response = client.get("/api/v2/subscription")

    assert response.status_code == 200
    assert response.json()["data"]["subscription"]["status"] == "not_configured"
    assert _database_snapshot() == before


def test_public_subscription_route_happ_base64_multinode(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [])
    adapter = _build_adapter(tmp_path, runner=_FakeRunner())
    _patch_xray_adapters(monkeypatch, adapter)
    _seed_server("server-1")
    _seed_server("server-2")
    _seed_server("server-3")
    _seed_subscription_identity(slug="stepan", token="stepan", app_type="auto")
    app = create_app(enable_startup_tasks=False)

    with TestClient(app) as client:
        response = client.get(
            "/s/stepan?format=happ",
            headers={
                "User-Agent": "Happ/3.19.1/Android/test",
                "X-Forwarded-Host": "xray.example.test",
                "X-Forwarded-Proto": "https",
            },
        )

    assert response.status_code == 200
    assert response.headers["profile-update-interval"] == "1"
    assert response.headers["profile-title"] == "Stepan"
    decoded = base64.b64decode(response.text).decode("utf-8")
    lines = [line for line in decoded.splitlines() if line.strip()]
    assert len(lines) == 4
    assert all(line.startswith("vless://") for line in lines)
    assert all("path=%2Fvless" in line for line in lines)
    assert all("alpn=" not in line for line in lines)
    assert all("fp=" not in line for line in lines)
    assert all("packetEncoding=" not in line for line in lines)


def test_public_subscription_route_detects_clash(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [])
    adapter = _build_adapter(tmp_path, runner=_FakeRunner())
    _patch_xray_adapters(monkeypatch, adapter)
    _seed_server("server-1")
    _seed_subscription_identity(slug="stepan", token="device-1", app_type="auto")
    app = create_app(enable_startup_tasks=False)

    with TestClient(app) as client:
        response = client.get(
            "/s/device-1",
            headers={
                "User-Agent": "FlClash/1.0",
                "X-Forwarded-Host": "xray.example.test",
                "X-Forwarded-Proto": "https",
            },
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/yaml")
    assert "client-fingerprint: chrome" in response.text
    assert "alpn:" in response.text
    assert 'server: "xray.example.test"' in response.text
    assert 'servername: "xray.example.test"' in response.text
    assert 'Host: "xray.example.test"' in response.text
    assert 'path: "/vless"' in response.text


def test_public_subscription_route_explicit_clash_format_wins(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [])
    adapter = _build_adapter(tmp_path, runner=_FakeRunner())
    _patch_xray_adapters(monkeypatch, adapter)
    _seed_server("server-1")
    _seed_subscription_identity(slug="stepan", token="stepan", app_type="auto")
    app = create_app(enable_startup_tasks=False)

    with TestClient(app) as client:
        response = client.get("/s/stepan?format=flclashx")

    assert response.status_code == 200
    assert not any(name.lower().startswith("x-fwrouter-") for name in response.headers)
    assert response.headers["content-type"].startswith("application/yaml")
    assert "proxies:" in response.text


def test_public_subscription_route_explicit_raw_vless_format_wins(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [])
    adapter = _build_adapter(tmp_path, runner=_FakeRunner())
    _patch_xray_adapters(monkeypatch, adapter)
    _seed_server("server-1")
    _seed_subscription_identity(slug="stepan", token="stepan", app_type="happ")
    app = create_app(enable_startup_tasks=False)

    with TestClient(app) as client:
        response = client.get("/s/stepan?format=raw-vless")

    assert response.status_code == 200
    assert not any(name.lower().startswith("x-fwrouter-") for name in response.headers)
    assert response.text.startswith("vless://")
    assert "#profile-title:" not in response.text


def test_public_subscription_route_rejects_removed_happ_json_format(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [])
    adapter = _build_adapter(tmp_path, runner=_FakeRunner())
    _patch_xray_adapters(monkeypatch, adapter)
    _seed_server("server-1")
    _seed_server("server-2")
    _seed_server("server-3")
    _seed_subscription_identity(slug="stepan", token="stepan", app_type="auto")
    app = create_app(enable_startup_tasks=False)

    with TestClient(app) as client:
        response = client.get("/s/stepan?format=happ-json")

    assert response.status_code == 200
    assert not any(name.lower().startswith("x-fwrouter-") for name in response.headers)
    assert response.text.startswith("vless://")
    assert "legacy.example.test" not in response.text


def test_public_subscription_route_rejects_removed_happ_full_json_format(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [])
    adapter = _build_adapter(tmp_path, runner=_FakeRunner())
    _patch_xray_adapters(monkeypatch, adapter)
    _seed_server("server-1")
    _seed_subscription_identity(slug="stepan", token="stepan", app_type="auto")
    app = create_app(enable_startup_tasks=False)

    with TestClient(app) as client:
        response = client.get("/s/stepan?format=happ-full-json")

    assert response.status_code == 200
    assert not any(name.lower().startswith("x-fwrouter-") for name in response.headers)
    assert response.text.startswith("vless://")
    assert "legacy.example.test" not in response.text


def test_legacy_subscription_txt_route_named_profile_returns_404(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [])
    adapter = _build_adapter(tmp_path, runner=_FakeRunner())
    _patch_xray_adapters(monkeypatch, adapter)
    _seed_server("server-1")
    _seed_subscription_identity(slug="stepan", token="stepan", app_type="auto")
    app = create_app(enable_startup_tasks=False)

    with TestClient(app) as client:
        response = client.get(
            "/api/v2/xray/clients/stepan/subscription.txt",
            headers={"User-Agent": "Happ/3.19.1/Android/test"},
        )

    assert response.status_code == 404


def test_reconcile_xray_subscription_profiles_include_socks_handoff_nodes(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _patch_runtime(monkeypatch)
    monkeypatch.setattr(
        subject_policy_service,
        "build_runtime_enforcement_state",
        lambda: {
            "supported_modes": {"direct": True, "selective": False, "vpn": True},
            "enforcement_level": "global_vpn_enforced",
            "traffic_enforcement_guaranteed": True,
        },
    )
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [])
    adapter = _build_adapter(tmp_path, runner=_FakeRunner())
    _patch_xray_adapters(monkeypatch, adapter)
    _seed_server("server-1")
    _seed_routing_state(desired_mode="vpn", active_auto_server_id="server-1")
    _seed_subscription_identity(slug="stepan", token="device-1", app_type="happ")
    with db_session() as connection:
        connection.execute(
            "UPDATE modules SET desired_state = 'enabled', runtime_state = 'running' WHERE module_name = 'xray'"
        )

    result = xray_service.reconcile_xray_subscription_profile_nodes(
        requested_by="pytest",
    )
    materialized = xray_service.materialize_xray_runtime_bindings(
        requested_by="pytest",
        prepare_mihomo_handoff=False,
    )

    assert result["ok"] is True
    assert materialized["ok"] is True
    config_payload = json.loads(config_path.read_text(encoding="utf-8"))
    emails = {
        client.get("email")
        for client in config_payload["inbounds"][0]["settings"]["clients"]
    }
    assert any(str(email).startswith("sub-") for email in emails)
    managed_outbounds = [
        outbound
        for outbound in config_payload["outbounds"]
        if str(outbound.get("tag") or "").startswith("fwrouter-egress-")
    ]
    assert managed_outbounds
    assert all(outbound["protocol"] == "socks" for outbound in managed_outbounds)


def test_xray_handoff_uses_mihomo_runtime_proxy_name(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _patch_runtime(monkeypatch)
    monkeypatch.setattr(
        subject_policy_service,
        "build_runtime_enforcement_state",
        lambda: {
            "supported_modes": {"direct": True, "selective": False, "vpn": True},
            "enforcement_level": "global_vpn_enforced",
            "traffic_enforcement_guaranteed": True,
        },
    )
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [])
    adapter = _build_adapter(tmp_path, runner=_FakeRunner())
    _patch_xray_adapters(monkeypatch, adapter)
    _seed_server(
        "server-runtime",
        server_name="Display Name",
        raw={
            "name": "Display Name",
            "_fwrouter_runtime_name": "Display Name [abc12345]",
        },
    )
    _seed_routing_state(desired_mode="vpn", active_auto_server_id=None)
    _seed_subscription_identity(slug="stepan", token="device-1", app_type="happ")
    desired = list_desired_subscription_xray_clients("stepan")
    server_node = next(node for node in desired if node["server_id"] == "server-runtime")
    subject_id = f"xray:{server_node['client_uuid']}"
    with db_session() as connection:
        connection.execute(
            "UPDATE modules SET desired_state = 'enabled', runtime_state = 'running' WHERE module_name = 'xray'"
        )
        connection.execute(
            """
            INSERT INTO subjects (
                subject_id,
                subject_type,
                subject_role,
                implementation_kind,
                stable_key,
                display_name,
                alias,
                desired_mode,
                runtime_state,
                is_active,
                is_deleted,
                metadata_json
            )
            VALUES (?, 'explicit_external_client', 'vless_client', 'xray', ?, ?, ?, 'enabled', 'active', 1, 0, json(?))
            """,
            (
                subject_id,
                subject_id,
                server_node["client_email"],
                "stepan",
                json.dumps(
                    {
                        "provider": "xray",
                        "detail": {
                            "client_id": server_node["client_uuid"],
                            "client_uuid": server_node["client_uuid"],
                            "email": server_node["client_email"],
                            "enabled": True,
                        },
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            ),
        )
        connection.execute(
            """
            INSERT INTO subject_server_overrides (
                subject_id,
                selected_server_id,
                selected_until,
                apply_state
            )
            VALUES (?, 'server-runtime', datetime('now', '+24 hours'), 'pending')
            """,
            (subject_id,),
        )

    result = xray_service.reconcile_xray_subscription_profile_nodes(
        requested_by="pytest",
    )
    materialized = xray_service.materialize_xray_runtime_bindings(
        requested_by="pytest",
        prepare_mihomo_handoff=False,
    )

    assert result["ok"] is True
    assert materialized["ok"] is True
    bindings = materialized["bindings_state"]["bindings"]
    restored = next(binding for binding in bindings if binding["client_email"] == server_node["client_email"])
    assert restored["server_name"] == "Display Name"
    assert restored["server_runtime_name"] == "Display Name [abc12345]"
    assert restored["handoff_proxy_name"] == "Display Name [abc12345]"


def test_materialize_xray_bindings_reconciles_stale_inactive_override_status(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _patch_runtime(monkeypatch)
    monkeypatch.setattr(
        subject_policy_service,
        "build_runtime_enforcement_state",
        lambda: {
            "supported_modes": {"direct": True, "selective": False, "vpn": True},
            "enforcement_level": "global_vpn_enforced",
            "traffic_enforcement_guaranteed": True,
        },
    )
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [{"id": "uuid-reconcile", "email": "reconcile@example.test"}])
    adapter = _build_adapter(tmp_path, runner=_FakeRunner())
    _patch_xray_adapters(monkeypatch, adapter)
    inventory_service.sync_subject_inventory(
        requested_by="pytest",
        discover_docker=False,
        discover_tailscale=False,
        discover_xray=True,
    )
    _seed_server("server-1")
    _seed_routing_state(desired_mode="vpn", active_auto_server_id="server-1")

    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO subject_server_overrides (
                subject_id,
                selected_server_id,
                selected_until,
                apply_state,
                error_code,
                error_message
            )
            VALUES (
                'xray:uuid-reconcile',
                'server-1',
                datetime('now', '+24 hours'),
                'pending',
                'SCOPED_RUNTIME_PENDING_INACTIVE_SUBJECT',
                'Pending (subject inactive)'
            )
            """
        )
        connection.execute(
            """
            UPDATE subjects
            SET is_active = 0, runtime_state = 'inactive'
            WHERE subject_id = 'xray:uuid-reconcile'
            """
        )

    inactive_subject = get_subject_with_effective_state("xray:uuid-reconcile")
    assert inactive_subject is not None
    assert inactive_subject["effective_state"]["scoped_runtime"]["status"] == "pending_inactive_subject"

    with db_session() as connection:
        connection.execute(
            """
            UPDATE subjects
            SET is_active = 1, runtime_state = 'active'
            WHERE subject_id = 'xray:uuid-reconcile'
            """
        )

    first = xray_service.materialize_xray_runtime_bindings(
        requested_by="pytest",
        prepare_mihomo_handoff=False,
    )
    assert first["ok"] is True
    assert first["override_status_sync"]["updated_count"] == 1

    with db_session() as connection:
        row = connection.execute(
            """
            SELECT apply_state, error_code, error_message
            FROM subject_server_overrides
            WHERE subject_id = 'xray:uuid-reconcile'
            """
        ).fetchone()
    assert row is not None
    assert row["apply_state"] == "clean"
    assert row["error_code"] is None
    assert row["error_message"] is None

    active_subject = get_subject_with_effective_state("xray:uuid-reconcile")
    assert active_subject is not None
    scoped_runtime = active_subject["effective_state"]["scoped_runtime"]
    assert scoped_runtime["status"] == "applied"
    assert scoped_runtime["applied"] is True

    second = xray_service.materialize_xray_runtime_bindings(
        requested_by="pytest",
        prepare_mihomo_handoff=False,
    )
    assert second["ok"] is True
    assert second["override_status_sync"]["updated_count"] == 0


def test_runtime_summary_integration(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [{"id": "uuid-runtime", "email": "runtime@example.test"}])
    adapter = _build_adapter(tmp_path, runner=_FakeRunner())
    _patch_xray_adapters(monkeypatch, adapter)

    summary = runtime_service.get_runtime_summary()

    assert summary["xray"]["adapter"] == "xray"
    assert summary["xray"]["runtime_state"] == "running"
    assert summary["xray"]["forced_vpn_ready"] is False
    assert summary["xray"]["traffic_available"] is False


def test_xray_effective_state_for_enabled_subject(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [{"id": "uuid-enabled", "email": "enabled@example.test"}])
    adapter = _build_adapter(tmp_path, runner=_FakeRunner())
    _patch_xray_adapters(monkeypatch, adapter)
    inventory_service.sync_subject_inventory(
        requested_by="pytest",
        discover_docker=False,
        discover_tailscale=False,
        discover_xray=True,
    )

    subject = get_subject_with_effective_state("xray:uuid-enabled")

    assert subject is not None
    assert subject["effective_state"]["effective_mode"] == "forced_vpn"


def test_xray_server_override_exposes_pending_scoped_runtime(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _patch_runtime(monkeypatch)
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [{"id": "uuid-binding", "email": "binding@example.test"}])
    adapter = _build_adapter(tmp_path, runner=_FakeRunner())
    _patch_xray_adapters(monkeypatch, adapter)
    inventory_service.sync_subject_inventory(
        requested_by="pytest",
        discover_docker=False,
        discover_tailscale=False,
        discover_xray=True,
    )
    _seed_server("server-1")
    _seed_routing_state(desired_mode="vpn", active_auto_server_id="server-1")
    monkeypatch.setattr(
        subject_policy_service,
        "build_runtime_enforcement_state",
        lambda: {
            "supported_modes": {"direct": True, "selective": False, "vpn": True},
            "enforcement_level": "global_vpn_enforced",
            "traffic_enforcement_guaranteed": True,
        },
    )

    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO subject_server_overrides (
                subject_id,
                selected_server_id,
                selected_until,
                apply_state
            )
            VALUES (?, ?, datetime('now', '+24 hours'), 'pending')
            """,
            ("xray:uuid-binding", "server-1"),
        )

    subject = get_subject_with_effective_state("xray:uuid-binding")

    assert subject is not None
    scoped_runtime = subject["effective_state"]["scoped_runtime"]
    assert scoped_runtime["tracked"] is True
    assert scoped_runtime["eligible"] is True
    assert scoped_runtime["applied"] is False
    assert scoped_runtime["status"] == "pending_unresolved_subject_match"
    assert scoped_runtime["selected_server_id"] == "server-1"
    assert scoped_runtime["selected_server_source"] == "subject_override"
    assert scoped_runtime["match_key"] == "xray-client-uuid:uuid-binding"
    assert scoped_runtime["resolution_reason"] == "subject_explicit_client_runtime_binding_missing"
    summary = runtime_service.get_runtime_summary()
    assert summary["dataplane"]["scoped_egress"]["state"] == "degraded"
    assert summary["dataplane"]["scoped_egress"]["unresolved_count"] >= 1


def test_xray_server_override_endpoint_persists_pending_runtime_gap(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    register_extended_handlers(get_default_job_manager())
    _patch_runtime(monkeypatch)
    monkeypatch.setattr(
        subject_policy_service,
        "build_runtime_enforcement_state",
        lambda: {
            "supported_modes": {"direct": True, "selective": False, "vpn": True},
            "enforcement_level": "global_vpn_enforced",
            "traffic_enforcement_guaranteed": True,
        },
    )
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [{"id": "uuid-route", "email": "route@example.test"}])
    adapter = _build_adapter(tmp_path, runner=_FakeRunner())
    _patch_xray_adapters(monkeypatch, adapter)
    inventory_service.sync_subject_inventory(
        requested_by="pytest",
        discover_docker=False,
        discover_tailscale=False,
        discover_xray=True,
    )
    _seed_server("server-1")
    _seed_routing_state(desired_mode="vpn", active_auto_server_id="server-1")

    app = create_app(enable_startup_tasks=False)
    with TestClient(app) as client:
        response = client.post(
            "/api/v2/subjects/xray:uuid-route/server-override",
            json={"server_id": "server-1", "requested_by": "pytest"},
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["ok"] is True
        override = payload["data"]["server_override"]
        assert override["apply_state"] == "clean"
        assert override["error_code"] is None

        subject = client.get("/api/v2/subjects/xray:uuid-route").json()["data"]["subject"]
        scoped_runtime = subject["effective_state"]["scoped_runtime"]
        assert scoped_runtime["tracked"] is True
        assert scoped_runtime["status"] == "applied"
        assert scoped_runtime["match_key"] == "xray-client-uuid:uuid-route"
        assert scoped_runtime["materialized_by"] == "xray_runtime_bindings"


def test_xray_binding_materialization_writes_runtime_metadata(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    register_extended_handlers(get_default_job_manager())
    _patch_runtime(monkeypatch)
    monkeypatch.setattr(
        subject_policy_service,
        "build_runtime_enforcement_state",
        lambda: {
            "supported_modes": {"direct": True, "selective": False, "vpn": True},
            "enforcement_level": "global_vpn_enforced",
            "traffic_enforcement_guaranteed": True,
        },
    )
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [{"id": "uuid-materialized", "email": "materialized@example.test"}])
    adapter = _build_adapter(tmp_path, runner=_FakeRunner())
    _patch_xray_adapters(monkeypatch, adapter)
    inventory_service.sync_subject_inventory(
        requested_by="pytest",
        discover_docker=False,
        discover_tailscale=False,
        discover_xray=True,
    )
    _seed_server("server-1")
    _seed_routing_state(desired_mode="vpn", active_auto_server_id="server-1")

    app = create_app(enable_startup_tasks=False)
    with TestClient(app) as client:
        response = client.post(
            "/api/v2/subjects/xray:uuid-materialized/server-override",
            json={"server_id": "server-1", "requested_by": "pytest"},
        )
        assert response.status_code == 200

    materialized_subject = get_subject_with_effective_state("xray:uuid-materialized")
    assert materialized_subject is not None
    assert materialized_subject["effective_state"]["scoped_runtime"]["status"] in [
        "applied",
        "pending_unresolved_subject_match",
    ]
    config_payload = json.loads(config_path.read_text(encoding="utf-8"))
    client_payload = config_payload["inbounds"][0]["settings"]["clients"][0]
    # skip fwrouterBinding check
    assert client_payload["fwrouterBinding"]["subject_id"] == "xray:uuid-materialized"


def test_xray_collects_vpn_auto_bindings_without_active_auto_server_id(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    register_extended_handlers(get_default_job_manager())
    _patch_runtime(monkeypatch)
    monkeypatch.setattr(
        subject_policy_service,
        "build_runtime_enforcement_state",
        lambda: {
            "supported_modes": {"direct": True, "selective": False, "vpn": True},
            "enforcement_level": "global_vpn_enforced",
            "traffic_enforcement_guaranteed": True,
        },
    )
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [{"id": "uuid-auto", "email": "auto@example.test"}])
    adapter = _build_adapter(tmp_path, runner=_FakeRunner())
    _patch_xray_adapters(monkeypatch, adapter)
    inventory_service.sync_subject_inventory(
        requested_by="pytest",
        discover_docker=False,
        discover_tailscale=False,
        discover_xray=True,
    )
    _seed_routing_state(desired_mode="vpn", active_auto_server_id=None)

    bindings = xray_service.collect_xray_runtime_bindings()

    assert len(bindings) == 1
    assert bindings[0]["subject_id"] == "xray:uuid-auto"
    assert bindings[0]["selected_server_id"] == "vpn-global"
    assert bindings[0]["selected_server_source"] == "vpn_auto"
    subject = get_subject_with_effective_state("xray:uuid-auto")
    assert subject is not None
    scoped_runtime = subject["effective_state"]["scoped_runtime"]
    assert scoped_runtime["selected_server_id"] == "vpn-global"
    assert scoped_runtime["status"] == "pending_unresolved_subject_match"


def test_xray_manual_only_custom_proxy_is_fixed_logical_handoff(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    register_extended_handlers(get_default_job_manager())
    _patch_runtime(monkeypatch)
    monkeypatch.setattr(
        subject_policy_service,
        "build_runtime_enforcement_state",
        lambda: {
            "supported_modes": {"direct": True, "selective": False, "vpn": True},
            "enforcement_level": "global_vpn_enforced",
            "traffic_enforcement_guaranteed": True,
        },
    )
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [{"id": "uuid-custom", "email": "custom@example.test"}])
    adapter = _build_adapter(tmp_path, runner=_FakeRunner())
    _patch_xray_adapters(monkeypatch, adapter)
    inventory_service.sync_subject_inventory(
        requested_by="pytest",
        discover_docker=False,
        discover_tailscale=False,
        discover_xray=True,
    )
    _seed_server(
        "custom-https:manual",
        server_name="Manual Proxy",
        raw={"name": "Manual Proxy", "type": "socks5", "server": "proxy.example.test", "port": 1080},
    )
    with db_session() as connection:
        connection.execute(
            """
            UPDATE server_preferences
            SET vpn_auto = 1,
                vpn_auto_priority = -1,
                vpn_auto_priority_origin = 'manual',
                global_list = 1
            WHERE server_id = 'custom-https:manual'
            """
        )
        connection.execute(
            """
            INSERT INTO server_custom_https_proxy (server_id, proxy_type, host, port)
            VALUES ('custom-https:manual', 'socks5', 'proxy.example.test', 1080)
            """
        )
        connection.execute(
            """
            INSERT INTO subject_server_overrides (
                subject_id,
                selected_server_id,
                selected_until,
                apply_state
            )
            VALUES ('xray:uuid-custom', 'custom-https:manual', datetime('now', '+24 hours'), 'clean')
            """
        )
    _seed_routing_state(desired_mode="vpn", active_auto_server_id=None)

    bindings = xray_service.collect_xray_runtime_bindings()

    assert len(bindings) == 1
    assert bindings[0]["subject_id"] == "xray:uuid-custom"
    assert bindings[0]["selected_server_id"] == "custom-https:manual"
    assert bindings[0]["selected_server_source"] == "subject_override"
    assert bindings[0]["handoff_proxy_name"] == "Manual Proxy"
    assert bindings[0]["server_runtime_name"] == "Manual Proxy"
    assert bindings[0]["handoff"]["outbound_tag"].startswith("fwrouter-egress-")
    subject = get_subject_with_effective_state("xray:uuid-custom")
    assert subject is not None
    scoped_runtime = subject["effective_state"]["scoped_runtime"]
    assert scoped_runtime["selected_server_id"] == "custom-https:manual"
    assert scoped_runtime["selected_server_source"] == "subject_override"


def test_xray_collects_bindings_without_per_subject_runtime_snapshot_calls(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    register_extended_handlers(get_default_job_manager())
    _patch_runtime(monkeypatch)
    monkeypatch.setattr(
        subject_policy_service,
        "build_runtime_enforcement_state",
        lambda: {
            "supported_modes": {"direct": True, "selective": False, "vpn": True},
            "enforcement_level": "global_vpn_enforced",
            "traffic_enforcement_guaranteed": True,
        },
    )
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [{"id": "uuid-fast", "email": "fast@example.test"}])
    adapter = _build_adapter(tmp_path, runner=_FakeRunner())
    _patch_xray_adapters(monkeypatch, adapter)
    inventory_service.sync_subject_inventory(
        requested_by="pytest",
        discover_docker=False,
        discover_tailscale=False,
        discover_xray=True,
    )
    _seed_routing_state(desired_mode="vpn", active_auto_server_id=None)
    monkeypatch.setattr(
        xray_service,
        "get_subject_with_effective_state",
        lambda subject_id: (_ for _ in ()).throw(AssertionError("legacy path must not be used")),
    )

    bindings = xray_service.collect_xray_runtime_bindings()

    assert len(bindings) == 1
    assert bindings[0]["subject_id"] == "xray:uuid-fast"


def test_xray_user_override_is_forbidden(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [{"id": "uuid-forbidden", "email": "forbidden@example.test"}])
    adapter = _build_adapter(tmp_path, runner=_FakeRunner())
    _patch_xray_adapters(monkeypatch, adapter)
    inventory_service.sync_subject_inventory(
        requested_by="pytest",
        discover_docker=False,
        discover_tailscale=False,
        discover_xray=True,
    )

    result = set_subject_mode("xray:uuid-forbidden", "vpn", actor_scope="user", requested_by="pytest")

    assert result["ok"] is False
    assert result["code"] == "SUBJECT_MODE_FORBIDDEN"


def test_list_subjects_with_effective_state_includes_xray(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [{"id": "uuid-list", "email": "list@example.test"}])
    adapter = _build_adapter(tmp_path, runner=_FakeRunner())
    _patch_xray_adapters(monkeypatch, adapter)
    inventory_service.sync_subject_inventory(
        requested_by="pytest",
        discover_docker=False,
        discover_tailscale=False,
        discover_xray=True,
    )

    subjects = list_subjects_with_effective_state(subject_type="xray")

    assert len(subjects) == 1
    assert subjects[0]["lifecycle"]["visible_in_ui"] is True
    assert subjects[0]["effective_state"]["effective_mode"] == "forced_vpn"


def test_xray_writer_guard_is_reentrant_and_serializes_threads(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    entered = threading.Event()
    release = threading.Event()
    second_entered = threading.Event()

    def first() -> None:
        with xray_writer_guard():
            with xray_writer_guard():
                entered.set()
                assert release.wait(2)

    def second() -> None:
        assert entered.wait(2)
        with xray_writer_guard():
            second_entered.set()

    first_thread = threading.Thread(target=first)
    second_thread = threading.Thread(target=second)
    first_thread.start()
    second_thread.start()
    assert entered.wait(2)
    assert not second_entered.wait(0.05)
    release.set()
    first_thread.join(2)
    second_thread.join(2)

    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert second_entered.is_set()
    lock_path = get_settings().paths.run_dir / "xray-writer.lock"
    assert lock_path.exists()
    assert lock_path.stat().st_mode & 0o777 == 0o600


def test_xray_writer_guard_serializes_processes(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    script = (
        "from fwrouter_api.adapters.xray_common import xray_writer_guard\n"
        "print('ready', flush=True)\n"
        "with xray_writer_guard():\n    print('acquired', flush=True)\n"
    )
    child = None
    try:
        with xray_writer_guard():
            child = subprocess.Popen(
                [sys.executable, "-c", script],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
            )
            assert child.stdout is not None
            assert child.stdout.readline().strip() == "ready"
            assert child.poll() is None
        assert child is not None
        stdout, stderr = child.communicate(timeout=3)
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
            child.wait(timeout=3)
    assert child.returncode == 0, stderr
    assert stdout.strip() == "acquired"


def test_explicit_xray_modes_are_scoped_direct_or_fail_closed(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    config_path, _ = _xray_paths()
    clients = [
        {"id": "uuid-direct", "email": "direct@example.test"},
        {"id": "uuid-disabled", "email": "disabled@example.test"},
        {"id": "uuid-selective", "email": "selective@example.test"},
        {"id": "uuid-vpn", "email": "vpn@example.test"},
    ]
    _write_xray_config(config_path, clients)
    adapter = _build_adapter(tmp_path, runner=_FakeRunner())
    result = adapter.materialize_client_bindings(
        [{"subject_id": "vpn-subject", "client_email": "vpn@example.test", "selected_server_id": "logical-vpn"},
         {"subject_id": "stale-vpn-subject", "client_email": "disabled@example.test", "selected_server_id": "logical-vpn"}],
        client_modes=[
            {"client_email": "direct@example.test", "effective_mode": "direct"},
            {"client_email": "disabled@example.test", "effective_mode": "disabled"},
            {"client_email": "selective@example.test", "effective_mode": "unsupported_selective"},
        ],
    )
    assert result.ok is True
    config = json.loads(config_path.read_text(encoding="utf-8"))
    outbounds = {item.get("tag") for item in config["outbounds"]}
    assert XRAY_EXPLICIT_DIRECT_OUTBOUND_TAG in outbounds
    assert XRAY_FALLBACK_OUTBOUND_TAG in outbounds
    rules = config["routing"]["rules"]
    assert rules[1]["user"] == ["direct@example.test"]
    assert rules[2]["user"] == ["disabled@example.test"]
    assert rules[3]["user"] == ["selective@example.test"]
    assert rules[4]["user"] == ["vpn@example.test"]
    assert rules[5]["user"] == ["disabled@example.test"]
    assert rules.index(next(rule for rule in rules if rule.get("user") == ["disabled@example.test"])) < rules.index(next(rule for rule in rules if rule.get("user") == ["disabled@example.test"] and rule.get("outboundTag", "").startswith("fwrouter-egress-")))
    for email, expected in (
        ("direct@example.test", XRAY_EXPLICIT_DIRECT_OUTBOUND_TAG),
        ("disabled@example.test", XRAY_FALLBACK_OUTBOUND_TAG),
        ("selective@example.test", XRAY_FALLBACK_OUTBOUND_TAG),
    ):
        assert any(
            rule.get("inboundTag") == ["vless-ws"]
            and rule.get("user") == [email]
            and rule.get("outboundTag") == expected
            for rule in rules
        )
    assert not any(
        rule.get("inboundTag") != ["vless-ws"]
        and ("direct@example.test" in str(rule) or "disabled@example.test" in str(rule))
        for rule in rules
    )
    verification = xray_materialize_service._verify_active_config_client_modes([
        {"subject_id": "direct", "client_email": "direct@example.test", "effective_mode": "direct"},
        {"subject_id": "disabled", "client_email": "disabled@example.test", "effective_mode": "disabled"},
        {"subject_id": "selective", "client_email": "selective@example.test", "effective_mode": "unsupported_selective"},
    ])
    assert verification["ok"] is True


def test_new_explicit_xray_mode_writes_allow_vpn_disabled_and_enabled_alias_only() -> None:
    from fwrouter_api.services.apply_orchestrator_commits import _validate_subject_admin_mode

    subject = {"subject_type": "explicit_external_client", "implementation_kind": "xray"}
    assert _validate_subject_admin_mode(subject, "vpn") is None
    assert _validate_subject_admin_mode(subject, "disabled") is None
    assert _validate_subject_admin_mode(subject, "enabled") is None
    assert _validate_subject_admin_mode(subject, "direct")["code"] == "SUBJECT_MODE_UNSUPPORTED"
    assert _validate_subject_admin_mode(subject, "selective")["code"] == "SUBJECT_MODE_UNSUPPORTED"
