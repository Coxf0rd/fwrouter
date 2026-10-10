from __future__ import annotations

import hashlib
import json
import threading
import urllib.request

import pytest

from .http_support import http_json
from .joined_support import await_core_job
from .xray_support import assert_mihomo_launch_matches_active


def _seed_source(stack: dict) -> str:
    """Create one synthetic saved subscription in this suite's SQLite DB."""
    from fwrouter_api.db.connection import db_session

    url = "https://acceptance.invalid/provider-source"
    source_ref = "src:" + hashlib.sha256(url.encode()).hexdigest()
    metadata = {"subscriptions": {"items": [{"url": url, "name": "Acceptance provider", "enabled": True}]}}
    with db_session() as connection:
        connection.execute(
            """INSERT INTO subscription_state (id, url, status, metadata_json, updated_at)
               VALUES (1, ?, 'success', ?, CURRENT_TIMESTAMP)
               ON CONFLICT(id) DO UPDATE SET url=excluded.url, status=excluded.status,
                   metadata_json=excluded.metadata_json, updated_at=CURRENT_TIMESTAMP""",
            (url, json.dumps(metadata, separators=(",", ":"))),
        )
    return source_ref


def _configure_provider(api: str, source_ref: str, *, enabled: bool = True) -> dict:
    code, response = http_json(
        f"{api}/subscription/sources/{source_ref}/provider/configuration",
        method="POST",
        payload={"enabled": enabled, "provider_id": "stealthsurf", "api_key": "acceptance-key",
                 "resource_id": 42, "protocol": "vless", "allow_automatic_member_switch": True},
    )
    assert code == 200 and response.get("ok") is True, response
    return response["data"]["binding"]


def _enable_owned_mihomo_runtime_state() -> None:
    """Mark the isolated application fixture's real Mihomo child as managed."""
    from fwrouter_api.db.connection import db_session

    with db_session() as connection:
        connection.execute(
            """UPDATE modules SET desired_state='enabled', lifecycle_mode='managed',
                   runtime_state='running', apply_state='clean', status_text='Owned native Mihomo acceptance child'
               WHERE module_name='vpn'"""
        )


def test_core_subscription_provider_discovery_exclusive_intent_and_real_mihomo_child(acceptance_stack):
    """Join API, persistent subscription/Core state, provider HTTP, and native Mihomo readback."""
    stack = acceptance_stack
    api, bridge, native = stack["api"], stack["provider_bridge"], stack["native"]
    _enable_owned_mihomo_runtime_state()
    source_ref = _seed_source(stack)
    binding = _configure_provider(api, source_ref)

    # Core projection comes through the application API and the provider call
    # crosses the real StealthSurf HTTP client into the local scripted server.
    code, projection = http_json(f"{api}/subscription")
    assert code == 200 and projection.get("ok") is True, projection
    assert projection["data"]["subscription"]["provider_managed"]["bindings"][0]["source_ref"] == source_ref
    code, discovered = http_json(f"{api}/subscription/sources/{source_ref}/provider/configs", method="POST", payload={})
    assert code == 200 and discovered.get("ok") is True, discovered
    assert discovered["data"].get("configs") or discovered["data"].get("available_configs")
    observed = bridge.snapshot_calls()
    assert any(method == "GET" and path == "/configs" for method, path, _query, _body in observed), observed
    assert all("acceptance-key" not in json.dumps(row) for row in observed), "provider credential leaked into test bridge evidence"

    probe_response_count = len(bridge.snapshot_probe_responses())
    code, enable = http_json(
        f"{api}/subscription/sources/{source_ref}/provider", method="POST", payload={"action": "enable"}
    )
    assert code == 200 and enable.get("ok") is True and enable["data"].get("accepted") is True, enable
    enabled_job = await_core_job(api, enable)
    assert enabled_job.get("status") == "success", {
        "job_status": enabled_job.get("status"),
        "job_error_code": enabled_job.get("error_code"),
        "probe_bridge": bridge.snapshot_probe_summary(),
        "native_provider_upstream": native.provider_upstream_summary,
    }
    # A successful native selector handoff must include the real loopback bridge response.
    observed_probe_responses = bridge.snapshot_probe_responses()[probe_response_count:]
    assert 204 in observed_probe_responses, {
        "probe_bridge": bridge.snapshot_probe_summary(),
        "native_provider_upstream": native.provider_upstream_summary,
    }

    # Exclusive-source intent is persisted by Core and projected back by API.
    code, exclusive = http_json(
        f"{api}/subscription/sources/{source_ref}/vpn-auto-exclusive", method="POST", payload={"enabled": True}
    )
    assert code == 200 and exclusive.get("ok") is True and exclusive["data"].get("accepted") is True, exclusive
    exclusive_job = await_core_job(api, exclusive)
    assert exclusive_job.get("status") == "success", exclusive_job
    from fwrouter_api.services.vpn_auto_exclusive import get_vpn_auto_exclusive_source_ref
    assert get_vpn_auto_exclusive_source_ref() == source_ref
    assert binding["allow_automatic_member_switch"] is True

    # Readback is from the pinned native Mihomo child and its actual controller.
    child = native.rpc("mihomo_status", {})
    incarnation = native.rpc("mihomo_incarnation", {})
    assert child.get("ok") is True, child
    assert incarnation.get("ok") is True and incarnation["details"].get("stdout"), incarnation
    code, mihomo = http_json(f"{api}/mihomo")
    assert code == 200 and mihomo.get("ok") is True, mihomo
    code, config = http_json(f"{api}/mihomo/config?include_config=true")
    assert code == 200 and config.get("ok") is True, config
    assert config["data"]["config"].get("base_exists") is True, config


