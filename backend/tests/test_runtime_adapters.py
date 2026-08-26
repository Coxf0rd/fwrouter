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
            "system_id": "external_vpn_sing_box",
            "label": "Sing Box",
            "connection_type": "external_vpn_module",
            "runtime_type": "sing-box",
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
    assert adapter["source"]["system_id"] == "external_vpn_sing_box"
    assert adapter["contour"]["tproxy_port"] == 16081


def test_runtime_adapter_exposes_external_explicit_client_replacement(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    upsert_external_connection_record(
        {
            "system_id": "explicit_core",
            "label": "Explicit Core",
            "connection_type": "external_vpn_module",
            "runtime_type": "generic-vless-core",
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
    assert adapter["source"]["system_id"] == "explicit_core"
    assert adapter["source"]["runtime_type"] == "generic-vless-core"


def test_external_vpn_module_rejects_duplicate_active_target(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    upsert_external_connection_record(
        {
            "system_id": "external-vpn-a",
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
                "system_id": "external-vpn-b",
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
