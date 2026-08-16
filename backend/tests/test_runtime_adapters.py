import json
from pathlib import Path

from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import db_session, initialize_database
from fwrouter_api.services.live_probe_cache import clear_live_probe_cache
from fwrouter_api.services.runtime_adapters import (
    active_explicit_client_runtime_adapter,
    active_vpn_dataplane_adapter,
)


def _configure_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FWROUTER_STATE_DIR", str(tmp_path / "state"))
    get_settings.cache_clear()
    clear_live_probe_cache()


def _save_display_settings(value: dict) -> None:
    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO settings (key, value_json, updated_at)
            VALUES ('ui.admin_client_display.v1', ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET
                value_json = excluded.value_json,
                updated_at = CURRENT_TIMESTAMP
            """,
            (json.dumps(value),),
        )


def test_runtime_adapter_prefers_ready_external_vpn_dataplane(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    monkeypatch.setattr(
        "fwrouter_api.services.external_vpn._external_vpn_runtime_ready",
        lambda module: True,
    )
    _save_display_settings(
        {
            "system_visibility": {"external_vpn_sing_box": True},
            "custom_external_systems": [
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
            ],
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
    _save_display_settings(
        {
            "system_visibility": {"explicit_core": True},
            "custom_external_systems": [
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
            ],
        }
    )

    adapter = active_explicit_client_runtime_adapter()

    assert adapter["role"] == "explicit_client_runtime"
    assert adapter["adapter_id"] == "external_explicit_client_runtime"
    assert adapter["lifecycle_mode"] == "external"
    assert adapter["ready"] is True
    assert adapter["source"]["system_id"] == "explicit_core"
    assert adapter["source"]["runtime_type"] == "generic-vless-core"
