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
) -> dict[str, object]:
    events = list_recent_events(
        limit=limit,
        event_category=type,
        severity=severity,
        entity_id=entity_id,
        since=since,
    )
    return {
        **events,
        "summary": summarize_events(events).model_dump(mode="json"),
    }
