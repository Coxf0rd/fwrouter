from pathlib import Path

from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import initialize_database
from fwrouter_api.services.external_connections_registry import upsert_external_connection_record
from fwrouter_api.services.live_probe_cache import clear_live_probe_cache
from fwrouter_api.services.runtime_adapters import (
    active_explicit_client_runtime_adapter,
    active_vpn_dataplane_adapter,
)
from fwrouter_api.services.ui_display_settings import ExternalConnectionValidationError


def _configure_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FWROUTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("FWROUTER_DATABASE_URL", f"sqlite:///{tmp_path}/fwrouter.db")
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
