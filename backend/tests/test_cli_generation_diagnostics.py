from __future__ import annotations

import importlib
import json
from pathlib import Path
import sys
import types
from contextlib import nullcontext

import pytest

from fwrouter_api import cli


def test_core_owner_checkpoint_key_sets_match_installer_schema_contract() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    path = repo_root / "installer" / "controlled_backend_lifecycle.py"
    spec = importlib.util.spec_from_file_location("core_owner_lifecycle_schema_test", path)
    assert spec is not None and spec.loader is not None
    lifecycle = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lifecycle)

    schema2_keys = {
        "schema", "operation_id", "status", "created_at", "backend_manifest",
        "backend_manifest_sha256", "active_units", "pending_units",
        "initial_backend_manifest_sha256", "source_transitions",
    }
    assert schema2_keys == lifecycle.PREVIOUS_CHECKPOINT_KEYS
    assert schema2_keys | {"maintenance_operation", "runtime_quiescence"} == lifecycle.CHECKPOINT_KEYS


def _install_legacy_admission_cli(monkeypatch, *, admission_result=None, plan_result=None):
    from fwrouter_api.services import maintenance_lifecycle_admission

    service_module = types.ModuleType("fwrouter_api.services.xray_subscription_service")
    service_module.admit_legacy_subscription_inventory = lambda **_kwargs: admission_result
    service_module.inspect_legacy_subscription_inventory_admission = lambda **_kwargs: plan_result
    monkeypatch.setitem(sys.modules, service_module.__name__, service_module)
    monkeypatch.setattr(maintenance_lifecycle_admission, "runtime_shared_lease", lambda _root: nullcontext())


def test_cold_rollout_import_chain_preserves_inventory_facade_exports() -> None:
    import fwrouter_api.services as services_package

    names = (
        "fwrouter_api.services.rollout_native_evidence",
        "fwrouter_api.services.xray_subscription_service",
        "fwrouter_api.services.custom_servers",
        "fwrouter_api.services.servers",
        "fwrouter_api.services.server_subject_overrides",
        "fwrouter_api.services.subject_server_override_codec",
    )
    saved_modules = {name: sys.modules.pop(name) for name in names if name in sys.modules}
    attributes = {name.rsplit(".", 1)[-1] for name in names}
    saved_attributes = {
        name: (hasattr(services_package, name), getattr(services_package, name, None))
        for name in attributes
    }
    try:
        for attribute in saved_attributes:
            if hasattr(services_package, attribute):
                delattr(services_package, attribute)
        importlib.import_module("fwrouter_api.services.rollout_native_evidence")
        from fwrouter_api.services import custom_servers, server_inventory, servers
        importlib.import_module("fwrouter_api.services.server_subject_overrides")
        codec = importlib.import_module("fwrouter_api.services.subject_server_override_codec")

        assert custom_servers.get_server is server_inventory.get_server
        assert custom_servers.list_servers is server_inventory.list_servers
        assert servers.get_server is server_inventory.get_server
        assert servers.list_servers is server_inventory.list_servers
        assert codec.VPN_AUTO_TARGET_KIND == "vpn_auto"
    finally:
        for name in names:
            sys.modules.pop(name, None)
        for name, module in saved_modules.items():
            sys.modules[name] = module
        for attribute, (present, module) in saved_attributes.items():
            if not present:
                if hasattr(services_package, attribute):
                    delattr(services_package, attribute)
            else:
                setattr(services_package, attribute, module)


def _run_supersession_cli(
    monkeypatch, capsys, service_result: dict[str, object]
) -> dict[str, object]:
    service_module = types.ModuleType("fwrouter_api.services.xray_subscription_service")
    service_module.reconcile_xray_subscription_profile_nodes = lambda **_kwargs: service_result
    monkeypatch.setitem(sys.modules, service_module.__name__, service_module)

    exit_code = cli.main([
        "xray", "generation", "supersede",
        "--checkpoint-sha256", "a" * 64,
        "--migration-plan-sha256", "b" * 64,
        "--apply",
    ])

    assert exit_code == 1
    return json.loads(capsys.readouterr().out)


