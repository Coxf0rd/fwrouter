from __future__ import annotations

import json

from fastapi.testclient import TestClient

from fwrouter_api import cli
from fwrouter_api.db.connection import db_session
from fwrouter_api.main import create_app
from fwrouter_api.services import diagnostics
from fwrouter_api.services.events import EventSummary
from fwrouter_api.services.reconcile import ReconcileResponse, ReconcileResult


def test_unknown_observation_is_not_reported_as_problem_or_warning() -> None:
    assert diagnostics._projection_severity(None) == "unknown"
    assert diagnostics._projection_severity({}) == "unknown"
    assert diagnostics._projection_severity({"entity": {"id": "vpn"}}) == "unknown"
    assert diagnostics._projection_severity({"projection": {"state": "unknown"}}) == "unknown"
    assert diagnostics._projection_severity({"projection": {"state": "unknown"}, "reconcile": {"state": "drift"}}) == "degraded"
    assert diagnostics._projection_severity({"projection": {"state": "healthy"}}) == "healthy"
    assert diagnostics._projection_severity({"reconcile": {"state": "in_sync"}}) == "healthy"
    result = ReconcileResult(entity_type="vpn", entity_id="vpn", reconcile_state="unknown")
    assert diagnostics._reconcile_severity("unknown") == "unknown"
    assert diagnostics._reconcile_problem(result) is None


def test_stale_enabled_xray_vless_without_failure_is_unknown_and_retained() -> None:
    item = _projection_item(
        "subject",
        "subject:test",
        role="vless_client",
        stale=True,
        reconcile_state="stale",
        projection_state="warning",
        evidence={"is_active": True},
    )
    item["intent"]["details"] = {
        "subject_type": "explicit_external_client",
        "implementation_kind": "xray",
    }

    assert diagnostics._stale_explicit_xray_client_without_failure(item)
    assert diagnostics._subject_user_severity(item) == "unknown"
    problem = diagnostics._subject_projection_problem(item)
    assert problem is not None
    assert problem.severity == "unknown"
    assert problem.details["overall_impact"] is False
    section, problems = diagnostics._build_subjects_section(
        {"items": [item]},
        [ReconcileResult(entity_type="subject", entity_id="subject:test", reconcile_state="stale")],
    )
    assert section["status"] == "unknown"
    assert section["affected_entity_count"] == 0
    assert section["stale_unconfirmed_count"] == 1
    assert len(problems) == 1


def test_stale_external_source_and_confirmed_xray_failure_are_not_suppressed() -> None:
    source = _projection_item("subject", "source:test", role="external_network_source", stale=True)
    assert not diagnostics._stale_explicit_xray_client_without_failure(source)

    failed = _projection_item(
        "subject", "subject:test", role="vless_client", stale=True,
        reconcile_state="failed", projection_state="failed",
    )
    failed["intent"]["details"] = {
        "subject_type": "explicit_external_client", "implementation_kind": "xray",
    }
    assert not diagnostics._stale_explicit_xray_client_without_failure(failed)


def test_external_unknown_and_expired_projection_is_nonimpacting_but_failure_is_not() -> None:
    item = _projection_item(
        "subject", "source:test", role="external_network_source", stale=True,
        reconcile_state="unknown", projection_state="unknown", observation_state="unknown",
        evidence={"is_active": True},
    )
    item["reconcile"]["reason_code"] = "EXTERNAL_SOURCE_OBSERVATION_UNCONFIRMED"
    problem = diagnostics._subject_projection_problem(item)
    assert diagnostics._external_observation_unconfirmed_without_failure(item)
    assert problem is not None
    assert problem.severity == "unknown"
    assert problem.reason_code == "EXTERNAL_SOURCE_OBSERVATION_UNCONFIRMED"
    assert problem.details["overall_impact"] is False

    failed = _projection_item(
        "subject", "source:test", role="external_network_source", stale=True,
        reconcile_state="failed", projection_state="failed", observation_state="unknown",
        evidence={"is_active": True},
    )
    failed["reconcile"]["reason_code"] = "SUBJECT_APPLY_FAILED"
    assert not diagnostics._external_observation_unconfirmed_without_failure(failed)


