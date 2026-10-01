from __future__ import annotations

import pytest

from fwrouter_api.services import runtime_convergence, state_projection


def test_runtime_convergence_checks_effective_direct_without_expiring_saved_vpn_intent(monkeypatch):
    calls = []
    monkeypatch.setattr(runtime_convergence, "emergency_override", lambda: {"emergency_direct": True})
    monkeypatch.setattr(runtime_convergence, "_load_routing_state", lambda: {"desired_mode": "vpn"})
    monkeypatch.setattr(
        runtime_convergence, "expire_global_fixed_server",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("must preserve saved VPN target")),
    )
    monkeypatch.setattr(
        runtime_convergence, "reconcile_current_routing_if_drift",
        lambda **kwargs: calls.append(("dataplane", kwargs)) or {
            "ok": True, "action": "none", "live_global_mode": "direct", "drift_detected": False,
        },
    )
    monkeypatch.setattr(runtime_convergence, "_compute_has_scoped_vpn_subjects", lambda: pytest.fail("effective Direct has no VPN scope"))
    monkeypatch.setattr(runtime_convergence, "_converge_dnsmasq_selective_contract", lambda: pytest.fail("Direct does not require selective DNS"))

    result = runtime_convergence.run_runtime_convergence_check(requested_by="pytest", log_events=False, force=True)

    assert result["ok"] is True
    assert result["mode"] == "direct"
    assert result["desired_mode"] == "vpn"
    assert result["effective_override"] == "emergency_direct"
    assert result["dataplane"]["live_global_mode"] == "direct"
    assert result["dnsmasq"] is None
    assert calls == [("dataplane", {"requested_by": "pytest"})]


def test_nonforced_scheduler_bypasses_failure_cooldown_for_emergency_direct_check(monkeypatch):
    monkeypatch.setattr(
        runtime_convergence, "get_live_probe_cache",
        lambda *_args, **_kwargs: pytest.fail("emergency Direct check must not use a stale normal cache entry"),
    )
    monkeypatch.setattr(runtime_convergence, "emergency_override", lambda: {"emergency_direct": True})
    monkeypatch.setattr(
        runtime_convergence, "_runtime_convergence_cooldown_result",
        lambda **_kwargs: pytest.fail("emergency Direct verification must not be hidden by cooldown"),
    )
    monkeypatch.setattr(runtime_convergence, "_load_routing_state", lambda: {"desired_mode": "vpn"})
    monkeypatch.setattr(runtime_convergence, "expire_global_fixed_server", lambda **_kwargs: pytest.fail("must not expire VPN intent"))
    monkeypatch.setattr(runtime_convergence, "reconcile_current_routing_if_drift", lambda **_kwargs: {"ok": True, "action": "none"})

    result = runtime_convergence.run_runtime_convergence_check(requested_by="pytest", log_events=False)

    assert result["status"] == "emergency_direct"
    assert result["effective_override"] == "emergency_direct"


