from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from fwrouter_api.db.connection import db_session
from fwrouter_api.db.schema_state import EXPECTED_SCHEMA_VERSION, inspect_database_schema
from fwrouter_api.services.events import list_recent_events, summarize_events
from fwrouter_api.services.reconcile import ReconcileResult, build_reconcile_response
from fwrouter_api.services.state_projection import (
    build_module_state_projection,
    build_routing_state_projection,
    build_subject_state_projection,
    build_vpn_state_projection,
    build_watchdog_state_projection,
    build_xray_state_projection,
)


DiagnosticSeverity = Literal["ok", "warning", "degraded", "failed"]
_SEVERITY_RANK: dict[str, int] = {"ok": 0, "warning": 1, "degraded": 2, "failed": 3}


class DiagnosticProblem(BaseModel):
    entity_type: str
    entity_id: str
    severity: DiagnosticSeverity
    reason: str
    source: str
    suggested_investigation: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class DiagnosticReport(BaseModel):
    status: DiagnosticSeverity
    summary: dict[str, Any]
    sections: dict[str, Any]
    problems: list[DiagnosticProblem] = Field(default_factory=list)
    generated_at: str


def _utc_timestamp() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _max_severity(values: list[str]) -> DiagnosticSeverity:
    severity = "ok"
    for value in values:
        if _SEVERITY_RANK.get(value, 0) > _SEVERITY_RANK[severity]:
            severity = value
    return severity  # type: ignore[return-value]


def _problem(
    *,
    entity_type: str,
    entity_id: str,
    severity: DiagnosticSeverity,
    reason: str,
    source: str,
    suggested_investigation: str | None = None,
    details: dict[str, Any] | None = None,
) -> DiagnosticProblem:
    return DiagnosticProblem(
        entity_type=entity_type,
        entity_id=entity_id,
        severity=severity,
        reason=reason,
        source=source,
        suggested_investigation=suggested_investigation,
        details=details or {},
    )


def _reconcile_severity(state: str | None) -> DiagnosticSeverity:
    if state == "failed":
        return "failed"
    if state == "drift":
        return "degraded"
    if state in {"stale", "unknown"}:
        return "warning"
    return "ok"


def _projection_severity(item: dict[str, Any] | None) -> DiagnosticSeverity:
    if not isinstance(item, dict):
        return "warning"
    projection = item.get("projection") if isinstance(item.get("projection"), dict) else {}
    reconcile = item.get("reconcile") if isinstance(item.get("reconcile"), dict) else {}
    if projection.get("state") == "error" or projection.get("severity") == "error":
        return "degraded"
    state = str(reconcile.get("state") or "")
    if state == "runtime_drift":
        return "degraded"
    if state in {"observation_stale", "intent_newer_than_runtime", "unknown", "legacy_ambiguous"}:
        return "warning"
    return "ok"


def _safe_call(name: str, loader: Any) -> tuple[dict[str, Any], DiagnosticProblem | None]:
    try:
        payload = loader()
    except Exception as exc:
        return {}, _problem(
            entity_type="diagnostics",
            entity_id=name,
            severity="failed",
            reason=f"{name} unavailable: {exc}",
            source=f"{name}_projection",
            suggested_investigation="check projection source",
        )
    if isinstance(payload, dict):
        return payload, None
    return {}, _problem(
        entity_type="diagnostics",
        entity_id=name,
        severity="failed",
        reason=f"{name} returned invalid payload",
        source=f"{name}_projection",
        suggested_investigation="check projection source",
    )


