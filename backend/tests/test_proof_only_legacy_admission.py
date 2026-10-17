from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from fwrouter_api.adapters.xray_common import xray_writer_guard
from fwrouter_api.db.connection import db_session
from fwrouter_api.services import subscription, xray_subscription_service


CONFIRMATION = "PROOF_ONLY_LEGACY_ADMISSION"


def test_synthetic_vpn_enable_requires_only_owned_loopback_source_before_public_call(monkeypatch, tmp_path):
    """The acceptance lifecycle helper must reject any non-owned refresh input before API mutation."""
    import sqlite3

    tests_root = Path(__file__).resolve().parents[2] / "tests"
    monkeypatch.syspath_prepend(str(tests_root))
    from application_acceptance import xray_support

    database = tmp_path / "fwrouter.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            "CREATE TABLE subscription_state (id INTEGER PRIMARY KEY, url TEXT, metadata_json TEXT);"
            "CREATE TABLE provider_bindings (source_ref TEXT, enabled INTEGER);"
        )
        connection.execute(
            "INSERT INTO subscription_state(id, url, metadata_json) VALUES(1, ?, ?)",
            ("http://127.0.0.1:49001/synthetic-subscription", json.dumps({
                "subscriptions": {"items": [{
                    "url": "http://127.0.0.1:49001/synthetic-subscription", "enabled": True,
                }]},
            })),
        )

    calls = []
    monkeypatch.setattr(xray_support, "http_json", lambda *args, **kwargs: calls.append((args, kwargs)))
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE subscription_state SET url=?, metadata_json=? WHERE id=1",
            ("http://198.51.100.9/profile", json.dumps({"subscriptions": {"items": [{
                "url": "http://198.51.100.9/profile", "enabled": True,
            }]}})),
        )
    with pytest.raises(AssertionError, match="not limited"):
        xray_support.enable_vpn_from_synthetic_subscription(
            "http://127.0.0.1:9000/api/v2", tmp_path, "http://127.0.0.1:49001/synthetic-subscription",
        )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE subscription_state SET url=?, metadata_json=? WHERE id=1",
            ("http://127.0.0.1:49001/synthetic-subscription", json.dumps({
                "subscriptions": {"items": [
                    {"url": "http://127.0.0.1:49001/synthetic-subscription", "enabled": True},
                    {"url": "http://198.51.100.9/disabled-profile", "enabled": False},
                ]},
            })),
        )
    with pytest.raises(AssertionError, match="not limited"):
        xray_support.enable_vpn_from_synthetic_subscription(
            "http://127.0.0.1:9000/api/v2", tmp_path, "http://127.0.0.1:49001/synthetic-subscription",
        )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE subscription_state SET url=?, metadata_json=? WHERE id=1",
            ("http://127.0.0.1:49001/synthetic-subscription", json.dumps({
                "subscriptions": {"items": [{
                    "url": "http://127.0.0.1:49001/synthetic-subscription", "enabled": True,
                }]},
            })),
        )
        connection.execute("INSERT INTO provider_bindings(source_ref, enabled) VALUES('synthetic', 1)")
    with pytest.raises(AssertionError, match="enabled provider binding"):
        xray_support.enable_vpn_from_synthetic_subscription(
            "http://127.0.0.1:9000/api/v2", tmp_path, "http://127.0.0.1:49001/synthetic-subscription",
        )
    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM provider_bindings")
    with pytest.raises(AssertionError, match="owned loopback worker"):
        xray_support.enable_vpn_from_synthetic_subscription(
            "https://api.example.invalid/api/v2", tmp_path,
            "http://127.0.0.1:49001/synthetic-subscription",
        )
    assert calls == []


def test_synthetic_vpn_enable_uses_public_managed_module_lifecycle(monkeypatch, tmp_path):
    """The native cohort enables its real `vpn` owner through the public API, never direct DB writes."""
    import sqlite3

    tests_root = Path(__file__).resolve().parents[2] / "tests"
    monkeypatch.syspath_prepend(str(tests_root))
    from application_acceptance import xray_support

    database = tmp_path / "fwrouter.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            "CREATE TABLE subscription_state (id INTEGER PRIMARY KEY, url TEXT, metadata_json TEXT);"
            "CREATE TABLE provider_bindings (source_ref TEXT, enabled INTEGER);"
        )
        connection.execute(
            "INSERT INTO subscription_state(id, url, metadata_json) VALUES(1, ?, ?)",
            ("http://127.0.0.1:49001/synthetic-subscription", json.dumps({
                "subscriptions": {"items": [{
                    "url": "http://127.0.0.1:49001/synthetic-subscription", "enabled": True,
                }]},
            })),
        )

    responses = [
        (200, {"ok": True, "data": {"module": {
            "module_name": "vpn", "desired_state": "disabled", "lifecycle_mode": "managed",
        }}}),
        (200, {"ok": True, "data": {"job": {"job_id": "synthetic-job"}}}),
        (200, {"ok": True, "data": {"module": {
            "module_name": "vpn", "desired_state": "enabled", "lifecycle_mode": "managed",
            "runtime_state": "running", "apply_state": "clean",
        }}}),
    ]
    calls = []

    def fake_http_json(url, **kwargs):
        calls.append((url, kwargs))
        return responses.pop(0)

    def fake_await_job(api, response, **kwargs):
        assert response.get("data", {}).get("job", {}).get("job_id") == "synthetic-job"
        assert kwargs == {}
        return {"status": "success"}

    monkeypatch.setattr(xray_support, "http_json", fake_http_json)
    monkeypatch.setattr(xray_support, "await_job", fake_await_job)
    xray_support.enable_vpn_from_synthetic_subscription(
        "http://127.0.0.1:9000/api/v2", tmp_path, "http://127.0.0.1:49001/synthetic-subscription",
    )
    assert [url.rsplit("/api/v2", 1)[-1] for url, _ in calls] == [
        "/modules/vpn", "/modules/vpn/desired-state", "/modules/vpn",
    ]
    assert not responses


def _seed_legacy_inventory() -> str:
    with db_session() as connection:
        connection.execute(
            "INSERT INTO modules (module_name, desired_state, lifecycle_mode, runtime_state, apply_state) "
            "VALUES ('core', 'enabled', 'managed', 'running', 'clean') "
            "ON CONFLICT(module_name) DO UPDATE SET desired_state='enabled', lifecycle_mode='managed', "
            "runtime_state='running', apply_state='clean'",
        )
        connection.execute(
            "INSERT INTO routing_global_state (id, desired_mode, applied_mode, apply_state) "
            "VALUES (1, 'direct', 'direct', 'clean') "
            "ON CONFLICT(id) DO UPDATE SET desired_mode='direct', applied_mode='direct', apply_state='clean'",
        )
        connection.execute(
            "INSERT INTO apply_versions (apply_id, manifest_path, status, created_at) "
            "VALUES ('synthetic-applied-generation', '/tmp/manifest.json', 'applied', '2099') "
            "ON CONFLICT(apply_id) DO UPDATE SET status='applied', created_at='2099'",
        )
        connection.execute(
            """INSERT INTO servers (server_id, server_name, provider_name, raw_json, inventory_state)
               VALUES ('legacy-admission-node', 'Legacy admission node', 'subscription', '{}', 'active')"""
        )
        connection.execute(
            """INSERT INTO subscription_server_memberships
               (source_id, server_id, source_url, entry_identity_hash, parser_format, is_active)
               VALUES ('src:legacy-admission', 'legacy-admission-node',
                       'http://127.0.0.1:9/fixture', 'fixture-entry', 'fixture', 1)"""
        )
        return subscription._subscription_inventory_digest(connection)[0]


def _seed_proof_native_module_rows(*, vpn_desired="enabled", vpn_lifecycle="managed", include_vpn=True):
    """Seed only real module ownership rows; Mihomo is owned by the `vpn` module."""
    with db_session() as connection:
        connection.execute(
            "INSERT INTO modules (module_name, desired_state, lifecycle_mode, runtime_state, apply_state) "
            "VALUES ('xray', 'enabled', 'managed', 'running', 'clean') "
            "ON CONFLICT(module_name) DO UPDATE SET desired_state='enabled', lifecycle_mode='managed', "
            "runtime_state='running', apply_state='clean'",
        )
        if include_vpn:
            connection.execute(
                "INSERT INTO modules (module_name, desired_state, lifecycle_mode, runtime_state, apply_state) "
                "VALUES ('vpn', ?, ?, 'running', 'clean') "
                "ON CONFLICT(module_name) DO UPDATE SET desired_state=excluded.desired_state, "
                "lifecycle_mode=excluded.lifecycle_mode, runtime_state='running', apply_state='clean'",
                (vpn_desired, vpn_lifecycle),
            )
        else:
            connection.execute("DELETE FROM modules WHERE module_name='vpn'")
        connection.execute("DELETE FROM modules WHERE module_name='mihomo'")


def _write_applied_manifest_via_existing_owner(monkeypatch, tmp_path, *, plan_id, routing):
    """Use the production materializer and artifact promoter under a test-owned temp root."""
    from types import SimpleNamespace
    from fwrouter_api.services import apply_manifest, dataplane_global, dataplane_nft_artifacts

    paths = SimpleNamespace(
        generated_dir=tmp_path / "generated",
        state_dir=tmp_path / "state",
        jobs_dir=tmp_path / "jobs",
    )
    settings = SimpleNamespace(paths=paths)
    monkeypatch.setattr(dataplane_nft_artifacts, "get_settings", lambda: settings)
    monkeypatch.setattr(dataplane_global, "get_settings", lambda: settings)
    manifest = apply_manifest._materialize_manifest(
        prebuilt_manifest={"routing_global_state": routing},
        plan_id=plan_id,
        reason="synthetic_proof_test",
        input_data={"scope": "test-owned"},
    )
    artifact_paths = dataplane_nft_artifacts.write_candidate_artifacts(
        job_id="synthetic-proof-job",
        apply_id=plan_id,
        manifest=manifest,
        renderer=lambda _manifest: "table inet fwrouter_v2 {\n}\n",
    )
    dataplane_nft_artifacts.promote_last_good(manifest=manifest, artifact_paths=artifact_paths)
    loaded = dataplane_global._read_applied_manifest_uncached()
    assert loaded == manifest
    assert loaded.get("plan_id") == plan_id
    assert "apply_id" not in loaded
    return loaded


