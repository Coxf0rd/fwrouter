from __future__ import annotations

from contextlib import contextmanager, nullcontext
import json
import sqlite3

import pytest

from fwrouter_api.services import provider_managed, provider_recovery
from fwrouter_api.services.routing_manifest import build_dataplane_manifest_from_state
from fwrouter_api.services.dataplane_nft_render import render_owned_table_candidate


def _install_pending_state(monkeypatch, pending=None):
    state = {"pending": pending}

    def get_pending():
        value = state["pending"]
        return dict(value) if isinstance(value, dict) else None

    def set_pending(value):
        state["pending"] = dict(value) if isinstance(value, dict) else None

    monkeypatch.setattr(provider_recovery, "get_recovery_pending", get_pending)
    monkeypatch.setattr(provider_recovery, "set_recovery_pending", set_pending)
    @contextmanager
    def no_guard(*_args, **_kwargs):
        yield
    monkeypatch.setattr("fwrouter_api.adapters.xray_common.xray_writer_guard", no_guard)
    monkeypatch.setattr(provider_recovery, "_transition_event", lambda *_args, **_kwargs: None)

    def cas(expected, value):
        if state["pending"] != expected:
            return False
        state["pending"] = dict(value) if isinstance(value, dict) else None
        return True

    def capture(controller, value):
        return {"pending": dict(value), "active_target_id": controller.target,
                "selection_fence": {"revision": 0}, "pool": "test-pool",
                "runtime_incarnation": "test-incarnation"}

    def matches(controller, snapshot, *, expected_pending=None, expected_target=None):
        expected = expected_pending if expected_pending is not None else snapshot["pending"]
        return (state["pending"] == expected
                and (expected_target is None or str(controller.target or "") == str(expected_target)))

    monkeypatch.setattr(provider_recovery, "_cas_pending", cas)
    monkeypatch.setattr(provider_recovery, "_capture_recovery_context", capture)
    monkeypatch.setattr(provider_recovery, "_recovery_context_matches", matches)
    monkeypatch.setattr(provider_recovery, "_automatic_member_switch_allowed", lambda _source: True)
    def complete(expected):
        from fwrouter_api.services.watchdog_failure_state import reset_traffic_failure_candidate
        if not cas(expected, None):
            return False
        reset_traffic_failure_candidate()
        return True
    monkeypatch.setattr("fwrouter_api.services.watchdog_failure_state.complete_recovery_pending", complete)
    return state


class _RecoveryController:
    def __init__(self, probes, target="logical-provider"):
        self.probes = list(probes)
        self.target = target
        self.calls = []

    def probe(self, *, update_ping_state, timeout_ms, reason):
        self.calls.append(("probe", update_ping_state, timeout_ms, reason))
        return self.probes.pop(0)

    def get_state(self):
        self.calls.append(("get_state",))
        return {"active_target_id": self.target}


class _RecoveryAdapter:
    provider_id = "stealthsurf"
    supported_protocols = ("hysteria2",)

    def __init__(self, status="down"):
        self.status = status
        self.calls = []
        self.closed = False

    def get_server_stats(self, config_id, *, budget=None, max_age_s=None):
        self.calls.append(("stats", config_id, budget.max_requests, budget.deadline_seconds, max_age_s))
        return {"status": self.status}

    def discover(self, *args, **kwargs):
        self.calls.append(("discover", args, kwargs))
        return []

    def get_configs(self, *args, **kwargs):
        self.calls.append(("get_configs", args, kwargs))
        return []

    def get_locations(self, *args, **kwargs):
        self.calls.append(("get_locations", args, kwargs))
        return []

    def close(self):
        self.closed = True


def _provider_binding():
    return {
        "source_ref": "source-provider",
        "binding_revision": 7,
        "logical_server_id": "logical-provider",
        "provider_id": "stealthsurf",
        "resource_id": 1234,
        "current_member_id": "server-current",
        "current_location_id": 26,
        "protocol": "hysteria2",
        "observed_protocol": "hysteria2",
        "allow_automatic_member_switch": True,
    }