def test_provider_disabled_and_unknown_source_do_not_call_provider_or_patch(acceptance_stack):
    stack = acceptance_stack
    api, bridge = stack["api"], stack["provider_bridge"]
    source_ref = _seed_source(stack)
    _configure_provider(api, source_ref, enabled=False)
    bridge.calls.clear()
    code, disabled = http_json(f"{api}/subscription/sources/{source_ref}/provider/configs", method="POST", payload={})
    assert code == 200 and disabled.get("ok") is False
    assert disabled.get("error", {}).get("code") == "PROVIDER_DISABLED", disabled
    assert bridge.snapshot_calls() == [], "disabled provider discovery must not reach the provider API"

    code, switch = http_json(
        f"{api}/subscription/sources/{source_ref}/provider", method="POST",
        payload={"action": "switch", "member_id": "901", "location_id": "6"},
    )
    assert code == 200 and switch.get("ok") is True, switch
    switch_job = await_core_job(api, switch)
    assert switch_job.get("status") == "failed", switch_job
    assert "PROVIDER_DISABLED" in json.dumps(switch_job)
    assert bridge.snapshot_calls() == [], "disabled provider mutation must not read or PATCH the provider API"

    unknown_ref = "src:" + "0" * 64
    code, unknown_discovery = http_json(
        f"{api}/subscription/sources/{unknown_ref}/provider/configs", method="POST", payload={}
    )
    assert code == 200 and unknown_discovery.get("ok") is False, unknown_discovery
    assert unknown_discovery.get("error", {}).get("code") == "SUBSCRIPTION_SOURCE_NOT_FOUND", unknown_discovery
    code, unknown = http_json(
        f"{api}/subscription/sources/{unknown_ref}/provider", method="POST",
        payload={"action": "switch", "member_id": "901", "location_id": "6"},
    )
    assert code == 200 and unknown.get("ok") is False, unknown
    assert unknown.get("error", {}).get("code") == "SUBSCRIPTION_SOURCE_NOT_FOUND", unknown
    assert bridge.snapshot_calls() == [], "unknown source discovery and mutation must not call the provider API"