def test_stale_dnsmasq_lan_inventory_is_unknown_without_confirmed_impact() -> None:
    item = _projection_item(
        "subject",
        "lan:test",
        role="lan_client",
        stale=True,
        reconcile_state="in_sync",
        projection_state="unknown",
        observation_state="unknown",
        evidence={"is_active": True, "inventory_observation_source": "dnsmasq_leases"},
    )

    assert diagnostics._stale_lan_inventory_without_failure(item)
    assert diagnostics._subject_user_severity(item) == "unknown"
    problem = diagnostics._subject_projection_problem(item)
    assert problem is not None
    assert problem.reason_code == "SUBJECT_OBSERVATION_STALE"
    assert problem.details["overall_impact"] is False
    section, problems = diagnostics._build_subjects_section(
        {"items": [item]},
        [ReconcileResult(entity_type="subject", entity_id="lan:test", reconcile_state="stale")],
    )
    assert section["status"] == "unknown"
    assert section["affected_entity_count"] == 0
    assert section["stale_unconfirmed_count"] == 1
    assert len(problems) == 1


def test_stale_lan_inventory_does_not_suppress_pending_or_drift() -> None:
    for reconcile_state in ("intent_newer_than_runtime", "drift", "failed"):
        item = _projection_item(
            "subject",
            "lan:test",
            role="lan_client",
            stale=True,
            reconcile_state=reconcile_state,
            projection_state="warning" if reconcile_state == "intent_newer_than_runtime" else reconcile_state,
            evidence={"is_active": True, "inventory_observation_source": "dnsmasq_leases"},
        )
        assert not diagnostics._stale_lan_inventory_without_failure(item)


def test_lan_observation_stale_reconcile_is_unknown_without_confirmed_impact() -> None:
    item = _projection_item(
        "subject", "lan:test", role="lan_client", stale=True,
        reconcile_state="stale", projection_state="warning",
        evidence={"is_active": True, "inventory_observation_source": "dnsmasq_leases"},
    )
    item["entity"]["label"] = "Living room tablet"
    item["reconcile"]["reason_code"] = "OBSERVATION_STALE"
    result = ReconcileResult(
        entity_type="subject", entity_id="lan:test", reconcile_state="stale", reason="OBSERVATION_STALE",
    )

    assert diagnostics._stale_lan_inventory_without_failure(item)
    section, problems = diagnostics._build_subjects_section({"items": [item]}, [result])
    assert section["status"] == "unknown"
    assert section["affected_entity_count"] == 0
    assert len(problems) == 1
    assert problems[0].reason_code == "SUBJECT_OBSERVATION_STALE"
    assert problems[0].details["overall_impact"] is False
    assert problems[0].details["display_name"] == "Living room tablet"

    # A lagging projection can omit its own problem while reconcile has the same stale evidence.
    item["projection"]["state"] = "healthy"
    section, problems = diagnostics._build_subjects_section({"items": [item]}, [result])
    assert section["affected_entity_count"] == 0
    assert len(problems) == 1
    assert problems[0].severity == "unknown"
    assert problems[0].details["overall_impact"] is False


