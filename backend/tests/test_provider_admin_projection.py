from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path


def test_provider_admin_member_projection_keeps_current_applied_and_preferences_separate(monkeypatch) -> None:
    from fwrouter_api.db import provider_managed as store
    from fwrouter_api.services import provider_admin_projection as projection
    from fwrouter_api.services import provider_managed

    binding = {
        "source_ref": "src:" + "a" * 64,
        "provider_id": "stealthsurf",
        "logical_server_id": "logical-provider",
        "protocol": "hysteria2",
        "observed_protocol": "hysteria2",
        "enabled": 1,
        "binding_revision": 4,
        "current_member_id": "102",
        "current_location_id": "us",
        "applied_member_id": "101",
        "applied_protocol": "hysteria2",
        "applied_at": 1234.0,
        "applied_revision": 3,
    }
    members = [
        {"provider_member_id": "101", "location_id": "us", "protocol": "hysteria2", "auto_enabled": 0, "priority": -1, "advertised": 1},
        {"provider_member_id": "102", "location_id": "us", "protocol": "hysteria2", "auto_enabled": 1, "priority": 3, "advertised": 0},
    ]

    @contextmanager
    def fake_db_session():
        yield object()

    monkeypatch.setattr(projection, "db_session", fake_db_session)
    monkeypatch.setattr(store, "list_bindings", lambda _conn: [binding])
    monkeypatch.setattr(store, "list_members", lambda _conn, _source: members)
    monkeypatch.setattr(store, "location_labels", lambda _conn, _source: {"us": "United States"})
    monkeypatch.setattr(provider_managed, "provider_runtime_member_id", lambda b, member, protocol: f"sub:{b['source_ref']}:{member}:{protocol}")

    rows = projection.provider_members_by_logical_id(["logical-provider"])["logical-provider"]

    applied, current = rows
    assert applied["member_id"] == "sub:src:" + "a" * 64 + ":101:hysteria2"
    assert applied["location_label"] == "United States"
    assert applied["is_provider_applied"] is False
    assert applied["is_provider_current"] is False
    assert applied["auto_enabled"] is False
    assert applied["priority"] == -1
    assert current["is_provider_current"] is True
    assert current["is_provider_applied"] is False
    assert current["auto_enabled"] is True
    assert current["priority"] == 3
    assert current["provider_advertised"] is False
    assert current["is_active"] is True


def test_runtime_logical_projection_shows_provider_only_intent_as_unknown(monkeypatch) -> None:
    from fwrouter_api.services import logical_topology

    member = {
        "member_id": "sub:provider:member-a",
        "runtime_name": "sub:provider:member-a",
        "provider_member_id": "member-a",
        "provider_source_ref": "src:" + "b" * 64,
        "provider_protocol": "hysteria2",
        "location_id": "us",
        "location_label": "United States",
        "provider_member_ordinal": 1,
        "is_provider_member": True,
        "is_provider_current": True,
        "is_provider_applied": False,
        "auto_enabled": True,
        "priority": 0,
        "is_active": True,
    }
    monkeypatch.setattr(logical_topology, "get_logical_topologies", lambda _ids: {})
    monkeypatch.setattr(logical_topology, "_provider_members_for_logical_ids", lambda _ids: {"logical-provider": [dict(member)]})

    topology = logical_topology.get_runtime_logical_topologies(["logical-provider"])["logical-provider"]

    assert topology["health"]["status"] == "unknown"
    assert topology["health"]["usable_members"] == 0
    assert topology["health"]["total_members"] == 1
    projected, = topology["members"]
    assert projected["status"] == "unknown"
    assert projected["latency_ms"] is None
    assert projected["is_provider_current"] is True
    assert projected["is_provider_applied"] is False
    assert projected["is_effective_active"] is False
    assert projected["provider_member_id"] == "member-a"


