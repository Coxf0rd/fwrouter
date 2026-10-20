from __future__ import annotations

import json
import hashlib
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace
from typing import Any

import pytest

from fwrouter_api.adapters.mihomo import DEFAULT_MIHOMO_ADAPTER, MihomoApplyResult
from fwrouter_api.db.connection import db_session
from fwrouter_api.services import mihomo_config as mihomo_config_service
from fwrouter_api.services.server_global_selection import (
    apply_global_auto_server,
    apply_global_fixed_server,
    clear_global_fixed_server,
    set_global_fixed_server,
    _restore_global_routing_state,
)
from fwrouter_api.services import selector as selector_service
from fwrouter_api.services.server_state import (
    ensure_routing_global_state,
    expire_global_fixed_server,
    get_routing_global_state,
)
from fwrouter_api.services.vpn_auto_selection_state import read_selection_revision


def _seed_server(server_id: str, *, vpn_auto: bool, global_list: bool) -> str:
    runtime_name = f"Runtime {server_id}"
    raw = {"name": runtime_name, "type": "socks5", "server": "198.18.0.10", "port": 12345}
    with db_session() as connection:
        connection.execute(
            """UPDATE modules SET desired_state='enabled', lifecycle_mode='managed',
                      apply_state='clean' WHERE module_name='core'""",
        )
        connection.execute(
            """
            INSERT INTO servers(server_id, server_name, provider_name, inventory_state, raw_json)
            VALUES (?, ?, 'fixed-mode-source-test', 'active', ?)
            """,
            (server_id, runtime_name, json.dumps(raw, sort_keys=True)),
        )
        connection.execute(
            """
            INSERT INTO server_preferences(server_id, vpn_auto, vpn_auto_priority, global_list)
            VALUES (?, ?, 0, ?)
            """,
            (server_id, int(vpn_auto), int(global_list)),
        )
    return runtime_name


def test_fixed_intent_marker_is_atomic_and_auto_intent_supersedes_it() -> None:
    _seed_server("fixed-workflow-marker", vpn_auto=False, global_list=False)
    _seed_server("fixed-workflow-marker-next", vpn_auto=False, global_list=False)

    fixed = set_global_fixed_server("fixed-workflow-marker", requested_by="pytest")
    with db_session() as connection:
        from fwrouter_api.services.xray_vpn_auto_pending import _load_row

        marker = _load_row(connection)
        assert marker is not None
        workflow = marker.get("workflow")
        assert marker["pending"] is True
        assert workflow["kind"] == "global_fixed_server"
        assert workflow["phase"] == "intent"
        assert workflow["selection_revision"] == fixed["selection_revision"]
        assert workflow["server_id"] == "fixed-workflow-marker"
        assert workflow["core_snapshot"] == ["enabled", "managed", "clean"]

    # A second explicit fixed intent is an authorized superseder and replaces
    # both the routing intent and its workflow marker in the same transaction.
    next_fixed = set_global_fixed_server("fixed-workflow-marker-next", requested_by="pytest")
    with db_session() as connection:
        marker = _load_row(connection)
        routing = dict(connection.execute(
            "SELECT server_mode, desired_fixed_server_id FROM routing_global_state WHERE id=1",
        ).fetchone())
    assert next_fixed["ok"] is True
    assert marker["revision"] > fixed["pending_revision"]
    assert marker["workflow"]["server_id"] == "fixed-workflow-marker-next"
    assert marker["workflow"]["selection_revision"] == next_fixed["selection_revision"]
    assert routing == {
        "server_mode": "fixed",
        "desired_fixed_server_id": "fixed-workflow-marker-next",
    }

    cleared = clear_global_fixed_server(requested_by="pytest")
    with db_session() as connection:
        from fwrouter_api.services.xray_vpn_auto_pending import _load_row

        marker = _load_row(connection)
    assert cleared["ok"] is True
    assert marker is not None
    assert marker["pending"] is True
    assert marker.get("workflow") is None


def test_generic_pending_mark_preserves_exact_fixed_workflow_revision() -> None:
    _seed_server("fixed-generic-mark", vpn_auto=False, global_list=False)
    fixed = set_global_fixed_server("fixed-generic-mark", requested_by="pytest")

    from fwrouter_api.services.xray_vpn_auto_pending import _load_row, mark_xray_vpn_auto_pending

    with db_session() as connection:
        before = _load_row(connection)
        generic_result = mark_xray_vpn_auto_pending(
            connection,
            trigger="ordinary_inventory_change",
            immediate=True,
        )
        after = _load_row(connection)

    assert fixed["ok"] is True
    assert generic_result == before == after


def test_generic_apply_now_reuses_fixed_owner_and_revision(monkeypatch) -> None:
    _seed_server("fixed-apply-now", vpn_auto=False, global_list=False)
    fixed = set_global_fixed_server("fixed-apply-now", requested_by="pytest")
    from fwrouter_api.services import xray_vpn_auto_pending as pending_service
    from fwrouter_api.services.xray_vpn_auto_pending import _load_row

    submitted: list[dict[str, Any]] = []

    def capture_submit(**kwargs: Any) -> dict[str, Any]:
        submitted.append(kwargs)
        return {"ok": True, "status": "captured"}

    monkeypatch.setattr(pending_service, "submit_xray_vpn_auto_reconcile", capture_submit)
    result = pending_service.request_xray_vpn_auto_apply_now(requested_by="pytest")
    with db_session() as connection:
        marker = _load_row(connection)

    assert result["status"] == "captured"
    assert submitted == [{
        "requested_by": "pytest",
        "bypass": True,
        "expected_revision": fixed["pending_revision"],
    }]
    assert marker is not None
    assert marker["revision"] == fixed["pending_revision"]
    assert marker["workflow"]["server_id"] == "fixed-apply-now"


def test_generic_core_mode_apply_is_blocked_while_fixed_workflow_is_pending(monkeypatch) -> None:
    _seed_server("fixed-generic-core", vpn_auto=False, global_list=False)
    fixed = set_global_fixed_server("fixed-generic-core", requested_by="pytest")
    from fwrouter_api.services import apply_orchestrator as orchestrator
    from fwrouter_api.services import apply_orchestrator_global_handlers as handlers
    from fwrouter_api.services.xray_vpn_auto_pending import _load_row

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("generic Core apply must stop before runtime, provider, or apply work")

    monkeypatch.setattr(orchestrator, "_current_routing_drift", forbidden)
    result = handlers._execute_set_global_mode(
        {"job_id": "generic-during-fixed", "requested_by": "pytest"},
        {"mode": "direct"},
    )
    with db_session() as connection:
        marker = _load_row(connection)
        routing = dict(connection.execute(
            "SELECT server_mode, desired_mode, desired_fixed_server_id FROM routing_global_state WHERE id=1",
        ).fetchone())
        revision = read_selection_revision(connection)

    assert result["ok"] is False
    assert result["code"] == "FIXED_WORKFLOW_PENDING"
    assert marker is not None and marker["revision"] == fixed["pending_revision"]
    assert marker["workflow"]["server_id"] == "fixed-generic-core"
    assert routing["server_mode"] == "fixed"
    assert routing["desired_fixed_server_id"] == "fixed-generic-core"
    assert revision == fixed["selection_revision"]


@pytest.mark.parametrize("operation", ["single", "replace"])
def test_preferences_mutations_are_rejected_without_side_effects_during_fixed_workflow(
    monkeypatch, operation: str,
) -> None:
    _seed_server("fixed-preferences-lock", vpn_auto=False, global_list=True)
    _seed_server("fixed-preferences-other", vpn_auto=True, global_list=True)
    fixed = set_global_fixed_server("fixed-preferences-lock", requested_by="pytest")
    from fwrouter_api.services import server_preferences as preferences_service
    from fwrouter_api.services.xray_vpn_auto_pending import _load_row

    def forbidden(**_kwargs: Any) -> Any:
        pytest.fail("blocked preferences must not reach reconciliation or native writers")

    monkeypatch.setattr(preferences_service, "_reconcile_mihomo_after_server_preferences", forbidden)
    if operation == "single":
        result = preferences_service.update_server_preferences(
            "fixed-preferences-lock",
            vpn_auto=True,
            reconcile_after_preferences=forbidden,
            requested_by="pytest",
        )
    else:
        result = preferences_service.replace_vpn_auto_servers(
            [],
            reconcile_after_preferences=forbidden,
            requested_by="pytest",
        )

    with db_session() as connection:
        marker = _load_row(connection)
        preference_rows = [dict(row) for row in connection.execute(
            "SELECT server_id, vpn_auto, vpn_auto_priority, vpn_auto_priority_origin, global_list "
            "FROM server_preferences WHERE server_id LIKE 'fixed-preferences-%' ORDER BY server_id",
        ).fetchall()]
        revision = read_selection_revision(connection)

    assert result["ok"] is False
    assert result["changed"] is False
    assert result["error_code"] == "FIXED_WORKFLOW_PENDING"
    assert marker is not None and marker["revision"] == fixed["pending_revision"]
    assert marker["workflow"]["server_id"] == "fixed-preferences-lock"
    assert preference_rows == [
        {
            "server_id": "fixed-preferences-lock",
            "vpn_auto": 0,
            "vpn_auto_priority": 0,
            "vpn_auto_priority_origin": "legacy",
            "global_list": 1,
        },
        {
            "server_id": "fixed-preferences-other",
            "vpn_auto": 1,
            "vpn_auto_priority": 0,
            "vpn_auto_priority_origin": "legacy",
            "global_list": 1,
        },
    ]
    assert revision == fixed["selection_revision"]


