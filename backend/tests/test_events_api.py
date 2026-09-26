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
        event_code="config_change",
        entity_type="module",
        entity_id="vpn",
        result="success",
    )
    write_operational_event(
        severity="warning",
        event_type="reconcile_drift",
        event_code="reconcile_drift",
        message="Routing drift.",
        entity_type="routing",
        entity_id="global",
        reconcile_state="drift",
    )
    write_diagnostic_event(
        component="dataplane",
        severity="debug",
        event_type="probe_result",
        event_code="probe_result",
        message="Probe payload.",
    )
    client = TestClient(create_app(enable_startup_tasks=False))

    response = client.get("/api/v2/events/recent")

    assert response.status_code == 200
    payload = response.json()
    assert payload["audit"][0]["action"] == "config_change"
    assert payload["audit"][0]["event_code"] == "config_change"
    assert payload["audit"][0]["schema_version"] == 2
    assert payload["operational"][0]["event_type"] == "reconcile_drift"
    assert payload["operational"][0]["event_code"] == "reconcile_drift"
    assert payload["operational"][0]["schema_version"] == 2
    assert payload["diagnostic"][0]["event_type"] == "probe_result"
    assert payload["summary"]["last_drift"]["event_type"] == "reconcile_drift"


def test_events_recent_endpoint_filters_type_and_entity_id() -> None:
    write_operational_event(
        severity="error",
        event_type="runtime_failed",
        event_code="runtime_failed",
        message="VPN failed.",
        entity_type="vpn",
        entity_id="vpn",
    )
    write_operational_event(
        severity="warning",
        event_type="reconcile_drift",
        event_code="reconcile_drift",
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


def test_events_summary_view_preserves_individual_ids_without_bulky_details() -> None:
    for index in (1, 2):
        write_operational_event(
            severity="warning", event_type="runtime_failed", event_code="runtime_failed",
            message="Runtime failed", entity_type="vpn", entity_id="vpn",
            details={"error_code": "RUNTIME_TIMEOUT", "provider_payload": "x" * 10000, "workflow_id": "flow-1"},
        )
    client = TestClient(create_app(enable_startup_tasks=False))

    payload = client.get("/api/v2/events/recent?view=summary").json()

    rows = [item for item in payload["operational"] if item.get("event_type") == "runtime_failed"]
    assert len(rows) == 2
    assert len({item["event_id"] for item in rows}) == 2
    assert all(item["details"]["error_code"] == "RUNTIME_TIMEOUT" for item in rows)
    assert all("provider_payload" not in item["details"] for item in rows)
    assert "summary" not in payload