def test_runtime_logical_projection_preserves_local_health_only_for_applied_identity(monkeypatch) -> None:
    from fwrouter_api.services import logical_topology

    provider_row = {
        "member_id": "sub:provider:member-a", "runtime_name": "runtime-member-a",
        "provider_member_id": "member-a", "provider_source_ref": "src:" + "c" * 64,
        "provider_protocol": "hysteria2", "location_id": "us", "location_label": "United States",
        "provider_member_ordinal": 1, "is_provider_member": True,
        "is_provider_current": False, "is_provider_applied": True,
        "auto_enabled": True, "priority": 1, "is_active": True,
    }
    topology = {
        "logical_server_id": "logical-provider", "topology_kind": "provider_managed",
        "selection_policy": "provider_managed", "active_member_id": "sub:provider:member-a",
        "active_member_source": "runtime_native", "runtime_observation_ok": True,
        "effective_latency_ms": 82, "health": {"status": "usable", "usable_members": 1, "total_members": 1},
        "members": [{"member_id": "sub:provider:member-a", "runtime_name": "runtime-member-a",
                     "member_order": 0, "is_active": True, "is_effective_active": True,
                     "presentation_index": 1, "status": "healthy", "fresh": True, "stale": False,
                     "latency_ms": 82, "checked_at": "2026-10-01 00:00:00", "freshness": "fresh"}],
    }
    monkeypatch.setattr(logical_topology, "_provider_members_for_logical_ids", lambda _ids: {"logical-provider": [dict(provider_row)]})
    projected = {"logical-provider": topology}
    logical_topology._merge_provider_members(projected, ["logical-provider"], provider_members={"logical-provider": [dict(provider_row)]})

    member, = projected["logical-provider"]["members"]
    assert member["status"] == "healthy"
    assert member["latency_ms"] == 82
    assert member["is_provider_applied"] is True
    assert member["is_effective_active"] is True
    assert member["is_provider_current"] is False
    assert member["runtime_name"] == "runtime-member-a"
    assert member["is_active"] is True


def test_revision_mismatch_clears_health_and_rebuilds_provider_group_summary() -> None:
    from fwrouter_api.services import logical_topology

    provider_row = {
        "member_id": "sub:provider:member-a", "runtime_name": "synthetic-provider-hash",
        "provider_member_id": "member-a", "provider_source_ref": "src:" + "e" * 64,
        "provider_protocol": "hysteria2", "provider_binding_revision": 9,
        "provider_advertised": True, "location_id": "us", "location_label": "United States",
        "provider_member_ordinal": 1, "is_provider_member": True,
        "is_provider_current": False, "is_provider_applied": False,
        "auto_enabled": True, "priority": 1, "is_active": True,
    }
    topology = {
        "logical_server_id": "logical-provider", "topology_kind": "provider_managed",
        "selection_policy": "provider_managed", "active_member_id": "sub:provider:member-a",
        "active_member_source": "runtime_native", "runtime_observation_ok": True,
        "effective_latency_ms": 82,
        "health": {"status": "usable", "usable_members": 1, "total_members": 1},
        "health_reason": "effective_member_healthy", "breakdown": {"healthy": 1, "failed": 0, "stale": 0, "unknown": 0, "unsupported": 0},
        "members": [{"member_id": "sub:provider:member-a", "runtime_name": "actual-runtime-endpoint",
                     "member_order": 0, "is_active": True, "is_effective_active": True,
                     "presentation_index": 1, "status": "healthy", "fresh": True, "stale": False,
                     "latency_ms": 82, "checked_at": "2026-10-01 00:00:00", "freshness": "fresh"}],
    }
    topologies = {"logical-provider": topology}
    logical_topology._merge_provider_members(
        topologies, ["logical-provider"], provider_members={"logical-provider": [provider_row]},
    )

    result = topologies["logical-provider"]
    member, = result["members"]
    assert member["runtime_name"] == "actual-runtime-endpoint"
    assert member["is_active"] is True
    assert member["status"] == "unknown"
    assert member["latency_ms"] is None
    assert result["effective_latency_ms"] is None
    assert result["health"] == {"status": "unknown", "usable_members": 0, "total_members": 1}
    assert result["breakdown"] == {"healthy": 0, "failed": 0, "stale": 0, "unknown": 1, "unsupported": 0}
    assert result["health_reason"] == "health_evidence_incomplete"


