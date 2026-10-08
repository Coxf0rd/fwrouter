from __future__ import annotations

from fastapi.testclient import TestClient
from _test_support import database_table_diff, database_table_snapshot

from fwrouter_api import cli
from fwrouter_api.db.connection import db_session
from fwrouter_api.main import create_app
from fwrouter_api.services.reconcile import ReconcileResponse, ReconcileResult, _summarize


def test_reconcile_summary_counts_public_states() -> None:
    results = [
        ReconcileResult(entity_type="module", entity_id="core", reconcile_state="in_sync"),
        ReconcileResult(entity_type="routing", entity_id="global", reconcile_state="drift"),
        ReconcileResult(entity_type="vpn", entity_id="vpn", reconcile_state="stale"),
        ReconcileResult(entity_type="xray", entity_id="xray", reconcile_state="failed"),
        ReconcileResult(entity_type="watchdog", entity_id="watchdog", reconcile_state="unknown"),
    ]

    assert _summarize(results) == {"healthy": 1, "drift": 1, "stale": 1, "failed": 1}


def test_reconcile_endpoint_returns_contract_and_is_read_only(monkeypatch) -> None:
    monkeypatch.setattr("fwrouter_api.services.dnsmasq._discover_router_dns_bindings", lambda: [])
    monkeypatch.setattr(
        "fwrouter_api.services.reconcile.build_module_state_projection",
        lambda: {"items": []},
    )
    monkeypatch.setattr(
        "fwrouter_api.services.reconcile.build_subject_state_projection",
        lambda **_: {"subject": None, "items": []},
    )
    monkeypatch.setattr(
        "fwrouter_api.services.reconcile.build_xray_state_projection",
        lambda: {"xray": {}},
    )
    monkeypatch.setattr(
        "fwrouter_api.services.reconcile.build_routing_state_projection",
        lambda: {"routing": {}},
    )
    monkeypatch.setattr(
        "fwrouter_api.services.reconcile.build_vpn_state_projection",
        lambda: {"vpn": {}},
    )
    monkeypatch.setattr(
        "fwrouter_api.services.reconcile.build_watchdog_state_projection",
        lambda: {"watchdog": {}},
    )
    monkeypatch.setattr(
        "fwrouter_api.services.reconcile.build_runtime_enforcement_state",
        lambda: {
            "traffic_enforcement_guaranteed": False,
            "active_mode_matches_intent": False,
            "enforcement_level": "owned_table_missing",
        },
    )
    monkeypatch.setattr("fwrouter_api.services.reconcile.read_live_dataplane_payload", lambda: None)
    monkeypatch.setattr(
        "fwrouter_api.services.reconcile._safe_health",
        lambda _adapter: {"runtime_state": "not_configured"},
    )
    before = database_table_snapshot()
    client = TestClient(create_app(enable_startup_tasks=False))

    response = client.get("/api/v2/reconcile")

    assert response.status_code == 200
    payload = response.json()
    assert sorted(payload) == ["entities", "summary"]
    assert sorted(payload["summary"]) == ["drift", "failed", "healthy", "stale"]
    after = database_table_snapshot()
    assert after == before, database_table_diff(before, after)


def test_reconcile_cli_check_prints_read_only_summary(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        cli,
        "build_reconcile_response",
        lambda: ReconcileResponse(
            entities=[
                ReconcileResult(entity_type="xray", entity_id="xray", reconcile_state="in_sync"),
                ReconcileResult(
                    entity_type="subject",
                    entity_id="xray:alice",
                    reconcile_state="stale",
                    details={"implementation_kind": "xray"},
                ),
                ReconcileResult(
                    entity_type="subject",
                    entity_id="lan:laptop",
                    reconcile_state="drift",
                    details={"implementation_kind": "lan"},
                ),
                ReconcileResult(
                    entity_type="routing",
                    entity_id="global",
                    reconcile_state="in_sync",
                ),
            ],
            summary={"healthy": 2, "drift": 0, "stale": 0, "failed": 0},
        ),
    )

    exit_code = cli.main(["reconcile", "check"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "SYSTEM OK" in output
    assert "XRay:" in output
    assert "  2 checked" in output
    assert "  1 stale" in output
    assert "Routing:" in output
