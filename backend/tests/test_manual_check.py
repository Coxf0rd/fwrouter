from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from fwrouter_api.main import create_app
from fwrouter_api.routes import servers as servers_route
from fwrouter_api.services import manual_check
from fwrouter_api.services.runtime_adapters import (
    RUNTIME_CAPABILITY_LOGICAL_GROUP_PROBE_MANY,
    RUNTIME_ROLE_VPN_DATAPLANE,
)


def _topology(count: int = 2) -> dict[str, object]:
    return {
        "logical_server_id": "logical-a",
        "members": [
            {
                "member_id": f"member-{index}",
                "runtime_name": f"runtime-{index}",
                "presentation_index": index + 1,
                "is_active": True,
            }
            for index in range(count)
        ],
    }


def _setup(monkeypatch, operations, topology=None):
    persisted = []
    monkeypatch.setattr(manual_check, "get_logical_topology", lambda _server_id: topology or _topology())
    monkeypatch.setattr(manual_check, "get_logical_runtime_name", lambda _server_id: "logical-runtime")
    monkeypatch.setattr(manual_check, "active_runtime_adapter", lambda _role: {
        "adapter_id": "test-runtime",
        "capabilities": [RUNTIME_CAPABILITY_LOGICAL_GROUP_PROBE_MANY],
    })
    monkeypatch.setattr(manual_check, "runtime_adapter_operations", lambda _adapter: operations)
    monkeypatch.setattr(manual_check, "_persist_manual_result", lambda *args, **kwargs: persisted.append((args, kwargs)))
    return persisted


def test_manual_check_probes_all_members_and_keeps_latency_lane(monkeypatch):
    class Operations:
        def probe_logical_groups(self, targets, *, test_url, timeout_ms):
            return [{
                "ok": True,
                "logical_runtime_target": targets[0],
                "observed_at": "2026-01-01T00:00:00+00:00",
                "evidence_source": "runtime_native",
                "members": [
                    {"runtime_identity": "runtime-0", "status": "healthy", "latency_ms": 31, "checked_at": "2026-01-01T00:00:00+00:00"},
                    {"runtime_identity": "runtime-1", "status": "healthy", "latency_ms": 42, "checked_at": "2026-01-01T00:00:00+00:00"},
                ],
            }]

    persisted = _setup(monkeypatch, Operations())
    result = manual_check.run_manual_check("logical-a")

    assert result["ok"] is True
    assert result["status"] == "success"
    assert [item["member_id"] for item in result["members"]] == ["member-0", "member-1"]
    assert result["aggregate"] == {"status": "success", "total": 2, "healthy": 2, "failed": 0}
    assert persisted[0][1]["latency_ms"] is None


def test_manual_check_single_member_persists_latency(monkeypatch):
    class Operations:
        def probe_logical_groups(self, targets, *, test_url, timeout_ms):
            return [{
                "ok": True,
                "logical_runtime_target": targets[0],
                "observed_at": "2026-01-01T00:00:00+00:00",
                "members": [{"runtime_identity": "runtime-0", "status": "healthy", "latency_ms": 19}],
            }]

    persisted = _setup(monkeypatch, Operations(), topology=_topology(1))
    result = manual_check.run_manual_check("logical-a")

    assert result["status"] == "success"
    assert result["members"][0]["latency_ms"] == 19
    assert persisted[0][1]["latency_ms"] == 19


def test_manual_writer_preserves_background_lane(monkeypatch, tmp_path):
    from fwrouter_api.core.config import get_settings
    from fwrouter_api.db.connection import db_session, initialize_database

    monkeypatch.setenv("FWROUTER_STATE_DIR", str(tmp_path / "state"))
    get_settings.cache_clear()
    initialize_database()
    with db_session() as connection:
        connection.execute("INSERT INTO servers (server_id, server_name, inventory_state) VALUES ('logical-a', 'Logical A', 'active')")
        connection.execute(
            "INSERT INTO server_ping_state (server_id, status, last_ping_ms, checked_at, checked_by) VALUES ('logical-a', 'success', 77, '2026-01-01 00:00:00', 'background')"
        )

    manual_check._persist_manual_result(
        "logical-a", status="success", latency_ms=19, checked_at="2026-01-01T00:00:01+00:00",
        checked_by="test", error_code=None, error_message=None, metadata={"operation": "manual_check"},
    )
    with db_session() as connection:
        row = connection.execute(
            "SELECT status, last_ping_ms, checked_by, manual_status, manual_ping_ms, manual_checked_by FROM server_ping_state WHERE server_id = 'logical-a'"
        ).fetchone()
    assert row["status"] == "success"
    assert row["last_ping_ms"] == 77
    assert row["checked_by"] == "background"
    assert row["manual_status"] == "success"
    assert row["manual_ping_ms"] == 19
    assert row["manual_checked_by"] == "test"