def test_confirmed_provider_failure_applies_emergency_direct_and_failed_reentry_stays_direct(acceptance_stack):
    stack = acceptance_stack
    api, bridge = stack["api"], stack["provider_bridge"]
    _enable_owned_mihomo_runtime_state()
    source_ref = _seed_source(stack)
    _configure_provider(api, source_ref)
    code, accepted = http_json(
        f"{api}/subscription/sources/{source_ref}/provider", method="POST", payload={"action": "enable"}
    )
    assert code == 200 and accepted.get("ok") is True, accepted
    initialized = await_core_job(api, accepted)
    assert initialized.get("status") == "success", initialized
    from fwrouter_api.services.provider_managed import binding_for
    binding = binding_for(source_ref)
    logical_id = binding["logical_server_id"]

    # Establish the failure precondition through the public Core API: the
    # provider is the active native Mihomo target and the generated transparent
    # contour is actually listening before global VPN is applied.
    active_config = stack["state"] / "generated" / "mihomo" / "config.yaml"
    assert_mihomo_launch_matches_active(stack["native"], active_config)
    code, mihomo = http_json(f"{api}/mihomo")
    assert code == 200 and mihomo.get("ok") is True, mihomo
    mihomo_state = mihomo["data"]["mihomo"]
    assert mihomo_state.get("runtime_state") == "running", mihomo_state
    mihomo_details = mihomo_state.get("details") or {}
    contours = ((mihomo_details.get("config") or {}).get("fwrouter_contours") or {})
    transparent_vpn = contours.get("transparent_vpn") or {}
    assert transparent_vpn.get("transparent_tcp_ready") is True, transparent_vpn
    assert transparent_vpn.get("transparent_udp_ready") is True, transparent_vpn
    assert transparent_vpn.get("transparent_tcp_listener_socket_present") is True, transparent_vpn
    assert transparent_vpn.get("transparent_udp_listener_socket_present") is True, transparent_vpn
    selectors = mihomo_details.get("selectors") or {}
    assert selectors.get("vpn_global_now") == "vpn-auto", selectors
    assert str(selectors.get("vpn_auto_now") or "").startswith("Provider VPN ["), selectors

    code, vpn_accepted = http_json(
        f"{api}/routing/global", method="POST",
        payload={"mode": "vpn", "requested_by": "hosted-acceptance", "run_now": True},
    )
    assert code == 200 and vpn_accepted.get("ok") is True, vpn_accepted
    vpn_job = await_core_job(api, vpn_accepted)
    assert vpn_job.get("status") == "success", vpn_job
    code, vpn_projection = http_json(f"{api}/routing/global")
    assert code == 200 and vpn_projection.get("ok") is True, vpn_projection
    vpn_routing = vpn_projection["data"]["routing"]
    vpn_enforcement = vpn_projection["data"]["runtime_enforcement"]
    assert vpn_routing.get("desired_mode") == "vpn", vpn_routing
    assert vpn_routing.get("applied_mode") == "vpn", vpn_routing
    assert vpn_enforcement.get("enforcement_level") == "global_vpn_enforced", vpn_enforcement
    assert vpn_enforcement.get("traffic_enforcement_guaranteed") is True, vpn_enforcement
    assert vpn_enforcement.get("supported_modes", {}).get("vpn") is True, vpn_enforcement
    assert vpn_enforcement.get("missing_runtime_requirements") == [], vpn_enforcement
    assert vpn_enforcement.get("active_mode_matches_intent") is True, vpn_enforcement
    assert vpn_enforcement.get("live_global_mode") == "vpn", vpn_enforcement
    applied_manifest_path = stack["state"] / "generated" / "dataplane" / "applied-manifest.json"
    vpn_manifest = json.loads(applied_manifest_path.read_text(encoding="utf-8"))
    assert vpn_manifest.get("routing_global_state", {}).get("desired_mode") == "vpn", vpn_manifest
    vpn_preflight = vpn_manifest.get("global_preflight") or {}
    assert vpn_preflight.get("vpn_policy_required") is True, vpn_preflight
    assert vpn_preflight.get("can_enforce_global_vpn") is True, vpn_preflight
    assert vpn_preflight.get("missing_by_mode", {}).get("vpn") == [], vpn_preflight
    vpn_contour = vpn_manifest.get("vpn_contour") or {}
    assert vpn_contour.get("required") is True, vpn_contour
    assert isinstance(vpn_contour.get("redir_port"), int) and vpn_contour["redir_port"] > 0, vpn_contour
    assert isinstance(vpn_contour.get("tproxy_port"), int) and vpn_contour["tproxy_port"] > 0, vpn_contour

    # Disable provider-driven automatic switching while retaining the source's
    # actual provider binding. Three distinct Core recovery decisions must
    # end in a real Emergency Direct apply after local Mihomo probe failure.
    code, policy = http_json(
        f"{api}/subscription/sources/{source_ref}/provider/configuration", method="POST",
        payload={"allow_automatic_member_switch": False},
    )
    assert code == 200 and policy.get("ok") is True, policy
    bridge.set_mode("normal")
    bridge.set_probe_available(False)
    code, first_confirmation = http_json(
        f"{api}/__acceptance/provider/recovery", method="POST",
        payload={"logical_server_id": logical_id, "decision_id": "incident-confirmation-1",
                 "timeout_ms": 1000, "allow_switch": True}, timeout=90,
    )
    assert code == 200 and first_confirmation.get("provider_recovery") is True, first_confirmation
    assert first_confirmation.get("provider_confirmation") == 1, first_confirmation
    # Confirmation two is policy-suppressed. It must not make a status,
    # discovery, config, or mutation call to the provider API.
    bridge.calls.clear()
    code, second_confirmation = http_json(
        f"{api}/__acceptance/provider/recovery", method="POST",
        payload={"logical_server_id": logical_id, "decision_id": "incident-confirmation-2",
                 "timeout_ms": 1000, "allow_switch": True}, timeout=90,
    )
    assert code == 200 and second_confirmation.get("status") == "provider_auto_switch_disabled", second_confirmation
    assert second_confirmation.get("switch_attempted") is False and second_confirmation.get("last_good_retained") is True, second_confirmation
    assert bridge.snapshot_calls() == [], second_confirmation
    bridge.calls.clear()
    code, direct = http_json(
        f"{api}/__acceptance/provider/recovery", method="POST",
        payload={"logical_server_id": logical_id, "decision_id": "incident-confirmation-3",
                 "timeout_ms": 1000, "allow_switch": True}, timeout=90,
    )
    assert code == 200 and direct.get("status") == "emergency_direct", direct
    assert direct.get("effective_override") == "emergency_direct", direct
    assert bridge.snapshot_calls() == [], "policy-disabled recovery must not rediscover or mutate the provider"
    code, projection = http_json(f"{api}/subscription")
    assert code == 200 and projection["data"]["subscription"]["provider_managed"]["effective_override"] == "emergency_direct", projection
    code, durable_routing = http_json(f"{api}/routing/global")
    assert code == 200 and durable_routing.get("ok") is True, durable_routing
    assert durable_routing["data"]["routing"].get("desired_mode") == "vpn", durable_routing
    assert durable_routing["data"]["runtime_enforcement"].get("live_global_mode") == "direct", durable_routing
    assert durable_routing["data"]["runtime_enforcement"].get("active_mode_matches_intent") is False, durable_routing
    assert durable_routing["data"]["runtime_enforcement"].get("missing_runtime_requirements") == [
        "active_dataplane_mode_mismatch"
    ], durable_routing
    direct_manifest = json.loads(applied_manifest_path.read_text(encoding="utf-8"))
    assert direct_manifest.get("reason") == "provider_emergency_direct", direct_manifest
    assert direct_manifest.get("routing_global_state", {}).get("desired_mode") == "direct", direct_manifest
    assert direct_manifest.get("global_preflight", {}).get("vpn_policy_required") is False, direct_manifest

    # Provider API traffic remains available while the owned loopback health
    # destination is deliberately unavailable. Reentry must preserve Direct
    # when the real controller connectivity preflight fails.
    code, reentry = http_json(f"{api}/__acceptance/provider/reentry", method="POST",
                              payload={"timeout_ms": 1000}, timeout=90)
    assert code == 200 and reentry.get("ok") is False, reentry
    assert reentry.get("effective_override") == "emergency_direct", reentry
    assert bridge.snapshot_calls() == [], "native reentry validation must not call the provider API"
    code, after = http_json(f"{api}/subscription")
    assert code == 200 and after["data"]["subscription"]["provider_managed"]["effective_override"] == "emergency_direct", after

    # Restore the owned VLESS→Xray→HTTP health destination and require a
    # verified reentry on the same provider member before Direct is cleared.
    bridge.set_probe_available(True)
    code, restored = http_json(f"{api}/__acceptance/provider/reentry", method="POST",
                               payload={"timeout_ms": 2000}, timeout=90)
    assert code == 200 and restored.get("ok") is True, restored
    assert restored.get("effective_override") in {None, ""}, restored
    assert bridge.snapshot_calls() == [], "native reentry validation must not call the provider API"
    code, state = http_json(f"{api}/state/vpn")
    assert code == 200 and state.get("ok") is True, state
    assert state["data"]["vpn"]["effective"]["server_health"]["active"]["server_id"] == logical_id, state
    code, final_routing = http_json(f"{api}/routing/global")
    assert code == 200 and final_routing.get("ok") is True, final_routing
    assert final_routing["data"]["routing"].get("desired_mode") == "vpn", final_routing
    assert final_routing["data"]["routing"].get("applied_mode") == "vpn", final_routing
    final_enforcement = final_routing["data"]["runtime_enforcement"]
    assert final_enforcement.get("enforcement_level") == "global_vpn_enforced", final_enforcement
    assert final_enforcement.get("traffic_enforcement_guaranteed") is True, final_enforcement
    assert final_enforcement.get("active_mode_matches_intent") is True, final_enforcement
    assert final_enforcement.get("live_global_mode") == "vpn", final_enforcement
    final_manifest = json.loads(applied_manifest_path.read_text(encoding="utf-8"))
    assert final_manifest.get("routing_global_state", {}).get("desired_mode") == "vpn", final_manifest
    assert final_manifest.get("global_preflight", {}).get("vpn_policy_required") is True, final_manifest
    assert final_manifest.get("vpn_contour", {}).get("required") is True, final_manifest
    assert_mihomo_launch_matches_active(stack["native"], active_config)
    code, final_mihomo = http_json(f"{api}/mihomo")
    assert code == 200 and final_mihomo.get("ok") is True, final_mihomo
    final_mihomo_state = final_mihomo["data"]["mihomo"]
    assert final_mihomo_state.get("runtime_state") == "running", final_mihomo_state
    final_selectors = (final_mihomo_state.get("details") or {}).get("selectors") or {}
    assert final_selectors.get("vpn_global_now") == "vpn-auto", final_selectors
    assert str(final_selectors.get("vpn_auto_now") or "").startswith("Provider VPN ["), final_selectors
    final_contours = ((final_mihomo_state.get("details") or {}).get("config") or {}).get("fwrouter_contours") or {}
    final_transparent_vpn = final_contours.get("transparent_vpn") or {}
    assert final_transparent_vpn.get("transparent_tcp_listener_socket_present") is True, final_transparent_vpn
    assert final_transparent_vpn.get("transparent_udp_listener_socket_present") is True, final_transparent_vpn
    final_binding = binding_for(source_ref)
    assert final_binding.get("current_member_id") == binding.get("current_member_id") == "901", final_binding
    assert final_binding.get("applied_member_id") == "901", final_binding
    assert final_binding.get("applied_revision") == final_binding.get("binding_revision"), final_binding


