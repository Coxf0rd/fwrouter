from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import db_session


EventCategory = Literal["audit", "operational", "diagnostic"]
EventSeverity = Literal["debug", "info", "warning", "error"]

AUDIT_EVENT_TYPES = {"user_action", "config_change", "manual_apply"}
OPERATIONAL_EVENT_TYPES = {
    "apply_started",
    "apply_finished",
    "runtime_failed",
    "reconcile_drift",
    "failover",
}
DIAGNOSTIC_EVENT_TYPES = {
    "probe_result",
    "debug_dump",
    "materialization_details",
}
DIAGNOSTIC_LEGACY_EVENT_TYPES = {
    "vpn_watchdog_healthy",
    "vpn_watchdog_no_traffic",
    "xray_binding_materialized",
    "mihomo_candidate_config_written",
    "mihomo_candidate_config_validated",
    "mihomo_candidate_promoted",
    "mihomo_selective_default_fast_reconciled",
}


class EventContext(BaseModel):
    request_id: str | None = None
    job_id: str | None = None
    apply_id: str | None = None
    entity_id: str | None = None
    server_id: str | None = None
    connection_id: str | None = None


class AuditEvent(BaseModel):
    event_id: str
    timestamp: str
    actor: str | None = None
    source: str | None = None
    request_id: str | None = None
    action: str
    entity_type: str | None = None
    entity_id: str | None = None
    result: str
    job_id: str | None = None
    apply_id: str | None = None
    server_id: str | None = None
    connection_id: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class OperationalEvent(BaseModel):
    event_id: str
    timestamp: str
    severity: EventSeverity
    event_type: str
    entity_type: str | None = None
    entity_id: str | None = None
    job_id: str | None = None
    apply_id: str | None = None
    request_id: str | None = None
    server_id: str | None = None
    connection_id: str | None = None
    reconcile_state: str | None = None
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class DiagnosticEvent(BaseModel):
    event_id: str
    timestamp: str
    severity: EventSeverity
    event_type: str
    component: str = "general"
    entity_type: str | None = None
    entity_id: str | None = None
    request_id: str | None = None
    job_id: str | None = None
    apply_id: str | None = None
    server_id: str | None = None
    connection_id: str | None = None
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class EventSummary(BaseModel):
    last_error: OperationalEvent | None = None
    last_drift: OperationalEvent | None = None
    last_apply: OperationalEvent | None = None
    last_change: AuditEvent | OperationalEvent | None = None


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_dumps(value: dict[str, Any] | None) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _json_loads(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        loaded = json.loads(value)
    except json.JSONDecodeError:
        return {"raw": value}
    if isinstance(loaded, dict):
        return loaded
    return {"value": loaded}


def _parse_timestamp(value: Any) -> datetime:
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


def _safe_severity(value: Any) -> EventSeverity:
    severity = str(value or "info").strip().lower()
    if severity in {"debug", "info", "warning", "error"}:
        return severity  # type: ignore[return-value]
    return "info"


def _safe_component(value: str | None) -> str:
    normalized = "".join(
        character if character.isalnum() or character in {"-", "_"} else "_"
        for character in str(value or "general")
    ).strip("_")
    return normalized or "general"


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        handle.write("\n")


def create_event_context(
    *,
    request_id: str | None = None,
    job_id: str | None = None,
    apply_id: str | None = None,
    entity_id: str | None = None,
    server_id: str | None = None,
    connection_id: str | None = None,
) -> EventContext:
    return EventContext(
        request_id=request_id,
        job_id=job_id,
        apply_id=apply_id,
        entity_id=entity_id,
        server_id=server_id,
        connection_id=connection_id,
    )


def classify_event(event_type: str, *, details: dict[str, Any] | None = None) -> EventCategory:
    normalized = str(event_type or "").strip().lower()
    details = details or {}
    if str(details.get("event_category") or "").strip().lower() in {
        "audit",
        "operational",
        "diagnostic",
    }:
        return str(details["event_category"]).strip().lower()  # type: ignore[return-value]
    if normalized in AUDIT_EVENT_TYPES:
        return "audit"
    if normalized in OPERATIONAL_EVENT_TYPES:
        return "operational"
    if normalized in DIAGNOSTIC_EVENT_TYPES or normalized in DIAGNOSTIC_LEGACY_EVENT_TYPES:
        return "diagnostic"
    if normalized.startswith("mutation_"):
        return "audit"
    if normalized.startswith("core_bypass_"):
        return "audit"
    if "config_change" in normalized or normalized.endswith("_updated"):
        return "audit"
    if "probe" in normalized or "debug" in normalized:
        return "diagnostic"
    if "materializ" in normalized and "failed" not in normalized:
        return "diagnostic"
    if "drift" in normalized:
        return "operational"
    if "failover" in normalized or "switched" in normalized:
        return "operational"
    if "failed" in normalized or "failure" in normalized or "error" in normalized:
        return "operational"
    if normalized.startswith("apply_") or normalized.startswith("runtime_"):
        return "operational"
    return "operational"


def _context_from_details(
    *,
    subject_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> EventContext:
    details = details or {}
    return create_event_context(
        request_id=details.get("request_id"),
        job_id=details.get("job_id") or details.get("last_apply_job_id"),
        apply_id=details.get("apply_id"),
        entity_id=details.get("entity_id") or subject_id,
        server_id=details.get("server_id") or details.get("selected_server_id"),
        connection_id=details.get("connection_id"),
    )


def _details_with_event_model(
    details: dict[str, Any] | None,
    *,
    category: EventCategory,
    context: EventContext,
) -> dict[str, Any]:
    payload = dict(details or {})
    payload["event_category"] = category
    payload["event_context"] = context.model_dump(exclude_none=True)
    for key, value in context.model_dump(exclude_none=True).items():
        payload.setdefault(key, value)
    return payload


def _insert_operational_row(
    *,
    event_id: str,
    level: EventSeverity,
    event_type: str,
    subject_id: str | None,
    message: str,
    details: dict[str, Any],
) -> dict[str, Any]:
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
            (event_id, level, event_type, subject_id, message, _json_dumps(details)),
        )
        row = connection.execute(
            """
            SELECT event_id, level, event_type, subject_id, message, details_json, created_at
            FROM operational_logs
            WHERE event_id = ?
            """,
            (event_id,),
        ).fetchone()
    return {
        "event_id": row["event_id"],
        "level": row["level"],
        "event_type": row["event_type"],
        "subject_id": row["subject_id"],
        "message": row["message"],
        "details": _json_loads(row["details_json"]),
        "created_at": row["created_at"],
    }


def write_audit_event(
    *,
    actor: str | None,
    source: str | None,
    action: str,
    entity_type: str | None = None,
    entity_id: str | None = None,
    result: str = "success",
    context: EventContext | None = None,
    details: dict[str, Any] | None = None,
) -> AuditEvent:
    context = context or create_event_context(entity_id=entity_id)
    enriched = _details_with_event_model(details, category="audit", context=context)
    enriched.update({"actor": actor, "source": source, "action": action, "result": result})
    row = _insert_operational_row(
        event_id=str(uuid4()),
        level="info" if result == "success" else "warning",
        event_type=action,
        subject_id=entity_id if entity_type == "subject" else None,
        message=f"{action}: {result}",
        details=enriched,
    )
    return _audit_from_legacy(row)


def write_operational_event(
    *,
    severity: EventSeverity = "info",
    event_type: str,
    message: str,
    entity_type: str | None = None,
    entity_id: str | None = None,
    job_id: str | None = None,
    apply_id: str | None = None,
    reconcile_state: str | None = None,
    context: EventContext | None = None,
    details: dict[str, Any] | None = None,
) -> OperationalEvent:
    context = context or create_event_context(
        job_id=job_id,
        apply_id=apply_id,
        entity_id=entity_id,
    )
    category = classify_event(event_type, details=details)
    enriched = _details_with_event_model(details, category=category, context=context)
    if reconcile_state:
        enriched["reconcile_state"] = reconcile_state
    row = _insert_operational_row(
        event_id=str(uuid4()),
        level=severity,
        event_type=event_type,
        subject_id=entity_id if entity_type == "subject" else None,
        message=message,
        details=enriched,
    )
    return _operational_from_legacy(row)


def write_diagnostic_event(
    *,
    component: str = "general",
    severity: EventSeverity = "debug",
    event_type: str,
    message: str,
    context: EventContext | None = None,
    details: dict[str, Any] | None = None,
) -> DiagnosticEvent:
    context = context or create_event_context()
    event_id = str(uuid4())
    timestamp = _utc_timestamp()
    enriched = _details_with_event_model(details, category="diagnostic", context=context)
    event = DiagnosticEvent(
        event_id=event_id,
        timestamp=timestamp,
        severity=severity,
        event_type=event_type,
        component=_safe_component(component),
        entity_id=context.entity_id,
        request_id=context.request_id,
        job_id=context.job_id,
        apply_id=context.apply_id,
        server_id=context.server_id,
        connection_id=context.connection_id,
        message=message,
        details=enriched,
    )
    _append_jsonl(
        get_settings().paths.technical_log_dir / f"{event.component}.jsonl",
        event.model_dump(mode="json"),
    )
    return event


def log_event(
    *,
    event_type: str,
    message: str,
    level: str = "info",
    subject_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> AuditEvent | OperationalEvent | DiagnosticEvent:
    category = classify_event(event_type, details=details)
    context = _context_from_details(subject_id=subject_id, details=details)
    if category == "audit":
        return write_audit_event(
            actor=(details or {}).get("actor") or (details or {}).get("requested_by"),
            source=(details or {}).get("source"),
            action=event_type,
            entity_type="subject" if subject_id else (details or {}).get("entity_type"),
            entity_id=context.entity_id,
            result=str((details or {}).get("result") or "success"),
            context=context,
            details=details,
        )
    if category == "diagnostic":
        return write_diagnostic_event(
            component=str((details or {}).get("component") or "legacy"),
            severity=_safe_severity(level),
            event_type=event_type,
            message=message,
            context=context,
            details=details,
        )
    return write_operational_event(
        severity=_safe_severity(level),
        event_type=event_type,
        message=message,
        entity_type="subject" if subject_id else (details or {}).get("entity_type"),
        entity_id=context.entity_id,
        job_id=context.job_id,
        apply_id=context.apply_id,
        reconcile_state=(details or {}).get("reconcile_state"),
        context=context,
        details=details,
    )


def _audit_from_legacy(event: dict[str, Any]) -> AuditEvent:
    details = event.get("details") if isinstance(event.get("details"), dict) else {}
    context = _context_from_details(subject_id=event.get("subject_id"), details=details)
    return AuditEvent(
        event_id=str(event.get("event_id")),
        timestamp=str(event.get("created_at") or event.get("timestamp") or ""),
        actor=details.get("actor") or details.get("requested_by"),
        source=details.get("source"),
        request_id=context.request_id,
        action=str(details.get("action") or event.get("event_type") or ""),
        entity_type=details.get("entity_type") or ("subject" if event.get("subject_id") else None),
        entity_id=context.entity_id,
        result=str(details.get("result") or "unknown"),
        job_id=context.job_id,
        apply_id=context.apply_id,
        server_id=context.server_id,
        connection_id=context.connection_id,
        details=details,
    )


def _operational_from_legacy(event: dict[str, Any]) -> OperationalEvent:
    details = event.get("details") if isinstance(event.get("details"), dict) else {}
    context = _context_from_details(subject_id=event.get("subject_id"), details=details)
    return OperationalEvent(
        event_id=str(event.get("event_id")),
        timestamp=str(event.get("created_at") or event.get("timestamp") or ""),
        severity=_safe_severity(event.get("level")),
        event_type=str(event.get("event_type") or ""),
        entity_type=details.get("entity_type") or ("subject" if event.get("subject_id") else None),
        entity_id=context.entity_id,
        job_id=context.job_id,
        apply_id=context.apply_id,
        request_id=context.request_id,
        server_id=context.server_id,
        connection_id=context.connection_id,
        reconcile_state=details.get("reconcile_state"),
        message=str(event.get("message") or ""),
        details=details,
    )


def _diagnostic_from_technical(event: dict[str, Any]) -> DiagnosticEvent:
    details = event.get("details") if isinstance(event.get("details"), dict) else {}
    context = _context_from_details(details=details)
    return DiagnosticEvent(
        event_id=str(event.get("event_id") or uuid4()),
        timestamp=str(event.get("timestamp") or event.get("created_at") or ""),
        severity=_safe_severity(event.get("severity") or event.get("level")),
        event_type=str(event.get("event_type") or ""),
        component=_safe_component(event.get("component")),
        entity_type=details.get("entity_type"),
        entity_id=context.entity_id,
        request_id=context.request_id,
        job_id=context.job_id,
        apply_id=context.apply_id,
        server_id=context.server_id,
        connection_id=context.connection_id,
        message=str(event.get("message") or ""),
        details=details,
    )


def adapt_legacy_event(event: dict[str, Any]) -> AuditEvent | OperationalEvent | DiagnosticEvent:
    details = event.get("details") if isinstance(event.get("details"), dict) else {}
    category = classify_event(str(event.get("event_type") or ""), details=details)
    if category == "audit":
        return _audit_from_legacy(event)
    if category == "diagnostic":
        return _diagnostic_from_technical(
            {
                "event_id": event.get("event_id"),
                "timestamp": event.get("created_at"),
                "level": event.get("level"),
                "event_type": event.get("event_type"),
                "component": details.get("component") or "operational_logs",
                "message": event.get("message"),
                "details": details,
            }
        )
    return _operational_from_legacy(event)


def _read_operational_rows(
    *,
    limit: int,
    severity: str | None = None,
    entity_id: str | None = None,
    since: str | None = None,
) -> list[dict[str, Any]]:
    safe_limit = max(1, min(limit, 500))
    where: list[str] = []
    params: list[Any] = []
    if severity:
        where.append("level = ?")
        params.append(severity)
    if entity_id:
        where.append("(subject_id = ? OR details_json LIKE ?)")
        params.extend([entity_id, f'%"{entity_id}"%'])
    if since:
        where.append("created_at >= ?")
        params.append(since)
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    with db_session() as connection:
        rows = connection.execute(
            f"""
            SELECT event_id, level, event_type, subject_id, message, details_json, created_at
            FROM operational_logs
            {where_sql}
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (*params, safe_limit),
        ).fetchall()
    return [
        {
            "event_id": row["event_id"],
            "level": row["level"],
            "event_type": row["event_type"],
            "subject_id": row["subject_id"],
            "message": row["message"],
            "details": _json_loads(row["details_json"]),
            "created_at": row["created_at"],
        }
        for row in rows
    ]


def _read_technical_events(
    *,
    limit: int,
    severity: str | None = None,
    since: str | None = None,
) -> list[dict[str, Any]]:
    safe_limit = max(1, min(limit, 500))
    log_dir = get_settings().paths.technical_log_dir
    events: list[dict[str, Any]] = []
    for path in sorted(log_dir.glob("*.jsonl")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            payload.setdefault("component", path.stem)
            payload.setdefault("details", {})
            payload_severity = _safe_severity(payload.get("severity") or payload.get("level"))
            if severity and payload_severity != severity:
                continue
            if since and _parse_timestamp(payload.get("timestamp")) < _parse_timestamp(since):
                continue
            events.append(payload)
    events.sort(key=lambda item: _parse_timestamp(item.get("timestamp")), reverse=True)
    return events[:safe_limit]


def list_recent_events(
    *,
    limit: int = 100,
    event_category: EventCategory | None = None,
    severity: str | None = None,
    entity_id: str | None = None,
    since: str | None = None,
) -> dict[str, list[dict[str, Any]]]:
    audit: list[AuditEvent] = []
    operational: list[OperationalEvent] = []
    diagnostic: list[DiagnosticEvent] = []
    for row in _read_operational_rows(
        limit=limit,
        severity=severity,
        entity_id=entity_id,
        since=since,
    ):
        event = adapt_legacy_event(row)
        if isinstance(event, AuditEvent):
            audit.append(event)
        elif isinstance(event, DiagnosticEvent):
            diagnostic.append(event)
        else:
            operational.append(event)
    for event in _read_technical_events(limit=limit, severity=severity, since=since):
        diagnostic_event = _diagnostic_from_technical(event)
        if entity_id and diagnostic_event.entity_id != entity_id:
            continue
        diagnostic.append(diagnostic_event)

    result = {
        "audit": [event.model_dump(mode="json") for event in audit],
        "operational": [event.model_dump(mode="json") for event in operational],
        "diagnostic": [event.model_dump(mode="json") for event in diagnostic],
    }
    if event_category:
        return {
            "audit": result["audit"] if event_category == "audit" else [],
            "operational": result["operational"] if event_category == "operational" else [],
            "diagnostic": result["diagnostic"] if event_category == "diagnostic" else [],
        }
    return result


def summarize_events(events: dict[str, list[dict[str, Any]]] | None = None) -> EventSummary:
    events = events or list_recent_events(limit=500)
    operational = [OperationalEvent(**event) for event in events.get("operational", [])]
    audit = [AuditEvent(**event) for event in events.get("audit", [])]
    last_error = next((event for event in operational if event.severity == "error"), None)
    last_drift = next(
        (
            event
            for event in operational
            if event.reconcile_state == "drift" or "drift" in event.event_type
        ),
        None,
    )
    last_apply = next((event for event in operational if "apply" in event.event_type), None)
    candidates: list[AuditEvent | OperationalEvent] = [*audit, *operational]
    candidates.sort(key=lambda event: _parse_timestamp(event.timestamp), reverse=True)
    return EventSummary(
        last_error=last_error,
        last_drift=last_drift,
        last_apply=last_apply,
        last_change=candidates[0] if candidates else None,
    )
