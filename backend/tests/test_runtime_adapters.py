from pathlib import Path

from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import initialize_database
from fwrouter_api.db.connection import db_session
from fwrouter_api.services.external_connections_registry import upsert_external_connection_record
from fwrouter_api.services.live_probe_cache import clear_live_probe_cache
from fwrouter_api.services.runtime_adapters import (
    RUNTIME_CAPABILITY_HEALTH,
    RUNTIME_CAPABILITY_LOGICAL_GROUP_PROBE,
    RUNTIME_CAPABILITY_LOGICAL_GROUP_PROBE_MANY,
    RUNTIME_CAPABILITY_LOGICAL_GROUP_STATE,
    RUNTIME_CAPABILITY_LOGICAL_GROUP_STATE_MANY,
    RUNTIME_CAPABILITY_LOGICAL_MEMBER_PROBE,
    RUNTIME_ROLE_VPN_DATAPLANE,
    RuntimeAdapterRegistration,
    active_runtime_adapter,
    active_explicit_client_runtime_adapter,
    active_vpn_dataplane_adapter,
    register_runtime_adapter,
    runtime_adapter_operations,
    runtime_role_for_replacement_target,
)
from fwrouter_api.services.ui_display_settings import ExternalConnectionValidationError
from fwrouter_api.services.vpn_runtime_control import MihomoVpnRuntimeController, VpnRuntimeController


def _configure_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FWROUTER_STATE_DIR", str(tmp_path / "state"))
    get_settings.cache_clear()
    clear_live_probe_cache()