@pytest.mark.parametrize(
    "mode",
    ["patch_timeout", "patch_429", "patch_503", "patch_malformed"],
    ids=["patch-timeout", "patch-rate-limited", "patch-unavailable", "patch-malformed"],
)
def test_provider_patch_failure_preserves_last_good_binding_and_mihomo_apply(acceptance_stack, mode: str):
    stack = acceptance_stack
    api, bridge, native = stack["api"], stack["provider_bridge"], stack["native"]
    _enable_owned_mihomo_runtime_state()
    source_ref = _seed_source(stack)
    _configure_provider(api, source_ref)
    code, accepted = http_json(
        f"{api}/subscription/sources/{source_ref}/provider", method="POST", payload={"action": "enable"}
    )
    assert code == 200 and accepted.get("ok") is True, accepted
    initial_job = await_core_job(api, accepted)
    assert initial_job.get("status") == "success", initial_job
    from fwrouter_api.services.provider_managed import binding_for
    before_binding = binding_for(source_ref)
    before_config = (stack["state"] / "generated" / "mihomo" / "config.yaml").read_bytes()
    before_incarnation = native.rpc("mihomo_incarnation", {})
    assert before_incarnation.get("ok") is True, before_incarnation

    bridge.set_mode(mode)
    code, switch = http_json(
        f"{api}/subscription/sources/{source_ref}/provider", method="POST",
        payload={"action": "switch", "member_id": "902", "location_id": "6"},
    )
    assert code == 200 and switch.get("ok") is True, switch
    failed_job = await_core_job(api, switch)
    assert failed_job.get("status") == "failed", failed_job
    calls = bridge.snapshot_calls()
    patches = [row for row in calls if row[0] == "PATCH"]
    assert len(patches) == 1, calls

    after_binding = binding_for(source_ref)
    assert after_binding["current_member_id"] == before_binding["current_member_id"] == "901"
    assert after_binding["applied_member_id"] == before_binding["applied_member_id"] == "901"
    assert after_binding["applied_revision"] == before_binding["applied_revision"]
    assert (stack["state"] / "generated" / "mihomo" / "config.yaml").read_bytes() == before_config
    after_incarnation = native.rpc("mihomo_incarnation", {})
    assert after_incarnation.get("ok") is True and after_incarnation["details"] == before_incarnation["details"]


