from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from fwrouter_api.services.events import get_event_by_id, list_recent_events, summarize_events
from fwrouter_api.services.api_client_attribution import redact_http_attribution
from fwrouter_api.services.ordinary_event_read_model import project_recent_event_summary


router = APIRouter()

def _secure_event_error_response(codes: list[str], description: str) -> dict:
    return {
        "description": description,
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "required": ["error_code"],
                    "properties": {"error_code": {"type": "string", "enum": codes}},
                    "additionalProperties": False,
                },
                "examples": {code: {"value": {"error_code": code}} for code in codes},
            },
        },
    }

_SECURE_EVENT_OPENAPI = {
    "summary": "Read recent registry-scoped events",
    "description": (
        "Returns a bounded event page and an opaque next_cursor for pagination. "
        "Requires a registry-issued client Bearer credential with events.read scope. "
        "Browser web clients must include the configured canonical Origin and matching Host. "
        "CLI and integration clients must omit Origin and use the fixed loopback API origin. "
        "POST is retained for browser Origin semantics and has the same read-only query contract."
    ),
    "security": [{"ScopedClientBearer": []}],
    "parameters": [
        {"name": "limit", "in": "query", "required": False,
         "description": "Maximum events in this page. Defaults to 50; maximum is 100.",
         "schema": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50}},
        {"name": "category", "in": "query", "required": False,
         "description": "Restrict events to a supported category.",
         "schema": {"type": "string", "enum": ["audit", "security"]}},
        {"name": "event_code", "in": "query", "required": False,
         "description": "Restrict events by a canonical event code.",
         "schema": {"type": "string", "maxLength": 128,
                    "pattern": "^[a-z][a-z0-9]*(?:[._][a-z0-9]+){1,4}$"}},
        {"name": "since", "in": "query", "required": False,
         "description": "Inclusive UTC timestamp in YYYY-MM-DDTHH:MM:SSZ form.",
         "schema": {"type": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}Z$"}},
        {"name": "cursor", "in": "query", "required": False,
         "description": "Opaque next_cursor returned by the preceding page. It is bound to the filters used there.",
         "schema": {"type": "string", "maxLength": 1536}},
        {"name": "Origin", "in": "header", "required": False,
         "description": (
             "Required for browser web clients and must equal the configured canonical origin. "
             "CLI/integration clients must omit it and use the fixed loopback API origin."
         ),
         "schema": {"type": "string", "format": "uri"}},
    ],
    "responses": {
        "200": {
            "description": "A bounded page of events and an optional next cursor.",
            "content": {"application/json": {"schema": {
                "type": "object",
                "required": ["events", "scanned", "next_cursor"],
                "properties": {
                    "events": {"type": "array", "items": {"type": "object"}},
                    "scanned": {"type": "integer", "minimum": 0},
                    "next_cursor": {"type": ["string", "null"]},
                },
                "additionalProperties": True,
            }}},
        },
        "400": _secure_event_error_response(
            ["EVENTS_QUERY_INVALID", "EVENTS_LIMIT_INVALID", "EVENTS_FILTER_INVALID", "EVENTS_CURSOR_INVALID"],
            "The query, event filter, or cursor format is invalid.",
        ),
        "401": _secure_event_error_response(
            ["EVENTS_CREDENTIAL_REQUIRED", "EVENTS_CREDENTIAL_INVALID"],
            "A valid events.read client Bearer credential is required.",
        ),
        "403": _secure_event_error_response(
            ["EVENTS_ORIGIN_REQUIRED", "EVENTS_ORIGIN_INVALID", "EVENTS_SCOPE_FORBIDDEN"],
            "The caller scope or Origin and Host combination is not permitted.",
        ),
        "409": _secure_event_error_response(
            ["EVENTS_CURSOR_STALE"], "The cursor no longer identifies the same event history anchor.",
        ),
        "503": _secure_event_error_response(
            ["EVENTS_AUTH_CONFIG_UNAVAILABLE", "EVENTS_READ_UNAVAILABLE"],
            "The client registry or event store is unavailable.",
        ),
    },
}


@router.get("/events/secure/recent", openapi_extra=_SECURE_EVENT_OPENAPI, summary='Read Recent Registry-Scoped Events')
@router.post("/events/secure/recent", openapi_extra=_SECURE_EVENT_OPENAPI, summary='Read Recent Registry-Scoped Events')
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

@router.get("/events/recent", summary='List Recent Events')
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


@router.get("/events/{event_id}", summary='Get Event')
def get_event_endpoint(event_id: str) -> dict[str, object]:
    event = get_event_by_id(event_id)
    return redact_http_attribution({"event_id": event_id, "found": event is not None, "event": event})