def _check_database() -> tuple[dict[str, Any], list[DiagnosticProblem]]:
    problems: list[DiagnosticProblem] = []
    try:
        with db_session() as connection:
            schema_state = inspect_database_schema(connection)
            integrity_rows = connection.execute("PRAGMA integrity_check").fetchall()
            fk_rows = connection.execute("PRAGMA foreign_key_check").fetchall()
    except Exception as exc:
        return {
            "status": "failed",
            "schema_version": None,
            "expected_schema_version": EXPECTED_SCHEMA_VERSION,
            "integrity_check": "unavailable",
            "foreign_key_check": "unavailable",
        }, [
            _problem(
                entity_type="database",
                entity_id="main",
                severity="failed",
                reason=f"database unavailable: {exc}",
                source="database",
                suggested_investigation="check database path and sqlite access",
            )
        ]

    integrity_values = [str(row[0]) for row in integrity_rows]
    integrity_ok = integrity_values == ["ok"]
    fk_count = len(fk_rows)
    schema_ok = bool(schema_state.get("ok"))
    severity: DiagnosticSeverity = "ok"
    if not schema_ok or not integrity_ok or fk_count:
        severity = "failed"
    if not schema_ok:
        problems.append(
            _problem(
                entity_type="database",
                entity_id="schema",
                severity="failed",
                reason="database schema mismatch",
                source="database_schema",
                suggested_investigation="check schema_state inspection",
                details={
                    "expected_schema_version": schema_state.get("expected_schema_version"),
                    "actual_schema_version": schema_state.get("actual_schema_version"),
                    "problems": schema_state.get("problems") or [],
                },
            )
        )
    if not integrity_ok:
        problems.append(
            _problem(
                entity_type="database",
                entity_id="integrity",
                severity="failed",
                reason="database integrity check failed",
                source="sqlite_integrity_check",
                suggested_investigation="inspect sqlite integrity_check output",
                details={"integrity_check": integrity_values},
            )
        )
    if fk_count:
        problems.append(
            _problem(
                entity_type="database",
                entity_id="foreign_keys",
                severity="failed",
                reason="database foreign key check failed",
                source="sqlite_foreign_key_check",
                suggested_investigation="inspect sqlite foreign_key_check output",
                details={"foreign_key_violations": fk_count},
            )
        )
    return {
        "status": severity,
        "schema_version": schema_state.get("actual_schema_version"),
        "expected_schema_version": schema_state.get("expected_schema_version"),
        "integrity_check": "ok" if integrity_ok else "failed",
        "foreign_key_check": "ok" if fk_count == 0 else "failed",
        "foreign_key_violations": fk_count,
        "schema": {
            "status": schema_state.get("status"),
            "problem_count": len(schema_state.get("problems") or []),
        },
    }, problems


def _reconcile_problem(result: ReconcileResult) -> DiagnosticProblem | None:
    severity = _reconcile_severity(result.reconcile_state)
    if severity == "ok":
        return None
    reason = result.reason or result.reconcile_state
    if result.entity_type == "xray" and result.reason == "binding_missing":
        missing = result.details.get("missing_subject_ids")
        if isinstance(missing, list) and missing:
            reason = "active client has no runtime binding"
    return _problem(
        entity_type=result.entity_type,
        entity_id=result.entity_id,
        severity=severity,
        reason=reason,
        source=f"{result.entity_type}_reconcile",
        suggested_investigation="check reconcile result",
        details=result.details,
    )


def _build_modules_section(
    projection: dict[str, Any],
    reconcile_entities: list[ReconcileResult],
) -> tuple[dict[str, Any], list[DiagnosticProblem]]:
    items = projection.get("items") if isinstance(projection.get("items"), list) else []
    module_results = [result for result in reconcile_entities if result.entity_type == "module"]
    severities = [_projection_severity(item) for item in items]
    severities.extend(_reconcile_severity(result.reconcile_state) for result in module_results)
    problems = [problem for result in module_results if (problem := _reconcile_problem(result))]
    configured = sum(1 for item in items if (item.get("intent") or {}).get("state") == "enabled")
    observed = sum(
        1
        for item in items
        if (item.get("observation") or {}).get("state") in {"running", "active", "paused"}
    )
    drift = sum(1 for result in module_results if result.reconcile_state == "drift")
    return {
        "status": _max_severity(severities),
        "configured": configured,
        "observed": observed,
        "drift": drift,
        "total": len(items),
        "summary": projection.get("summary") or {},
    }, problems