def test_confirmed_recovery_uses_three_distinct_cycles_and_bounded_provider_calls(monkeypatch):
    state = _install_pending_state(monkeypatch)
    binding = _provider_binding()
    adapter = _RecoveryAdapter(status="down")
    operations = []
    candidate = {"member_id": "server-alternative"}

    monkeypatch.setattr(provider_managed, "binding_for_logical", lambda _logical: binding)
    monkeypatch.setattr(provider_managed, "execute_provider_operation", lambda source, action, **kwargs: (
        operations.append((source, action, kwargs)) or {"ok": True, "outcome": "verified"}
    ))
    monkeypatch.setattr(provider_managed, "provider_candidates", lambda _source: [candidate])
    monkeypatch.setattr(provider_managed, "db_session", lambda: nullcontext(object()))
    monkeypatch.setattr(provider_managed.store, "update_observation", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("fwrouter_api.services.provider_adapters.provider_adapter", lambda *_args, **_kwargs: adapter)
    monkeypatch.setattr(
        "fwrouter_api.services.selector._select_candidate_with_priority",
        lambda candidates: (candidates[0], 0),
    )
    apply_calls = []
    monkeypatch.setattr(
        provider_recovery, "_apply_override",
        lambda *, reentry: apply_calls.append(reentry) or {"ok": True, "outcome": "verified"},
    )
    controller = _RecoveryController([{"ok": False}])

    first = provider_recovery.confirmed_provider_recovery(
        logical_server_id="logical-provider", path_key="lan", decision_id="decision-1",
        controller=controller, timeout_ms=5000, allow_switch=True,
    )
    repeated = provider_recovery.confirmed_provider_recovery(
        logical_server_id="logical-provider", path_key="lan", decision_id="decision-1",
        controller=controller, timeout_ms=5000, allow_switch=True,
    )
    second = provider_recovery.confirmed_provider_recovery(
        logical_server_id="logical-provider", path_key="lan", decision_id="decision-2",
        controller=controller, timeout_ms=5000, allow_switch=True,
    )
    third = provider_recovery.confirmed_provider_recovery(
        logical_server_id="logical-provider", path_key="lan", decision_id="decision-3",
        controller=controller, timeout_ms=5000, allow_switch=True,
    )

    assert first["provider_confirmation"] == 1
    assert repeated["status"] == "provider_recovery_pending"
    assert second["provider_confirmation"] == 2
    assert third["provider_confirmation"] == 3
    assert third["effective_override"] == "emergency_direct"
    assert operations[0][0:2] == ("source-provider", "recovery_refresh")
    assert operations[0][2]["expected_revision"] == 7
    assert operations[0][2]["expected_selection_revision"] == 0
    assert operations[0][2]["expected_runtime_incarnation"] == "test-incarnation"
    assert operations[1][0:2] == ("source-provider", "switch")
    assert operations[1][2]["member_id"] == "server-alternative"
    assert operations[1][2]["expected_revision"] == 7
    assert operations[1][2]["_adapter"] is adapter
    assert operations[1][2]["_budget"].max_requests == 4
    assert operations[1][2]["_budget"].deadline_seconds == 30
    assert adapter.calls == [("stats", 1234, 4, 30, 0)]
    assert adapter.closed
    assert not any(call[0] in {"discover", "get_configs", "get_locations"} for call in adapter.calls)
    assert controller.calls == [("probe", True, 5000, "provider_emergency_preflight")]
    assert apply_calls == [False]
    assert state["pending"]["phase"] == "emergency_direct"
    assert state["pending"]["provider_confirmation"] == 3


def test_suppressed_or_unconfirmed_watchdog_cycles_make_zero_provider_calls(monkeypatch):
    _install_pending_state(monkeypatch)
    binding = _provider_binding()
    provider_calls = []
    monkeypatch.setattr(provider_managed, "binding_for_logical", lambda _logical: binding)
    monkeypatch.setattr(
        provider_managed, "execute_provider_operation",
        lambda *args, **kwargs: provider_calls.append((args, kwargs)),
    )
    monkeypatch.setattr(
        "fwrouter_api.services.provider_adapters.provider_adapter",
        lambda *_args, **_kwargs: pytest.fail("provider adapter must not be opened"),
    )
    controller = _RecoveryController([])

    suppressed = provider_recovery.confirmed_provider_recovery(
        logical_server_id="logical-provider", path_key="lan", decision_id="d1",
        controller=controller, timeout_ms=3000, allow_switch=False,
    )

    assert suppressed["status"] == "provider_recovery_suppressed"
    assert provider_calls == []
    assert controller.calls == []


def test_disabled_automatic_member_switch_gates_phase_two_before_provider_io(monkeypatch):
    state = _install_pending_state(monkeypatch, {
        "provider_managed": True,
        "source_ref": "source-provider",
        "binding_revision": 7,
        "logical_server_id": "logical-provider",
        "path_key": "lan",
        "phase": "provider_recovery",
        "provider_confirmation": 1,
        "traffic_decision_id": "d0",
    })
    binding = _provider_binding() | {"allow_automatic_member_switch": False}
    monkeypatch.setattr(provider_managed, "binding_for_logical", lambda _logical: binding)
    monkeypatch.setattr("fwrouter_api.services.vpn_auto_exclusive.get_vpn_auto_exclusive_source_ref", lambda: None)
    monkeypatch.setattr(provider_recovery, "_automatic_member_switch_allowed", lambda _source: False)
    calls = []
    monkeypatch.setattr("fwrouter_api.services.provider_adapters.provider_adapter",
                        lambda *_args, **_kwargs: calls.append("adapter") or pytest.fail("provider adapter must not open"))
    monkeypatch.setattr(provider_managed, "provider_candidates",
                        lambda *_args, **_kwargs: calls.append("candidates") or pytest.fail("candidates must not be selected"))
    monkeypatch.setattr(provider_managed, "execute_provider_operation",
                        lambda *_args, **_kwargs: calls.append("provider_operation") or pytest.fail("operation must not run"))
    controller = _RecoveryController([])

    result = provider_recovery.confirmed_provider_recovery(
        logical_server_id="logical-provider", path_key="lan", decision_id="d1",
        controller=controller, timeout_ms=3000, allow_switch=True,
    )

    assert result["status"] == "provider_auto_switch_disabled"
    assert result["reason"] == "provider_auto_switch_disabled"
    assert result["switch_attempted"] is False
    assert calls == []
    assert controller.calls == []
    assert state["pending"]["provider_confirmation"] == 2
    assert state["pending"]["last_outcome"] == "policy_disabled"
    assert state["pending"]["error_code"] == "provider_auto_switch_disabled"


def test_policy_change_during_provider_status_probe_fences_candidate_selection(monkeypatch):
    _install_pending_state(monkeypatch, {
        "provider_managed": True,
        "source_ref": "source-provider",
        "binding_revision": 7,
        "logical_server_id": "logical-provider",
        "path_key": "lan",
        "phase": "provider_recovery",
        "provider_confirmation": 1,
        "traffic_decision_id": "d0",
    })
    binding = _provider_binding()
    disabled = {"value": False}
    adapter = _RecoveryAdapter(status="down")
    get_stats = adapter.get_server_stats

    def status_probe(*args, **kwargs):
        result = get_stats(*args, **kwargs)
        disabled["value"] = True
        return result

    adapter.get_server_stats = status_probe
    monkeypatch.setattr(provider_managed, "binding_for_logical", lambda _logical: binding)
    monkeypatch.setattr("fwrouter_api.services.vpn_auto_exclusive.get_vpn_auto_exclusive_source_ref", lambda: None)
    monkeypatch.setattr(provider_recovery, "_automatic_member_switch_allowed", lambda _source: not disabled["value"])
    monkeypatch.setattr(provider_recovery, "_recovery_context_matches", lambda *_a, **_kw: not disabled["value"])
    monkeypatch.setattr(provider_managed, "db_session", lambda: nullcontext(object()))
    monkeypatch.setattr(provider_managed.store, "update_observation", lambda *_a, **_kw: None)
    monkeypatch.setattr("fwrouter_api.services.provider_adapters.provider_adapter", lambda *_a, **_kw: adapter)
    monkeypatch.setattr(provider_managed, "provider_candidates",
                        lambda *_a, **_kw: pytest.fail("stale policy must fence candidates"))
    controller = _RecoveryController([])

    result = provider_recovery.confirmed_provider_recovery(
        logical_server_id="logical-provider", path_key="lan", decision_id="d1",
        controller=controller, timeout_ms=3000, allow_switch=True,
    )

    assert result["error_code"] == "provider_recovery_stale"
    assert [call[0] for call in adapter.calls] == ["stats"]


def test_policy_disabled_recovery_uses_transition_journal_reason(monkeypatch):
    from types import SimpleNamespace
    from fwrouter_api.services import watchdog_auto_stall_flow

    journal = []
    monkeypatch.setattr(watchdog_auto_stall_flow, "get_recovery_pending", lambda: None)
    monkeypatch.setattr(provider_recovery, "confirmed_provider_recovery", lambda **_kwargs: {
        "ok": True, "status": "provider_auto_switch_disabled", "outcome": "policy_disabled",
        "action": "none", "error_code": "provider_auto_switch_disabled",
        "reason": "provider_auto_switch_disabled", "switch_attempted": False,
    })
    deps = SimpleNamespace(
        traffic_failure_confirmation=lambda **_kwargs: {"confirmed": True},
        get_settings=lambda: SimpleNamespace(watchdog_traffic_failure_confirm_seconds=10),
        update_watchdog_module=lambda **_kwargs: {"runtime_state": "degraded"},
        write_watchdog_decision_log=lambda **kwargs: journal.append(kwargs),
    )

    result = watchdog_auto_stall_flow.handle_stalled_traffic_auto_flow(
        deps, runtime_controller=object(), traffic_signal={"decision_id": "d1"},
        active_server_id="logical-provider", selection_mode="auto",
        runtime_state={}, reason="confirmed_failure", timeout_ms=1000,
        update_ping_state=True, path_key="lan", allow_switch=True, candidate_limit=4,
        routing={}, runtime_convergence={}, vpn_adapter={}, runtime_response_fields={},
        vpn_auto_state={},
    )

    assert result["status"] == "provider_auto_switch_disabled"
    assert journal[0]["event_type"] == "watchdog_recovery_transition"
    assert journal[0]["error_code"] == "provider_auto_switch_disabled"
    assert all(item["event_type"] != "watchdog_switch_unconfirmed" for item in journal)


def test_policy_disabled_phase_two_advances_to_existing_emergency_direct_phase(monkeypatch):
    state = _install_pending_state(monkeypatch, {
        "provider_managed": True,
        "source_ref": "source-provider",
        "binding_revision": 7,
        "logical_server_id": "logical-provider",
        "path_key": "lan",
        "phase": "provider_recovery",
        "provider_confirmation": 1,
        "traffic_decision_id": "d0",
    })
    monkeypatch.setattr(provider_managed, "binding_for_logical", lambda _logical: _provider_binding() | {
        "allow_automatic_member_switch": False,
    })
    monkeypatch.setattr("fwrouter_api.services.vpn_auto_exclusive.get_vpn_auto_exclusive_source_ref", lambda: None)
    monkeypatch.setattr(provider_recovery, "_automatic_member_switch_allowed", lambda _source: False)
    monkeypatch.setattr("fwrouter_api.services.provider_adapters.provider_adapter",
                        lambda *_a, **_kw: pytest.fail("policy-disabled recovery must not open provider adapter"))
    applied = []
    monkeypatch.setattr(provider_recovery, "_apply_override",
                        lambda *, reentry: applied.append(reentry) or {"ok": True, "outcome": "verified"})
    controller = _RecoveryController([{"ok": False}])

    skipped = provider_recovery.confirmed_provider_recovery(
        logical_server_id="logical-provider", path_key="lan", decision_id="d1",
        controller=controller, timeout_ms=3000, allow_switch=True,
    )
    direct = provider_recovery.confirmed_provider_recovery(
        logical_server_id="logical-provider", path_key="lan", decision_id="d2",
        controller=controller, timeout_ms=3000, allow_switch=True,
    )

    assert skipped["error_code"] == "provider_auto_switch_disabled"
    assert state["pending"]["provider_confirmation"] == 3
    assert direct["status"] == "emergency_direct"
    assert direct["effective_override"] == "emergency_direct"
    assert controller.calls == [("probe", True, 3000, "provider_emergency_preflight")]
    assert applied == [False]


def test_emergency_direct_manifest_and_nft_candidate_preserve_intent_and_block_disabled_subject(
    monkeypatch,
):
    _install_pending_state(monkeypatch, {
        "provider_managed": True,
        "emergency_direct": True,
        "logical_server_id": "logical-provider",
        "phase": "emergency_direct",
    })
    monkeypatch.setattr(
        "fwrouter_api.services.routing_manifest.build_global_preflight",
        lambda **_kwargs: {
            "can_enforce_global_direct": True,
            "can_enforce_global_selective": True,
            "can_enforce_global_vpn": True,
            "missing": [],
            "profile": {"profile": "provider-recovery-test", "vpn_routing_contract": {"tproxy_port": 5202}},
            "vpn_contour": {"tproxy_port": 5202, "fwmark_hex": "0x00000100"},
            "selective_vpn_ready": True,
            "selective_degraded": False,
            "selective_rules": {"requires_vpn_runtime": True, "selective_default": "vpn"},
        },
    )
    monkeypatch.setattr("fwrouter_api.services.routing_manifest.network_contract_manifest", lambda: {})
    monkeypatch.setattr("fwrouter_api.services.dataplane_nft_render.trusted_client_ipv4_nft_set", lambda: [])
    monkeypatch.setattr("fwrouter_api.services.dataplane_nft_render.trusted_client_ipv6_nft_set", lambda: [])
    monkeypatch.setattr("fwrouter_api.services.scoped_egress._load_explicit_client_runtime_bindings", lambda *_args, **_kwargs: {})

    original_routing = {
        "desired_mode": "vpn", "applied_mode": "vpn", "selective_default": "vpn",
        "server_mode": "auto", "active_auto_server_id": "logical-provider",
        "desired_fixed_server_id": None, "applied_fixed_server_id": None,
    }
    subjects = [
        {
            "subject_id": "explicit:enabled", "subject_type": "explicit_external_client",
            "implementation_kind": "xray", "display_name": "Enabled client",
            "desired_mode": "enabled", "applied_mode": "forced_vpn", "runtime_state": "running",
            "is_active": True, "user_override": None, "server_override": None,
            "effective_state": {"effective_mode": "forced_vpn", "dataplane_path": "vpn"},
        },
        {
            "subject_id": "explicit:disabled", "subject_type": "explicit_external_client",
            "implementation_kind": "xray", "display_name": "Disabled client",
            "desired_mode": "disabled", "applied_mode": "disabled", "runtime_state": "running",
            "is_active": True, "user_override": None, "server_override": None,
            "effective_state": {"effective_mode": "disabled", "dataplane_path": "blocked"},
        },
    ]
    original_subjects = json.loads(json.dumps(subjects))
    manifest = build_dataplane_manifest_from_state(
        plan_id="provider-emergency-direct",
        reason="provider_emergency_direct",
        routing=original_routing,
        subjects=subjects,
        extra={"rules_effective": {"rules": [], "selective_default": "vpn"}, "core_bypass": {"enabled": False}},
    )
    candidate = render_owned_table_candidate(manifest, rules_effective_loader=lambda: {"rules": []})
    explicit = {item["subject_id"]: item for item in manifest["subjects"]}

    assert manifest["routing_global_state"]["desired_mode"] == "direct"
    assert manifest["summary"]["global_mode"] == "direct"
    assert manifest["summary"]["requires_vpn_policy_routing"] is False
    assert explicit["explicit:enabled"]["effective_mode"] == "direct"
    assert explicit["explicit:enabled"]["mode_source"] == "emergency_direct"
    assert explicit["explicit:enabled"]["dataplane_path"] == "direct"
    assert explicit["explicit:disabled"]["effective_mode"] == "disabled"
    assert explicit["explicit:disabled"]["dataplane_path"] == "blocked"
    assert 'goto fwrouter_direct comment "global direct v1"' in candidate
    assert 'goto fwrouter_vpn_full comment "global vpn v1"' not in candidate
    assert 'jump fwrouter_vpn_full' not in candidate
    assert original_routing["desired_mode"] == "vpn"
    assert subjects == original_subjects


def test_emergency_xray_mode_directives_follow_projected_policy_without_unblocking_disabled(
    monkeypatch,
):
    _install_pending_state(monkeypatch, {"provider_managed": True, "emergency_direct": True})
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        "CREATE TABLE subjects (subject_id TEXT, desired_mode TEXT, implementation_kind TEXT, subject_type TEXT, subject_role TEXT, is_active INTEGER, is_deleted INTEGER, metadata_json TEXT)"
    )
    connection.executemany(
        "INSERT INTO subjects VALUES (?, ?, 'xray', 'explicit_external_client', 'vless_client', 1, 0, ?) ",
        [
            ("explicit:enabled", "enabled", json.dumps({"detail": {"client_id": "enabled-id", "client_uuid": "enabled-uuid", "email": "enabled@example.test", "enabled": True}})),
            ("explicit:disabled", "disabled", json.dumps({"detail": {"client_id": "disabled-id", "client_uuid": "disabled-uuid", "email": "disabled@example.test", "enabled": True}})),
        ],
    )
    connection.commit()

    @contextmanager
    def session():
        yield connection

    monkeypatch.setattr("fwrouter_api.services.xray_bindings.db_session", session)
    monkeypatch.setattr("fwrouter_api.services.scoped_egress._load_explicit_client_runtime_bindings", lambda *_args, **_kwargs: {})
    from fwrouter_api.services import subject_policy, xray_bindings

    def projected(subject_id):
        desired = "disabled" if subject_id.endswith("disabled") else "enabled"
        return subject_policy.enrich_subject_with_effective_state(
            {
                "subject_id": subject_id,
                "subject_type": "explicit_external_client",
                "implementation_kind": "xray",
                "subject_role": "vless_client",
                "desired_mode": desired,
                "is_active": True,
                "display_name": subject_id,
                "user_override": None,
                "server_override": None,
            },
            routing={"desired_mode": "vpn", "applied_mode": "vpn", "server_mode": "auto", "active_auto_server_id": "logical-provider", "applied_fixed_server_id": None, "desired_fixed_server_id": None},
            user_override=None,
            server_override=None,
            runtime_enforcement={"supported_modes": {"vpn": True}},
            bypass_state={"enabled": False},
        )

    monkeypatch.setattr(subject_policy, "get_subject_with_effective_state", projected)
    try:
        directives = xray_bindings.collect_xray_client_mode_directives()
    finally:
        connection.close()

    by_id = {item["subject_id"]: item for item in directives}
    assert by_id["explicit:enabled"]["desired_mode"] == "enabled"
    assert by_id["explicit:enabled"]["effective_mode"] == "direct"
    assert by_id["explicit:enabled"]["mode_support_state"] == "legacy_supported_direct"
    assert by_id["explicit:disabled"]["effective_mode"] == "disabled"
    assert by_id["explicit:disabled"]["mode_support_state"] == "supported"


