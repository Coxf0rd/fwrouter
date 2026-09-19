from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import db_session, initialize_database
from fwrouter_api.services import logical_topology
from fwrouter_api.services import server_ping


def _configure_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FWROUTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("FWROUTER_DATABASE_URL", f"sqlite:///{tmp_path}/fwrouter.db")
    get_settings.cache_clear()


def _server(server_id: str, name: str, endpoints: list[tuple[str, int]]) -> dict:
    raw = {"name": name, "_fwrouter_runtime_name": name}
    if endpoints:
        raw["_fwrouter_topology"] = {
            "kind": "logical_profile",
            "endpoints": [
                {
                    "identity": member_id,
                    "runtime": {"name": name, "type": "socks5", "server": "203.0.113.10", "port": port},
                }
                for member_id, port in endpoints
            ],
        }
    return {"server_id": server_id, "raw": raw}


def _seed_servers(*servers: dict) -> None:
    with db_session() as connection:
        for server in servers:
            connection.execute(
                "INSERT INTO servers (server_id, server_name, provider_name, raw_json, inventory_state) VALUES (?, ?, 'pytest', ?, 'active')",
                (server["server_id"], server["raw"]["name"], json.dumps(server["raw"])),
            )


def test_topology_keeps_distinct_same_name_profiles_and_member_churn(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    first = _server("logical-a", "Same label", [("member-a", 1001), ("member-b", 1002)])
    second = _server("logical-b", "Same label", [("member-c", 1003)])
    _seed_servers(first, second)
    with db_session() as connection:
        logical_topology.sync_logical_topology(connection, [first, second])
    first_topology = logical_topology.get_logical_topology("logical-a")
    second_topology = logical_topology.get_logical_topology("logical-b")
    assert first_topology["topology_kind"] == "structured_profile"
    assert first_topology["health"]["total_members"] == 2
    assert second_topology["health"]["total_members"] == 1
    changed = _server("logical-a", "Renamed label", [("member-b", 1002), ("member-d", 1004)])
    with db_session() as connection:
        logical_topology.sync_logical_topology(connection, [changed])
    topology = logical_topology.get_logical_topology("logical-a")
    assert [item["member_id"] for item in topology["members"] if item["is_active"]] == ["member-b", "member-d"]
    assert topology["logical_server_id"] == "logical-a"


def test_single_endpoint_is_logical_server_with_one_member(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    server = _server("single-a", "Single Alpha", [])
    _seed_servers(server)
    with db_session() as connection:
        logical_topology.sync_logical_topology(connection, [server])

    topology = logical_topology.get_logical_topology("single-a")

    assert topology["logical_server_id"] == "single-a"
    assert topology["topology_kind"] == "concrete_single"
    assert topology["health"]["total_members"] == 1
    assert topology["members"][0]["member_id"] == "single-a"
    assert topology["members"][0]["runtime_name"] == "Single Alpha"


def test_member_health_aggregates_without_crossing_logical_boundary(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    server = _server("logical-a", "Profile", [("member-a", 1001), ("member-b", 1002)])
    _seed_servers(server)
    with db_session() as connection:
        logical_topology.sync_logical_topology(connection, [server])
        connection.execute("INSERT INTO logical_server_member_health (logical_server_id, member_id, provider_role, status) VALUES ('logical-a', 'member-a', ?, 'failed')", (logical_topology.PROVIDER_ROLE_VPN_DATAPLANE,))
        connection.execute("INSERT INTO logical_server_member_health (logical_server_id, member_id, provider_role, status) VALUES ('logical-a', 'member-b', ?, 'healthy')", (logical_topology.PROVIDER_ROLE_VPN_DATAPLANE,))
    topology = logical_topology.get_logical_topology("logical-a")
    assert topology["health"] == {"status": "usable", "usable_members": 1, "total_members": 2}
    with db_session() as connection:
        connection.execute("UPDATE logical_server_member_health SET status = 'failed' WHERE logical_server_id = 'logical-a'")
    assert logical_topology.get_logical_topology("logical-a")["health"]["status"] == "unavailable"


def test_stale_member_health_is_not_healthy_or_failed(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    server = _server("logical-a", "Profile", [("member-a", 1001)])
    _seed_servers(server)
    with db_session() as connection:
        logical_topology.sync_logical_topology(connection, [server])
        connection.execute(
            """
            INSERT INTO logical_server_member_health (logical_server_id, member_id, provider_role, status, checked_at)
            VALUES ('logical-a', 'member-a', ?, 'healthy', datetime('now', '-3600 seconds'))
            """,
            (logical_topology.PROVIDER_ROLE_VPN_DATAPLANE,),
        )

    topology = logical_topology.get_logical_topology("logical-a")

    assert topology["members"][0]["status"] == "stale"
    assert topology["health"]["status"] == "unknown"


def test_failed_stale_member_reprobe_can_recover_to_healthy(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    server = _server("logical-a", "Profile", [("member-a", 1001)])
    _seed_servers(server)
    with db_session() as connection:
        logical_topology.sync_logical_topology(connection, [server])
        connection.execute(
            """
            INSERT INTO logical_server_member_health (
                logical_server_id,
                member_id,
                provider_role,
                status,
                checked_at,
                consecutive_failures
            )
            VALUES ('logical-a', 'member-a', ?, 'failed', datetime('now', '-3600 seconds'), 3)
            """,
            (logical_topology.PROVIDER_ROLE_VPN_DATAPLANE,),
        )

    class _Adapter:
        def check_delay(self, server_id: str, **kwargs):
            return SimpleNamespace(ok=True, delay_ms=77, error_code=None, error_message=None, details={})

    monkeypatch.setattr(logical_topology, "DEFAULT_MIHOMO_ADAPTER", _Adapter())

    result = logical_topology.check_member_delay("logical-a", "member-a")
    topology = logical_topology.get_logical_topology("logical-a")

    assert result["ok"] is True
    assert result["status"] == "healthy"
    assert topology["members"][0]["status"] == "healthy"
    assert topology["members"][0]["latency_ms"] == 77
    assert topology["health"] == {"status": "usable", "usable_members": 1, "total_members": 1}
    with db_session() as connection:
        row = connection.execute(
            """
            SELECT consecutive_failures
            FROM logical_server_member_health
            WHERE logical_server_id = 'logical-a'
              AND member_id = 'member-a'
              AND provider_role = ?
            """,
            (logical_topology.PROVIDER_ROLE_VPN_DATAPLANE,),
        ).fetchone()
    assert row["consecutive_failures"] == 0


class _FakeDelayAdapter:
    def __init__(self) -> None:
        self.now = "Profile :: member-a"
        self.delays = {
            "Profile": SimpleNamespace(ok=True, delay_ms=200, error_code=None, error_message=None, details={}, to_dict=lambda: {"ok": True, "delay_ms": 200}),
            "Profile :: member-a": SimpleNamespace(ok=True, delay_ms=80, error_code=None, error_message=None, details={}, to_dict=lambda: {"ok": True, "delay_ms": 80}),
            "Profile :: member-b": SimpleNamespace(ok=True, delay_ms=140, error_code=None, error_message=None, details={}, to_dict=lambda: {"ok": True, "delay_ms": 140}),
        }

    def get_proxy_state(self, proxy_name: str) -> dict:
        return {"name": proxy_name, "type": "Fallback", "now": self.now, "all": ["Profile :: member-a", "Profile :: member-b"]}

    def check_delay(self, server_id: str, **kwargs):
        return self.delays[server_id]


def test_active_member_observation_tracks_mihomo_runtime_switch(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    server = _server("logical-a", "Profile", [("member-a", 1001), ("member-b", 1002)])
    _seed_servers(server)
    with db_session() as connection:
        logical_topology.sync_logical_topology(connection, [server])
    adapter = _FakeDelayAdapter()
    monkeypatch.setattr(logical_topology, "DEFAULT_MIHOMO_ADAPTER", adapter)

    first = logical_topology.observe_active_member("logical-a")
    adapter.now = "Profile :: member-b"
    second = logical_topology.observe_active_member("logical-a")

    assert first["member_id"] == "member-a"
    assert second["member_id"] == "member-b"
    assert logical_topology.get_logical_topology("logical-a")["active_member_id"] == "member-b"


def test_logical_ping_uses_effective_member_latency_not_group_or_minimum(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    server = _server("logical-a", "Profile", [("member-a", 1001), ("member-b", 1002)])
    _seed_servers(server)
    with db_session() as connection:
        logical_topology.sync_logical_topology(connection, [server])
    adapter = _FakeDelayAdapter()
    adapter.now = "Profile :: member-b"
    adapter.delays["Profile :: member-a"] = SimpleNamespace(ok=True, delay_ms=10, error_code=None, error_message=None, details={}, to_dict=lambda: {"ok": True, "delay_ms": 10})
    monkeypatch.setattr(logical_topology, "DEFAULT_MIHOMO_ADAPTER", adapter)
    monkeypatch.setattr(server_ping, "DEFAULT_MIHOMO_ADAPTER", adapter)

    result = server_ping.check_server_delay("logical-a", update_state=True, checked_by="selector")

    assert result["ok"] is True
    assert result["last_ping_ms"] == 140
    assert result["active_member_id"] == "member-b"
    topology = logical_topology.get_logical_topology("logical-a")
    member_b = next(item for item in topology["members"] if item["member_id"] == "member-b")
    assert member_b["status"] == "healthy"
    assert member_b["latency_ms"] == 140


def test_server_ping_keeps_direct_runtime_for_custom_server_without_topology(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    with db_session() as connection:
        connection.execute(
            "INSERT INTO servers (server_id, server_name, provider_name, raw_json, inventory_state) VALUES ('custom-a', 'Custom A', 'custom', '{}', 'active')"
        )
    calls: list[str] = []

    class _Adapter:
        def check_delay(self, server_id: str, **kwargs):
            calls.append(server_id)
            return SimpleNamespace(ok=True, delay_ms=55, error_code=None, error_message=None, details={})

    monkeypatch.setattr(server_ping, "DEFAULT_MIHOMO_ADAPTER", _Adapter())

    result = server_ping.check_server_delay("custom-a")

    assert result["ok"] is True
    assert result["last_ping_ms"] == 55
    assert calls == ["Custom A"]


def test_member_probe_honors_budget_and_persists_cursor(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    server = _server("logical-a", "Profile", [("member-a", 1001), ("member-b", 1002), ("member-c", 1003)])
    _seed_servers(server)
    with db_session() as connection:
        logical_topology.sync_logical_topology(connection, [server])
    called: list[tuple[str, str]] = []
    monkeypatch.setattr(logical_topology, "check_member_delay", lambda logical_server_id, member_id, timeout_ms: called.append((logical_server_id, member_id)) or {"ok": True})
    first = logical_topology.probe_members(budget=2)
    second = logical_topology.probe_members(budget=2)
    assert first["probed"] == 2
    assert second["probed"] == 2
    assert len(called) == 4
    assert called[0:2] != called[2:4]


def test_logical_ping_resolves_the_logical_runtime_target(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    server = _server("logical-a", "Profile", [("member-a", 1001), ("member-b", 1002)])
    _seed_servers(server)
    with db_session() as connection:
        logical_topology.sync_logical_topology(connection, [server])
    target = server_ping.resolve_server_runtime_target("logical-a")
    assert target["runtime_target"] == "Profile"