def test_manual_check_does_not_change_canonical_health_or_effective_member(monkeypatch, tmp_path):
    from fwrouter_api.core.config import get_settings
    from fwrouter_api.db.connection import db_session, initialize_database

    monkeypatch.setenv("FWROUTER_STATE_DIR", str(tmp_path / "state"))
    get_settings.cache_clear()
    initialize_database()
    with db_session() as connection:
        connection.execute("INSERT INTO servers (server_id, server_name, inventory_state) VALUES ('logical-a', 'Logical A', 'active')")
        connection.execute("INSERT INTO logical_server_topology (logical_server_id, topology_kind, selection_policy, active_member_id) VALUES ('logical-a', 'logical_multi', 'fallback', 'member-0')")
        for index in range(2):
            connection.execute(
                "INSERT INTO logical_server_members (logical_server_id, member_id, member_runtime_name, member_config_json, transport_fingerprint, member_order) VALUES ('logical-a', ?, ?, '{}', ?, ?)",
                (f"member-{index}", f"runtime-{index}", f"fingerprint-{index}", index),
            )
        connection.execute(
            "INSERT INTO logical_server_member_health (logical_server_id, member_id, provider_role, status, latency_ms, checked_at, evidence_json) VALUES ('logical-a', 'member-0', 'vpn_dataplane', 'healthy', 88, '2026-01-01 00:00:00', '{}')"
        )

    class Operations:
        def probe_logical_groups(self, targets, *, test_url, timeout_ms):
            return [{
                "ok": True,
                "logical_runtime_target": targets[0],
                "observed_at": "2026-01-01T00:00:00+00:00",
                "members": [
                    {"runtime_identity": "runtime-0", "status": "healthy", "latency_ms": 11},
                    {"runtime_identity": "runtime-1", "status": "healthy", "latency_ms": 12},
                ],
            }]

    monkeypatch.setattr(manual_check, "active_runtime_adapter", lambda _role: {
        "adapter_id": "test-runtime", "capabilities": [RUNTIME_CAPABILITY_LOGICAL_GROUP_PROBE_MANY],
    })
    monkeypatch.setattr(manual_check, "runtime_adapter_operations", lambda _adapter: Operations())
    monkeypatch.setattr(manual_check, "get_logical_runtime_name", lambda _server_id: "logical-runtime")
    before = None
    with db_session() as connection:
        before = connection.execute("SELECT status, latency_ms FROM logical_server_member_health WHERE logical_server_id = 'logical-a' AND member_id = 'member-0'").fetchone()
        topology_before = connection.execute("SELECT active_member_id FROM logical_server_topology WHERE logical_server_id = 'logical-a'").fetchone()

    result = manual_check.run_manual_check("logical-a")
    assert result["ok"] is True
    with db_session() as connection:
        after = connection.execute("SELECT status, latency_ms FROM logical_server_member_health WHERE logical_server_id = 'logical-a' AND member_id = 'member-0'").fetchone()
        topology_after = connection.execute("SELECT active_member_id FROM logical_server_topology WHERE logical_server_id = 'logical-a'").fetchone()
    assert tuple(after) == tuple(before)
    assert tuple(topology_after) == tuple(topology_before)


def test_manual_check_adapter_exception_is_structured(monkeypatch):
    class Operations:
        def probe_logical_groups(self, targets, *, test_url, timeout_ms):
            raise TimeoutError("runtime timeout")

    _setup(monkeypatch, Operations())
    result = manual_check.run_manual_check("logical-a")
    assert result["ok"] is False
    assert result["api_ok"] is False
    assert result["error_code"] == "RUNTIME_MANUAL_CHECK_FAILED"


