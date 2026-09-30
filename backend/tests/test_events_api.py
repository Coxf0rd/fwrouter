from __future__ import annotations

from fastapi.testclient import TestClient
import json

from fwrouter_api.core.config import get_settings
from fwrouter_api.main import create_app
from fwrouter_api.services.events import list_recent_events, write_audit_event, write_diagnostic_event
from fwrouter_api.services.events import safe_human_label, write_operational_event
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


def test_safe_human_label_rejects_url_token_and_opaque_identity() -> None:
    assert safe_human_label("MacBook Air — Afonin") == "MacBook Air — Afonin"
    assert safe_human_label("https://example.test/client/token") is None
    assert safe_human_label("private-token-alias") is None
    assert safe_human_label("123e4567-e89b-12d3-a456-426614174000") is None


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


def test_event_category_filter_is_applied_before_limit() -> None:
    audit = write_audit_event(
        actor="admin", source="api", action="preferences_changed",
        event_code="server.preferences_changed", entity_type="server", entity_id="srv-category",
        previous_value={"vpn_auto": False}, new_value={"vpn_auto": True},
        details={"changed_fields": ["vpn_auto"]},
    )
    write_operational_event(
        severity="info", event_type="routine_status", event_code="routine_status",
        message="newer diagnostic", details={"event_category": "diagnostic"},
    )
    with events_route.db_session() as connection:
        connection.execute("UPDATE operational_logs SET created_at = '2026-09-29 00:00:00' WHERE event_id = ?", (audit.event_id,))
        connection.execute("UPDATE operational_logs SET created_at = '2026-09-29 00:01:00' WHERE event_type = 'routine_status'")

    client = TestClient(create_app(enable_startup_tasks=False))
    payload = client.get("/api/v2/events/recent?type=audit&limit=1").json()

    assert len(payload["audit"]) == 1
    assert payload["audit"][0]["event_code"] == "server.preferences_changed"
    assert payload["operational"] == []
    assert payload["diagnostic"] == []


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


def test_summary_infers_stable_audit_entity_and_sanitizes_alias_snapshots(monkeypatch) -> None:
    with events_route.db_session() as connection:
        connection.execute(
            "INSERT INTO servers (server_id, server_name, inventory_state) VALUES ('legacy-server-id', 'Edge Europe', 'active')"
        )
    monkeypatch.setattr(events_route, "list_recent_events", lambda **_: {
        "audit": [
            {
                "event_id": "legacy-server-preferences",
                "event_code": "server.preferences_changed",
                "entity_id": "legacy-server-id",
                "details": {"previous_value": {"vpn_auto": False}, "new_value": {"vpn_auto": True}},
            },
            {
                "event_id": "unsafe-alias-history",
                "event_code": "client.alias_changed",
                "entity_type": "subject",
                "entity_id": "subject:opaque-id",
                "details": {
                    "previous_value": {"alias_present": True, "alias_label": "Safe old name"},
                    "new_value": {"alias_present": True, "alias_label": "https://example.test/token"},
                },
            },
        ],
        "operational": [],
        "diagnostic": [],
    })

    rows = events_route.list_recent_events_endpoint(view="summary")["audit"]

    assert rows[0]["entity_type"] == "server"
    assert rows[0]["entity_label"] == "Edge Europe"
    assert rows[0]["entity_label_source"] == "current"
    assert rows[1]["details"]["previous_value"]["alias_label"] == "Safe old name"
    assert "alias_label" not in rows[1]["details"]["new_value"]


def test_event_label_snapshot_precedes_current_server_name(monkeypatch) -> None:
    with events_route.db_session() as connection:
        connection.execute(
            "INSERT INTO servers (server_id, server_name, inventory_state) VALUES ('snapshot-source-server', 'Current Name', 'active')"
        )
    monkeypatch.setattr(events_route, "list_recent_events", lambda **_: {
        "audit": [{
            "event_id": "server-snapshot", "event_code": "server.preferences_changed",
            "entity_type": "server", "entity_id": "snapshot-source-server",
            "details": {"entity_label": "Event Time Name"},
        }],
        "operational": [], "diagnostic": [],
    })

    event = events_route.list_recent_events_endpoint(view="summary")["audit"][0]

    assert event["entity_label"] == "Event Time Name"
    assert event["entity_label_source"] == "event_snapshot"