def test_subject_problems_preserve_safe_labels_and_aggregate_unknown_evidence() -> None:
    stale_a = _projection_item(
        "subject", "xray:opaque-a", role="vless_client", stale=True,
        reconcile_state="stale", projection_state="unknown", observed_at="2026-09-30T11:00:00Z",
        evidence={"is_active": True},
    )
    stale_b = _projection_item(
        "subject", "xray:opaque-b", role="vless_client", stale=True,
        reconcile_state="stale", projection_state="unknown", observed_at="2026-09-30T11:01:00Z",
        evidence={"is_active": True},
    )
    stale_a["entity"]["label"] = "Alice phone"
    stale_b["entity"]["label"] = "Bob tablet"
    for item in (stale_a, stale_b):
        item["intent"]["details"] = {
            "subject_type": "explicit_external_client",
            "implementation_kind": "xray",
        }
    confirmed = _projection_item(
        "subject", "tailscale-node:30", role="external_network_source",
        reconcile_state="stale", projection_state="warning", observed_at="2026-09-30T10:00:00Z",
        evidence={"is_active": True},
    )
    confirmed["entity"]["label"] = "Desktop-AS"
    confirmed["reconcile"]["reason_code"] = "EXTERNAL_SOURCE_OFFLINE"

    section, problems = diagnostics._build_subjects_section(
        {"items": [stale_a, stale_b, confirmed]},
        [
            ReconcileResult(entity_type="subject", entity_id="xray:opaque-a", reconcile_state="stale"),
            ReconcileResult(entity_type="subject", entity_id="xray:opaque-b", reconcile_state="stale"),
            ReconcileResult(entity_type="subject", entity_id="tailscale-node:30", reconcile_state="stale"),
        ],
    )

    assert section["affected_entity_count"] == 1
    assert section["last_observation"] == "2026-09-30T10:00:00Z"
    assert section["affected_entities"] == [{"display_name": "Desktop-AS"}]
    assert len(problems) == 2
    aggregate = next(problem for problem in problems if problem.severity == "unknown")
    assert aggregate.details["overall_impact"] is False
    assert aggregate.details["affected_count"] == 2
    assert aggregate.details["affected_entities"] == [
        {"display_name": "Alice phone"}, {"display_name": "Bob tablet"},
    ]
    assert len(aggregate.details["evidence"]) == 2
    confirmed_problem = next(problem for problem in problems if problem.reason_code == "EXTERNAL_SOURCE_OFFLINE")
    assert confirmed_problem.details["display_name"] == "Desktop-AS"


def test_unsafe_subject_id_is_not_used_as_display_label() -> None:
    item = _projection_item("subject", "tailscale-node:30", role="external_network_source")
    item["entity"]["label"] = "tailscale-node:30"
    assert diagnostics._safe_subject_label(item) is None


def test_missing_interval_collector_observation_is_explicit_and_nonimpacting(monkeypatch) -> None:
    monkeypatch.setattr(
        diagnostics,
        "list_external_connections",
        lambda **_kwargs: [{
            "connection_id": "external-test",
            "enabled": True,
            "refresh_mode": "interval",
            "integration_mode": "command_probe",
            "last_seen_at": None,
        }],
    )

    section, problems = diagnostics._build_external_connections_section({"status": "healthy"})

    assert section["status"] == "warning"
    assert section["overall_impact"] is False
    assert section["connections_stale"] == 1
    assert len(problems) == 1
    assert problems[0].reason_code == "EXTERNAL_INTEGRATION_OBSERVATION_MISSING"
    assert "no successful interval collector observation" in problems[0].reason
    assert "latest successful run" in (problems[0].suggested_investigation or "")


def _table_counts() -> dict[str, int]:
    with db_session() as connection:
        rows = connection.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
              AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        ).fetchall()
        return {
            str(row["name"]): int(
                connection.execute(f"SELECT COUNT(*) AS count FROM {row['name']}").fetchone()[
                    "count"
                ]
            )
            for row in rows
        }


def _projection_item(
    entity_type: str,
    entity_id: str,
    *,
    role: str | None = None,
    intent_state: str = "enabled",
    observation_state: str = "running",
    reconcile_state: str = "in_sync",
    projection_state: str = "healthy",
    stale: bool = False,
    observed_at: str | None = None,
    evidence: dict[str, object] | None = None,
    effective: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "entity": {"type": entity_type, "id": entity_id, "role": role},
        "intent": {"state": intent_state, "mode": "vpn", "target_id": "server-1"},
        "execution": {"state": "idle"},
        "observation": {
            "state": observation_state,
            "observed_at": observed_at,
            "stale": stale,
            "evidence": evidence or {},
        },
        "reconcile": {"state": reconcile_state},
        "projection": {"state": projection_state, "severity": "none"},
        "effective": effective or {},
        "reason": {},
        "legacy": {"raw": {}},
    }


