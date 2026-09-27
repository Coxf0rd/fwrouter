from __future__ import annotations

import json
from typing import Literal

from fastapi import APIRouter, Query

from fwrouter_api.services.events import list_recent_events, summarize_events


router = APIRouter()


def _safe_entity_label(event: dict[str, object]) -> str:
    details = event.get("details") if isinstance(event.get("details"), dict) else {}
    candidates = [
        event.get("entity_label"), event.get("display_name"), event.get("alias"),
        details.get("entity_label"), details.get("display_name"), details.get("alias"),
    ]
    for candidate in candidates:
        if not isinstance(candidate, str):
            continue
        label = " ".join(candidate.split()).strip()
        entity_id = str(event.get("entity_id") or "").strip()
        if (
            not label
            or len(label) > 120
            or label == entity_id
            or ":" in label
            or "://" in label
            or "/" in label
            or "@" in label
            or label.lower().startswith("fwrouter-e2e-")
            or any(marker in label.lower() for marker in ("password", "passwd", "token", "secret", "credential", "subscription_uri"))
            or (len(label) == 36 and label.count("-") == 4)
        ):
            continue
        if len(label) >= 32 and all(char.isalnum() or char in "_-" for char in label):
            continue
        return label
    return ""


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
            "causation_id", "recovery_attempt_id", "record_source", "changed_fields",
            "actor_attribution", "previous_value", "new_value", "runtime_apply_outcome",
        }
        field_keys = {
            "event_id", "timestamp", "severity", "event_type", "event_code", "component",
            "actor", "source", "action", "entity_type", "entity_id", "subject_id",
            "connection_id", "request_id", "job_id", "apply_id", "server_id",
            "workflow_id", "causation_id", "correlation_id", "outcome", "message",
            "actor_attribution", "result",
        }
        return {
            category: [
                {
                    **{key: value for key, value in event.items() if key in field_keys},
                    **({"entity_label": label} if (label := _safe_entity_label(event)) else {}),
                    "details": {
                        key: value for key, value in (event.get("details") or {}).items()
                        if key in detail_keys and (
                            value is None or isinstance(value, (str, int, float, bool))
                            or (
                                key in {"previous_value", "new_value"}
                                and isinstance(value, (dict, list))
                                and len(json.dumps(value, ensure_ascii=False)) <= 8192
                            )
                        )
                    },
                }
                for event in events[category]
            ]
            for category in ("audit", "operational", "diagnostic")
        }
    return {**events, "summary": summarize_events(events).model_dump(mode="json")}