def test_pending_reconcile_dispatch_resumes_fixed_owner_not_generic_xray(monkeypatch) -> None:
    _seed_server("fixed-resume-owner", vpn_auto=False, global_list=False)
    fixed = set_global_fixed_server("fixed-resume-owner", requested_by="pytest")
    from fwrouter_api.services import server_global_selection, xray_vpn_auto_pending as pending_service
    from fwrouter_api.services.xray_vpn_auto_pending import _load_row

    resumed: list[dict[str, Any]] = []

    def capture_resume(workflow: dict[str, Any], *, requested_by: str) -> dict[str, Any]:
        resumed.append({"workflow": workflow, "requested_by": requested_by})
        return {"ok": False, "error_code": "SYNTHETIC_OWNER_PAUSED"}

    monkeypatch.setattr(pending_service, "pending_is_due", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(server_global_selection, "resume_global_fixed_server_workflow", capture_resume)
    result = pending_service.run_xray_vpn_auto_reconcile(
        requested_by="scheduler",
        bypass=True,
        expected_revision=fixed["pending_revision"],
    )
    with db_session() as connection:
        marker = _load_row(connection)

    assert result["ok"] is False
    assert resumed == [{
        "workflow": {
            **marker["workflow"],
            "pending_revision": fixed["pending_revision"],
        },
        "requested_by": "pytest",
    }]
    assert marker is not None
    assert marker["pending"] is True
    assert marker["revision"] == fixed["pending_revision"]
    assert marker["workflow"]["server_id"] == "fixed-resume-owner"


def test_generic_reconcile_rechecks_fixed_marker_after_waiting_for_writer(monkeypatch) -> None:
    _seed_server("fixed-reconcile-race", vpn_auto=False, global_list=False)
    from contextlib import contextmanager
    from threading import Event, Thread

    from fwrouter_api.adapters import xray_common
    from fwrouter_api.services import xray_vpn_auto_pending as pending_service
    from fwrouter_api.services.xray_vpn_auto_pending import _load_row

    waiting = Event()
    results: list[dict[str, Any]] = []
    failures: list[BaseException] = []
    real_guard = xray_common.xray_writer_guard

    @contextmanager
    def observed_guard(*, timeout_seconds: float | None = None) -> Any:
        waiting.set()
        with real_guard(timeout_seconds=timeout_seconds):
            yield

    def forbidden(*_args: Any, **_kwargs: Any) -> bool:
        pytest.fail("generic reconcile must not run inventory/provider/Xray work for a newly fixed marker")

    monkeypatch.setattr(xray_common, "xray_writer_guard", observed_guard)
    monkeypatch.setattr(pending_service, "_inventory_and_runtime_authoritative", forbidden)

    def run_generic_reconcile() -> None:
        try:
            results.append(pending_service.run_xray_vpn_auto_reconcile(
                requested_by="scheduler",
                bypass=True,
            ))
        except BaseException as exc:  # surface worker assertion failures in the parent test
            failures.append(exc)

    with real_guard():
        worker = Thread(target=run_generic_reconcile, daemon=True)
        worker.start()
        assert waiting.wait(5), "generic reconcile did not reach the guarded read"
        fixed = set_global_fixed_server("fixed-reconcile-race", requested_by="pytest")

    worker.join(5)
    assert not worker.is_alive(), "generic reconcile stayed blocked after writer release"
    assert failures == []
    assert fixed["ok"] is True
    assert results == [{
        "ok": True,
        "status": "fixed_owner_pending",
        "pending": True,
        "revision": fixed["pending_revision"],
    }]
    with db_session() as connection:
        marker = _load_row(connection)
    assert marker is not None
    assert marker["pending"] is True
    assert marker["revision"] == fixed["pending_revision"]
    assert marker["attempt_count"] == 0
    assert marker["workflow"]["server_id"] == "fixed-reconcile-race"


def test_fixed_intent_refuses_disabled_core_without_mutating_selection_or_marker() -> None:
    _seed_server("fixed-core-disabled", vpn_auto=False, global_list=False)
    with db_session() as connection:
        connection.execute(
            "UPDATE modules SET desired_state='disabled' WHERE module_name='core'",
        )
        before = read_selection_revision(connection)

    result = set_global_fixed_server("fixed-core-disabled", requested_by="pytest")

    with db_session() as connection:
        after = read_selection_revision(connection)
        from fwrouter_api.services.xray_vpn_auto_pending import _load_row

        marker = _load_row(connection)
    assert result["ok"] is False
    assert result["error_code"] == "CORE_NOT_ENABLED_MANAGED"
    assert after == before
    assert marker is None or marker.get("pending") is not True


def test_fixed_pending_blocks_ordinary_xray_generation_until_core_phase() -> None:
    _seed_server("fixed-xray-generation-gate", vpn_auto=False, global_list=False)
    fixed = set_global_fixed_server("fixed-xray-generation-gate", requested_by="pytest")
    from fwrouter_api.services.xray_subscription_service import reconcile_xray_subscription_profile_nodes
    from fwrouter_api.services.xray_vpn_auto_pending import _assert_xray_generation_authorized

    blocked = reconcile_xray_subscription_profile_nodes(requested_by="pytest")
    assert blocked["ok"] is False
    assert blocked["status"] == "blocked"
    assert blocked["error_code"] == "XRAY_GENERATION_BLOCKED_BY_FIXED_WORKFLOW"
    assert _assert_xray_generation_authorized() is False

    from fwrouter_api.services.server_global_selection import _update_fixed_workflow

    workflow = _update_fixed_workflow(fixed["pending_revision"], phase="core_verified")
    assert workflow is not None
    assert _assert_xray_generation_authorized() is False
    assert _assert_xray_generation_authorized(fixed["pending_revision"]) is True


@pytest.mark.parametrize(
    ("native_runtime_target", "selected_runtime_target", "selected_fixed_target", "expected_ok"),
    [
        ("Runtime fixed-xray-receipt", "REJECT", False, True),
        ("Runtime fixed-xray-receipt", "Runtime fixed-xray-receipt", True, True),
        ("foreign-runtime-target", "REJECT", False, False),
        ("foreign-runtime-target", "Runtime fixed-xray-receipt", True, False),
    ],
)
def test_fixed_xray_generation_receipt_rebinds_changed_owner_snapshot(
    monkeypatch, tmp_path: Path, native_runtime_target: str,
    selected_runtime_target: str, selected_fixed_target: bool, expected_ok: bool,
) -> None:
    from fwrouter_api.services import core_intent, mihomo_config_paths, mihomo_config_status, mihomo_reconcile, mihomo_runtime
    from fwrouter_api.services.server_global_selection import (
        _capture_fixed_apply_context,
        _fixed_xray_generation_callback,
        _fixed_xray_generation_receipt_matches,
        _update_fixed_workflow,
    )
    from fwrouter_api.services.vpn_auto_selection_state import advance_selection_revision
    from fwrouter_api.services.xray_vpn_auto_pending import _load_row, pending_fixed_workflow_state
    from fwrouter_api.services.mihomo_reconcile import _invoke_verification_callback

    _seed_server("fixed-xray-receipt", vpn_auto=False, global_list=False)
    from fwrouter_api.services.mihomo_config_status import _resolve_active_fixed_runtime_target

    mihomo_target = "Runtime fixed-xray-receipt"
    assert _resolve_active_fixed_runtime_target(
        "fixed-xray-receipt", expected_targets={mihomo_target},
    ) == mihomo_target
    assert mihomo_target != "fixed-xray-receipt"
    core_context = {"boot_id": "synthetic-boot", "network_namespace_inode": "42"}
    runtime_incarnation = {"value": "mihomo-before-xray-generation"}
    input_fingerprint = {"value": "1" * 64}
    active_config = tmp_path / "mihomo.yaml"
    active_config.write_text("generation: before\n", encoding="utf-8")
    monkeypatch.setattr(core_intent, "read_core_runtime_context", lambda: dict(core_context))
    monkeypatch.setattr(mihomo_runtime, "get_mihomo_runtime_incarnation", lambda **_kwargs: runtime_incarnation["value"])
    monkeypatch.setattr(mihomo_config_paths, "_resolved_base_config_path", lambda: str(active_config))
    monkeypatch.setattr(
        mihomo_reconcile, "current_mihomo_input_fingerprint",
        lambda _routing: {"hash": input_fingerprint["value"]},
    )
    monkeypatch.setattr(
        mihomo_config_status, "mihomo_runtime_fixed_target_materialized",
        lambda _routing, require_selected=False: {
            "ok": True,
            "fixed_runtime_target": native_runtime_target,
            "config_membership_ok": True,
            "native_membership_ok": True,
            "logical_group_native_membership_ok": True,
            "vpn_global_now": selected_runtime_target,
            "vpn_global_selected_target": selected_runtime_target,
            "selected_fixed_target": selected_fixed_target,
        },
    )

    fixed = set_global_fixed_server("fixed-xray-receipt", requested_by="pytest")
    assert fixed["ok"] is True
    workflow = _update_fixed_workflow(
        fixed["pending_revision"], phase="mihomo_verified", mihomo_generation_mode="unchanged",
    )
    assert workflow is not None
    with db_session() as connection:
        connection.execute(
            "UPDATE routing_global_state SET applied_fixed_server_id=?, apply_state='clean' WHERE id=1",
            ("fixed-xray-receipt",),
        )
        mihomo_stage_revision = advance_selection_revision(
            connection, expected_revision=workflow["selection_revision"],
        )
    assert mihomo_stage_revision == workflow["selection_revision"] + 1
    workflow = _update_fixed_workflow(
        fixed["pending_revision"], phase="core_verified", selection_revision=mihomo_stage_revision,
    )
    assert workflow is not None
    fixed["routing"]["applied_fixed_server_id"] = "fixed-xray-receipt"
    fixed["routing"]["apply_state"] = "clean"
    token = _capture_fixed_apply_context(fixed["routing"])
    assert token is not None
    original_revision = token["selection_revision"]

    # Model the generation owner completing a staged Mihomo write/restart.
    # The callback must bind the resulting revision and runtime/config hashes
    # only while the original fixed/Core/pending intent still owns the state.
    with db_session() as connection:
        generated_revision = advance_selection_revision(connection, expected_revision=original_revision)
    assert generated_revision == original_revision + 1
    active_config.write_text("generation: after\n", encoding="utf-8")
    runtime_incarnation["value"] = "mihomo-after-xray-generation"
    input_fingerprint["value"] = "2" * 64

    callback = _fixed_xray_generation_callback(
        server_id="fixed-xray-receipt",
        mihomo_target=mihomo_target,
        fixed_until=str(fixed["routing"]["fixed_server_until"]),
        token=token,
    )
    receipt = _invoke_verification_callback(
        callback,
        operation_id="owned-xray-generation-operation",
        expected_revision=generated_revision,
    )
    assert receipt["ok"] is expected_ok, receipt
    if not expected_ok:
        assert receipt["error_code"] == "FIXED_XRAY_MIHOMO_PROOF_FAILED"
        assert "xray_generation_operation_id" not in token
        with db_session() as connection:
            pending = _load_row(connection)
        assert pending["workflow"]["phase"] == "core_verified"
        assert "xray_generation_operation_id" not in pending["workflow"]
        return
    assert receipt["selection_revision"] == generated_revision
    assert receipt["fixed_pending_revision"] == fixed["pending_revision"]
    assert receipt["mihomo_input_sha256"] == input_fingerprint["value"]
    assert receipt["mihomo_config_sha256"] == hashlib.sha256(active_config.read_bytes()).hexdigest()
    assert receipt["mihomo_runtime_incarnation"] == runtime_incarnation["value"]
    assert token["xray_generation_operation_id"] == "owned-xray-generation-operation"

    with db_session() as connection:
        pending = _load_row(connection)
    saved_workflow = pending.get("workflow") if isinstance(pending, dict) else None
    assert pending is not None and pending["pending"] is True
    assert saved_workflow["phase"] == "core_verified"
    assert saved_workflow["selection_revision"] == generated_revision
    assert saved_workflow["xray_generation_operation_id"] == "owned-xray-generation-operation"
    assert pending_fixed_workflow_state()["pending_revision"] == fixed["pending_revision"]

    result = {
        "ok": True,
        "status": "success",
        "pre_publication_verification": receipt,
        "generation_apply": {
            "final_mihomo": {"ok": True, "container": {"selection_revision": generated_revision}},
        },
    }
    assert _fixed_xray_generation_receipt_matches(
        result, server_id="fixed-xray-receipt", token=token,
    ) is True
    result["pre_publication_verification"] = {**receipt, "operation_id": "unrelated-operation"}
    assert _fixed_xray_generation_receipt_matches(
        result, server_id="fixed-xray-receipt", token=token,
    ) is False


def test_fixed_target_outside_auto_and_global_lists_is_generated_only_for_vpn_global() -> None:
    runtime_name = _seed_server("fixed-outside-lists", vpn_auto=False, global_list=False)

    config = mihomo_config_service.build_mihomo_config(
        {
            "server_mode": "fixed",
            "desired_fixed_server_id": "fixed-outside-lists",
            "applied_fixed_server_id": "fixed-outside-lists",
            "desired_mode": "selective",
            "applied_mode": "selective",
            "selective_default": "direct",
        },
        xray_handoff_assignments=[],
    )

    proxies = {str(proxy.get("name")) for proxy in config.get("proxies", []) if isinstance(proxy, dict)}
    groups = {
        str(group.get("name")): list(group.get("proxies") or [])
        for group in config.get("proxy-groups", [])
        if isinstance(group, dict)
    }
    assert runtime_name in proxies
    assert groups["vpn-global"] == ["REJECT", runtime_name]
    assert "vpn-auto" not in groups["vpn-global"]
    assert runtime_name not in groups["vpn-auto"]


def test_fixed_logical_profile_beyond_default_inventory_limit_gets_owned_group() -> None:
    from fwrouter_api.services.custom_servers import resolve_mihomo_runtime_proxy_rows
    from fwrouter_api.services.mihomo_config_proxies import (
        _ensure_selector_groups,
        _logical_profile_groups,
        _merge_runtime_proxies,
    )

    filler_count = 1000
    filler_rows = []
    filler_preferences = []
    for index in range(filler_count):
        server_id = f"limit-filler-{index:04d}"
        server_name = f"00 filler {index:04d}"
        raw = {"name": server_name, "type": "socks5", "server": "198.18.0.10", "port": 12345}
        filler_rows.append((server_id, server_name, json.dumps(raw, sort_keys=True)))
        filler_preferences.append((server_id, 0, 1))

    fixed_id = "zz-fixed-logical-profile"
    group_name = "Fixed Logical Profile [profile123456]"
    member_identity = "member-profile-1"
    member_config = {
        "type": "vless",
        "server": "198.18.0.44",
        "port": 443,
        "uuid": "00000000-0000-4000-8000-000000000001",
        "name": "Profile member",
    }
    target_raw = {
        "name": group_name,
        "_fwrouter_runtime_name": group_name,
        "_fwrouter_topology": {
            "kind": "logical_profile",
            "runtime_policy": "fallback",
            "endpoints": [{"identity": member_identity, "runtime": member_config}],
        },
    }
    with db_session() as connection:
        connection.executemany(
            "INSERT INTO servers(server_id, server_name, provider_name, inventory_state, raw_json) "
            "VALUES (?, ?, 'synthetic-fixed-profile-test', 'active', ?)",
            filler_rows,
        )
        connection.executemany(
            "INSERT INTO server_preferences(server_id, vpn_auto, global_list) VALUES (?, ?, ?)",
            filler_preferences,
        )
        connection.execute(
            "INSERT INTO servers(server_id, server_name, provider_name, inventory_state, raw_json) "
            "VALUES (?, ?, 'synthetic-fixed-profile-test', 'active', ?)",
            (fixed_id, group_name, json.dumps(target_raw, sort_keys=True)),
        )
        connection.execute(
            "INSERT INTO server_preferences(server_id, vpn_auto, global_list) VALUES (?, 0, 0)",
            (fixed_id,),
        )

    assert not any(group.get("name") == group_name for group in _logical_profile_groups())
    explicit_rows = resolve_mihomo_runtime_proxy_rows(
        inventory_state="active",
        limit=1000,
        explicit_server_ids={fixed_id},
    )
    assert any(row.get("server_id") == fixed_id for row in explicit_rows)

    routing = {
        "server_mode": "fixed",
        "desired_fixed_server_id": fixed_id,
        "applied_fixed_server_id": fixed_id,
        "desired_mode": "selective",
        "applied_mode": "selective",
        "selective_default": "direct",
    }
    config = {"proxies": _merge_runtime_proxies({}, routing=routing), "proxy-groups": []}
    groups = {
        str(group.get("name")): group
        for group in _ensure_selector_groups(config, routing=routing)
        if isinstance(group, dict)
    }
    expected_member = f"{group_name} :: {member_identity[:12]}"
    assert groups[group_name]["proxies"] == [expected_member]
    assert any(proxy.get("name") == expected_member for proxy in config["proxies"])
    assert groups["vpn-global"]["proxies"] == ["REJECT", group_name]
    assert group_name not in groups["vpn-auto"]["proxies"]
    assert expected_member not in groups["vpn-auto"]["proxies"]


@pytest.mark.parametrize(
    "invalid_members",
    [
        ["REJECT", "Runtime fixed-membership", "vpn-auto"],
        ["Runtime fixed-membership"],
        ["REJECT", "Runtime different-target"],
    ],
)
def test_fixed_selector_validation_requires_exact_failclosed_target_membership(invalid_members: list[str]) -> None:
    _seed_server("fixed-membership", vpn_auto=False, global_list=False)
    routing = {
        "server_mode": "fixed",
        "desired_fixed_server_id": "fixed-membership",
        "applied_fixed_server_id": "fixed-membership",
        "desired_mode": "selective",
        "applied_mode": "selective",
        "selective_default": "direct",
    }
    config = mihomo_config_service.build_mihomo_config(routing, xray_handoff_assignments=[])
    from fwrouter_api.services.mihomo_config_validation import _validate_candidate_structure

    assert _validate_candidate_structure(config, routing=routing)["fixed_global_membership_ok"] is True
    groups = {str(group.get("name")): group for group in config.get("proxy-groups", []) if isinstance(group, dict)}
    groups["vpn-global"]["proxies"] = invalid_members
    assert _validate_candidate_structure(config, routing=routing)["fixed_global_membership_ok"] is False


def test_targetless_fixed_direct_keeps_legacy_group_without_fixed_proof() -> None:
    runtime_name = _seed_server("fixed-direct-legacy", vpn_auto=True, global_list=True)
    routing = {
        "server_mode": "fixed",
        "desired_fixed_server_id": None,
        "applied_fixed_server_id": None,
        "desired_mode": "direct",
        "applied_mode": "direct",
        "selective_default": "direct",
    }
    config = mihomo_config_service.build_mihomo_config(routing, xray_handoff_assignments=[])
    from fwrouter_api.services.mihomo_config_validation import _validate_candidate_structure

    structure = _validate_candidate_structure(config, routing=routing)
    groups = {
        str(group.get("name")): list(group.get("proxies") or [])
        for group in config.get("proxy-groups", [])
        if isinstance(group, dict)
    }
    assert groups["vpn-global"] == ["vpn-auto", runtime_name, "REJECT"]
    assert structure["fixed_targetless_direct_compat"] is True
    assert structure["fixed_targetless_intent_supported"] is True
    assert structure["fixed_global_membership_required"] is False
    assert structure["fixed_global_membership_ok"] is False
    assert structure["selector_groups_ok"] is True


@pytest.mark.parametrize("mode", ["selective", "vpn"])
def test_targetless_fixed_non_direct_candidate_is_fail_closed_and_rejected(mode: str) -> None:
    _seed_server(f"fixed-no-target-{mode}", vpn_auto=True, global_list=True)
    routing = {
        "server_mode": "fixed",
        "desired_fixed_server_id": None,
        "applied_fixed_server_id": None,
        "desired_mode": mode,
        "applied_mode": mode,
        "selective_default": "vpn" if mode == "vpn" else "direct",
    }
    config = mihomo_config_service.build_mihomo_config(routing, xray_handoff_assignments=[])
    from fwrouter_api.services.mihomo_config_validation import _validate_candidate_structure

    structure = _validate_candidate_structure(config, routing=routing)
    groups = {
        str(group.get("name")): list(group.get("proxies") or [])
        for group in config.get("proxy-groups", [])
        if isinstance(group, dict)
    }
    assert groups["vpn-global"] == ["REJECT"]
    assert structure["fixed_targetless_direct_compat"] is False
    assert structure["fixed_targetless_intent_supported"] is False
    assert structure["fixed_global_membership_ok"] is False
    assert structure["selector_groups_ok"] is False


def test_fixed_routing_status_requires_live_ttl_exact_membership_and_selection(
    monkeypatch, tmp_path: Path,
) -> None:
    import yaml

    from fwrouter_api.services import mihomo_config_paths, mihomo_config_status

    runtime_name = "Runtime fixed-native-proof"
    _seed_server("fixed-native-proof", vpn_auto=False, global_list=False)
    with db_session() as connection:
        connection.execute(
            "UPDATE servers SET raw_json=? WHERE server_id=?",
            (json.dumps({
                "name": runtime_name,
                "type": "socks5",
                "server": "198.18.0.11",
                "port": 12345,
                "_fwrouter_runtime_name": runtime_name,
            }), "fixed-native-proof"),
        )
    selected = set_global_fixed_server("fixed-native-proof", requested_by="pytest")
    assert selected["ok"] is True
    with db_session() as connection:
        connection.execute(
            """UPDATE routing_global_state
               SET applied_fixed_server_id=desired_fixed_server_id,
                   apply_state='clean', fixed_server_until=datetime('now', '+1 day')
               WHERE id=1"""
        )
    routing = get_routing_global_state(expire_ttl=False)
    config = mihomo_config_service.build_mihomo_config(routing, xray_handoff_assignments=[])
    config_path = tmp_path / "fixed-status.yaml"
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    monkeypatch.setattr(mihomo_config_paths, "_resolved_base_config_path", lambda: str(config_path))
    monkeypatch.setattr(mihomo_config_status, "_resolved_base_config_path", lambda: str(config_path))

    groups = {
        str(group.get("name")): list(group.get("proxies") or [])
        for group in config.get("proxy-groups", [])
        if isinstance(group, dict)
    }
    runtime_readback = {"vpn-global-now": runtime_name}

    def health():
        current = runtime_readback["vpn-global-now"]
        return SimpleNamespace(
            runtime_state="running",
            active_server_id=current,
            details={
                "selectors": {
                    "vpn_global_exists": True,
                    "vpn_global_has_vpn_auto": "vpn-auto" in groups.get("vpn-global", []),
                    "vpn_global_targets": list(groups.get("vpn-global", [])),
                    "vpn_global_now": current,
                    "vpn_global_selected_target": current,
                    "vpn_auto_targets": list(groups.get("vpn-auto", [])),
                    "vpn_auto_now": "REJECT",
                    "vpn_auto_selected_target": "REJECT",
                    "subject_selector_targets": {},
                },
                "config": {"fwrouter_contours": {"transparent_vpn": {
                    "transparent_tcp_ready": True,
                    "transparent_udp_ready": True,
                    "transparent_tcp_listener_socket_present": True,
                    "transparent_udp_listener_socket_present": True,
                }}},
            },
        )

    monkeypatch.setattr(mihomo_config_status.DEFAULT_MIHOMO_ADAPTER, "health", health)
    valid = mihomo_config_status.mihomo_runtime_satisfies_routing(routing)
    assert valid["ok"] is True, valid
    assert valid["vpn_egress_ready"] is True

    runtime_readback["vpn-global-now"] = "REJECT"
    unselected = mihomo_config_status.mihomo_runtime_satisfies_routing(routing)
    assert unselected["ok"] is False
    assert unselected["selector_ok"] is False

    runtime_readback["vpn-global-now"] = runtime_name
    groups["vpn-global"].insert(1, "vpn-auto")
    extra_member = mihomo_config_status.mihomo_runtime_satisfies_routing(routing)
    assert extra_member["ok"] is False
    assert extra_member["selector_native_membership_ok"] is False

    with db_session() as connection:
        connection.execute("UPDATE routing_global_state SET fixed_server_until='garbage' WHERE id=1")
    malformed_ttl = mihomo_config_status.mihomo_runtime_satisfies_routing(routing)
    assert malformed_ttl["ok"] is False
    assert malformed_ttl["selector_ok"] is False


def test_same_fixed_target_request_reserves_a_new_selection_revision() -> None:
    _seed_server("fixed-same-target", vpn_auto=False, global_list=False)

    first = set_global_fixed_server("fixed-same-target", requested_by="pytest")
    with db_session() as connection:
        first_revision = read_selection_revision(connection)
    second = set_global_fixed_server("fixed-same-target", requested_by="pytest")
    with db_session() as connection:
        second_revision = read_selection_revision(connection)

    assert first["ok"] is True
    assert second["ok"] is True
    assert second_revision == first_revision + 1
    assert second["selection_revision"] == second_revision


def test_stale_fixed_failure_rollback_cannot_replace_newer_auto_intent() -> None:
    _seed_server("fixed-stale-rollback", vpn_auto=False, global_list=False)
    fixed = set_global_fixed_server("fixed-stale-rollback", requested_by="pytest")
    previous = {
        "desired_mode": "direct",
        "applied_mode": "direct",
        "selective_default": "direct",
        "server_mode": "auto",
        "desired_fixed_server_id": None,
        "applied_fixed_server_id": None,
        "fixed_server_until": None,
        "apply_state": "clean",
        "error_code": None,
        "error_message": None,
    }
    clear = clear_global_fixed_server(requested_by="pytest")

    rollback = _restore_global_routing_state(
        previous,
        expected_revision=fixed["selection_revision"],
        expected_server_id="fixed-stale-rollback",
        expected_fixed_until=str(fixed["routing"]["fixed_server_until"]),
    )

    routing = get_routing_global_state(expire_ttl=False)
    assert clear["ok"] is True
    assert rollback["ok"] is False
    assert rollback["error_code"] == "GLOBAL_FIXED_SERVER_STALE"
    assert routing["server_mode"] == "auto"
    assert routing["desired_fixed_server_id"] is None


def test_fixed_ttl_expiry_advances_selection_fence_without_runtime_apply() -> None:
    _seed_server("fixed-expired", vpn_auto=False, global_list=False)
    set_global_fixed_server("fixed-expired", requested_by="pytest")
    with db_session() as connection:
        connection.execute(
            """UPDATE routing_global_state
               SET applied_fixed_server_id='fixed-expired', apply_state='clean',
                   fixed_server_until=datetime('now', '-1 second') WHERE id=1"""
        )
        revision_before = read_selection_revision(connection)

    result = expire_global_fixed_server(dry_run=False, apply_runtime=False)

    routing = get_routing_global_state(expire_ttl=False)
    with db_session() as connection:
        revision_after = read_selection_revision(connection)
        from fwrouter_api.services.xray_vpn_auto_pending import _load_row

        pending = _load_row(connection)
    assert result["expired_global_fixed_server_count"] == 1
    assert result["runtime_apply"]["skipped"] is True
    assert routing["server_mode"] == "auto"
    assert routing["apply_state"] == "pending"
    assert revision_after > revision_before
    assert pending is not None
    assert pending["trigger"] == "global_fixed_server_ttl_expired"
    assert pending.get("workflow") is None


@pytest.mark.parametrize("drift", ["core_revision", "source", "runtime_incarnation", "core_context"])
def test_fixed_apply_token_rejects_core_source_and_runtime_drift(monkeypatch, drift: str) -> None:
    _seed_server(f"fixed-token-{drift}", vpn_auto=False, global_list=False)
    from fwrouter_api.services import core_intent, mihomo_runtime, server_global_selection

    runtime_context = {
        "boot_id": "00000000-0000-0000-0000-000000000001",
        "network_namespace_inode": "1",
    }
    incarnation = {"value": "fixed-token-incarnation"}
    monkeypatch.setattr(core_intent, "read_core_runtime_context", lambda: dict(runtime_context))
    monkeypatch.setattr(
        mihomo_runtime,
        "get_mihomo_runtime_incarnation",
        lambda **_kwargs: incarnation["value"],
    )
    desired = set_global_fixed_server(f"fixed-token-{drift}", requested_by="pytest")
    assert desired["ok"] is True
    token = server_global_selection._capture_fixed_apply_context(desired["routing"])
    assert isinstance(token, dict)
    assert server_global_selection._fixed_apply_token_matches(
        server_id=f"fixed-token-{drift}",
        fixed_until=str(desired["routing"]["fixed_server_until"]),
        token=token,
    ) is True

    if drift == "core_revision":
        with db_session() as connection:
            from fwrouter_api.services.core_intent import advance_core_intent_revision

            advance_core_intent_revision(connection)
    elif drift == "source":
        with db_session() as connection:
            connection.execute(
                "UPDATE servers SET server_name='Changed source row' WHERE server_id=?",
                (f"fixed-token-{drift}",),
            )
    elif drift == "runtime_incarnation":
        incarnation["value"] = "new-runtime-incarnation"
    else:
        runtime_context["network_namespace_inode"] = "2"

    assert server_global_selection._fixed_apply_token_matches(
        server_id=f"fixed-token-{drift}",
        fixed_until=str(desired["routing"]["fixed_server_until"]),
        token=token,
    ) is False


@pytest.mark.parametrize(
    ("failure_stage", "expected_apply_calls"),
    [
        ("owner_fence_before_selection", 0),
        ("membership_before_selection", 0),
        ("selected_proof_before_selection", 0),
        ("selected_proof_after_selection", 1),
        ("owner_fence_after_selection", 1),
    ],
)
def test_fixed_selection_stale_diagnostics_attribute_existing_fenced_paths(
    monkeypatch, tmp_path: Path, failure_stage: str, expected_apply_calls: int,
) -> None:
    """Exercise stale attribution through fixed apply with isolated SQLite intent."""
    server_id = f"fixed-stage-{failure_stage}"
    runtime_name = _seed_server(server_id, vpn_auto=False, global_list=False)
    fixed = set_global_fixed_server(server_id, requested_by="pytest")
    assert fixed["ok"] is True
    from fwrouter_api.adapters import mihomo as mihomo_adapter_module
    from fwrouter_api.services import (
        core_intent, mihomo_config_paths, mihomo_reconcile, mihomo_runtime,
        server_global_selection,
    )
    from fwrouter_api.services.vpn_auto_selection_state import advance_selection_revision
    from fwrouter_api.db.connection import db_session

    monkeypatch.setattr(core_intent, "read_core_runtime_context", lambda: {
        "boot_id": "00000000-0000-0000-0000-000000000001",
        "network_namespace_inode": "1",
    })
    monkeypatch.setattr(mihomo_runtime, "get_mihomo_runtime_incarnation", lambda **_kwargs: "stage-test-runtime")
    active_config = tmp_path / "active.yaml"
    active_config.write_text("fixed: true\n", encoding="utf-8")
    monkeypatch.setattr(mihomo_config_paths, "_resolved_base_config_path", lambda: active_config)
    monkeypatch.setattr(
        mihomo_reconcile, "current_mihomo_input_fingerprint",
        lambda _routing: {"hash": "a" * 64},
    )
    native_calls: list[tuple[str, str]] = []
    selected = {"value": "vpn-auto"}

    def bump_selection_revision() -> None:
        with db_session() as connection:
            revision = read_selection_revision(connection)
            advance_selection_revision(connection, expected_revision=revision)

    def apply_selector(selector: str, target: str):
        native_calls.append((selector, target))
        selected["value"] = target
        if failure_stage == "owner_fence_after_selection":
            bump_selection_revision()
        return MihomoApplyResult(ok=True, message="synthetic native boundary", active_server_id=target)

    monkeypatch.setattr(
        mihomo_adapter_module, "DEFAULT_MIHOMO_ADAPTER",
        SimpleNamespace(get_active_server_id=lambda: selected["value"], apply_server_to_selector=apply_selector),
    )

    def valid_generation(routing, *, token, job_id):
        del routing
        if failure_stage == "owner_fence_before_selection":
            bump_selection_revision()
        fence = {
            "schema": "mihomo-reentry-generation-fence/v1",
            "operation_id": "stage-diagnostic-operation",
            "job_id": job_id,
            "before_revision": token.get("source_selection_revision", token["selection_revision"]),
            "selection_revision": token.get("source_selection_revision", token["selection_revision"]),
            "runtime_incarnation": token["runtime_incarnation"],
            "active_config_hash": token["active_config_hash"],
            "native_proof_verified": True,
            "publication_verified": True,
            "unchanged_config": True,
        }
        return {
            "ok": True,
            "operation_id": fence["operation_id"],
            "job_id": job_id,
            "reentry_generation_fence": fence,
        }

    monkeypatch.setattr(server_global_selection, "_materialize_fixed_server_generation", valid_generation)

    calls = 0

    def materialized_proof(_routing, *, require_selected=False):
        nonlocal calls
        calls += 1
        proof = {
            "ok": True,
            "fixed_runtime_target": runtime_name,
            "config_membership_ok": True,
            "native_membership_ok": True,
            "logical_group_native_membership_ok": True,
            "vpn_global_now": selected["value"],
            "selected_fixed_target": selected["value"] == runtime_name,
            "reason": "fixed_target_native_membership_unverified",
            "private_token": "must-not-escape",
            "config_path": "/private/config.yaml",
        }
        if failure_stage == "membership_before_selection" and calls == 2:
            proof.update(ok=False, config_membership_ok=False)
        elif failure_stage == "selected_proof_before_selection" and calls == 3:
            proof.update(ok=False, native_membership_ok=False)
        elif failure_stage == "selected_proof_after_selection" and calls == 4:
            proof.update(ok=False, selected_fixed_target=False)
        if require_selected:
            proof["ok"] = proof["ok"] and selected["value"] == runtime_name
        return proof

    monkeypatch.setattr(
        "fwrouter_api.services.mihomo_config_status.mihomo_runtime_fixed_target_materialized",
        materialized_proof,
    )
    monkeypatch.setattr(
        "fwrouter_api.services.server_ping.check_server_delay",
        lambda *_args, **_kwargs: {"ok": True},
    )

    result = apply_global_fixed_server(server_id, requested_by="pytest")
    assert result["ok"] is False
    assert result["error_code"] == "GLOBAL_FIXED_SERVER_STALE", result
    assert result["fixed_selection_stage"] == failure_stage
    assert len(native_calls) == expected_apply_calls
    assert result["fixed_selection_proof"] is not None
    assert "private_token" not in result["fixed_selection_proof"]
    assert "config_path" not in result["fixed_selection_proof"]
    assert result["fixed_selection_proof"]["ok"] in {True, False, None}
    routing_after = get_routing_global_state(expire_ttl=False)
    assert routing_after["server_mode"] == "fixed"
    assert routing_after["desired_fixed_server_id"] == server_id
    assert routing_after["applied_fixed_server_id"] is None
    assert routing_after["apply_state"] == "pending"
    with db_session() as connection:
        from fwrouter_api.services.xray_vpn_auto_pending import _load_row

        pending = _load_row(connection)
    assert pending is not None and pending["pending"] is True
    assert pending["workflow"]["phase"] in {"intent", "mihomo_reserved", "mihomo_generation_verified"}


def test_fixed_apply_uses_real_membership_proof_to_select_from_reject(
    monkeypatch, tmp_path: Path,
) -> None:
    """The real verifier must admit a valid target when native selection is REJECT."""
    import yaml

    server_id = "fixed-real-membership-from-reject"
    runtime_name = _seed_server(server_id, vpn_auto=False, global_list=False)
    from fwrouter_api.services import (
        core_intent, mihomo_config_paths, mihomo_config_status,
        mihomo_reconcile, mihomo_runtime, server_global_selection,
    )

    active_config = tmp_path / "mihomo.yaml"
    routing = {
        "server_mode": "fixed",
        "desired_fixed_server_id": server_id,
        "applied_fixed_server_id": server_id,
        "desired_mode": "selective",
        "applied_mode": "selective",
        "selective_default": "direct",
    }
    config = mihomo_config_service.build_mihomo_config(routing, xray_handoff_assignments=[])
    active_config.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    monkeypatch.setattr(mihomo_config_paths, "_resolved_base_config_path", lambda: active_config)
    monkeypatch.setattr(mihomo_config_status, "_resolved_base_config_path", lambda: active_config)
    monkeypatch.setattr(core_intent, "read_core_runtime_context", lambda: {
        "boot_id": "00000000-0000-0000-0000-000000000001",
        "network_namespace_inode": "1",
    })
    monkeypatch.setattr(mihomo_runtime, "get_mihomo_runtime_incarnation", lambda **_kwargs: "real-proof-runtime")
    monkeypatch.setattr(
        mihomo_reconcile, "current_mihomo_input_fingerprint",
        lambda _routing: {"hash": "a" * 64},
    )

    state = {"vpn-global": "REJECT"}

    def health():
        groups = {
            str(group.get("name")): list(group.get("proxies") or [])
            for group in config.get("proxy-groups", [])
            if isinstance(group, dict)
        }
        return SimpleNamespace(
            runtime_state="running",
            active_server_id=state["vpn-global"],
            details={"selectors": {
                "vpn_global_exists": True,
                "vpn_global_has_vpn_auto": "vpn-auto" in groups.get("vpn-global", []),
                "vpn_global_targets": groups.get("vpn-global", []),
                "vpn_auto_targets": groups.get("vpn-auto", []),
                "vpn_global_now": state["vpn-global"],
                "vpn_global_selected_target": state["vpn-global"],
            }},
        )

    native_calls: list[tuple[str, str]] = []
    from fwrouter_api.services.vpn_auto_selection_state import advance_selection_revision

    def apply_selector(selector: str, target: str) -> MihomoApplyResult:
        native_calls.append((selector, target))
        state[selector] = target
        # Force the existing post-write owner fence to stop before intermediate
        # publication; the assertion below is about reaching this native boundary.
        with db_session() as connection:
            revision = read_selection_revision(connection)
            advance_selection_revision(connection, expected_revision=revision)
        return MihomoApplyResult(ok=True, message="synthetic Mihomo transport", active_server_id=target)

    monkeypatch.setattr(DEFAULT_MIHOMO_ADAPTER, "health", health)
    monkeypatch.setattr(DEFAULT_MIHOMO_ADAPTER, "get_active_server_id", lambda: state["vpn-global"])
    monkeypatch.setattr(DEFAULT_MIHOMO_ADAPTER, "apply_server_to_selector", apply_selector)

    strict_proof = mihomo_config_status.mihomo_runtime_fixed_target_materialized(
        routing, require_selected=True,
    )
    assert strict_proof["native_membership_ok"] is True, strict_proof
    assert strict_proof["selected_fixed_target"] is False, strict_proof
    assert strict_proof["ok"] is False, strict_proof

    def generation_unchanged(routing, *, token, job_id):
        del routing
        revision = token["selection_revision"]
        fence = {
            "schema": "mihomo-reentry-generation-fence/v1",
            "operation_id": "real-proof-no-change-operation",
            "job_id": job_id,
            "before_revision": revision,
            "selection_revision": revision,
            "runtime_incarnation": token["runtime_incarnation"],
            "active_config_hash": token["active_config_hash"],
            "native_proof_verified": True,
            "publication_verified": True,
            "unchanged_config": True,
        }
        return {
            "ok": True,
            "operation_id": fence["operation_id"],
            "job_id": job_id,
            "reentry_generation_fence": fence,
        }

    monkeypatch.setattr(server_global_selection, "_materialize_fixed_server_generation", generation_unchanged)
    monkeypatch.setattr(
        "fwrouter_api.services.server_ping.check_server_delay",
        lambda *_args, **_kwargs: {"ok": True},
    )

    result = apply_global_fixed_server(server_id, requested_by="pytest")

    # The controlled revision race is after the fake native PUT. This keeps the
    # test focused on admission while preserving the production owner fence.
    assert native_calls == [("vpn-global", runtime_name)]
    assert state["vpn-global"] == runtime_name
    assert result["error_code"] == "GLOBAL_FIXED_SERVER_STALE"
    assert result["fixed_selection_stage"] == "owner_fence_after_selection"
    routing_after = get_routing_global_state(expire_ttl=False)
    assert routing_after["desired_fixed_server_id"] == server_id
    assert routing_after["applied_fixed_server_id"] is None
    assert routing_after["apply_state"] == "pending"


@pytest.mark.parametrize(
    "race", [
        None, "receipt_before", "receipt_after", "auto_before_put", "auto_after_put",
        "core_before", "core_after", "core_unknown",
        "core_receipt_before", "core_receipt_after",
    ],
)
def test_xray_verified_resume_restores_only_exact_owned_reject_selection(
    monkeypatch, race: str | None,
) -> None:
    """The fixed owner may reselect only after its Xray receipt is durable."""
    from fwrouter_api.services import server_global_selection
    from fwrouter_api.services.xray_vpn_auto_pending import _load_row

    server_id = f"fixed-xray-reentry-{race or 'clean'}"
    runtime_name = _seed_server(server_id, vpn_auto=False, global_list=False)
    fixed = set_global_fixed_server(server_id, requested_by="pytest")
    assert fixed["ok"] is True
    revision = fixed["pending_revision"]
    selection_revision = fixed["selection_revision"]
    xray_fields = {
        "phase": "xray_verified",
        "selection_revision": selection_revision,
        "server_id": server_id,
        "fixed_until": fixed["routing"]["fixed_server_until"],
        "core_apply_id": "owned-core-apply",
        "core_manifest_hash": "f" * 64,
        "core_enforcement_level": "global_selective_enforced",
        "core_live_mode": "selective",
        "xray_generation_operation_id": "owned-xray-generation-op",
        "xray_generation_mihomo_input_sha256": "a" * 64,
        "xray_generation_mihomo_config_sha256": "b" * 64,
        "xray_generation_mihomo_runtime_incarnation": "mihomo-after-xray",
        "xray_state": "verified",
        "xray_runtime_incarnation": "xray-incarnation",
        "xray_config_sha256": "c" * 64,
        "xray_artifact_sha256": "e" * 64,
    }
    assert server_global_selection._update_fixed_workflow(revision, **xray_fields) is not None
    token = {
        "selection_revision": selection_revision,
        "pending_revision": revision,
        "input_hash": xray_fields["xray_generation_mihomo_input_sha256"],
        "active_config_hash": xray_fields["xray_generation_mihomo_config_sha256"],
        "runtime_incarnation": xray_fields["xray_generation_mihomo_runtime_incarnation"],
    }
    workflow = server_global_selection._fixed_workflow_current(revision)
    assert workflow is not None

    effects: list[tuple[str, str]] = []
    selector_state = {"current": "REJECT"}
    proof_calls = 0
    core_proof_calls = 0
    real_workflow_current = server_global_selection._fixed_workflow_current

    def owned_workflow_matches(expected_revision: int) -> dict[str, Any] | None:
        current = real_workflow_current(expected_revision)
        if race == "receipt_before" and current is not None and not effects:
            server_global_selection._update_fixed_workflow(
                expected_revision, xray_generation_operation_id="foreign-op",
            )
            return real_workflow_current(expected_revision)
        if race == "core_receipt_before" and current is not None and not effects:
            server_global_selection._update_fixed_workflow(
                expected_revision, core_apply_id="foreign-core-apply",
            )
            return real_workflow_current(expected_revision)
        if race == "receipt_after" and effects:
            server_global_selection._update_fixed_workflow(
                expected_revision, xray_generation_operation_id="foreign-op",
            )
            return real_workflow_current(expected_revision)
        if race == "core_receipt_after" and effects:
            server_global_selection._update_fixed_workflow(
                expected_revision, core_apply_id="foreign-core-apply",
            )
            return real_workflow_current(expected_revision)
        return current

    monkeypatch.setattr(server_global_selection, "_fixed_workflow_current", owned_workflow_matches)
    monkeypatch.setattr(server_global_selection, "_fixed_apply_token_matches", lambda **_kwargs: True)
    monkeypatch.setattr(server_global_selection, "_fixed_xray_readback", lambda: {
        "state": "verified",
        "runtime_incarnation": xray_fields["xray_runtime_incarnation"],
        "active_config_sha256": xray_fields["xray_config_sha256"],
        "artifact_sha256": xray_fields["xray_artifact_sha256"],
    })

    def core_readback() -> dict[str, Any]:
        nonlocal core_proof_calls
        core_proof_calls += 1
        valid = race not in {"core_before", "core_unknown"} or core_proof_calls > 1
        if race == "core_after" and core_proof_calls >= 3:
            valid = False
        return {
            "ok": False if race == "core_unknown" and core_proof_calls == 1 else valid,
            "apply_id": xray_fields["core_apply_id"] if valid else "foreign-core-apply",
            "manifest_hash": xray_fields["core_manifest_hash"] if valid else "0" * 64,
            "enforcement_level": xray_fields["core_enforcement_level"],
            "live_global_mode": xray_fields["core_live_mode"],
        }

    monkeypatch.setattr(server_global_selection, "_fixed_core_readback", core_readback)

    def materialized(_routing, *, require_selected=False):
        nonlocal proof_calls
        proof_calls += 1
        is_selected = selector_state["current"] == runtime_name
        return {
            "ok": is_selected if require_selected else selector_state["current"] in {"REJECT", runtime_name},
            "fixed_runtime_target": runtime_name,
            "config_membership_ok": True,
            "native_membership_ok": True,
            "logical_group_native_membership_ok": True,
            "vpn_global_now": selector_state["current"],
            "selected_fixed_target": is_selected,
        }

    monkeypatch.setattr(
        "fwrouter_api.services.mihomo_config_status.mihomo_runtime_fixed_target_materialized",
        materialized,
    )

    def apply_selector(selector: str, target: str) -> MihomoApplyResult:
        effects.append((selector, target))
        selector_state["current"] = target
        if race == "auto_after_put":
            assert clear_global_fixed_server(requested_by="pytest-auto-race")["ok"] is True
        return MihomoApplyResult(ok=True, message="synthetic Mihomo selector transport", active_server_id=target)

    monkeypatch.setattr(DEFAULT_MIHOMO_ADAPTER, "apply_server_to_selector", apply_selector)
    if race == "receipt_before":
        # Corrupt the exact receipt before the helper performs its first owner read.
        server_global_selection._update_fixed_workflow(
            revision, xray_generation_operation_id="foreign-op",
        )
    elif race == "core_receipt_before":
        server_global_selection._update_fixed_workflow(
            revision, core_apply_id="foreign-core-apply",
        )
    elif race == "auto_before_put":
        assert clear_global_fixed_server(requested_by="pytest-auto-race")["ok"] is True

    result = server_global_selection._restore_fixed_selection_after_xray_generation(
        server_id=server_id,
        mihomo_target=runtime_name,
        fixed_until=str(fixed["routing"]["fixed_server_until"]),
        token=token,
        workflow=workflow,
    )
    with db_session() as connection:
        pending = _load_row(connection)

    if race:
        assert result["ok"] is False
        assert effects == [] if race in {
            "receipt_before", "core_receipt_before", "auto_before_put", "core_before", "core_unknown",
        } else effects == [("vpn-global", runtime_name)]
        assert pending["pending"] is True
        if race.startswith("receipt_"):
            assert pending["workflow"]["phase"] == "xray_verified"
            assert pending["workflow"]["xray_generation_operation_id"] == "foreign-op"
        elif race.startswith("core_receipt_"):
            assert pending["workflow"]["phase"] == "xray_verified"
            assert pending["workflow"]["core_apply_id"] == "foreign-core-apply"
        elif race.startswith("core_"):
            assert pending["workflow"]["phase"] == "xray_verified"
            assert pending["workflow"]["core_apply_id"] == "owned-core-apply"
        else:
            assert pending.get("workflow") is None
            assert get_routing_global_state(expire_ttl=False)["server_mode"] == "auto"
    else:
        assert result["ok"] is True, result
        assert result["proof"]["selected_fixed_target"] is True
        assert effects == [("vpn-global", runtime_name)]
        assert pending["pending"] is True
        assert pending["workflow"]["phase"] == "xray_verified"
        assert pending["workflow"]["xray_generation_operation_id"] == "owned-xray-generation-op"
    assert proof_calls == (
        0 if race in {
            "receipt_before", "core_receipt_before", "auto_before_put", "core_before", "core_unknown",
        } else 2
    )


def test_core_verified_reentry_does_not_restore_unproven_xray_generation(
    monkeypatch, tmp_path: Path,
) -> None:
    """A crash before the Xray receipt phase leaves REJECT pending and untouched."""
    from fwrouter_api.services import (
        core_intent, mihomo_config_paths, mihomo_reconcile, mihomo_runtime,
        server_global_selection,
    )
    from fwrouter_api.services.xray_vpn_auto_pending import _load_row

    server_id = "fixed-core-verified-unproven-xray"
    runtime_name = _seed_server(server_id, vpn_auto=False, global_list=False)
    fixed = set_global_fixed_server(server_id, requested_by="pytest")
    revision = fixed["pending_revision"]
    active_config = tmp_path / "active-mihomo.yaml"
    active_config.write_text("fixed: generated\n", encoding="utf-8")
    monkeypatch.setattr(mihomo_config_paths, "_resolved_base_config_path", lambda: active_config)
    monkeypatch.setattr(mihomo_runtime, "get_mihomo_runtime_incarnation", lambda **_kwargs: "resume-runtime")
    monkeypatch.setattr(core_intent, "read_core_runtime_context", lambda: {
        "boot_id": "00000000-0000-0000-0000-000000000001",
        "network_namespace_inode": "1",
    })
    monkeypatch.setattr(
        mihomo_reconcile, "current_mihomo_input_fingerprint",
        lambda _routing: {"hash": "a" * 64},
    )
    core_snapshot = ("enabled", "managed", "clean")
    workflow = server_global_selection._update_fixed_workflow(
        revision,
        phase="core_verified",
        selection_revision=fixed["selection_revision"],
        server_id=server_id,
        fixed_until=fixed["routing"]["fixed_server_until"],
        core_snapshot=list(core_snapshot),
        mihomo_generation_mode="unchanged",
    )
    assert workflow is not None
    token = server_global_selection._capture_fixed_apply_context(fixed["routing"])
    assert token is not None
    assert token["fixed_workflow_phase"] == "core_verified"
    status_calls: list[bool] = []
    native_calls: list[tuple[str, str]] = []

    def reject_status(_routing, *, require_selected=False):
        status_calls.append(require_selected)
        return {
            "ok": not require_selected,
            "fixed_runtime_target": runtime_name,
            "config_membership_ok": True,
            "native_membership_ok": True,
            "logical_group_native_membership_ok": True,
            "vpn_global_now": "REJECT",
            "selected_fixed_target": False,
        }

    monkeypatch.setattr(
        "fwrouter_api.services.mihomo_config_status.mihomo_runtime_fixed_target_materialized",
        reject_status,
    )
    monkeypatch.setattr(
        DEFAULT_MIHOMO_ADAPTER,
        "get_active_server_id",
        lambda: "REJECT",
    )

    def unexpected_native_apply(selector: str, target: str) -> MihomoApplyResult:
        native_calls.append((selector, target))
        return MihomoApplyResult(ok=True, message="unexpected", active_server_id=target)

    monkeypatch.setattr(DEFAULT_MIHOMO_ADAPTER, "apply_server_to_selector", unexpected_native_apply)
    monkeypatch.setattr(
        server_global_selection,
        "_materialize_fixed_server_generation",
        lambda _routing, *, token, job_id: {
            "ok": True,
            "operation_id": "unchanged-operation",
            "job_id": job_id,
            "reentry_generation_fence": {
                "schema": "mihomo-reentry-generation-fence/v1",
                "operation_id": "unchanged-operation",
                "job_id": job_id,
                "before_revision": token["selection_revision"],
                "selection_revision": token["selection_revision"],
                "runtime_incarnation": token["runtime_incarnation"],
                "active_config_hash": token["active_config_hash"],
                "native_proof_verified": True,
                "publication_verified": True,
                "unchanged_config": True,
            },
        },
    )
    monkeypatch.setattr(
        "fwrouter_api.services.server_ping.check_server_delay",
        lambda *_args, **_kwargs: {"ok": True},
    )
    monkeypatch.setattr(
        server_global_selection, "_restore_fixed_selection_after_xray_generation",
        lambda **_kwargs: pytest.fail("core_verified without an Xray receipt cannot restore REJECT"),
    )

    result = apply_global_fixed_server(
        server_id, requested_by="pytest", _resume_workflow=workflow,
    )
    with db_session() as connection:
        pending = _load_row(connection)
        routing = dict(connection.execute(
            "SELECT applied_fixed_server_id, apply_state FROM routing_global_state WHERE id=1",
        ).fetchone())
    assert result["ok"] is False
    assert result["error_code"] == "GLOBAL_FIXED_SERVER_STALE"
    assert status_calls == [False, True]
    assert native_calls == []
    assert pending["pending"] is True
    assert pending["workflow"]["phase"] == "core_verified"
    assert "xray_generation_operation_id" not in pending["workflow"]
    assert routing == {"applied_fixed_server_id": None, "apply_state": "pending"}


def test_fixed_xray_stage_candidates_keep_fixed_selector_membership(monkeypatch, tmp_path: Path) -> None:
    """Connected staging must render fixed membership before native validation."""
    import yaml

    from fwrouter_api.services import mihomo_config_inbounds, mihomo_config_paths
    from fwrouter_api.services import subscription_pipeline
    from fwrouter_api.services import xray_subscription_service
    from fwrouter_api.services.xray_subscription_service import _stage_profile_native_candidates

    server_id = "fixed-xray-stage-routing"
    runtime_name = _seed_server(server_id, vpn_auto=False, global_list=False)
    fixed = set_global_fixed_server(server_id, requested_by="pytest")
    assert fixed["ok"] is True
    base_config = tmp_path / "mihomo-base.yaml"
    base_config.write_text(yaml.safe_dump({
        "allow-lan": True,
        "bind-address": "0.0.0.0",
        "proxies": [],
        "proxy-groups": [],
        "rules": ["MATCH,DIRECT"],
    }), encoding="utf-8")
    monkeypatch.setattr(mihomo_config_paths, "_resolved_base_config_path", lambda: base_config)
    monkeypatch.setattr(mihomo_config_inbounds, "_resolved_base_config_path", lambda: base_config)
    monkeypatch.setattr(
        mihomo_config_inbounds, "_candidate_base_config_paths", lambda: [base_config],
    )
    monkeypatch.setattr(
        mihomo_config_service, "validate_mihomo_candidate_config",
        lambda **_kwargs: {"ok": True, "structure_checked": True},
    )
    monkeypatch.setattr(subscription_pipeline, "resolve_mihomo_validator_image_id", lambda: "test-image")
    monkeypatch.setattr(
        subscription_pipeline, "validate_mihomo_candidate_config",
        lambda _path, *, image_reference=None: {"ok": True, "returncode": 0},
    )

    class Adapter:
        config_path = tmp_path / "xray" / "config.json"

        def stage_subscription_generation(self, *, candidate_path: Path, **_kwargs):
            candidate_path.parent.mkdir(parents=True, exist_ok=True)
            candidate_path.write_text("{}\\n", encoding="utf-8")
            return SimpleNamespace(ok=True, details={"candidate_sha256": "x" * 64})

    result = _stage_profile_native_candidates(
        adapter=Adapter(), desired_clients=[], managed_email_prefixes=[], bindings=[],
        client_modes=[], assignments=[], routing_snapshot=fixed["routing"],
    )
    assert result["ok"] is True, result
    stage_groups = {}
    for name in ("transition", "final"):
        candidate = yaml.safe_load(Path(result["mihomo_candidates"][name]).read_text(encoding="utf-8"))
        group = next(item for item in candidate["proxy-groups"] if item.get("name") == "vpn-global")
        stage_groups[name] = group["proxies"]
    assert stage_groups == {
        "transition": ["REJECT", runtime_name],
        "final": ["REJECT", runtime_name],
    }, stage_groups
    assert result["fixed_routing_snapshot"] == {
        key: fixed["routing"].get(key)
        for key in xray_subscription_service._FIXED_XRAY_ROUTING_FIELDS
    }

    # Exercise the actual private checkpoint writer with the actual stage
    # result. The durable operation record must contain only the allowlisted
    # fixed projection, while ordinary generations retain a null projection.
    from fwrouter_api.services.vpn_auto_selection_state import read_selection_fence

    class CheckpointAdapter:
        config_path = Adapter.config_path

    with db_session() as connection:
        selection_fence = read_selection_fence(connection)
    checkpoint_path = xray_subscription_service._write_xray_generation_checkpoint(
        adapter=CheckpointAdapter(), generation_id="fixed-stage-checkpoint",
        tokens=set(), phase="prepared", source_fingerprint="source-proof",
        staged_generation=result, managed_email_prefixes=[],
        fixed_workflow_revision=fixed["pending_revision"],
        fixed_origin_selection_revision=selection_fence["revision"],
    )
    checkpoint_data = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert checkpoint_data["fixed_routing_snapshot"] == result["fixed_routing_snapshot"]
    assert set(checkpoint_data["fixed_routing_snapshot"]) == set(
        xray_subscription_service._FIXED_XRAY_ROUTING_FIELDS
    )
    checkpoint_path.unlink()

    ordinary = _stage_profile_native_candidates(
        adapter=Adapter(), desired_clients=[], managed_email_prefixes=[], bindings=[],
        client_modes=[], assignments=[], routing_snapshot=None,
    )
    assert ordinary["ok"] is True
    assert ordinary["fixed_routing_snapshot"] is None
    ordinary_checkpoint = xray_subscription_service._write_xray_generation_checkpoint(
        adapter=CheckpointAdapter(), generation_id="ordinary-stage-checkpoint",
        tokens=set(), phase="prepared", source_fingerprint="source-proof",
        staged_generation=ordinary, managed_email_prefixes=[],
    )
    ordinary_checkpoint_data = json.loads(ordinary_checkpoint.read_text(encoding="utf-8"))
    assert ordinary_checkpoint_data["fixed_routing_snapshot"] is None
    ordinary_checkpoint.unlink()


def test_fixed_xray_staged_apply_promotes_restarts_restores_and_publishes_outer_receipt(
    monkeypatch, tmp_path: Path,
) -> None:
    """Exercise real fixed promotion/restart/selector restore/checkpoint receipt wiring."""
    import yaml

    from fwrouter_api.adapters.xray_common import xray_writer_guard
    from fwrouter_api.services import (
        apply_manifest, dataplane_global, dataplane_nft_artifacts, dataplane_status,
        dataplane_live, live_probe_cache, mihomo_config, mihomo_config_paths,
        mihomo_config_status, mihomo_runtime, runtime_adapters, xray_subscription_service,
    )
    from fwrouter_api.services.core_intent import read_core_intent_revision
    from fwrouter_api.services.server_global_selection import (
        _capture_fixed_apply_context, _fixed_xray_generation_callback, _update_fixed_workflow,
    )
    from fwrouter_api.services.xray_vpn_auto_pending import _load_row
    from fwrouter_api.services.vpn_auto_selection_state import read_selection_revision
    from fwrouter_api.services.xray_subscription_service import (
        _fixed_xray_routing_projection, _record_xray_generation_selection_revision,
    )
    from fwrouter_api.services import mihomo_reconcile_fingerprint

    server_id = "fixed-xray-effect-path"
    runtime_name = _seed_server(server_id, vpn_auto=False, global_list=False)
    fixed = set_global_fixed_server(server_id, requested_by="pytest")
    assert fixed["ok"] is True
    with db_session() as connection:
        connection.execute(
            "UPDATE routing_global_state SET applied_fixed_server_id=?, applied_mode=desired_mode, "
            "apply_state='clean' WHERE id=1",
            (server_id,),
        )
        connection.execute(
            "INSERT INTO modules(module_name, desired_state, lifecycle_mode, runtime_state, apply_state) "
            "VALUES ('vpn', 'enabled', 'managed', 'running', 'clean') "
            "ON CONFLICT(module_name) DO UPDATE SET desired_state='enabled', lifecycle_mode='managed', "
            "runtime_state='running', apply_state='clean'",
        )
        route = dict(connection.execute("SELECT * FROM routing_global_state WHERE id=1").fetchone())
        selection_revision = read_selection_revision(connection)
        core_revision = read_core_intent_revision(connection)

    # Seed a real applied manifest and matching apply_versions owner row. Only
    # the low-level live dataplane probes below stand in for native transport.
    from types import SimpleNamespace
    paths = SimpleNamespace(
        generated_dir=tmp_path / "generated", state_dir=tmp_path / "state", jobs_dir=tmp_path / "jobs",
    )
    settings = SimpleNamespace(paths=paths)
    monkeypatch.setattr(dataplane_nft_artifacts, "get_settings", lambda: settings)
    monkeypatch.setattr(dataplane_global, "get_settings", lambda: settings)
    manifest = apply_manifest._materialize_manifest(
        prebuilt_manifest={"routing_global_state": route, "extra": {"rules_effective": {}}},
        plan_id="fixed-xray-core-apply", reason="test_fixture", input_data={"scope": "test"},
    )
    artifact_paths = dataplane_nft_artifacts.write_candidate_artifacts(
        job_id="fixed-xray-core-job", apply_id=manifest["plan_id"], manifest=manifest,
        renderer=lambda _manifest: "table inet fwrouter_v2 {\n}\n",
    )
    dataplane_nft_artifacts.promote_last_good(manifest=manifest, artifact_paths=artifact_paths)
    with db_session() as connection:
        connection.execute(
            "INSERT INTO apply_versions(apply_id, manifest_path, status, created_at, core_intent_revision) "
            "VALUES (?, ?, 'applied', '2099', ?)",
            (manifest["plan_id"], artifact_paths["applied_manifest_path"], core_revision),
        )

    monkeypatch.setattr(dataplane_status, "_read_live_dataplane_payload", lambda: {
        "table_exists": True,
        "required_chains": {"fwrouter_classify": True},
        "artifact_consistency": {"ok": True},
    })
    monkeypatch.setattr(dataplane_live, "_probe_live_global_mode_uncached", lambda: {
        "ok": True, "mode": str(route["desired_mode"]), "selective_default": route["selective_default"],
    })
    monkeypatch.setattr(dataplane_live, "probe_live_global_mode", lambda: {
        "ok": True, "mode": str(route["desired_mode"]), "selective_default": route["selective_default"],
    })
    monkeypatch.setattr(dataplane_global, "build_global_preflight", lambda **_kwargs: {
        "can_enforce_global_direct": True, "can_enforce_global_selective": True,
        "can_enforce_global_vpn": True, "vpn_egress_ready": True,
        "selective_degraded": False, "selective_degraded_missing": [],
        "missing_by_mode": {"direct": [], "selective": [], "vpn": []},
        "missing": [], "profile": "synthetic",
    })
    monkeypatch.setattr(dataplane_status, "build_global_preflight", lambda **_kwargs: {
        "can_enforce_global_direct": True, "can_enforce_global_selective": True,
        "can_enforce_global_vpn": True, "vpn_egress_ready": True,
        "selective_degraded": False, "selective_degraded_missing": [],
        "missing_by_mode": {"direct": [], "selective": [], "vpn": []},
        "missing": [], "profile": "synthetic",
    })
    monkeypatch.setattr(live_probe_cache, "get_live_probe_cache", lambda _key, *, loader, **_kwargs: loader())

    fixed_context = {"boot_id": "synthetic-fixed-boot", "network_namespace_inode": "17"}
    monkeypatch.setattr(
        "fwrouter_api.services.core_intent.read_core_runtime_context", lambda: dict(fixed_context),
    )
    runtime_state = {"vpn-global": "REJECT"}
    groups_from_active = lambda: {
        str(group.get("name")): list(group.get("proxies") or [])
        for group in (yaml.safe_load(active_path.read_text()).get("proxy-groups") or [])
        if isinstance(group, dict)
    }

    active_path = tmp_path / "mihomo.yaml"
    candidate_path = tmp_path / "config.next.yaml"
    base_config_path = tmp_path / "base.yaml"
    base_config_path.write_text(yaml.safe_dump({"proxies": [], "proxy-groups": [], "rules": ["MATCH,DIRECT"]}))
    active_path.write_text("initial: true\n", encoding="utf-8")
    monkeypatch.setattr(mihomo_config_paths, "_resolved_base_config_path", lambda: str(active_path))
    monkeypatch.setattr(mihomo_config_paths, "_resolved_candidate_config_path", lambda: str(candidate_path))
    monkeypatch.setattr(mihomo_config, "_resolved_base_config_path", lambda: str(active_path))
    monkeypatch.setattr(mihomo_config, "_resolved_candidate_config_path", lambda: str(candidate_path))
    monkeypatch.setattr(mihomo_config_status, "_resolved_base_config_path", lambda: str(active_path))
    monkeypatch.setattr(mihomo_reconcile_fingerprint, "_resolved_base_config_path", lambda: str(active_path))
    monkeypatch.setattr(mihomo_reconcile_fingerprint, "_resolved_candidate_config_path", lambda: str(candidate_path))
    monkeypatch.setattr(mihomo_reconcile_fingerprint, "_resolved_applied_manifest_path", lambda: paths.generated_dir / "dataplane" / "applied-manifest.json")
    monkeypatch.setattr(mihomo_reconcile_fingerprint, "_resolved_contours_path", lambda: paths.generated_dir / "contours.json")

    class NativeAdapter:
        adapter_id = "mihomo"

        def health(self):
            groups = groups_from_active()
            return SimpleNamespace(
                runtime_state="running", active_server_id=runtime_state["vpn-global"],
                details={"selectors": {
                    "vpn_global_exists": True,
                    "vpn_global_has_vpn_auto": "vpn-auto" in groups.get("vpn-global", []),
                    "vpn_global_targets": groups.get("vpn-global", []),
                    "vpn_auto_targets": groups.get("vpn-auto", []),
                    "vpn_global_now": runtime_state["vpn-global"],
                    "vpn_global_selected_target": runtime_state["vpn-global"],
                }},
            )

        def apply_server_to_selector(self, selector_name: str, server_id: str):
            runtime_state[selector_name] = server_id
            return SimpleNamespace(ok=True, active_server_id=server_id,
                                   to_dict=lambda: {"ok": True, "active_server_id": server_id})

        def list_servers(self):
            return [SimpleNamespace(server_id=server_id)]

    native_adapter = NativeAdapter()
    monkeypatch.setattr(runtime_adapters, "DEFAULT_MIHOMO_ADAPTER", native_adapter)
    monkeypatch.setattr(mihomo_config_status, "DEFAULT_MIHOMO_ADAPTER", native_adapter)
    # Load an isolated copy of the real wrapper because the autouse test guard
    # replaces the shared module. Fake only its native transport boundaries.
    import importlib.util
    runtime_spec = importlib.util.spec_from_file_location(
        "_fwrouter_test_mihomo_runtime", mihomo_runtime.__file__,
    )
    assert runtime_spec is not None and runtime_spec.loader is not None
    actual_mihomo_runtime = importlib.util.module_from_spec(runtime_spec)
    runtime_spec.loader.exec_module(actual_mihomo_runtime)
    monkeypatch.setattr(mihomo_runtime, "restart_mihomo_container", actual_mihomo_runtime.restart_mihomo_container)
    for runtime_module in (mihomo_runtime, actual_mihomo_runtime):
        monkeypatch.setattr(runtime_module, "_run_compose_command", lambda *_a, **_k: {"ok": True, "returncode": 0})
        monkeypatch.setattr(runtime_module, "get_mihomo_container_status", lambda: {"ok": True})
        monkeypatch.setattr(runtime_module, "wait_for_mihomo_controller", lambda **_kwargs: {"ok": True})
        monkeypatch.setattr(runtime_module, "get_mihomo_runtime_incarnation", lambda **_kwargs: "fixed-runtime-proof")

    core_proof = xray_subscription_service._collect_legacy_admission_core_native_proof()
    assert core_proof["ok"] is True, core_proof
    workflow = _update_fixed_workflow(
        fixed["pending_revision"], phase="core_verified",
        mihomo_generation_mode="unchanged",
        core_apply_id=core_proof["apply_id"], core_manifest_hash=core_proof["manifest_hash"],
        core_enforcement_level=core_proof["enforcement_level"], core_live_mode=core_proof["live_global_mode"],
    )
    assert workflow is not None
    route = get_routing_global_state(expire_ttl=False)
    snapshot = _fixed_xray_routing_projection(route)
    token = _capture_fixed_apply_context(route)
    assert token is not None

    def candidate_bytes(marker: str) -> bytes:
        config = mihomo_config.build_mihomo_config(route, xray_handoff_assignments=[])
        return (yaml.safe_dump(config, sort_keys=False) + f"\n# {marker}\n").encode()

    with db_session() as connection:
        _load_row(connection)
    checkpoint = tmp_path / "generation-checkpoint.json"
    checkpoint.write_text(json.dumps({
        "selection_revision": selection_revision,
        "fixed_workflow_revision": fixed["pending_revision"],
        "fixed_origin_selection_revision": selection_revision,
        "fixed_routing_snapshot": snapshot,
        "selection_operation_id": "fixed-xray-operation",
    }), encoding="utf-8")
    applied = []
    for label in ("transition", "final"):
        staged = tmp_path / f"{label}.yaml"
        payload = candidate_bytes(label)
        staged.write_bytes(payload)
        fingerprint = mihomo_reconcile_fingerprint.current_mihomo_input_fingerprint(snapshot)
        with xray_writer_guard(timeout_seconds=5.0):
            result = xray_subscription_service._apply_staged_mihomo_candidate(
                str(staged), hashlib.sha256(payload).hexdigest(),
                candidate_input_fingerprint={"hash": fingerprint["hash"]},
                fixed_routing_snapshot=snapshot,
                fixed_workflow_revision=fixed["pending_revision"],
                fixed_origin_selection_revision=selection_revision,
                expected_selection_revision=(selection_revision if not applied else applied[-1]),
            )
        assert result["ok"] is True, result
        assert type(result.get("selection_revision")) is int
        _record_xray_generation_selection_revision(checkpoint, result["selection_revision"])
        applied.append(result["selection_revision"])
        with db_session() as connection:
            assert read_selection_revision(connection) == result["selection_revision"]
        assert runtime_state["vpn-global"] == runtime_name

    callback = _fixed_xray_generation_callback(
        server_id=server_id, mihomo_target=runtime_name,
        fixed_until=str(route["fixed_server_until"]), token=token,
    )
    receipt = callback(operation_id="fixed-xray-operation", expected_selection_revision=applied[-1])
    assert receipt["ok"] is True, receipt
    assert receipt["selection_revision"] == applied[-1]
    saved_checkpoint = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert saved_checkpoint["selection_revision"] == applied[-1]
    with db_session() as connection:
        pending = _load_row(connection)
    assert pending["workflow"]["xray_generation_operation_id"] == "fixed-xray-operation"
    assert pending["workflow"]["selection_revision"] == applied[-1]


@pytest.mark.parametrize("stale_case", ["expired_ttl", "foreign_revision", "new_auto_selection"])
def test_fixed_staged_effect_refuses_expiry_or_later_selection_without_writes(
    monkeypatch, tmp_path: Path, stale_case: str,
) -> None:
    from fwrouter_api.services import xray_subscription_service
    from fwrouter_api.services.vpn_auto_selection_state import advance_selection_revision
    from fwrouter_api.services.xray_subscription_service import _fixed_xray_routing_projection

    server_id = f"fixed-stale-effect-{stale_case}"
    _seed_server(server_id, vpn_auto=False, global_list=False)
    fixed = set_global_fixed_server(server_id, requested_by="pytest")
    assert fixed["ok"] is True
    with db_session() as connection:
        connection.execute(
            "UPDATE routing_global_state SET applied_fixed_server_id=?, apply_state='clean' WHERE id=1",
            (server_id,),
        )
        initial_revision = read_selection_revision(connection)
        if stale_case == "expired_ttl":
            connection.execute(
                "UPDATE routing_global_state SET fixed_server_until='2000-01-01 00:00:00' WHERE id=1",
            )
        elif stale_case == "new_auto_selection":
            next_id = f"{server_id}-auto"
            connection.execute(
                "INSERT INTO servers(server_id, server_name, provider_name, inventory_state, raw_json) "
                "VALUES (?, ?, 'fixed-mode-source-test', 'active', ?)",
                (next_id, f"Runtime {next_id}", json.dumps({"name": f"Runtime {next_id}", "type": "socks5"})),
            )
            connection.execute(
                "INSERT INTO server_preferences(server_id, vpn_auto, vpn_auto_priority, global_list) "
                "VALUES (?, 1, 0, 1)", (next_id,),
            )
            connection.execute(
                "UPDATE routing_global_state SET active_auto_server_id=? WHERE id=1", (next_id,),
            )
            assert advance_selection_revision(connection, expected_revision=initial_revision) == initial_revision + 1
        elif stale_case == "foreign_revision":
            assert advance_selection_revision(connection, expected_revision=initial_revision) == initial_revision + 1
        route = dict(connection.execute("SELECT * FROM routing_global_state WHERE id=1").fetchone())
        pending_revision = fixed["pending_revision"]

    snapshot = _fixed_xray_routing_projection(route)
    candidate = tmp_path / "must-not-be-copied.yaml"
    result = xray_subscription_service._apply_staged_mihomo_candidate(
        str(candidate), "a" * 64,
        candidate_input_fingerprint={"hash": "b" * 64},
        fixed_routing_snapshot=snapshot,
        fixed_workflow_revision=pending_revision,
        fixed_origin_selection_revision=initial_revision,
        expected_selection_revision=initial_revision,
    )

    assert result["ok"] is False
    assert result["error_code"] == "FIXED_XRAY_ROUTING_SNAPSHOT_STALE"
    assert candidate.exists() is False
    with db_session() as connection:
        from fwrouter_api.services.xray_vpn_auto_pending import _load_row

        pending = _load_row(connection)
        assert pending["pending"] is True
        assert pending["revision"] == pending_revision
        assert pending["workflow"]["phase"] == "intent"


def test_fixed_checkpoint_restore_stays_pending_without_qualified_preimage(
    monkeypatch, tmp_path: Path,
) -> None:
    from fwrouter_api.services.xray_subscription_service import _restore_xray_generation_checkpoint

    checkpoint = tmp_path / "fixed-generation-checkpoint.json"
    original = json.dumps({
        "fixed_workflow_revision": 7,
        "fixed_origin_selection_revision": 4,
        "selection_revision": 5,
        "phase": "transition_applied",
    })
    checkpoint.write_text(original, encoding="utf-8")
    adapter = SimpleNamespace()

    result = _restore_xray_generation_checkpoint(adapter, checkpoint)

    assert result == {
        "ok": False,
        "status": "pending",
        "runtime_verified": False,
        "recovered": "fixed_generation_recovery_unproven",
        "reason": "fixed_generation_preimage_not_authorized",
    }
    assert checkpoint.read_text(encoding="utf-8") == original


@pytest.mark.parametrize(
    ("phase", "expected_phase"),
    [("core_verified", "core_verified"), ("xray_verified", "xray_verified"), ("unknown_phase", None)],
)
def test_fixed_context_preserves_only_known_unchanged_generation_phases(
    monkeypatch, tmp_path: Path, phase: str, expected_phase: str | None,
) -> None:
    from fwrouter_api.services import core_intent, mihomo_config_paths, mihomo_reconcile, mihomo_runtime
    from fwrouter_api.services.server_global_selection import (
        _capture_fixed_apply_context, _update_fixed_workflow,
    )

    server_id = f"fixed-phase-context-{phase}"
    _seed_server(server_id, vpn_auto=False, global_list=False)
    fixed = set_global_fixed_server(server_id, requested_by="pytest")
    active_config = tmp_path / "active-mihomo.yaml"
    active_config.write_text("fixed: unchanged\n", encoding="utf-8")
    monkeypatch.setattr(mihomo_config_paths, "_resolved_base_config_path", lambda: active_config)
    monkeypatch.setattr(mihomo_runtime, "get_mihomo_runtime_incarnation", lambda **_kwargs: "phase-runtime")
    monkeypatch.setattr(core_intent, "read_core_runtime_context", lambda: {
        "boot_id": "00000000-0000-0000-0000-000000000001",
        "network_namespace_inode": "1",
    })
    monkeypatch.setattr(
        mihomo_reconcile, "current_mihomo_input_fingerprint",
        lambda _routing: {"hash": "a" * 64},
    )
    assert _update_fixed_workflow(
        fixed["pending_revision"],
        phase=phase,
        selection_revision=fixed["selection_revision"],
        server_id=server_id,
        fixed_until=fixed["routing"]["fixed_server_until"],
        mihomo_generation_mode="unchanged",
    ) is not None

    token = _capture_fixed_apply_context(fixed["routing"])
    if expected_phase is None:
        assert token is None
    else:
        assert token is not None
        assert token["fixed_workflow_phase"] == expected_phase


@pytest.mark.parametrize("receipt_case", ["missing", "wrong_operation"])
def test_fixed_apply_rejects_missing_or_mismatched_generation_receipt(
    monkeypatch, receipt_case: str,
) -> None:
    server_id = f"fixed-bad-receipt-{receipt_case}"
    _seed_server(server_id, vpn_auto=False, global_list=False)
    from fwrouter_api.adapters import mihomo as mihomo_adapter_module
    from fwrouter_api.services import core_intent, mihomo_runtime, server_global_selection

    monkeypatch.setattr(core_intent, "read_core_runtime_context", lambda: {
        "boot_id": "00000000-0000-0000-0000-000000000001",
        "network_namespace_inode": "1",
    })
    monkeypatch.setattr(mihomo_runtime, "get_mihomo_runtime_incarnation", lambda **_kwargs: "bad-receipt-runtime")
    native_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        mihomo_adapter_module,
        "DEFAULT_MIHOMO_ADAPTER",
        SimpleNamespace(
            get_active_server_id=lambda: "REJECT",
            apply_server_to_selector=lambda selector, target: native_calls.append((selector, target)),
        ),
    )

    def invalid_receipt(routing, *, token, job_id):
        del routing, token
        operation_id = "actual-operation"
        return {
            "ok": True,
            "operation_id": operation_id,
            "job_id": job_id,
            "reentry_generation_fence": None if receipt_case == "missing" else {
                "schema": "mihomo-reentry-generation-fence/v1",
                "operation_id": "different-operation",
                "job_id": job_id,
                "before_revision": 1,
                "selection_revision": 1,
                "runtime_incarnation": "bad-receipt-runtime",
                "active_config_hash": "0" * 64,
                "native_proof_verified": True,
                "publication_verified": True,
                "unchanged_config": True,
            },
        }

    monkeypatch.setattr(server_global_selection, "_materialize_fixed_server_generation", invalid_receipt)
    result = apply_global_fixed_server(server_id, requested_by="pytest")

    assert result["ok"] is False
    assert result["error_code"] == "MIHOMO_RECONCILE_FAILED"
    assert result["mihomo_reconcile"]["error_code"] == "MIHOMO_GENERATION_RECEIPT_INVALID"
    assert native_calls == []
    assert get_routing_global_state(expire_ttl=False)["apply_state"] == "pending"