def _healthy_projection_loaders(monkeypatch, *, xray_item: dict[str, object] | None = None) -> None:
    monkeypatch.setattr(
        diagnostics,
        "build_module_state_projection",
        lambda: {
            "items": [
                _projection_item("module", "core"),
                _projection_item("module", "vpn"),
                _projection_item("module", "xray"),
            ],
            "summary": {"total_count": 3},
        },
    )
    monkeypatch.setattr(
        diagnostics,
        "build_subject_state_projection",
        lambda: {
            "items": [
                _projection_item(
                    "subject",
                    "lan:laptop",
                    evidence={"is_active": True},
                    effective={"mode": "vpn"},
                )
            ],
            "summary": {"total_count": 1},
        },
    )
    monkeypatch.setattr(
        diagnostics,
        "build_routing_state_projection",
        lambda: {"routing": _projection_item("routing", "global")},
    )
    monkeypatch.setattr(
        diagnostics,
        "build_vpn_state_projection",
        lambda: {
            "vpn": _projection_item(
                "vpn",
                "vpn",
                effective={"adapter_state": "running", "selected_server_id": "server-1"},
            )
        },
    )
    monkeypatch.setattr(
        diagnostics,
        "build_xray_state_projection",
        lambda: {
            "xray": xray_item
            or _projection_item(
                "xray",
                "xray",
                effective={
                    "active_clients_count": 1,
                    "runtime_bindings_count": 1,
                    "applied_bindings_count": 1,
                    "pending_apply_count": 0,
                    "failed_apply_count": 0,
                },
            )
        },
    )
    monkeypatch.setattr(
        diagnostics,
        "build_watchdog_state_projection",
        lambda: {
            "watchdog": _projection_item(
                "watchdog",
                "watchdog",
                effective={},
            )
        },
    )
    monkeypatch.setattr(
        diagnostics,
        "list_recent_events",
        lambda limit=100: {"audit": [], "operational": [], "diagnostic": []},
    )
    monkeypatch.setattr(
        diagnostics,
        "summarize_events",
        lambda events=None: EventSummary(),
    )


def _healthy_reconcile() -> ReconcileResponse:
    entities = [
        ReconcileResult(entity_type="module", entity_id="core", reconcile_state="in_sync"),
        ReconcileResult(entity_type="module", entity_id="vpn", reconcile_state="in_sync"),
        ReconcileResult(entity_type="module", entity_id="xray", reconcile_state="in_sync"),
        ReconcileResult(entity_type="subject", entity_id="lan:laptop", reconcile_state="in_sync"),
        ReconcileResult(entity_type="routing", entity_id="global", reconcile_state="in_sync"),
        ReconcileResult(entity_type="vpn", entity_id="vpn", reconcile_state="in_sync"),
        ReconcileResult(entity_type="xray", entity_id="xray", reconcile_state="in_sync"),
        ReconcileResult(entity_type="watchdog", entity_id="watchdog", reconcile_state="in_sync"),
    ]
    return ReconcileResponse(
        entities=entities,
        summary={"healthy": len(entities), "drift": 0, "stale": 0, "failed": 0},
    )


def _healthy_report(monkeypatch) -> diagnostics.DiagnosticReport:
    _healthy_projection_loaders(monkeypatch)
    monkeypatch.setattr(diagnostics, "build_reconcile_response", _healthy_reconcile)
    return diagnostics.build_diagnostic_report()


def test_diagnose_healthy_system_status_healthy(monkeypatch) -> None:
    report = _healthy_report(monkeypatch)

    assert report.status == "healthy"
    assert report.summary["overall_status"] == "healthy"
    assert report.problems == []


