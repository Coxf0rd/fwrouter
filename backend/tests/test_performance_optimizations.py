from __future__ import annotations
from _test_support import configure_test_state_dir as _configure_env

import json
from importlib import import_module
from pathlib import Path

from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import db_session, initialize_database
from fwrouter_api.services import logical_topology

# Preserve the historical import order that initializes the Mihomo facade before
# importing the reconciler which imports that facade back.
import_module("fwrouter_api.services.mihomo_config")
from fwrouter_api.services import mihomo_reconcile as mihomo_reconcile_service
from fwrouter_api.services import mihomo_reconcile_fingerprint as fingerprint_service
from fwrouter_api.services import subscription_pipeline
from fwrouter_api.services import xray_subscription_service




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
        self.inventory_calls = 0

    def list_servers(self) -> list[dict[str, str]]:
        self.inventory_calls += 1
        return [{"server_name": "Single"}]

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
        RUNTIME_CAPABILITY_LIST_SERVERS,
        RUNTIME_ROLE_VPN_DATAPLANE,
        RuntimeAdapterRegistration,
        register_runtime_adapter,
    )

    monkeypatch.setattr(runtime_adapters, "_RUNTIME_ADAPTER_REGISTRY", [])
    capabilities = {
        RUNTIME_CAPABILITY_HEALTH,
        RUNTIME_CAPABILITY_LOGICAL_GROUP_STATE,
        RUNTIME_CAPABILITY_LIST_SERVERS,
    }
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
    runtime = _BatchRuntime(
        {
            "Multi": {
                "ok": True,
                "logical_runtime_target": "Multi",
                "effective_member_runtime_identity": "Multi :: two",
                "evidence_source": "runtime_native",
                "members": [],
            },
            "Batch available": {
                "ok": True,
                "logical_runtime_target": "Batch available",
                "effective_member_runtime_identity": "Batch available :: one",
                "evidence_source": "runtime_native",
                "members": [],
            },
        }
    )
    _register_runtime(monkeypatch, runtime)
    _seed_topology("batch-available", "Batch available", ["one", "two"])
    ids = ["single", "multi", "batch-available", "multi"]

    batched = logical_topology.get_runtime_logical_topologies(ids)
    assert len(runtime.batch_calls) == 1, f"expected one batch read, got {runtime.batch_calls!r}"
    assert len(runtime.batch_calls[0]) == 2, f"expected two unique batch targets, got {runtime.batch_calls[0]!r}"
    assert set(runtime.batch_calls[0]) == {"Multi", "Batch available"}, (
        f"unexpected batch targets: {runtime.batch_calls[0]!r}"
    )
    assert runtime.inventory_calls == 1, f"expected one inventory observation, got {runtime.inventory_calls}"
    assert runtime.single_calls == [], f"unexpected single-read fallback: {runtime.single_calls!r}"
    for server_id in ("single", "multi", "batch-available"):
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


