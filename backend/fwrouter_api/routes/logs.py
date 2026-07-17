from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Query

from fwrouter_api.schemas import ApiResponse
from fwrouter_api.services.logs import list_operational_logs, list_technical_logs
from fwrouter_api.services.ui_state import _summarize_log_event


router = APIRouter()


def _parse_event_timestamp(value: Any) -> datetime:
    raw = str(value or "").strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    if " " in raw and "T" not in raw:
        raw = raw.replace(" ", "T") + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _event_fingerprint(event: dict[str, Any]) -> str:
    return json.dumps(
        {
            "level": event.get("level"),
            "event_type": event.get("event_type"),
            "subject_id": event.get("subject_id"),
            "message": event.get("message"),
            "details": event.get("details") or {},
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _coalesce_adjacent_ui_duplicates(
    events: list[dict[str, Any]],
    *,
    window_seconds: int = 90,
) -> list[dict[str, Any]]:
    coalesced: list[dict[str, Any]] = []
    last_fingerprint: str | None = None
    last_timestamp: datetime | None = None

    for event in events:
        fingerprint = _event_fingerprint(event)
        timestamp = _parse_event_timestamp(event.get("created_at"))
        if (
            coalesced
            and fingerprint == last_fingerprint
            and last_timestamp is not None
            and abs((last_timestamp - timestamp).total_seconds()) <= window_seconds
        ):
            continue
        coalesced.append(event)
        last_fingerprint = fingerprint
        last_timestamp = timestamp

    return coalesced


@router.get("/logs/operational", response_model=ApiResponse)
def list_operational_logs_endpoint(
    limit: int = Query(default=100, ge=1, le=500),
    level: str | None = None,
    event_type: str | None = None,
    subject_id: str | None = None,
    ui_only: bool = Query(default=True),
) -> ApiResponse:
    events = list_operational_logs(
        limit=500 if ui_only else limit,
        level=level,
        event_type=event_type,
        subject_id=subject_id,
    )
    summarized = [_summarize_log_event(event) for event in events]
    if ui_only:
        summarized = _coalesce_adjacent_ui_duplicates(
            [event for event in summarized if event.get("ui_visible")]
        )[:limit]
    return ApiResponse(
        ok=True,
        data={"events": summarized},
    )


@router.get("/logs/technical", response_model=ApiResponse)
def list_technical_logs_endpoint(
    limit: int = Query(default=100, ge=1, le=500),
    level: str | None = None,
    component: str | None = None,
    event_type: str | None = None,
    ui_only: bool = Query(default=True),
) -> ApiResponse:
    events = list_technical_logs(
        limit=500 if ui_only else limit,
        level=level,
        component=component,
        event_type=event_type,
    )
    summarized = [_summarize_log_event(event, technical=True) for event in events]
    if ui_only:
        summarized = _coalesce_adjacent_ui_duplicates(
            [event for event in summarized if event.get("ui_visible")]
        )[:limit]
    return ApiResponse(
        ok=True,
        data={"events": summarized},
    )