def test_diagnose_xray_pending_db_runtime_applied_is_warning_not_failed(monkeypatch) -> None:
    xray_item = _projection_item(
        "xray",
        "xray",
        effective={
            "active_clients_count": 1,
            "runtime_bindings_count": 1,
            "applied_bindings_count": 1,
            "pending_apply_count": 1,
            "failed_apply_count": 0,
        },
    )
    _healthy_projection_loaders(monkeypatch, xray_item=xray_item)

    def _reconcile() -> ReconcileResponse:
        response = _healthy_reconcile()
        response.entities = [
            result
            for result in response.entities
            if result.entity_type != "xray"
        ] + [
            ReconcileResult(
                entity_type="xray",
                entity_id="xray",
                reconcile_state="in_sync",
                reason="runtime_confirmed",
                details={"pending_subject_ids": ["xray:alice"]},
            )
        ]
        return response

    monkeypatch.setattr(diagnostics, "build_reconcile_response", _reconcile)

    report = diagnostics.build_diagnostic_report()

    assert report.status == "warning"
    assert report.sections["connections"]["status"] == "warning"
    assert report.sections["connections"]["pending"] == 1
    assert all(problem.severity != "failed" for problem in report.problems)


def test_diagnose_missing_runtime_binding_is_degraded(monkeypatch) -> None:
    xray_item = _projection_item(
        "xray",
        "xray",
        effective={
            "active_clients_count": 1,
            "runtime_bindings_count": 0,
            "applied_bindings_count": 0,
            "pending_apply_count": 0,
            "failed_apply_count": 0,
        },
    )
    xray_item["observation"] = {
        "state": "running",
        "evidence": {"missing_binding_ids": ["xray:alice"], "active_clients_count": 1},
    }
    _healthy_projection_loaders(monkeypatch, xray_item=xray_item)

    def _reconcile() -> ReconcileResponse:
        response = _healthy_reconcile()
        response.entities = [
            result
            for result in response.entities
            if result.entity_type != "xray"
        ] + [
            ReconcileResult(
                entity_type="xray",
                entity_id="xray",
                reconcile_state="drift",
                reason="binding_missing",
                details={"missing_subject_ids": ["xray:alice", "xray:bob"]},
            )
        ]
        return response

    monkeypatch.setattr(diagnostics, "build_reconcile_response", _reconcile)

    report = diagnostics.build_diagnostic_report()

    assert report.status == "degraded"
    assert report.sections["connections"]["status"] == "degraded"
    assert report.sections["connections"]["affected_entity_count"] == 2
    assert report.sections["connections"]["drift"] == 2
    xray_problems = [
        problem for problem in report.problems
        if problem.reason_code == "XRAY_BINDING_MISSING" and problem.entity_id != "xray"
    ]
    assert len(xray_problems) == 2
    assert all("traffic impact is unconfirmed" in problem.reason for problem in xray_problems)


def test_diagnose_database_schema_mismatch_is_failed(monkeypatch) -> None:
    _healthy_projection_loaders(monkeypatch)
    monkeypatch.setattr(diagnostics, "build_reconcile_response", _healthy_reconcile)

    with db_session() as connection:
        connection.execute(
            """
            REPLACE INTO schema_meta (key, value, updated_at)
            VALUES ('schema_version', '0', CURRENT_TIMESTAMP)
            """
        )

    report = diagnostics.build_diagnostic_report()

    assert report.status == "failed"
    assert report.sections["database"]["status"] == "failed"
    assert any(problem.source == "database_schema" for problem in report.problems)


def test_diagnose_diagnostic_only_events_do_not_degrade_system(monkeypatch) -> None:
    _healthy_projection_loaders(monkeypatch)
    monkeypatch.setattr(diagnostics, "build_reconcile_response", _healthy_reconcile)

    monkeypatch.setattr(
        diagnostics,
        "summarize_events",
        lambda events=None: EventSummary(
            last_error={
                "event_id": "diag-1",
                "event_code": "probe_warning",
                "entity_type": "diagnostics",
                "entity_id": "probe",
                "event_type": "probe_warning",
                "message": "probe warning",
                "severity": "warning",
                "timestamp": "2026-09-05T00:00:00Z",
            }
        ),
    )

    report = diagnostics.build_diagnostic_report()

    assert report.status == "healthy"
    assert "events" not in report.sections
    assert report.summary["hidden_sections"]["diagnostic_only_problem_count"] == 1


