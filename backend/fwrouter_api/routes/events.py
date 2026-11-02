from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from fwrouter_api.services.events import get_event_by_id, list_recent_events, summarize_events
from fwrouter_api.services.api_client_attribution import redact_http_attribution
from fwrouter_api.services.ordinary_event_read_model import project_recent_event_summary


router = APIRouter()


@router.get("/events/secure/recent")
@router.post("/events/secure/recent")
def secure_recent_events_endpoint(request: Request) -> JSONResponse:
    """Read-only registry-scoped projection; POST supports browser Origin semantics."""
    from fwrouter_api.services.secure_event_read_admission import (
        SecureEventAdmissionError, secure_event_read_admission,
    )
    from fwrouter_api.services.secure_events import SecureEventReadError, read_secure_recent

    headers = {"Cache-Control": "no-store, private", "Pragma": "no-cache"}
    try:
        with secure_event_read_admission(request):
            raw_query = request.scope.get("query_string", b"")
            if len(raw_query) > 4096:
                raise SecureEventReadError("EVENTS_QUERY_INVALID", 400)
            allowed = {"limit", "category", "event_code", "since", "cursor"}
            if set(request.query_params.keys()) - allowed or any(
                len(request.query_params.getlist(key)) != 1
                for key in request.query_params.keys()
            ):
                raise SecureEventReadError("EVENTS_QUERY_INVALID", 400)
            raw_limit = request.query_params.get("limit")
            if raw_limit is not None and (len(raw_limit) > 3 or not raw_limit.isascii() or not raw_limit.isdigit()):
                raise SecureEventReadError("EVENTS_LIMIT_INVALID", 400)
            limit = int(raw_limit) if raw_limit is not None else 50
            payload = read_secure_recent(
                limit=limit,
                category=request.query_params.get("category"),
                event_code=request.query_params.get("event_code"),
                since=request.query_params.get("since"),
                cursor=request.query_params.get("cursor"),
            )
            # JSON serialization occurs under the shared policy lease so a
            # concurrent revoke drains this admitted page before publication.
            return JSONResponse(content=payload, headers=headers)
    except (SecureEventAdmissionError, SecureEventReadError) as exc:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error_code": exc.code},
            headers=headers,
        )

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
        return redact_http_attribution(project_recent_event_summary(events))
    return redact_http_attribution({**events, "summary": summarize_events(events).model_dump(mode="json")})


@router.get("/events/{event_id}")
def get_event_endpoint(event_id: str) -> dict[str, object]:
    event = get_event_by_id(event_id)
    return redact_http_attribution({"event_id": event_id, "found": event is not None, "event": event})
