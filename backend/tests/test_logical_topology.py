from __future__ import annotations

import json
from pathlib import Path

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