def test_failed_emergency_apply_retains_unconfirmed_override_and_does_not_claim_direct(monkeypatch):
    previous = {"provider_managed": True, "phase": "provider_recovery", "provider_confirmation": 3}
    state = _install_pending_state(monkeypatch, previous)
    monkeypatch.setattr(provider_recovery, "_apply_override", lambda *, reentry: {"ok": False, "outcome": "failed"})

    controller = _RecoveryController([])
    snapshot = provider_recovery._capture_recovery_context(controller, previous)
    result = provider_recovery.enter_emergency_direct({
        "source_ref": "source-provider", "binding_revision": 7,
        "logical_server_id": "logical-provider", "provider_confirmation": 3,
    }, controller=controller, snapshot_context=snapshot)

    assert result["ok"] is False
    assert result["action"] == "none"
    assert result["effective_override"] is None
    assert state["pending"]["phase"] == "emergency_direct_unconfirmed"
    assert state["pending"]["emergency_direct"] is True


def test_direct_apply_stops_before_nft_pipeline_when_managed_xray_readback_fails(monkeypatch):
    from fwrouter_api.services import apply_orchestrator, jobs, xray_runtime_state, xray

    monkeypatch.setattr(apply_orchestrator, "get_routing_snapshot", lambda: {"desired_mode": "vpn"})
    monkeypatch.setattr(apply_orchestrator, "_load_subjects_with_overrides", lambda **_kwargs: [])
    monkeypatch.setattr(apply_orchestrator, "_load_user_override_map", lambda: {})
    monkeypatch.setattr(apply_orchestrator, "_load_server_override_map", lambda: {})
    monkeypatch.setattr(jobs, "create_job", lambda *_args, **_kwargs: {"job_id": "test-job"})
    job_events = []
    monkeypatch.setattr(jobs, "mark_job_running", lambda job_id: job_events.append(("running", job_id)))
    monkeypatch.setattr(jobs, "mark_job_failed", lambda *args, **kwargs: job_events.append(("failed", args, kwargs)))
    monkeypatch.setattr(jobs, "mark_job_success", lambda *args, **kwargs: pytest.fail("job must not succeed"))
    monkeypatch.setattr(xray_runtime_state, "_module_state", lambda _name: {"desired_state": "enabled", "lifecycle_mode": "managed"})
    xray_calls = []
    monkeypatch.setattr(xray, "materialize_xray_runtime_bindings", lambda **kwargs: xray_calls.append(kwargs) or {"ok": False})
    pipeline_calls = []
    monkeypatch.setattr(apply_orchestrator, "_run_pipeline_for_state", lambda **kwargs: pipeline_calls.append(kwargs))

    result = provider_recovery._apply_override_under_policy(reentry=False)

    assert result == {"ok": False, "outcome": "failed", "error_code": "PROVIDER_EMERGENCY_APPLY_FAILED"}
    assert xray_calls == [{"requested_by": "watchdog.provider_recovery", "prepare_mihomo_handoff": False}]
    assert pipeline_calls == []
    assert job_events[0] == ("running", "test-job")
    assert job_events[1][0] == "failed"
    assert job_events[1][2]["error_code"] == "PROVIDER_EMERGENCY_APPLY_FAILED"


def test_failed_vpn_reentry_runs_direct_fallback_and_keeps_emergency_marker(monkeypatch):
    pending = {
        "provider_managed": True, "emergency_direct": True, "phase": "emergency_direct",
        "logical_server_id": "logical-provider", "source_ref": "source-provider", "binding_revision": 7,
    }
    state = _install_pending_state(monkeypatch, pending)
    controller = _RecoveryController([{"ok": True}])
    apply_calls = []

    def apply_override(*, reentry):
        apply_calls.append(reentry)
        return {"ok": not reentry, "outcome": "failed" if reentry else "verified"}

    monkeypatch.setattr(provider_recovery, "_apply_override", apply_override)

    result = provider_recovery.try_verified_reentry(controller, timeout_ms=4000, reason="test")

    assert result["ok"] is False
    assert result["effective_override"] == "emergency_direct"
    assert result["fallback_verified"] is True
    assert apply_calls == [True, False]
    assert len([call for call in controller.calls if call[0] == "probe"]) == 1
    assert state["pending"] == pending


def test_failed_direct_fallback_is_reported_as_unverified_and_marker_remains(monkeypatch):
    pending = {
        "provider_managed": True, "emergency_direct": True, "phase": "emergency_direct",
        "logical_server_id": "logical-provider", "source_ref": "source-provider", "binding_revision": 7,
    }
    state = _install_pending_state(monkeypatch, pending)
    controller = _RecoveryController([{"ok": True}])
    apply_results = iter([{"ok": False, "outcome": "failed"}, {"ok": False, "outcome": "failed"}])
    monkeypatch.setattr(provider_recovery, "_apply_override", lambda *, reentry: next(apply_results))

    result = provider_recovery.try_verified_reentry(controller, timeout_ms=4000, reason="test")

    assert result["ok"] is False
    assert result["fallback_verified"] is False
    assert result["effective_override"] == "emergency_direct"
    assert state["pending"]["phase"] == "emergency_direct_unconfirmed"