def test_provider_handoff_rejects_stale_selection_after_real_selector_wins_probe_race(acceptance_stack):
    """A Core selector change during unlocked real handoff validation fences the older apply."""
    stack = acceptance_stack
    api, bridge = stack["api"], stack["provider_bridge"]
    _enable_owned_mihomo_runtime_state()
    source_ref = _seed_source(stack)
    _configure_provider(api, source_ref)
    bridge.set_mode("normal")
    bridge.set_probe_available(True)

    code, enabled = http_json(
        f"{api}/subscription/sources/{source_ref}/provider", method="POST", payload={"action": "enable"}
    )
    assert code == 200 and enabled.get("ok") is True and enabled["data"].get("accepted") is True, enabled
    enabled_job = await_core_job(api, enabled)
    assert enabled_job.get("status") == "success", enabled_job

    # Add one independent, ordinary VLESS candidate through the canonical
    # parser and inventory persistence seam. It uses the owned Xray fixture's
    # real upstream; this setup does not claim to exercise subscription fetch.
    from fwrouter_api.adapters.subscription import parse_subscription_payload
    from fwrouter_api.services.subscription import _upsert_subscription_servers

    ordinary_url = "https://independent.example.test/acceptance"
    ordinary_payload = (
        "vless://88c7ce2a-465e-4e72-9c56-2a9e2fc84a51@127.0.0.1:5301"
        "?type=tcp&encryption=none#A-Independent-Core"
    )
    parsed = parse_subscription_payload(ordinary_payload)
    assert parsed.status.value == "success" and len(parsed.servers) == 1, parsed.to_dict()
    ordinary = parsed.servers[0]
    inventory = _upsert_subscription_servers(
        [ordinary], servers_by_url={ordinary_url: [ordinary]},
    )
    assert inventory["seen_count"] == 1, inventory

    # Make the ordinary candidate eligible through the public preferences API
    # before the handoff starts. The valid provider target remains active, so
    # the normal membership-change path reconciles inventory without switching.
    code, preferences = http_json(
        f"{api}/servers/{ordinary.server_id}/preferences", method="PATCH",
        payload={"vpn_auto": True, "vpn_auto_priority": 5},
    )
    assert code == 200 and preferences.get("ok") is True, preferences
    from fwrouter_api.services.provider_managed import binding_for
    binding_before_handoff = binding_for(source_ref)
    logical_id = binding_before_handoff["logical_server_id"]
    applied_member_before_handoff = binding_before_handoff["applied_member_id"]
    active = urllib.request.urlopen("http://127.0.0.1:5200/proxies/vpn-auto", timeout=3)
    with active:
        active_before = json.loads(active.read(256 * 1024))
    assert active_before.get("now", "").startswith("Provider VPN ["), active_before
    from fwrouter_api.services.selector import get_vpn_auto_state
    state_before = get_vpn_auto_state(read_only=True)
    assert state_before.get("active_auto_server_id") == logical_id, state_before
    assert ordinary.server_id in state_before.get("auto_selectable_candidate_ids", []), state_before

    bridge.hold_probe()

    try:
        code, accepted = http_json(
            f"{api}/subscription/sources/{source_ref}/provider", method="POST",
            payload={"action": "switch", "member_id": "902", "location_id": "6"},
        )
        assert code == 200 and accepted.get("ok") is True and accepted["data"].get("accepted") is True, accepted
        from fwrouter_api.services.vpn_auto_selection_state import read_selection_fence
        from fwrouter_api.db.connection import db_session

        assert bridge.probe_entered.wait(timeout=30), "real provider handoff did not reach the HTTP probe barrier"
        active = urllib.request.urlopen("http://127.0.0.1:5200/proxies/vpn-auto", timeout=3)
        with active:
            active_at_barrier = json.loads(active.read(256 * 1024))
        assert active_at_barrier.get("now", "").startswith("Provider VPN ["), active_at_barrier
        with db_session() as connection:
            fence_before = read_selection_fence(connection)

        # A second operation on the same provider source is still rejected
        # while the first operation owns its lock, without another provider
        # request or a change to the held operation's state.
        provider_calls_at_barrier = bridge.snapshot_calls()
        code, busy = http_json(
            f"{api}/subscription/sources/{source_ref}/provider", method="POST",
            payload={"action": "refresh"},
        )
        assert code == 200 and busy.get("ok") is False, busy
        assert busy.get("error", {}).get("code") == "SUBSCRIPTION_OPERATION_IN_PROGRESS", busy
        assert bridge.snapshot_calls() == provider_calls_at_barrier

        # This is a public Core selector request. The one held health request
        # stays at the external HTTP boundary. exclude_active removes the
        # verified active provider root, leaving the ordinary DB/runtime
        # candidate to be checked and applied through the real Core path.
        code, switched = http_json(
            f"{api}/selector/vpn-auto/switch", method="POST",
            payload={"confirm_switch": True, "exclude_active": True, "update_ping_state": False,
                     "limit": 20, "timeout_ms": 1000, "reason": "acceptance_provider_fence"},
            timeout=90,
        )
        assert code == 200 and switched.get("ok") is True, switched
        selector = switched.get("data", {}).get("selector", {})
        assert selector.get("ok") is True and selector.get("applied") is True, selector
        assert selector.get("selected_server_id") == ordinary.server_id, selector
        assert selector.get("active_after") == ordinary.server_id, selector
        assert selector.get("selected_member_id") is None, selector
        with db_session() as connection:
            fence_after = read_selection_fence(connection)
        assert fence_after["revision"] > fence_before["revision"], {"before": fence_before, "after": fence_after}
    finally:
        bridge.release_probe()

    stale_job = await_core_job(api, accepted)
    assert stale_job.get("status") == "failed", stale_job
    assert "VPN_AUTO_SELECTION_STALE_STATE" in json.dumps(stale_job), stale_job
    current = binding_for(source_ref)
    assert current["current_member_id"] == "902", current
    assert current["applied_member_id"] == applied_member_before_handoff, current
    controller = urllib.request.urlopen("http://127.0.0.1:5200/proxies/vpn-auto", timeout=3)
    with controller:
        response = json.loads(controller.read(256 * 1024))
    assert response.get("now") == ordinary.server_name, response