def test_generation_supersession_cli_exposes_bounded_failure_codes(monkeypatch, capsys) -> None:
    output = _run_supersession_cli(monkeypatch, capsys, {
        "ok": False,
        "status": "failed",
        "stage": "generation_apply",
        "error_code": "XRAY_IDENTITY_INTENT_COMPENSATION_UNVERIFIED",
        "failed_operation": "MIHOMO_XRAY_HANDOFF_TARGET_MISSING",
        "compensation_reason": "handoff_target_missing",
    })

    assert output["failed_operation"] == "MIHOMO_XRAY_HANDOFF_TARGET_MISSING"
    assert output["compensation_reason"] == "handoff_target_missing"
    assert output["error_code"] == "XRAY_IDENTITY_INTENT_COMPENSATION_UNVERIFIED"


def test_generation_supersession_cli_omits_untrusted_failure_details(monkeypatch, capsys) -> None:
    private_value = "11111111-1111-4111-8111-111111111111 token=secret-value"
    output = _run_supersession_cli(monkeypatch, capsys, {
        "ok": False,
        "status": "failed",
        "failed_operation": private_value,
        "compensation_reason": "Compensation failed: " + private_value,
    })

    captured = json.dumps(output)
    assert output["failed_operation"] is None
    assert output["compensation_reason"] is None
    assert private_value not in captured
    assert "secret-value" not in captured


def test_generation_supersession_cli_bounds_failure_codes(monkeypatch, capsys) -> None:
    output = _run_supersession_cli(monkeypatch, capsys, {
        "ok": False,
        "status": "failed",
        "failed_operation": "A" * 97,
        "compensation_reason": "a" * 97,
    })

    assert output["failed_operation"] is None
    assert output["compensation_reason"] is None


def test_legacy_admission_cli_projects_allowlisted_native_proof_reason(monkeypatch, capsys) -> None:
    _install_legacy_admission_cli(monkeypatch, admission_result={
        "ok": False, "status": "blocked", "reason": "native_generation_receipt_incomplete",
        "native_proof_reason": "CORE_IP_POLICY_MISMATCH", "private_extra": "must not escape",
    })

    exit_code = cli.main([
        "xray", "generation", "admit-legacy",
        "--expected-inventory-digest", "a" * 64,
        "--confirm", "PROOF_ONLY_LEGACY_ADMISSION", "--require-native-proof",
    ])

    assert exit_code == 1
    assert json.loads(capsys.readouterr().out) == {
        "ok": False, "status": "blocked", "reason": "native_generation_receipt_incomplete",
        "native_proof_reason": "CORE_IP_POLICY_MISMATCH",
    }


@pytest.mark.parametrize("reason", [
    "not_an_allowlisted_reason",
    "CORE_IP_POLICY_MISMATCH token=secret-value",
    "CORE_IP_POLICY_MISMATCH\nraw exception text",
    {"code": "CORE_IP_POLICY_MISMATCH"},
    ["CORE_IP_POLICY_MISMATCH"],
    None,
])
def test_legacy_admission_cli_omits_malformed_or_unsafe_native_reason(monkeypatch, capsys, reason) -> None:
    _install_legacy_admission_cli(monkeypatch, admission_result={
        "ok": False, "status": "blocked", "reason": "native_generation_receipt_incomplete",
        "native_proof_reason": reason,
        "unexpected": "secret-value",
    })

    exit_code = cli.main([
        "xray", "generation", "admit-legacy",
        "--expected-inventory-digest", "a" * 64,
        "--confirm", "PROOF_ONLY_LEGACY_ADMISSION", "--require-native-proof",
    ])
    output = capsys.readouterr().out

    assert exit_code == 1
    parsed = json.loads(output)
    assert parsed == {
        "ok": False, "status": "blocked", "reason": "native_generation_receipt_incomplete",
    }
    assert "secret-value" not in output