def test_summary_exposes_changed_fields_and_safe_membership_objects(monkeypatch) -> None:
    with events_route.db_session() as connection:
        connection.executemany(
            "INSERT INTO servers (server_id, server_name, inventory_state) VALUES (?, ?, 'active')",
            [("journal-added-id", "Edge Europe"), ("journal-removed-id", "Edge West")],
        )
    monkeypatch.setattr(events_route, "list_recent_events", lambda **_: {
        "audit": [{
            "event_id": "membership-audit",
            "event_code": "server.vpn_auto_membership_changed",
            "event_class": "audit",
            "entity_type": "server_assignment",
            "entity_id": "vpn-auto",
            "previous_value": {"server_ids": ["journal-removed-id"]},
            "new_value": {"server_ids": ["journal-added-id"]},
            "details": {"changed_fields": ["vpn_auto"], "changed_count": 2},
        }],
        "operational": [],
        "diagnostic": [],
    })

    event = events_route.list_recent_events_endpoint(view="summary")["audit"][0]

    assert event["details"]["changed_fields"] == ["vpn_auto"]
    assert event["details"]["objects_added"] == ["Edge Europe"]
    assert event["details"]["objects_removed"] == ["Edge West"]
    assert "journal-added-id" not in str(event["details"])
    assert "journal-removed-id" not in str(event["details"])


def test_membership_event_snapshot_survives_server_rename_and_delete() -> None:
    with events_route.db_session() as connection:
        connection.executemany(
            "INSERT INTO servers (server_id, server_name, inventory_state) VALUES (?, ?, 'active')",
            [("snapshot-added-id", "Snapshot Added"), ("snapshot-removed-id", "Snapshot Removed")],
        )
        event = write_audit_event(
            actor="admin", source="api", action="vpn_auto_membership_changed",
            event_code="server.vpn_auto_membership_changed", entity_type="server_assignment",
            entity_id="vpn-auto", previous_value={"server_ids": ["snapshot-removed-id"]},
            new_value={"server_ids": ["snapshot-added-id"]}, connection=connection,
        )
    with events_route.db_session() as connection:
        connection.execute("UPDATE servers SET server_name = 'Renamed' WHERE server_id = 'snapshot-added-id'")
        connection.execute("DELETE FROM servers WHERE server_id = 'snapshot-removed-id'")
    client = TestClient(create_app(enable_startup_tasks=False))

    summary = client.get("/api/v2/events/recent?type=audit&view=summary").json()
    projected = next(item for item in summary["audit"] if item["event_id"] == event.event_id)

    assert projected["details"]["objects_added"] == ["Snapshot Added"]
    assert projected["details"]["objects_removed"] == ["Snapshot Removed"]
    assert projected["details"]["objects_label_source"] == "event_snapshot"
    assert "snapshot-added-id" not in json.dumps(projected["details"])


def test_summary_only_exposes_allowlisted_job_type(monkeypatch) -> None:
    monkeypatch.setattr(events_route, "list_recent_events", lambda **_: {
        "audit": [],
        "operational": [{
            "event_id": "known-job", "event_type": "job_handler_exception",
            "details": {"job_type": "subscription_refresh", "error": "traceback / secret text"},
        }, {
            "event_id": "unknown-job", "event_type": "job_handler_exception",
            "details": {"job_type": "arbitrary_custom_job", "error": "traceback / secret text"},
        }],
        "diagnostic": [],
    })

    rows = events_route.list_recent_events_endpoint(view="summary")["operational"]

    assert rows[0]["details"]["job_type"] == "subscription_refresh"
    assert "error" not in rows[0]["details"]
    assert "job_type" not in rows[1]["details"]


