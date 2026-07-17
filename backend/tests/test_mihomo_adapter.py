from __future__ import annotations

from pathlib import Path

import yaml

from fwrouter_api.adapters.mihomo import (
    DEFAULT_BASE_URL,
    MihomoHttpAdapter,
    MihomoRuntimeState,
)


def _write_yaml(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def test_mihomo_health_marks_loopback_bound_transparent_listener_degraded(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    contours_path = tmp_path / "contours.yaml"
    _write_yaml(
        config_path,
        {
            "secret": "secret",
            "bind-address": "127.0.0.1",
            "listeners": [
                {"name": "fwrouter-mixed", "type": "mixed", "listen": "127.0.0.1", "port": 5201},
                {"name": "fwrouter-redir", "type": "redir", "listen": "127.0.0.1", "port": 5202, "proxy": "vpn-global"},
                {"name": "fwrouter-tproxy", "type": "tproxy", "listen": "127.0.0.1", "port": 5203, "proxy": "vpn-global", "udp": True},
            ],
        },
    )
    _write_yaml(
        contours_path,
        {
            "transparent_vpn": {"ready": True, "isolated_from_explicit_proxy": True, "redir_port": 5202, "tproxy_port": 5203},
            "explicit_proxy": {"preserved": True},
        },
    )

    adapter = MihomoHttpAdapter(base_url=DEFAULT_BASE_URL, config_path=config_path, contours_path=contours_path)
    adapter._get_json = lambda path: (  # type: ignore[method-assign]
        {"version": "test"}
        if path == "/version"
        else {"proxies": {"vpn-auto": {"all": ["DIRECT"]}, "vpn-global": {"all": ["vpn-auto", "DIRECT"], "now": "vpn-auto"}}}
    )

    health = adapter.health()

    assert health.runtime_state == MihomoRuntimeState.DEGRADED
    assert "transparent TPROXY listener bind is invalid" in health.message
    contours = health.details["config"]["fwrouter_contours"]
    assert contours["transparent_vpn"]["listener_loopback_bound"] is True
    assert contours["transparent_vpn"]["listener_bind_valid"] is False
    assert contours["transparent_vpn"]["ready"] is False


def test_mihomo_health_uses_managed_split_listeners_as_canonical_source(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    contours_path = tmp_path / "contours.yaml"
    _write_yaml(
        config_path,
        {
            "secret": "secret",
            "redir-port": 6202,
            "tproxy-port": 6203,
            "listeners": [
                {"name": "fwrouter-mixed", "type": "mixed", "listen": "127.0.0.1", "port": 5201},
                {"name": "fwrouter-redir", "type": "redir", "listen": "0.0.0.0", "port": 5202, "proxy": "vpn-global"},
                {"name": "fwrouter-tproxy", "type": "tproxy", "listen": "0.0.0.0", "port": 5203, "proxy": "vpn-global", "udp": True},
            ],
        },
    )
    _write_yaml(contours_path, {"transparent_vpn": {"ready": True}})

    adapter = MihomoHttpAdapter(base_url=DEFAULT_BASE_URL, config_path=config_path, contours_path=contours_path)
    adapter.check_port = lambda port, host="127.0.0.1", timeout=1.0: True  # type: ignore[method-assign]
    adapter._get_json = lambda path: (  # type: ignore[method-assign]
        {"version": "test"}
        if path == "/version"
        else {
            "connections": [
                {"network": "tcp", "inbound": "fwrouter-redir", "proxy": "vpn-global"},
            ]
        }
        if path == "/connections"
        else {"proxies": {"vpn-auto": {"all": ["DIRECT"]}, "vpn-global": {"all": ["vpn-auto", "DIRECT"], "now": "vpn-auto"}}}
    )

    health = adapter.health()

    assert health.runtime_state == MihomoRuntimeState.RUNNING
    assert health.details["config"]["redir_port"] == 5202
    assert health.details["config"]["tproxy_port"] == 5203
    assert health.details["config"]["fwrouter_contours"]["transparent_vpn"]["transparent_tcp_ready"] is True
    assert health.details["config"]["fwrouter_contours"]["transparent_vpn"]["transparent_udp_ready"] is True
    assert health.details["transparent_runtime"]["transparent_tcp_session_materialized"] is True


def test_mihomo_health_accepts_rule_based_transparent_listeners(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    contours_path = tmp_path / "contours.yaml"
    _write_yaml(
        config_path,
        {
            "secret": "secret",
            "listeners": [
                {"name": "fwrouter-mixed", "type": "mixed", "listen": "127.0.0.1", "port": 5201, "proxy": "vpn-global"},
                {"name": "fwrouter-redir", "type": "redir", "listen": "0.0.0.0", "port": 5202, "rule": "fwrouter-transparent"},
                {"name": "fwrouter-tproxy", "type": "tproxy", "listen": "0.0.0.0", "port": 5203, "rule": "fwrouter-transparent", "udp": True},
            ],
        },
    )
    _write_yaml(contours_path, {"transparent_vpn": {"ready": True}})

    adapter = MihomoHttpAdapter(base_url=DEFAULT_BASE_URL, config_path=config_path, contours_path=contours_path)
    adapter.check_port = lambda port, host="127.0.0.1", timeout=1.0: True  # type: ignore[method-assign]
    adapter._get_json = lambda path: (  # type: ignore[method-assign]
        {"version": "test"}
        if path == "/version"
        else {"connections": []}
        if path == "/connections"
        else {"proxies": {"vpn-auto": {"all": ["DIRECT"]}, "vpn-global": {"all": ["vpn-auto", "DIRECT"], "now": "vpn-auto"}}}
    )

    health = adapter.health()

    transparent = health.details["config"]["fwrouter_contours"]["transparent_vpn"]
    assert health.runtime_state == MihomoRuntimeState.RUNNING
    assert transparent["listener_rule"] == "fwrouter-transparent"
    assert transparent["listener_proxy"] is None
    assert transparent["transparent_tcp_ready"] is True
    assert transparent["transparent_udp_ready"] is True