def test_legacy_admission_cli_success_keeps_existing_projection(monkeypatch, capsys) -> None:
    _install_legacy_admission_cli(monkeypatch, admission_result={
        "ok": True, "status": "verified", "digest": "a" * 64, "revision": 7,
        "already_admitted": False, "proof_receipt_written": True,
        "native_proof_reason": "CORE_IP_POLICY_MISMATCH",
    })

    exit_code = cli.main([
        "xray", "generation", "admit-legacy",
        "--expected-inventory-digest", "a" * 64,
        "--confirm", "PROOF_ONLY_LEGACY_ADMISSION",
    ])

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == {
        "ok": True, "status": "verified", "digest": "a" * 64, "revision": 7,
        "already_admitted": False, "proof_receipt_written": True,
    }


def test_legacy_admission_cli_does_not_expose_owner_exception_text(monkeypatch, capsys) -> None:
    from fwrouter_api.services import maintenance_lifecycle_admission

    service_module = types.ModuleType("fwrouter_api.services.xray_subscription_service")

    def raise_owner_error(**_kwargs):
        raise RuntimeError("synthetic token=secret")

    service_module.admit_legacy_subscription_inventory = raise_owner_error
    service_module.inspect_legacy_subscription_inventory_admission = lambda **_kwargs: None
    monkeypatch.setitem(sys.modules, service_module.__name__, service_module)
    monkeypatch.setattr(maintenance_lifecycle_admission, "runtime_shared_lease", lambda _root: nullcontext())

    exit_code = cli.main([
        "xray", "generation", "admit-legacy",
        "--expected-inventory-digest", "a" * 64,
        "--confirm", "PROOF_ONLY_LEGACY_ADMISSION",
    ])
    output = capsys.readouterr().out

    assert exit_code == 1
    assert json.loads(output) == {
        "ok": False, "status": "blocked", "error_code": "RuntimeError",
    }
    assert "secret" not in output


def test_legacy_admission_cli_plan_and_confirmation_contract_unchanged(monkeypatch, capsys) -> None:
    from fwrouter_api.services import maintenance_lifecycle_admission

    calls = []
    service_module = types.ModuleType("fwrouter_api.services.xray_subscription_service")
    service_module.inspect_legacy_subscription_inventory_admission = lambda **kwargs: (
        calls.append(kwargs) or {"ok": True, "status": "ready", "native_proof_reason": "CORE_IP_POLICY_MISMATCH"}
    )
    service_module.admit_legacy_subscription_inventory = lambda **_kwargs: pytest.fail(
        "missing confirmation must not call the admission owner"
    )
    monkeypatch.setitem(sys.modules, service_module.__name__, service_module)
    monkeypatch.setattr(maintenance_lifecycle_admission, "runtime_shared_lease", lambda _root: nullcontext())

    assert cli.main(["xray", "generation", "admit-legacy", "--plan"]) == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "status": "ready"}
    assert calls == [{}]

    assert cli.main(["xray", "generation", "admit-legacy"]) == 2
    assert json.loads(capsys.readouterr().out) == {
        "ok": False, "status": "blocked", "error_code": "ADMISSION_CONFIRMATION_REQUIRED",
    }
    assert calls == [{}]


