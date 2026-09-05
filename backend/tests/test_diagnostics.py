from __future__ import annotations

import json

from fastapi.testclient import TestClient

from fwrouter_api import cli
from fwrouter_api.db.connection import db_session
from fwrouter_api.main import create_app
from fwrouter_api.services import diagnostics
from fwrouter_api.services.events import EventSummary
from fwrouter_api.services.reconcile import ReconcileResponse, ReconcileResult


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
                details={"missing_subject_ids": ["xray:alice"]},
            )
        ]
        return response

    monkeypatch.setattr(diagnostics, "build_reconcile_response", _reconcile)

    report = diagnostics.build_diagnostic_report()

    assert report.status == "degraded"
    assert report.sections["connections"]["status"] == "degraded"
    assert any(
        problem.reason == "active client has no runtime binding" for problem in report.problems
    )


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