def test_provider_reentry_rejects_old_probe_after_real_mihomo_incarnation_change(acceptance_stack):
    stack = acceptance_stack
    api, bridge, native = stack["api"], stack["provider_bridge"], stack["native"]
    _enable_owned_mihomo_runtime_state()
    source_ref = _seed_source(stack)
    _configure_provider(api, source_ref)
    code, enable = http_json(f"{api}/subscription/sources/{source_ref}/provider", method="POST",
                             payload={"action": "enable"})
    assert code == 200 and enable.get("ok") is True, enable
    assert await_core_job(api, enable).get("status") == "success"
    from fwrouter_api.services.provider_managed import binding_for
    logical_id = binding_for(source_ref)["logical_server_id"]

    code, changed = http_json(f"{api}/subscription/sources/{source_ref}/provider/configuration", method="POST",
                              payload={"allow_automatic_member_switch": False})
    assert code == 200 and changed.get("ok") is True, changed
    bridge.set_probe_available(False)
    for decision_id in ("incarnation-failure-1", "incarnation-failure-2"):
        code, response = http_json(f"{api}/__acceptance/provider/recovery", method="POST",
                                   payload={"logical_server_id": logical_id, "decision_id": decision_id,
                                            "timeout_ms": 1000, "allow_switch": True}, timeout=90)
        assert code == 200 and isinstance(response, dict), response
    code, direct = http_json(f"{api}/__acceptance/provider/recovery", method="POST",
                             payload={"logical_server_id": logical_id, "decision_id": "incarnation-failure-3",
                                      "timeout_ms": 1000, "allow_switch": True}, timeout=90)
    assert code == 200 and direct.get("status") == "emergency_direct", direct
    assert direct.get("effective_override") == "emergency_direct", direct

    bridge.set_probe_available(True)
    native.hold_action("mihomo_probe_result")
    result_box: dict[str, object] = {}
    probe_response_count = len(bridge.snapshot_probe_responses())

    def request_reentry() -> None:
        status_code, payload = http_json(
            f"{api}/__acceptance/provider/reentry", method="POST", payload={"timeout_ms": 1500}, timeout=60
        )
        result_box["status_code"] = status_code
        result_box["payload"] = payload

    request_thread = threading.Thread(target=request_reentry, name="acceptance-provider-reentry")
    request_thread.start()
    try:
        assert native.action_entered.wait(timeout=30), "reentry did not finish a real Mihomo health probe"
        probe_evidence = native.last_mihomo_probe_result
        assert probe_evidence is not None and probe_evidence.get("probe_ok") is True, probe_evidence
        assert bridge.snapshot_probe_responses()[probe_response_count:] and bridge.snapshot_probe_responses()[-1] == 204
        before_incarnation = native.rpc("mihomo_incarnation", {})
        assert before_incarnation.get("ok") is True, before_incarnation
        native.restart_mihomo_child()
        after_incarnation = native.rpc("mihomo_incarnation", {})
        assert after_incarnation.get("ok") is True, after_incarnation
        assert after_incarnation["details"] != before_incarnation["details"]
    finally:
        native.release_action()
        request_thread.join(timeout=65)
    assert not request_thread.is_alive(), "reentry request thread did not terminate after probe release"
    code, stale = result_box.get("status_code"), result_box.get("payload")
    assert code == 200 and isinstance(stale, dict), result_box
    assert stale.get("ok") is False and stale.get("error_code") == "provider_recovery_stale", stale
    assert stale.get("effective_override") == "emergency_direct", stale
    code, projection = http_json(f"{api}/subscription")
    assert code == 200 and projection["data"]["subscription"]["provider_managed"]["effective_override"] == "emergency_direct", projection


