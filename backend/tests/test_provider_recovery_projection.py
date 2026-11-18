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
    monkeypatch.setattr(
        provider_recovery, "emergency_override", lambda: {"emergency_direct": True}
    )
    canonical_artifact_probe = apply_orchestrator_drift._live_applied_nft_artifact_consistency
    patched_artifact_probe = {"detected": False, "compat_patch_seen": True}
    intent = {"desired_mode": "vpn", "applied_mode": "vpn"}

    with monkeypatch.context() as patch:
        patch.setattr(
            apply_orchestrator,
            "probe_live_global_mode",
            lambda: {"ok": True, "mode": "direct"},
        )
        patch.setattr(
            apply_orchestrator,
            "_live_applied_nft_artifact_consistency",
            lambda: patched_artifact_probe,
        )

        assert (
            apply_orchestrator_drift._live_applied_nft_artifact_consistency
            is canonical_artifact_probe
        )
        result = apply_orchestrator_drift._current_routing_drift(routing=intent)

    assert result["detected"] is False
    assert result["expected_mode"] == "direct"
    assert result["live_artifact_consistency"] is patched_artifact_probe
    assert (
        apply_orchestrator_drift._live_applied_nft_artifact_consistency
        is canonical_artifact_probe
    )
    assert apply_orchestrator._live_applied_nft_artifact_consistency is canonical_artifact_probe
    assert intent["desired_mode"] == "vpn"


@pytest.mark.parametrize(
    ("applied_ready", "current_ready", "expected_drift"),
    [
        (False, True, True), (True, False, True), (True, True, False),
        (False, False, False), (None, True, True), (False, None, True),
    ],
)
def test_selective_policy_readiness_change_is_routing_drift(
    monkeypatch, applied_ready, current_ready, expected_drift,
):
    from fwrouter_api.services import apply_orchestrator_drift

    routing = {
        "desired_mode": "selective",
        "applied_mode": "selective",
        "selective_default": "direct",
    }
    monkeypatch.setattr(
        apply_orchestrator_drift, "probe_live_global_mode",
        lambda: {"ok": True, "mode": "selective", "selective_default": "direct"},
    )
    monkeypatch.setattr(
        apply_orchestrator_drift, "_probe_live_artifact_consistency",
        lambda: {"detected": False, "checked": True},
    )
    monkeypatch.setattr(
        apply_orchestrator_drift, "read_applied_manifest",
        lambda: {"global_preflight": {
            "selective_rules": {"requires_vpn_runtime": True},
            "selective_vpn_policy_ready": applied_ready,
            "selective_degraded": not applied_ready,
        }},
    )
    monkeypatch.setattr(
        apply_orchestrator_drift, "build_global_preflight",
        lambda **_kwargs: {
            "selective_vpn_policy_ready": current_ready,
            "selective_degraded": not current_ready,
        },
    )

    drift = apply_orchestrator_drift._current_routing_drift(
        routing=routing, check_policy_readiness=True,
    )

    assert drift["detected"] is expected_drift
    readiness_valid = isinstance(applied_ready, bool) and isinstance(current_ready, bool)
    assert drift["policy_readiness_drift"]["checked"] is readiness_valid
    assert drift["policy_readiness_drift"]["current_ready"] is current_ready
    assert drift["policy_readiness_drift"]["state"] == (
        "compared" if readiness_valid else "unknown"
    )
    assert drift["code"] == (
        "SELECTIVE_VPN_POLICY_READINESS_DRIFT" if expected_drift else None
    )


def test_selective_policy_readiness_is_checked_only_by_runtime_reconciliation(monkeypatch):
    from fwrouter_api.services import apply_orchestrator_drift

    monkeypatch.setattr(
        apply_orchestrator_drift, "probe_live_global_mode",
        lambda: {"ok": True, "mode": "selective", "selective_default": "direct"},
    )
    monkeypatch.setattr(
        apply_orchestrator_drift, "_probe_live_artifact_consistency",
        lambda: {"detected": False, "checked": True},
    )
    monkeypatch.setattr(
        apply_orchestrator_drift, "read_applied_manifest",
        lambda: {"global_preflight": {
            "selective_rules": {"requires_vpn_runtime": True},
            "selective_vpn_policy_ready": False,
        }},
    )
    monkeypatch.setattr(
        apply_orchestrator_drift, "build_global_preflight",
        lambda **_kwargs: pytest.fail(
            "ordinary drift inspection must not run live policy readiness preflight"
        ),
    )

    drift = apply_orchestrator_drift._current_routing_drift(routing={
        "desired_mode": "selective", "applied_mode": "selective",
        "selective_default": "direct",
    })

    assert drift["detected"] is False
    assert drift["policy_readiness_drift"]["state"] == "not_applicable"


