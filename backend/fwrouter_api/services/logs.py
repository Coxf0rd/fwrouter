from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any
from uuid import uuid4

from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import db_session
from fwrouter_api.services.events import classify_event, log_event
from fwrouter_api.services.event_contract import (
    bounded_event_message,
    database_timestamp,
    event_context_from_details,
    normalize_event_details,
    sanitize_value,
)

_LOG_DEDUPE_LOCK = Lock()
_LOG_DEDUPE_STATE: dict[tuple[str, str, str], datetime] = {}


def _json_dumps(value: dict[str, Any] | None) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _json_loads(value: str | None) -> dict[str, Any] | None:
    if not value:
        return None

    loaded = json.loads(value)
    if isinstance(loaded, dict):
        return loaded

    return {"value": loaded}


def _row_to_event(row: Any) -> dict[str, Any]:
    details = sanitize_value(_json_loads(row["details_json"]))
    message = bounded_event_message(row["message"])
    return {
        "event_id": row["event_id"],
        "level": row["level"],
        "event_type": row["event_type"],
        "subject_id": row["subject_id"],
        "message": message,
        "details": details,
        "created_at": row["created_at"],
    }


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso_timestamp(value: str | None) -> datetime:
    raw = str(value or "").strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)


def _dedupe_token(component: str, event_type: str, dedupe_key: str) -> tuple[str, str, str]:
    return (component, event_type, dedupe_key)


def _should_emit_deduped_log(
    *,
    component: str,
    event_type: str,
    dedupe_key: str | None,
    cooldown_seconds: int | None,
) -> bool:
    if not dedupe_key or not cooldown_seconds or cooldown_seconds <= 0:
        return True

    now = datetime.now(timezone.utc)
    token = _dedupe_token(component, event_type, dedupe_key)
    with _LOG_DEDUPE_LOCK:
        previous = _LOG_DEDUPE_STATE.get(token)
        if previous is not None and (now - previous).total_seconds() < cooldown_seconds:
            return False
        _LOG_DEDUPE_STATE[token] = now
    return True


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        handle.write("\n")


def list_operational_logs(
    *,
    limit: int = 100,
    level: str | None = None,
    event_type: str | None = None,
    subject_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return recent UI-visible operational logs, newest first."""

    safe_limit = max(1, min(limit, 500))

    where: list[str] = []
    params: list[Any] = []

    if level:
        where.append("level = ?")
        params.append(level)

    if event_type:
        where.append("event_type = ?")
        params.append(event_type)

    if subject_id:
        where.append("subject_id = ?")
        params.append(subject_id)

    where_sql = f"WHERE {' AND '.join(where)}" if where else ""

    with db_session() as connection:
        rows = connection.execute(
            f"""
            SELECT
                event_id,
                level,
                event_type,
                subject_id,
                message,
                details_json,
                created_at
            FROM operational_logs
            {where_sql}
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (*params, safe_limit),
        ).fetchall()

    return [_row_to_event(row) for row in rows]


def _truncate_large_details(details: dict[str, Any] | None) -> dict[str, Any] | None:
    return details


def write_operational_log(
    *,
    event_type: str,
    message: str,
    level: str = "info",
    subject_id: str | None = None,
    details: dict[str, Any] | None = None,
    dedupe_key: str | None = None,
    cooldown_seconds: int | None = None,
    event_id: str | None = None,
    timestamp: str | None = None,
    component: str | None = None,
    event_category: str | None = None,
    event_code: str | None = None,
    operation: str | None = None,
    outcome: str | None = None,
    workflow_id: str | None = None,
    causation_id: str | None = None,
) -> dict[str, Any]:
    """Write one UI-visible operational event to SQLite."""

    event_id = str(event_id or (details or {}).get("event_id") or uuid4())
    timestamp = str(timestamp or (details or {}).get("timestamp") or _utc_timestamp())
    context = event_context_from_details(details)
    if workflow_id:
        context["workflow_id"] = str(workflow_id)
    if causation_id:
        context["causation_id"] = str(causation_id)
    safe_component = str(component or (details or {}).get("component") or "fwrouter-api")
    category = str(event_category or (details or {}).get("event_category") or classify_event(event_type, details=details))
    canonical = dict(details or {})
    canonical.update({key: value for key, value in context.items() if value is not None})
    if operation is not None:
        canonical["operation"] = operation
    if outcome is not None:
        canonical["outcome"] = outcome
    canonical = normalize_event_details(
        canonical,
        event_id=event_id,
        timestamp=timestamp,
        severity=level,
        component=safe_component,
        event_category=category,
        event_code=str(event_code or (details or {}).get("event_code") or event_type),
        event_type=event_type,
    )
    safe_message = bounded_event_message(message, fallback=event_type)
    if not _should_emit_deduped_log(
        component="operational",
        event_type=event_type,
        dedupe_key=dedupe_key,
        cooldown_seconds=cooldown_seconds,
    ):
        return {
            "event_id": None,
            "level": level,
            "event_type": event_type,
            "subject_id": subject_id,
            "message": safe_message,
            "details": canonical,
            "created_at": timestamp,
            "deduplicated": True,
        }

    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO operational_logs (
                event_id,
                level,
                event_type,
                subject_id,
                message,
                details_json,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                level,
                event_type,
                subject_id,
                safe_message,
                _json_dumps(canonical),
                database_timestamp(timestamp),
            ),
        )

        row = connection.execute(
            """
            SELECT
                event_id,
                level,
                event_type,
                subject_id,
                message,
                details_json,
                created_at
            FROM operational_logs
            WHERE event_id = ?
            """,
            (event_id,),
        ).fetchone()

    event = _row_to_event(row)
    _append_jsonl(
        get_settings().paths.operational_events_path,
        {
            "event_id": event["event_id"],
            "timestamp": timestamp,
            "severity": level,
            "level": event["level"],
            "component": safe_component,
            "event_category": category,
            "event_code": canonical["event_code"],
            "schema_version": canonical["schema_version"],
            "event_type": event["event_type"],
            "subject_id": event["subject_id"],
            "message": event["message"],
            **event_context_from_details(canonical),
            "details": event["details"],
            "created_at": event["created_at"],
        },
    )
    return event