def test_servers_api_enriches_existing_logical_row_and_only_adds_missing_provider_group(monkeypatch) -> None:
    from fwrouter_api.services import custom_servers, logical_topology, provider_admin_projection

    existing = {"server_id": "logical-provider", "server_name": "Existing name", "kind": "vpn_server"}
    monkeypatch.setattr(custom_servers, "list_servers", lambda **_kwargs: [dict(existing)])
    monkeypatch.setattr(custom_servers, "enrich_server_with_custom_metadata", lambda server: server)
    monkeypatch.setattr(provider_admin_projection, "provider_members_by_logical_id", lambda: {"logical-provider": [], "logical-new": []})
    monkeypatch.setattr(logical_topology, "get_runtime_logical_topology", lambda _logical_id: {
        "topology_kind": "provider_managed", "selection_policy": "provider_managed",
        "active_member_id": None, "active_member_source": "runtime_unavailable",
        "runtime_observation_ok": False, "effective_latency_ms": None,
        "health": {"status": "unknown", "usable_members": 0, "total_members": 0},
        "health_reason": "runtime_not_applied", "members": [],
    })

    servers = custom_servers.list_servers_api(inventory_state="active", limit=20)
    by_id = {server["server_id"]: server for server in servers}

    assert set(by_id) == {"logical-provider", "logical-new"}
    assert by_id["logical-provider"]["provider_admin_group"] is True
    assert by_id["logical-provider"]["server_name"] == "Existing name"
    assert by_id["logical-provider"]["kind"] == "vpn_server"
    assert by_id["logical-new"]["provider_group_label"] == "Provider vpn"
    assert by_id["logical-new"]["kind"] == "provider_vpn"
    assert by_id["logical-new"]["topology"]["health_status"] == "unknown"


def test_servers_and_logical_members_endpoints_project_provider_intent_locally(monkeypatch, tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from fwrouter_api.core.config import get_settings
    from fwrouter_api.db.connection import db_session, initialize_database
    from fwrouter_api.db import provider_managed as store
    from fwrouter_api.main import create_app
    from fwrouter_api.services.provider_managed import provider_runtime_member_id

    monkeypatch.setenv("FWROUTER_STATE_DIR", str(tmp_path / "state"))
    get_settings.cache_clear()
    initialize_database()
    source_ref = "src:" + "d" * 64
    binding = {
        "source_ref": source_ref,
        "provider_id": "stealthsurf",
        "logical_server_id": "logical-provider-api",
        "protocol": "hysteria2",
    }
    with db_session() as connection:
        binding = store.save_binding(
            connection, source_ref, "stealthsurf", "123", "logical-provider-api", "hysteria2", True,
        )
        store.save_locations(connection, source_ref, [{"id": "us", "name": "United States"}])
        store.save_members(connection, source_ref, [{
            "provider_member_id": "901", "location_id": "us", "protocol": "hysteria2",
        }], revision=binding["binding_revision"])
        store.record_config(connection, source_ref, binding["binding_revision"], {
            "server_id": "901", "location_id": "us", "protocol": "hysteria2",
        }, expected_binding_revision=binding["binding_revision"])
        connection.execute("UPDATE provider_members SET advertised=0 WHERE source_ref=? AND provider_member_id='901'", (source_ref,))
        store.update_member_preference(
            connection, source_ref, "901", "us", "hysteria2", auto_enabled=False, priority=-1,
            expected_binding_revision=binding["binding_revision"],
        )

    client = TestClient(create_app(enable_startup_tasks=False))
    servers_response = client.get("/api/v2/servers?inventory_state=active&limit=1000")
    assert servers_response.status_code == 200
    [server] = servers_response.json()["data"]["servers"]
    assert server["server_id"] == "logical-provider-api"
    assert server["server_name"] == "Provider vpn"
    assert server["kind"] == "provider_vpn"
    assert server["topology"]["health_status"] == "unknown"
    assert server["topology"]["effective_latency_ms"] is None

    members_response = client.get("/api/v2/servers/logical-provider-api/members")
    assert members_response.status_code == 200
    topology = members_response.json()["data"]["topology"]
    [member] = topology["members"]
    assert topology["health"]["status"] == "unknown"
    assert member["member_id"] == provider_runtime_member_id(binding, "901", "hysteria2")
    assert member["location_label"] == "United States"
    assert member["provider_member_id"] == "901"
    assert member["provider_advertised"] is False
    assert member["is_active"] is True
    assert member["is_provider_current"] is True
    assert member["auto_enabled"] is False
    assert member["priority"] == -1
    assert member["latency_ms"] is None
    assert member["is_provider_applied"] is False
    assert member["is_effective_active"] is False
