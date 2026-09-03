from __future__ import annotations

from fwrouter_api.services.events import classify_event


def test_classify_event_explicit_categories() -> None:
    assert classify_event("user_action") == "audit"
    assert classify_event("config_change") == "audit"
    assert classify_event("manual_apply") == "audit"
    assert classify_event("apply_started") == "operational"
    assert classify_event("apply_finished") == "operational"
    assert classify_event("runtime_failed") == "operational"
    assert classify_event("reconcile_drift") == "operational"
    assert classify_event("failover") == "operational"
    assert classify_event("probe_result") == "diagnostic"
    assert classify_event("debug_dump") == "diagnostic"
    assert classify_event("materialization_details") == "diagnostic"


def test_classify_legacy_events() -> None:
    assert classify_event("mutation_set_global_mode_success") == "audit"
    assert classify_event("core_bypass_enabled") == "audit"
    assert classify_event("routing_live_drift_detected") == "operational"
    assert classify_event("vpn_auto_server_switched") == "operational"
    assert classify_event("xray_binding_materialization_failed") == "operational"
    assert classify_event("runtime_enforcement_probe_failed") == "diagnostic"
    assert classify_event("xray_binding_materialized") == "diagnostic"


def test_details_can_override_event_category() -> None:
    assert classify_event("custom_event", details={"event_category": "diagnostic"}) == "diagnostic"