def test_acceptance_worker_proof_only_routes_use_real_service_owned_xray_helper(monkeypatch, tmp_path, capsys):
    """Execute both nested worker route callables with owned native boundary doubles."""
    import ast
    from concurrent.futures import ThreadPoolExecutor

    tests_root = Path(__file__).resolve().parents[2] / "tests"
    monkeypatch.syspath_prepend(str(tests_root))
    from application_acceptance import worker
    from fwrouter_api.services import bootstrap, xray, xray_subscription_service
    from fwrouter_api.services import subscription as subscription_service

    build_app = next(
        node for node in ast.parse(Path(worker.__file__).read_text()).body
        if isinstance(node, ast.FunctionDef) and node.name == "build_app"
    )
    nested = {
        node.name: node for node in build_app.body
        if isinstance(node, ast.FunctionDef)
        and node.name in {
            "_run_legacy_admission_mutation_tripwires",
            "with_legacy_admission_mutation_tripwires",
            "admit_legacy_inventory_for_acceptance",
            "proof_only_tripwire_self_check",
            "recover_startup_xray_subscription_generation",
        }
    }
    assert set(nested) == {
        "_run_legacy_admission_mutation_tripwires",
        "with_legacy_admission_mutation_tripwires",
        "admit_legacy_inventory_for_acceptance",
        "proof_only_tripwire_self_check",
        "recover_startup_xray_subscription_generation",
    }

    class SyntheticXrayAdapter:
        _runner = staticmethod(lambda _action, _request: None)

        def _run(self, action):
            return self._runner(action, {})

    class SyntheticDataplaneAdapter:
        apply = staticmethod(lambda _manifest: None)
        rollback = staticmethod(lambda _manifest: None)

    adapter = SyntheticXrayAdapter()
    monkeypatch.setattr(xray, "DEFAULT_XRAY_ADAPTER", adapter)
    monkeypatch.setattr(bootstrap, "recover_startup_xray_subscription_profiles", lambda: {
        "ok": False, "status": "blocked", "stage": "generation_recovery",
        "error_code": "XRAY_PROOF_ONLY_CHECKPOINT_PENDING",
    })
    monkeypatch.setattr(subscription_service, "get_subscription_native_generation_state", lambda: {
        "status": "pending", "inventory_authority": "proof_only_pending",
    })
    checkpoint = tmp_path / "generation-checkpoint.json"
    checkpoint.write_text(json.dumps({"operation_mode": "proof_only_legacy_admission"}))
    checkpoint.chmod(0o600)
    monkeypatch.setattr(xray_subscription_service, "_xray_generation_checkpoint_path", lambda _adapter: checkpoint)
    monkeypatch.setenv("FWROUTER_ENVIRONMENT", "test")
    monkeypatch.setenv("FWROUTER_APPLICATION_ACCEPTANCE_ROOT", "/tmp/fwrouter-application-acceptance")

    namespace = {
        "Any": object,
        "Path": Path,
        "os": os,
        "stat": stat,
        "json": json,
        "_acceptance_xray_adapter": worker._acceptance_xray_adapter,
        "threading": __import__("threading"),
        "_LEGACY_ADMISSION_MUTATION_LOCK": worker._LEGACY_ADMISSION_MUTATION_LOCK,
        "re": __import__("re"),
    }
    exec(compile(ast.Module(body=list(nested.values()), type_ignores=[]), worker.__file__, "exec"), namespace)

    secret_canary = "SYNTHETIC-SECRET-CANARY-never-output"
    monkeypatch.setattr(xray_subscription_service, "admit_legacy_subscription_inventory", lambda **_kwargs: {
        "ok": False, "status": "blocked", "reason": "synthetic_native_proof_unavailable",
    })
    monkeypatch.setattr(xray_subscription_service, "inspect_legacy_subscription_inventory_admission", lambda: {
        "ok": True, "eligible": False, "action": "blocked", "reason": "synthetic_blocked",
        "generation_status": "unknown", "generation_reason": secret_canary,
        "checkpoint_mode": None, "checkpoint_phase": None,
    })
    original_tripwires = namespace["with_legacy_admission_mutation_tripwires"]
    namespace["with_legacy_admission_mutation_tripwires"] = lambda operation, receipt_observation=None: (
        operation(), {"xray_apply_or_reload": 0, "mihomo_apply_or_reload": 0,
                      "core_dataplane_apply_or_rollback": 0, "unexpected_http_request": 0}, False,
    )
    admission_response = namespace["admit_legacy_inventory_for_acceptance"]({
        "expected_digest": "a" * 64,
    })
    assert admission_response["reason"] == "synthetic_native_proof_unavailable"
    assert admission_response["entry_facts"] == {
        "eligible": False, "action": "blocked", "reason": "synthetic_blocked",
        "generation_status": "unknown", "checkpoint_mode": None, "checkpoint_phase": None,
    }
    assert secret_canary not in repr(admission_response)
    admission_log = capsys.readouterr().out
    assert admission_log == (
        'FWROUTER_ACCEPTANCE_LEGACY_ADMISSION '
        '{"ok":false,"reason":"unknown","schema":"fwrouter-acceptance-legacy-admission/v1",'
        '"status":"blocked"}\n'
    )
    monkeypatch.setattr(xray_subscription_service, "admit_legacy_subscription_inventory", lambda **_kwargs: {
        "ok": False, "status": "blocked", "reason": "XrayAdapterError",
    })
    namespace["admit_legacy_inventory_for_acceptance"]({"expected_digest": "b" * 64})
    assert '"reason":"XrayAdapterError"' in capsys.readouterr().out
    namespace["with_legacy_admission_mutation_tripwires"] = original_tripwires

    from fwrouter_api.services import apply as core_apply, mihomo_runtime
    monkeypatch.setattr(core_apply, "DEFAULT_DATAPLANE_ADAPTER", SyntheticDataplaneAdapter())
    monkeypatch.setattr(core_apply, "_apply_global_mode_hot_swap", lambda: None)
    monkeypatch.setattr(mihomo_runtime, "_run_compose_command", lambda _argv: None)

    tripwire_result = namespace["proof_only_tripwire_self_check"]()
    assert tripwire_result["ok"] is True
    assert all(tripwire_result["intercepts"].values())

    from types import SimpleNamespace
    from fwrouter_api.services import mihomo_reconcile, mihomo_reconcile_fingerprint

    fingerprint_hash = "a" * 64
    source_fingerprint = "b" * 64
    active_config_hash = "c" * 64
    runtime_incarnation = "synthetic-mihomo-incarnation"
    native_proof_calls = []
    receipt_write_calls = []
    fence_facts = {
        "xray_incarnation_match": True,
        "xray_runtime_config_match": True,
        "xray_active_config_match": True,
        "mihomo_incarnation_match": True,
        "mihomo_active_config_match": True,
        "mihomo_input_fingerprint_match": True,
        "core_runtime_fingerprint_match": True,
        "core_state_available": True,
        "core_native_proof_ok": True,
        "core_manifest_match": True,
        "core_apply_id_match": False,
        "core_enforcement_match": True,
        "core_live_mode_match": True,
    }
    fence_view = SimpleNamespace()
    proof = {
        "mihomo_native_policy_match": True,
        "mihomo_fingerprint_hash": fingerprint_hash,
        "source_fingerprint": source_fingerprint,
        "mihomo_config_sha256": active_config_hash,
        "mihomo_runtime_incarnation": runtime_incarnation,
    }
    monkeypatch.setattr(xray_subscription_service, "_legacy_admission_runtime_fence_facts",
                        lambda _view: dict(fence_facts))
    monkeypatch.setattr(xray_subscription_service, "_legacy_admission_source_fingerprint",
                        lambda _connection=None: source_fingerprint)
    monkeypatch.setattr(xray_subscription_service, "xray_writer_guard_is_held", lambda: True)

    def synthetic_legacy_publish(synthetic_proof, operation_id):
        if not concurrent_ordinary_results:
            native_before = len(native_proof_calls)
            writes_before = len(receipt_write_calls)
            # Exercise an unrelated receipt publication while admission's
            # temporary diagnostic wrappers are installed process-wide.
            with ThreadPoolExecutor(max_workers=1) as executor:
                concurrent_ordinary_results.append(executor.submit(
                    xray_subscription_service._publish_mihomo_udp_receipt_with_owner,
                    expected_mihomo_runtime=runtime_incarnation,
                    expected_mihomo_sha=active_config_hash,
                    reconcile_reason="ordinary_concurrent_publish",
                    operation_id="synthetic-independent-operation",
                    owner_matches=lambda _fingerprint: True,
                ).result())
            concurrent_ordinary_stage_counts.append((
                len(native_proof_calls) - native_before,
                len(receipt_write_calls) - writes_before,
            ))
        return xray_subscription_service._publish_mihomo_udp_receipt_with_owner(
            expected_mihomo_runtime=runtime_incarnation,
            expected_mihomo_sha=active_config_hash,
            reconcile_reason="proof_only_legacy_admission",
            operation_id=operation_id,
            owner_matches=lambda fingerprint: (
                fingerprint == fingerprint_hash
                and xray_subscription_service._legacy_admission_source_fingerprint() == source_fingerprint
                and xray_subscription_service._legacy_admission_runtime_fences_match(fence_view)
            ),
        )

    concurrent_ordinary_results = []
    concurrent_ordinary_stage_counts = []

    # The actual shared publisher and its native-proof/write boundaries are
    # wrapped by the test worker; the controlled doubles keep this unit test
    # independent from processes, controllers and external networks.
    monkeypatch.setattr(xray_subscription_service, "_publish_legacy_admission_mihomo_receipt",
                        synthetic_legacy_publish)
    monkeypatch.setattr(mihomo_reconcile_fingerprint, "current_mihomo_input_fingerprint",
                        lambda: {"hash": fingerprint_hash})
    def synthetic_native_udp_proof():
        native_proof_calls.append(True)
        return {
            "ok": True, "native_top_level_rules_match": True,
            "selector_membership_matches": True, "guarded_rules_match": True,
        }

    monkeypatch.setattr(mihomo_reconcile, "_native_udp_policy_proof", synthetic_native_udp_proof)
    monkeypatch.setattr(mihomo_reconcile_fingerprint, "active_config_hash", lambda: active_config_hash)
    monkeypatch.setattr(mihomo_reconcile_fingerprint, "_current_runtime_incarnation",
                        lambda: runtime_incarnation)

    def synthetic_write_receipt(**_kwargs):
        # Model the existing writer's one-pass active-config/incarnation reads;
        # the actual hosted worker wrapper observes these return values.
        receipt_write_calls.append(True)
        mihomo_reconcile_fingerprint.active_config_hash()
        mihomo_reconcile_fingerprint._current_runtime_incarnation()
        return False

    monkeypatch.setattr(mihomo_reconcile_fingerprint, "write_mihomo_reconcile_fingerprint_state",
                        synthetic_write_receipt)

    def synthetic_admission(**_kwargs):
        written = xray_subscription_service._publish_legacy_admission_mihomo_receipt(proof, "synthetic-op")
        return {
            "ok": False, "status": "blocked",
            "reason": "native_receipt_unverified" if not written else "synthetic-unexpected-success",
        }

    monkeypatch.setattr(xray_subscription_service, "admit_legacy_subscription_inventory", synthetic_admission)
    failed_gate = namespace["admit_legacy_inventory_for_acceptance"]({"expected_digest": "d" * 64})
    assert failed_gate["reason"] == "native_receipt_unverified"
    assert concurrent_ordinary_results == [False]
    assert concurrent_ordinary_stage_counts == [(1, 1)]
    diagnostic = failed_gate["receipt_diagnostic"]
    assert diagnostic["writer_guard_held"] is True
    assert diagnostic["owner_checks"] == [{
        "input_fingerprint_match": True,
        "source_fingerprint_match": True,
        "runtime_fences": fence_facts,
        "owner_result": False,
    }]
    assert "native_policy_proof" not in diagnostic
    assert "durable_write" not in diagnostic
    output = capsys.readouterr().out
    assert "FWROUTER_ACCEPTANCE_LEGACY_RECEIPT_DIAGNOSTIC " in output
    for secret in (fingerprint_hash, source_fingerprint, active_config_hash, runtime_incarnation):
        assert secret not in output
    assert "synthetic-op" not in output

    # With all owner fences passing, the wrapper records the real native-proof
    # and persistence function results without exposing their inputs.
    fence_facts["core_apply_id_match"] = True
    succeeded_to_write = namespace["admit_legacy_inventory_for_acceptance"]({"expected_digest": "e" * 64})
    write_diagnostic = succeeded_to_write["receipt_diagnostic"]
    assert write_diagnostic["owner_checks"][0]["owner_result"] is True
    assert write_diagnostic["owner_checks"][1]["owner_result"] is True
    assert write_diagnostic["native_policy_proof"] == {
        "ok": True, "top_level_rules_match": True,
        "selector_membership_match": True, "guarded_rules_match": True,
    }
    assert write_diagnostic["durable_write"] == {
        "input_fingerprint_match": True, "active_config_match": True,
        "runtime_incarnation_match": True, "native_proof_complete": True,
        "write_result": False,
    }
    output = capsys.readouterr().out
    for secret in (fingerprint_hash, source_fingerprint, active_config_hash, runtime_incarnation):
        assert secret not in output

    recovery_result = namespace["recover_startup_xray_subscription_generation"]()
    assert recovery_result["ok"] is False
    assert recovery_result["tripwire_armed"] is True
    assert recovery_result["error_code"] == "XRAY_PROOF_ONLY_CHECKPOINT_PENDING"
    assert set(recovery_result["mutation_attempts"].values()) == {0}

    # Two synchronous FastAPI requests execute on different worker threads.
    # Their process-global monkeypatch windows must never overlap.
    from fwrouter_api.services import mihomo_reconcile, mihomo_reconcile_fingerprint
    from fwrouter_api.services import xray_subscription_service
    original_receipt_functions = (
        xray_subscription_service._publish_legacy_admission_mihomo_receipt,
        xray_subscription_service._publish_mihomo_udp_receipt_with_owner,
        mihomo_reconcile._native_udp_policy_proof,
        mihomo_reconcile_fingerprint.write_mihomo_reconcile_fingerprint_state,
    )
    state_lock = __import__("threading").Lock()
    first_entered = __import__("threading").Event()
    second_entered = __import__("threading").Event()
    active_calls = 0
    maximum_active_calls = 0

    def tracked_operation(*, first):
        nonlocal active_calls, maximum_active_calls
        with state_lock:
            active_calls += 1
            maximum_active_calls = max(maximum_active_calls, active_calls)
        try:
            if first:
                first_entered.set()
                second_entered.wait(timeout=0.2)
            else:
                second_entered.set()
            return first
        finally:
            with state_lock:
                active_calls -= 1

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_call = executor.submit(
            namespace["with_legacy_admission_mutation_tripwires"],
            lambda: tracked_operation(first=True),
        )
        assert first_entered.wait(timeout=1)
        second_call = executor.submit(
            namespace["with_legacy_admission_mutation_tripwires"],
            lambda: tracked_operation(first=False),
        )
        first_result = first_call.result(timeout=2)
        second_result = second_call.result(timeout=2)
    assert first_result[2] is False and second_result[2] is False
    assert maximum_active_calls == 1
    assert original_receipt_functions == (
        xray_subscription_service._publish_legacy_admission_mihomo_receipt,
        xray_subscription_service._publish_mihomo_udp_receipt_with_owner,
        mihomo_reconcile._native_udp_policy_proof,
        mihomo_reconcile_fingerprint.write_mihomo_reconcile_fingerprint_state,
    )


