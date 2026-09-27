from __future__ import annotations

from fastapi.testclient import TestClient

from fwrouter_api.main import create_app
from fwrouter_api.services.events import write_audit_event, write_diagnostic_event
from fwrouter_api.services.events import write_operational_event
from fwrouter_api.routes import events as events_route


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


def test_events_summary_only_exposes_safe_entity_aliases(monkeypatch) -> None:
    monkeypatch.setattr(events_route, "list_recent_events", lambda **_: {
        "audit": [],
        "operational": [
            {"event_id": "safe", "entity_id": "subject:uuid-1", "details": {"display_name": "NikitaPlus"}},
            {"event_id": "secret", "entity_id": "subject:uuid-2", "details": {"alias": "https://example.test/s/private-token"}},
            {"event_id": "opaque", "entity_id": "subject:uuid-3", "display_name": "a" * 40},
            {"event_id": "generated", "entity_id": "subject:uuid-4", "alias": "fwrouter-e2e-1789020266"},
            {"event_id": "credential", "entity_id": "subject:uuid-5", "display_name": "subscription token secret"},
        ],
        "diagnostic": [],
    })

    payload = events_route.list_recent_events_endpoint(view="summary")
    rows = payload["operational"]
    assert rows[0]["entity_label"] == "NikitaPlus"
    assert "entity_label" not in rows[1]
    assert "entity_label" not in rows[2]
    assert "entity_label" not in rows[3]
    assert "entity_label" not in rows[4]
    assert all(row["entity_id"].startswith("subject:") for row in rows)


def test_events_summary_exposes_only_safe_member_status_enums(monkeypatch) -> None:
    monkeypatch.setattr(events_route, "list_recent_events", lambda **_: {
        "audit": [],
        "operational": [
            {"event_id": "safe-transition", "event_code": "HEALTH_MEMBER_STATE_CHANGED", "details": {"old_status": "failed", "new_status": "healthy"}},
            {"event_id": "unsafe-transition", "event_code": "HEALTH_MEMBER_STATE_CHANGED", "details": {"old_status": "secret-token-value", "new_status": "healthy"}},
            {"event_id": "unrelated-transition", "event_code": "unrelated.changed", "details": {"old_status": "failed", "new_status": "healthy"}},
        ],
        "diagnostic": [],
    })

    rows = events_route.list_recent_events_endpoint(view="summary")["operational"]

    assert rows[0]["details"] == {"old_status": "failed", "new_status": "healthy"}
    assert rows[1]["details"] == {"new_status": "healthy"}
    assert rows[2]["details"] == {}