def test_runtime_convergence_reapplies_selective_policy_after_recovery(monkeypatch):
    from fwrouter_api.services import apply_orchestrator_drift, apply_orchestrator_public
    from fwrouter_api.services import provider_recovery

    routing = {
        "desired_mode": "selective",
        "applied_mode": "selective",
        "selective_default": "direct",
    }
    monkeypatch.setattr(apply_orchestrator_public, "get_routing_snapshot", lambda: routing)
    monkeypatch.setattr(
        apply_orchestrator_drift, "probe_live_global_mode",
        lambda: {"ok": True, "mode": "selective", "selective_default": "direct"},
    )
    monkeypatch.setattr(
        apply_orchestrator_drift, "_probe_live_artifact_consistency",
        lambda: {"detected": False, "checked": True},
    )
    monkeypatch.setattr(
        apply_orchestrator_drift, "read_applied_manifest",
        lambda: {"global_preflight": {
            "selective_rules": {"requires_vpn_runtime": True},
            "selective_vpn_policy_ready": False,
            "selective_degraded": True,
        }},
    )
    monkeypatch.setattr(
        apply_orchestrator_drift, "build_global_preflight",
        lambda **_kwargs: {
            "selective_vpn_policy_ready": True,
            "selective_degraded": False,
        },
    )
    monkeypatch.setattr(provider_recovery, "emergency_override", lambda: None)
    applied = []
    monkeypatch.setattr(
        apply_orchestrator_public, "set_global_mode",
        lambda mode, **kwargs: applied.append((mode, kwargs)) or {"ok": True},
    )

    result = apply_orchestrator_public.reconcile_current_routing_if_drift(
        requested_by="runtime_convergence_scheduler",
    )

    assert result["ok"] is True
    assert result["action"] == "reapply_global_mode"
    assert result["drift"]["policy_readiness_drift"]["detected"] is True
    assert applied == [("selective", {"requested_by": "runtime_convergence_scheduler"})]


def test_scoped_selective_readiness_drift_reconciles_under_global_direct(monkeypatch):
    from fwrouter_api.services import apply_orchestrator_drift, apply_orchestrator_public

    routing = {
        "desired_mode": "direct",
        "applied_mode": "direct",
        "selective_default": "direct",
    }
    monkeypatch.setattr(apply_orchestrator_public, "get_routing_snapshot", lambda: routing)
    monkeypatch.setattr(
        apply_orchestrator_drift, "probe_live_global_mode",
        lambda: {"ok": True, "mode": "direct", "selective_default": "direct"},
    )
    monkeypatch.setattr(
        apply_orchestrator_drift, "_probe_live_artifact_consistency",
        lambda: {"detected": False, "checked": True},
    )
    applied_manifest = {
        "subjects": [{
            "is_active": True,
            "effective_state": {"effective_mode": "selective"},
        }],
        "global_preflight": {
            "selective_rules": {"requires_vpn_runtime": True},
            "selective_vpn_policy_ready": False,
            "selective_degraded": True,
        },
    }
    monkeypatch.setattr(apply_orchestrator_drift, "read_applied_manifest", lambda: applied_manifest)
    monkeypatch.setattr(
        apply_orchestrator_drift, "build_global_preflight",
        lambda **_kwargs: {
            "selective_rules": {"requires_vpn_runtime": True},
            "selective_vpn_policy_ready": True,
            "selective_degraded": False,
        },
    )
    applied = []

    def _apply_once(mode, **kwargs):
        applied.append((mode, kwargs))
        applied_manifest["global_preflight"]["selective_vpn_policy_ready"] = True
        applied_manifest["global_preflight"]["selective_degraded"] = False
        return {"ok": True}

    monkeypatch.setattr(
        apply_orchestrator_public, "set_global_mode",
        _apply_once,
    )

    result = apply_orchestrator_public.reconcile_current_routing_if_drift(
        requested_by="runtime_convergence_scheduler",
    )

    assert result["ok"] is True
    assert result["action"] == "reapply_global_mode"
    assert applied == [("direct", {"requested_by": "runtime_convergence_scheduler"})]

    second = apply_orchestrator_public.reconcile_current_routing_if_drift(
        requested_by="runtime_convergence_scheduler",
    )
    assert second["ok"] is True
    assert second["action"] == "none"
    assert applied == [("direct", {"requested_by": "runtime_convergence_scheduler"})]


