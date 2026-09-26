from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import db_session, initialize_database
from fwrouter_api.services import logical_topology
from fwrouter_api.services import server_ping
from fwrouter_api.services.runtime_adapters import (
    RUNTIME_CAPABILITY_HEALTH,
    RUNTIME_CAPABILITY_LIST_SERVERS,
    RUNTIME_CAPABILITY_LOGICAL_GROUP_PROBE,
    RUNTIME_CAPABILITY_LOGICAL_GROUP_PROBE_MANY,
    RUNTIME_CAPABILITY_LOGICAL_GROUP_STATE,
    RUNTIME_CAPABILITY_LOGICAL_GROUP_STATE_MANY,
    RUNTIME_CAPABILITY_LOGICAL_MEMBER_PROBE,
    RUNTIME_ROLE_VPN_DATAPLANE,
    RuntimeAdapterRegistration,
    register_runtime_adapter,
)


def _configure_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FWROUTER_STATE_DIR", str(tmp_path / "state"))
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


class _NativeRuntime:
    def __init__(self, snapshots: dict[str, dict], probe_snapshots: dict[str, dict] | None = None) -> None:
        self.snapshots = snapshots
        self.probe_snapshots = probe_snapshots or snapshots
        self.group_probes: list[str] = []
        self.bulk_probes: list[list[str]] = []
        self.member_probes: list[tuple[str, str]] = []
        self.local_probes: list[str] = []
        self.group_states: list[str] = []

    def get_logical_group_state(self, target: str) -> dict:
        self.group_states.append(target)
        return json.loads(json.dumps(self.snapshots[target]))

    def get_logical_groups_state(self, targets: list[str]) -> list[dict]:
        return [self.get_logical_group_state(target) for target in targets]

    def probe_logical_group(self, target: str, **kwargs) -> dict:
        self.group_probes.append(target)
        return json.loads(json.dumps(self.probe_snapshots[target]))

    def probe_logical_member(self, target: str, member: str, **kwargs) -> dict:
        self.member_probes.append((target, member))
        return json.loads(json.dumps(self.probe_snapshots[target]))

    def probe_logical_groups(self, targets: list[str], **kwargs) -> list[dict]:
        self.bulk_probes.append(list(targets))
        return [json.loads(json.dumps(self.probe_snapshots[target])) for target in targets]

    def check_delay(self, target: str, **kwargs):
        self.local_probes.append(target)
        raise AssertionError("local probe must not run")


def _register_native_runtime(monkeypatch, runtime: _NativeRuntime, *, bulk: bool = False) -> None:
    from fwrouter_api.services import runtime_adapters

    monkeypatch.setattr(runtime_adapters, "_RUNTIME_ADAPTER_REGISTRY", [])
    register_runtime_adapter(
        RuntimeAdapterRegistration(
            role=RUNTIME_ROLE_VPN_DATAPLANE,
            adapter_id="native-runtime",
            capabilities=frozenset(
                {
                    RUNTIME_CAPABILITY_HEALTH,
                    RUNTIME_CAPABILITY_LIST_SERVERS,
                    RUNTIME_CAPABILITY_LOGICAL_GROUP_STATE,
                    RUNTIME_CAPABILITY_LOGICAL_GROUP_STATE_MANY,
                    RUNTIME_CAPABILITY_LOGICAL_GROUP_PROBE,
                    RUNTIME_CAPABILITY_LOGICAL_MEMBER_PROBE,
                }
                | ({RUNTIME_CAPABILITY_LOGICAL_GROUP_PROBE_MANY} if bulk else set())
            ),
            priority=100,
            replacement_targets=frozenset({"native-runtime"}),
            resolver=lambda: {
                "role": RUNTIME_ROLE_VPN_DATAPLANE,
                "adapter_id": "native-runtime",
                "lifecycle_mode": "external",
                "ready": True,
                "source": {"kind": "test"},
            },
            operations_factory=lambda adapter: runtime,
        )
    )