def test_historical_failure_and_recovery_are_separate_from_current_status(monkeypatch) -> None:
    from types import SimpleNamespace

    monkeypatch.setattr(
        diagnostics,
        "list_recent_events",
        lambda *, limit: {
            "audit": [],
            "operational": [],
            "diagnostic": [
                {"event_id": "fail-1", "event_type": "probe_failed", "severity": "error", "outcome": "timeout"},
                {"event_id": "recover-1", "event_type": "probe_recovered", "severity": "info", "outcome": "recovered"},
                {"event_id": "health-1", "event_type": "logical_member_health_transition", "severity": "warning", "outcome": "failed", "details": {"record_source": "operational_sqlite", "event_code_compatibility": "legacy_event_type"}},
            ],
        },
    )
    monkeypatch.setattr(
        diagnostics,
        "summarize_events",
        lambda events: SimpleNamespace(model_dump=lambda *, mode: {
            "last_error": None,
            "last_drift": None,
            "last_apply": None,
            "last_change": None,
        }),
    )

    section, problems = diagnostics._build_events_section()

    assert section["status"] == "healthy"
    assert problems == []
    assert {item["event_id"] for item in section["history"]["recent_technical_failures"]} == {"fail-1", "health-1"}
    assert [item["event_id"] for item in section["history"]["resolved_or_recovery_events"]] == ["recover-1"]
    assert section["history"]["coverage"]["sources"]["technical_jsonl"]["available"] is True
    assert section["history"]["coverage"]["sources"]["health_transition_events"]["records"] == 1
def test_diagnose_inactive_subjects_do_not_warn_system(monkeypatch) -> None:
    _healthy_projection_loaders(monkeypatch)
    monkeypatch.setattr(
        diagnostics,
        "build_subject_state_projection",
        lambda: {
            "items": [
                _projection_item(
                    "subject",
                    "lan:inactive",
                    observation_state="inactive",
                    reconcile_state="not_applicable",
                    projection_state="inactive",
                    evidence={"is_active": False},
                )
            ],
            "summary": {"total_count": 1, "inactive_count": 1},
        },
    )
    monkeypatch.setattr(
        diagnostics,
        "build_reconcile_response",
        lambda: ReconcileResponse(
            entities=[
                ReconcileResult(entity_type="subject", entity_id="lan:inactive", reconcile_state="in_sync"),
                ReconcileResult(entity_type="routing", entity_id="global", reconcile_state="in_sync"),
                ReconcileResult(entity_type="vpn", entity_id="vpn", reconcile_state="in_sync"),
                ReconcileResult(entity_type="xray", entity_id="xray", reconcile_state="in_sync"),
                ReconcileResult(entity_type="watchdog", entity_id="watchdog", reconcile_state="in_sync"),
            ],
            summary={"healthy": 5, "drift": 0, "stale": 0, "failed": 0},
        ),
    )

    report = diagnostics.build_diagnostic_report()

    assert report.sections["subjects"]["status"] == "healthy"
    assert report.status == "healthy"


def test_diagnose_technical_subject_inventory_stale_does_not_warn_system(monkeypatch) -> None:
    _healthy_projection_loaders(monkeypatch)
    monkeypatch.setattr(
        diagnostics,
        "build_subject_state_projection",
        lambda: {
            "items": [
                _projection_item(
                    "subject",
                    "host:svc",
                    role="host_runtime",
                    projection_state="warning",
                    stale=True,
                    observed_at="2026-01-01T00:00:00Z",
                    evidence={"is_active": True},
                ),
                _projection_item(
                    "subject",
                    "docker:svc",
                    role="docker_runtime",
                    projection_state="warning",
                    stale=True,
                    observed_at="2026-01-01T00:00:00Z",
                    evidence={"is_active": True},
                ),
            ],
            "summary": {"total_count": 2, "warning_count": 2},
        },
    )
    monkeypatch.setattr(diagnostics, "build_reconcile_response", _healthy_reconcile)

    report = diagnostics.build_diagnostic_report()

    assert report.sections["subjects"]["status"] == "healthy"
    assert report.sections["subjects"]["technical_stale_count"] == 2
    assert report.status == "healthy"