@pytest.mark.parametrize("direct_path", ["repair", "failed_reentry_fallback"])
def test_direct_apply_incarnation_change_keeps_marker_unconfirmed(monkeypatch, direct_path):
    pending = {"provider_managed": True, "emergency_direct": True,
               "phase": "emergency_direct_unconfirmed" if direct_path == "repair" else "emergency_direct",
               "logical_server_id": "logical-provider", "source_ref": "source-provider", "binding_revision": 7}
    state = _install_pending_state(monkeypatch, pending)
    controller = _RecoveryController([{"ok": True}])
    runtime = {"incarnation": "runtime-a"}

    def capture(ctrl, value):
        return {"pending": dict(value), "active_target_id": ctrl.target,
                "selection_fence": {"revision": 0}, "pool": "pool-a",
                "runtime_incarnation": runtime["incarnation"]}

    def matches(ctrl, snapshot, *, expected_pending=None, expected_target=None):
        expected = expected_pending if expected_pending is not None else snapshot["pending"]
        return (state["pending"] == expected
                and runtime["incarnation"] == snapshot["runtime_incarnation"]
                and (expected_target is None or ctrl.target == expected_target))

    monkeypatch.setattr(provider_recovery, "_capture_recovery_context", capture)
    monkeypatch.setattr(provider_recovery, "_recovery_context_matches", matches)
    apply_calls = []

    def apply_override(*, reentry):
        apply_calls.append(reentry)
        if not reentry:
            runtime["incarnation"] = "runtime-b"
            return {"ok": True, "outcome": "verified"}
        return {"ok": False, "outcome": "failed"}

    monkeypatch.setattr(provider_recovery, "_apply_override", apply_override)
    result = provider_recovery.try_verified_reentry(controller, timeout_ms=3000, reason="direct-incarnation-test")

    assert result["fallback_verified"] is False
    assert result["effective_override"] == "emergency_direct"
    assert state["pending"]["phase"] == "emergency_direct_unconfirmed"
    assert apply_calls == ([False] if direct_path == "repair" else [True, False])
    assert result.get("error_code") == "provider_recovery_stale"


@pytest.mark.parametrize(
    ("target", "probes", "apply_results", "expected_action", "expected_pending"),
    [
        ("logical-provider", [{"ok": True}, {"ok": True}], [True], "verified_vpn_reentry", None),
        ("logical-provider", [{"ok": True}, {"ok": False}], [True, True], "none", "emergency_direct"),
        ("other-logical", [{"ok": True}], [True, True], "none", "emergency_direct"),
        ("logical-provider", [{"ok": False}], [], "none", "emergency_direct"),
    ],
)
def test_reentry_requires_pre_and_post_probe_and_exact_saved_target(
    monkeypatch, target, probes, apply_results, expected_action, expected_pending,
):
    pending = {
        "provider_managed": True, "emergency_direct": True, "phase": "emergency_direct",
        "logical_server_id": "logical-provider", "source_ref": "source-provider", "binding_revision": 7,
    }
    state = _install_pending_state(monkeypatch, pending)
    controller = _RecoveryController(probes, target=target)
    results = iter(apply_results)
    apply_calls = []
    monkeypatch.setattr(provider_recovery, "_apply_override", lambda *, reentry: (
        apply_calls.append(reentry) or {"ok": next(results), "outcome": "verified"}
    ))
    reset_calls = []
    monkeypatch.setattr(
        "fwrouter_api.services.watchdog_failure_state.reset_traffic_failure_candidate",
        lambda: reset_calls.append(True),
    )

    result = provider_recovery.try_verified_reentry(controller, timeout_ms=4000, reason="test")

    assert result["action"] == expected_action
    assert bool([call for call in controller.calls if call[0] == "probe"]) == (target == "logical-provider")
    if expected_pending:
        assert state["pending"]["phase"] == expected_pending
    else:
        assert result["effective_override"] is None
        assert reset_calls == [True]
    expected_apply_calls = (
        [True] if expected_action == "verified_vpn_reentry" else
        [True, False] if target == "logical-provider" and probes[0].get("ok") and apply_results else []
    )
    assert apply_calls == expected_apply_calls


@pytest.mark.parametrize("locale", ["ru", "en"])
@pytest.mark.parametrize("code", ["provider_operation_verified", "provider_operation_unconfirmed", "provider_emergency_direct", "provider_vpn_reentry"])
def test_provider_events_use_shared_localization(code, locale):
    from fwrouter_api.services.ui_text import _ui_text_title, _ui_text_reason
    assert _ui_text_title("log.event", code, locale=locale)
    assert _ui_text_reason("log.event", code, locale=locale)


@pytest.mark.parametrize("locale", ["ru", "en"])
@pytest.mark.parametrize("code", [
    "provider_api_timeout", "provider_api_unreachable", "provider_api_5xx",
    "provider_api_rate_limited", "provider_response_unknown",
    "provider_member_confirmed_down", "switch_not_attempted",
    "switch_outcome_unconfirmed", "local_apply_failed", "readback_failed",
    "provider_recovery_stale",
])
def test_typed_recovery_outcomes_have_localized_reasons(code, locale):
    from fwrouter_api.services.ui_text import _ui_text_reason
    assert _ui_text_reason("error.code", code, locale=locale)


def test_unconfirmed_direct_is_repaired_before_reentry_probe(monkeypatch):
    state = _install_pending_state(monkeypatch, {"provider_managed": True, "emergency_direct": True,
                                                 "phase": "emergency_direct_unconfirmed",
                                                 "logical_server_id": "logical-provider"})
    controller = _RecoveryController([])
    monkeypatch.setattr(provider_recovery, "_apply_override", lambda *, reentry: {"ok": False})
    result = provider_recovery.try_verified_reentry(controller, timeout_ms=4000, reason="test")
    assert result["fallback_verified"] is False
    assert controller.calls == []
    assert state["pending"]["phase"] == "emergency_direct_unconfirmed"


def test_provider_recovery_backoff_survives_next_watchdog_cycle(monkeypatch):
    binding = _provider_binding()
    _install_pending_state(monkeypatch, {
        "source_ref": binding["source_ref"], "binding_revision": binding["binding_revision"],
        "provider_confirmation": 1, "traffic_decision_id": "d1", "not_before": 200,
    })
    monkeypatch.setattr(provider_managed, "binding_for_logical", lambda _logical: binding)
    monkeypatch.setattr(provider_recovery.time, "time", lambda: 100)
    monkeypatch.setattr(provider_managed, "execute_provider_operation", lambda *_args, **_kwargs: pytest.fail("backoff forbids execution"))
    controller = _RecoveryController([])
    result = provider_recovery.confirmed_provider_recovery(
        logical_server_id="logical-provider", path_key="lan", decision_id="d2",
        controller=controller, timeout_ms=3000, allow_switch=True,
    )
    assert result["outcome"] == "deferred"
    assert result["not_before"] == 200
    assert controller.calls == []


def test_recovery_pending_compare_and_set_rejects_competing_incident(monkeypatch):
    from fwrouter_api.services import watchdog_failure_state as failure_state
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE watchdog_state (id INTEGER PRIMARY KEY, failure_candidate_json TEXT, path_key TEXT, last_processed_decision_id TEXT, updated_at TEXT)")
    @contextmanager
    def session():
        yield conn
    monkeypatch.setattr(failure_state, "db_session", session)
    initial = {"kind": "traffic_recovery", "recovery_pending": {"phase": "provider_recovery", "traffic_decision_id": "d1"}}
    conn.execute("INSERT INTO watchdog_state(id, failure_candidate_json) VALUES (1, ?)", (json.dumps(initial, sort_keys=True),))
    expected = dict(initial["recovery_pending"])
    replacement = {"kind": "traffic_recovery", "recovery_pending": {"phase": "provider_recovery", "traffic_decision_id": "d2"}}
    conn.execute("UPDATE watchdog_state SET failure_candidate_json=? WHERE id=1", (json.dumps(replacement, sort_keys=True),))

    assert failure_state.compare_and_set_recovery_pending(expected, None) is False
    assert json.loads(conn.execute("SELECT failure_candidate_json FROM watchdog_state WHERE id=1").fetchone()[0]) == replacement


def test_recovery_terminal_cas_resets_only_confirmed_incident(monkeypatch):
    from fwrouter_api.services import watchdog_failure_state as failure_state
    from fwrouter_api.services import watchdog_runtime_state as runtime_state
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE watchdog_state (id INTEGER PRIMARY KEY, failure_candidate_json TEXT, path_key TEXT, last_processed_decision_id TEXT, updated_at TEXT)")
    @contextmanager
    def session():
        yield conn
    monkeypatch.setattr(failure_state, "db_session", session)
    monkeypatch.setattr(runtime_state, "db_session", session)
    expected = {"phase": "reentry_verification", "reentry_operation_id": "claim-a"}
    candidate = {"kind": "active_quality_degraded", "quality_incident": "preserve",
                 "recovery_pending": expected}
    conn.execute("INSERT INTO watchdog_state(id, path_key, failure_candidate_json, last_processed_decision_id) VALUES (1, 'lan', ?, 'decision-a')",
                 (json.dumps(candidate, sort_keys=True),))

    assert failure_state.complete_recovery_pending({**expected, "reentry_operation_id": "claim-b"}) is False
    row = conn.execute("SELECT path_key, failure_candidate_json, last_processed_decision_id FROM watchdog_state WHERE id=1").fetchone()
    assert row["path_key"] == "lan" and row["last_processed_decision_id"] == "decision-a"
    assert json.loads(row["failure_candidate_json"]) == candidate
    assert failure_state.complete_recovery_pending(expected) is True
    row = conn.execute("SELECT path_key, failure_candidate_json, last_processed_decision_id FROM watchdog_state WHERE id=1").fetchone()
    assert row["path_key"] is None and row["last_processed_decision_id"] is None
    assert json.loads(row["failure_candidate_json"]) == {"kind": "active_quality_degraded", "quality_incident": "preserve"}