def _install_core_owner_checkpoint(
    state_root, installed_root, *, status="complete", checkpoint_root=None, schema=2,
):
    import hashlib
    import uuid

    from fwrouter_api.services import native_proof
    from fwrouter_api.services.maintenance_lifecycle_admission import _lifecycle_paths

    checkpoint_root = checkpoint_root or installed_root
    manifest = native_proof.backend_manifest(checkpoint_root)
    digest = hashlib.sha256(native_proof._canonical_json(manifest)).hexdigest()
    active_units = {name: False for name in (
        "fwrouter-xray-sub-gateway.service", "fwrouter-docker-subject-events.service",
        "fwrouter-jobs-retention-dry-run.timer", "fwrouter-maintenance.timer",
        "fwrouter-subscription-refresh.timer", "fwrouter-traffic-collect.timer",
        "fwrouter-jobs-retention-dry-run.service", "fwrouter-maintenance.service",
        "fwrouter-subscription-refresh.service", "fwrouter-traffic-collect.service",
        "fwrouter-api.service",
    )}
    operation_id = str(uuid.uuid4())
    checkpoint = {
        "schema": schema, "operation_id": operation_id, "status": status,
        "created_at": "2026-10-29T00:00:00Z", "backend_manifest": manifest,
        "backend_manifest_sha256": digest, "active_units": active_units,
        "pending_units": [], "initial_backend_manifest_sha256": digest,
        "source_transitions": [],
    }
    if schema == 4:
        initial_digest = "f" * 64
        checkpoint.update(
            initial_backend_manifest_sha256=initial_digest,
            source_transitions=[{
                "operation_id": operation_id,
                "from_manifest_sha256": initial_digest,
                "to_manifest_sha256": digest,
                "created_at": "2026-10-29T00:01:00Z",
            }],
            maintenance_operation=None,
            runtime_quiescence=None,
        )
    _, path, _ = _lifecycle_paths(state_root)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    (state_root / "state").chmod(0o700)
    path.parent.chmod(0o700)
    path.write_text(json.dumps(checkpoint, sort_keys=True), encoding="utf-8")
    path.chmod(0o600)


def _core_owner_fixture(
    monkeypatch, tmp_path, *, status="complete", checkpoint_matches=True, schema=2,
):
    import os

    from fwrouter_api.services import maintenance_lifecycle_admission, native_proof

    state_root = tmp_path / "state-root"
    tool_root = tmp_path / "qualified-tool"
    installed_root = tmp_path / "installed-old"
    checkpoint_root = installed_root if checkpoint_matches else tmp_path / "other-old"
    for root in {tool_root, installed_root, checkpoint_root}:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        (root / "payload.py").write_text(f"ROOT = {root.name!r}\n", encoding="utf-8")
    installed_digest = native_proof.backend_manifest_sha256(installed_root)
    tool_digest = native_proof.backend_manifest_sha256(tool_root)
    _install_core_owner_checkpoint(
        state_root, installed_root, status=status, checkpoint_root=checkpoint_root, schema=schema,
    )
    monkeypatch.setattr(maintenance_lifecycle_admission, "_trusted_uid", lambda: os.geteuid())
    monkeypatch.setattr(maintenance_lifecycle_admission, "DEFAULT_STATE_ROOT", state_root)
    # The CLI gate remains literal effective UID 0 in production; tests model
    # that authorization separately from temp-file ownership under the runner.
    monkeypatch.setattr(cli, "_is_root_process", lambda: True)
    monkeypatch.setattr(native_proof, "_backend_root", lambda: tool_root)
    monkeypatch.setattr(native_proof, "_INSTALLED_BACKEND_ROOT", installed_root)
    return tool_digest, installed_digest


def test_core_ip_receipt_reattest_cli_admits_new_tool_against_complete_old_checkpoint(
    monkeypatch, capsys, tmp_path,
) -> None:
    from fwrouter_api.services import maintenance_lifecycle_admission, native_proof

    tool_digest, installed_digest = _core_owner_fixture(monkeypatch, tmp_path)
    generic_admission_called = False

    def generic_lease(_root):
        nonlocal generic_admission_called
        generic_admission_called = True
        raise AssertionError("Core owner command must not use generic startup manifest admission")

    monkeypatch.setattr(maintenance_lifecycle_admission, "runtime_shared_lease", generic_lease)
    captured: dict[str, object] = {}

    def fake_reattest(**kwargs):
        captured.update(kwargs)
        return {"status": "PASS", "action": "REATTEST", "plan_sha256": "d" * 64,
                "receipt_sha256": "e" * 64}

    monkeypatch.setattr(native_proof, "reattest_core_ip_receipt", fake_reattest)
    exit_code = cli.main([
        "core", "ip-receipt-reattest", "--apply",
        "--expected-tool-manifest-sha256", tool_digest,
        "--expected-installed-backend-manifest-sha256", installed_digest,
        "--expected-plan-sha256", "c" * 64,
        "--confirm", "CORE_IP_RECEIPT_REATTEST",
    ])
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert captured == {
        "expected_tool_manifest_sha256": tool_digest,
        "expected_installed_backend_manifest_sha256": installed_digest,
        "apply": True, "expected_plan_sha256": "c" * 64,
        "confirmation": "CORE_IP_RECEIPT_REATTEST",
    }
    assert output["status"] == "PASS"
    assert generic_admission_called is False


