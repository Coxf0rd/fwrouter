from __future__ import annotations

from fastapi.testclient import TestClient

from fwrouter_api.main import create_app
from fwrouter_api.services.events import write_audit_event, write_diagnostic_event
from fwrouter_api.services.events import write_operational_event


def test_events_recent_endpoint_returns_audit_operational_and_diagnostic() -> None:
    write_audit_event(
        actor="user:admin",
        source="api",
        action="config_change",
        entity_type="module",
        entity_id="vpn",
        result="success",
    )
    write_operational_event(
        severity="warning",
        event_type="reconcile_drift",
        message="Routing drift.",
        entity_type="routing",
        entity_id="global",
        reconcile_state="drift",
    )
    write_diagnostic_event(
        component="dataplane",
        severity="debug",
        event_type="probe_result",
        message="Probe payload.",
    )
    client = TestClient(create_app(enable_startup_tasks=False))

    response = client.get("/api/v2/events/recent")

    assert response.status_code == 200
    payload = response.json()
    assert payload["audit"][0]["action"] == "config_change"
    assert payload["operational"][0]["event_type"] == "reconcile_drift"
    assert payload["diagnostic"][0]["event_type"] == "probe_result"
    assert payload["summary"]["last_drift"]["event_type"] == "reconcile_drift"


def test_events_recent_endpoint_filters_type_and_entity_id() -> None:
    write_operational_event(
        severity="error",
        event_type="runtime_failed",
        message="VPN failed.",
        entity_type="vpn",
        entity_id="vpn",
    )
    write_operational_event(
        severity="warning",
        event_type="reconcile_drift",
        message="Routing drift.",
        entity_type="routing",
        entity_id="global",
    )
    client = TestClient(create_app(enable_startup_tasks=False))

    response = client.get("/api/v2/events/recent?type=operational&entity_id=vpn")

    assert response.status_code == 200
    payload = response.json()
    assert payload["audit"] == []
    assert payload["diagnostic"] == []
    assert len(payload["operational"]) == 1
    assert payload["operational"][0]["entity_id"] == "vpn"
