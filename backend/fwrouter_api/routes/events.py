from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Query

from fwrouter_api.services.events import list_recent_events, summarize_events


router = APIRouter()


@router.get("/events/recent")
def list_recent_events_endpoint(
    type: Literal["audit", "operational", "diagnostic"] | None = Query(default=None),
    severity: str | None = None,
    entity_id: str | None = None,
    since: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    view: Literal["full", "summary"] = Query(default="full"),
) -> dict[str, object]:
    events = list_recent_events(
        limit=limit,
        event_category=type,
        severity=severity,
        entity_id=entity_id,
        since=since,
    )
    if view == "summary":
        detail_keys = {
            "event_code_compatibility", "reason_code", "error_code", "error_reason",
            "phase", "outcome", "stage", "workflow_id", "correlation_id",
            "causation_id", "recovery_attempt_id", "record_source",
        }
        field_keys = {
            "event_id", "timestamp", "severity", "event_type", "event_code", "component",
            "actor", "source", "action", "entity_type", "entity_id", "subject_id",
            "connection_id", "request_id", "job_id", "apply_id", "server_id",
            "workflow_id", "causation_id", "correlation_id", "outcome", "message",
        }
        return {
            category: [
                {
                    **{key: value for key, value in event.items() if key in field_keys},
                    "details": {
                        key: value for key, value in (event.get("details") or {}).items()
                        if key in detail_keys and (value is None or isinstance(value, (str, int, float, bool)))
                    },
                }
                for event in events[category]
            ]
            for category in ("audit", "operational", "diagnostic")
        }
    return {**events, "summary": summarize_events(events).model_dump(mode="json")}