def test_routing_projection_preserves_desired_vpn_and_warns_for_verified_emergency_direct(monkeypatch):
    monkeypatch.setattr(state_projection, "emergency_override", lambda: {"emergency_direct": True})
    monkeypatch.setattr(state_projection, "_read_routing_global_state_readonly", lambda: {
        "desired_mode": "vpn", "applied_mode": "vpn", "selective_default": "vpn",
        "server_mode": "fixed", "desired_fixed_server_id": "saved-target",
        "applied_fixed_server_id": "saved-target", "active_auto_server_id": None,
        "apply_state": "clean", "error_code": None, "error_message": None,
        "updated_at": "2026-10-01T00:00:00Z",
    })
    monkeypatch.setattr(state_projection, "read_live_dataplane_payload", lambda: {"ok": True, "checked_at": "2026-10-01T00:00:01Z"})
    monkeypatch.setattr(state_projection, "read_applied_manifest", lambda: {"routing_global_state": {"desired_mode": "direct"}})
    monkeypatch.setattr(state_projection, "build_runtime_enforcement_state", lambda **_kwargs: {
        "traffic_enforcement_guaranteed": True, "enforcement_level": "global_direct_enforced",
        "active_mode_matches_intent": False, "live_global_mode": "direct", "live_selective_default": "direct",
        "supported_modes": {"direct": True}, "missing_runtime_requirements": [], "bypass_active": False,
        "selective_rules": {},
    })
    monkeypatch.setattr(state_projection, "get_rules_state", lambda: {"status": "ready"})
    monkeypatch.setattr(state_projection, "list_rules_metadata", lambda: [])

    result = state_projection.build_routing_state_projection()["routing"]

    assert result["intent"]["mode"] == "vpn"
    assert result["intent"]["target_id"] == "saved-target"
    assert result["effective"]["global_mode"] == "direct"
    assert result["effective"]["desired_global_mode"] == "vpn"
    assert result["effective"]["effective_override"] == "emergency_direct"
    assert result["reconcile"]["state"] == "in_sync"
    assert result["reconcile"]["reason_code"] == "PROVIDER_EMERGENCY_DIRECT"
    assert result["projection"]["state"] == "warning"
    assert result["projection"]["message_key"] == "provider_emergency_direct"
    assert result["reason"]["code"] == "provider_emergency_direct"
    assert result["reason"]["message_key"] == "provider_emergency_direct"
    assert result["observation"]["evidence"]["forced_vpn_bindings"]["xray_forced_vpn"] is False


def test_routing_projection_degrades_if_emergency_direct_is_not_live(monkeypatch):
    monkeypatch.setattr(state_projection, "emergency_override", lambda: {"emergency_direct": True})
    monkeypatch.setattr(state_projection, "_read_routing_global_state_readonly", lambda: {"desired_mode": "vpn", "applied_mode": "vpn", "apply_state": "clean"})
    monkeypatch.setattr(state_projection, "read_live_dataplane_payload", lambda: {"ok": True})
    monkeypatch.setattr(state_projection, "read_applied_manifest", lambda: None)
    monkeypatch.setattr(state_projection, "build_runtime_enforcement_state", lambda **_kwargs: {
        "traffic_enforcement_guaranteed": False, "enforcement_level": "owned_table_missing",
        "active_mode_matches_intent": True, "live_global_mode": "vpn", "live_selective_default": "vpn",
        "supported_modes": {}, "missing_runtime_requirements": [], "bypass_active": False,
        "selective_rules": {},
    })
    monkeypatch.setattr(state_projection, "get_rules_state", lambda: {})
    monkeypatch.setattr(state_projection, "list_rules_metadata", lambda: [])

    result = state_projection.build_routing_state_projection()["routing"]

    assert result["intent"]["mode"] == "vpn"
    assert result["effective"]["effective_override"] == "emergency_direct"
    assert result["reconcile"]["state"] == "runtime_drift"
    assert result["projection"]["state"] == "degraded"
    assert result["observation"]["evidence"]["forced_vpn_bindings"]["xray_forced_vpn"] is None


def test_shared_routing_drift_compares_live_direct_to_emergency_override(monkeypatch):
    from fwrouter_api.services import apply_orchestrator, apply_orchestrator_drift, provider_recovery
    monkeypatch.setattr(provider_recovery, "emergency_override", lambda: {"emergency_direct": True})
    monkeypatch.setattr(apply_orchestrator, "probe_live_global_mode", lambda: {"ok": True, "mode": "direct"})
    monkeypatch.setattr(apply_orchestrator, "_live_applied_nft_artifact_consistency", lambda: {"detected": False})
    intent = {"desired_mode": "vpn", "applied_mode": "vpn"}
    result = apply_orchestrator_drift._current_routing_drift(routing=intent)
    assert result["detected"] is False
    assert result["expected_mode"] == "direct"
    assert intent["desired_mode"] == "vpn"