def test_acceptance_worker_xray_candidate_diagnostic_is_bounded_and_redacted(monkeypatch, tmp_path, capsys):
    """The actual staging wrapper may report only bounded synthetic hashes and fixed structural labels."""
    import ast
    import hashlib

    tests_root = Path(__file__).resolve().parents[2] / "tests"
    monkeypatch.syspath_prepend(str(tests_root))
    from application_acceptance import worker
    from fwrouter_api.services import apply as core_apply, mihomo_runtime, xray
    from fwrouter_api.services import xray_common

    build_app = next(
        node for node in ast.parse(Path(worker.__file__).read_text()).body
        if isinstance(node, ast.FunctionDef) and node.name == "build_app"
    )
    tripwire_functions = [
        node for node in build_app.body
        if isinstance(node, ast.FunctionDef)
        and node.name in {
            "_run_legacy_admission_mutation_tripwires",
            "with_legacy_admission_mutation_tripwires",
        }
    ]
    namespace = {
        "Any": object,
        "Path": Path,
        "json": json,
        "os": os,
        "hashlib": hashlib,
        "_acceptance_xray_adapter": worker._acceptance_xray_adapter,
        "_safe_xray_candidate_diff_summary": worker._safe_xray_candidate_diff_summary,
        "threading": __import__("threading"),
        "_LEGACY_ADMISSION_MUTATION_LOCK": worker._LEGACY_ADMISSION_MUTATION_LOCK,
    }
    exec(compile(ast.Module(body=tripwire_functions, type_ignores=[]), worker.__file__, "exec"), namespace)

    secret_canary = "SYNTHETIC-UUID-CANARY-8e662f1c-9b30-4f0b-bd84-7f403110fb31"
    active = tmp_path / "active.json"
    staging = tmp_path / "fwrouter-legacy-admission-synthetic"
    staging.mkdir(mode=0o700)
    candidate = staging / "candidate.json"
    active.write_text(json.dumps({
        "inbounds": [{"settings": {"clients": [{"id": secret_canary, "email": "secret@acceptance.invalid"}]}}],
        "outbounds": [{"tag": "proxy", "endpoint": "synthetic.invalid"}],
        "routing": {"rules": [{"user": ["secret@acceptance.invalid"]}]},
        "opaque-canary-key": "never report arbitrary JSON keys",
    }), encoding="utf-8")
    marker = object()

    class SyntheticAdapter:
        config_path = active
        _runner = staticmethod(lambda _action, _request: None)

        def stage_subscription_generation(self, *, candidate_path, **_kwargs):
            Path(candidate_path).write_text(json.dumps({
                "inbounds": [{"settings": {"clients": [{"id": secret_canary, "email": "secret@acceptance.invalid"}]}}],
                "outbounds": [{"tag": "proxy", "endpoint": "changed.synthetic.invalid"}],
                "routing": {"rules": [{"user": ["secret@acceptance.invalid"]}]},
                "opaque-canary-key": "different arbitrary content",
            }), encoding="utf-8")
            return marker

    adapter = SyntheticAdapter()
    monkeypatch.setenv("FWROUTER_ENVIRONMENT", "test")
    monkeypatch.setattr(xray_common, "_xray_adapter", lambda: adapter)
    monkeypatch.setattr(xray, "DEFAULT_XRAY_ADAPTER", adapter)
    monkeypatch.setattr(core_apply, "DEFAULT_DATAPLANE_ADAPTER", type("D", (), {
        "apply": staticmethod(lambda *_args, **_kwargs: None),
        "rollback": staticmethod(lambda *_args, **_kwargs: None),
    })())
    monkeypatch.setattr(core_apply, "_apply_global_mode_hot_swap", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(mihomo_runtime, "_run_compose_command", lambda *_args, **_kwargs: None)

    result, attempts, raised = namespace["with_legacy_admission_mutation_tripwires"](
        lambda: adapter.stage_subscription_generation(candidate_path=candidate),
    )
    assert result is marker and not raised
    assert attempts == {
        "xray_apply_or_reload": 0,
        "mihomo_apply_or_reload": 0,
        "core_dataplane_apply_or_rollback": 0,
        "unexpected_http_request": 0,
    }
    diagnostic = capsys.readouterr().out
    assert "FWROUTER_ACCEPTANCE_XRAY_CANDIDATE_DIFF " in diagnostic
    assert secret_canary not in diagnostic
    assert "secret@acceptance.invalid" not in diagnostic
    assert "opaque-canary-key" not in diagnostic
    assert "changed.synthetic.invalid" not in diagnostic
    decoded = json.loads(diagnostic.split("FWROUTER_ACCEPTANCE_XRAY_CANDIDATE_DIFF ", 1)[1])
    assert decoded["schema"] == "fwrouter-acceptance-xray-candidate-diff/v1"
    assert decoded["equal_bytes"] is False
    assert decoded["active_sha256"] == hashlib.sha256(active.read_bytes()).hexdigest()
    assert decoded["candidate_sha256"] == hashlib.sha256(candidate.read_bytes()).hexdigest()
    assert decoded["changed_sections"] == ["outbounds"]
    assert decoded["changed_paths"] == ["$.outbounds", "$.outbounds[*]", "$.<other_top_level>"]
    assert decoded["other_top_level_changed"] is True
    assert decoded["routing_rules_multiset_equal"] is True
    assert decoded["routing_rules_order_only"] is False
    assert decoded["routing_rules_first_diff_index"] is None
    assert decoded["shape"] == {
        "active": {"inbounds": 1, "inbound_clients": 1, "outbounds": 1, "routing_rules": 1},
        "candidate": {"inbounds": 1, "inbound_clients": 1, "outbounds": 1, "routing_rules": 1},
    }

    # Order is safety-relevant to first-match routing, so the diagnostic may
    # identify an order-only delta but must never itself treat it as equivalent.
    active_rules = [
        {"inboundTag": ["vless-ws"], "user": ["private-one@acceptance.invalid"], "outboundTag": "private-vpn-a"},
        {"inboundTag": ["vless-ws"], "user": ["private-two@acceptance.invalid"], "outboundTag": "private-vpn-b"},
    ]
    candidate_rules = list(reversed(active_rules))
    active.write_text(json.dumps({"routing": {"rules": active_rules}}), encoding="utf-8")
    candidate.write_text(json.dumps({"routing": {"rules": candidate_rules}}), encoding="utf-8")
    order_summary = worker._safe_xray_candidate_diff_summary(active, candidate)
    assert order_summary["routing_rules_multiset_equal"] is True
    assert order_summary["routing_rules_order_only"] is True
    assert order_summary["routing_rules_first_diff_index"] == 0
    assert "private-one@acceptance.invalid" not in json.dumps(order_summary)
    assert "private-vpn-a" not in json.dumps(order_summary)


def _positive_synthetic_proof() -> dict[str, object]:
    from fwrouter_api.services.vpn_auto_selection_state import read_selection_fence
    with db_session() as connection:
        selection = read_selection_fence(connection)
    return {
        "ok": True,
        "xray_config_match": True,
        "xray_loaded_users_match": True,
        "xray_bindings_match": True,
        "xray_modes_match": True,
        "core_selection_match": True,
        "core_native_policy_match": True,
        "mihomo_candidate_match": True,
        "mihomo_native_policy_match": True,
        "source_fingerprint": xray_subscription_service._legacy_admission_source_fingerprint(),
        "core_runtime_fingerprint": xray_subscription_service._legacy_admission_core_runtime_fingerprint(),
        "core_dataplane_proof": {
            "ok": True,
            "manifest_hash": "4" * 64,
            "apply_id": "synthetic-applied-generation",
            "enforcement_level": "global_direct_enforced",
            "traffic_enforcement_guaranteed": True,
            "active_mode_matches_intent": True,
            "live_global_mode": "direct",
        },
        "selection_revision": selection.get("revision"),
        "selection_decision_id": selection.get("decision_id"),
        "xray_runtime_incarnation": "synthetic-xray-incarnation",
        "mihomo_runtime_incarnation": "synthetic-mihomo-incarnation",
        "xray_config_sha256": "1" * 64,
        "mihomo_config_sha256": "2" * 64,
        "mihomo_fingerprint_hash": "3" * 64,
    }


def test_legacy_admission_requires_explicit_confirmation_and_exact_digest(monkeypatch, tmp_path):
    digest = _seed_legacy_inventory()
    adapter = object()
    checkpoint = tmp_path / "not-created-until-eligible" / "generation-checkpoint.json"
    monkeypatch.setattr(xray_subscription_service, "_xray_adapter", lambda: adapter)
    monkeypatch.setattr(xray_subscription_service, "_xray_generation_checkpoint_path", lambda _adapter: checkpoint)
    proof_calls: list[bool] = []
    monkeypatch.setattr(xray_subscription_service, "_collect_legacy_inventory_native_proof",
                        lambda *_args, **_kwargs: proof_calls.append(True) or {"ok": True})

    wrong_confirmation = xray_subscription_service.admit_legacy_subscription_inventory(
        expected_digest=digest, operator_confirmation="yes",
    )
    wrong_digest = xray_subscription_service.admit_legacy_subscription_inventory(
        expected_digest="0" * 64, operator_confirmation=CONFIRMATION,
    )

    assert wrong_confirmation["ok"] is False
    assert wrong_digest["ok"] is False
    assert proof_calls == []
    assert subscription.get_subscription_native_generation_state()["status"] == "unknown"
    assert not checkpoint.exists()
    assert not checkpoint.parent.exists()


def test_legacy_admission_checkpoint_is_explicit_and_generic_recovery_never_mutates(monkeypatch, tmp_path):
    digest = _seed_legacy_inventory()
    adapter = object()
    checkpoint = tmp_path / "generation-checkpoint.json"
    monkeypatch.setattr(xray_subscription_service, "_xray_adapter", lambda: adapter)
    monkeypatch.setattr(xray_subscription_service, "_xray_generation_checkpoint_path", lambda _adapter: checkpoint)
    mutation_calls: list[str] = []
    monkeypatch.setattr(xray_subscription_service, "_collect_legacy_inventory_native_proof",
                        lambda *_args, **_kwargs: {"ok": False, "reason": "synthetic_proof_unavailable"})
    monkeypatch.setattr(xray_subscription_service, "_restore_xray_generation_checkpoint",
                        lambda *_args: mutation_calls.append("restore") or {"ok": True})
    monkeypatch.setattr(xray_subscription_service, "_recover_checkpoint_from_current_projection",
                        lambda *_args: mutation_calls.append("current_projection") or {"ok": True})

    result = xray_subscription_service.admit_legacy_subscription_inventory(
        expected_digest=digest, operator_confirmation=CONFIRMATION,
    )

    assert result["ok"] is False
    assert checkpoint.is_file()
    recovery = xray_subscription_service._resolve_existing_xray_generation_checkpoint(adapter, checkpoint)
    assert recovery["ok"] is False
    assert recovery.get("reason") == "proof_only_operation_requires_explicit_resume"
    assert mutation_calls == []


def test_proof_only_admission_promotes_only_after_fresh_proof_and_is_idempotent(monkeypatch, tmp_path):
    digest = _seed_legacy_inventory()
    adapter = object()
    checkpoint = tmp_path / "generation-checkpoint.json"
    monkeypatch.setattr(xray_subscription_service, "_xray_adapter", lambda: adapter)
    monkeypatch.setattr(xray_subscription_service, "_xray_generation_checkpoint_path", lambda _adapter: checkpoint)
    proof_order: list[str] = []
    monkeypatch.setattr(xray_subscription_service, "_collect_legacy_inventory_native_proof",
                        lambda *_args, **_kwargs: proof_order.append("proof") or _positive_synthetic_proof())
    monkeypatch.setattr(xray_subscription_service, "_publish_legacy_admission_mihomo_receipt",
                        lambda proof, operation_id: proof_order.append("receipt") is None)
    monkeypatch.setattr(xray_subscription_service, "_legacy_admission_runtime_fences_match", lambda _admission: True)
    from fwrouter_api.services import mihomo_reconcile_fingerprint
    monkeypatch.setattr(mihomo_reconcile_fingerprint, "current_mihomo_input_fingerprint",
                        lambda: {"hash": "3" * 64})
    monkeypatch.setattr(mihomo_reconcile_fingerprint, "mihomo_udp_policy_receipt_matches", lambda: True)
    monkeypatch.setattr(mihomo_reconcile_fingerprint, "mihomo_input_unchanged", lambda _fingerprint: True)

    result = xray_subscription_service.admit_legacy_subscription_inventory(
        expected_digest=digest, operator_confirmation=CONFIRMATION,
    )

    state = subscription.get_subscription_native_generation_state()
    assert result["ok"] is True, result
    assert result["status"] == "verified"
    assert state["status"] == "verified"
    assert state["candidate_digest"] == digest
    assert state["verified_digest"] == digest
    assert state["candidate_revision"] == 1
    assert proof_order == ["proof", "receipt"]
    assert not checkpoint.exists()
    from fwrouter_api.adapters import xray_common
    monkeypatch.setattr(xray_common, "_default_xray_config_path", lambda: str(tmp_path / "xray.json"))
    verified_plan = xray_subscription_service.inspect_legacy_subscription_inventory_admission()
    assert verified_plan["action"] == "reprove_verified"
    assert verified_plan["native_proof_required"] is True

    repeated = xray_subscription_service.admit_legacy_subscription_inventory(
        expected_digest=digest, operator_confirmation=CONFIRMATION,
    )
    assert repeated["ok"] is True
    assert repeated["already_admitted"] is True
    assert subscription.get_subscription_native_generation_state()["candidate_revision"] == 1


def test_proof_only_admission_never_publishes_when_fences_drift_after_readback(monkeypatch, tmp_path):
    digest = _seed_legacy_inventory()
    adapter = object()
    checkpoint = tmp_path / "generation-checkpoint.json"
    monkeypatch.setattr(xray_subscription_service, "_xray_adapter", lambda: adapter)
    monkeypatch.setattr(xray_subscription_service, "_xray_generation_checkpoint_path", lambda _adapter: checkpoint)
    legacy_fingerprint = xray_subscription_service._legacy_admission_source_fingerprint
    fake_proof = _positive_synthetic_proof()
    fingerprints = iter([legacy_fingerprint(), legacy_fingerprint(), legacy_fingerprint(), "b" * 64])
    monkeypatch.setattr(xray_subscription_service, "_legacy_admission_source_fingerprint", lambda connection=None: next(fingerprints))
    monkeypatch.setattr(xray_subscription_service, "_collect_legacy_inventory_native_proof",
                        lambda *_args, **_kwargs: fake_proof)
    receipts: list[bool] = []
    monkeypatch.setattr(xray_subscription_service, "_publish_legacy_admission_mihomo_receipt",
                        lambda *_args: receipts.append(True) or True)
    monkeypatch.setattr(xray_subscription_service, "_legacy_admission_runtime_fences_match", lambda _admission: True)
    from fwrouter_api.services import mihomo_reconcile_fingerprint
    monkeypatch.setattr(mihomo_reconcile_fingerprint, "current_mihomo_input_fingerprint",
                        lambda: {"hash": "3" * 64})
    monkeypatch.setattr(mihomo_reconcile_fingerprint, "mihomo_udp_policy_receipt_matches", lambda: True)
    monkeypatch.setattr(mihomo_reconcile_fingerprint, "mihomo_input_unchanged", lambda _fingerprint: True)

    result = xray_subscription_service.admit_legacy_subscription_inventory(
        expected_digest=digest, operator_confirmation=CONFIRMATION,
    )

    assert result["ok"] is False
    assert result["reason"] == "legacy_admission_fence_changed", {"result": result, "receipts": receipts}
    assert receipts == [True]
    assert subscription.get_subscription_native_generation_state()["status"] == "pending"
    checkpoint_state = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert checkpoint_state["operation_mode"] == "proof_only_legacy_admission"
    assert checkpoint_state["phase"] == "proof_complete"
    assert "artifacts" not in checkpoint_state
    assert subscription.get_subscription_native_generation_state()["status"] == "pending"

    recovery = xray_subscription_service._resolve_existing_xray_generation_checkpoint(adapter, checkpoint)
    assert recovery["ok"] is False
    assert recovery.get("reason") == "proof_only_operation_requires_explicit_resume"
    assert checkpoint.is_file()


def test_legacy_admission_runtime_fence_facts_are_closed_and_match_owner_predicate(monkeypatch, tmp_path):
    from contextlib import nullcontext
    from types import SimpleNamespace

    from fwrouter_api.services import mihomo_config, mihomo_runtime
    from fwrouter_api.services import mihomo_reconcile_fingerprint

    xray_path = tmp_path / "xray.json"
    mihomo_path = tmp_path / "mihomo.yaml"
    xray_path.write_bytes(b"synthetic xray active")
    mihomo_path.write_bytes(b"synthetic mihomo active")
    xray_sha = __import__("hashlib").sha256(xray_path.read_bytes()).hexdigest()
    mihomo_sha = __import__("hashlib").sha256(mihomo_path.read_bytes()).hexdigest()
    fingerprint_hash = "a" * 64
    core_fingerprint = "b" * 64
    core_proof = {
        "ok": True, "manifest_hash": "c" * 64, "apply_id": "synthetic-apply",
        "enforcement_level": "global_vpn_enforced", "live_global_mode": "vpn",
    }

    class RuntimeAdapter:
        config_path = xray_path

        @staticmethod
        def get_runtime_incarnation():
            return "synthetic-xray-incarnation"

        @staticmethod
        def get_runtime_config_sha256():
            return xray_sha

    admission = SimpleNamespace(
        proof_only_xray_incarnation="synthetic-xray-incarnation",
        proof_only_xray_config_sha256=xray_sha,
        proof_only_mihomo_incarnation="synthetic-mihomo-incarnation",
        proof_only_mihomo_config_sha256=mihomo_sha,
        proof_only_mihomo_fingerprint_hash=fingerprint_hash,
        proof_only_core_runtime_fingerprint=core_fingerprint,
        proof_only_core_manifest_hash=core_proof["manifest_hash"],
        proof_only_core_apply_id=core_proof["apply_id"],
        proof_only_core_enforcement_level=core_proof["enforcement_level"],
        proof_only_core_live_mode=core_proof["live_global_mode"],
    )
    monkeypatch.setattr(xray_subscription_service, "_xray_adapter", lambda: RuntimeAdapter())
    monkeypatch.setattr(xray_subscription_service, "db_session", lambda: nullcontext(object()))
    monkeypatch.setattr(xray_subscription_service, "_legacy_admission_core_runtime_fingerprint",
                        lambda _connection=None: core_fingerprint)
    monkeypatch.setattr(xray_subscription_service, "_legacy_admission_core_unavailable_reason",
                        lambda _connection=None: None)
    monkeypatch.setattr(xray_subscription_service, "_collect_legacy_admission_core_native_proof",
                        lambda: dict(core_proof))
    monkeypatch.setattr(mihomo_config, "_resolved_base_config_path", lambda: mihomo_path)
    monkeypatch.setattr(mihomo_runtime, "get_mihomo_runtime_incarnation",
                        lambda: "synthetic-mihomo-incarnation")
    monkeypatch.setattr(mihomo_reconcile_fingerprint, "current_mihomo_input_fingerprint",
                        lambda: {"hash": fingerprint_hash})

    facts = xray_subscription_service._legacy_admission_runtime_fence_facts(admission)
    assert xray_subscription_service._legacy_admission_runtime_fences_match(admission) is True
    assert facts and all(type(value) is bool and value for value in facts.values())
    assert set(facts) == {
        "xray_incarnation_match", "xray_runtime_config_match", "xray_active_config_match",
        "mihomo_incarnation_match", "mihomo_active_config_match", "mihomo_input_fingerprint_match",
        "core_runtime_fingerprint_match", "core_state_available", "core_native_proof_ok",
        "core_manifest_match", "core_apply_id_match", "core_enforcement_match", "core_live_mode_match",
    }

    admission.proof_only_core_apply_id = "stale-apply"
    stale = xray_subscription_service._legacy_admission_runtime_fence_facts(admission)
    assert stale["core_apply_id_match"] is False
    assert sum(value is False for value in stale.values()) == 1
    assert xray_subscription_service._legacy_admission_runtime_fences_match(admission) is False


def test_proof_only_checkpoint_direct_recovery_helpers_are_non_mutating(tmp_path, monkeypatch):
    checkpoint = tmp_path / "generation-checkpoint.json"
    xray_subscription_service._atomic_write_private_checkpoint(checkpoint, json.dumps({
        "schema": 1, "operation_mode": "proof_only_legacy_admission",
        "operation_id": "a" * 32, "phase": "proof_started",
        "candidate_digest": "b" * 64, "candidate_revision": 1,
        "source_fingerprint": "c" * 64, "selection_revision": 0,
        "selection_decision_id": None,
    }, sort_keys=True))
    mutations: list[str] = []
    monkeypatch.setattr(xray_subscription_service, "_update_xray_generation_checkpoint",
                        lambda *_args, **_kwargs: mutations.append("checkpoint"))
    restored = xray_subscription_service._restore_xray_generation_checkpoint(object(), checkpoint)
    projected = xray_subscription_service._recover_checkpoint_from_current_projection(object(), checkpoint)
    assert restored["ok"] is False
    assert projected["ok"] is False
    assert mutations == []
    assert checkpoint.exists()


def test_legacy_admission_blocks_when_database_source_changes_during_readback(monkeypatch, tmp_path):
    digest = _seed_legacy_inventory()
    adapter = object()
    checkpoint = tmp_path / "generation-checkpoint.json"
    monkeypatch.setattr(xray_subscription_service, "_xray_adapter", lambda: adapter)
    monkeypatch.setattr(xray_subscription_service, "_xray_generation_checkpoint_path", lambda _adapter: checkpoint)
    proof = _positive_synthetic_proof()

    def mutate_membership(*_args, **_kwargs):
        with db_session() as connection:
            connection.execute(
                "UPDATE subscription_server_memberships SET entry_identity_hash='changed' "
                "WHERE source_id='src:legacy-admission'",
            )
        return proof

    receipts: list[bool] = []
    monkeypatch.setattr(xray_subscription_service, "_collect_legacy_inventory_native_proof", mutate_membership)
    monkeypatch.setattr(xray_subscription_service, "_publish_legacy_admission_mihomo_receipt",
                        lambda *_args: receipts.append(True) or True)
    result = xray_subscription_service.admit_legacy_subscription_inventory(
        expected_digest=digest, operator_confirmation=CONFIRMATION,
    )
    assert result["ok"] is False
    assert result["reason"] == "legacy_admission_fence_changed"
    assert receipts == []
    assert subscription.get_subscription_native_generation_state()["status"] == "unknown"
    assert checkpoint.is_file()


def test_legacy_admission_blocks_when_selection_revision_changes_during_readback(monkeypatch, tmp_path):
    digest = _seed_legacy_inventory()
    adapter = object()
    checkpoint = tmp_path / "generation-checkpoint.json"
    monkeypatch.setattr(xray_subscription_service, "_xray_adapter", lambda: adapter)
    monkeypatch.setattr(xray_subscription_service, "_xray_generation_checkpoint_path", lambda _adapter: checkpoint)
    proof = _positive_synthetic_proof()

    def advance_revision(*_args, **_kwargs):
        with db_session() as connection:
            connection.execute(
                "INSERT INTO settings (key, value_json) VALUES ('routing.auto_selection_revision', '9') "
                "ON CONFLICT(key) DO UPDATE SET value_json='9'",
            )
        return proof

    monkeypatch.setattr(xray_subscription_service, "_collect_legacy_inventory_native_proof", advance_revision)
    result = xray_subscription_service.admit_legacy_subscription_inventory(
        expected_digest=digest, operator_confirmation=CONFIRMATION,
    )
    assert result["ok"] is False
    assert result["reason"] == "selection_fence_changed"
    assert subscription.get_subscription_native_generation_state()["status"] == "unknown"
    assert checkpoint.is_file()


@pytest.mark.parametrize("status,lock_key", [
    ("queued", "apply"),
    ("running", "module:core+selector+xray"),
    ("running", "apply+module:core+selector+xray"),
])
def test_admission_blocks_while_core_apply_job_owns_native_apply(monkeypatch, tmp_path, status, lock_key):
    digest = _seed_legacy_inventory()
    checkpoint = tmp_path / "generation-checkpoint.json"
    monkeypatch.setattr(xray_subscription_service, "_xray_adapter", lambda: object())
    monkeypatch.setattr(xray_subscription_service, "_xray_generation_checkpoint_path", lambda _adapter: checkpoint)
    monkeypatch.setattr(
        xray_subscription_service, "_collect_legacy_inventory_native_proof",
        lambda *_args, **_kwargs: pytest.fail("active Core apply must block before native readback"),
    )
    with db_session() as connection:
        connection.execute(
            "INSERT INTO jobs (job_id, job_type, status, lock_key) VALUES (?, ?, ?, ?)",
            ("synthetic-core-apply", "apply_mutation", status, lock_key),
        )

    result = xray_subscription_service.admit_legacy_subscription_inventory(
        expected_digest=digest, operator_confirmation=CONFIRMATION,
    )

    assert result["ok"] is False
    assert result["reason"] == "core_apply_inflight"
    assert not checkpoint.exists()


def test_admission_rejects_enabled_core_bypass_before_checkpoint(monkeypatch, tmp_path):
    digest = _seed_legacy_inventory()
    checkpoint = tmp_path / "generation-checkpoint.json"
    monkeypatch.setattr(xray_subscription_service, "_xray_adapter", lambda: object())
    monkeypatch.setattr(xray_subscription_service, "_xray_generation_checkpoint_path", lambda _adapter: checkpoint)
    monkeypatch.setattr(
        xray_subscription_service, "_collect_legacy_inventory_native_proof",
        lambda *_args, **_kwargs: pytest.fail("active bypass must block before native readback"),
    )
    with db_session() as connection:
        connection.execute(
            "INSERT INTO settings (key, value_json) VALUES ('core.bypass', '{\"enabled\":true}')",
        )

    result = xray_subscription_service.admit_legacy_subscription_inventory(
        expected_digest=digest, operator_confirmation=CONFIRMATION,
    )

    assert result["ok"] is False
    assert result["reason"] == "core_bypass_active"
    assert not checkpoint.exists()


def test_admission_requires_owned_current_applied_manifest_before_native_proof(monkeypatch, tmp_path):
    digest = _seed_legacy_inventory()
    checkpoint = tmp_path / "generation-checkpoint.json"
    monkeypatch.setattr(xray_subscription_service, "_xray_adapter", lambda: object())
    monkeypatch.setattr(xray_subscription_service, "_xray_generation_checkpoint_path", lambda _adapter: checkpoint)
    with db_session() as connection:
        connection.execute("DELETE FROM apply_versions")
    monkeypatch.setattr(
        xray_subscription_service, "_collect_legacy_inventory_native_proof",
        lambda *_args, **_kwargs: pytest.fail("unowned/missing applied Core generation must block before native proof"),
    )

    result = xray_subscription_service.admit_legacy_subscription_inventory(
        expected_digest=digest, operator_confirmation=CONFIRMATION,
    )

    assert result["ok"] is False
    assert result["reason"] == "core_applied_generation_unowned"
    assert subscription.get_subscription_native_generation_state()["status"] == "unknown"
    assert not checkpoint.exists()


def test_core_admission_gate_uses_owned_apply_not_runtime_observation(monkeypatch, tmp_path):
    _seed_legacy_inventory()
    from types import SimpleNamespace
    from fwrouter_api.services import apply_results

    with db_session() as connection:
        connection.execute(
            "UPDATE modules SET runtime_state='not_configured' WHERE module_name='core'",
        )
        connection.execute("DELETE FROM apply_versions")
        connection.execute(
            "INSERT INTO jobs (job_id, job_type, status) VALUES (?, 'apply', 'success')",
            ("synthetic-real-apply-job",),
        )
    monkeypatch.setattr(apply_results, "write_apply_result_log", lambda **_kwargs: None)
    monkeypatch.setattr(apply_results, "write_job_json_artifact", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(apply_results, "atomic_write_json", lambda *_args, **_kwargs: None)
    manifest = {
        "generated_at": "synthetic-generation-time",
        "summary": {"path_counts": {}},
        "owned_table": "fwrouter",
    }
    plan = {
        "apply_id": "synthetic-real-apply-writer",
        "job_id": "synthetic-real-apply-job",
        "artifacts": {"artifact_dir": str(tmp_path / "apply-artifacts")},
    }
    apply_results.persist_apply_result(
        job_id=plan["job_id"],
        result_manifest_path=tmp_path / "apply-result.json",
        result={"ok": True, "dataplane": {"error_code": None, "error_message": None}},
        plan=plan,
        mode=SimpleNamespace(value="apply"),
        reason="set_global_mode",
        manifest=manifest,
        manifest_paths={"versioned_manifest_path": str(tmp_path / "manifest.json")},
        result_runtime_enforcement={
            "dataplane_capability": "synthetic-owned",
            "enforcement_level": "global_direct_enforced",
        },
        rollback_result=None,
    )

    with db_session() as connection:
        core = connection.execute(
            "SELECT desired_state, lifecycle_mode, runtime_state, apply_state "
            "FROM modules WHERE module_name='core'",
        ).fetchone()
    assert tuple(core) == ("enabled", "managed", "not_configured", "clean")
    assert xray_subscription_service._legacy_admission_core_unavailable_reason() is None


@pytest.mark.parametrize(
    "updates,expected_reason",
    [
        ({"desired_state": "disabled"}, "core_apply_state_unverified"),
        ({"lifecycle_mode": "none"}, "core_apply_state_unverified"),
        ({"apply_state": "failed"}, "core_apply_state_unverified"),
    ],
)
def test_core_admission_gate_rejects_unowned_or_dirty_module_state(updates, expected_reason):
    _seed_legacy_inventory()
    with db_session() as connection:
        assignments = ", ".join(f"{key}=?" for key in updates)
        connection.execute(
            f"UPDATE modules SET {assignments} WHERE module_name='core'",
            tuple(updates.values()),
        )
    assert xray_subscription_service._legacy_admission_core_unavailable_reason() == expected_reason


@pytest.mark.parametrize(
    "intent_mode,live_mode,enforcement_level,expected_ok",
    [
        ("direct", "direct", "global_direct_enforced", True),
        ("selective", "selective", "global_selective_enforced", True),
        ("vpn", "vpn", "global_vpn_enforced", True),
        ("vpn", "direct", "global_vpn_enforced", False),
    ],
)
def test_core_native_proof_requires_owned_manifest_and_fresh_enforcement_for_each_mode(
    monkeypatch, tmp_path, intent_mode, live_mode, enforcement_level, expected_ok,
):
    _seed_legacy_inventory()
    from fwrouter_api.services import dataplane_global, dataplane_live, dataplane_status

    with db_session() as connection:
        # The historical Core schema seeds an observational not_configured
        # runtime state even when routing has a valid owned applied generation.
        connection.execute(
            "UPDATE modules SET runtime_state='not_configured' WHERE module_name='core'",
        )
        connection.execute(
            "UPDATE routing_global_state SET desired_mode=?, applied_mode=?, apply_state='clean' WHERE id=1",
            (intent_mode, intent_mode),
        )
        connection.execute(
            "UPDATE apply_versions SET status='applied' WHERE apply_id='synthetic-applied-generation'",
        )

    manifest_routing = {
        "desired_mode": intent_mode,
        "applied_mode": intent_mode,
        "selective_default": "direct",
        "server_mode": "auto",
        "desired_fixed_server_id": None,
        "applied_fixed_server_id": None,
        "fixed_server_until": None,
        "active_auto_server_id": None,
    }
    manifest = _write_applied_manifest_via_existing_owner(
        monkeypatch,
        tmp_path,
        plan_id="synthetic-applied-generation",
        routing=manifest_routing,
    )
    force_refresh_calls = []

    def fresh_cache(key, *, ttl_seconds, loader, force_refresh=False):
        force_refresh_calls.append((key, force_refresh))
        return loader()

    monkeypatch.setattr(dataplane_status, "_read_live_dataplane_payload", lambda: {"table_exists": True})
    monkeypatch.setattr(dataplane_status, "_live_owned_table_ready", lambda _payload: True)
    monkeypatch.setattr(dataplane_global, "read_applied_manifest", lambda: manifest)
    monkeypatch.setattr(dataplane_live, "_probe_live_global_mode_uncached", lambda: {
        "ok": True, "mode": live_mode, "selective_default": "direct",
    })
    monkeypatch.setattr(dataplane_live, "probe_live_global_mode", lambda: {
        "ok": True, "mode": live_mode, "selective_default": "direct",
    })
    monkeypatch.setattr(dataplane_global, "build_global_preflight", lambda **_kwargs: {
        "can_enforce_global_direct": True,
        "can_enforce_global_selective": True,
        "can_enforce_global_vpn": True,
        "vpn_egress_ready": True,
        "selective_degraded": False,
        "selective_degraded_missing": [],
        "missing_by_mode": {"direct": [], "selective": [], "vpn": []},
        "missing": [],
        "profile": "synthetic",
    })
    monkeypatch.setattr(dataplane_status, "build_global_preflight", lambda **_kwargs: {
        "can_enforce_global_direct": True,
        "can_enforce_global_selective": True,
        "can_enforce_global_vpn": True,
        "vpn_egress_ready": True,
        "selective_degraded": False,
        "selective_degraded_missing": [],
        "missing_by_mode": {"direct": [], "selective": [], "vpn": []},
        "missing": [],
        "profile": "synthetic",
    })
    monkeypatch.setattr(dataplane_status, "_runtime_routing_state", lambda _manifest: manifest["routing_global_state"])
    # Exercise the existing enforcement contract without introducing a test-only proof path.
    from fwrouter_api.services import live_probe_cache
    monkeypatch.setattr(live_probe_cache, "get_live_probe_cache", fresh_cache)
    monkeypatch.setattr(dataplane_global, "read_effective_rules_artifact", lambda: {})
    monkeypatch.setattr(dataplane_global, "_read_effective_rules_artifact_uncached", lambda: {})

    proof = xray_subscription_service._collect_legacy_admission_core_native_proof()

    assert proof["ok"] is expected_ok, proof
    if not expected_ok:
        assert proof["reason"] == "core_native_enforcement_unproven"
        return
    assert proof["enforcement_level"] == enforcement_level
    assert proof["traffic_enforcement_guaranteed"] is True
    assert proof["active_mode_matches_intent"] is True
    assert proof["live_global_mode"] == live_mode
    assert proof["apply_id"] == "synthetic-applied-generation"
    assert len(proof["manifest_hash"]) == 64
    with db_session() as connection:
        core_runtime_state = connection.execute(
            "SELECT runtime_state FROM modules WHERE module_name='core'",
        ).fetchone()["runtime_state"]
    assert core_runtime_state == "not_configured"
    assert ("dataplane_global.applied_manifest", True) in force_refresh_calls
    assert ("dataplane_live.global_mode", True) in force_refresh_calls


def test_core_native_proof_blocks_missing_applied_generation_and_live_mode_mismatch(monkeypatch):
    _seed_legacy_inventory()
    from fwrouter_api.services import dataplane_global, dataplane_live, dataplane_status
    with db_session() as connection:
        connection.execute("DELETE FROM apply_versions")
    monkeypatch.setattr(dataplane_status, "_read_live_dataplane_payload", lambda: {"table_exists": True})
    monkeypatch.setattr(dataplane_status, "_live_owned_table_ready", lambda _payload: True)
    monkeypatch.setattr(dataplane_global, "_read_applied_manifest_uncached", lambda: {
        "plan_id": "not-owned", "routing_global_state": {"desired_mode": "direct", "applied_mode": "direct"},
    })
    proof = xray_subscription_service._collect_legacy_admission_core_native_proof()
    assert proof["ok"] is False
    assert proof["reason"] == "core_applied_generation_unowned"


@pytest.mark.parametrize(
    "manifest,expected_reason",
    [
        ({"routing_global_state": {"desired_mode": "direct", "applied_mode": "direct"}},
         "core_applied_manifest_unavailable"),
        ({"plan_id": "foreign-apply", "routing_global_state": {"desired_mode": "direct", "applied_mode": "direct"}},
         "core_applied_generation_unowned"),
        ({"apply_id": "synthetic-applied-generation", "routing_global_state": {
            "desired_mode": "direct", "applied_mode": "direct",
        }}, "core_applied_manifest_unavailable"),
        ({"plan_id": 9, "routing_global_state": {
            "desired_mode": "direct", "applied_mode": "direct",
        }}, "core_applied_manifest_unavailable"),
        ({"plan_id": "synthetic-applied-generation", "apply_id": "foreign-apply",
          "routing_global_state": {"desired_mode": "direct", "applied_mode": "direct"}},
         "core_applied_manifest_unavailable"),
        ({"plan_id": "synthetic-applied-generation", "apply_id": 9,
          "routing_global_state": {"desired_mode": "direct", "applied_mode": "direct"}},
         "core_applied_manifest_unavailable"),
        ({"plan_id": 7, "apply_id": 9, "routing_global_state": {
            "desired_mode": "direct", "applied_mode": "direct",
        }}, "core_applied_manifest_unavailable"),
        ({"plan_id": "synthetic-applied-generation", "routing_global_state": {
            "desired_mode": "vpn", "applied_mode": "vpn", "selective_default": "direct",
            "server_mode": "auto", "desired_fixed_server_id": None, "applied_fixed_server_id": None,
            "fixed_server_until": None, "active_auto_server_id": None,
        }}, "core_applied_routing_mismatch"),
    ],
)
def test_core_native_proof_rejects_foreign_manifest_or_routing_drift(monkeypatch, manifest, expected_reason):
    _seed_legacy_inventory()
    from fwrouter_api.services import dataplane_global
    monkeypatch.setattr(dataplane_global, "_read_applied_manifest_uncached", lambda: manifest)

    proof = xray_subscription_service._collect_legacy_admission_core_native_proof()

    assert proof["ok"] is False
    assert proof["reason"] == expected_reason


def test_inventory_native_collector_keeps_readonly_db_fence_reads_in_session(monkeypatch, tmp_path):
    digest = _seed_legacy_inventory()
    _seed_proof_native_module_rows()
    core_proof_calls: list[bool] = []

    def reached_core_native_proof(*_args, **_kwargs):
        core_proof_calls.append(True)
        return {"ok": False, "reason": "synthetic_core_native_readback_stop"}

    monkeypatch.setattr(
        xray_subscription_service, "_collect_legacy_admission_core_native_proof", reached_core_native_proof,
    )
    source = xray_subscription_service._legacy_admission_source_fingerprint()
    with xray_writer_guard():
        result = xray_subscription_service._collect_legacy_inventory_native_proof(
            object(), expected_digest=digest, source_fingerprint=source,
            checkpoint_path=tmp_path / "generation-checkpoint.json",
        )

    assert core_proof_calls == [True]
    assert result["ok"] is False
    assert result["reason"] == "synthetic_core_native_readback_stop"


@pytest.mark.parametrize(
    "vpn_desired,vpn_lifecycle,include_vpn",
    [
        ("disabled", "managed", True),
        ("enabled", "external", True),
        ("enabled", "managed", False),
    ],
)
def test_inventory_collector_requires_existing_managed_vpn_module_owner(
    monkeypatch, tmp_path, vpn_desired, vpn_lifecycle, include_vpn,
):
    digest = _seed_legacy_inventory()
    _seed_proof_native_module_rows(
        vpn_desired=vpn_desired, vpn_lifecycle=vpn_lifecycle, include_vpn=include_vpn,
    )
    monkeypatch.setattr(
        xray_subscription_service, "_collect_legacy_admission_core_native_proof",
        lambda: pytest.fail("unavailable vpn module owner must block before native proof"),
    )
    source = xray_subscription_service._legacy_admission_source_fingerprint()
    with xray_writer_guard():
        result = xray_subscription_service._collect_legacy_inventory_native_proof(
            object(), expected_digest=digest, source_fingerprint=source,
            checkpoint_path=tmp_path / "generation-checkpoint.json",
        )

    assert result == {"ok": False, "reason": "managed_native_runtimes_required"}


def test_inventory_native_collector_completes_with_real_db_and_synthetic_native_boundaries(monkeypatch, tmp_path):
    """Exercise the real proof collector while stubbing only native/OS boundaries."""
    import hashlib
    from types import SimpleNamespace
    import yaml

    digest = _seed_legacy_inventory()
    real_temp_directory = xray_subscription_service.tempfile.TemporaryDirectory
    monkeypatch.setattr(
        xray_subscription_service.tempfile, "TemporaryDirectory",
        lambda *, prefix: real_temp_directory(prefix=prefix, dir=tmp_path),
    )
    xray_path = tmp_path / "active-xray.json"
    xray_bytes = b'{"inbounds":[],"outbounds":[]}'
    xray_path.write_bytes(xray_bytes)
    xray_sha = hashlib.sha256(xray_bytes).hexdigest()
    binding_path = tmp_path / "bindings.json"
    binding_path.write_text("{}", encoding="utf-8")
    mihomo_path = tmp_path / "active-mihomo.yaml"
    candidate_mihomo = {"proxies": [], "proxy-groups": []}
    mihomo_path.write_text(yaml.safe_dump(candidate_mihomo, sort_keys=False, allow_unicode=True), encoding="utf-8")

    from fwrouter_api.services import dataplane_global, dataplane_live, dataplane_status
    from fwrouter_api.services import mihomo_config, mihomo_reconcile, mihomo_reconcile_fingerprint, mihomo_runtime
    from fwrouter_api.services import subscription_profiles, xray_materialize, xray_runtime_state
    _seed_proof_native_module_rows()
    with db_session() as connection:
        routing = dict(connection.execute("SELECT * FROM routing_global_state WHERE id=1").fetchone())
        module_names = {row[0] for row in connection.execute("SELECT module_name FROM modules")}
        from fwrouter_api.services.vpn_auto_selection_state import read_selection_fence
        selection = read_selection_fence(connection)
    assert "vpn" in module_names and "xray" in module_names and "mihomo" not in module_names
    selection_revision = selection["revision"]
    selection_decision_id = selection["decision_id"]
    manifest = {"plan_id": "synthetic-applied-generation", "routing_global_state": {
        key: routing.get(key) for key in (
            "desired_mode", "applied_mode", "selective_default", "server_mode",
            "desired_fixed_server_id", "applied_fixed_server_id", "fixed_server_until", "active_auto_server_id",
        )
    }}

    # Native/service boundaries are bounded test doubles; SQLite ownership,
    # inventory reads, selection fencing and the proof collector stay real.
    monkeypatch.setattr(xray_subscription_service, "_capture_generation_auto_selection", lambda: {
        "selection_revision": selection_revision, "selection_decision_id": selection_decision_id,
    })
    monkeypatch.setattr(xray_subscription_service, "_verify_generation_recovery_selection_readback",
                        lambda _expected: {"ok": True})
    monkeypatch.setattr(xray_subscription_service, "_current_xray_projection_snapshot", lambda: {
        "fence": {"revision": selection_revision}, "projection": "synthetic-stable",
    })
    monkeypatch.setattr(xray_subscription_service, "_current_projection_fingerprint", lambda value: str(value))
    bindings_sha = hashlib.sha256(b"{}").hexdigest()
    monkeypatch.setattr(xray_subscription_service, "_current_bindings_artifact_parity", lambda *_args: (True, bindings_sha))
    monkeypatch.setattr(xray_subscription_service, "_prospective_profile_bindings", lambda *_args, **_kwargs: ({}, {}, {}))
    monkeypatch.setattr(xray_subscription_service, "list_subscription_profile_tokens", lambda: [])
    monkeypatch.setattr(subscription_profiles, "_subscription_servers", lambda: [])
    monkeypatch.setattr(xray_subscription_service, "_vpn_auto_servers_for_xray_subscription", lambda: [])
    monkeypatch.setattr(xray_subscription_service, "list_desired_subscription_xray_clients", lambda: [{
        "server_id": "synthetic-node", "client_uuid": "synthetic-client-id",
        "client_email": "synthetic-client@example.invalid", "xray_alias": "synthetic-node",
    }])
    monkeypatch.setattr(mihomo_config, "_resolved_base_config_path", lambda: str(mihomo_path))
    monkeypatch.setattr(mihomo_config, "build_mihomo_config", lambda **_kwargs: candidate_mihomo)
    monkeypatch.setattr(mihomo_config, "validate_mihomo_candidate_config", lambda **_kwargs: {"ok": True})
    monkeypatch.setattr(xray_runtime_state, "_xray_bindings_path", lambda: binding_path)
    monkeypatch.setattr(xray_materialize, "_verify_active_config_bindings", lambda *_args, **_kwargs: {"ok": True})
    monkeypatch.setattr(xray_materialize, "_verify_active_config_client_modes", lambda *_args: {"ok": True})
    monkeypatch.setattr(mihomo_reconcile, "_native_udp_policy_proof", lambda: {
        "ok": True, "native_top_level_rules_match": True,
        "selector_membership_matches": True, "guarded_rules_match": True,
    })
    monkeypatch.setattr(mihomo_reconcile_fingerprint, "current_mihomo_input_fingerprint", lambda: {"hash": "b" * 64})
    monkeypatch.setattr(mihomo_runtime, "get_mihomo_runtime_incarnation", lambda: "synthetic-mihomo-incarnation")

    monkeypatch.setattr(dataplane_status, "_read_live_dataplane_payload", lambda: {"table_exists": True})
    monkeypatch.setattr(dataplane_status, "_live_owned_table_ready", lambda _payload: True)
    monkeypatch.setattr(dataplane_global, "_read_applied_manifest_uncached", lambda: manifest)
    monkeypatch.setattr(dataplane_global, "read_applied_manifest", lambda: manifest)
    monkeypatch.setattr(dataplane_global, "read_effective_rules_artifact", lambda: {})
    monkeypatch.setattr(dataplane_live, "_probe_live_global_mode_uncached", lambda: {
        "ok": True, "mode": "direct", "selective_default": "direct",
    })
    monkeypatch.setattr(dataplane_live, "probe_live_global_mode", lambda: {
        "ok": True, "mode": "direct", "selective_default": "direct",
    })
    preflight = {
        "can_enforce_global_direct": True, "can_enforce_global_selective": True,
        "can_enforce_global_vpn": True, "vpn_egress_ready": True,
        "selective_degraded": False, "selective_degraded_missing": [],
        "missing_by_mode": {"direct": [], "selective": [], "vpn": []}, "missing": [], "profile": "synthetic",
    }
    monkeypatch.setattr(dataplane_status, "build_global_preflight", lambda **_kwargs: preflight)
    monkeypatch.setattr(dataplane_status, "_runtime_routing_state", lambda _manifest: manifest["routing_global_state"])

    cache_refreshes: list[str] = []
    def cache_get(key, *, ttl_seconds, loader, force_refresh=False):
        if force_refresh:
            cache_refreshes.append(key)
        return loader()
    monkeypatch.setattr("fwrouter_api.services.live_probe_cache.get_live_probe_cache", cache_get)

    staged: list[Path] = []
    class SyntheticAdapter:
        config_path = str(xray_path)
        def get_runtime_incarnation(self):
            return "synthetic-xray-incarnation"
        def get_runtime_config_sha256(self):
            return xray_sha
        def stage_subscription_generation(self, *, candidate_path, **_kwargs):
            candidate_path.write_bytes(xray_bytes)
            staged.append(candidate_path)
            return SimpleNamespace(ok=True, details={
                "expected_client_identities": [("synthetic-client-id", "synthetic-client@example.invalid")],
            })
        def list_loaded_client_identities(self):
            return [("synthetic-client-id", "synthetic-client@example.invalid")]

    source = xray_subscription_service._legacy_admission_source_fingerprint()
    with xray_writer_guard():
        proof = xray_subscription_service._collect_legacy_inventory_native_proof(
            SyntheticAdapter(), expected_digest=digest, source_fingerprint=source,
            checkpoint_path=tmp_path / "generation-checkpoint.json",
        )

    assert proof["ok"] is True, proof
    assert proof["core_native_policy_match"] is True
    assert proof["xray_loaded_users_match"] is True
    assert proof["mihomo_native_policy_match"] is True
    assert len(staged) == 1 and "fwrouter-legacy-admission-" in str(staged[0].parent)
    assert "dataplane_global.applied_manifest" in cache_refreshes
    assert cache_refreshes.count("dataplane_live.global_mode") >= 2


def test_fresh_admission_rejects_checkpoint_parent_symlink_without_touching_target(monkeypatch, tmp_path):
    digest = _seed_legacy_inventory()
    target = tmp_path / "operator-owned-target"
    target.mkdir(mode=0o755)
    sentinel = target / "sentinel"
    sentinel.write_text("preserve", encoding="utf-8")
    sentinel_before = sentinel.stat()
    target_mode_before = stat.S_IMODE(target.stat().st_mode)
    checkpoint_parent = tmp_path / ".generation"
    checkpoint_parent.symlink_to(target, target_is_directory=True)
    checkpoint = checkpoint_parent / "generation-checkpoint.json"
    monkeypatch.setattr(xray_subscription_service, "_xray_adapter", lambda: object())
    monkeypatch.setattr(xray_subscription_service, "_xray_generation_checkpoint_path", lambda _adapter: checkpoint)
    monkeypatch.setattr(
        xray_subscription_service, "_collect_legacy_inventory_native_proof",
        lambda *_args, **_kwargs: pytest.fail("untrusted checkpoint parent must block before proof"),
    )

    result = xray_subscription_service.admit_legacy_subscription_inventory(
        expected_digest=digest, operator_confirmation=CONFIRMATION,
    )

    assert result["ok"] is False
    assert result["reason"] == "checkpoint_directory_untrusted"
    assert checkpoint_parent.is_symlink()
    assert stat.S_IMODE(target.stat().st_mode) == target_mode_before
    assert sentinel.read_text(encoding="utf-8") == "preserve"
    sentinel_after = sentinel.stat()
    assert (sentinel_after.st_ino, sentinel_after.st_mtime_ns, sentinel_after.st_size) == (
        sentinel_before.st_ino, sentinel_before.st_mtime_ns, sentinel_before.st_size,
    )
    assert not checkpoint.exists()


def test_checkpoint_parent_rejects_unsafe_mode_without_chmod_or_file_changes(tmp_path):
    parent = tmp_path / ".generation"
    parent.mkdir(mode=0o700)
    parent.chmod(0o755)
    sentinel = parent / "sentinel"
    sentinel.write_text("unchanged", encoding="utf-8")
    before = sentinel.stat()
    mode_before = stat.S_IMODE(parent.stat().st_mode)

    assert xray_subscription_service._legacy_admission_checkpoint_parent_ready(
        parent / "generation-checkpoint.json",
    ) is False

    after = sentinel.stat()
    assert stat.S_IMODE(parent.stat().st_mode) == mode_before
    assert (after.st_ino, after.st_mtime_ns, after.st_size) == (before.st_ino, before.st_mtime_ns, before.st_size)
    assert not (parent / "generation-checkpoint.json").exists()


def test_checkpoint_parent_rejects_unowned_directory(tmp_path, monkeypatch):
    parent = tmp_path / ".generation"
    parent.mkdir(mode=0o700)
    monkeypatch.setattr(xray_subscription_service.os, "geteuid", lambda: os.getuid() + 12345)

    assert xray_subscription_service._legacy_admission_checkpoint_parent_ready(
        parent / "generation-checkpoint.json",
    ) is False
    assert stat.S_IMODE(parent.stat().st_mode) == 0o700
    assert list(parent.iterdir()) == []


def test_admission_refuses_broken_checkpoint_symlink_without_overwriting_it(monkeypatch, tmp_path):
    digest = _seed_legacy_inventory()
    checkpoint = tmp_path / "generation-checkpoint.json"
    checkpoint.symlink_to(tmp_path / "missing-target")
    monkeypatch.setattr(xray_subscription_service, "_xray_adapter", lambda: object())
    monkeypatch.setattr(xray_subscription_service, "_xray_generation_checkpoint_path", lambda _adapter: checkpoint)
    monkeypatch.setattr(
        xray_subscription_service, "_collect_legacy_inventory_native_proof",
        lambda *_args, **_kwargs: pytest.fail("untrusted checkpoint path must block before proof"),
    )

    result = xray_subscription_service.admit_legacy_subscription_inventory(
        expected_digest=digest, operator_confirmation=CONFIRMATION,
    )

    assert result["ok"] is False
    assert result["reason"] == "checkpoint_unverifiable"
    assert checkpoint.is_symlink()
    assert not checkpoint.resolve(strict=False).exists()


def test_admission_rechecks_core_apply_generation_after_native_proof(monkeypatch, tmp_path):
    digest = _seed_legacy_inventory()
    checkpoint = tmp_path / "generation-checkpoint.json"
    monkeypatch.setattr(xray_subscription_service, "_xray_adapter", lambda: object())
    monkeypatch.setattr(xray_subscription_service, "_xray_generation_checkpoint_path", lambda _adapter: checkpoint)
    proof = _positive_synthetic_proof()

    def advance_core_apply(*_args, **_kwargs):
        with db_session() as connection:
            connection.execute(
                "INSERT INTO apply_versions (apply_id, manifest_path, status) VALUES (?, ?, ?)",
                ("synthetic-apply-generation", "/tmp/synthetic-manifest.json", "applied"),
            )
            connection.execute(
                "INSERT INTO settings (key, value_json) VALUES ('core.bypass', '{\"enabled\":true}') "
                "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json",
            )
            connection.execute("UPDATE modules SET runtime_state='paused' WHERE module_name='core'")
        return proof

    monkeypatch.setattr(xray_subscription_service, "_collect_legacy_inventory_native_proof", advance_core_apply)
    result = xray_subscription_service.admit_legacy_subscription_inventory(
        expected_digest=digest, operator_confirmation=CONFIRMATION,
    )

    assert result["ok"] is False
    assert result["reason"] == "legacy_admission_fence_changed"
    assert subscription.get_subscription_native_generation_state()["status"] == "unknown"
    assert checkpoint.is_file()


def test_admission_rechecks_core_module_runtime_after_native_proof(monkeypatch, tmp_path):
    digest = _seed_legacy_inventory()
    checkpoint = tmp_path / "generation-checkpoint.json"
    monkeypatch.setattr(xray_subscription_service, "_xray_adapter", lambda: object())
    monkeypatch.setattr(xray_subscription_service, "_xray_generation_checkpoint_path", lambda _adapter: checkpoint)
    proof = _positive_synthetic_proof()

    def pause_core_during_proof(*_args, **_kwargs):
        with db_session() as connection:
            connection.execute("UPDATE modules SET runtime_state='paused' WHERE module_name='core'")
        return proof

    monkeypatch.setattr(xray_subscription_service, "_collect_legacy_inventory_native_proof", pause_core_during_proof)
    result = xray_subscription_service.admit_legacy_subscription_inventory(
        expected_digest=digest, operator_confirmation=CONFIRMATION,
    )

    assert result["ok"] is False
    assert result["reason"] == "legacy_admission_fence_changed"
    assert subscription.get_subscription_native_generation_state()["status"] == "unknown"
    assert checkpoint.is_file()


def test_final_promotion_rejects_core_apply_job_started_after_native_receipt(monkeypatch, tmp_path):
    digest = _seed_legacy_inventory()
    checkpoint = tmp_path / "generation-checkpoint.json"
    monkeypatch.setattr(xray_subscription_service, "_xray_adapter", lambda: object())
    monkeypatch.setattr(xray_subscription_service, "_xray_generation_checkpoint_path", lambda _adapter: checkpoint)
    monkeypatch.setattr(
        xray_subscription_service, "_collect_legacy_inventory_native_proof",
        lambda *_args, **_kwargs: _positive_synthetic_proof(),
    )
    from fwrouter_api.services import mihomo_reconcile_fingerprint
    monkeypatch.setattr(mihomo_reconcile_fingerprint, "current_mihomo_input_fingerprint", lambda: {"hash": "3" * 64})
    monkeypatch.setattr(mihomo_reconcile_fingerprint, "mihomo_udp_policy_receipt_matches", lambda: True)
    monkeypatch.setattr(mihomo_reconcile_fingerprint, "mihomo_input_unchanged", lambda _fingerprint: True)

    def start_core_apply_after_receipt(*_args):
        with db_session() as connection:
            connection.execute(
                "INSERT INTO jobs (job_id, job_type, status, lock_key) VALUES (?, ?, ?, ?)",
                ("late-core-apply", "apply_mutation", "running", "apply"),
            )
        return True

    monkeypatch.setattr(
        xray_subscription_service, "_publish_legacy_admission_mihomo_receipt", start_core_apply_after_receipt,
    )
    monkeypatch.setattr(xray_subscription_service, "_legacy_admission_runtime_fences_match", lambda _admission: True)

    result = xray_subscription_service.admit_legacy_subscription_inventory(
        expected_digest=digest, operator_confirmation=CONFIRMATION,
    )

    assert result["ok"] is False
    assert result["reason"] == "core_apply_inflight"
    assert subscription.get_subscription_native_generation_state()["status"] == "pending"


def test_plan_classifies_fresh_resume_verified_and_blocked_checkpoint_states(monkeypatch, tmp_path):
    from fwrouter_api.adapters import xray_common
    from fwrouter_api.services.vpn_auto_selection_state import read_selection_fence
    config = tmp_path / "xray.json"
    monkeypatch.setattr(xray_common, "_default_xray_config_path", lambda: str(config))
    monkeypatch.setattr(
        xray_subscription_service, "_xray_adapter",
        lambda: pytest.fail("plan classification must not initialize a native adapter"),
    )
    digest = _seed_legacy_inventory()
    fresh = xray_subscription_service.inspect_legacy_subscription_inventory_admission()
    assert fresh["action"] == "fresh_admission"
    assert fresh["eligible"] is True

    checkpoint = config.parent / ".generation" / "generation-checkpoint.json"
    with db_session() as connection:
        selection = read_selection_fence(connection)
        source = xray_subscription_service._legacy_admission_source_fingerprint(connection)
    checkpoint.parent.mkdir(mode=0o700)
    checkpoint.parent.chmod(0o700)
    xray_subscription_service._atomic_write_private_checkpoint(checkpoint, json.dumps({
        "schema": 1, "operation_mode": "proof_only_legacy_admission",
        "operation_id": "d" * 32, "phase": "proof_started", "candidate_digest": digest,
        "candidate_revision": 1, "source_fingerprint": source,
        "selection_revision": selection["revision"],
        "selection_decision_id": selection["decision_id"], "created_at": 1.0,
    }))
    resume = xray_subscription_service.inspect_legacy_subscription_inventory_admission()
    assert resume["action"] == "resume_proof_only"
    assert resume["eligible"] is True

    payload = json.loads(checkpoint.read_text())
    payload["phase"] = "proof_complete"
    xray_subscription_service._atomic_write_private_checkpoint(checkpoint, json.dumps(payload))
    with db_session() as connection:
        subscription._write_subscription_inventory_candidate(connection, force_pending=True)
    pending_resume = xray_subscription_service.inspect_legacy_subscription_inventory_admission()
    assert pending_resume["action"] == "resume_proof_only"

    payload = json.loads(checkpoint.read_text())
    payload["candidate_digest"] = "e" * 64
    xray_subscription_service._atomic_write_private_checkpoint(checkpoint, json.dumps(payload))
    stale = xray_subscription_service.inspect_legacy_subscription_inventory_admission()
    assert stale["action"] == "blocked"
    assert stale["eligible"] is False
    assert stale["reason"] == "checkpoint_inventory_mismatch"

    checkpoint.write_text("{}")
    checkpoint.chmod(0o600)
    malformed = xray_subscription_service.inspect_legacy_subscription_inventory_admission()
    assert malformed["action"] == "blocked"
    assert malformed["reason"] == "checkpoint_unverifiable"


def test_failed_promotion_resume_requires_same_explicit_operation(monkeypatch, tmp_path):
    digest = _seed_legacy_inventory()
    adapter = object()
    checkpoint = tmp_path / "generation-checkpoint.json"
    monkeypatch.setattr(xray_subscription_service, "_xray_adapter", lambda: adapter)
    monkeypatch.setattr(xray_subscription_service, "_xray_generation_checkpoint_path", lambda _adapter: checkpoint)
    monkeypatch.setattr(xray_subscription_service, "_collect_legacy_inventory_native_proof",
                        lambda *_args, **_kwargs: _positive_synthetic_proof())
    monkeypatch.setattr(xray_subscription_service, "_publish_legacy_admission_mihomo_receipt", lambda *_args: True)
    monkeypatch.setattr(xray_subscription_service, "_legacy_admission_runtime_fences_match", lambda _admission: True)
    from fwrouter_api.services import mihomo_reconcile_fingerprint
    monkeypatch.setattr(mihomo_reconcile_fingerprint, "current_mihomo_input_fingerprint", lambda: {"hash": "3" * 64})
    monkeypatch.setattr(mihomo_reconcile_fingerprint, "mihomo_udp_policy_receipt_matches", lambda: True)
    monkeypatch.setattr(mihomo_reconcile_fingerprint, "mihomo_input_unchanged", lambda _fingerprint: True)
    original_promote = subscription._promote_subscription_inventory_generation
    attempts = 0

    def fail_first_promotion(admission):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return {"ok": False, "reason": "synthetic_crash_window"}
        return original_promote(admission)

    monkeypatch.setattr(subscription, "_promote_subscription_inventory_generation", fail_first_promotion)
    first = xray_subscription_service.admit_legacy_subscription_inventory(
        expected_digest=digest, operator_confirmation=CONFIRMATION,
    )
    assert first["ok"] is False
    assert subscription.get_subscription_native_generation_state()["status"] == "pending"
    assert checkpoint.is_file()
    generic = xray_subscription_service._resolve_existing_xray_generation_checkpoint(adapter, checkpoint)
    assert generic["ok"] is False
    assert generic.get("reason") == "proof_only_operation_requires_explicit_resume"

    second = xray_subscription_service.admit_legacy_subscription_inventory(
        expected_digest=digest, operator_confirmation=CONFIRMATION,
    )
    assert second["ok"] is True, second
    assert subscription.get_subscription_native_generation_state()["status"] == "verified"
    assert not checkpoint.exists()


def test_admission_plan_cli_is_read_only_and_never_calls_admission(monkeypatch, capsys):
    from fwrouter_api import cli

    plan = {
        "ok": True, "status": "plan", "eligible": True,
        "action": "fresh_admission", "native_proof_required": True,
        "inventory_digest": "a" * 64, "inventory_count": 40,
        "generation_status": "unknown", "generation_reason": "verified_generation_missing",
        "checkpoint_mode": None, "checkpoint_phase": None, "source_fingerprint": "b" * 64,
    }
    monkeypatch.setattr(xray_subscription_service, "inspect_legacy_subscription_inventory_admission",
                        lambda: plan)
    monkeypatch.setattr(xray_subscription_service, "admit_legacy_subscription_inventory",
                        lambda **_kwargs: pytest.fail("plan must never execute admission"))
    result = cli.main(["xray", "generation", "admit-legacy", "--plan"])
    output = json.loads(capsys.readouterr().out)
    assert result == 0
    assert output["inventory_digest"] == "a" * 64
    assert output["eligible"] is True
    assert output["action"] == "fresh_admission"
    assert output["native_proof_required"] is True


def test_admission_cli_requires_explicit_digest_and_confirmation(monkeypatch, capsys):
    from fwrouter_api import cli

    monkeypatch.setattr(
        xray_subscription_service, "admit_legacy_subscription_inventory",
        lambda **_kwargs: pytest.fail("missing operator input must not execute admission"),
    )
    result = cli.main(["xray", "generation", "admit-legacy"])
    assert result == 2
    assert json.loads(capsys.readouterr().out)["error_code"] == "ADMISSION_CONFIRMATION_REQUIRED"


def test_exact_large_mihomo_candidate_match_skips_semantic_yaml_parse(tmp_path, monkeypatch):
    from fwrouter_api.services import mihomo_reconcile

    content = b"x" * (1024 * 1024 + 7)
    active = tmp_path / "active.yaml"
    candidate = tmp_path / "candidate.yaml"
    active.write_bytes(content)
    candidate.write_bytes(content)
    monkeypatch.setattr(mihomo_reconcile, "_semantic_config_files_equal",
                        lambda *_args: pytest.fail("identical large candidates need no YAML parse"))
    assert xray_subscription_service._legacy_mihomo_candidate_matches(active, candidate) is True


@pytest.mark.parametrize("marker", [
    {"schema": 1, "status": "pending"},
    {"schema": 1, "status": "verified"},
])
def _legacy_admission_refuses_malformed_marker(monkeypatch, tmp_path, marker):
    digest = _seed_legacy_inventory()
    with db_session() as connection:
        subscription._write_subscription_inventory_candidate(connection)
        connection.execute(
            "UPDATE settings SET value_json=? WHERE key=?",
            (json.dumps(marker), subscription.SUBSCRIPTION_NATIVE_GENERATION_KEY),
        )
    adapter = object()
    checkpoint = tmp_path / "generation-checkpoint.json"
    monkeypatch.setattr(xray_subscription_service, "_xray_adapter", lambda: adapter)
    monkeypatch.setattr(xray_subscription_service, "_xray_generation_checkpoint_path", lambda _adapter: checkpoint)
    proof_calls: list[bool] = []
    monkeypatch.setattr(xray_subscription_service, "_collect_legacy_inventory_native_proof",
                        lambda *_args, **_kwargs: proof_calls.append(True) or {"ok": True})

    result = xray_subscription_service.admit_legacy_subscription_inventory(
        expected_digest=digest, operator_confirmation=CONFIRMATION,
    )

    assert result["ok"] is False
    assert proof_calls == []
    assert not checkpoint.exists()


def test_legacy_admission_refuses_malformed_pending_marker(monkeypatch, tmp_path):
    _legacy_admission_refuses_malformed_marker(monkeypatch, tmp_path, {"schema": 1, "status": "pending"})


def test_legacy_admission_refuses_malformed_verified_marker(monkeypatch, tmp_path):
    _legacy_admission_refuses_malformed_marker(monkeypatch, tmp_path, {"schema": 1, "status": "verified"})


def test_valid_foreign_pending_generation_is_not_claimed(monkeypatch, tmp_path):
    digest = _seed_legacy_inventory()
    with db_session() as connection:
        subscription._write_subscription_inventory_candidate(connection)
    adapter = object()
    checkpoint = tmp_path / "generation-checkpoint.json"
    monkeypatch.setattr(xray_subscription_service, "_xray_adapter", lambda: adapter)
    monkeypatch.setattr(xray_subscription_service, "_xray_generation_checkpoint_path", lambda _adapter: checkpoint)
    proof_calls: list[bool] = []
    monkeypatch.setattr(xray_subscription_service, "_collect_legacy_inventory_native_proof",
                        lambda *_args, **_kwargs: proof_calls.append(True) or {"ok": True})
    result = xray_subscription_service.admit_legacy_subscription_inventory(
        expected_digest=digest, operator_confirmation=CONFIRMATION,
    )
    assert result["ok"] is False
    assert result["reason"] == "generation_marker_already_present"
    assert proof_calls == []
    assert subscription.get_subscription_native_generation_state()["status"] == "pending"
    assert not checkpoint.exists()


def test_admission_checkpoint_write_refuses_proof_mode_supersession(tmp_path):
    adapter = type("Adapter", (), {"config_path": tmp_path / "xray.json"})()
    checkpoint = xray_subscription_service._xray_generation_checkpoint_path(adapter)
    checkpoint.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    checkpoint.parent.chmod(0o700)
    xray_subscription_service._atomic_write_private_checkpoint(checkpoint, json.dumps({
        "schema": 1, "operation_mode": "proof_only_legacy_admission",
        "operation_id": "a" * 32, "phase": "proof_started",
        "candidate_digest": "b" * 64, "candidate_revision": 1,
        "source_fingerprint": "c" * 64, "selection_revision": 0,
        "selection_decision_id": None,
    }, sort_keys=True))
    with pytest.raises(RuntimeError, match="XRAY_PROOF_ONLY_CHECKPOINT_CANNOT_BE_SUPERSEDED"):
        xray_subscription_service._write_xray_generation_checkpoint(
            adapter=adapter, generation_id="test", tokens=set(), phase="restore_started",
            source_fingerprint="d" * 64, staged_generation={}, managed_email_prefixes=[],
            supersession={"checkpoint_sha256": "e" * 64},
        )
    assert xray_subscription_service._legacy_admission_checkpoint_payload(checkpoint)["phase"] == "proof_started"


def test_proof_only_checkpoint_unknown_mode_fails_closed_without_mutation(tmp_path, monkeypatch):
    checkpoint = tmp_path / "generation-checkpoint.json"
    checkpoint.write_text(json.dumps({"operation_mode": "unsupported_future_mode", "phase": "runtime_applied"}))
    mutations: list[str] = []
    monkeypatch.setattr(xray_subscription_service, "_restore_xray_generation_checkpoint",
                        lambda *_args: mutations.append("restore") or {"ok": True})
    recovery = xray_subscription_service._resolve_existing_xray_generation_checkpoint(object(), checkpoint)
    assert recovery["ok"] is False
    assert mutations == []
    assert checkpoint.exists()


def test_proof_only_checkpoint_closes_verified_commit_after_restart_window(monkeypatch, tmp_path):
    digest = _seed_legacy_inventory()
    adapter = object()
    checkpoint = tmp_path / "generation-checkpoint.json"
    monkeypatch.setattr(xray_subscription_service, "_xray_adapter", lambda: adapter)
    monkeypatch.setattr(xray_subscription_service, "_xray_generation_checkpoint_path", lambda _adapter: checkpoint)
    monkeypatch.setattr(xray_subscription_service, "_collect_legacy_inventory_native_proof",
                        lambda *_args, **_kwargs: _positive_synthetic_proof())
    monkeypatch.setattr(xray_subscription_service, "_publish_legacy_admission_mihomo_receipt", lambda *_args: True)
    monkeypatch.setattr(xray_subscription_service, "_legacy_admission_runtime_fences_match", lambda _admission: True)
    from fwrouter_api.services import mihomo_reconcile_fingerprint
    monkeypatch.setattr(mihomo_reconcile_fingerprint, "current_mihomo_input_fingerprint", lambda: {"hash": "3" * 64})
    monkeypatch.setattr(mihomo_reconcile_fingerprint, "mihomo_udp_policy_receipt_matches", lambda: True)
    monkeypatch.setattr(mihomo_reconcile_fingerprint, "mihomo_input_unchanged", lambda _fingerprint: True)
    original_atomic_write = xray_subscription_service._atomic_write_private_checkpoint
    injected = False

    def stop_after_verified_publication(path, text):
        nonlocal injected
        if not injected and json.loads(text).get("phase") == "verified":
            injected = True
            raise OSError("synthetic interruption after verified marker")
        original_atomic_write(path, text)

    monkeypatch.setattr(xray_subscription_service, "_atomic_write_private_checkpoint", stop_after_verified_publication)
    first = xray_subscription_service.admit_legacy_subscription_inventory(
        expected_digest=digest, operator_confirmation=CONFIRMATION,
    )
    assert first["ok"] is False
    assert subscription.get_subscription_native_generation_state()["status"] == "verified"
    assert checkpoint.is_file()
    assert json.loads(checkpoint.read_text())["phase"] == "proof_complete"
    repeated = xray_subscription_service.admit_legacy_subscription_inventory(
        expected_digest=digest, operator_confirmation=CONFIRMATION,
    )
    assert repeated["ok"] is True
    assert repeated["already_admitted"] is True
    assert not checkpoint.exists()


def test_legacy_admission_source_fingerprint_covers_credentials_public_projection_and_core_intent():
    digest = _seed_legacy_inventory()
    with db_session() as connection:
        connection.execute("INSERT OR IGNORE INTO routing_global_state (id) VALUES (1)")
        baseline = xray_subscription_service._legacy_admission_source_fingerprint(connection)
        connection.execute(
            "INSERT INTO subscription_node_identities "
            "(identity_id, origin_server_id, current_server_id, revision, provenance_sha256) "
            "VALUES ('test-identity', 'legacy-admission-node', 'legacy-admission-node', 1, ?) ",
            ("a" * 64,),
        )
        connection.execute("INSERT INTO subscription_node_aliases VALUES ('legacy-admission-node', 'test-identity')")
        connection.execute(
            "INSERT INTO subscription_node_credentials (scope, identity_id, client_uuid, client_email) "
            "VALUES ('subscription:test', 'test-identity', '00000000-0000-4000-8000-000000000001', 'synthetic@test')",
        )
        with_credentials = xray_subscription_service._legacy_admission_source_fingerprint(connection)
        connection.execute(
            "INSERT INTO subscription_accounts (slug, display_name) VALUES ('synthetic', 'Synthetic')",
        )
        account_id = connection.execute("SELECT account_id FROM subscription_accounts WHERE slug='synthetic'").fetchone()[0]
        connection.execute(
            "INSERT INTO subscription_clients (account_id, token, display_name) VALUES (?, 'synthetic-token', 'Synthetic')",
            (account_id,),
        )
        connection.execute(
            "INSERT INTO subscription_profile_snapshots (token, nodes_json) VALUES ('synthetic-token', '[]')",
        )
        with_snapshot = xray_subscription_service._legacy_admission_source_fingerprint(connection)
        connection.execute(
            "INSERT INTO settings (key, value_json) VALUES ('core.bypass', '{\"enabled\":false}')",
        )
        with_bypass_state = xray_subscription_service._legacy_admission_source_fingerprint(connection)
        connection.execute(
            "INSERT INTO apply_versions (apply_id, manifest_path, status) VALUES (?, ?, ?)",
            ("synthetic-apply-fence", "/tmp/synthetic-manifest.json", "applied"),
        )
        with_apply_generation = xray_subscription_service._legacy_admission_source_fingerprint(connection)
        connection.execute("UPDATE routing_global_state SET desired_mode='vpn'")
        with_core_intent = xray_subscription_service._legacy_admission_source_fingerprint(connection)
    assert digest
    assert baseline != with_credentials
    assert with_credentials != with_snapshot
    assert with_snapshot != with_bypass_state
    assert with_bypass_state != with_apply_generation
    assert with_apply_generation != with_core_intent


def test_explicit_admission_rejects_missing_native_adapter(monkeypatch, tmp_path):
    digest = _seed_legacy_inventory()
    checkpoint = tmp_path / "generation-checkpoint.json"
    monkeypatch.setattr(xray_subscription_service, "_xray_generation_checkpoint_path", lambda _adapter: checkpoint)
    monkeypatch.setattr(xray_subscription_service, "_xray_adapter", lambda: (_ for _ in ()).throw(RuntimeError("no adapter")))
    result = xray_subscription_service.admit_legacy_subscription_inventory(
        expected_digest=digest, operator_confirmation=CONFIRMATION,
    )
    assert result["ok"] is False
    assert subscription.get_subscription_native_generation_state()["status"] == "unknown"
    assert not checkpoint.exists()


def _proof_only_xray_config(rules):
    from fwrouter_api.adapters.xray_common import XRAY_INBOUND_TAG

    return {
        "log": {"loglevel": "warning"},
        "inbounds": [{"tag": XRAY_INBOUND_TAG, "protocol": "vless", "settings": {"clients": [
            {"id": "synthetic-user-a", "email": "a@example.test"},
            {"id": "synthetic-user-b", "email": "b@example.test"},
        ]}}],
        "outbounds": [{"tag": "vpn-a", "protocol": "freedom"}, {"tag": "vpn-b", "protocol": "freedom"}],
        "dns": {"servers": ["192.0.2.53"]},
        "routing": {"domainStrategy": "AsIs", "rules": json.loads(json.dumps(rules))},
    }


def _proof_only_user_rule(email, outbound="vpn-a"):
    from fwrouter_api.adapters.xray_common import XRAY_INBOUND_TAG

    return {
        "type": "field", "inboundTag": [XRAY_INBOUND_TAG],
        "user": [email], "outboundTag": outbound,
    }


def _write_proof_only_config(path, config):
    path.write_text(json.dumps(config, sort_keys=True, separators=(",", ":")), encoding="utf-8")


def test_proof_only_xray_candidate_allows_only_safe_per_user_rule_permutation(tmp_path):
    active = tmp_path / "active.json"
    candidate = tmp_path / "candidate.json"
    first = _proof_only_user_rule("a@example.test", "vpn-a")
    second = _proof_only_user_rule("b@example.test", "vpn-b")
    anchor = {"type": "field", "protocol": ["dns"], "outboundTag": "vpn-a"}
    _write_proof_only_config(active, _proof_only_xray_config([anchor, first, second]))
    _write_proof_only_config(candidate, _proof_only_xray_config([anchor, second, first]))

    assert xray_subscription_service._legacy_xray_candidate_matches_active(active, candidate) is True


@pytest.mark.parametrize("mutation", [
    "outbound", "client_identity", "dns", "api_anchor", "anchor_moved", "duplicate_user",
    "broad_user", "extra_rule_option", "unknown_user_shape", "duplicate_casefold_user",
    "duplicate_json_key", "nonfinite_number", "bool_int_type",
])
def test_proof_only_xray_candidate_rejects_unproven_equivalence(tmp_path, mutation):
    active = tmp_path / "active.json"
    candidate = tmp_path / "candidate.json"
    first = _proof_only_user_rule("a@example.test", "vpn-a")
    second = _proof_only_user_rule("b@example.test", "vpn-b")
    anchor = {"type": "field", "inboundTag": ["api-in"], "port": "0-65535", "outboundTag": "api-out"}
    original = _proof_only_xray_config([anchor, first, second])
    changed = _proof_only_xray_config([anchor, second, first])

    if mutation == "outbound":
        changed["routing"]["rules"][1]["outboundTag"] = "vpn-c"
    elif mutation == "client_identity":
        changed["inbounds"][0]["settings"]["clients"][0]["id"] = "synthetic-user-c"
    elif mutation == "dns":
        changed["dns"]["servers"] = ["192.0.2.54"]
    elif mutation == "api_anchor":
        changed["routing"]["rules"][0]["outboundTag"] = "vpn-a"
    elif mutation == "anchor_moved":
        changed["routing"]["rules"] = [second, anchor, first]
    elif mutation == "duplicate_user":
        duplicate_a = _proof_only_user_rule("a@example.test", "vpn-a")
        duplicate_b = _proof_only_user_rule("a@example.test", "vpn-b")
        original["routing"]["rules"] = [anchor, duplicate_a, duplicate_b]
        changed["routing"]["rules"] = [anchor, duplicate_b, duplicate_a]
    elif mutation == "broad_user":
        changed["routing"]["rules"][1]["user"] = ["a@example.test", "b@example.test"]
    elif mutation == "extra_rule_option":
        changed["routing"]["rules"][1]["domain"] = ["example.test"]
    elif mutation == "unknown_user_shape":
        changed["routing"]["rules"][1]["user"] = [""]
    elif mutation == "duplicate_casefold_user":
        first_case = _proof_only_user_rule("A@example.test", "vpn-a")
        second_case = _proof_only_user_rule("a@example.test", "vpn-b")
        original["routing"]["rules"] = [anchor, first_case, second_case]
        changed["routing"]["rules"] = [anchor, second_case, first_case]
    elif mutation == "duplicate_json_key":
        active.write_text('{"routing":{"rules":[],"rules":[]}}', encoding="utf-8")
        candidate.write_text('{"routing":{"rules":[]}}', encoding="utf-8")
        assert xray_subscription_service._legacy_xray_candidate_matches_active(active, candidate) is False
        return
    elif mutation == "nonfinite_number":
        active.write_text('{"routing":{"rules":[],"domainStrategy":NaN}}', encoding="utf-8")
        candidate.write_text('{"routing":{"rules":[],"domainStrategy":Infinity}}', encoding="utf-8")
        assert xray_subscription_service._legacy_xray_candidate_matches_active(active, candidate) is False
        return
    elif mutation == "bool_int_type":
        original["routing"]["domainStrategy"] = True
        changed["routing"]["domainStrategy"] = 1

    _write_proof_only_config(active, original)
    _write_proof_only_config(candidate, changed)
    assert xray_subscription_service._legacy_xray_candidate_matches_active(active, candidate) is False


def test_proof_only_xray_candidate_exact_bytes_fastpath_includes_unknown_shapes(tmp_path):
    active = tmp_path / "active.json"
    candidate = tmp_path / "candidate.json"
    data = b'{"routing":{"rules":[{"user":["wide","shape"]}]}}'
    active.write_bytes(data)
    candidate.write_bytes(data)

    assert xray_subscription_service._legacy_xray_candidate_matches_active(active, candidate) is True


def test_real_xray_stager_candidate_order_proof_uses_existing_generator_without_mutation(tmp_path):
    from fwrouter_api.adapters.xray import XrayApplyResult
    from fwrouter_api.adapters.xray_common import XRAY_INBOUND_TAG
    from fwrouter_api.adapters.xray_real import RealXrayAdapter

    active = tmp_path / "active-xray.json"
    candidate = tmp_path / "candidate-xray.json"
    active.parent.mkdir(parents=True, exist_ok=True)
    active.write_text(json.dumps({
        "log": {"loglevel": "warning"},
        "inbounds": [{
            "tag": XRAY_INBOUND_TAG, "listen": "127.0.0.1", "port": 5300,
            "protocol": "vless", "settings": {"clients": [
                {"id": "synthetic-uuid-a", "email": "sub-a@example.test"},
                {"id": "synthetic-uuid-b", "email": "sub-b@example.test"},
            ], "decryption": "none"},
            "streamSettings": {"network": "ws", "wsSettings": {"path": "/synthetic"}},
        }],
        "outbounds": [{"protocol": "freedom", "tag": "direct"}],
        "routing": {"rules": [{
            "type": "field", "protocol": ["dns"], "outboundTag": "direct",
        }]},
    }), encoding="utf-8")
    original_active = active.read_bytes()
    native_validation_actions = []

    def test_config_only_runner(action, payload):
        assert action == "test_config"
        assert Path(payload["path"]).is_file()
        native_validation_actions.append(action)
        return XrayApplyResult(ok=True, message="synthetic local config validation")

    adapter = RealXrayAdapter(config_path=active, compose_path=tmp_path / "unused-compose.yml", runner=test_config_only_runner)
    desired = [
        {"client_uuid": "synthetic-uuid-a", "email": "sub-a@example.test"},
        {"client_uuid": "synthetic-uuid-b", "email": "sub-b@example.test"},
    ]

    def binding(email, uuid, server):
        return {
            "client_uuid": uuid, "client_id": uuid, "client_email": email,
            "subject_id": "subject-" + email[4], "selected_server_id": server,
            "selected_server_source": "synthetic", "status": "bound", "match_key": email,
        }

    first_bindings = [
        binding("sub-a@example.test", "synthetic-uuid-a", "server-a"),
        binding("sub-b@example.test", "synthetic-uuid-b", "server-b"),
    ]
    staged_initial = adapter.stage_subscription_generation(
        desired_clients=desired, managed_email_prefixes=["sub-"], bindings=first_bindings,
        client_modes=[], candidate_path=candidate,
    )
    assert staged_initial.ok is True
    # Seed the synthetic active generation from the actual producer output;
    # subsequent stage calls remain private and never invoke apply/reload.
    active.write_bytes(candidate.read_bytes())
    active_generation = active.read_bytes()

    reversed_candidate = tmp_path / "reversed-candidate.json"
    staged_reversed = adapter.stage_subscription_generation(
        desired_clients=desired, managed_email_prefixes=["sub-"], bindings=list(reversed(first_bindings)),
        client_modes=[], candidate_path=reversed_candidate,
    )
    assert staged_reversed.ok is True
    assert xray_subscription_service._legacy_xray_candidate_matches_active(active, reversed_candidate) is True
    assert active.read_bytes() == active_generation

    changed_candidate = tmp_path / "changed-candidate.json"
    changed_bindings = [dict(item) for item in first_bindings]
    changed_bindings[0]["selected_server_id"] = "server-c"
    staged_changed = adapter.stage_subscription_generation(
        desired_clients=desired, managed_email_prefixes=["sub-"], bindings=changed_bindings,
        client_modes=[], candidate_path=changed_candidate,
    )
    assert staged_changed.ok is True
    assert xray_subscription_service._legacy_xray_candidate_matches_active(active, changed_candidate) is False
    assert active.read_bytes() == active_generation
    assert native_validation_actions == ["test_config", "test_config", "test_config"]
    assert all(action != "reload" for action in native_validation_actions)


def test_mihomo_candidate_diagnostic_redacts_values_and_compares_existing_renderers(monkeypatch, tmp_path):
    import yaml

    tests_root = Path(__file__).resolve().parents[2] / "tests"
    monkeypatch.syspath_prepend(str(tests_root))
    from application_acceptance.worker import _safe_mihomo_candidate_diff_summary
    from fwrouter_api.services.mihomo_serialization import dump_mihomo_config

    shared = {"type": "select", "proxies": ["SECRET_PROXY_A", "SECRET_PROXY_B"]}
    candidate_config = {
        "rules": ["MATCH,SECRET_PROXY_A"],
        "proxy-groups": [shared, shared],
        "proxies": [{"name": "SECRET_PROXY_A", "type": "ss", "server": "secret.example.test"}],
    }
    active = tmp_path / "active.yaml"
    candidate_dir = tmp_path / "fwrouter-legacy-admission-diagnostic"
    candidate_dir.mkdir()
    candidate = candidate_dir / "mihomo-candidate.yaml"
    # The active file has equal values but no shared Python references. The
    # private candidate serializer sees the original shared object graph.
    active_config = json.loads(json.dumps(candidate_config))
    active.write_text(dump_mihomo_config(active_config), encoding="utf-8")
    candidate.write_text(yaml.safe_dump(candidate_config, sort_keys=False, allow_unicode=True), encoding="utf-8")
    monkeypatch.setenv("FWROUTER_ENVIRONMENT", "test")
    monkeypatch.setenv("FWROUTER_APPLICATION_ACCEPTANCE_ROOT", str(tmp_path))

    summary = _safe_mihomo_candidate_diff_summary(active, candidate, candidate_config)
    assert summary is not None
    assert summary["active_has_yaml_aliases_or_ambiguity"] is False
    assert summary["candidate_has_yaml_aliases_or_ambiguity"] is True
    assert summary["candidate_semantic_equal_active"] is False
    assert summary["candidate_tree_equal_active"] is True
    assert summary["changed_sections"] == []
    assert "SECRET_PROXY_A" not in json.dumps(summary)
    assert "secret.example.test" not in json.dumps(summary)


def test_actual_mihomo_builder_candidate_serialization_is_observed_without_values(monkeypatch, tmp_path):
    import yaml

    tests_root = Path(__file__).resolve().parents[2] / "tests"
    monkeypatch.syspath_prepend(str(tests_root))
    from application_acceptance.worker import _safe_mihomo_candidate_diff_summary
    from fwrouter_api.services import mihomo_config

    config = mihomo_config.build_mihomo_config(
        {"selective_default": "direct"}, xray_handoff_assignments=[],
    )
    active = tmp_path / "active.yaml"
    candidate_dir = tmp_path / "fwrouter-legacy-admission-builder"
    candidate_dir.mkdir()
    candidate = candidate_dir / "mihomo-candidate.yaml"
    active.write_text(mihomo_config.dump_mihomo_config(config), encoding="utf-8")
    candidate.write_text(yaml.safe_dump(config, sort_keys=False, allow_unicode=True), encoding="utf-8")
    monkeypatch.setenv("FWROUTER_ENVIRONMENT", "test")
    monkeypatch.setenv("FWROUTER_APPLICATION_ACCEPTANCE_ROOT", str(tmp_path))

    summary = _safe_mihomo_candidate_diff_summary(active, candidate, config)
    assert summary is not None and summary["available"] is True
    assert summary["candidate_tree_equal_active"] is True
    assert summary["changed_sections"] == []
    # The result is intentionally a boolean diagnostic, never serialized config.
    assert isinstance(summary["candidate_has_yaml_aliases_or_ambiguity"], (bool, type(None)))