def test_diagnose_user_subject_stale_remains_actionable_warning(monkeypatch) -> None:
    _healthy_projection_loaders(monkeypatch)
    monkeypatch.setattr(
        diagnostics,
        "build_subject_state_projection",
        lambda: {
            "items": [
                _projection_item(
                    "subject",
                    "xray:alice",
                    role="vless_client",
                    projection_state="warning",
                    stale=True,
                    observed_at="2026-01-01T00:00:00Z",
                    evidence={"is_active": True},
                ),
            ],
            "summary": {"total_count": 1, "warning_count": 1},
        },
    )
    monkeypatch.setattr(
        diagnostics,
        "build_reconcile_response",
        lambda: ReconcileResponse(
            entities=[
                ReconcileResult(entity_type="subject", entity_id="xray:alice", reconcile_state="in_sync"),
                ReconcileResult(entity_type="routing", entity_id="global", reconcile_state="in_sync"),
                ReconcileResult(entity_type="vpn", entity_id="vpn", reconcile_state="in_sync"),
                ReconcileResult(entity_type="xray", entity_id="xray", reconcile_state="in_sync"),
                ReconcileResult(entity_type="watchdog", entity_id="watchdog", reconcile_state="in_sync"),
            ],
            summary={"healthy": 5, "drift": 0, "stale": 0, "failed": 0},
        ),
    )

    report = diagnostics.build_diagnostic_report()

    assert report.sections["subjects"]["status"] == "warning"
    assert report.sections["subjects"]["affected_entity_count"] == 1
    assert report.status == "warning"


def test_diagnose_optional_external_connection_warning_does_not_drive_overall(monkeypatch) -> None:
    _healthy_projection_loaders(monkeypatch)
    monkeypatch.setattr(diagnostics, "build_reconcile_response", _healthy_reconcile)
    monkeypatch.setattr(
        diagnostics,
        "list_external_connections",
        lambda enabled_only=False: [
            {
                "connection_id": "external-network-tailscale",
                "enabled": True,
                "refresh_mode": "interval",
                "last_seen_at": None,
                "connection_type": "external_network_source",
            }
        ],
    )

    report = diagnostics.build_diagnostic_report()

    assert report.sections["connections"]["status"] == "warning"
    assert report.sections["connections"]["overall_impact"] is False
    assert report.status == "healthy"


def test_diagnose_legacy_database_fk_warning_does_not_drive_overall(monkeypatch) -> None:
    _healthy_projection_loaders(monkeypatch)
    monkeypatch.setattr(diagnostics, "build_reconcile_response", _healthy_reconcile)
    monkeypatch.setattr(
        diagnostics,
        "_check_database",
        lambda: (
            {
                "status": "warning",
                "reason": "legacy database references need cleanup; no runtime impact is confirmed",
                "affected_entity_count": 1,
                "foreign_key_violations": 1,
                "overall_impact": False,
            },
            [
                diagnostics.DiagnosticProblem(
                    entity_type="database",
                    entity_id="foreign_keys",
                    severity="warning",
                    reason="legacy database references need cleanup; no runtime impact is confirmed",
                    source="sqlite_foreign_key_check",
                    details={"overall_impact": False, "classification": "legacy_history_issue"},
                )
            ],
        ),
    )

    report = diagnostics.build_diagnostic_report()

    assert report.sections["database"]["status"] == "warning"
    assert report.status == "healthy"