@pytest.mark.parametrize("apply", [False, True])
def test_core_ip_receipt_reattest_cli_admits_complete_schema4_checkpoint(
    monkeypatch, capsys, tmp_path, apply,
) -> None:
    from fwrouter_api.services import maintenance_lifecycle_admission, native_proof

    tool_digest, installed_digest = _core_owner_fixture(monkeypatch, tmp_path, schema=4)
    generic_admission_called = False

    def generic_lease(_root):
        nonlocal generic_admission_called
        generic_admission_called = True
        raise AssertionError("Core owner command must not use generic startup manifest admission")

    monkeypatch.setattr(maintenance_lifecycle_admission, "runtime_shared_lease", generic_lease)
    captured: dict[str, object] = {}

    def fake_reattest(**kwargs):
        captured.update(kwargs)
        return {"status": "PASS", "action": "REATTEST", "plan_sha256": "d" * 64,
                "receipt_sha256": "e" * 64}

    monkeypatch.setattr(native_proof, "reattest_core_ip_receipt", fake_reattest)
    args = [
        "core", "ip-receipt-reattest", "--apply" if apply else "--plan",
        "--expected-tool-manifest-sha256", tool_digest,
        "--expected-installed-backend-manifest-sha256", installed_digest,
    ]
    if apply:
        args.extend(("--expected-plan-sha256", "c" * 64,
                     "--confirm", "CORE_IP_RECEIPT_REATTEST"))
    exit_code = cli.main(args)
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output["status"] == "PASS"
    assert captured == {
        "expected_tool_manifest_sha256": tool_digest,
        "expected_installed_backend_manifest_sha256": installed_digest,
        "apply": apply,
        "expected_plan_sha256": "c" * 64 if apply else None,
        "confirmation": "CORE_IP_RECEIPT_REATTEST" if apply else None,
    }
    assert generic_admission_called is False


