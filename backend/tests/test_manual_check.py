from __future__ import annotations

from fwrouter_api.services import manual_check
from fastapi.testclient import TestClient
from fwrouter_api.main import create_app
from fwrouter_api.routes import servers as servers_route


def _topology(server_id: str = "logical-a", count: int = 2, status: str = "healthy"):
    return {
        "logical_server_id": server_id,
        "active_member_id": "member-0",
        "members": [
            {"member_id": f"member-{index}", "runtime_name": f"runtime-{index}", "presentation_index": index + 1, "is_active": True, "status": status, "fresh": True, "freshness": "fresh", "latency_ms": 20 + index, "checked_at": "2026-01-01 00:00:00"}
            for index in range(count)
        ],
    }


def test_manual_check_refreshes_canonical_health(monkeypatch):
    calls = []
    topology = _topology()
    monkeypatch.setattr(manual_check, "get_logical_topology", lambda _server_id: topology)
    monkeypatch.setattr(manual_check, "check_logical_server_delay", lambda server_id, **kwargs: calls.append((server_id, kwargs)) or {"ok": True, "probe_backend": "runtime_native"})
    result = manual_check.run_manual_check("logical-a")
    assert result["status"] == "success"
    assert result["aggregate"] == {"status": "success", "total": 2, "healthy": 2, "failed": 0}
    assert calls[0][1]["probe_reason"] == "manual_health_refresh"
    assert calls[0][1]["probe_lane"] == "manual"


def test_global_manual_check_batches_all_scope_groups_and_rereads(monkeypatch):
    servers = [
        {"server_id": "global", "preferences": {"global_list": True, "vpn_auto": False}},
        {"server_id": "auto", "preferences": {"global_list": True, "vpn_auto": True}},
        {"server_id": "hidden", "preferences": {"global_list": False, "vpn_auto": True}},
    ]
    topologies = {item["server_id"]: _topology(item["server_id"], 2) for item in servers}
    calls = []
    monkeypatch.setattr(manual_check, "list_servers", lambda **_: servers)
    monkeypatch.setattr(manual_check, "get_logical_topologies", lambda ids: (calls.append(list(ids)) or {item: topologies[item] for item in ids}))
    monkeypatch.setattr(manual_check, "check_logical_server_delays", lambda ids, **kwargs: [{"logical_server_id": item, "ok": True} for item in ids])
    result = manual_check.run_global_manual_check(scope="user_global")
    assert result["status"] == "success"
    assert result["groups"]["total"] == 2
    assert result["members"] == {"total": 4, "success": 4, "failed": 0}
    assert calls == [["auto", "global"], ["auto", "global"]]


def test_global_manual_check_preserves_scope_contract(monkeypatch):
    monkeypatch.setattr(manual_check, "list_servers", lambda **_: [{"server_id": "a", "preferences": {"global_list": True, "vpn_auto": True}}])
    monkeypatch.setattr(manual_check, "get_logical_topologies", lambda ids: {"a": _topology("a", 1)})
    monkeypatch.setattr(manual_check, "check_logical_server_delays", lambda ids, **kwargs: [{"logical_server_id": "a", "ok": True}])
    assert manual_check.run_global_manual_check(scope="admin_all")["scope"] == "admin_all"
    assert manual_check.run_global_manual_check(scope="user_vpn_auto")["scope"] == "user_vpn_auto"


def test_routes_preserve_global_and_per_group_contracts(monkeypatch):
    monkeypatch.setattr(servers_route, "run_global_manual_check", lambda **kwargs: {"api_ok": True, "ok": True, "scope": kwargs["scope"], "status": "success"})
    monkeypatch.setattr(servers_route, "run_manual_check", lambda server_id, **kwargs: {"api_ok": True, "ok": True, "server_id": server_id, "status": "success"})
    client = TestClient(create_app(enable_startup_tasks=False))
    assert client.post("/api/v2/servers/manual-check", json={"scope": "admin_all"}).status_code == 200
    assert client.post("/api/v2/servers/logical-a/manual-check", json={}).status_code == 200
    assert client.post("/api/v2/servers/manual-check", json={"scope": "invalid"}).status_code == 422


def test_manual_check_has_no_selector_or_watchdog_dependency():
    assert "selector" not in manual_check.__dict__
    assert "watchdog" not in manual_check.__dict__
