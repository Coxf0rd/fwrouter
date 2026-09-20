from __future__ import annotations

import json
from pathlib import Path

from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import db_session, initialize_database
from fwrouter_api.services import logical_topology
from fwrouter_api.services import mihomo_config as _mihomo_config
from fwrouter_api.services import mihomo_reconcile as mihomo_reconcile_service
from fwrouter_api.services import mihomo_reconcile_fingerprint as fingerprint_service
from fwrouter_api.services import subscription_pipeline
from fwrouter_api.services import xray_subscription_service


def _configure_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FWROUTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("FWROUTER_DATABASE_URL", f"sqlite:///{tmp_path}/fwrouter.db")
    get_settings.cache_clear()


def _seed_topology(server_id: str, name: str, members: list[str]) -> None:
    raw = {"name": name, "_fwrouter_runtime_name": name}
    raw["_fwrouter_topology"] = {
        "kind": "logical_profile",
        "endpoints": [
            {"identity": member, "runtime": {"name": f"{name} :: {member}"}}
            for member in members
        ],
    }
    server = {"server_id": server_id, "raw": raw}
    with db_session() as connection:
        connection.execute(
            "INSERT INTO servers (server_id, server_name, provider_name, raw_json, inventory_state) "
            "VALUES (?, ?, 'pytest', ?, 'active')",
            (server_id, name, json.dumps(raw)),
        )
        logical_topology.sync_logical_topology(connection, [server])


class _BatchRuntime:
    def __init__(self, snapshots: dict[str, dict]) -> None:
        self.snapshots = snapshots
        self.batch_calls: list[list[str]] = []
        self.single_calls: list[str] = []

    def get_logical_groups_state(self, targets: list[str]) -> list[dict]:
        self.batch_calls.append(list(targets))
        return [self.snapshots[target] for target in targets]

    def get_logical_group_state(self, target: str) -> dict:
        self.single_calls.append(target)
        return self.snapshots[target]


def _register_runtime(monkeypatch, runtime: _BatchRuntime, *, batch: bool = True) -> None:
    from fwrouter_api.services import runtime_adapters
    from fwrouter_api.services.runtime_adapters import (
        RUNTIME_CAPABILITY_HEALTH,
        RUNTIME_CAPABILITY_LOGICAL_GROUP_STATE,
        RUNTIME_CAPABILITY_LOGICAL_GROUP_STATE_MANY,
        RUNTIME_ROLE_VPN_DATAPLANE,
        RuntimeAdapterRegistration,
        register_runtime_adapter,
    )

    monkeypatch.setattr(runtime_adapters, "_RUNTIME_ADAPTER_REGISTRY", [])
    capabilities = {RUNTIME_CAPABILITY_HEALTH, RUNTIME_CAPABILITY_LOGICAL_GROUP_STATE}
    if batch:
        capabilities.add(RUNTIME_CAPABILITY_LOGICAL_GROUP_STATE_MANY)
    register_runtime_adapter(
        RuntimeAdapterRegistration(
            role=RUNTIME_ROLE_VPN_DATAPLANE,
            adapter_id="pytest-batch",
            capabilities=frozenset(capabilities),
            priority=100,
            replacement_targets=frozenset(),
            resolver=lambda: {"adapter_id": "pytest-batch", "ready": True},
            operations_factory=lambda _adapter: runtime,
        )
    )