def test_fixed_generation_reservation_receipt_and_resume_use_existing_sqlite_owner(
    monkeypatch, tmp_path: Path,
) -> None:
    """Exercise the real R -> R+1 handoff and durable receipt in isolated SQLite."""
    from fwrouter_api.adapters.xray_common import xray_writer_guard
    from fwrouter_api.services import core_intent, mihomo_config_paths, mihomo_runtime, server_global_selection
    from fwrouter_api.services.mihomo_reconcile import _write_fixed_generation_checkpoint
    from fwrouter_api.services.xray_vpn_auto_pending import (
        _load_row, get_xray_vpn_auto_pending_state, pending_fixed_workflow_state,
    )

    server_id = "fixed-reservation-rebind"
    _seed_server(server_id, vpn_auto=False, global_list=False)
    base_config = tmp_path / "mihomo.yaml"
    base_config.write_text("before: fixed\n", encoding="utf-8")
    runtime_incarnation = {"value": "reservation-runtime"}
    monkeypatch.setattr(core_intent, "read_core_runtime_context", lambda: {
        "boot_id": "00000000-0000-0000-0000-000000000001",
        "network_namespace_inode": "1",
    })
    monkeypatch.setattr(mihomo_runtime, "get_mihomo_runtime_incarnation", lambda **_kwargs: runtime_incarnation["value"])
    monkeypatch.setattr(mihomo_config_paths, "_resolved_base_config_path", lambda: str(base_config))
    monkeypatch.setattr(mihomo_config_service, "_resolved_last_good_mihomo_dir", lambda: tmp_path / "last-good")
    monkeypatch.setattr(
        "fwrouter_api.services.mihomo_config_status.mihomo_runtime_fixed_target_materialized",
        lambda _routing, **_kwargs: {
            "ok": True,
            "fixed_runtime_target": "Runtime fixed-reservation-rebind",
        },
    )
    monkeypatch.setattr(
        "fwrouter_api.services.mihomo_reconcile.current_mihomo_input_fingerprint",
        lambda _routing: {"hash": "a" * 64},
    )

    desired = set_global_fixed_server(server_id, requested_by="pytest")
    token = server_global_selection._capture_fixed_apply_context(desired["routing"])
    assert desired["ok"] is True and token is not None
    source_revision = int(token["selection_revision"])
    operation_id = "8ab7f539-32af-4cf9-acf3-b75d48b43b7d"
    candidate_bytes = b"after: fixed\n"
    candidate_hash = hashlib.sha256(candidate_bytes).hexdigest()
    checkpoint = _write_fixed_generation_checkpoint(operation_id, base_config.read_bytes())
    assert checkpoint is not None
    reservation = {
        "schema": "mihomo-fixed-generation-reservation/v1",
        "operation_id": operation_id,
        "job_id": "fixed-reconcile-test",
        "before_revision": source_revision,
        "selection_revision": source_revision + 1,
        "input_fingerprint_hash": "a" * 64,
        "candidate_config_hash": candidate_hash,
        "before_active_config_hash": hashlib.sha256(base_config.read_bytes()).hexdigest(),
        "before_runtime_incarnation": "reservation-runtime",
        "checkpoint_ref": checkpoint,
    }
    reserve, publish = server_global_selection._fixed_generation_callbacks(desired["routing"], token)
    with xray_writer_guard(timeout_seconds=5):
        reserved = reserve(
            mode="reserve", reservation=reservation,
            expected_selection_revision=source_revision,
        )
        assert reserved == {
            "ok": True, "before_revision": source_revision,
            "selection_revision": source_revision + 1,
        }
        with db_session() as connection:
            marker = _load_row(connection)
        assert marker["workflow"]["phase"] == "mihomo_reserved"
        assert marker["workflow"]["mihomo_generation_reservation"] == reservation

        resumed_reservation_token = server_global_selection._capture_fixed_apply_context(desired["routing"])
        assert resumed_reservation_token is not None
        assert resumed_reservation_token["source_selection_revision"] == source_revision
        resume_reservation, _resume_publish = server_global_selection._fixed_generation_callbacks(
            desired["routing"], resumed_reservation_token,
        )
        validation = resume_reservation(
            mode="validate",
            operation_id=operation_id,
            expected_selection_revision=source_revision + 1,
            reservation=reservation,
            candidate_config_hash=candidate_hash,
            input_fingerprint_hash="a" * 64,
            active_config_hash=reservation["before_active_config_hash"],
            runtime_incarnation="reservation-runtime",
        )
        assert validation["ok"] is True, validation

        materialization = server_global_selection._fixed_materialization_callback(desired["routing"], token)(
            operation_id=operation_id,
            expected_selection_revision=source_revision + 1,
        )
        assert materialization["ok"] is True
        base_config.write_bytes(candidate_bytes)
        runtime_incarnation["value"] = "reservation-runtime-after"
        receipt = {
            "schema": "mihomo-fixed-generation-receipt/v1",
            "operation_id": operation_id,
            "job_id": "fixed-reconcile-test",
            "before_revision": source_revision,
            "selection_revision": source_revision + 1,
            "input_fingerprint_hash": "a" * 64,
            "candidate_config_hash": candidate_hash,
            "active_config_hash": candidate_hash,
            "runtime_incarnation": runtime_incarnation["value"],
            "native_udp_policy_verified": True,
            "native_top_level_rules_match": True,
            "native_selector_membership_matches": True,
            "guarded_rules_match": True,
            "publication_verified": True,
            "unchanged_config": False,
        }
        assert publish(
            operation_id=operation_id,
            expected_selection_revision=source_revision + 1,
            receipt=receipt,
        ) == {"ok": True}

    resumed_token = server_global_selection._capture_fixed_apply_context(desired["routing"])
    assert resumed_token is not None
    assert resumed_token["selection_revision"] == source_revision + 1
    assert resumed_token["source_selection_revision"] == source_revision
    assert resumed_token["fixed_workflow_phase"] == "mihomo_generation_verified"
    assert resumed_token["mihomo_generation_receipt"] == receipt
    assert "mihomo_generation_reservation" not in get_xray_vpn_auto_pending_state()["workflow"]
    assert pending_fixed_workflow_state()["mihomo_generation_reservation"] == reservation
    receipt_resume_reservation, _ = server_global_selection._fixed_generation_callbacks(
        desired["routing"], resumed_token,
    )
    with xray_writer_guard(timeout_seconds=5):
        assert receipt_resume_reservation(
            mode="validate",
            operation_id=operation_id,
            expected_selection_revision=source_revision + 1,
            reservation=reservation,
            candidate_config_hash=candidate_hash,
            input_fingerprint_hash="a" * 64,
            active_config_hash=candidate_hash,
            runtime_incarnation="reservation-runtime-after",
        ) == {"ok": False}

    monkeypatch.setattr(
        "fwrouter_api.services.mihomo_reconcile_fingerprint.mihomo_udp_policy_receipt_matches",
        lambda: True,
    )
    resumed = server_global_selection._materialize_fixed_server_generation(
        desired["routing"], token=resumed_token, job_id="fixed-reconcile-resume",
    )
    assert resumed["ok"] is True
    assert resumed["operation_id"] == operation_id
    assert resumed["reentry_generation_fence"]["selection_revision"] == source_revision + 1


