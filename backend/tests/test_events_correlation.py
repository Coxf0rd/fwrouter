from __future__ import annotations

from fwrouter_api.services.events import (
    create_event_context,
    list_recent_events,
    write_operational_event,
)


def test_create_event_context_supports_correlation_fields() -> None:
    context = create_event_context(
        request_id="req-1",
        job_id="job-1",
        apply_id="apply-1",
        entity_id="lan:laptop",
        server_id="server-1",
        connection_id="conn-1",
    )

    assert context.request_id == "req-1"
    assert context.job_id == "job-1"
    assert context.apply_id == "apply-1"
    assert context.entity_id == "lan:laptop"
    assert context.server_id == "server-1"
    assert context.connection_id == "conn-1"


def test_new_operational_event_preserves_job_apply_entity_links() -> None:
    event = write_operational_event(
        severity="warning",
        event_type="reconcile_drift",
        message="Subject drift.",
        entity_type="subject",
        entity_id="lan:laptop",
        job_id="job-1",
        apply_id="apply-1",
        reconcile_state="drift",
        details={"request_id": "req-1"},
    )

    assert event.entity_id == "lan:laptop"
    assert event.job_id == "job-1"
    assert event.apply_id == "apply-1"
    assert event.reconcile_state == "drift"

    recent = list_recent_events(entity_id="lan:laptop")
    assert recent["operational"][0]["entity_id"] == "lan:laptop"
    assert recent["operational"][0]["job_id"] == "job-1"
    assert recent["operational"][0]["apply_id"] == "apply-1"
