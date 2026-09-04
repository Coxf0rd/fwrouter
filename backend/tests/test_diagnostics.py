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
    intent_state: str = "enabled",
    observation_state: str = "running",
    reconcile_state: str = "in_sync",
    projection_state: str = "healthy",
    evidence: dict[str, object] | None = None,
    effective: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "entity": {"type": entity_type, "id": entity_id},
        "intent": {"state": intent_state, "mode": "vpn", "target_id": "server-1"},
        "execution": {"state": "idle"},
        "observation": {"state": observation_state, "evidence": evidence or {}},
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


def test_diagnose_healthy_system_status_ok(monkeypatch) -> None:
    report = _healthy_report(monkeypatch)

    assert report.status == "ok"
    assert report.summary["overall_status"] == "ok"
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
    assert report.sections["xray"]["status"] == "warning"
    assert report.sections["xray"]["pending"] == 1
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
    assert report.sections["xray"]["status"] == "degraded"
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