def test_fixed_generation_foreign_checkpoint_cannot_reserve_or_advance_revision(monkeypatch) -> None:
    from fwrouter_api.adapters.xray_common import xray_writer_guard
    from fwrouter_api.services import core_intent, mihomo_runtime, server_global_selection
    from fwrouter_api.services.vpn_auto_selection_state import read_selection_revision
    from fwrouter_api.services.xray_vpn_auto_pending import _load_row

    server_id = "fixed-reservation-rollback"
    _seed_server(server_id, vpn_auto=False, global_list=False)
    monkeypatch.setattr(core_intent, "read_core_runtime_context", lambda: {
        "boot_id": "00000000-0000-0000-0000-000000000001",
        "network_namespace_inode": "1",
    })
    monkeypatch.setattr(mihomo_runtime, "get_mihomo_runtime_incarnation", lambda **_kwargs: "reservation-runtime")
    desired = set_global_fixed_server(server_id, requested_by="pytest")
    token = server_global_selection._capture_fixed_apply_context(desired["routing"])
    assert token is not None
    source_revision = token["selection_revision"]
    reservation = {
        "schema": "mihomo-fixed-generation-reservation/v1",
        "operation_id": "8ab7f539-32af-4cf9-acf3-b75d48b43b7d",
        "before_revision": source_revision,
        "selection_revision": source_revision + 1,
        "candidate_config_hash": "b" * 64,
        "input_fingerprint_hash": token["input_hash"],
        "before_active_config_hash": token["active_config_hash"],
        "before_runtime_incarnation": token["runtime_incarnation"],
        "checkpoint_ref": {"path": "/foreign/checkpoint", "sha256": "c" * 64, "size": 1},
    }
    reserve, _publish = server_global_selection._fixed_generation_callbacks(desired["routing"], token)
    with xray_writer_guard(timeout_seconds=5):
        result = reserve(
            mode="reserve", reservation=reservation,
            expected_selection_revision=source_revision,
        )
    with db_session() as connection:
        assert read_selection_revision(connection) == source_revision
        marker = _load_row(connection)
    assert result == {"ok": False}
    assert marker["workflow"]["phase"] == "intent"