def test_manual_check_has_no_selector_or_watchdog_dependency():
    assert "selector" not in manual_check.__dict__
    assert "watchdog" not in manual_check.__dict__


def test_manual_check_returns_partial_result_for_timeout(monkeypatch):
    class Operations:
        def probe_logical_groups(self, targets, *, test_url, timeout_ms):
            return [{
                "ok": False,
                "logical_runtime_target": targets[0],
                "observed_at": "2026-01-01T00:00:00+00:00",
                "members": [
                    {"runtime_identity": "runtime-0", "status": "healthy", "latency_ms": 31},
                    {"runtime_identity": "runtime-1", "status": "failed", "error_code": "TIMEOUT", "error_message": "Timed out."},
                ],
            }]

    _setup(monkeypatch, Operations())
    result = manual_check.run_manual_check("logical-a")

    assert result["ok"] is True
    assert result["status"] == "partial"
    assert result["members"][1]["error_code"] == "TIMEOUT"


def test_manual_check_reports_unsupported_without_canonical_health(monkeypatch):
    persisted = _setup(monkeypatch, SimpleNamespace(), topology=_topology(1))
    monkeypatch.setattr(manual_check, "active_runtime_adapter", lambda _role: {
        "adapter_id": "unsupported",
        "capabilities": [],
    })

    result = manual_check.run_manual_check("logical-a")

    assert result["ok"] is False
    assert result["error_code"] == "RUNTIME_MANUAL_CHECK_UNSUPPORTED"
    assert persisted


def test_manual_check_route_contract(monkeypatch):
    monkeypatch.setattr(
        servers_route,
        "run_manual_check",
        lambda server_id, *, timeout_ms, checked_by: {
            "api_ok": True,
            "ok": True,
            "server_id": server_id,
            "status": "success",
            "aggregate": {"status": "success", "total": 1, "healthy": 1, "failed": 0},
            "members": [],
        },
    )
    client = TestClient(create_app(enable_startup_tasks=False))
    response = client.post("/api/v2/servers/logical-a/manual-check", json={"timeout_ms": 1500})

    assert response.status_code == 200
    assert response.json()["data"]["manual_check"]["server_id"] == "logical-a"


def test_global_manual_check_scope_and_single_batch(monkeypatch):
    servers = [
        {"server_id": "admin-only", "preferences": {"global_list": False, "vpn_auto": False}},
        {"server_id": "global", "preferences": {"global_list": True, "vpn_auto": False}},
        {"server_id": "auto", "preferences": {"global_list": True, "vpn_auto": True}},
        {"server_id": "hidden-auto", "preferences": {"global_list": False, "vpn_auto": True}},
    ]
    topologies = {item["server_id"]: _topology(2) | {"logical_server_id": item["server_id"]} for item in servers}
    calls = []

    class Operations:
        def probe_logical_groups(self, targets, *, test_url, timeout_ms):
            calls.append(list(targets))
            return [{
                "logical_runtime_target": target,
                "observed_at": "2026-01-01T00:00:00+00:00",
                "members": [
                    {"runtime_identity": "runtime-0", "status": "healthy", "latency_ms": 11},
                    {"runtime_identity": "runtime-1", "status": "healthy", "latency_ms": 12},
                ],
            } for target in targets]

    monkeypatch.setattr(manual_check, "list_servers", lambda **_: servers)
    monkeypatch.setattr(manual_check, "get_logical_topologies", lambda ids: {item: topologies[item] for item in ids})
    monkeypatch.setattr(manual_check, "get_logical_runtime_name", lambda item: item)
    monkeypatch.setattr(manual_check, "active_runtime_adapter", lambda _role: {"adapter_id": "test", "capabilities": [RUNTIME_CAPABILITY_LOGICAL_GROUP_PROBE_MANY]})
    monkeypatch.setattr(manual_check, "runtime_adapter_operations", lambda _adapter: Operations())
    monkeypatch.setattr(manual_check, "_persist_manual_result", lambda *args, **kwargs: None)

    result = manual_check.run_global_manual_check(scope="user_vpn_auto")
    assert result["status"] == "success"
    assert result["groups"]["total"] == 1
    assert result["members"]["total"] == 2
    assert len(calls) == 1
    assert calls[0] == ["auto"]


