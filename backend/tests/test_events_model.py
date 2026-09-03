from __future__ import annotations

from fwrouter_api.services.events import (
    AuditEvent,
    DiagnosticEvent,
    OperationalEvent,
    adapt_legacy_event,
    log_event,
    summarize_events,
)


def test_event_models_expose_expected_first_class_fields() -> None:
    audit = AuditEvent(
        event_id="audit-1",
        timestamp="2026-09-04T00:00:00+00:00",
        actor="user:admin",
        source="api",
        request_id="req-1",
        action="config_change",
        entity_type="module",
        entity_id="vpn",
        result="success",
    )
    operational = OperationalEvent(
        event_id="op-1",
        timestamp="2026-09-04T00:00:01+00:00",
        severity="warning",
        event_type="reconcile_drift",
        entity_type="routing",
        entity_id="global",
        job_id="job-1",
        apply_id="apply-1",
        reconcile_state="drift",
        message="Routing drift detected.",
    )
    diagnostic = DiagnosticEvent(
        event_id="diag-1",
        timestamp="2026-09-04T00:00:02+00:00",
        severity="debug",
        event_type="probe_result",
        component="dataplane",
        message="Raw probe payload.",
    )

    assert audit.request_id == "req-1"
    assert operational.job_id == "job-1"
    assert operational.apply_id == "apply-1"
    assert diagnostic.component == "dataplane"


def test_legacy_log_event_adapter_keeps_old_call_shape() -> None:
    event = log_event(
        event_type="apply_finished",
        message="Apply completed.",
        level="info",
        subject_id="lan:laptop",
        details={"job_id": "job-1", "apply_id": "apply-1", "result": "success"},
    )

    assert isinstance(event, OperationalEvent)
    assert event.entity_id == "lan:laptop"
    assert event.job_id == "job-1"
    assert event.apply_id == "apply-1"


def test_adapt_legacy_event_maps_mutation_to_audit() -> None:
    event = adapt_legacy_event(
        {
            "event_id": "event-1",
            "created_at": "2026-09-04T00:00:00+00:00",
            "level": "info",
            "event_type": "mutation_set_global_mode_success",
            "subject_id": None,
            "message": "Mode changed.",
            "details": {"requested_by": "user:admin", "request_id": "req-1"},
        }
    )

    assert isinstance(event, AuditEvent)
    assert event.actor == "user:admin"
    assert event.request_id == "req-1"


def test_summarize_events_returns_latest_operational_markers() -> None:
    summary = summarize_events(
        {
            "audit": [
                {
                    "event_id": "audit-1",
                    "timestamp": "2026-09-04T00:00:04+00:00",
                    "actor": "user",
                    "source": "api",
                    "request_id": None,
                    "action": "config_change",
                    "entity_type": "module",
                    "entity_id": "vpn",
                    "result": "success",
                    "details": {},
                }
            ],
            "operational": [
                {
                    "event_id": "op-1",
                    "timestamp": "2026-09-04T00:00:03+00:00",
                    "severity": "error",
                    "event_type": "runtime_failed",
                    "entity_type": "vpn",
                    "entity_id": "vpn",
                    "reconcile_state": None,
                    "message": "Runtime failed.",
                    "details": {},
                },
                {
                    "event_id": "op-2",
                    "timestamp": "2026-09-04T00:00:02+00:00",
                    "severity": "warning",
                    "event_type": "reconcile_drift",
                    "entity_type": "routing",
                    "entity_id": "global",
                    "reconcile_state": "drift",
                    "message": "Drift.",
                    "details": {},
                },
                {
                    "event_id": "op-3",
                    "timestamp": "2026-09-04T00:00:01+00:00",
                    "severity": "info",
                    "event_type": "apply_finished",
                    "entity_type": "routing",
                    "entity_id": "global",
                    "reconcile_state": None,
                    "message": "Apply done.",
                    "details": {},
                },
            ],
            "diagnostic": [],
        }
    )

    assert summary.last_error and summary.last_error.event_type == "runtime_failed"
    assert summary.last_drift and summary.last_drift.event_type == "reconcile_drift"
    assert summary.last_apply and summary.last_apply.event_type == "apply_finished"
    assert summary.last_change and summary.last_change.event_id == "audit-1"