def _build_subjects_section(
    projection: dict[str, Any],
    reconcile_entities: list[ReconcileResult],
) -> tuple[dict[str, Any], list[DiagnosticProblem]]:
    items = projection.get("items") if isinstance(projection.get("items"), list) else []
    subject_results = [result for result in reconcile_entities if result.entity_type == "subject"]
    problems = [problem for result in subject_results if (problem := _reconcile_problem(result))]
    active_count = sum(
        1
        for item in items
        if bool(((item.get("observation") or {}).get("evidence") or {}).get("is_active"))
    )
    inactive_count = len(items) - active_count
    drift_count = sum(1 for result in subject_results if result.reconcile_state == "drift")
    severities = [_projection_severity(item) for item in items]
    severities.extend(_reconcile_severity(result.reconcile_state) for result in subject_results)
    return {
        "status": _max_severity(severities),
        "active_count": active_count,
        "inactive_count": inactive_count,
        "drift_count": drift_count,
        "total": len(items),
        "summary": projection.get("summary") or {},
    }, problems


def _build_single_section(
    *,
    name: str,
    item: dict[str, Any] | None,
    reconcile_entities: list[ReconcileResult],
) -> tuple[dict[str, Any], list[DiagnosticProblem]]:
    result = next((entity for entity in reconcile_entities if entity.entity_type == name), None)
    problems = []
    if result:
        maybe_problem = _reconcile_problem(result)
        if maybe_problem:
            problems.append(maybe_problem)
    result_state = result.reconcile_state if result else None
    severity = _max_severity([_projection_severity(item), _reconcile_severity(result_state)])
    projection_reconcile = (item or {}).get("reconcile") or {}
    projection_reason = (item or {}).get("reason") or {}
    return {
        "status": severity,
        "intent": (item or {}).get("intent") or {},
        "execution": (item or {}).get("execution") or {},
        "observation": (item or {}).get("observation") or {},
        "effective": (item or {}).get("effective") or {},
        "reconcile": {
            "state": result_state if result else projection_reconcile.get("state"),
            "reason": result.reason if result else projection_reason.get("code"),
        },
    }, problems


def _build_xray_section(
    projection: dict[str, Any],
    reconcile_entities: list[ReconcileResult],
) -> tuple[dict[str, Any], list[DiagnosticProblem]]:
    item = projection.get("xray") if isinstance(projection.get("xray"), dict) else {}
    section, problems = _build_single_section(
        name="xray",
        item=item,
        reconcile_entities=reconcile_entities,
    )
    effective = item.get("effective") if isinstance(item.get("effective"), dict) else {}
    observation = item.get("observation") if isinstance(item.get("observation"), dict) else {}
    evidence = observation.get("evidence") if isinstance(observation.get("evidence"), dict) else {}
    pending_count = int(
        effective.get("pending_apply_count") or len(evidence.get("pending_subject_ids") or [])
    )
    failed_count = int(
        effective.get("failed_apply_count") or len(evidence.get("failed_binding_ids") or [])
    )
    missing = (
        evidence.get("missing_binding_ids")
        if isinstance(evidence.get("missing_binding_ids"), list)
        else []
    )
    if pending_count and section["status"] == "ok":
        section["status"] = "warning"
        problems.append(
            _problem(
                entity_type="xray",
                entity_id="xray",
                severity="warning",
                reason="pending binding is runtime confirmed",
                source="xray_reconcile",
                suggested_investigation="check reconcile result",
                details={"pending_apply_count": pending_count, "reconcile_state": "in_sync"},
            )
        )
    for subject_id in missing:
        if not any(problem.entity_id == str(subject_id) for problem in problems):
            problems.append(
                _problem(
                    entity_type="xray",
                    entity_id=str(subject_id),
                    severity="degraded",
                    reason="active client has no runtime binding",
                    source="xray_reconcile",
                    suggested_investigation="check reconcile result",
                )
            )
    if failed_count and section["status"] != "failed":
        section["status"] = "degraded"
    section.update(
        {
            "clients_count": int(
                effective.get("active_clients_count") or evidence.get("active_clients_count") or 0
            ),
            "bindings": int(
                effective.get("runtime_bindings_count") or evidence.get("bindings_count") or 0
            ),
            "pending": pending_count,
            "failed": failed_count,
            "drift": len(missing),
        }
    )
    return section, problems