def test_fixed_generation_checkpoint_accepts_generated_config_above_base_input_limit(
    monkeypatch, tmp_path: Path,
) -> None:
    from fwrouter_api.services import mihomo_config, mihomo_reconcile
    from fwrouter_api.services.mihomo_config_paths import MAX_BASE_CONFIG_BYTES

    monkeypatch.setattr(mihomo_config, "_resolved_last_good_mihomo_dir", lambda: tmp_path / "last-good")
    accepted_id = "8ab7f539-32af-4cf9-acf3-b75d48b43b7d"
    payload = b"x" * (MAX_BASE_CONFIG_BYTES + 1024 * 1024)
    reference = mihomo_reconcile._write_fixed_generation_checkpoint(accepted_id, payload)

    assert reference is not None
    assert reference["size"] == len(payload)
    assert mihomo_reconcile._fixed_generation_checkpoint_matches(accepted_id, reference)


def test_fixed_generation_pending_cas_failure_rolls_back_selection_reservation(
    monkeypatch, tmp_path: Path,
) -> None:
    from fwrouter_api.adapters.xray_common import xray_writer_guard
    from fwrouter_api.services import core_intent, mihomo_config_paths, mihomo_runtime, server_global_selection
    from fwrouter_api.services.mihomo_reconcile import _write_fixed_generation_checkpoint
    from fwrouter_api.services.vpn_auto_selection_state import read_selection_revision
    from fwrouter_api.services.xray_vpn_auto_pending import _load_row

    server_id = "fixed-reservation-cas-rollback"
    _seed_server(server_id, vpn_auto=False, global_list=False)
    base_config = tmp_path / "mihomo.yaml"
    base_config.write_bytes(b"before\n")
    monkeypatch.setattr(core_intent, "read_core_runtime_context", lambda: {
        "boot_id": "00000000-0000-0000-0000-000000000001",
        "network_namespace_inode": "1",
    })
    monkeypatch.setattr(mihomo_runtime, "get_mihomo_runtime_incarnation", lambda **_kwargs: "reservation-runtime")
    monkeypatch.setattr(mihomo_config_paths, "_resolved_base_config_path", lambda: str(base_config))
    monkeypatch.setattr(mihomo_config_service, "_resolved_last_good_mihomo_dir", lambda: tmp_path / "last-good")
    desired = set_global_fixed_server(server_id, requested_by="pytest")
    token = server_global_selection._capture_fixed_apply_context(desired["routing"])
    assert token is not None
    source_revision = token["selection_revision"]
    operation_id = "8ab7f539-32af-4cf9-acf3-b75d48b43b7d"
    checkpoint = _write_fixed_generation_checkpoint(operation_id, base_config.read_bytes())
    assert checkpoint is not None
    reservation = {
        "schema": "mihomo-fixed-generation-reservation/v1",
        "operation_id": operation_id,
        "before_revision": source_revision,
        "selection_revision": source_revision + 1,
        "candidate_config_hash": "b" * 64,
        "input_fingerprint_hash": token["input_hash"],
        "before_active_config_hash": token["active_config_hash"],
        "before_runtime_incarnation": token["runtime_incarnation"],
        "checkpoint_ref": checkpoint,
    }
    from fwrouter_api.services import xray_vpn_auto_pending

    monkeypatch.setattr(xray_vpn_auto_pending, "_save_if_revision", lambda *_args, **_kwargs: False)
    reserve, _publish = server_global_selection._fixed_generation_callbacks(desired["routing"], token)
    with xray_writer_guard(timeout_seconds=5):
        result = reserve(
            mode="reserve", reservation=reservation,
            expected_selection_revision=source_revision,
        )
    with db_session() as connection:
        assert read_selection_revision(connection) == source_revision
        marker = _load_row(connection)
    assert result == {"ok": False}
    assert marker["workflow"]["phase"] == "intent"