def _snapshot(
    target: str,
    effective: str,
    members: list[tuple[str, str, int | None, str | None]],
) -> dict:
    return {
        "ok": True,
        "logical_runtime_target": target,
        "effective_member_runtime_identity": effective,
        "observed_at": "2026-09-20T00:00:00+00:00",
        "evidence_source": "runtime_native",
        "members": [
            {
                "runtime_identity": identity,
                "status": status,
                "latency_ms": latency,
                "checked_at": checked_at,
                "error_code": None if status == "healthy" else "RUNTIME_MEMBER_UNAVAILABLE",
                "error_message": None,
            }
            for identity, status, latency, checked_at in members
        ],
    }


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


def test_active_observation_follows_current_routing_target_only(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    first = _server("logical-a", "Alpha", [("member-a", 1001)])
    second = _server("logical-b", "Beta", [("member-b", 1002)])
    _seed_servers(first, second)
    from fwrouter_api.services.server_state import ensure_routing_global_state

    ensure_routing_global_state()
    with db_session() as connection:
        logical_topology.sync_logical_topology(connection, [first, second])
        connection.execute(
            "UPDATE routing_global_state SET server_mode = 'auto', active_auto_server_id = 'logical-b' WHERE id = 1"
        )
    checked_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    runtime = _NativeRuntime(
        {
            "Beta": _snapshot("Beta", "Beta :: member-b", [("Beta :: member-b", "healthy", 42, checked_at)]),
            "Alpha": _snapshot("Alpha", "Alpha :: member-a", [("Alpha :: member-a", "healthy", 99, checked_at)]),
        }
    )
    _register_native_runtime(monkeypatch, runtime)

    result = logical_topology.observe_active_paths()

    assert result["observed"] == 1
    assert result["mapped"] == 1
    assert runtime.group_states == ["Beta"]
    topology = logical_topology.get_logical_topology("logical-b")
    assert topology["members"][0]["latency_ms"] == 42
    assert topology["members"][0]["checked_at"] == checked_at.replace("T", " ").replace("+00:00", "")
    assert topology["members"][0]["source"] == "runtime_native"
    assert topology["members"][0]["freshness"] == "fresh"
    other = logical_topology.get_logical_topology("logical-a")
    assert other["members"][0]["latency_ms"] is None


def test_active_lane_imports_only_runtime_effective_member(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    server = _server("logical-active-only", "Profile", [("member-a", 1001), ("member-b", 1002)])
    _seed_servers(server)
    with db_session() as connection:
        logical_topology.sync_logical_topology(connection, [server])
    checked_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    snapshot = _snapshot("Profile", "Profile :: member-b", [
        ("Profile :: member-a", "healthy", 31, checked_at),
        ("Profile :: member-b", "healthy", 42, checked_at),
    ])
    imported = logical_topology._import_runtime_snapshot(
        "logical-active-only", snapshot, adapter={"adapter_id": "test"},
        probe_reason="active_observation", probe_lane="active", effective_only=True,
    )
    assert imported["imported"] == 1
    topology = logical_topology.get_logical_topology("logical-active-only")
    assert [member["status"] for member in topology["members"]] == ["unknown", "healthy"]
    assert topology["members"][1]["checked_at"] == checked_at.replace("T", " ").replace("+00:00", "")


def test_explicit_recovery_health_refresh_persists_successful_group_outcome(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    server = _server("logical-recovery-refresh", "Profile", [("member-a", 1001)])
    _seed_servers(server)
    with db_session() as connection:
        logical_topology.sync_logical_topology(connection, [server])
    checked_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    snapshot = _snapshot("Profile", "Profile :: member-a", [("Profile :: member-a", "healthy", 37, checked_at)])
    logical_topology.import_runtime_health_snapshot(
        "logical-recovery-refresh", snapshot, adapter={"adapter_id": "test"},
        probe_reason="watchdog_recovery", probe_lane="recovery_full_refresh",
    )
    with db_session() as connection:
        outcome = connection.execute("SELECT outcome, checked_at FROM logical_server_group_probe_outcome WHERE logical_server_id = ?", ("logical-recovery-refresh",)).fetchone()
    assert outcome["outcome"] == "success"
    assert outcome["checked_at"] == snapshot["observed_at"].replace("T", " ").replace("+00:00", "")


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


def test_mixed_failed_and_unknown_member_health_is_unknown(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    server = _server("logical-a", "Profile", [("member-a", 1001), ("member-b", 1002)])
    _seed_servers(server)
    with db_session() as connection:
        logical_topology.sync_logical_topology(connection, [server])
        connection.execute(
            "INSERT INTO logical_server_member_health (logical_server_id, member_id, provider_role, status) VALUES ('logical-a', 'member-a', ?, 'failed')",
            (logical_topology.PROVIDER_ROLE_VPN_DATAPLANE,),
        )

    assert logical_topology.get_logical_topology("logical-a")["health"]["status"] == "unknown"


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

    monkeypatch.setattr("fwrouter_api.services.runtime_adapters.DEFAULT_MIHOMO_ADAPTER", _Adapter())

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
    monkeypatch.setattr("fwrouter_api.services.runtime_adapters.DEFAULT_MIHOMO_ADAPTER", adapter)

    first = logical_topology.observe_active_member("logical-a")
    adapter.now = "Profile :: member-b"
    second = logical_topology.observe_active_member("logical-a")

    assert first["member_id"] == "member-a"
    assert second["member_id"] == "member-b"
    assert logical_topology.get_logical_topology("logical-a")["active_member_id"] == "member-b"
    logical_topology.observe_active_member("logical-a")
    with db_session() as connection:
        rows = connection.execute(
            "SELECT details_json FROM operational_logs WHERE event_type = 'logical_effective_member_changed'"
        ).fetchall()
    assert len(rows) == 2
    details = json.loads(rows[-1]["details_json"])
    assert details["old_member_id"] == "member-a"
    assert details["new_member_id"] == "member-b"


def test_member_health_transitions_emit_only_on_state_changes(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    server = _server("logical-health", "Health", [("member-1", 1080)])
    _seed_servers(server)
    with db_session() as connection:
        logical_topology.sync_logical_topology(connection, [server])
    checked = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    evidence = {"adapter_id": "test-adapter", "evidence_source": "runtime_native"}
    with db_session() as connection:
        connection.execute(
            """INSERT INTO logical_server_member_health
            (logical_server_id, member_id, provider_role, status, checked_at)
            VALUES ('logical-health', 'member-1', 'vpn_dataplane', 'healthy', ?)""",
            (checked,),
        )
        logical_topology._persist_member_health(
            connection, logical_server_id="logical-health", member_id="member-1",
            status="failed", latency_ms=None, error_code="PROBE_TIMEOUT",
            error_message="Probe timed out.", evidence=evidence, checked_at=checked,
        )
        logical_topology._persist_member_health(
            connection, logical_server_id="logical-health", member_id="member-1",
            status="failed", latency_ms=None, error_code="PROBE_TIMEOUT",
            error_message="Probe timed out.", evidence=evidence,
            checked_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        )
        logical_topology._persist_member_health(
            connection, logical_server_id="logical-health", member_id="member-1",
            status="healthy", latency_ms=41, error_code=None,
            error_message=None, evidence=evidence,
            checked_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        )
        rows = connection.execute(
            "SELECT details_json FROM operational_logs WHERE event_type = 'logical_member_health_transition' ORDER BY created_at, rowid"
        ).fetchall()

    assert len(rows) == 2
    failed, recovered = [json.loads(row["details_json"]) for row in rows]
    assert failed["old_status"] == "healthy" and failed["new_status"] == "failed"
    assert failed["logical_server_id"] == "logical-health" and failed["member_id"] == "member-1"
    assert failed["adapter_id"] == "test-adapter" and failed["evidence_source"] == "runtime_native"
    assert recovered["old_status"] == "failed" and recovered["new_status"] == "healthy"
    assert recovered["event_code"] == "HEALTH_MEMBER_RECOVERED"


def test_group_probe_timeout_to_success_emits_recovery_transition(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    server = _server("logical-group-health", "Group Health", [])
    _seed_servers(server)
    with db_session() as connection:
        logical_topology.sync_logical_topology(connection, [server])
    checked = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with db_session() as connection:
        connection.execute(
            """INSERT INTO logical_server_group_probe_outcome
            (logical_server_id, provider_role, outcome, checked_at)
            VALUES ('logical-group-health', 'vpn_dataplane', 'timeout', ?)""",
            (checked,),
        )
        logical_topology._persist_group_probe_outcome(
            connection, "logical-group-health",
            {"ok": True, "checked_at": checked, "probe_outcome": "success"},
            {"adapter_id": "test-adapter"},
        )
        rows = connection.execute(
            "SELECT details_json FROM operational_logs WHERE event_type = 'logical_group_probe_transition'"
        ).fetchall()
    assert len(rows) == 1
    details = json.loads(rows[0]["details_json"])
    assert details["old_outcome"] == "timeout"
    assert details["new_outcome"] == "success"
    assert details["adapter_id"] == "test-adapter"


def test_runtime_projection_replaces_stale_persisted_active_member(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    server = _server("logical-a", "Profile", [("member-a", 1001), ("member-b", 1002)])
    _seed_servers(server)
    with db_session() as connection:
        logical_topology.sync_logical_topology(connection, [server])
        connection.execute(
            "UPDATE logical_server_topology SET active_member_id = 'member-a' WHERE logical_server_id = 'logical-a'"
        )
        connection.execute(
            """
            INSERT INTO logical_server_member_health (
                logical_server_id, member_id, provider_role, status, latency_ms
            ) VALUES ('logical-a', 'member-a', ?, 'healthy', 81)
            """,
            (logical_topology.PROVIDER_ROLE_VPN_DATAPLANE,),
        )
    adapter = _FakeDelayAdapter()
    adapter.now = "Profile :: member-b"
    monkeypatch.setattr("fwrouter_api.services.runtime_adapters.DEFAULT_MIHOMO_ADAPTER", adapter)

    topology = logical_topology.get_runtime_logical_topology("logical-a")

    assert topology["logical_server_id"] == "logical-a"
    assert topology["active_member_id"] == "member-b"
    assert topology["active_member_source"] == "runtime_state"
    assert topology["effective_latency_ms"] is None
    assert next(item for item in topology["members"] if item["member_id"] == "member-b")["is_effective_active"] is True
    assert next(item for item in topology["members"] if item["member_id"] == "member-a")["is_effective_active"] is False


def test_member_probe_does_not_change_effective_member(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    server = _server("logical-a", "Profile", [("member-a", 1001), ("member-b", 1002)])
    _seed_servers(server)
    with db_session() as connection:
        logical_topology.sync_logical_topology(connection, [server])
        connection.execute(
            "UPDATE logical_server_topology SET active_member_id = 'member-a' WHERE logical_server_id = 'logical-a'"
        )
    adapter = _FakeDelayAdapter()
    monkeypatch.setattr("fwrouter_api.services.runtime_adapters.DEFAULT_MIHOMO_ADAPTER", adapter)

    result = logical_topology.check_member_delay("logical-a", "member-b")

    assert result["ok"] is True
    assert logical_topology.get_logical_topology("logical-a")["active_member_id"] == "member-a"


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
    monkeypatch.setattr("fwrouter_api.services.runtime_adapters.DEFAULT_MIHOMO_ADAPTER", adapter)

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

    monkeypatch.setattr("fwrouter_api.services.runtime_adapters.DEFAULT_MIHOMO_ADAPTER", _Adapter())

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
    monkeypatch.setattr(logical_topology, "observe_effective_members", lambda: {"observed": 1, "mapped": 1, "results": []})
    monkeypatch.setattr(logical_topology, "_runtime_context", lambda: ({"adapter_id": "local"}, object(), set()))
    monkeypatch.setattr(logical_topology, "check_member_delay", lambda logical_server_id, member_id, **kwargs: called.append((logical_server_id, member_id)) or {"ok": True})
    first = logical_topology.probe_members(budget=2)
    second = logical_topology.probe_members(budget=2)
    assert first["probed"] == 2
    assert second["probed"] == 2
    assert len(called) == 4
    assert called[0:2] != called[2:4]


def test_member_probe_prioritizes_effective_member_and_advances_no_evidence(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    server = _server(
        "logical-a",
        "Profile",
        [("member-a", 1001), ("member-b", 1002), ("member-c", 1003), ("member-d", 1004)],
    )
    _seed_servers(server)
    with db_session() as connection:
        logical_topology.sync_logical_topology(connection, [server])
        connection.execute(
            "UPDATE logical_server_topology SET active_member_id = 'member-c' WHERE logical_server_id = 'logical-a'"
        )
    called: list[tuple[str, str]] = []
    monkeypatch.setattr(logical_topology, "observe_effective_members", lambda: {"observed": 1, "mapped": 1, "results": []})
    monkeypatch.setattr(logical_topology, "_runtime_context", lambda: ({"adapter_id": "local"}, object(), set()))
    monkeypatch.setattr(
        logical_topology,
        "check_member_delay",
        lambda logical_server_id, member_id, **kwargs: called.append((logical_server_id, member_id)) or {"ok": True},
    )

    first = logical_topology.probe_members(budget=2)
    second = logical_topology.probe_members(budget=2)

    assert first["probed"] == 2
    assert called[0] == ("logical-a", "member-c")
    assert len(set(called)) >= 3
    assert first["runtime_observations"]["mapped"] == 1
    assert second["probed"] == 2


def test_fresh_healthy_member_is_not_reprobed_before_ttl(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    server = _server("logical-a", "Profile", [("member-a", 1001), ("member-b", 1002)])
    _seed_servers(server)
    with db_session() as connection:
        logical_topology.sync_logical_topology(connection, [server])
        connection.execute(
            """
            INSERT INTO logical_server_member_health (
                logical_server_id, member_id, provider_role, status, checked_at
            ) VALUES ('logical-a', 'member-a', ?, 'healthy', CURRENT_TIMESTAMP)
            """,
            (logical_topology.PROVIDER_ROLE_VPN_DATAPLANE,),
        )
    called: list[tuple[str, str]] = []
    monkeypatch.setattr(logical_topology, "observe_effective_members", lambda: {"observed": 1, "mapped": 1, "results": []})
    monkeypatch.setattr(logical_topology, "_runtime_context", lambda: ({"adapter_id": "local"}, object(), set()))
    monkeypatch.setattr(
        logical_topology,
        "check_member_delay",
        lambda logical_server_id, member_id, **kwargs: called.append((logical_server_id, member_id)) or {"ok": True},
    )

    logical_topology.probe_members(budget=2)

    assert called == [("logical-a", "member-b")]


def test_failed_member_is_reprobed_after_backoff(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    server = _server("logical-a", "Profile", [("member-a", 1001), ("member-b", 1002)])
    _seed_servers(server)
    with db_session() as connection:
        logical_topology.sync_logical_topology(connection, [server])
        connection.executemany(
            """
            INSERT INTO logical_server_member_health (
                logical_server_id, member_id, provider_role, status, checked_at
            ) VALUES ('logical-a', ?, ?, ?, ?)
            """,
            [
                ("member-a", logical_topology.PROVIDER_ROLE_VPN_DATAPLANE, "failed", "2000-01-01 00:00:00"),
                ("member-b", logical_topology.PROVIDER_ROLE_VPN_DATAPLANE, "healthy", "2999-01-01 00:00:00"),
            ],
        )
    called: list[tuple[str, str]] = []
    monkeypatch.setattr(logical_topology, "observe_effective_members", lambda: {"observed": 1, "mapped": 1, "results": []})
    monkeypatch.setattr(logical_topology, "_runtime_context", lambda: ({"adapter_id": "local"}, object(), set()))
    monkeypatch.setattr(
        logical_topology,
        "check_member_delay",
        lambda logical_server_id, member_id, **kwargs: called.append((logical_server_id, member_id)) or {"ok": True},
    )

    logical_topology.probe_members(budget=2)

    assert called == [("logical-a", "member-a")]


def test_logical_ping_resolves_the_logical_runtime_target(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    server = _server("logical-a", "Profile", [("member-a", 1001), ("member-b", 1002)])
    _seed_servers(server)
    with db_session() as connection:
        logical_topology.sync_logical_topology(connection, [server])
    target = server_ping.resolve_server_runtime_target("logical-a")
    assert target["runtime_target"] == "Profile"


def test_native_health_runtime_owns_background_probe_without_local_probe(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    server = _server("single-a", "Single", [])
    _seed_servers(server)
    with db_session() as connection:
        logical_topology.sync_logical_topology(connection, [server])
    runtime = _NativeRuntime(
        {"Single": _snapshot("Single", "Single", [("Single", "unknown", None, None)])},
        {"Single": _snapshot("Single", "Single", [("Single", "healthy", 71, "2999-01-01T00:00:00+00:00")])},
    )
    _register_native_runtime(monkeypatch, runtime)

    result = logical_topology.probe_members(budget=1)

    assert result["probe_backend"] == "runtime_native"
    assert runtime.member_probes == [("Single", "Single")]
    assert runtime.local_probes == []
    topology = logical_topology.get_logical_topology("single-a")
    assert topology["members"][0]["status"] == "healthy"
    with db_session() as connection:
        evidence = json.loads(
            connection.execute(
                "SELECT evidence_json FROM logical_server_member_health WHERE logical_server_id = 'single-a'"
            ).fetchone()["evidence_json"]
        )
    assert evidence["adapter_id"] == "native-runtime"
    assert evidence["evidence_source"] == "runtime_native"
    assert evidence["probe_lane"] == "background"


def test_native_background_probe_coalesces_selected_logical_groups(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    server_a = _server("single-a", "Single A", [])
    server_b = _server("single-b", "Single B", [])
    _seed_servers(server_a, server_b)
    with db_session() as connection:
        logical_topology.sync_logical_topology(connection, [server_a, server_b])
    runtime = _NativeRuntime(
        {
            "Single A": _snapshot("Single A", "Single A", [("Single A", "unknown", None, None)]),
            "Single B": _snapshot("Single B", "Single B", [("Single B", "unknown", None, None)]),
        },
        {
            "Single A": _snapshot("Single A", "Single A", [("Single A", "healthy", 61, "2999-01-01T00:00:00+00:00")]),
            "Single B": _snapshot("Single B", "Single B", [("Single B", "healthy", 72, "2999-01-01T00:00:01+00:00")]),
        },
    )
    _register_native_runtime(monkeypatch, runtime, bulk=True)

    result = logical_topology.probe_members(budget=2)

    assert result["probe_backend"] == "runtime_native"
    assert runtime.bulk_probes == [["Single A", "Single B"]]
    assert runtime.group_probes == []
    assert runtime.member_probes == []
    assert runtime.local_probes == []


def test_runtime_without_native_capability_uses_local_probe(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    server = _server("single-a", "Single", [])
    _seed_servers(server)
    with db_session() as connection:
        logical_topology.sync_logical_topology(connection, [server])
    calls: list[str] = []

    class _LocalRuntime:
        def check_delay(self, target: str, **kwargs):
            calls.append(target)
            return SimpleNamespace(ok=True, delay_ms=63, error_code=None, error_message=None, details={})

    monkeypatch.setattr("fwrouter_api.services.runtime_adapters.DEFAULT_MIHOMO_ADAPTER", _LocalRuntime())

    result = logical_topology.check_member_delay("single-a", "single-a")

    assert result["probe_backend"] == "local_fallback"
    assert calls == ["Single"]


def test_runtime_state_without_refresh_capability_uses_local_probe(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    server = _server("single-a", "Single", [])
    _seed_servers(server)
    with db_session() as connection:
        logical_topology.sync_logical_topology(connection, [server])
    calls: list[str] = []

    class _StateOnlyRuntime:
        def get_logical_group_state(self, target: str) -> dict:
            return _snapshot(target, target, [(target, "healthy", 41, "2999-01-01T00:00:00+00:00")])

        def check_delay(self, target: str, **kwargs):
            calls.append(target)
            return SimpleNamespace(ok=True, delay_ms=65, error_code=None, error_message=None, details={})

    from fwrouter_api.services import runtime_adapters

    runtime = _StateOnlyRuntime()
    monkeypatch.setattr(runtime_adapters, "_RUNTIME_ADAPTER_REGISTRY", [])
    register_runtime_adapter(
        RuntimeAdapterRegistration(
            role=RUNTIME_ROLE_VPN_DATAPLANE,
            adapter_id="state-only-runtime",
            capabilities=frozenset({RUNTIME_CAPABILITY_LOGICAL_GROUP_STATE}),
            priority=100,
            replacement_targets=frozenset({"state-only-runtime"}),
            resolver=lambda: {
                "role": RUNTIME_ROLE_VPN_DATAPLANE,
                "adapter_id": "state-only-runtime",
                "lifecycle_mode": "external",
                "ready": True,
                "source": {"kind": "test"},
            },
            operations_factory=lambda adapter: runtime,
        )
    )

    result = logical_topology.check_member_delay("single-a", "single-a")

    assert result["probe_backend"] == "local_fallback"
    assert calls == ["Single"]


def test_native_runtime_timestamp_remains_stale_after_import(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    server = _server("single-a", "Single", [])
    _seed_servers(server)
    with db_session() as connection:
        logical_topology.sync_logical_topology(connection, [server])
    runtime = _NativeRuntime(
        {"Single": _snapshot("Single", "Single", [("Single", "healthy", 54, "2000-01-01T00:00:00+00:00")])}
    )
    _register_native_runtime(monkeypatch, runtime)

    logical_topology.observe_effective_members()
    topology = logical_topology.get_logical_topology("single-a")

    assert topology["members"][0]["status"] == "stale"
    assert topology["members"][0]["checked_at"] == "2000-01-01 00:00:00"
    assert topology["health"]["status"] == "unknown"


def test_native_effective_member_controls_persisted_active_and_logical_latency(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    server = _server("logical-a", "Profile", [("member-a", 1001), ("member-b", 1002)])
    _seed_servers(server)
    with db_session() as connection:
        logical_topology.sync_logical_topology(connection, [server])
    member_a = "Profile :: member-a"
    member_b = "Profile :: member-b"
    runtime = _NativeRuntime(
        {
            "Profile": _snapshot(
                "Profile",
                member_b,
                [
                    (member_a, "healthy", 9, "2999-01-01T00:00:00+00:00"),
                    (member_b, "healthy", 143, "2999-01-01T00:00:01+00:00"),
                ],
            )
        }
    )
    _register_native_runtime(monkeypatch, runtime)

    measured = server_ping.check_server_delay("logical-a", update_state=True, checked_by="selector", source="selector")
    topology = logical_topology.get_runtime_logical_topology("logical-a")

    assert measured["probe_backend"] == "runtime_native"
    assert measured["active_member_id"] == "member-b"
    assert measured["last_ping_ms"] == 143
    assert topology["active_member_id"] == "member-b"
    assert topology["effective_latency_ms"] == 143
    assert runtime.group_probes == ["Profile"]
    assert runtime.local_probes == []


def test_native_manual_member_ping_uses_member_operation(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    server = _server("logical-a", "Profile", [("member-a", 1001), ("member-b", 1002)])
    _seed_servers(server)
    with db_session() as connection:
        logical_topology.sync_logical_topology(connection, [server])
    member_a = "Profile :: member-a"
    member_b = "Profile :: member-b"
    runtime = _NativeRuntime(
        {
            "Profile": _snapshot(
                "Profile",
                member_a,
                [(member_a, "healthy", 60, "2999-01-01T00:00:00+00:00"), (member_b, "unknown", None, None)],
            )
        },
        {
            "Profile": _snapshot(
                "Profile",
                member_a,
                [(member_a, "healthy", 60, "2999-01-01T00:00:00+00:00"), (member_b, "healthy", 82, "2999-01-01T00:00:01+00:00")],
            )
        },
    )
    _register_native_runtime(monkeypatch, runtime)

    result = logical_topology.check_member_delay("logical-a", "member-b")

    assert result["ok"] is True
    assert result["latency_ms"] == 82
    assert runtime.member_probes == [("Profile", member_b)]
    assert runtime.group_probes == []


def test_group_timeout_does_not_synthesize_member_failures(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    server = _server("logical-a", "Profile", [("member-a", 1080), ("member-b", 1081)])
    _seed_servers(server)
    with db_session() as connection:
        logical_topology.sync_logical_topology(connection, [server])
    topology = logical_topology.get_logical_topology("logical-a")
    assert topology is not None
    adapter = {"adapter_id": "test-runtime"}
    for member in topology["members"]:
        with db_session() as connection:
            connection.execute(
                "INSERT INTO logical_server_member_health (logical_server_id, member_id, provider_role, status, latency_ms, checked_at) VALUES (?, ?, 'vpn_dataplane', 'healthy', 42, '2026-01-01 00:00:00')",
                ("logical-a", member["member_id"]),
            )
    failure = {"ok": False, "logical_runtime_target": "Profile", "error_code": "TIMEOUT", "error_message": "Timed out.", "members": []}
    before = [(item["status"], item["latency_ms"]) for item in (logical_topology.get_logical_topology("logical-a") or {})["members"]]
    result = logical_topology._import_runtime_snapshot("logical-a", failure, adapter=adapter, probe_reason="active_observation", probe_lane="active")
    assert result["imported"] == 0
    after = [(item["status"], item["latency_ms"]) for item in (logical_topology.get_logical_topology("logical-a") or {})["members"]]
    assert after == before
    result = logical_topology._import_runtime_snapshot("logical-a", failure, adapter=adapter, probe_reason="manual_health_refresh", probe_lane="manual", record_probe_outcome=True)
    assert result["imported"] == 0
    refreshed = logical_topology.get_logical_topology("logical-a")
    assert refreshed is not None
    assert [(item["status"], item["latency_ms"]) for item in refreshed["members"]] == before
    with db_session() as connection:
        outcome = connection.execute("SELECT outcome FROM logical_server_group_probe_outcome WHERE logical_server_id = 'logical-a'").fetchone()
    assert outcome["outcome"] == "timeout"
    batch = logical_topology.get_logical_topologies(["logical-a"])["logical-a"]
    assert batch["group_probe_outcome"]["outcome"] == "timeout"
    assert batch["breakdown"] == {"healthy": 0, "failed": 0, "stale": 2, "unknown": 0, "unsupported": 0}
    from fwrouter_api.services.server_inventory import list_servers
    api_server = next(item for item in list_servers() if item["server_id"] == "logical-a")
    assert api_server["topology"]["group_probe_outcome"]["outcome"] == "timeout"
    assert api_server["topology"]["breakdown"] == batch["breakdown"]
    import httpx
    assert logical_topology._probe_error_code(httpx.ReadTimeout("timed out"), "GROUP") == "GROUP_TIMEOUT"
    assert logical_topology._probe_outcome("GROUP_TIMEOUT") == "timeout"
    assert logical_topology._probe_outcome("HTTP_404") == "runtime_missing"
    assert logical_topology._probe_outcome("NETWORK_ERROR") == "transport_error"


def test_older_group_probe_does_not_emit_rejected_health_transition(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    server = _server("logical-old-probe", "Old Probe", [])
    _seed_servers(server)
    with db_session() as connection:
        logical_topology.sync_logical_topology(connection, [server])
    with db_session() as connection:
        connection.execute(
            """INSERT INTO logical_server_group_probe_outcome
            (logical_server_id, provider_role, outcome, checked_at)
            VALUES ('logical-old-probe', 'vpn_dataplane', 'success', '2026-09-25 12:00:00')"""
        )
        before = connection.execute(
            "SELECT COUNT(*) FROM operational_logs WHERE event_type = 'logical_group_probe_transition'"
        ).fetchone()[0]
        logical_topology._persist_group_probe_outcome(
            connection,
            "logical-old-probe",
            {
                "ok": False,
                "probe_outcome": "timeout",
                "checked_at": "2026-09-25 11:59:00",
                "error_code": "GROUP_TIMEOUT",
                "error_message": "Timed out.",
            },
            {"adapter_id": "test-adapter"},
        )
        current = connection.execute(
            "SELECT outcome, checked_at FROM logical_server_group_probe_outcome WHERE logical_server_id = 'logical-old-probe'"
        ).fetchone()
        after = connection.execute(
            "SELECT COUNT(*) FROM operational_logs WHERE event_type = 'logical_group_probe_transition'"
        ).fetchone()[0]

    assert current["outcome"] == "success"
    assert current["checked_at"] == "2026-09-25 12:00:00"
    assert after == before