def _build_watchdog_section(
    projection: dict[str, Any],
    reconcile_entities: list[ReconcileResult],
) -> tuple[dict[str, Any], list[DiagnosticProblem]]:
    item = projection.get("watchdog") if isinstance(projection.get("watchdog"), dict) else {}
    section, problems = _build_single_section(
        name="watchdog",
        item=item,
        reconcile_entities=reconcile_entities,
    )
    legacy = item.get("legacy") if isinstance(item.get("legacy"), dict) else {}
    raw = legacy.get("raw") if isinstance(legacy.get("raw"), dict) else {}
    module = raw.get("module") if isinstance(raw.get("module"), dict) else {}
    state = raw.get("watchdog_state") if isinstance(raw.get("watchdog_state"), dict) else {}
    section.update(
        {
            "enabled": (
                str(module.get("desired_state") or (item.get("intent") or {}).get("state") or "")
                == "enabled"
            ),
            "last_decision": state.get("last_processed_decision_id"),
            "last_success": state.get("last_successful_failover_at"),
            "failure_candidates": state.get("failure_candidate"),
        }
    )
    return section, problems


def _build_events_section() -> tuple[dict[str, Any], list[DiagnosticProblem]]:
    events = list_recent_events(limit=100)
    summary = summarize_events(events).model_dump(mode="json")
    problems: list[DiagnosticProblem] = []
    for key, reason in (
        ("last_error", "recent operational error"),
        ("last_drift", "recent reconcile drift event"),
    ):
        event = summary.get(key)
        if isinstance(event, dict):
            problems.append(
                _problem(
                    entity_type=str(event.get("entity_type") or "event"),
                    entity_id=str(event.get("entity_id") or event.get("event_id") or key),
                    severity="warning",
                    reason=reason,
                    source="events",
                    suggested_investigation="check recent events",
                    details={
                        "event_type": event.get("event_type"),
                        "message": event.get("message"),
                    },
                )
            )
    return {
        "status": "warning" if problems else "ok",
        "last_errors": summary.get("last_error"),
        "last_drift_events": summary.get("last_drift"),
        "last_failed_operations": summary.get("last_error"),
        "last_apply": summary.get("last_apply"),
        "last_change": summary.get("last_change"),
    }, problems


def build_diagnostic_report() -> DiagnosticReport:
    generated_at = _utc_timestamp()
    sections: dict[str, Any] = {}
    problems: list[DiagnosticProblem] = []

    database_section, database_problems = _check_database()
    sections["database"] = database_section
    problems.extend(database_problems)

    module_projection, projection_problem = _safe_call("modules", build_module_state_projection)
    if projection_problem:
        problems.append(projection_problem)
    subject_projection, projection_problem = _safe_call("subjects", build_subject_state_projection)
    if projection_problem:
        problems.append(projection_problem)
    routing_projection, projection_problem = _safe_call("routing", build_routing_state_projection)
    if projection_problem:
        problems.append(projection_problem)
    vpn_projection, projection_problem = _safe_call("vpn", build_vpn_state_projection)
    if projection_problem:
        problems.append(projection_problem)
    xray_projection, projection_problem = _safe_call("xray", build_xray_state_projection)
    if projection_problem:
        problems.append(projection_problem)
    watchdog_projection, projection_problem = _safe_call(
        "watchdog",
        build_watchdog_state_projection,
    )
    if projection_problem:
        problems.append(projection_problem)

    try:
        reconcile = build_reconcile_response()
        reconcile_entities = reconcile.entities
    except Exception as exc:
        reconcile_entities = []
        problems.append(
            _problem(
                entity_type="reconcile",
                entity_id="global",
                severity="failed",
                reason=f"reconcile unavailable: {exc}",
                source="reconcile",
                suggested_investigation="check reconcile framework",
            )
        )

    sections["modules"], section_problems = _build_modules_section(
        module_projection,
        reconcile_entities,
    )
    problems.extend(section_problems)
    sections["subjects"], section_problems = _build_subjects_section(
        subject_projection,
        reconcile_entities,
    )
    problems.extend(section_problems)
    sections["routing"], section_problems = _build_single_section(
        name="routing",
        item=(
            routing_projection.get("routing")
            if isinstance(routing_projection.get("routing"), dict)
            else {}
        ),
        reconcile_entities=reconcile_entities,
    )
    problems.extend(section_problems)
    sections["vpn"], section_problems = _build_single_section(
        name="vpn",
        item=vpn_projection.get("vpn") if isinstance(vpn_projection.get("vpn"), dict) else {},
        reconcile_entities=reconcile_entities,
    )
    sections["vpn"].update(
        {
            "adapter_state": sections["vpn"]["effective"].get("adapter_state")
            or sections["vpn"]["observation"].get("state"),
            "selected_server": sections["vpn"]["effective"].get("selected_server_id")
            or sections["vpn"]["intent"].get("target_id"),
            "health": sections["vpn"]["observation"].get("evidence", {}).get("health", {}),
        }
    )
    problems.extend(section_problems)
    sections["xray"], section_problems = _build_xray_section(xray_projection, reconcile_entities)
    problems.extend(section_problems)
    sections["watchdog"], section_problems = _build_watchdog_section(
        watchdog_projection,
        reconcile_entities,
    )
    problems.extend(section_problems)
    sections["events"], section_problems = _build_events_section()
    problems.extend(section_problems)

    status = _max_severity(
        [section.get("status", "ok") for section in sections.values()]
        + [problem.severity for problem in problems]
    )
    checks_total = len(sections)
    checks_failed = sum(1 for section in sections.values() if section.get("status") == "failed")
    checks_warning = sum(
        1 for section in sections.values() if section.get("status") in {"warning", "degraded"}
    )
    summary = {
        "overall_status": status,
        "generated_at": generated_at,
        "checks_total": checks_total,
        "checks_failed": checks_failed,
        "checks_warning": checks_warning,
    }
    return DiagnosticReport(
        status=status,
        summary=summary,
        sections=sections,
        problems=problems,
        generated_at=generated_at,
    )