def write_operational_log_in_connection(
    connection: Any,
    *,
    event_type: str,
    message: str,
    level: str,
    details: dict[str, Any],
    component: str,
    event_category: str = "diagnostic",
) -> dict[str, Any]:
    """Insert a canonical event in a caller-owned transaction without nesting DB sessions."""
    event_id = str(uuid4())
    timestamp = _utc_timestamp()
    canonical = normalize_event_details(
        details,
        event_id=event_id,
        timestamp=timestamp,
        severity=level,
        component=component,
        event_category=event_category,
        event_code=str(details.get("event_code") or event_type),
        event_type=event_type,
    )
    safe_message = bounded_event_message(message, fallback=event_type)
    connection.execute(
        """INSERT INTO operational_logs
        (event_id, level, event_type, subject_id, message, details_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (event_id, level, event_type, None, safe_message, _json_dumps(canonical), database_timestamp(timestamp)),
    )
    return {
        "event_id": event_id,
        "level": level,
        "event_type": event_type,
        "subject_id": None,
        "message": safe_message,
        "details": canonical,
        "created_at": timestamp,
    }


def write_technical_log(
    *,
    component: str,
    event_type: str,
    message: str,
    level: str = "info",
    details: dict[str, Any] | None = None,
    dedupe_key: str | None = None,
    cooldown_seconds: int | None = None,
    event_id: str | None = None,
    timestamp: str | None = None,
    event_category: str = "diagnostic",
    event_code: str | None = None,
    operation: str | None = None,
    outcome: str | None = None,
    workflow_id: str | None = None,
    causation_id: str | None = None,
) -> dict[str, Any]:
    """Append one technical event to component-scoped JSONL log."""

    event_id = str(event_id or (details or {}).get("event_id") or uuid4())
    timestamp = str(timestamp or (details or {}).get("timestamp") or _utc_timestamp())
    safe_component = "".join(
        character if character.isalnum() or character in {"-", "_"} else "_"
        for character in component
    ).strip("_") or "general"
    context = event_context_from_details(details)
    if workflow_id:
        context["workflow_id"] = str(workflow_id)
    if causation_id:
        context["causation_id"] = str(causation_id)
    canonical = dict(details or {})
    canonical.update({key: value for key, value in context.items() if value is not None})
    if operation is not None:
        canonical["operation"] = operation
    if outcome is not None:
        canonical["outcome"] = outcome
    canonical = normalize_event_details(
        canonical,
        event_id=event_id,
        timestamp=timestamp,
        severity=level,
        component=safe_component,
        event_category=event_category,
        event_code=str(event_code or (details or {}).get("event_code") or event_type),
        event_type=event_type,
    )
    safe_message = bounded_event_message(message, fallback=event_type)
    if not _should_emit_deduped_log(
        component=f"technical:{safe_component}",
        event_type=event_type,
        dedupe_key=dedupe_key,
        cooldown_seconds=cooldown_seconds,
    ):
        return {
            "event_id": event_id,
            "timestamp": timestamp,
            "severity": level,
            "level": level,
            "component": safe_component,
            "event_category": event_category,
            "event_code": canonical["event_code"],
            "schema_version": canonical["schema_version"],
            "event_type": event_type,
            "message": safe_message,
            "details": canonical,
            "deduplicated": True,
        }
    event = {
        "event_id": event_id,
        "timestamp": timestamp,
        "severity": level,
        "level": level,
        "component": safe_component,
        "event_category": event_category,
        "event_code": canonical["event_code"],
        "schema_version": canonical["schema_version"],
        "event_type": event_type,
        "message": safe_message,
        **event_context_from_details(canonical),
        "details": canonical,
    }
    _append_jsonl(get_settings().paths.technical_log_dir / f"{safe_component}.jsonl", event)
    return event


def list_technical_logs(
    *,
    limit: int = 100,
    level: str | None = None,
    component: str | None = None,
    event_type: str | None = None,
) -> list[dict[str, Any]]:
    """Return recent technical JSONL events across components, newest first."""

    safe_limit = max(1, min(limit, 500))
    log_dir = get_settings().paths.technical_log_dir
    files = sorted(log_dir.glob("*.jsonl"))
    if component:
        normalized = "".join(
            character if character.isalnum() or character in {"-", "_"} else "_"
            for character in str(component).strip()
        ).strip("_")
        files = [log_dir / f"{normalized}.jsonl"] if normalized else []

    events: list[dict[str, Any]] = []
    for path in files:
        if not path.exists():
            continue
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    payload = json.loads(line)
                    if not isinstance(payload, dict):
                        continue
                    if level and str(payload.get("level") or "") != level:
                        continue
                    if event_type and str(payload.get("event_type") or "") != event_type:
                        continue
                    payload.setdefault("component", path.stem)
                    payload.setdefault("details", {})
                    safe_payload = sanitize_value(payload)
                    if isinstance(safe_payload, dict):
                        events.append(safe_payload)
        except OSError:
            continue
        except json.JSONDecodeError:
            continue

    events.sort(key=lambda item: _parse_iso_timestamp(str(item.get("timestamp") or "")), reverse=True)
    return events[:safe_limit]
