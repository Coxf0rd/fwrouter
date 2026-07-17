from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any
from uuid import uuid4

from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import db_session

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
    return {
        "event_id": row["event_id"],
        "level": row["level"],
        "event_type": row["event_type"],
        "subject_id": row["subject_id"],
        "message": row["message"],
        "details": _json_loads(row["details_json"]),
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
    if not details:
        return details
    try:
        serialized = json.dumps(details, ensure_ascii=False)
        if len(serialized) > 256 * 1024:  # 256KB
            summary = {
                "truncated_payload": True,
                "original_bytes": len(serialized),
                "keys": list(details.keys())
            }
            for k, v in details.items():
                if isinstance(v, list):
                    summary[f"{k}_count"] = len(v)
            return summary
    except Exception:
        pass
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
) -> dict[str, Any]:
    """Write one UI-visible operational event to SQLite."""

    event_id = str(uuid4())
    details = _truncate_large_details(details)
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
            "message": message,
            "details": details,
            "created_at": _utc_timestamp(),
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
                details_json
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                level,
                event_type,
                subject_id,
                message,
                _json_dumps(details),
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
            "level": event["level"],
            "event_type": event["event_type"],
            "subject_id": event["subject_id"],
            "message": event["message"],
            "details": event["details"],
            "created_at": event["created_at"],
        },
    )
    return event


def write_technical_log(
    *,
    component: str,
    event_type: str,
    message: str,
    level: str = "info",
    details: dict[str, Any] | None = None,
    dedupe_key: str | None = None,
    cooldown_seconds: int | None = None,
) -> dict[str, Any]:
    """Append one technical event to component-scoped JSONL log."""

    details = _truncate_large_details(details)
    safe_component = "".join(
        character if character.isalnum() or character in {"-", "_"} else "_"
        for character in component
    ).strip("_") or "general"
    if not _should_emit_deduped_log(
        component=f"technical:{safe_component}",
        event_type=event_type,
        dedupe_key=dedupe_key,
        cooldown_seconds=cooldown_seconds,
    ):
        return {
            "timestamp": _utc_timestamp(),
            "level": level,
            "component": safe_component,
            "event_type": event_type,
            "message": message,
            "details": details or {},
            "deduplicated": True,
        }
    event = {
        "timestamp": _utc_timestamp(),
        "level": level,
        "component": safe_component,
        "event_type": event_type,
        "message": message,
        "details": details or {},
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
                    events.append(payload)
        except OSError:
            continue
        except json.JSONDecodeError:
            continue

    events.sort(key=lambda item: _parse_iso_timestamp(str(item.get("timestamp") or "")), reverse=True)
    return events[:safe_limit]