def test_watchdog_runtime_read_is_read_only_and_writer_initializes_row(monkeypatch):
    from fwrouter_api.services import watchdog_runtime_state as runtime_state

    raw = sqlite3.connect(":memory:")
    raw.row_factory = sqlite3.Row
    raw.execute("""
        CREATE TABLE watchdog_state (
            id INTEGER PRIMARY KEY, path_key TEXT, failure_candidate_json TEXT,
            last_processed_decision_id TEXT, last_successful_failover_at TEXT,
            failover_path_key TEXT, previous_target_id TEXT, selected_target_id TEXT,
            cooldown_until TEXT, last_idle_probe_at TEXT,
            last_idle_probe_server_id TEXT, last_idle_probe_status TEXT, updated_at TEXT
        )
    """)
    statements: list[str] = []

    class TrackedConnection:
        def execute(self, sql, parameters=()):
            statements.append(sql.strip().split(None, 1)[0].upper())
            return raw.execute(sql, parameters)

    @contextmanager
    def session():
        yield TrackedConnection()

    monkeypatch.setattr(runtime_state, "db_session", session)
    empty = runtime_state.load_watchdog_runtime_state()
    assert empty == runtime_state.empty_watchdog_runtime_state()
    assert statements == ["SELECT"]
    assert raw.execute("SELECT COUNT(*) FROM watchdog_state").fetchone()[0] == 0

    updated = runtime_state.update_watchdog_runtime_state(path_key="lan")
    assert updated["path_key"] == "lan"
    assert "INSERT" in statements and "UPDATE" in statements
    before_read = len(statements)
    loaded = runtime_state.load_watchdog_runtime_state()
    assert loaded == updated
    assert statements[before_read:] == ["SELECT"]


@pytest.mark.parametrize(("code", "status", "expected"), [
    ("PROVIDER_TIMEOUT", None, "provider_api_timeout"),
    ("RATE_LIMITED", 429, "provider_api_rate_limited"),
    ("PROVIDER_TRANSPORT_ERROR", 503, "provider_api_5xx"),
    ("PROVIDER_OTHER_FAILURE", 503, "provider_api_5xx"),
    ("PROVIDER_TRANSPORT_ERROR", None, "provider_api_unreachable"),
    ("PROVIDER_INVALID_RESPONSE", None, "provider_response_unknown"),
])
def test_provider_api_failure_codes_are_never_remote_down(code, status, expected):
    from fwrouter_api.adapters.provider_base import recovery_evidence_code
    from fwrouter_api.services.provider_adapters import ProviderError
    assert recovery_evidence_code(ProviderError(code, status_code=status)) == expected


def test_reentry_runs_probes_outside_real_writer_guard(monkeypatch, tmp_path):
    from pathlib import Path
    from types import SimpleNamespace
    from fwrouter_api.adapters import xray_common
    real_guard = xray_common.xray_writer_guard
    state = _install_pending_state(monkeypatch, {
        "provider_managed": True, "emergency_direct": True, "phase": "emergency_direct",
        "logical_server_id": "logical-provider",
    })
    monkeypatch.setattr(xray_common, "get_settings", lambda: SimpleNamespace(
        paths=SimpleNamespace(run_dir=Path(tmp_path)),
    ))
    monkeypatch.setattr(xray_common, "xray_writer_guard", real_guard)
    observations = []
    class GuardController(_RecoveryController):
        def probe(self, **kwargs):
            observations.append(("probe", xray_common.xray_writer_guard_is_held()))
            return super().probe(**kwargs)
    controller = GuardController([{"ok": True}, {"ok": True}])
    monkeypatch.setattr(provider_recovery, "_apply_override", lambda *, reentry: (
        observations.append(("apply", xray_common.xray_writer_guard_is_held())) or {"ok": True}
    ))

    result = provider_recovery.try_verified_reentry(controller, timeout_ms=3000, reason="test")

    assert result["action"] == "verified_vpn_reentry"
    assert observations == [("probe", False), ("apply", True), ("probe", False)]
    assert state["pending"] is None


def test_concurrent_reentry_claim_allows_one_vpn_apply(monkeypatch):
    from threading import Event, Lock, Thread
    pending = {"provider_managed": True, "emergency_direct": True, "phase": "emergency_direct",
               "logical_server_id": "logical-provider"}
    state = _install_pending_state(monkeypatch, pending)
    entered_post_probe = Event()
    release_post_probe = Event()
    counter_lock = Lock()
    probes = 0
    applies = []
    class ConcurrentController(_RecoveryController):
        def probe(self, **kwargs):
            nonlocal probes
            with counter_lock:
                probes += 1
                call = probes
            if call == 2:
                entered_post_probe.set()
                assert release_post_probe.wait(3)
            return {"ok": True}
    controller = ConcurrentController([])
    monkeypatch.setattr(provider_recovery, "_apply_override", lambda *, reentry: applies.append(reentry) or {"ok": True})
    results = []
    first = Thread(target=lambda: results.append(provider_recovery.try_verified_reentry(
        controller, timeout_ms=3000, reason="concurrent-test")))
    first.start()
    assert entered_post_probe.wait(3)
    second = provider_recovery.try_verified_reentry(controller, timeout_ms=3000, reason="concurrent-test")
    release_post_probe.set()
    first.join(timeout=3)

    assert not first.is_alive()
    assert second.get("outcome") == "deferred"
    assert applies == [True]
    assert results[0]["action"] == "verified_vpn_reentry"
    assert state["pending"] is None


def test_expired_reentry_claim_requires_direct_repair_before_probe(monkeypatch):
    pending = {"provider_managed": True, "emergency_direct": True,
               "phase": "reentry_verification", "logical_server_id": "logical-provider",
               "reentry_operation_id": "dead-process-claim", "reentry_started_at": 1}
    state = _install_pending_state(monkeypatch, pending)
    monkeypatch.setattr(provider_recovery.time, "time", lambda: 1000)
    controller = _RecoveryController([])
    applied = []
    monkeypatch.setattr(provider_recovery, "_apply_override", lambda *, reentry: applied.append(reentry) or {"ok": False})

    result = provider_recovery.try_verified_reentry(controller, timeout_ms=1000, reason="restart-test")

    assert result["status"] == "emergency_direct_unconfirmed"
    assert state["pending"]["phase"] == "emergency_direct_unconfirmed"
    assert applied == [False]
    assert controller.calls == []