def test_malformed_fixed_ttl_is_rejected_by_proof_token_and_restore(monkeypatch) -> None:
    _seed_server("fixed-malformed-ttl", vpn_auto=False, global_list=False)
    selected = set_global_fixed_server("fixed-malformed-ttl", requested_by="pytest")
    assert selected["ok"] is True
    with db_session() as connection:
        connection.execute(
            """UPDATE routing_global_state
               SET applied_fixed_server_id=desired_fixed_server_id,
                   fixed_server_until='garbage', apply_state='clean'
               WHERE id=1"""
        )
        revision = read_selection_revision(connection)
        from fwrouter_api.services.core_intent import read_core_intent_revision

        core_revision = read_core_intent_revision(connection)
        core_row = connection.execute(
            "SELECT desired_state, lifecycle_mode, apply_state FROM modules WHERE module_name='core'"
        ).fetchone()
        core_snapshot = tuple(str(core_row[key]) for key in (
            "desired_state", "lifecycle_mode", "apply_state",
        ))

    routing = get_routing_global_state(expire_ttl=False)
    from fwrouter_api.services.mihomo_config_status import _fixed_intent_is_clean_and_unexpired
    from fwrouter_api.services.server_global_selection import _fixed_apply_token_matches
    from fwrouter_api.services import core_intent, mihomo_runtime

    assert _fixed_intent_is_clean_and_unexpired(routing) is False
    monkeypatch.setattr(core_intent, "read_core_runtime_context", lambda: {
        "boot_id": "00000000-0000-0000-0000-000000000001",
        "network_namespace_inode": "1",
    })
    monkeypatch.setattr(mihomo_runtime, "get_mihomo_runtime_incarnation", lambda **_kwargs: "synthetic-incarnation")
    assert _fixed_apply_token_matches(
        server_id="fixed-malformed-ttl",
        fixed_until="garbage",
        token={
            "selection_revision": revision,
            "core_revision": core_revision,
            "core_snapshot": core_snapshot,
            "core_runtime_context": {
                "boot_id": "00000000-0000-0000-0000-000000000001",
                "network_namespace_inode": "1",
            },
            "runtime_incarnation": "synthetic-incarnation",
        },
    ) is False

    class Operations:
        def list_servers(self):
            return []

        def apply_server_to_selector(self, selector_name: str, target: str):
            pytest.fail(f"malformed fixed TTL must not write {selector_name}={target}")

    adapter = {
        "adapter_id": "synthetic-mihomo",
        "capabilities": [
            selector_service.RUNTIME_CAPABILITY_LIST_SERVERS,
            selector_service.RUNTIME_CAPABILITY_APPLY_SELECTOR,
            selector_service.RUNTIME_CAPABILITY_HEALTH,
        ],
    }
    monkeypatch.setattr(selector_service, "_active_selector_runtime", lambda: (adapter, Operations()))
    monkeypatch.setattr(
        selector_service,
        "_runtime_health_or_error",
        lambda *_args, **_kwargs: (SimpleNamespace(runtime_state="running"), None),
    )
    result = selector_service.restore_mihomo_selector_state(
        routing=get_routing_global_state(expire_ttl=False),
    )
    assert result["ok"] is False
    assert result.get("blocked") is True
    assert result.get("skip_reason") == "fixed_routing_intent_unsettled"