def test_runtime_topology_batch_failure_uses_explicit_single_read_fallback(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _seed_topology("multi", "Multi", ["one", "two"])

    class FailedBatch(_BatchRuntime):
        def get_logical_groups_state(self, targets: list[str]) -> list[dict]:
            self.batch_calls.append(list(targets))
            raise RuntimeError("batch transport unavailable")

    runtime = FailedBatch({
        "Multi": {
            "ok": True,
            "logical_runtime_target": "Multi",
            "effective_member_runtime_identity": "Multi :: two",
            "evidence_source": "runtime_native",
            "members": [],
        }
    })
    _register_runtime(monkeypatch, runtime)

    result = logical_topology.get_runtime_logical_topologies(["multi"])

    assert runtime.batch_calls == [["Multi"]]
    assert runtime.single_calls == ["Multi"]
    assert result["multi"]["active_member_id"] == "two"


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
    monkeypatch.setattr(mihomo_reconcile_service, "_mihomo_incarnation", lambda: "pytest-mihomo-incarnation")
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
    monkeypatch.setattr(subscription_pipeline, "reconcile_mihomo_runtime", lambda *, verification_callback=None, _inventory_admission=None: {
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


# Fixed module-level names present on the baseline application facades before cleanup.
PUBLIC_FACADE_BINDINGS = {
    'fwrouter_api.adapters.xray': ('XRAY_API_PORT', 'XRAY_API_TAG', 'XRAY_COMPOSE_PATH', 'XRAY_CONTAINER_NAME', 'XRAY_FALLBACK_OUTBOUND_TAG', 'XRAY_INBOUND_TAG', 'XRAY_LOG_ROOT', 'XRAY_MANAGED_DNS_OUTBOUND_TAG', 'XRAY_TRANSPORT'),
    'fwrouter_api.db.schema_state': ('Iterable',),
    'fwrouter_api.routes.xray': ('Field', 'delete_xray_client', 'delete_xray_subscription_profile'),
    'fwrouter_api.services.apply': ('DataplaneResult', 'db_session', 'get_dataplane_capability', 'prime_runtime_read_models_async', 'subprocess', 'update_job_running_result'),
    'fwrouter_api.services.dataplane_nft': ('CONTROL_PLANE_INPUT_PORTS', 'OWNED_TABLE', 'REQUIRED_CHAINS', 'ROOT_UID', 'STATIC_SECURE_DNS_BYPASS_IPV4', 'get_dataplane_artifact_paths', 'promote_last_good', 'utc_timestamp'),
    'fwrouter_api.services.logs': ('log_event',),
    'fwrouter_api.services.logs_retention': ('atomic_write_text',),
    'fwrouter_api.services.mihomo_config': ('APPLIED_MANIFEST_PATH', 'BASE_CONFIG_PATH', 'DEFAULT_FULL_VPN_TCP_REDIR_PORT', 'DEFAULT_FULL_VPN_UDP_TPROXY_PORT', 'DEFAULT_TRANSPARENT_TCP_REDIR_PORT', 'DEFAULT_TRANSPARENT_UDP_TPROXY_PORT', 'FULL_VPN_REDIR_LISTENER_NAME', 'FULL_VPN_TPROXY_LISTENER_NAME', 'LEGACY_INBOUND_KEYS', 'MIHOMO_CANDIDATE_CONFIG_PATH', 'SUBJECT_SELECTOR_PREFIX', 'TRANSPARENT_BIND_ADDRESS', 'TRANSPARENT_REDIR_LISTENER_NAME', 'TRANSPARENT_TPROXY_PROXY_NAME', 'XRAY_MIHOMO_LISTENER_PREFIX', 'get_mihomo_config_status', 'managed_runtime_operation_blocked', 'mihomo_runtime_satisfies_routing', 'promote_mihomo_candidate_config', 'reconcile_mihomo_runtime', 'reconcile_mihomo_selective_default_fast', 'resolve_runtime_proxy_rows', 'restart_mihomo_container', 'subject_selector_name', 'validate_and_promote_mihomo_candidate_config', 'yaml'),
    'fwrouter_api.services.mihomo_config_validation': ('EXPLICIT_MIXED_LISTENER_BIND', 'EXPLICIT_MIXED_LISTENER_PORT'),
    'fwrouter_api.services.mihomo_reconcile': ('shutil',),
    'fwrouter_api.services.mihomo_reconcile_fingerprint': ('os',),
    'fwrouter_api.services.provider_managed': ('configured_provider_binding',),
    'fwrouter_api.services.routing_manifest': ('build_dataplane_profile',),
    'fwrouter_api.services.rules': ('ApplyMode', 'BIG_VPN_BROAD_AGGREGATE_PATHS', 'DEFAULT_RULES_SOURCE_ADAPTER', 'DOMAIN_LABEL_RE', 'DOMAIN_RE', 'JOB_TYPE_RULES_FULL_UPDATE', 'JobLockConflictError', 'LOCK_RULES_APPLY', 'PROTECTED_LOCAL_NETWORKS', 'PROTECTED_SERVICE_DOMAINS', 'RULESET_BIG_DIRECT', 'RULESET_BIG_VPN', 'RULESET_EFFECTIVE', 'RULESET_MANUAL', 'RULESET_ORDER', 'RULESET_STATIC_DIRECT', 'RULES_PIPELINE_VERSION', 'RULE_ACTIONS', 'RULE_ACTION_ALIASES', 'RulesSourceFetchError', 'atomic_write_json', 'atomic_write_text', 'build_effective_rules_artifact', 'db_session', 'get_default_job_manager', 'get_settings', 'json', 'reconcile_mihomo_runtime', 'render_effective_rules_text', 'run_apply_pipeline', 'validate_manual_rules', 'validate_value_list', 'write_job_json_artifact', 'write_job_text_artifact', 'write_operational_log'),
    'fwrouter_api.services.server_global_selection': ('explicit_external_client_allows_virtual_vpn_auto',),
    'fwrouter_api.services.server_inventory': ('explicit_external_client_allows_virtual_vpn_auto',),
    'fwrouter_api.services.server_preferences': ('explicit_external_client_allows_virtual_vpn_auto', 'json'),
    'fwrouter_api.services.server_state': ('explicit_external_client_allows_virtual_vpn_auto', 'json'),
    'fwrouter_api.services.server_subject_overrides': ('json',),
    'fwrouter_api.services.servers': ('apply_global_auto_server', 'apply_global_fixed_server', 'clear_global_fixed_server', 'clear_subject_server_override', 'ensure_routing_global_state', 'expire_global_fixed_server', 'get_routing_global_state', 'get_server', 'get_subject_server_override', 'list_servers', 'reconcile_current_routing_if_drift', 'set_global_fixed_server', 'set_global_mode', 'set_selective_default', 'set_subject_server_override', 'submit_global_fixed_server_apply_job', 'sync_servers_from_mihomo', 'update_subject_server_override_apply_status'),
    'fwrouter_api.services.subscription_profiles': ('quote',),
    'fwrouter_api.services.system_subjects': ('list_subjects_with_effective_state',),
    'fwrouter_api.services.ui_display_settings': ('DEFAULT_EXTERNAL_COLLECTOR_INTERVAL_SECONDS', 'EXTERNAL_CAPABILITY_KEYS', 'EXTERNAL_COLLECTOR_BASE_KEYS', 'EXTERNAL_COLLECTOR_MODE_KEYS', 'EXTERNAL_CONNECTION_TYPES', 'EXTERNAL_ENDPOINT_KEYS', 'EXTERNAL_INTEGRATION_MODES', 'EXTERNAL_LOCATIONS', 'EXTERNAL_REFRESH_MODES', 'UI_DISPLAY_SETTINGS_KEY', 'UI_DISPLAY_SYSTEMS', 'UI_SYSTEM_VISIBILITY_DEFAULTS'),
    'fwrouter_api.services.ui_display_settings_display': ('json',),
    'fwrouter_api.services.ui_state': ('DEFAULT_TRAFFIC_PANEL_KEYS', 'INVENTORY_ROLE_ALIASES', 'INVENTORY_ROLE_BY_KIND', 'KINDS_BY_INVENTORY_ROLE', 'TRAFFIC_METRIC_KEYS', 'XRAY_INTERNAL_PREFIXES', 'XRAY_SUBSCRIPTION_ACTIVE_WINDOW_SECONDS', 'get_ui_display_settings', 'save_ui_display_settings'),
    'fwrouter_api.services.ui_state_summary': ('list_ui_settings_inventory',),
    'fwrouter_api.services.xray_materialize': ('collect_xray_runtime_bindings',),
    'fwrouter_api.services.xray_subscription_service': ('XRAY_PUBLIC_PATH', 'XRAY_PUBLIC_PORT'),
}

# Private names with tracked downstream imports/attribute lookups are explicit seams.
PRIVATE_FACADE_SEAMS = {
    'fwrouter_api.services.mihomo_config': ('_build_fallback_rule', '_build_transparent_fallback_rule', '_configs_equal', '_iso8601_mtime', '_resolved_base_config_path', '_resolved_last_good_mihomo_dir', '_summarize_candidate', '_summarize_config_status'),
    'fwrouter_api.services.rules': ('_configured_rules_sources', '_utc_now_iso', '_validate_big_vpn_source_policy'),
    'fwrouter_api.services.ui_display_settings': ('_display_systems', '_json_loads', '_normalize_system_visibility', '_system_visible'),
    'fwrouter_api.services.ui_state': ('_month_key', '_summarize_log_event'),
}


def test_existing_facade_bindings_remain_available() -> None:
    for modules in (PUBLIC_FACADE_BINDINGS, PRIVATE_FACADE_SEAMS):
        for module_name, names in modules.items():
            module = import_module(module_name)
            missing = [name for name in names if not hasattr(module, name)]
            assert not missing, f"{module_name} lost existing bindings: {missing}"