@pytest.mark.parametrize(
    ("schema", "mutation", "expected_reason"),
    [
        (1, None, "CORE_OWNER_COMPLETE_SCHEMA2_CHECKPOINT_REQUIRED"),
        (3, None, "CORE_OWNER_COMPLETE_SCHEMA2_CHECKPOINT_REQUIRED"),
        (5, None, "CORE_OWNER_COMPLETE_SCHEMA2_CHECKPOINT_REQUIRED"),
        (True, None, "CORE_OWNER_COMPLETE_SCHEMA2_CHECKPOINT_REQUIRED"),
        (2, "missing_key", "CORE_OWNER_COMPLETE_SCHEMA2_CHECKPOINT_REQUIRED"),
        (2, "unexpected_key", "CORE_OWNER_COMPLETE_SCHEMA2_CHECKPOINT_REQUIRED"),
        (2, "incomplete_status", "CORE_OWNER_COMPLETE_SCHEMA2_CHECKPOINT_REQUIRED"),
        (2, "pending_unit", "CORE_OWNER_CHECKPOINT_INVALID"),
        (4, "missing_key", "CORE_OWNER_COMPLETE_SCHEMA2_CHECKPOINT_REQUIRED"),
        (4, "missing_maintenance_key", "CORE_OWNER_COMPLETE_SCHEMA2_CHECKPOINT_REQUIRED"),
        (4, "unexpected_key", "CORE_OWNER_COMPLETE_SCHEMA2_CHECKPOINT_REQUIRED"),
        (4, "incomplete_status", "CORE_OWNER_COMPLETE_SCHEMA2_CHECKPOINT_REQUIRED"),
        (4, "maintenance_operation", "CORE_OWNER_CHECKPOINT_INVALID"),
        (4, "runtime_quiescence", "CORE_OWNER_CHECKPOINT_INVALID"),
        (4, "pending_unit", "CORE_OWNER_CHECKPOINT_INVALID"),
        (4, "operation_uuid", "CORE_OWNER_CHECKPOINT_INVALID"),
        (4, "manifest_digest", "CORE_OWNER_CHECKPOINT_INVALID"),
        (4, "transition_operation_uuid", "CORE_OWNER_CHECKPOINT_INVALID"),
    ],
)
def test_core_ip_receipt_reattest_cli_rejects_unsafe_checkpoint_shapes(
    monkeypatch, capsys, tmp_path, schema, mutation, expected_reason,
) -> None:
    from fwrouter_api.services import maintenance_lifecycle_admission, native_proof

    tool_digest, installed_digest = _core_owner_fixture(monkeypatch, tmp_path, schema=schema)
    state_root = tmp_path / "state-root"
    _, checkpoint_path, _ = maintenance_lifecycle_admission._lifecycle_paths(state_root)
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    if mutation == "missing_key":
        checkpoint.pop("runtime_quiescence" if schema == 4 else "source_transitions")
    elif mutation == "missing_maintenance_key":
        checkpoint.pop("maintenance_operation")
    elif mutation == "unexpected_key":
        checkpoint["phase"] = "complete"
    elif mutation == "incomplete_status":
        checkpoint["status"] = "pending"
    elif mutation == "maintenance_operation":
        checkpoint["maintenance_operation"] = {"operation_id": "not-null"}
    elif mutation == "runtime_quiescence":
        checkpoint["runtime_quiescence"] = {"status": "complete"}
    elif mutation == "pending_unit":
        checkpoint["pending_units"] = ["fwrouter-api.service"]
    elif mutation == "operation_uuid":
        checkpoint["operation_id"] = "not-a-uuid"
    elif mutation == "manifest_digest":
        checkpoint["backend_manifest_sha256"] = "0" * 64
    elif mutation == "transition_operation_uuid":
        checkpoint["source_transitions"][0]["operation_id"] = (
            "00000000-0000-0000-0000-000000000000"
        )
    checkpoint_path.write_text(json.dumps(checkpoint, sort_keys=True), encoding="utf-8")
    checkpoint_path.chmod(0o600)

    called = False

    def fake_reattest(**_kwargs):
        nonlocal called
        called = True
        return {"status": "PASS"}

    monkeypatch.setattr(native_proof, "reattest_core_ip_receipt", fake_reattest)
    assert cli.main([
        "core", "ip-receipt-reattest", "--plan",
        "--expected-tool-manifest-sha256", tool_digest,
        "--expected-installed-backend-manifest-sha256", installed_digest,
    ]) == 1
    assert json.loads(capsys.readouterr().out) == {
        "status": "BLOCKED", "reason": expected_reason,
    }
    assert called is False


def test_core_ip_receipt_reattest_cli_rejects_pending_checkpoint(monkeypatch, capsys, tmp_path) -> None:
    from fwrouter_api.services import native_proof

    tool_digest, installed_digest = _core_owner_fixture(monkeypatch, tmp_path, status="pending")
    called = False

    def fake_reattest(**_kwargs):
        nonlocal called
        called = True
        return {"status": "PASS"}

    monkeypatch.setattr(native_proof, "reattest_core_ip_receipt", fake_reattest)
    assert cli.main([
        "core", "ip-receipt-reattest", "--plan",
        "--expected-tool-manifest-sha256", tool_digest,
        "--expected-installed-backend-manifest-sha256", installed_digest,
    ]) == 1
    assert json.loads(capsys.readouterr().out) == {
        "status": "BLOCKED", "reason": "CORE_OWNER_COMPLETE_SCHEMA2_CHECKPOINT_REQUIRED",
    }
    assert called is False