def test_diagnose_watchdog_cooldown_is_warning_with_user_reason(monkeypatch) -> None:
    _healthy_projection_loaders(monkeypatch)
    monkeypatch.setattr(
        diagnostics,
        "build_watchdog_state_projection",
        lambda: {
            "watchdog": _projection_item(
                "watchdog",
                "watchdog",
                observation_state="degraded",
                reconcile_state="runtime_drift",
                projection_state="failed",
            )
        },
    )

    def _reconcile() -> ReconcileResponse:
        response = _healthy_reconcile()
        response.entities = [
            result
            for result in response.entities
            if result.entity_type != "watchdog"
        ] + [
            ReconcileResult(
                entity_type="watchdog",
                entity_id="watchdog",
                reconcile_state="drift",
                reason="WATCHDOG_FAILOVER_COOLDOWN",
            )
        ]
        return response

    monkeypatch.setattr(diagnostics, "build_reconcile_response", _reconcile)

    report = diagnostics.build_diagnostic_report()

    assert report.status == "warning"
    assert report.sections["watchdog"]["status"] == "warning"
    assert report.sections["watchdog"]["reason"] == (
        "watchdog failover is in cooldown; dataplane impact is not confirmed"
    )
    assert all(problem.reason != "WATCHDOG_FAILOVER_COOLDOWN" for problem in report.problems)


def test_diagnose_watchdog_manual_selection_is_warning_with_user_reason(monkeypatch) -> None:
    _healthy_projection_loaders(monkeypatch)
    monkeypatch.setattr(
        diagnostics,
        "build_watchdog_state_projection",
        lambda: {
            "watchdog": _projection_item(
                "watchdog",
                "watchdog",
                observation_state="running",
                reconcile_state="observation_stale",
                projection_state="warning",
            )
            | {"reason": {"code": "WATCHDOG_MANUAL_SELECTION"}}
        },
    )
    monkeypatch.setattr(diagnostics, "build_reconcile_response", _healthy_reconcile)

    report = diagnostics.build_diagnostic_report()

    assert report.status == "warning"
    assert report.sections["watchdog"]["status"] == "warning"
    assert report.sections["watchdog"]["reason"] == (
        "watchdog automatic failover is suppressed by manual selection; dataplane impact is not confirmed"
    )
    assert all(problem.reason != "WATCHDOG_MANUAL_SELECTION" for problem in report.problems)


def test_diagnose_report_does_not_write_database(monkeypatch) -> None:
    _healthy_projection_loaders(monkeypatch)
    monkeypatch.setattr(diagnostics, "build_reconcile_response", _healthy_reconcile)
    before = _table_counts()

    diagnostics.build_diagnostic_report()

    assert _table_counts() == before


def test_diagnose_cli_and_api_return_same_structure(monkeypatch, capsys) -> None:
    report = _healthy_report(monkeypatch)
    monkeypatch.setattr(diagnostics, "build_diagnostic_report", lambda: report)
    client = TestClient(create_app(enable_startup_tasks=False))

    response = client.get("/api/v2/diagnose")
    exit_code = cli.main(["diagnose", "--json"])

    assert exit_code == 0
    assert response.status_code == 200
    assert json.loads(capsys.readouterr().out) == response.json()


def test_summary_diagnose_does_not_read_event_history(monkeypatch) -> None:
    _healthy_projection_loaders(monkeypatch)
    monkeypatch.setattr(diagnostics, "build_reconcile_response", _healthy_reconcile)
    monkeypatch.setattr(diagnostics, "_build_events_section", lambda: (_ for _ in ()).throw(AssertionError("history queried")))
    client = TestClient(create_app(enable_startup_tasks=False))

    response = client.get("/api/v2/diagnose?view=summary")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "healthy"
    assert "summary" not in payload and "problems" not in payload
    assert "reason_code" in payload["sections"]["routing"]


def test_summary_diagnose_includes_safe_subject_labels(monkeypatch) -> None:
    report = diagnostics.DiagnosticReport(
        status="warning",
        summary={"overall_status": "warning"},
        sections={"subjects": {
            "status": "warning",
            "affected_entity_count": 2,
            "affected_entities": [{"display_name": "Desktop-AS"}],
        }},
        problems=[],
        generated_at="2026-09-30T00:00:00Z",
    )
    monkeypatch.setattr(diagnostics, "build_diagnostic_report", lambda **_kwargs: report)
    client = TestClient(create_app(enable_startup_tasks=False))

    payload = client.get("/api/v2/diagnose?view=summary").json()

    assert payload["sections"]["subjects"]["affected_entity_count"] == 2
    assert payload["sections"]["subjects"]["affected_entities"] == [{"display_name": "Desktop-AS"}]