def test_global_manual_check_all_scopes_and_batch_failure_persists_each_group(monkeypatch):
    servers = [
        {"server_id": "global", "preferences": {"global_list": True, "vpn_auto": False}},
        {"server_id": "auto", "preferences": {"global_list": True, "vpn_auto": True}},
        {"server_id": "hidden-auto", "preferences": {"global_list": False, "vpn_auto": True}},
        {"server_id": "deleted", "preferences": {"global_list": True, "vpn_auto": True, "manually_deleted_at": "now"}},
    ]
    topologies = {item["server_id"]: _topology(3) | {"logical_server_id": item["server_id"]} for item in servers[:3]}
    persisted = []
    monkeypatch.setattr(manual_check, "list_servers", lambda **_: servers)
    monkeypatch.setattr(manual_check, "get_logical_topologies", lambda ids: {item: topologies[item] for item in ids})
    monkeypatch.setattr(manual_check, "get_logical_runtime_name", lambda item: item)
    monkeypatch.setattr(manual_check, "active_runtime_adapter", lambda _role: {"adapter_id": "test", "capabilities": [RUNTIME_CAPABILITY_LOGICAL_GROUP_PROBE_MANY]})
    monkeypatch.setattr(manual_check, "_persist_manual_result", lambda *args, **kwargs: persisted.append((args, kwargs)))

    class Operations:
        def probe_logical_groups(self, targets, *, test_url, timeout_ms):
            raise TimeoutError("batch timeout")

    monkeypatch.setattr(manual_check, "runtime_adapter_operations", lambda _adapter: Operations())
    assert manual_check._global_manual_server_ids("admin_all") == ["auto", "global", "hidden-auto"]
    assert manual_check._global_manual_server_ids("user_global") == ["auto", "global"]
    assert manual_check._global_manual_server_ids("user_vpn_auto") == ["auto"]
    result = manual_check.run_global_manual_check(scope="admin_all")
    assert result["api_ok"] is True
    assert result["status"] == "failed"
    assert result["groups"] == {"total": 3, "success": 0, "partial": 0, "failed": 3}
    assert result["members"] == {"total": 9, "success": 0, "failed": 9}
    assert len(persisted) == 3


def test_global_manual_check_route_requires_scope(monkeypatch):
    monkeypatch.setattr(servers_route, "run_global_manual_check", lambda **kwargs: {
        "api_ok": True, "ok": True, "scope": kwargs["scope"], "status": "success",
        "groups": {"total": 1, "success": 1, "partial": 0, "failed": 0},
        "members": {"total": 3, "success": 3, "failed": 0}, "results": [],
    })
    client = TestClient(create_app(enable_startup_tasks=False))
    response = client.post("/api/v2/servers/manual-check", json={"scope": "user_global"})
    assert response.status_code == 200
    assert response.json()["data"]["manual_check"]["scope"] == "user_global"
    assert client.post("/api/v2/servers/manual-check", json={"scope": "invalid"}).status_code == 422


def test_global_manual_check_unsupported_persists_each_member(monkeypatch):
    servers = [{"server_id": "logical-a", "preferences": {"global_list": True, "vpn_auto": True}}]
    monkeypatch.setattr(manual_check, "list_servers", lambda **_: servers)
    monkeypatch.setattr(manual_check, "get_logical_topologies", lambda ids: {"logical-a": _topology(2)})
    monkeypatch.setattr(manual_check, "get_logical_runtime_name", lambda item: item)
    monkeypatch.setattr(manual_check, "active_runtime_adapter", lambda _role: {"adapter_id": "unsupported", "capabilities": []})
    monkeypatch.setattr(manual_check, "runtime_adapter_operations", lambda _adapter: SimpleNamespace())
    persisted = []
    monkeypatch.setattr(manual_check, "_persist_manual_result", lambda *args, **kwargs: persisted.append(kwargs))
    result = manual_check.run_global_manual_check(scope="admin_all")
    assert result["members"] == {"total": 2, "success": 0, "failed": 2}
    assert result["results"][0]["members"][0]["error_code"] == "RUNTIME_MANUAL_CHECK_UNSUPPORTED"
    assert len(persisted) == 1