def test_provider_reentry_fences_concurrent_public_exclusive_intent_change(acceptance_stack):
    stack = acceptance_stack
    api, bridge, native = stack["api"], stack["provider_bridge"], stack["native"]
    _enable_owned_mihomo_runtime_state()
    source_ref = _seed_source(stack)
    _configure_provider(api, source_ref)
    code, enable = http_json(f"{api}/subscription/sources/{source_ref}/provider", method="POST",
                             payload={"action": "enable"})
    assert code == 200 and enable.get("ok") is True, enable
    assert await_core_job(api, enable).get("status") == "success"
    code, exclusive = http_json(f"{api}/subscription/sources/{source_ref}/vpn-auto-exclusive",
                                method="POST", payload={"enabled": True})
    assert code == 200 and exclusive.get("ok") is True and exclusive["data"].get("accepted") is True, exclusive
    assert await_core_job(api, exclusive).get("status") == "success"
    from fwrouter_api.services.provider_managed import binding_for
    from fwrouter_api.services.vpn_auto_exclusive import get_vpn_auto_exclusive_source_ref
    from fwrouter_api.services.vpn_auto_selection_state import read_selection_fence
    from fwrouter_api.db.connection import db_session
    logical_id = binding_for(source_ref)["logical_server_id"]

    code, policy = http_json(f"{api}/subscription/sources/{source_ref}/provider/configuration", method="POST",
                              payload={"allow_automatic_member_switch": False})
    assert code == 200 and policy.get("ok") is True, policy
    bridge.set_probe_available(False)
    for decision_id in ("exclusive-race-failure-1", "exclusive-race-failure-2"):
        code, response = http_json(f"{api}/__acceptance/provider/recovery", method="POST",
                                   payload={"logical_server_id": logical_id, "decision_id": decision_id,
                                            "timeout_ms": 1000, "allow_switch": True}, timeout=90)
        assert code == 200 and response.get("provider_recovery") is True, response
    code, direct = http_json(f"{api}/__acceptance/provider/recovery", method="POST",
                             payload={"logical_server_id": logical_id, "decision_id": "exclusive-race-failure-3",
                                      "timeout_ms": 1000, "allow_switch": True}, timeout=90)
    assert code == 200 and direct.get("status") == "emergency_direct", direct
    assert get_vpn_auto_exclusive_source_ref() == source_ref
    bridge.set_probe_available(True)

    with db_session() as connection:
        fence_before = read_selection_fence(connection)
    native.hold_action("mihomo_probe_result")
    result_box: dict[str, object] = {}
    probe_response_count = len(bridge.snapshot_probe_responses())

    def request_reentry() -> None:
        status_code, payload = http_json(
            f"{api}/__acceptance/provider/reentry", method="POST", payload={"timeout_ms": 1500}, timeout=60
        )
        result_box["status_code"] = status_code
        result_box["payload"] = payload

    request_thread = threading.Thread(target=request_reentry, name="acceptance-exclusive-reentry-race")
    request_thread.start()
    try:
        assert native.action_entered.wait(timeout=30), "reentry did not reach the post-response probe barrier"
        probe_evidence = native.last_mihomo_probe_result
        assert probe_evidence is not None and probe_evidence.get("probe_ok") is True, probe_evidence
        assert bridge.snapshot_probe_responses()[probe_response_count:] and bridge.snapshot_probe_responses()[-1] == 204
        # The external probe is complete and successful, while Core has not
        # consumed its evidence. Public intent mutation must complete without
        # holding the probe/writer guard; the older reentry then fails its CAS.
        code, disabled = http_json(f"{api}/subscription/sources/{source_ref}/vpn-auto-exclusive",
                                   method="POST", payload={"enabled": False})
        assert code == 200 and disabled.get("ok") is True and disabled["data"].get("accepted") is True, disabled
        disabled_job = await_core_job(api, disabled)
        assert disabled_job.get("status") == "success", disabled_job
        assert get_vpn_auto_exclusive_source_ref() is None
        with db_session() as connection:
            fence_after = read_selection_fence(connection)
        assert fence_after["revision"] > fence_before["revision"], {"before": fence_before, "after": fence_after}
    finally:
        native.release_action()
        request_thread.join(timeout=65)
    assert not request_thread.is_alive(), "reentry request did not leave its post-response barrier"
    assert result_box.get("status_code") == 200, result_box
    stale = result_box.get("payload")
    assert isinstance(stale, dict) and stale.get("ok") is False, stale
    assert stale.get("status") == "provider_recovery_stale" and stale.get("error_code") == "provider_recovery_stale", stale
    assert stale.get("effective_override") == "emergency_direct", stale
    code, projection = http_json(f"{api}/subscription")
    assert code == 200 and projection["data"]["subscription"]["provider_managed"]["effective_override"] == "emergency_direct", projection


@pytest.mark.parametrize(
    ("mode", "expected"),
    [("timeout", "PROVIDER_TIMEOUT"), ("429", "PROVIDER_RATE_LIMIT"),
     ("503", "PROVIDER_UNAVAILABLE"), ("malformed", "PROVIDER_INVALID_RESPONSE")],
    ids=["timeout", "rate-limited", "unavailable-503", "malformed-json"],
)
def test_provider_http_failures_are_typed_and_preserve_configuration(acceptance_stack, mode: str, expected: str):
    stack = acceptance_stack
    api, bridge = stack["api"], stack["provider_bridge"]
    source_ref = _seed_source(stack)
    binding_before = _configure_provider(api, source_ref)
    bridge.set_mode(mode)
    code, failed = http_json(f"{api}/subscription/sources/{source_ref}/provider/configs", method="POST", payload={}, timeout=12)
    assert code == 200 and failed.get("ok") is False, failed
    assert failed.get("error", {}).get("code") == expected, failed
    from fwrouter_api.services.provider_managed import binding_for
    binding_after = binding_for(source_ref)
    assert binding_after["binding_revision"] == binding_before["binding_revision"]
    assert binding_after["resource_id"] == "42"
    assert not any(method == "PATCH" for method, _path, _query, _body in bridge.snapshot_calls())