def test_real_recovery_snapshot_fences_revision_exclusive_runtime_and_pending(monkeypatch, tmp_path):
    from pathlib import Path
    from types import SimpleNamespace
    from fwrouter_api.core.config import get_settings
    from fwrouter_api.db.connection import db_session, initialize_database
    from fwrouter_api.db.provider_managed import save_binding
    from fwrouter_api.services import vpn_auto_selection_state
    from fwrouter_api.services.server_state import ensure_routing_global_state
    from fwrouter_api.adapters import xray_common

    monkeypatch.setenv("FWROUTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("FWROUTER_MAINTENANCE_SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("FWROUTER_WATCHDOG_SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("FWROUTER_RUNTIME_CONVERGENCE_SCHEDULER_ENABLED", "false")
    get_settings.cache_clear()
    initialize_database()
    ensure_routing_global_state()
    source_ref = "recovery-source"
    with db_session() as conn:
        conn.execute("INSERT INTO servers(server_id,server_name,provider_name,inventory_state,raw_json) VALUES('logical-provider','Logical provider','pytest','active','{}')")
        conn.execute("INSERT INTO server_preferences(server_id,vpn_auto,vpn_auto_priority,manually_deleted_at) VALUES('logical-provider',1,0,'')")
        conn.execute("INSERT INTO logical_server_topology(logical_server_id,topology_kind,selection_policy) VALUES('logical-provider','logical_multi','fallback')")
        binding = save_binding(conn, source_ref, "stealthsurf", 1234, "logical-provider", "hysteria2", True)
        conn.execute("UPDATE provider_bindings SET current_member_id='member-1',current_location_id='26',observed_protocol='hysteria2',observed_at=100 WHERE source_ref=?", (source_ref,))
        vpn_auto_selection_state.read_selection_fence(conn)
    pending = {"provider_managed": True, "source_ref": source_ref,
               "binding_revision": binding["binding_revision"], "logical_server_id": "logical-provider",
               "phase": "emergency_direct"}
    pending_state = {"value": pending}
    monkeypatch.setattr(provider_recovery, "get_recovery_pending", lambda: pending_state["value"])
    monkeypatch.setattr(provider_recovery, "_runtime_incarnation", lambda: "runtime-a")
    from fwrouter_api.services import selector
    from types import SimpleNamespace
    from fwrouter_api.services.provider_managed import provider_runtime_member_id
    runtime_member = provider_runtime_member_id({"provider_id": "stealthsurf", "source_ref": source_ref}, "member-1", "hysteria2")
    with db_session() as conn:
        conn.execute("INSERT INTO logical_server_members(logical_server_id,member_id,member_runtime_name,is_active,member_config_json,transport_fingerprint,member_order) VALUES(?,?,?,1,'{}','fingerprint',0)",
                     ("logical-provider", runtime_member, "member-runtime-1"))
    monkeypatch.setattr(selector, "_active_selector_runtime", lambda: (None, SimpleNamespace(
        get_recovery_selection_snapshot=lambda *_args, **_kwargs: {
            "active_target": "logical-provider", "effective_member_runtime_identity": "member-runtime-1", "ok": True},
    )))
    monkeypatch.setattr("fwrouter_api.services.logical_topology.get_logical_runtime_name", lambda _logical: "logical-provider")
    controller = _RecoveryController([], target="logical-provider")
    assert provider_recovery._capture_recovery_context(
        _RecoveryController([], target="other-logical"), pending) is None
    snapshot = provider_recovery._capture_recovery_context(controller, pending)
    assert snapshot is not None
    monkeypatch.setattr(xray_common, "get_settings", lambda: SimpleNamespace(paths=SimpleNamespace(run_dir=Path(tmp_path / "run"))))

    with xray_common.xray_writer_guard(timeout_seconds=2):
        assert provider_recovery._recovery_context_matches(controller, snapshot)
        with db_session() as conn:
            vpn_auto_selection_state.advance_selection_revision(conn)
        assert not provider_recovery._recovery_context_matches(controller, snapshot)

    snapshot = provider_recovery._capture_recovery_context(controller, pending)
    assert snapshot is not None
    with db_session() as conn:
        conn.execute("INSERT OR REPLACE INTO settings(key,value_json,updated_at) VALUES('vpn_auto_exclusive_source_ref','{\"source_ref\":\"other\"}',0)")
    with xray_common.xray_writer_guard(timeout_seconds=2):
        assert not provider_recovery._recovery_context_matches(controller, snapshot)

    with db_session() as conn:
        conn.execute("DELETE FROM settings WHERE key='vpn_auto_exclusive_source_ref'")
    snapshot = provider_recovery._capture_recovery_context(controller, pending)
    monkeypatch.setattr(provider_recovery, "_runtime_incarnation", lambda: "runtime-b")
    with xray_common.xray_writer_guard(timeout_seconds=2):
        assert not provider_recovery._recovery_context_matches(controller, snapshot)

    monkeypatch.setattr(provider_recovery, "_runtime_incarnation", lambda: "runtime-a")
    snapshot = provider_recovery._capture_recovery_context(controller, pending)
    pending_state["value"] = {**pending, "phase": "newer-incident"}
    with xray_common.xray_writer_guard(timeout_seconds=2):
        assert not provider_recovery._recovery_context_matches(controller, snapshot)


@pytest.mark.parametrize(("provider_error", "expected"), [
    ("PROVIDER_TIMEOUT", "provider_api_timeout"),
    ("PROVIDER_TRANSPORT_503", "provider_api_5xx"),
    ("RATE_LIMITED", "provider_api_rate_limited"),
])
def test_phase_two_api_failure_preserves_last_good_and_never_switches(monkeypatch, provider_error, expected):
    from fwrouter_api.adapters.provider_base import ProviderError
    binding = _provider_binding()
    state = _install_pending_state(monkeypatch, {
        "provider_managed": True, "source_ref": binding["source_ref"],
        "binding_revision": binding["binding_revision"], "provider_confirmation": 1,
        "traffic_decision_id": "d1", "logical_server_id": binding["logical_server_id"],
    })
    adapter = _RecoveryAdapter()
    def fail_stats(*_args, **_kwargs):
        code, status = {
            "PROVIDER_TIMEOUT": ("PROVIDER_TIMEOUT", None),
            "PROVIDER_TRANSPORT_503": ("PROVIDER_TRANSPORT_ERROR", 503),
            "RATE_LIMITED": ("RATE_LIMITED", 429),
        }[provider_error]
        raise ProviderError(code, status_code=status)
    adapter.get_server_stats = fail_stats
    calls = []
    monkeypatch.setattr(provider_managed, "binding_for_logical", lambda _logical: binding)
    monkeypatch.setattr(provider_managed, "execute_provider_operation", lambda *args, **kwargs: calls.append((args, kwargs)) or {"ok": True})
    monkeypatch.setattr(provider_managed, "provider_operation_reservation", lambda: nullcontext())
    monkeypatch.setattr(provider_managed, "db_session", lambda: nullcontext(object()))
    monkeypatch.setattr(provider_managed.store, "update_observation", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("fwrouter_api.services.provider_adapters.provider_adapter", lambda *_args, **_kwargs: adapter)

    result = provider_recovery.confirmed_provider_recovery(
        logical_server_id=binding["logical_server_id"], path_key="lan", decision_id="d2",
        controller=_RecoveryController([]), timeout_ms=3000, allow_switch=True,
    )

    assert result["error_code"] == expected
    assert result["outcome"] == "unknown"
    assert result["switch_attempted"] is False
    assert calls == []
    assert state["pending"]["provider_confirmation"] == 2



def test_phase_three_healthy_probe_keeps_ambiguous_switch_marker(monkeypatch):
    binding = _provider_binding()
    pending = {"provider_managed": True, "emergency_direct": False,
               "source_ref": binding["source_ref"], "binding_revision": binding["binding_revision"],
               "logical_server_id": binding["logical_server_id"], "phase": "provider_recovery",
               "provider_confirmation": 2, "traffic_decision_id": "old-decision",
               "switch_outcome_unconfirmed": True, "switch_confirmation_attempted": True}
    state = _install_pending_state(monkeypatch, pending)
    monkeypatch.setattr(provider_managed, "binding_for_logical", lambda _logical: binding)
    reset_calls = []
    monkeypatch.setattr("fwrouter_api.services.watchdog_failure_state.reset_traffic_failure_candidate",
                        lambda: reset_calls.append(True))
    result = provider_recovery.confirmed_provider_recovery(
        logical_server_id=binding["logical_server_id"], path_key="lan", decision_id="new-decision",
        controller=_RecoveryController([{"ok": True}]), timeout_ms=3000, allow_switch=True)
    assert result["status"] == "local_connectivity_restored_but_mutation_unconfirmed"
    assert result["error_code"] == "switch_outcome_unconfirmed"
    assert result["effective_override"] is None
    assert state["pending"]["switch_outcome_unconfirmed"] is True
    assert state["pending"]["switch_confirmation_attempted"] is True
    assert reset_calls == []

def test_local_apply_failure_is_not_recorded_as_provider_api_failure(monkeypatch):
    binding = _provider_binding()
    state = _install_pending_state(monkeypatch, {
        "provider_managed": True, "source_ref": binding["source_ref"],
        "binding_revision": binding["binding_revision"], "provider_confirmation": 1,
        "traffic_decision_id": "d1", "logical_server_id": binding["logical_server_id"],
    })
    adapter = _RecoveryAdapter(status="down")
    monkeypatch.setattr(provider_managed, "binding_for_logical", lambda _logical: binding)
    monkeypatch.setattr(provider_managed, "provider_candidates", lambda _source: [{"member_id": "candidate-2"}])
    monkeypatch.setattr(provider_managed, "execute_provider_operation", lambda *_args, **_kwargs: {
        "ok": False, "outcome": "unconfirmed", "error_code": "PROVIDER_LOCAL_VERIFICATION_FAILED",
        "mutation_attempted": True, "requested_member_id": "candidate-2"})
    monkeypatch.setattr(provider_managed, "provider_operation_reservation", lambda: nullcontext())
    monkeypatch.setattr(provider_managed, "db_session", lambda: nullcontext(object()))
    monkeypatch.setattr(provider_managed.store, "update_observation", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("fwrouter_api.services.provider_adapters.provider_adapter", lambda *_args, **_kwargs: adapter)
    monkeypatch.setattr("fwrouter_api.services.selector._select_candidate_with_priority", lambda candidates: (candidates[0], 0))

    result = provider_recovery.confirmed_provider_recovery(
        logical_server_id=binding["logical_server_id"], path_key="lan", decision_id="d2",
        controller=_RecoveryController([]), timeout_ms=3000, allow_switch=True)

    assert result["error_code"] == "switch_outcome_unconfirmed"
    assert result["switch_confirmation_reason"] == "readback_failed"
    assert "provider_api_error_code" not in result
    assert "switch_api_error_code" not in state["pending"]


def test_phase_two_up_local_apply_failure_does_not_claim_ambiguous_switch(monkeypatch):
    binding = _provider_binding()
    state = _install_pending_state(monkeypatch, {
        "provider_managed": True, "source_ref": binding["source_ref"],
        "binding_revision": binding["binding_revision"], "provider_confirmation": 1,
        "traffic_decision_id": "d1", "logical_server_id": binding["logical_server_id"],
    })
    adapter = _RecoveryAdapter(status="up")
    execute_calls = []
    monkeypatch.setattr(provider_managed, "binding_for_logical", lambda _logical: binding)
    monkeypatch.setattr(provider_managed, "execute_provider_operation", lambda *args, **kwargs: (
        execute_calls.append((args, kwargs)) or {
            "ok": False, "outcome": "unconfirmed", "error_code": "PROVIDER_LOCAL_VERIFICATION_FAILED",
            "mutation_attempted": False,
        }
    ))
    monkeypatch.setattr(provider_managed, "provider_operation_reservation", lambda: nullcontext())
    monkeypatch.setattr(provider_managed, "db_session", lambda: nullcontext(object()))
    monkeypatch.setattr(provider_managed.store, "update_observation", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("fwrouter_api.services.provider_adapters.provider_adapter", lambda *_args, **_kwargs: adapter)

    result = provider_recovery.confirmed_provider_recovery(
        logical_server_id=binding["logical_server_id"], path_key="lan", decision_id="d2",
        controller=_RecoveryController([]), timeout_ms=3000, allow_switch=True)

    assert execute_calls and execute_calls[0][0][1] == "recovery_refresh"
    assert result["error_code"] == "readback_failed"
    assert result["outcome"] == "unconfirmed"
    assert result.get("switch_attempted") is None
    assert state["pending"].get("switch_outcome_unconfirmed") is None
    assert state["pending"].get("switch_confirmation_attempted") is None
    assert "switch_api_error_code" not in state["pending"]

def test_expired_down_evidence_after_candidate_probe_blocks_switch(monkeypatch):
    binding = _provider_binding()
    state = _install_pending_state(monkeypatch, {
        "provider_managed": True, "source_ref": binding["source_ref"],
        "binding_revision": binding["binding_revision"], "provider_confirmation": 1,
        "traffic_decision_id": "d1", "logical_server_id": binding["logical_server_id"],
    })
    adapter = _RecoveryAdapter(status="down")
    candidate = {"member_id": "candidate-2"}
    calls = []
    monkeypatch.setattr(provider_managed, "binding_for_logical", lambda _logical: binding)
    monkeypatch.setattr(provider_managed, "provider_candidates", lambda _source: [candidate])
    monkeypatch.setattr(provider_managed, "execute_provider_operation", lambda *args, **kwargs: calls.append((args, kwargs)) or {"ok": True})
    monkeypatch.setattr(provider_managed, "provider_operation_reservation", lambda: nullcontext())
    monkeypatch.setattr(provider_managed, "db_session", lambda: nullcontext(object()))
    monkeypatch.setattr(provider_managed.store, "update_observation", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(provider_recovery, "_provider_down_evidence_is_fresh", lambda *_args, **_kwargs: False)
    monkeypatch.setattr("fwrouter_api.services.provider_adapters.provider_adapter", lambda *_args, **_kwargs: adapter)
    monkeypatch.setattr("fwrouter_api.services.selector._select_candidate_with_priority", lambda candidates: (candidates[0], 0))

    result = provider_recovery.confirmed_provider_recovery(
        logical_server_id=binding["logical_server_id"], path_key="lan", decision_id="d2",
        controller=_RecoveryController([]), timeout_ms=3000, allow_switch=True)

    assert result["error_code"] == "provider_evidence_stale"
    assert result["evidence_code"] == "switch_not_attempted"
    assert result["switch_attempted"] is False
    assert calls == []
    assert state["pending"]["last_outcome"] == "deferred"

def test_busy_provider_reservation_does_not_consume_read_confirmation(monkeypatch):
    from contextlib import contextmanager
    from fwrouter_api.adapters.provider_base import ProviderError
    pending = {"provider_managed": True, "emergency_direct": True,
               "source_ref": "source-provider", "binding_revision": 7,
               "logical_server_id": "logical-provider", "phase": "provider_recovery",
               "switch_outcome_unconfirmed": True, "switch_requested_member_id": "902",
               "switch_previous_member_id": "server-current"}
    state = _install_pending_state(monkeypatch, pending)
    binding = _provider_binding()
    adapter = _RecoveryAdapter()
    def configs(*_args, **_kwargs):
        adapter.calls.append(("get_configs",))
        return [{"id": 1234, "server_id": "server-current", "location_id": 26,
                 "protocol": "hysteria2", "connection_url": "redacted"}]
    adapter.get_configs = configs
    attempts = {"busy": True}
    @contextmanager
    def reservation():
        if attempts["busy"]:
            attempts["busy"] = False
            raise ProviderError("PROVIDER_OPERATION_BUSY")
        yield
    monkeypatch.setattr(provider_managed, "provider_operation_reservation", reservation)
    monkeypatch.setattr("fwrouter_api.services.provider_adapters.provider_adapter", lambda *_args, **_kwargs: adapter)

    deferred = provider_recovery._confirm_unconfirmed_switch(binding, pending, _RecoveryController([]))
    assert deferred["outcome"] == "deferred"
    assert state["pending"] == pending
    assert adapter.calls == []

    confirmed_unknown = provider_recovery._confirm_unconfirmed_switch(binding, pending, _RecoveryController([]))
    assert confirmed_unknown["outcome"] == "unconfirmed"
    assert state["pending"]["switch_confirmation_attempted"] is True
    assert adapter.calls == [("get_configs",)]

def test_ambiguous_switch_confirmation_reuses_read_budget_and_owned_inventory_receipt(monkeypatch):
    pending = {"provider_managed": True, "emergency_direct": True,
               "source_ref": "source-provider", "binding_revision": 7,
               "logical_server_id": "logical-provider", "phase": "provider_recovery",
               "switch_outcome_unconfirmed": True, "switch_requested_member_id": "901",
               "switch_previous_member_id": "900", "traffic_decision_id": "d2"}
    state = _install_pending_state(monkeypatch, pending)
    binding = {**_provider_binding(), "current_member_id": "900", "current_location_id": 26}
    before = {"pending": pending, "selection_fence": {"revision": 10}, "pool": "pool-before",
              "runtime_incarnation": "runtime-a", "desired_mode": "vpn", "exclusive": "",
              "credential_fingerprint": "credential-fp", "active_target_id": "logical-provider",
              "path_key": "lan", "eligible": True,
              "binding": ("source-provider", 7, "stealthsurf", 1234, "logical-provider", 1,
                          "hysteria2", "900", "26", "hysteria2", 100.0, "900", "hysteria2", 7)}
    after = {**before, "pending": {**pending, "switch_confirmation_attempted": True},
             "selection_fence": {"revision": 11}, "pool": "pool-after",
             "binding": ("source-provider", 7, "stealthsurf", 1234, "logical-provider", 1,
                         "hysteria2", "901", "26", "hysteria2", 200.0, "901", "hysteria2", 7)}
    captures = iter([before, after])
    monkeypatch.setattr(provider_recovery, "_capture_recovery_context", lambda *_args: next(captures))
    monkeypatch.setattr(provider_recovery, "_recovery_context_matches", lambda _controller, _snapshot, *, expected_pending=None, **_kwargs:
                        state["pending"] == (expected_pending if expected_pending is not None else _snapshot["pending"]))
    from fwrouter_api.services.provider_adapters import RequestBudget
    class Adapter(_RecoveryAdapter):
        def get_configs(self, config_id, *, budget=None, max_age_s=None):
            self.calls.append(("get_configs", config_id, budget, max_age_s))
            return [{"id": 1234, "server_id": 901, "location_id": 26,
                     "protocol": "hysteria2", "connection_url": "redacted-test-material"}]
    adapter = Adapter()
    monkeypatch.setattr(provider_managed, "provider_operation_reservation", lambda: nullcontext())
    monkeypatch.setattr("fwrouter_api.services.provider_adapters.provider_adapter", lambda *_args, **_kwargs: adapter)
    execution = []
    receipt = {"operation_id": "owned-refresh", "source_ref": "source-provider", "binding_revision": 7,
               "member_id": "901", "location_id": "26", "protocol": "hysteria2",
               "selection_revision": 11, "pool_signature": "pool-after", "runtime_incarnation": "runtime-a"}
    monkeypatch.setattr(provider_managed, "execute_provider_operation", lambda *args, **kwargs: (
        execution.append((args, kwargs)) or {"ok": True, "runtime_verified": True,
                                            "operation_id": "owned-refresh", "_owned_receipt": receipt}
    ))

    result = provider_recovery._confirm_unconfirmed_switch(binding, pending, _RecoveryController([]))

    assert result["status"] == "provider_switch_confirmed"
    assert state["pending"]["switch_confirmation_result"] == "requested_member"
    assert "switch_outcome_unconfirmed" not in state["pending"]
    assert len(adapter.calls) == 1 and adapter.calls[0][0] == "get_configs"
    args = execution[0][1]
    assert args["_adapter"] is adapter
    assert args["_budget"] is adapter.calls[0][2]
    assert isinstance(args["_budget"], RequestBudget)
    assert args["_budget"].max_requests == 2 and args["_budget"].deadline_seconds == 15


@pytest.mark.parametrize(("probe_phase", "mutation"), [
    ("pre", "selection_revision"), ("pre", "exclusive"), ("pre", "incarnation"), ("pre", "binding"),
    ("post", "selection_revision"), ("post", "exclusive"), ("post", "incarnation"), ("post", "binding"),
])
def test_real_reentry_orchestration_defers_after_probe_fence_change(monkeypatch, tmp_path, probe_phase, mutation):
    from pathlib import Path
    from types import SimpleNamespace
    from fwrouter_api.core.config import get_settings
    from fwrouter_api.db.connection import db_session, initialize_database
    from fwrouter_api.db.provider_managed import save_binding
    from fwrouter_api.services import selector, watchdog_failure_state
    from fwrouter_api.services.provider_managed import provider_runtime_member_id
    from fwrouter_api.services.server_state import ensure_routing_global_state
    from fwrouter_api.services.vpn_auto_selection_state import advance_selection_revision
    from fwrouter_api.adapters import xray_common

    monkeypatch.setenv("FWROUTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("FWROUTER_MAINTENANCE_SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("FWROUTER_WATCHDOG_SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("FWROUTER_RUNTIME_CONVERGENCE_SCHEDULER_ENABLED", "false")
    get_settings.cache_clear()
    initialize_database()
    ensure_routing_global_state()
    source_ref = "reentry-probe-source"
    with db_session() as conn:
        conn.execute("INSERT INTO servers(server_id,server_name,provider_name,inventory_state,raw_json) VALUES('logical-provider','logical-provider','pytest','active','{}')")
        conn.execute("INSERT INTO server_preferences(server_id,vpn_auto,vpn_auto_priority,manually_deleted_at) VALUES('logical-provider',1,0,'')")
        conn.execute("UPDATE routing_global_state SET desired_mode='vpn',applied_mode='direct' WHERE id=1")
        conn.execute("INSERT INTO logical_server_topology(logical_server_id,topology_kind,selection_policy) VALUES('logical-provider','logical_multi','fallback')")
        binding = save_binding(conn, source_ref, "stealthsurf", 1234, "logical-provider", "hysteria2", True)
        conn.execute("UPDATE provider_bindings SET current_member_id='member-1',current_location_id='26',observed_protocol='hysteria2',observed_at=100,applied_member_id='member-1',applied_protocol='hysteria2',applied_revision=? WHERE source_ref=?", (binding["binding_revision"], source_ref))
        canonical_member = provider_runtime_member_id({"provider_id": "stealthsurf", "source_ref": source_ref}, "member-1", "hysteria2")
        conn.execute("INSERT INTO logical_server_members(logical_server_id,member_id,member_runtime_name,member_config_json,transport_fingerprint,member_order,is_active) VALUES('logical-provider',?,'member-runtime','{}','fingerprint',0,1)", (canonical_member,))
        conn.execute("INSERT OR REPLACE INTO watchdog_state(id,path_key,failure_candidate_json,last_processed_decision_id) VALUES(1,'lan',?,NULL)", (json.dumps({"kind":"traffic_recovery","path_key":"lan"}, sort_keys=True),))
    pending = {"provider_managed": True, "emergency_direct": True, "phase": "emergency_direct",
               "source_ref": source_ref, "binding_revision": binding["binding_revision"],
               "logical_server_id": "logical-provider", "path_key": "lan"}
    assert watchdog_failure_state.compare_and_set_recovery_pending(None, pending)

    incarnation = {"value": "runtime-a"}
    monkeypatch.setattr(provider_recovery, "_runtime_incarnation", lambda: incarnation["value"])
    readback_guard_states = []
    def get_selection_readback(*_args, **_kwargs):
        readback_guard_states.append(xray_common.xray_writer_guard_is_held())
        return {"active_target": "logical-provider",
                "effective_member_runtime_identity": "member-runtime", "ok": True}
    monkeypatch.setattr(selector, "_active_selector_runtime", lambda: (None, SimpleNamespace(
        get_recovery_selection_snapshot=get_selection_readback,
    )))
    monkeypatch.setattr("fwrouter_api.services.logical_topology.get_logical_runtime_name", lambda _logical: "logical-provider")
    monkeypatch.setattr(xray_common, "get_settings", lambda: SimpleNamespace(paths=SimpleNamespace(run_dir=Path(tmp_path / "run"))))

    class MutatingController(_RecoveryController):
        def __init__(self):
            super().__init__([{"ok": True}, {"ok": True}])
            self.probe_index = 0
        def probe(self, **kwargs):
            assert not xray_common.xray_writer_guard_is_held()
            self.probe_index += 1
            if (probe_phase == "pre" and self.probe_index == 1) or (probe_phase == "post" and self.probe_index == 2):
                with db_session() as conn:
                    if mutation == "selection_revision":
                        advance_selection_revision(conn)
                    elif mutation == "exclusive":
                        conn.execute("INSERT OR REPLACE INTO settings(key,value_json,updated_at) VALUES('vpn_auto_exclusive_source_ref','{\"source_ref\":\"other\"}',CURRENT_TIMESTAMP)")
                    elif mutation == "binding":
                        conn.execute("UPDATE provider_bindings SET observed_at=observed_at+1 WHERE source_ref=?", (source_ref,))
                if mutation == "incarnation":
                    incarnation["value"] = "runtime-b"
            return super().probe(**kwargs)

    controller = MutatingController()
    apply_calls = []
    monkeypatch.setattr(provider_recovery, "_apply_override", lambda *, reentry: apply_calls.append(reentry) or {"ok": True})
    result = provider_recovery.try_verified_reentry(controller, timeout_ms=3000, reason="probe-interleaving-test")

    assert result["error_code"] == "provider_recovery_stale"
    assert readback_guard_states and all(readback_guard_states)
    assert apply_calls == ([True] if probe_phase == "post" else [])
    current = watchdog_failure_state.get_recovery_pending()
    assert current is not None
    assert current.get("phase") != "verified_vpn_recovered"
    assert result.get("fallback_verified") is None
    get_settings.cache_clear()


@pytest.mark.parametrize("competing_revision", [False, True])
def test_ambiguous_confirmation_uses_sql_owned_pipeline_receipt(monkeypatch, tmp_path, competing_revision):
    from pathlib import Path
    from types import SimpleNamespace
    from fwrouter_api.core.config import get_settings
    from fwrouter_api.db.connection import db_session, initialize_database
    from fwrouter_api.db.provider_managed import save_binding
    from fwrouter_api.services import selector, subscription_pipeline, watchdog_failure_state
    from fwrouter_api.services.provider_managed import provider_runtime_member_id
    from fwrouter_api.services.server_state import ensure_routing_global_state
    from fwrouter_api.services.vpn_auto_selection_state import advance_selection_revision
    from fwrouter_api.adapters import xray_common

    monkeypatch.setenv("FWROUTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("FWROUTER_MAINTENANCE_SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("FWROUTER_WATCHDOG_SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("FWROUTER_RUNTIME_CONVERGENCE_SCHEDULER_ENABLED", "false")
    get_settings.cache_clear()
    initialize_database()
    ensure_routing_global_state()
    source_ref = "owned-receipt-source"
    with db_session() as conn:
        conn.execute("INSERT INTO servers(server_id,server_name,provider_name,inventory_state,raw_json) VALUES('logical-provider','logical-provider','pytest','active','{}')")
        conn.execute("INSERT INTO server_preferences(server_id,vpn_auto,vpn_auto_priority,manually_deleted_at) VALUES('logical-provider',1,0,'')")
        conn.execute("UPDATE routing_global_state SET desired_mode='vpn' WHERE id=1")
        conn.execute("INSERT INTO logical_server_topology(logical_server_id,topology_kind,selection_policy) VALUES('logical-provider','logical_multi','fallback')")
        binding = save_binding(conn, source_ref, "stealthsurf", 1234, "logical-provider", "hysteria2", True)
        conn.execute("UPDATE provider_bindings SET current_member_id='900',current_location_id='26',observed_protocol='hysteria2',observed_at=100,applied_member_id='900',applied_protocol='hysteria2',applied_revision=? WHERE source_ref=?", (binding["binding_revision"], source_ref))
        for member_id, runtime_name in (("900", "runtime-old"), ("901", "runtime-requested")):
            canonical = provider_runtime_member_id({"provider_id": "stealthsurf", "source_ref": source_ref}, member_id, "hysteria2")
            conn.execute("INSERT INTO logical_server_members(logical_server_id,member_id,member_runtime_name,member_config_json,transport_fingerprint,member_order,is_active) VALUES('logical-provider',?,?, '{}','fingerprint',?,1)", (canonical, runtime_name, int(member_id)))
        conn.execute("INSERT OR REPLACE INTO watchdog_state(id,path_key,failure_candidate_json,last_processed_decision_id) VALUES(1,'lan',?,NULL)",
                     (json.dumps({"kind":"traffic_recovery","path_key":"lan"}, sort_keys=True),))
    binding = provider_managed.binding_for(source_ref)
    pending = {"provider_managed": True, "emergency_direct": True,
               "source_ref": source_ref, "binding_revision": binding["binding_revision"],
               "logical_server_id": "logical-provider", "phase": "provider_recovery",
               "switch_outcome_unconfirmed": True, "switch_requested_member_id": "901",
               "switch_previous_member_id": "900", "traffic_decision_id": "decision-1"}
    assert watchdog_failure_state.compare_and_set_recovery_pending(None, pending)

    current_runtime_member = {"name": "runtime-old"}
    monkeypatch.setattr(provider_recovery, "_runtime_incarnation", lambda: "runtime-a")
    monkeypatch.setattr(provider_managed, "_active_provider_runtime_incarnation", lambda: "runtime-a")
    monkeypatch.setattr("fwrouter_api.services.subscription._subscription_url_for_source_ref", lambda _source: "https://provider.invalid/config")
    monkeypatch.setattr(provider_managed, "_normalized_refresh", lambda *_args, **_kwargs: SimpleNamespace(ok=True))
    monkeypatch.setattr(xray_common, "get_settings", lambda: SimpleNamespace(paths=SimpleNamespace(run_dir=Path(tmp_path / "run"))))
    monkeypatch.setattr(selector, "_active_selector_runtime", lambda: (None, SimpleNamespace(
        get_recovery_selection_snapshot=lambda *_args, **_kwargs: {
            "active_target": "logical-provider", "effective_member_runtime_identity": current_runtime_member["name"], "ok": True},
    )))
    monkeypatch.setattr("fwrouter_api.services.logical_topology.get_logical_runtime_name", lambda _logical: "logical-provider")

    class Adapter(_RecoveryAdapter):
        def get_configs(self, config_id, *, budget=None, max_age_s=None):
            self.calls.append(("get_configs", config_id, max_age_s))
            return [{"id": 1234, "server_id": 901, "location_id": 26,
                     "protocol": "hysteria2", "connection_url": "hysteria2://user:pass@example.invalid:443"}]
    adapter = Adapter()
    monkeypatch.setattr("fwrouter_api.services.provider_adapters.provider_adapter", lambda *_args, **_kwargs: adapter)
    receipts = []
    original_refresh = subscription_pipeline.refresh_subscription
    def commit_inventory(_source_ref):
        with db_session() as conn:
            current = provider_managed.binding_for(source_ref)
            provider_managed._record_discovery_revisioned(
                conn, source_ref, current["binding_revision"], 26, "hysteria2",
                [{"server_id": 901, "location_id": 26, "protocol": "hysteria2", "available_slots": 1}],
                expected_binding_revision=current["binding_revision"])
            from fwrouter_api.db.provider_managed import record_applied
            record_applied(conn, source_ref, revision=current["binding_revision"], member_id="901",
                           protocol="hysteria2", expected_binding_revision=current["binding_revision"])
            provider_managed.adopt_provider_inventory_revision(conn)
            receipts.append(dict(provider_managed._MATERIAL.get()["owned_inventory_receipt"]))
            if competing_revision:
                advance_selection_revision(conn)
        current_runtime_member["name"] = "runtime-requested"
        return {"ok": True, "runtime_verified": True, "outcome": "verified", "last_good_retained": False}
    monkeypatch.setattr(subscription_pipeline, "refresh_subscription", commit_inventory)

    result = provider_recovery._confirm_unconfirmed_switch(binding, pending, _RecoveryController([]))

    assert receipts and receipts[0]["operation_id"]
    assert receipts[0]["member_id"] == "901"
    assert receipts[0]["selection_revision"] > 0
    if competing_revision:
        assert result["status"] == "provider_recovery_stale"
        assert watchdog_failure_state.get_recovery_pending()["switch_outcome_unconfirmed"] is True
    else:
        assert result["status"] == "provider_switch_confirmed"
        assert result["evidence_code"] == "switch_outcome_confirmed"
        assert watchdog_failure_state.get_recovery_pending().get("switch_outcome_unconfirmed") is None
    get_settings.cache_clear()