def test_selective_mode_with_missing_readiness_receipt_requests_guarded_refresh(monkeypatch):
    from fwrouter_api.services import apply_orchestrator_drift

    monkeypatch.setattr(
        apply_orchestrator_drift, "probe_live_global_mode",
        lambda: {"ok": True, "mode": "selective", "selective_default": "direct"},
    )
    monkeypatch.setattr(
        apply_orchestrator_drift, "_probe_live_artifact_consistency",
        lambda: {"detected": False, "checked": True},
    )
    monkeypatch.setattr(apply_orchestrator_drift, "read_applied_manifest", lambda: None)
    monkeypatch.setattr(
        apply_orchestrator_drift, "build_global_preflight",
        lambda **_kwargs: {
            "selective_rules": {"requires_vpn_runtime": True},
            "selective_vpn_policy_ready": True,
            "selective_degraded": False,
        },
    )

    drift = apply_orchestrator_drift._current_routing_drift(routing={
        "desired_mode": "selective", "applied_mode": "selective",
        "selective_default": "direct",
    }, check_policy_readiness=True)

    assert drift["detected"] is True
    assert drift["policy_readiness_drift"] == {
        "detected": True, "checked": False, "state": "unknown",
        "applied_ready": None, "current_ready": True,
        "applied_degraded": None, "current_degraded": False,
    }


def test_pure_direct_without_scoped_selective_policy_skips_readiness_probe(monkeypatch):
    from fwrouter_api.services import apply_orchestrator_drift

    monkeypatch.setattr(
        apply_orchestrator_drift, "probe_live_global_mode",
        lambda: {"ok": True, "mode": "direct", "selective_default": "direct"},
    )
    monkeypatch.setattr(
        apply_orchestrator_drift, "_probe_live_artifact_consistency",
        lambda: {"detected": False, "checked": True},
    )
    monkeypatch.setattr(
        apply_orchestrator_drift, "read_applied_manifest", lambda: {"subjects": []},
    )
    monkeypatch.setattr(
        apply_orchestrator_drift, "build_global_preflight",
        lambda **_kwargs: pytest.fail("pure Direct has no selective VPN policy to probe"),
    )

    drift = apply_orchestrator_drift._current_routing_drift(routing={
        "desired_mode": "direct", "applied_mode": "direct",
        "selective_default": "direct",
    })

    assert drift["detected"] is False
    assert drift["policy_readiness_drift"]["state"] == "not_applicable"


def test_failed_reconciliation_apply_is_not_reported_as_success(monkeypatch):
    from fwrouter_api.services import apply_orchestrator_public
    from fwrouter_api.services import provider_recovery

    monkeypatch.setattr(apply_orchestrator_public, "get_routing_snapshot", lambda: {
        "desired_mode": "selective", "applied_mode": "selective",
    })
    monkeypatch.setattr(
        apply_orchestrator_public, "_current_routing_drift",
        lambda **_kwargs: {"detected": True, "code": "SELECTIVE_VPN_POLICY_READINESS_DRIFT"},
    )
    monkeypatch.setattr(provider_recovery, "emergency_override", lambda: None)
    monkeypatch.setattr(
        apply_orchestrator_public, "set_global_mode",
        lambda *_args, **_kwargs: {"ok": False, "code": "APPLY_FAILED", "message": "apply failed"},
    )

    result = apply_orchestrator_public.reconcile_current_routing_if_drift(
        requested_by="runtime_convergence_scheduler",
    )

    assert result["ok"] is False
    assert result["action"] == "reapply_global_mode"
    assert result["error_code"] == "APPLY_FAILED"
    assert result["error_message"] == "apply failed"