@pytest.mark.parametrize("auto_commits_during_postcheck", [True, False])
def test_fixed_completion_cannot_overwrite_a_newer_auto_clear_or_native_drift(
    monkeypatch,
    tmp_path: Path,
    auto_commits_during_postcheck: bool,
) -> None:
    runtime_name = _seed_server("fixed-clear-race", vpn_auto=False, global_list=True)
    state: dict[str, str] = {"vpn-global": "vpn-auto"}
    native_put_done = Event()
    allow_fixed_apply_return = Event()
    clear_started = Event()
    clear_done = Event()
    post_check_started = Event()
    allow_post_check_finish = Event()
    thread_errors: list[BaseException] = []
    fixed_result: dict[str, Any] = {}
    clear_result: dict[str, Any] = {}

    import yaml

    from fwrouter_api.services import mihomo_config_status
    from fwrouter_api.services import mihomo_reconcile
    from fwrouter_api.services import mihomo_runtime
    from fwrouter_api.services import mihomo_config_paths
    from fwrouter_api.services import core_intent

    active_config = tmp_path / "config.yaml"
    synthetic_routing = {
        "server_mode": "fixed",
        "desired_fixed_server_id": "fixed-clear-race",
        "applied_fixed_server_id": "fixed-clear-race",
        "desired_mode": "selective",
        "applied_mode": "selective",
        "selective_default": "direct",
    }
    config = mihomo_config_service.build_mihomo_config(synthetic_routing, xray_handoff_assignments=[])
    active_config.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    monkeypatch.setattr(mihomo_config_paths, "_resolved_base_config_path", lambda: active_config)
    monkeypatch.setattr(mihomo_config_status, "_resolved_base_config_path", lambda: active_config)
    monkeypatch.setattr(core_intent, "read_core_runtime_context", lambda: {
        "boot_id": "00000000-0000-0000-0000-000000000001",
        "network_namespace_inode": "1",
    })
    monkeypatch.setattr(mihomo_runtime, "get_mihomo_runtime_incarnation", lambda **_kwargs: "synthetic-incarnation")

    def fake_health():
        groups = {
            str(group.get("name")): list(group.get("proxies") or [])
            for group in config.get("proxy-groups", [])
            if isinstance(group, dict)
        }
        return SimpleNamespace(
            runtime_state="running",
            active_server_id=state["vpn-global"],
            details={"selectors": {
                "vpn_global_exists": True,
                "vpn_global_has_vpn_auto": "vpn-auto" in groups.get("vpn-global", []),
                "vpn_global_targets": groups.get("vpn-global", []),
                "vpn_auto_targets": groups.get("vpn-auto", []),
                "vpn_auto_now": "REJECT" if groups.get("vpn-auto") == ["REJECT"] else None,
                "vpn_auto_selected_target": "REJECT" if groups.get("vpn-auto") == ["REJECT"] else None,
                "vpn_global_now": state["vpn-global"],
                "vpn_global_selected_target": state["vpn-global"],
            }, "config": {"fwrouter_contours": {"transparent_vpn": {
                "transparent_tcp_ready": True,
                "transparent_udp_ready": True,
                "transparent_tcp_listener_socket_present": True,
                "transparent_udp_listener_socket_present": True,
            }}}},
        )

    monkeypatch.setattr(DEFAULT_MIHOMO_ADAPTER, "health", fake_health)
    monkeypatch.setattr(
        mihomo_config_status, "mihomo_runtime_fixed_target_materialized",
        lambda _routing, require_selected=False, **_kwargs: (
            {
                "ok": (not require_selected or state["vpn-global"] == runtime_name),
                "fixed_runtime_target": runtime_name,
                "config_membership_ok": True,
                "native_membership_ok": True,
                "logical_group_native_membership_ok": True,
                "vpn_global_now": state["vpn-global"],
                "selected_fixed_target": state["vpn-global"] == runtime_name,
            }
        ),
    )

    def synthetic_generation(
        *, routing, job_id, verification_callback,
        generation_materialization_callback=None,
        generation_reservation_callback=None,
        generation_completion_callback=None,
        **_generation_callbacks,
    ):
        nonlocal config
        from fwrouter_api.adapters.xray_common import xray_writer_guard_is_held
        from fwrouter_api.services.mihomo_reconcile import (
            _file_hash, _write_fixed_generation_checkpoint,
            current_mihomo_input_fingerprint,
        )
        from fwrouter_api.services.vpn_auto_selection_state import read_selection_revision

        if routing.get("server_mode") == "fixed":
            assert callable(generation_materialization_callback)
            assert generation_materialization_callback is not verification_callback
        config = mihomo_config_service.build_mihomo_config(routing, xray_handoff_assignments=[])
        candidate_bytes = yaml.safe_dump(config, sort_keys=False).encode("utf-8")
        if str(routing.get("server_mode") or "auto").strip().lower() == "fixed":
            assert callable(generation_reservation_callback)
            assert callable(generation_completion_callback)
            assert xray_writer_guard_is_held()
            operation_id = "b58826ea-61e1-4cf0-8b96-5c51402cde11"
            before_bytes = active_config.read_bytes()
            checkpoint_ref = _write_fixed_generation_checkpoint(operation_id, before_bytes)
            assert checkpoint_ref is not None
            with db_session() as connection:
                before_revision = read_selection_revision(connection)
            input_hash = current_mihomo_input_fingerprint(routing)["hash"]
            reservation = {
                "schema": "mihomo-fixed-generation-reservation/v1",
                "operation_id": operation_id,
                "job_id": job_id,
                "before_revision": before_revision,
                "selection_revision": before_revision + 1,
                "input_fingerprint_hash": input_hash,
                "candidate_config_hash": hashlib.sha256(candidate_bytes).hexdigest(),
                "before_active_config_hash": hashlib.sha256(before_bytes).hexdigest(),
                "before_runtime_incarnation": "synthetic-incarnation",
                "checkpoint_ref": checkpoint_ref,
            }
            reserved = generation_reservation_callback(
                mode="reserve", reservation=reservation,
                expected_selection_revision=before_revision,
            )
            assert reserved.get("ok") is True, reserved
            expected_revision = reserved["selection_revision"]
            active_config.write_bytes(candidate_bytes)
            verification = generation_materialization_callback(
                operation_id=operation_id,
                expected_selection_revision=expected_revision,
            )
            assert verification.get("ok") is True, verification
            state["vpn-global"] = "REJECT"
            native_proof = mihomo_config_status.mihomo_runtime_fixed_target_materialized(routing)
            assert native_proof.get("ok") is True, native_proof
            active_hash = _file_hash(str(active_config))
            receipt = {
                "schema": "mihomo-fixed-generation-receipt/v1",
                "operation_id": operation_id,
                "job_id": job_id,
                "before_revision": before_revision,
                "selection_revision": expected_revision,
                "input_fingerprint_hash": input_hash,
                "candidate_config_hash": reservation["candidate_config_hash"],
                "active_config_hash": active_hash,
                "runtime_incarnation": "synthetic-incarnation",
                "native_udp_policy_verified": True,
                "native_top_level_rules_match": True,
                "native_selector_membership_matches": True,
                "guarded_rules_match": True,
                "publication_verified": True,
                "unchanged_config": False,
            }
            published = generation_completion_callback(
                operation_id=operation_id,
                expected_selection_revision=expected_revision,
                receipt=receipt,
            )
            assert published.get("ok") is True, published
            generation_fence = {
                "schema": "mihomo-reentry-generation-fence/v1",
                "operation_id": operation_id,
                "job_id": job_id,
                "before_revision": before_revision,
                "selection_revision": expected_revision,
                "runtime_incarnation": "synthetic-incarnation",
                "active_config_hash": active_hash,
                "native_proof_verified": True,
                "publication_verified": True,
                "unchanged_config": False,
            }
        else:
            active_config.write_bytes(candidate_bytes)
            with db_session() as connection:
                expected_revision = read_selection_revision(connection)
            operation_id = "synthetic-generation"
            verification = verification_callback(
                operation_id=operation_id,
                expected_selection_revision=expected_revision,
            )
            generation_fence = {
                "schema": "mihomo-reentry-generation-fence/v1",
                "operation_id": operation_id,
                "job_id": job_id,
                "before_revision": expected_revision,
                "selection_revision": expected_revision,
                "runtime_incarnation": "synthetic-incarnation",
                "active_config_hash": hashlib.sha256(active_config.read_bytes()).hexdigest(),
                "native_proof_verified": True,
                "publication_verified": True,
                "unchanged_config": True,
            }
        return {
            "ok": bool(verification.get("ok")),
            "operation_id": operation_id,
            "job_id": job_id,
            "reconcile_reason": "input_fingerprint_unchanged",
            "verification_callback_result": verification,
            "reentry_generation_fence": generation_fence,
        }

    monkeypatch.setattr(mihomo_reconcile, "reconcile_mihomo_runtime", synthetic_generation)
    def fake_get_active_server_id() -> str:
        return state["vpn-global"]

    def fake_apply_server_to_selector(selector_name: str, server_id: str) -> MihomoApplyResult:
        assert selector_name == "vpn-global"
        state[selector_name] = server_id
        result = MihomoApplyResult(
            ok=True,
            message="synthetic native selector readback matched",
            active_server_id=server_id,
            details={"selector_after": server_id},
        )
        if server_id == runtime_name:
            native_put_done.set()
            if not allow_fixed_apply_return.wait(timeout=5):
                raise TimeoutError("bounded synthetic interleaving was not released")
        return result

    monkeypatch.setattr(DEFAULT_MIHOMO_ADAPTER, "get_active_server_id", fake_get_active_server_id)
    monkeypatch.setattr(DEFAULT_MIHOMO_ADAPTER, "apply_server_to_selector", fake_apply_server_to_selector)
    ping_count = 0

    def fake_check_server_delay(server_id: str, **_kwargs):
        nonlocal ping_count
        ping_count += 1
        if ping_count == 2:
            if not auto_commits_during_postcheck:
                # Simulate an unowned selector change during the unlocked probe.
                state["vpn-global"] = "vpn-auto"
            post_check_started.set()
            if not allow_post_check_finish.wait(timeout=5):
                raise TimeoutError("bounded synthetic post-check was not released")
        return {
            "ok": True,
            "server_id": server_id,
            "status": "success",
            "error_code": None,
            "error_message": None,
        }

    monkeypatch.setattr("fwrouter_api.services.server_ping.check_server_delay", fake_check_server_delay)

    def fixed_apply_thread() -> None:
        try:
            fixed_result.update(
                apply_global_fixed_server(
                    "fixed-clear-race",
                    requested_by="pytest",
                )
            )
        except BaseException as exc:  # bounded handoff to deterministic main-thread assertion
            thread_errors.append(exc)

    def auto_clear_thread() -> None:
        clear_started.set()
        try:
            clear_result.update(apply_global_auto_server(requested_by="pytest"))
        except BaseException as exc:
            thread_errors.append(exc)
        finally:
            clear_done.set()

    fixed_thread = Thread(target=fixed_apply_thread, name="fixed-mode-clear-race")
    clear_thread = Thread(target=auto_clear_thread, name="fixed-mode-newer-auto-clear")
    fixed_thread.start()
    try:
        assert native_put_done.wait(timeout=5), (
            "fixed native PUT did not reach the controlled barrier; "
            f"result={fixed_result!r}; thread_errors={thread_errors!r}"
        )
        with db_session() as connection:
            revision_after_fixed_intent = read_selection_revision(connection)

        allow_fixed_apply_return.set()
        assert post_check_started.wait(timeout=5), "fixed apply skipped the unlocked post-check window"
        if auto_commits_during_postcheck:
            clear_thread.start()
            assert clear_started.wait(timeout=5)
            assert clear_done.wait(timeout=5), "newer auto clear did not finish during the fixed post-check"
        allow_post_check_finish.set()
        fixed_thread.join(timeout=5)
        if clear_thread.ident is not None:
            clear_thread.join(timeout=5)

        assert not fixed_thread.is_alive(), "fixed apply thread exceeded the join bound"
        assert not clear_thread.is_alive(), "auto clear thread exceeded the join bound"
        assert not thread_errors, f"selection thread failed: {thread_errors!r}"
        assert fixed_result["ok"] is False
        assert fixed_result["error_code"] == (
            "GLOBAL_FIXED_SERVER_STALE"
            if auto_commits_during_postcheck
            else "GLOBAL_FIXED_SERVER_NATIVE_PROOF_FAILED"
        )
        if auto_commits_during_postcheck:
            assert clear_result["ok"] is True, clear_result
            assert clear_result["routing_after"]["server_mode"] == "auto"
        assert state["vpn-global"] == "vpn-auto"
        with db_session() as connection:
            revision_after_clear = read_selection_revision(connection)
        if auto_commits_during_postcheck:
            assert revision_after_clear > revision_after_fixed_intent
            assert clear_result["routing_after"]["applied_fixed_server_id"] is None
        else:
            assert revision_after_clear == revision_after_fixed_intent + 1
            from fwrouter_api.services.xray_vpn_auto_pending import _load_row

            with db_session() as connection:
                pending = _load_row(connection)
            assert pending is not None and pending["pending"] is True
    finally:
        allow_fixed_apply_return.set()
        allow_post_check_finish.set()
        fixed_thread.join(timeout=5)
        if clear_thread.ident is not None:
            clear_thread.join(timeout=5)