def test_runtime_topology_batch_matches_single_and_uses_one_observation(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _seed_topology("single", "Single", ["one"])
    _seed_topology("multi", "Multi", ["one", "two"])
    _seed_topology("unavailable", "Unavailable", ["one", "two"])
    runtime = _BatchRuntime(
        {
            "Multi": {
                "ok": True,
                "logical_runtime_target": "Multi",
                "effective_member_runtime_identity": "Multi :: two",
                "evidence_source": "runtime_native",
                "members": [],
            },
            "Unavailable": {
                "ok": False,
                "logical_runtime_target": "Unavailable",
                "effective_member_runtime_identity": None,
                "evidence_source": "runtime_native",
                "members": [],
            },
        }
    )
    _register_runtime(monkeypatch, runtime)
    ids = ["single", "multi", "unavailable", "multi"]

    batched = logical_topology.get_runtime_logical_topologies(ids)
    assert runtime.batch_calls == [["Multi", "Unavailable"]]
    assert runtime.single_calls == []
    for server_id in ("single", "multi", "unavailable"):
        assert batched[server_id] == logical_topology.get_runtime_logical_topology(server_id)


def test_runtime_topology_batch_falls_back_when_batch_operation_unsupported(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _seed_topology("multi", "Multi", ["one", "two"])

    class Unsupported(_BatchRuntime):
        def get_logical_groups_state(self, targets: list[str]) -> list[dict]:
            self.batch_calls.append(list(targets))
            raise NotImplementedError

    runtime = Unsupported(
        {
            "Multi": {
                "ok": True,
                "logical_runtime_target": "Multi",
                "effective_member_runtime_identity": "Multi :: one",
                "evidence_source": "runtime_native",
                "members": [],
            }
        }
    )
    _register_runtime(monkeypatch, runtime)
    result = logical_topology.get_runtime_logical_topologies(["multi"])
    assert result["multi"]["active_member_id"] == "one"
    assert runtime.batch_calls == [["Multi"]]
    assert runtime.single_calls == ["Multi"]


def test_mihomo_fingerprint_ignores_timestamps_but_tracks_semantics(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    with db_session() as connection:
        connection.execute(
            "INSERT INTO servers (server_id, server_name, provider_name, raw_json, inventory_state) "
            "VALUES ('a', 'A', 'pytest', '{\"mode\":\"vpn\"}', 'active')"
        )
    first = fingerprint_service.current_mihomo_input_fingerprint()
    with db_session() as connection:
        connection.execute("UPDATE servers SET updated_at = '2000-01-01 00:00:00' WHERE server_id = 'a'")
    second = fingerprint_service.current_mihomo_input_fingerprint()
    assert first["hash"] == second["hash"]
    with db_session() as connection:
        connection.execute("UPDATE servers SET raw_json = '{\"mode\":\"direct\"}' WHERE server_id = 'a'")
    third = fingerprint_service.current_mihomo_input_fingerprint()
    assert third["hash"] != first["hash"]


def test_prepared_mihomo_candidate_requires_matching_hashes_and_validates(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    candidate_path = get_settings().paths.generated_dir / "mihomo" / "config.next.yaml"
    candidate_path.parent.mkdir(parents=True, exist_ok=True)
    candidate_path.write_text("candidate", encoding="utf-8")
    fingerprint = {"hash": "input-a", "version": 2}
    validation_calls: list[int] = []
    generation_calls: list[int] = []
    monkeypatch.setattr(mihomo_reconcile_service, "current_mihomo_input_fingerprint", lambda _routing: fingerprint)
    monkeypatch.setattr(mihomo_reconcile_service, "mihomo_input_unchanged", lambda _fingerprint: False)
    monkeypatch.setattr(
        mihomo_reconcile_service.config,
        "validate_mihomo_candidate_config",
        lambda _routing=None: validation_calls.append(1) or {"ok": False},
    )
    monkeypatch.setattr(
        mihomo_reconcile_service.config,
        "write_mihomo_candidate_config",
        lambda _routing=None: generation_calls.append(len(generation_calls) + 1)
        or {"candidate_path": str(candidate_path)},
    )
    result = mihomo_reconcile_service.reconcile_mihomo_runtime(
        prepared_candidate_metadata={
            "input_fingerprint_hash": "input-a",
            "candidate_file_hash": fingerprint_service._file_hash(candidate_path),
        }
    )
    assert result["ok"] is False
    assert validation_calls == [1]
    assert generation_calls == []

    candidate_path.write_text("changed", encoding="utf-8")
    mihomo_reconcile_service.reconcile_mihomo_runtime(
        prepared_candidate_metadata={
            "input_fingerprint_hash": "input-a",
            "candidate_file_hash": "stale",
        }
    )
    assert generation_calls == [1]
    candidate_hash = fingerprint_service._file_hash(candidate_path)
    mihomo_reconcile_service.reconcile_mihomo_runtime(
        prepared_candidate_metadata={
            "input_fingerprint_hash": "input-b",
            "candidate_file_hash": candidate_hash,
        }
    )
    assert generation_calls == [1, 2]


def test_xray_batch_binding_insert_update_and_noop(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    with db_session() as connection:
        connection.execute(
            "INSERT INTO servers (server_id, server_name, provider_name, raw_json, inventory_state) "
            "VALUES ('server-a', 'A', 'pytest', '{}', 'active')"
        )
        connection.execute(
            "INSERT INTO servers (server_id, server_name, provider_name, raw_json, inventory_state) "
            "VALUES ('server-b', 'B', 'pytest', '{}', 'active')"
        )
        connection.execute(
            """INSERT INTO subjects (
                subject_id, subject_type, subject_role, implementation_kind, stable_key,
                display_name, alias, desired_mode, runtime_state, is_active, is_deleted, metadata_json
            ) VALUES ('xray:client-a', 'explicit_external_client', 'vless_client', 'xray',
                'xray:client-a', 'Client', NULL, 'enabled', 'inactive', 1, 0, ?)""",
            (json.dumps({"detail": {"client_uuid": "client-a"}}),),
        )
    node = {"client_uuid": "client-a", "client_email": "a@example.test", "xray_alias": "Alias A", "server_id": "server-a"}
    inserted = xray_subscription_service._batch_materialize_xray_subject_bindings([node], requested_by="pytest")
    assert inserted["inserted_overrides"] == 1
    with db_session() as connection:
        first = connection.execute(
            "SELECT s.alias, o.selected_server_id, o.updated_at "
            "FROM subjects AS s JOIN subject_server_overrides AS o USING (subject_id)"
        ).fetchone()
    updated = xray_subscription_service._batch_materialize_xray_subject_bindings(
        [{**node, "xray_alias": "Alias B", "server_id": "server-b"}], requested_by="pytest"
    )
    assert updated["updated_aliases"] == 1
    assert updated["updated_overrides"] == 1
    with db_session() as connection:
        override_before_noop = connection.execute(
            "SELECT updated_at FROM subject_server_overrides WHERE subject_id = 'xray:client-a'"
        ).fetchone()[0]
        connection.execute("UPDATE subjects SET updated_at = '2000-01-01 00:00:00'")
    noop = xray_subscription_service._batch_materialize_xray_subject_bindings(
        [{**node, "xray_alias": "Alias B", "server_id": "server-b"}], requested_by="pytest"
    )
    assert noop["updated_aliases"] == 0
    assert noop["updated_overrides"] == 0
    with db_session() as connection:
        assert connection.execute("SELECT updated_at FROM subjects").fetchone()[0] == "2000-01-01 00:00:00"
        assert connection.execute(
            "SELECT updated_at FROM subject_server_overrides WHERE subject_id = 'xray:client-a'"
        ).fetchone()[0] == override_before_noop
        assert first["selected_server_id"] == "server-a"


def test_xray_batch_binding_preserves_missing_subject_failure(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    result = xray_subscription_service._batch_materialize_xray_subject_bindings(
        [{"client_uuid": "missing", "client_email": "missing@example.test", "xray_alias": "Missing", "server_id": "none"}],
        requested_by="pytest",
    )
    assert result["ok"] is False
    assert result["error_code"] == "XRAY_SUB_PROFILE_SUBJECT_MISSING"


def test_subscription_timings_are_stage_specific_and_technical_log_is_compact(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    technical: list[dict] = []
    monkeypatch.setattr(subscription_pipeline, "reconcile_mihomo_runtime", lambda: {
        "ok": True,
        "reconcile_action": "none",
        "reconcile_reason": "unchanged_config",
        "promoted": {"promoted": False},
        "container": {"action": "none"},
    })
    monkeypatch.setattr(subscription_pipeline, "get_routing_global_state", lambda: {"server_mode": "fixed"})
    monkeypatch.setattr(subscription_pipeline, "get_vpn_auto_state", lambda: {"server_mode": "fixed"})
    monkeypatch.setattr(
        subscription_pipeline,
        "_reconcile_xray_subscription_profiles_after_refresh",
        lambda **_kwargs: {"ok": True, "status": "skipped", "nodes_count": 0},
    )
    monkeypatch.setattr(subscription_pipeline, "write_operational_log", lambda **_kwargs: None)
    monkeypatch.setattr(
        subscription_pipeline,
        "write_technical_log",
        lambda **kwargs: technical.append(kwargs),
    )
    result = subscription_pipeline.apply_prepared_subscription_refresh(
        {
            "ok": True,
            "stage": "candidate_validated",
            "refresh": {"large": "payload"},
            "candidate": {"large": "payload"},
            "config_validation": {"ok": True},
            "timings_ms": {"prepare_total": 10.0},
        }
    )
    assert result["timings_ms"]["prepare_total"] == 10.0
    assert "apply_total" in result["timings_ms"]
    assert "total" not in result["timings_ms"]
    assert technical
    details = technical[-1]["details"]
    assert "refresh" not in details
    assert "candidate" not in details
    assert "timings_ms" in details