@pytest.mark.parametrize(
    ("mode", "evidence", "provider_error"),
    [
        ("timeout", "provider_api_timeout", "PROVIDER_TIMEOUT"),
        ("429", "provider_api_rate_limited", "PROVIDER_RATE_LIMIT"),
        ("503", "provider_api_5xx", "PROVIDER_UNAVAILABLE"),
        ("malformed", "provider_response_unknown", "PROVIDER_INVALID_RESPONSE"),
    ],
    ids=["recovery-timeout", "recovery-rate-limited", "recovery-unavailable", "recovery-malformed"],
)
def test_confirmed_recovery_typed_provider_api_errors_are_unknown_not_member_down(
    acceptance_stack, mode: str, evidence: str, provider_error: str,
):
    stack = acceptance_stack
    api, bridge = stack["api"], stack["provider_bridge"]
    _enable_owned_mihomo_runtime_state()
    source_ref = _seed_source(stack)
    _configure_provider(api, source_ref)
    code, enable = http_json(f"{api}/subscription/sources/{source_ref}/provider", method="POST",
                             payload={"action": "enable"})
    assert code == 200 and enable.get("ok") is True, enable
    assert await_core_job(api, enable).get("status") == "success"
    from fwrouter_api.services.provider_managed import binding_for
    binding = binding_for(source_ref)
    logical_id = binding["logical_server_id"]

    # The first distinct decision performs a targeted refresh. Its local probe
    # then fails against the real VLESS→Xray→HTTP path, leaving phase two ready
    # for a separate provider status observation.
    bridge.set_mode("normal")
    bridge.set_probe_available(False)
    code, first = http_json(
        f"{api}/__acceptance/provider/recovery", method="POST",
        payload={"logical_server_id": logical_id, "decision_id": f"{mode}-recovery-1",
                 "timeout_ms": 1000, "allow_switch": True}, timeout=90,
    )
    assert code == 200 and first.get("provider_confirmation") == 1, first
    before_fault = binding_for(source_ref)
    bridge.calls.clear()
    bridge.set_mode(mode)
    code, fault = http_json(
        f"{api}/__acceptance/provider/recovery", method="POST",
        payload={"logical_server_id": logical_id, "decision_id": f"{mode}-recovery-2",
                 "timeout_ms": 1000, "allow_switch": True}, timeout=20,
    )
    assert code == 200 and fault.get("provider_confirmation") == 2, fault
    assert fault.get("outcome") == "unknown", fault
    assert fault.get("error_code") == evidence, fault
    assert fault.get("provider_error_code") == provider_error, fault
    assert fault.get("switch_attempted") is False and fault.get("last_good_retained") is True, fault
    assert fault.get("status") != "emergency_direct" and fault.get("effective_override") is None, fault
    calls = bridge.snapshot_calls()
    assert calls and all(method == "GET" for method, _path, _query, _body in calls), calls
    assert any(path.endswith("/serverStats") for _method, path, _query, _body in calls), calls
    assert not any(path == "/configs/available-servers" for _method, path, _query, _body in calls), calls
    assert not any(method == "PATCH" for method, _path, _query, _body in calls), calls
    after_fault = binding_for(source_ref)
    for key in ("current_member_id", "applied_member_id", "applied_revision", "binding_revision"):
        assert after_fault[key] == before_fault[key], {"field": key, "before": before_fault, "after": after_fault}


@pytest.mark.parametrize(("mode", "expected_job"), [("down", "failed"), ("unknown", "success")],
                         ids=["explicit-down-rejected", "unknown-status-neutral"])
def test_provider_status_controls_real_core_apply_and_native_mihomo_parity(acceptance_stack, mode: str, expected_job: str):
    stack = acceptance_stack
    api, bridge = stack["api"], stack["provider_bridge"]
    _enable_owned_mihomo_runtime_state()
    source_ref = _seed_source(stack)
    binding = _configure_provider(api, source_ref)
    bridge.set_mode(mode)
    before_config = (stack["state"] / "generated" / "mihomo" / "config.yaml").read_bytes()
    code, accepted = http_json(
        f"{api}/subscription/sources/{source_ref}/provider", method="POST",
        payload={"action": "enable"},
    )
    assert code == 200 and accepted.get("ok") is True and accepted["data"].get("accepted") is True, accepted
    job = await_core_job(api, accepted)
    assert job.get("status") == expected_job, job
    calls = bridge.snapshot_calls()
    assert any(method == "GET" and path == "/configs/available-servers" for method, path, _query, _body in calls), calls
    if mode == "down":
        assert not any(method == "PATCH" for method, _path, _query, _body in calls), calls
        assert (stack["state"] / "generated" / "mihomo" / "config.yaml").read_bytes() == before_config
        return

    # Unknown provider health remains neutral. A successful Core operation
    # must persist its intent, apply through the real Mihomo controller, and
    # match the child process's exact launch config and selector readback.
    assert job.get("status") == "success", job
    from fwrouter_api.services.provider_managed import binding_for
    applied = binding_for(source_ref)
    assert applied["current_member_id"] == "901"
    assert applied["applied_member_id"] == "901"
    assert applied["applied_revision"] == applied["binding_revision"]
    active_path = stack["state"] / "generated" / "mihomo" / "config.yaml"
    assert_mihomo_launch_matches_active(stack["native"], active_path)
    def controller_json(path: str) -> dict:
        with urllib.request.urlopen("http://127.0.0.1:5200" + path, timeout=3) as response:
            assert response.status == 200
            return json.loads(response.read(256 * 1024))

    global_group = controller_json("/proxies/vpn-global")
    auto_group = controller_json("/proxies/vpn-auto")
    # Provider profile is the real nested vpn-auto member; vpn-global keeps
    # its canonical virtual-selector target.
    assert global_group.get("now") == "vpn-auto", global_group
    assert str(auto_group.get("now") or "").startswith("Provider VPN ["), auto_group
    code, state = http_json(f"{api}/state/vpn")
    assert code == 200 and state.get("ok") is True, state
    vpn_state = state["data"]["vpn"]
    assert vpn_state["effective"]["server_health"]["active_matches_selected"] is True, vpn_state
    assert vpn_state["effective"]["server_health"]["active"]["server_id"] == applied["logical_server_id"], vpn_state
