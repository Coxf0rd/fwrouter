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
    monkeypatch.setattr("fwrouter_api.adapters.xray_common.xray_writer_guard", nullcontext)
    monkeypatch.setattr(provider_recovery, "_transition_event", lambda *_args: None)
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

    def get_server_stats(self, config_id, *, budget=None):
        self.calls.append(("stats", config_id, budget.max_requests, budget.deadline_seconds))
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
    monkeypatch.setattr("fwrouter_api.services.provider_adapters.provider_adapter", lambda *_args: adapter)
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
    assert operations[0] == ("source-provider", "recovery_refresh", {"expected_revision": 7})
    assert operations[1][0:2] == ("source-provider", "switch")
    assert operations[1][2]["member_id"] == "server-alternative"
    assert operations[1][2]["expected_revision"] == 7
    assert operations[1][2]["_adapter"] is adapter
    assert operations[1][2]["_budget"].max_requests == 4
    assert operations[1][2]["_budget"].deadline_seconds == 30
    assert adapter.calls == [("stats", 1234, 4, 30)]
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
        lambda *_args: pytest.fail("provider adapter must not be opened"),
    )
    controller = _RecoveryController([])

    suppressed = provider_recovery.confirmed_provider_recovery(
        logical_server_id="logical-provider", path_key="lan", decision_id="d1",
        controller=controller, timeout_ms=3000, allow_switch=False,
    )

    assert suppressed["status"] == "provider_recovery_suppressed"
    assert provider_calls == []
    assert controller.calls == []


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
    monkeypatch.setattr("fwrouter_api.services.scoped_egress._load_explicit_client_runtime_bindings", lambda *_args: {})

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
    monkeypatch.setattr("fwrouter_api.services.scoped_egress._load_explicit_client_runtime_bindings", lambda *_args: {})
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

    result = provider_recovery.enter_emergency_direct({
        "source_ref": "source-provider", "binding_revision": 7,
        "logical_server_id": "logical-provider", "provider_confirmation": 3,
    })

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
    assert [call for call in controller.calls if call[0] == "probe"]
    if expected_pending:
        assert state["pending"]["phase"] == expected_pending
    else:
        assert result["effective_override"] is None
        assert reset_calls == [True]
    expected_apply_calls = (
        [True] if expected_action == "verified_vpn_reentry" else
        [True, False] if probes[0].get("ok") and apply_results else []
    )
    assert apply_calls == expected_apply_calls


@pytest.mark.parametrize("locale", ["ru", "en"])
@pytest.mark.parametrize("code", ["provider_operation_verified", "provider_operation_unconfirmed", "provider_emergency_direct", "provider_vpn_reentry"])
def test_provider_events_use_shared_localization(code, locale):
    from fwrouter_api.services.ui_text import _ui_text_title, _ui_text_reason
    assert _ui_text_title("log.event", code, locale=locale)
    assert _ui_text_reason("log.event", code, locale=locale)


def test_unconfirmed_direct_is_repaired_before_reentry_probe(monkeypatch):
    state = _install_pending_state(monkeypatch, {"provider_managed": True, "emergency_direct": True, "phase": "emergency_direct_unconfirmed"})
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
