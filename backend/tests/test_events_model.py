from __future__ import annotations

import pytest
from pydantic import ValidationError

from fwrouter_api.services.events import (
    AuditEvent,
    DiagnosticEvent,
    OperationalEvent,
    _diagnostic_from_technical,
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
        event_code="config_change",
    )
    operational = OperationalEvent(
        event_id="op-1",
        timestamp="2026-09-04T00:00:01+00:00",
        severity="warning",
        event_type="reconcile_drift",
        event_code="reconcile_drift",
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
        event_code="probe_result",
        component="dataplane",
        message="Raw probe payload.",
    )

    assert audit.request_id == "req-1"
    assert operational.job_id == "job-1"
    assert operational.apply_id == "apply-1"
    assert diagnostic.component == "dataplane"
    assert operational.event_code == "reconcile_drift"


def test_typed_event_contract_requires_stable_event_code() -> None:
    with pytest.raises(ValidationError):
        OperationalEvent(
            event_id="missing-code", timestamp="2026-09-04T00:00:00Z",
            severity="warning", event_type="reconcile_drift", message="drift",
        )


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
    assert event.details["event_code_compatibility"] == "legacy_event_type"


@pytest.mark.parametrize("schema_version, expected_compat", [(1, True), (2, False)])
def test_operational_equal_code_compatibility_is_schema_gated(
    schema_version: int, expected_compat: bool
) -> None:
    event = adapt_legacy_event({
        "event_id": f"same-code-v{schema_version}", "event_type": "custom_notice",
        "level": "info", "message": "Notice",
        "details": {
            "event_code": "custom_notice", "schema_version": schema_version,
            "event_category": "operational",
        },
    })
    assert isinstance(event, OperationalEvent)
    assert (event.details.get("event_code_compatibility") == "legacy_event_type") is expected_compat


@pytest.mark.parametrize("schema_version, expected_compat", [(1, True), (2, False)])
def test_technical_equal_code_compatibility_is_schema_gated(
    schema_version: int, expected_compat: bool
) -> None:
    event = _diagnostic_from_technical({
        "event_id": f"technical-v{schema_version}", "event_type": "probe_result",
        "event_code": "probe_result", "schema_version": schema_version,
        "severity": "info", "component": "probe", "message": "Probe",
    })
    assert isinstance(event, DiagnosticEvent)
    assert (event.details.get("event_code_compatibility") == "legacy_event_type") is expected_compat


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


def test_legacy_event_without_id_gets_deterministic_read_id() -> None:
    legacy = {
        "created_at": "2026-09-04 00:00:00",
        "level": "warning",
        "event_type": "runtime_failed",
        "subject_id": None,
        "message": "Runtime failed.",
        "details": {"error_code": "RUNTIME_FAILED"},
    }
    first = adapt_legacy_event(legacy)
    second = adapt_legacy_event(legacy)
    assert first.event_id == second.event_id
    assert first.event_id.startswith("legacy:")


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
                    "event_type": "apply_job_completed",
                    "event_code": "apply_completed",
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
    assert summary.last_apply and summary.last_apply.event_code == "apply_completed"
    assert summary.last_change and summary.last_change.event_id == "audit-1"