def test_auto_switch_summary_exposes_safe_event_time_transition(monkeypatch) -> None:
    monkeypatch.setattr(events_route, "list_recent_events", lambda **_: {
        "audit": [], "diagnostic": [],
        "operational": [{
            "event_id": "auto-switch", "event_type": "vpn_auto_server_switched",
            "event_code": "vpn_auto_server_switched",
            "details": {
                "active_before": "internal-id-a", "active_after": "internal-id-b",
                "previous_value": {"server_id": "internal-id-a", "server_label": "Old Server"},
                "new_value": {"server_id": "internal-id-b", "server_label": "New Server"},
                "reason_code": "watchdog_failover", "result": "success", "source": "selector",
                "requested_by": "admin:operator",
            },
        }],
    })

    event = events_route.list_recent_events_endpoint(view="summary")["operational"][0]

    assert event["details"]["previous_value"] == {"server_label": "Old Server"}
    assert event["details"]["new_value"] == {"server_label": "New Server"}
    assert event["details"]["reason_code"] == "watchdog_failover"
    assert event["details"]["result"] == "success"
    assert event["details"]["source"] == "selector"
    assert event["actor"] == "admin:operator"
    assert event["actor_attribution"] == "caller_supplied_unverified"
    assert "requested_by" not in event["details"]
    assert "internal-id" not in json.dumps(event)


def test_exact_event_lookup_reads_sqlite_event_outside_recent_window() -> None:
    old = write_audit_event(
        actor="admin", source="api", action="old_change", event_code="config_change",
        entity_type="configuration", entity_id="old-event", details={"evidence": {"nested": "retained"}},
    )
    with events_route.db_session() as connection:
        connection.execute(
            "UPDATE operational_logs SET created_at = '2020-01-01 00:00:00' WHERE event_id = ?",
            (old.event_id,),
        )
    write_audit_event(actor="admin", source="api", action="new_change", event_code="config_change")
    client = TestClient(create_app(enable_startup_tasks=False))

    recent = client.get("/api/v2/events/recent?type=audit&limit=1").json()
    exact = client.get(f"/api/v2/events/{old.event_id}").json()

    assert all(item["event_id"] != old.event_id for item in recent["audit"])
    assert exact["found"] is True
    assert exact["event_id"] == old.event_id
    assert exact["event"]["details"]["evidence"] == {"nested": "retained"}
    assert exact["event"]["details"]["record_source"] == "operational_sqlite"

    with events_route.db_session() as connection:
        connection.execute(
            """INSERT INTO operational_logs (event_id, level, event_type, message, details_json, created_at)
               VALUES (NULL, 'warning', 'legacy_runtime_failed', 'Old legacy failure', '{}', '2019-01-01 00:00:00')"""
        )
    legacy = next(
        item for item in list_recent_events(event_category="operational")["operational"]
        if item["event_type"] == "legacy_runtime_failed"
    )
    recovered_legacy = client.get(f"/api/v2/events/{legacy['event_id']}").json()
    assert recovered_legacy["found"] is True
    assert recovered_legacy["event"]["event_id"] == legacy["event_id"]
    assert recovered_legacy["event"]["details"]["record_source"] == "operational_sqlite"


def test_exact_event_lookup_reads_technical_jsonl_and_legacy_deterministic_id() -> None:
    event = write_diagnostic_event(
        component="exact-lookup", severity="warning", event_type="legacy_technical_probe",
        event_code="legacy_technical_probe", message="Probe", details={"nested": {"evidence": "kept"}},
    )
    client = TestClient(create_app(enable_startup_tasks=False))
    full = client.get(f"/api/v2/events/{event.event_id}").json()
    assert full["found"] is True
    assert full["event"]["details"]["nested"] == {"evidence": "kept"}
    assert full["event"]["details"]["record_source"] == "technical_jsonl"

    path = get_settings().paths.technical_log_dir / "legacy-ids.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "timestamp": "2020-01-01T00:00:00Z", "severity": "info",
            "event_type": "legacy_jsonl_without_id", "component": "legacy-ids",
            "message": "Legacy event", "details": {"nested": "legacy"},
        }) + "\n")
    legacy = next(
        item for item in list_recent_events(event_category="diagnostic")["diagnostic"]
        if item["event_type"] == "legacy_jsonl_without_id"
    )
    recovered = client.get(f"/api/v2/events/{legacy['event_id']}").json()
    assert recovered["found"] is True
    assert recovered["event"]["event_id"] == legacy["event_id"]
    assert recovered["event"]["details"]["record_source"] == "technical_jsonl"