def test_runtime_adapter_prefers_ready_external_vpn_dataplane(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    monkeypatch.setattr(
        "fwrouter_api.services.external_vpn._external_vpn_runtime_ready",
        lambda module: True,
    )
    upsert_external_connection_record(
        {
            "connection_id": "connection-a",
            "system_id": "connection-a",
            "label": "Connection A",
            "connection_type": "external_vpn_module",
            "runtime_type": "provider-a",
            "replacement_target": "mihomo",
            "location": "host",
            "endpoints": {
                "tcp_redir_port": "16080",
                "udp_tproxy_port": "16081",
            },
        }
    )

    adapter = active_vpn_dataplane_adapter()

    assert adapter["role"] == "vpn_dataplane"
    assert adapter["adapter_id"] == "external_vpn_module"
    assert adapter["lifecycle_mode"] == "external"
    assert adapter["ready"] is True
    assert adapter["source"]["connection_id"] == "connection-a"
    assert adapter["contour"]["tproxy_port"] == 16081


def test_managed_vpn_runtime_declares_logical_health_operations(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    adapter = active_vpn_dataplane_adapter()
    operations = runtime_adapter_operations(adapter)

    assert {
        RUNTIME_CAPABILITY_LOGICAL_GROUP_STATE,
        RUNTIME_CAPABILITY_LOGICAL_GROUP_STATE_MANY,
        RUNTIME_CAPABILITY_LOGICAL_GROUP_PROBE,
        RUNTIME_CAPABILITY_LOGICAL_GROUP_PROBE_MANY,
        RUNTIME_CAPABILITY_LOGICAL_MEMBER_PROBE,
    }.issubset(set(adapter["capabilities"]))
    assert callable(operations.get_logical_group_state)
    assert callable(operations.get_logical_groups_state)
    assert callable(operations.probe_logical_group)
    assert callable(operations.probe_logical_groups)
    assert callable(operations.probe_logical_member)
    assert callable(operations.get_active_member_state)
    assert callable(operations.get_member_health)
    assert callable(operations.get_member_latency)
    assert callable(operations.request_member_reselection)
    assert callable(operations.request_group_health_refresh)


def test_runtime_health_refresh_is_scoped_to_vpn_auto_servers(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    with db_session() as connection:
        for server_id in ("auto-server", "non-auto-server"):
            connection.execute(
                "INSERT INTO servers (server_id, server_name, provider_name, inventory_state) VALUES (?, ?, 'pytest', 'active')",
                (server_id, server_id),
            )
            connection.execute(
                "INSERT INTO server_preferences (server_id, vpn_auto, global_list) VALUES (?, ?, 1)",
                (server_id, 1 if server_id == "auto-server" else 0),
            )
            connection.execute(
                "INSERT INTO logical_server_topology (logical_server_id, topology_kind, selection_policy) VALUES (?, 'concrete_single', 'single')",
                (server_id,),
            )
            connection.execute(
                "INSERT INTO logical_server_members (logical_server_id, member_id, member_runtime_name, member_config_json, transport_fingerprint, member_order, is_active) VALUES (?, ?, ?, '{}', 'pytest', 0, 1)",
                (server_id, f"{server_id}:member", server_id),
            )
    calls: list[str] = []

    class Operations:
        def request_group_health_refresh(self, target, **kwargs):
            calls.append(target)
            return {"ok": False, "logical_runtime_target": target, "members": [], "observed_at": "2026-07-01T00:00:00+00:00"}

    controller = VpnRuntimeController(
        vpn_adapter={"adapter_id": "test-runtime", "role": "vpn_dataplane", "ready": True},
    )
    monkeypatch.setattr("fwrouter_api.services.vpn_runtime_control.runtime_adapter_operations", lambda adapter: Operations())

    result = controller.full_health_refresh(timeout_ms=1000)

    assert result["logical_servers"] == 1
    assert calls == ["auto-server"]


def test_runtime_controller_does_not_report_unconfirmed_apply_as_failover(monkeypatch) -> None:
    controller = MihomoVpnRuntimeController(vpn_adapter={"adapter_id": "mihomo", "ready": True})
    state = {"active_target_id": "server-old", "failover_supported": True}
    controller.get_state = lambda: state
    monkeypatch.setattr(
        "fwrouter_api.services.vpn_runtime_control.select_vpn_auto_server",
        lambda **kwargs: {
            "ok": False,
            "applied": True,
            "selection_outcome": "unconfirmed",
            "active_before": "server-old",
            "selected_server_id": "server-new",
            "active_after": None,
        },
    )

    result = controller.failover(
        apply=True, reason="degraded", update_ping_state=False,
        candidate_limit=5, timeout_ms=1000,
    )

    assert result["ok"] is False
    assert result["applied"] is False
    assert result["action"] == "none"
    assert result["selection_outcome"] == "unconfirmed"

    initial = controller.initial_select(
        apply=True, reason="startup", update_ping_state=False,
        candidate_limit=5, timeout_ms=1000,
    )
    assert initial["ok"] is False
    assert initial["applied"] is False
    assert initial["action"] == "none"
    assert initial["selection_outcome"] == "unconfirmed"

    monkeypatch.setattr(
        "fwrouter_api.services.vpn_runtime_control.select_vpn_auto_server",
        lambda **kwargs: {
            "ok": True,
            "applied": False,
            "selection_outcome": "selected",
            "active_before": "server-old",
            "selected_server_id": "server-new",
            "active_after": "server-new",
        },
    )
    contradictory = controller.failover(
        apply=True, reason="degraded", update_ping_state=False,
        candidate_limit=5, timeout_ms=1000,
    )
    assert contradictory["ok"] is False
    assert contradictory["applied"] is False
    assert contradictory["action"] == "none"


def test_mihomo_controller_uses_structural_target_validity_not_health(monkeypatch) -> None:
    controller = MihomoVpnRuntimeController(vpn_adapter={"adapter_id": "mihomo", "ready": True})
    monkeypatch.setattr(
        "fwrouter_api.services.vpn_runtime_control.get_vpn_auto_state",
        lambda: {
            "server_mode": "auto",
            "active_auto_server_id": "srv-current",
            "active_auto_target_valid": True,
            "active_auto_server_valid": False,
            "mihomo_runtime_state": "running",
        },
    )

    state = controller.get_state()

    assert state["active_target_id"] == "srv-current"
    assert state["active_target_valid"] is True
    assert state["selector_state"]["active_auto_server_valid"] is False


def test_runtime_controller_preserves_confirmed_selector_noop(monkeypatch) -> None:
    controller = MihomoVpnRuntimeController(vpn_adapter={"adapter_id": "mihomo", "ready": True})
    state = {"active_target_id": "server-current", "failover_supported": True}
    controller.get_state = lambda: state
    monkeypatch.setattr(
        "fwrouter_api.services.vpn_runtime_control.select_vpn_auto_server",
        lambda **kwargs: {
            "ok": True,
            "applied": False,
            "selection_outcome": "noop",
            "noop": True,
            "active_before": "server-current",
            "selected_server_id": "server-current",
            "active_after": "server-current",
        },
    )

    result = controller.failover(
        apply=True, reason="degraded", update_ping_state=False,
        candidate_limit=5, timeout_ms=1000,
    )

    assert result["ok"] is True
    assert result["applied"] is False
    assert result["action"] == "noop"
    assert result["selection_outcome"] == "noop"


def test_runtime_registry_resolves_active_adapter_by_role(monkeypatch) -> None:
    from fwrouter_api.services import runtime_adapters

    fake_operations = object()
    monkeypatch.setattr(runtime_adapters, "_RUNTIME_ADAPTER_REGISTRY", [])
    register_runtime_adapter(
        RuntimeAdapterRegistration(
            role=RUNTIME_ROLE_VPN_DATAPLANE,
            adapter_id="low",
            capabilities=frozenset(),
            priority=0,
            replacement_targets=frozenset({"low-target"}),
            resolver=lambda: {
                "role": RUNTIME_ROLE_VPN_DATAPLANE,
                "adapter_id": "low",
                "lifecycle_mode": "external",
                "ready": True,
                "source": {"kind": "fake"},
            },
        )
    )
    register_runtime_adapter(
        RuntimeAdapterRegistration(
            role=RUNTIME_ROLE_VPN_DATAPLANE,
            adapter_id="high",
            capabilities=frozenset({RUNTIME_CAPABILITY_HEALTH}),
            priority=100,
            replacement_targets=frozenset({"high-target"}),
            resolver=lambda: {
                "role": RUNTIME_ROLE_VPN_DATAPLANE,
                "adapter_id": "high",
                "lifecycle_mode": "external",
                "ready": True,
                "source": {"kind": "fake"},
            },
            operations_factory=lambda adapter: fake_operations,
        )
    )

    adapter = active_runtime_adapter(RUNTIME_ROLE_VPN_DATAPLANE)

    assert adapter["adapter_id"] == "high"
    assert adapter["capabilities"] == [RUNTIME_CAPABILITY_HEALTH]
    assert runtime_adapter_operations(adapter) is fake_operations
    assert runtime_role_for_replacement_target("high-target") == RUNTIME_ROLE_VPN_DATAPLANE


def test_runtime_registry_allows_role_implementation_switch(monkeypatch) -> None:
    from fwrouter_api.services import runtime_adapters

    monkeypatch.setattr(runtime_adapters, "_RUNTIME_ADAPTER_REGISTRY", [])
    register_runtime_adapter(
        RuntimeAdapterRegistration(
            role=RUNTIME_ROLE_VPN_DATAPLANE,
            adapter_id="runtime-a",
            capabilities=frozenset(),
            priority=0,
            replacement_targets=frozenset({"vpn"}),
            resolver=lambda: {
                "role": RUNTIME_ROLE_VPN_DATAPLANE,
                "adapter_id": "runtime-a",
                "lifecycle_mode": "external",
                "ready": True,
                "source": {"kind": "a"},
            },
        )
    )
    assert active_runtime_adapter(RUNTIME_ROLE_VPN_DATAPLANE)["adapter_id"] == "runtime-a"

    register_runtime_adapter(
        RuntimeAdapterRegistration(
            role=RUNTIME_ROLE_VPN_DATAPLANE,
            adapter_id="runtime-b",
            capabilities=frozenset(),
            priority=50,
            replacement_targets=frozenset({"vpn"}),
            resolver=lambda: {
                "role": RUNTIME_ROLE_VPN_DATAPLANE,
                "adapter_id": "runtime-b",
                "lifecycle_mode": "external",
                "ready": True,
                "source": {"kind": "b"},
            },
        )
    )

    assert active_runtime_adapter(RUNTIME_ROLE_VPN_DATAPLANE)["adapter_id"] == "runtime-b"


def test_runtime_adapter_exposes_external_explicit_client_replacement(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    upsert_external_connection_record(
        {
            "connection_id": "connection-a",
            "system_id": "connection-a",
            "label": "Connection A",
            "connection_type": "external_vpn_module",
            "runtime_type": "provider-a",
            "replacement_target": "xray",
            "location": "ip",
            "address": "127.0.0.1:18080",
            "endpoints": {
                "controller_url": "http://127.0.0.1:18080/api",
            },
        }
    )

    adapter = active_explicit_client_runtime_adapter()

    assert adapter["role"] == "explicit_client_runtime"
    assert adapter["adapter_id"] == "external_explicit_client_runtime"
    assert adapter["lifecycle_mode"] == "external"
    assert adapter["ready"] is True
    assert adapter["source"]["connection_id"] == "connection-a"
    assert adapter["source"]["runtime_type"] == "provider-a"


def test_external_vpn_module_rejects_duplicate_active_target(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    upsert_external_connection_record(
        {
            "connection_id": "connection-a",
            "system_id": "connection-a",
            "label": "External VPN A",
            "connection_type": "external_vpn_module",
            "runtime_type": "generic",
            "replacement_target": "mihomo",
            "endpoints": {
                "tcp_redir_port": "16080",
                "udp_tproxy_port": "16081",
            },
        }
    )

    try:
        upsert_external_connection_record(
            {
                "connection_id": "connection-b",
                "system_id": "connection-b",
                "label": "External VPN B",
                "connection_type": "external_vpn_module",
                "runtime_type": "generic",
                "replacement_target": "mihomo",
                "endpoints": {
                    "tcp_redir_port": "17080",
                    "udp_tproxy_port": "17081",
                },
            }
        )
    except ExternalConnectionValidationError as exc:
        assert exc.code == "EXTERNAL_VPN_MODULE_TARGET_CONFLICT"
    else:  # pragma: no cover - defensive
        raise AssertionError("duplicate external VPN module target must be rejected")


def test_external_vpn_modules_allow_disabled_duplicate_and_different_targets(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    first = upsert_external_connection_record(
        {
            "connection_id": "connection-a",
            "system_id": "shared-vpn",
            "label": "Connection A",
            "connection_type": "external_vpn_module",
            "runtime_type": "provider-a",
            "replacement_target": "mihomo",
            "endpoints": {
                "tcp_redir_port": "16080",
                "udp_tproxy_port": "16081",
            },
        }
    )
    disabled_duplicate = upsert_external_connection_record(
        {
            "connection_id": "connection-b",
            "system_id": "shared-vpn",
            "label": "Connection B",
            "connection_type": "external_vpn_module",
            "runtime_type": "provider-a",
            "replacement_target": "mihomo",
            "enabled": False,
            "endpoints": {
                "tcp_redir_port": "17080",
                "udp_tproxy_port": "17081",
            },
        }
    )
    different_target = upsert_external_connection_record(
        {
            "connection_id": "connection-c",
            "system_id": "shared-vpn",
            "label": "Connection C",
            "connection_type": "external_vpn_module",
            "runtime_type": "provider-a",
            "replacement_target": "xray",
            "endpoints": {
                "controller_url": "http://127.0.0.1:18080/api",
            },
        }
    )

    assert first["connection_id"] == "connection-a"
    assert disabled_duplicate["connection_id"] == "connection-b"
    assert disabled_duplicate["enabled"] is False
    assert different_target["connection_id"] == "connection-c"