def test_newer_auto_intent_wins_before_fixed_generation_starts(monkeypatch, tmp_path: Path) -> None:
    _seed_server("fixed-before-generation-race", vpn_auto=False, global_list=False)
    from fwrouter_api.adapters import mihomo as mihomo_adapter_module
    from fwrouter_api.services import core_intent, mihomo_reconcile, mihomo_runtime, server_global_selection

    monkeypatch.setattr(core_intent, "read_core_runtime_context", lambda: {
        "boot_id": "00000000-0000-0000-0000-000000000001",
        "network_namespace_inode": "1",
    })
    monkeypatch.setattr(mihomo_runtime, "get_mihomo_runtime_incarnation", lambda **_kwargs: "pre-generation-runtime")
    monkeypatch.setattr(
        mihomo_adapter_module,
        "DEFAULT_MIHOMO_ADAPTER",
        SimpleNamespace(get_active_server_id=lambda: "vpn-auto", apply_server_to_selector=lambda *_args: pytest.fail("stale generation must not select")),
    )

    materialization_captured = Event()
    allow_generation_check = Event()
    reconcile_calls: list[dict[str, Any]] = []
    original_materializer = server_global_selection._materialize_fixed_server_generation

    def delayed_materializer(routing, *, token, job_id):
        materialization_captured.set()
        if not allow_generation_check.wait(timeout=5):
            raise TimeoutError("bounded pre-generation race was not released")
        return original_materializer(routing, token=token, job_id=job_id)

    monkeypatch.setattr(server_global_selection, "_materialize_fixed_server_generation", delayed_materializer)
    monkeypatch.setattr(
        mihomo_reconcile,
        "reconcile_mihomo_runtime",
        lambda **kwargs: reconcile_calls.append(kwargs) or {"ok": False},
    )
    fixed_result: dict[str, Any] = {}
    thread_errors: list[BaseException] = []

    def fixed_apply_thread() -> None:
        try:
            fixed_result.update(apply_global_fixed_server("fixed-before-generation-race", requested_by="pytest"))
        except BaseException as exc:
            thread_errors.append(exc)

    thread = Thread(target=fixed_apply_thread, name="fixed-pre-generation-race")
    thread.start()
    try:
        assert materialization_captured.wait(timeout=5), "fixed apply did not capture intent before generation"
        auto_result = clear_global_fixed_server(requested_by="pytest-newer-auto")
        allow_generation_check.set()
        thread.join(timeout=5)
        assert not thread.is_alive(), "fixed apply thread exceeded the join bound"
        assert not thread_errors, f"fixed apply thread failed: {thread_errors!r}"
        assert auto_result["ok"] is True
        assert fixed_result["ok"] is False, fixed_result
        assert fixed_result["error_code"] == "GLOBAL_FIXED_SERVER_STALE"
        assert reconcile_calls == []
        routing = get_routing_global_state(expire_ttl=False)
        assert routing["server_mode"] == "auto"
        assert routing["desired_fixed_server_id"] is None
        assert routing["applied_fixed_server_id"] is None
    finally:
        allow_generation_check.set()
        thread.join(timeout=5)


def test_auto_clear_cannot_enter_while_fixed_reconcile_effect_window_is_open(monkeypatch, tmp_path: Path) -> None:
    _seed_server("fixed-reconcile-window-race", vpn_auto=False, global_list=False)
    from fwrouter_api.adapters import mihomo as mihomo_adapter_module
    from fwrouter_api.adapters.xray_common import xray_writer_guard_is_held
    from fwrouter_api.services import core_intent, mihomo_reconcile, mihomo_runtime

    monkeypatch.setattr(core_intent, "read_core_runtime_context", lambda: {
        "boot_id": "00000000-0000-0000-0000-000000000001",
        "network_namespace_inode": "1",
    })
    monkeypatch.setattr(mihomo_runtime, "get_mihomo_runtime_incarnation", lambda **_kwargs: "reconcile-window-runtime")
    monkeypatch.setattr(
        mihomo_adapter_module,
        "DEFAULT_MIHOMO_ADAPTER",
        SimpleNamespace(
            get_active_server_id=lambda: "vpn-auto",
            apply_server_to_selector=lambda *_args: pytest.fail("failed reconcile must not select"),
        ),
    )

    reconcile_entered = Event()
    allow_reconcile_finish = Event()
    clear_started = Event()
    clear_done = Event()
    reconcile_guard_held: list[bool] = []
    fixed_result: dict[str, Any] = {}
    clear_result: dict[str, Any] = {}
    thread_errors: list[BaseException] = []

    def paused_reconcile(**_kwargs):
        reconcile_guard_held.append(xray_writer_guard_is_held())
        reconcile_entered.set()
        if not allow_reconcile_finish.wait(timeout=5):
            raise TimeoutError("bounded reconcile window was not released")
        return {"ok": False, "error_code": "SYNTHETIC_RECONCILE_STOP"}

    monkeypatch.setattr(mihomo_reconcile, "reconcile_mihomo_runtime", paused_reconcile)

    def fixed_apply_thread() -> None:
        try:
            fixed_result.update(apply_global_fixed_server("fixed-reconcile-window-race", requested_by="pytest"))
        except BaseException as exc:
            thread_errors.append(exc)

    def auto_clear_thread() -> None:
        clear_started.set()
        try:
            clear_result.update(clear_global_fixed_server(requested_by="pytest-newer-auto"))
        except BaseException as exc:
            thread_errors.append(exc)
        finally:
            clear_done.set()

    fixed_thread = Thread(target=fixed_apply_thread, name="fixed-reconcile-window")
    fixed_thread.start()
    clear_thread: Thread | None = None
    try:
        assert reconcile_entered.wait(timeout=5), "fixed reconcile owner was not entered"
        clear_thread = Thread(target=auto_clear_thread, name="auto-clear-during-fixed-reconcile")
        clear_thread.start()
        assert clear_started.wait(timeout=5), "newer auto clear did not start"
        assert not clear_done.wait(timeout=0.1), "auto clear entered while fixed reconcile effect window was open"
    finally:
        allow_reconcile_finish.set()
        fixed_thread.join(timeout=5)
        if clear_thread is not None:
            clear_thread.join(timeout=5)

    assert not fixed_thread.is_alive(), "fixed apply exceeded the join bound"
    assert clear_thread is not None and not clear_thread.is_alive(), "auto clear exceeded the join bound"
    assert not thread_errors, f"selection thread failed: {thread_errors!r}"
    assert reconcile_guard_held == [True]
    assert fixed_result["ok"] is False, fixed_result
    assert clear_result["ok"] is True, clear_result
    routing = get_routing_global_state(expire_ttl=False)
    assert routing["server_mode"] == "auto"
    assert routing["desired_fixed_server_id"] is None
    assert routing["applied_fixed_server_id"] is None


def test_pending_fixed_restore_does_not_replay_stale_applied_target(monkeypatch) -> None:
    _seed_server("fixed-old-applied", vpn_auto=False, global_list=True)
    _seed_server("fixed-new-desired", vpn_auto=False, global_list=True)
    ensure_routing_global_state()
    with db_session() as connection:
        connection.execute(
            """
            UPDATE routing_global_state
            SET server_mode='fixed', desired_fixed_server_id='fixed-new-desired',
                applied_fixed_server_id='fixed-old-applied',
                fixed_server_until=datetime('now', '+1 day'), apply_state='pending',
                error_code=NULL, error_message=NULL
            WHERE id=1
            """
        )

    calls: list[tuple[str, str]] = []

    class Operations:
        def list_servers(self):
            return []

        def apply_server_to_selector(self, selector_name: str, target: str):
            calls.append((selector_name, target))
            return SimpleNamespace(ok=True, to_dict=lambda: {"ok": True, "active_server_id": target})

    operations = Operations()
    adapter = {
        "adapter_id": "synthetic-mihomo",
        "capabilities": [
            selector_service.RUNTIME_CAPABILITY_LIST_SERVERS,
            selector_service.RUNTIME_CAPABILITY_APPLY_SELECTOR,
            selector_service.RUNTIME_CAPABILITY_HEALTH,
        ],
    }
    monkeypatch.setattr(selector_service, "_active_selector_runtime", lambda: (adapter, operations))
    from fwrouter_api.services import core_intent, mihomo_runtime
    monkeypatch.setattr(core_intent, "read_core_runtime_context", lambda: {
        "boot_id": "00000000-0000-0000-0000-000000000001",
        "network_namespace_inode": "1",
    })
    monkeypatch.setattr(mihomo_runtime, "get_mihomo_runtime_incarnation", lambda **_kwargs: "synthetic-incarnation")
    monkeypatch.setattr(
        selector_service,
        "_runtime_health_or_error",
        lambda *_args, **_kwargs: (SimpleNamespace(runtime_state="running"), None),
    )
    monkeypatch.setattr(selector_service, "_load_selector_candidates", lambda: [])

    result = selector_service.restore_mihomo_selector_state(
        routing=get_routing_global_state(expire_ttl=False)
    )

    assert result["ok"] is False, result
    assert result.get("blocked") is True
    assert result.get("skip_reason") == "fixed_routing_intent_unsettled", result
    assert calls == []