def format_diagnostic_report(report: DiagnosticReport) -> str:
    sections = report.sections
    lines = [
        "FWRouter Diagnose",
        "=================",
        "",
        f"STATUS: {report.status.upper()}",
        "",
    ]

    database = sections.get("database", {})
    lines.extend(
        [
            "Database:",
            (
                f"  {str(database.get('status', 'unknown')).upper()} "
                f"schema={database.get('schema_version')}"
            ),
            "",
        ]
    )
    modules = sections.get("modules", {})
    lines.extend(
        [
            "Modules:",
            (
                f"  {str(modules.get('status', 'unknown')).upper()} "
                f"{modules.get('observed', 0)}/"
                f"{modules.get('configured', 0) or modules.get('total', 0)}"
            ),
            "",
        ]
    )
    routing = sections.get("routing", {})
    lines.extend(["Routing:", f"  {str(routing.get('status', 'unknown')).upper()}", ""])
    vpn = sections.get("vpn", {})
    lines.extend(["VPN:", f"  {str(vpn.get('status', 'unknown')).upper()}"])
    if vpn.get("selected_server"):
        lines.append(f"  active server: {vpn.get('selected_server')}")
    lines.append("")
    xray = sections.get("xray", {})
    lines.extend(["Xray:", f"  {str(xray.get('status', 'unknown')).upper()}"])
    if xray.get("pending"):
        lines.append(f"  {xray.get('pending')} pending bindings")
    if xray.get("drift"):
        lines.append(f"  {xray.get('drift')} drift")
    lines.append("")
    watchdog = sections.get("watchdog", {})
    lines.extend(["Watchdog:", f"  {str(watchdog.get('status', 'unknown')).upper()}", ""])

    lines.append("Recent problems:")
    if not report.problems:
        lines.append("  none")
    else:
        for problem in report.problems[:10]:
            lines.extend(
                [
                    f"  - {problem.severity.upper()}: {problem.reason}",
                    f"    Entity: {problem.entity_type}:{problem.entity_id}",
                    f"    Source: {problem.source}",
                ]
            )
            if problem.suggested_investigation:
                lines.append(f"    Suggested investigation: {problem.suggested_investigation}")
    return "\n".join(lines)