def test_core_ip_receipt_reattest_cli_rejects_noninteger_schema_value(monkeypatch, capsys, tmp_path) -> None:
    from fwrouter_api.services import native_proof

    tool_digest, installed_digest = _core_owner_fixture(monkeypatch, tmp_path, schema=2.0)
    called = False

    def fake_reattest(**_kwargs):
        nonlocal called
        called = True
        return {"status": "PASS"}

    monkeypatch.setattr(native_proof, "reattest_core_ip_receipt", fake_reattest)
    assert cli.main([
        "core", "ip-receipt-reattest", "--plan",
        "--expected-tool-manifest-sha256", tool_digest,
        "--expected-installed-backend-manifest-sha256", installed_digest,
    ]) == 1
    assert json.loads(capsys.readouterr().out) == {
        "status": "BLOCKED", "reason": "CORE_OWNER_COMPLETE_SCHEMA2_CHECKPOINT_REQUIRED",
    }
    assert called is False


def test_core_ip_receipt_reattest_cli_rejects_checkpoint_for_other_installed_manifest(
    monkeypatch, capsys, tmp_path,
) -> None:
    from fwrouter_api.services import native_proof

    tool_digest, installed_digest = _core_owner_fixture(
        monkeypatch, tmp_path, checkpoint_matches=False,
    )
    called = False

    def fake_reattest(**_kwargs):
        nonlocal called
        called = True
        return {"status": "PASS"}

    monkeypatch.setattr(native_proof, "reattest_core_ip_receipt", fake_reattest)
    assert cli.main([
        "core", "ip-receipt-reattest", "--plan",
        "--expected-tool-manifest-sha256", tool_digest,
        "--expected-installed-backend-manifest-sha256", installed_digest,
    ]) == 1
    assert json.loads(capsys.readouterr().out) == {
        "status": "BLOCKED", "reason": "CORE_OWNER_INSTALLED_BACKEND_CHECKPOINT_MISMATCH",
    }
    assert called is False


def test_core_ip_receipt_reattest_cli_rejects_unpinned_executing_tool(monkeypatch, capsys, tmp_path) -> None:
    from fwrouter_api.services import native_proof

    _tool_digest, installed_digest = _core_owner_fixture(monkeypatch, tmp_path)
    called = False

    def fake_reattest(**_kwargs):
        nonlocal called
        called = True
        return {"status": "PASS"}

    monkeypatch.setattr(native_proof, "reattest_core_ip_receipt", fake_reattest)
    assert cli.main([
        "core", "ip-receipt-reattest", "--plan",
        "--expected-tool-manifest-sha256", "0" * 64,
        "--expected-installed-backend-manifest-sha256", installed_digest,
    ]) == 1
    assert json.loads(capsys.readouterr().out) == {
        "status": "BLOCKED", "reason": "CORE_OWNER_TOOL_MANIFEST_MISMATCH",
    }
    assert called is False


def test_core_ip_receipt_reattest_cli_rejects_nonroot_before_lifecycle_access(
    monkeypatch, capsys,
) -> None:
    from fwrouter_api.services import native_proof

    # Simulate a non-root caller on every CI runner without changing the actual
    # owner policy, which remains TRUSTED_HOST_UID == 0.
    monkeypatch.setattr(cli, "_is_root_process", lambda: False)
    monkeypatch.setattr(native_proof, "reattest_core_ip_receipt",
                        lambda **_kwargs: pytest.fail("non-root caller reached Core operation"))
    assert cli.main([
        "core", "ip-receipt-reattest", "--plan",
        "--expected-tool-manifest-sha256", "a" * 64,
        "--expected-installed-backend-manifest-sha256", "b" * 64,
    ]) == 1
    assert json.loads(capsys.readouterr().out) == {
        "status": "BLOCKED", "reason": "CORE_IP_RECEIPT_REATTEST_ROOT_REQUIRED",
    }
