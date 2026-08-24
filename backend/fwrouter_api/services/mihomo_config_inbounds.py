from __future__ import annotations

import ipaddress
from pathlib import Path
from typing import Any

import yaml

from fwrouter_api.services.mihomo_config_paths import (
    DEFAULT_FULL_VPN_TCP_REDIR_PORT,
    DEFAULT_FULL_VPN_UDP_TPROXY_PORT,
    DEFAULT_TRANSPARENT_TCP_REDIR_PORT,
    DEFAULT_TRANSPARENT_UDP_TPROXY_PORT,
    EXPLICIT_MIXED_LISTENER_BIND,
    EXPLICIT_MIXED_LISTENER_NAME,
    EXPLICIT_MIXED_LISTENER_PORT,
    FULL_VPN_REDIR_LISTENER_NAME,
    FULL_VPN_RULE_NAME,
    FULL_VPN_TPROXY_LISTENER_NAME,
    LEGACY_INBOUND_KEYS,
    MAX_BASE_CONFIG_BYTES,
    TRANSPARENT_BIND_ADDRESS,
    TRANSPARENT_REDIR_LISTENER_NAME,
    TRANSPARENT_TPROXY_LISTENER_NAME,
    TRANSPARENT_TPROXY_RULE_NAME,
    XRAY_MIHOMO_LISTENER_PREFIX,
    _resolved_base_config_path,
    _resolved_contours_path,
    _resolved_debug_dir,
    _resolved_last_good_mihomo_dir,
    _resolve_proxy_bypass_mark_value,
    _safe_load_yaml,
)


def _normalize_proxy_list(config: dict[str, Any]) -> dict[str, Any]:
    proxies = config.get("proxies")
    if isinstance(proxies, list):
        normalized_proxies: list[dict[str, Any]] = []
        for proxy in proxies:
            if not isinstance(proxy, dict):
                continue
            normalized = dict(proxy)
            proxy_type = normalized.get("type")
            if isinstance(proxy_type, str):
                normalized["type"] = proxy_type.lower()
            normalized_proxies.append(normalized)
        config["proxies"] = normalized_proxies
    return config


def _has_valid_proxy_definitions(config: dict[str, Any]) -> bool:
    proxies = config.get("proxies")
    if not isinstance(proxies, list) or not proxies:
        return False
    first_proxy = next((proxy for proxy in proxies if isinstance(proxy, dict)), None)
    if first_proxy is None:
        return False
    has_valid_first_proxy = bool(
        str(first_proxy.get("type") or "").strip().lower()
        and str(first_proxy.get("server") or "").strip()
    )
    proxy_groups = config.get("proxy-groups")
    has_vpn_global = isinstance(proxy_groups, list) and any(
        isinstance(group, dict) and str(group.get("name") or "").strip() == "vpn-global"
        for group in proxy_groups
    )
    return has_valid_first_proxy and has_vpn_global


def _candidate_base_config_paths() -> list[Path]:
    paths: list[Path] = [Path(_resolved_base_config_path())]

    last_good_dir = _resolved_last_good_mihomo_dir()
    if last_good_dir.exists():
        for candidate in sorted(last_good_dir.glob("config.*.yaml"), reverse=True):
            if candidate not in paths:
                paths.append(candidate)

    debug_dir = _resolved_debug_dir()
    if debug_dir.exists():
        for candidate in sorted(debug_dir.glob("*/mihomo-config.yaml"), reverse=True):
            if candidate not in paths:
                paths.append(candidate)

    previous = last_good_dir / "config.previous.yaml"
    if previous not in paths:
        paths.append(previous)

    return paths


def _load_base_config() -> dict[str, Any]:
    config: dict[str, Any] = {}
    for candidate in _candidate_base_config_paths():
        try:
            if candidate.exists() and candidate.stat().st_size > MAX_BASE_CONFIG_BYTES:
                continue
        except OSError:
            continue
        loaded = _safe_load_yaml(str(candidate))
        if not isinstance(loaded, dict):
            continue
        normalized = _normalize_proxy_list(dict(loaded))
        if _has_valid_proxy_definitions(normalized):
            config = normalized
            break
        if not config:
            config = normalized
    config["routing-mark"] = _resolve_proxy_bypass_mark_value()
    return config


def _load_contours() -> dict[str, Any]:
    contours_path = _resolved_contours_path()
    if not contours_path.exists():
        return {}
    try:
        loaded = yaml.safe_load(contours_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _collect_xray_handoff_assignments() -> list[dict[str, Any]]:
    from fwrouter_api.services.xray import collect_xray_runtime_bindings
    from fwrouter_api.services.xray_handoff import build_xray_handoff_assignments

    bindings = collect_xray_runtime_bindings()
    return build_xray_handoff_assignments(bindings)


def _build_xray_handoff_listeners(assignments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    listeners: list[dict[str, Any]] = []
    for assignment in assignments:
        listeners.append(
            {
                "name": assignment["listener_name"],
                "type": "mixed",
                "listen": assignment["listen"],
                "port": int(assignment["port"]),
                "udp": True,
                "proxy": str(assignment.get("proxy") or assignment["selected_server_id"]),
            }
        )
    return listeners


def _resolve_transparent_tproxy_port() -> int:
    contours = _load_contours()
    transparent = contours.get("transparent_vpn") if isinstance(contours, dict) else None
    redir_port = transparent.get("redir_port") if isinstance(transparent, dict) else None
    tproxy_port = transparent.get("tproxy_port") if isinstance(transparent, dict) else None
    if (
        isinstance(tproxy_port, int)
        and tproxy_port > 0
        and isinstance(redir_port, int)
        and redir_port > 0
        and tproxy_port != redir_port
    ):
        return int(tproxy_port)
    if not isinstance(tproxy_port, int) or tproxy_port <= 0:
        tproxy_port = DEFAULT_TRANSPARENT_UDP_TPROXY_PORT
    # Legacy contours stored a single tproxy_port=5202 for all transparent
    # ingress. Split-port contour upgrades that legacy value to
    # redir=5202/tproxy=5203 instead of reusing 5202 twice.
    if int(tproxy_port) == DEFAULT_TRANSPARENT_TCP_REDIR_PORT:
        return DEFAULT_TRANSPARENT_UDP_TPROXY_PORT
    return int(tproxy_port)


def _build_explicit_mixed_listener() -> dict[str, Any]:
    return {
        "name": EXPLICIT_MIXED_LISTENER_NAME,
        "type": "mixed",
        "port": EXPLICIT_MIXED_LISTENER_PORT,
        "listen": EXPLICIT_MIXED_LISTENER_BIND,
        "proxy": "vpn-global",
    }


def _resolve_transparent_bind_address() -> str:
    return TRANSPARENT_BIND_ADDRESS


def _transparent_bind_address_valid(value: str | None) -> bool:
    bind = str(value or "").strip()
    if not bind:
        return False
    if bind == TRANSPARENT_BIND_ADDRESS:
        return True
    try:
        parsed = ipaddress.ip_address(bind)
    except ValueError:
        return False
    return parsed.version == 4 and not parsed.is_loopback


def _resolve_transparent_redir_port() -> int:
    contours = _load_contours()
    transparent = contours.get("transparent_vpn") if isinstance(contours, dict) else None
    redir_port = transparent.get("redir_port") if isinstance(transparent, dict) else None
    if not isinstance(redir_port, int) or redir_port <= 0:
        legacy_tproxy_port = transparent.get("tproxy_port") if isinstance(transparent, dict) else None
        if isinstance(legacy_tproxy_port, int) and legacy_tproxy_port > 0:
            redir_port = legacy_tproxy_port
        else:
            redir_port = DEFAULT_TRANSPARENT_TCP_REDIR_PORT
    return int(redir_port)


def _managed_transparent_redir_port() -> int:
    return _resolve_transparent_redir_port()


def _managed_transparent_tproxy_port() -> int:
    return _resolve_transparent_tproxy_port()


def _managed_full_vpn_redir_port() -> int:
    return DEFAULT_FULL_VPN_TCP_REDIR_PORT


def _managed_full_vpn_tproxy_port() -> int:
    return DEFAULT_FULL_VPN_UDP_TPROXY_PORT


def _build_managed_transparent_listeners(bind_address: str) -> list[dict[str, Any]]:
    return [
        {
            "name": TRANSPARENT_REDIR_LISTENER_NAME,
            "type": "redir",
            "listen": bind_address,
            "port": _managed_transparent_redir_port(),
            "rule": TRANSPARENT_TPROXY_RULE_NAME,
        },
        {
            "name": TRANSPARENT_TPROXY_LISTENER_NAME,
            "type": "tproxy",
            "listen": bind_address,
            "port": _managed_transparent_tproxy_port(),
            "rule": TRANSPARENT_TPROXY_RULE_NAME,
            "udp": True,
        },
        {
            "name": FULL_VPN_REDIR_LISTENER_NAME,
            "type": "redir",
            "listen": bind_address,
            "port": _managed_full_vpn_redir_port(),
            "rule": FULL_VPN_RULE_NAME,
        },
        {
            "name": FULL_VPN_TPROXY_LISTENER_NAME,
            "type": "tproxy",
            "listen": bind_address,
            "port": _managed_full_vpn_tproxy_port(),
            "rule": FULL_VPN_RULE_NAME,
            "udp": True,
        },
    ]


def _ensure_fwrouter_sniffer(base_config: dict[str, Any]) -> None:
    """Force a sniffer profile that can recover transparent TCP destinations.

    Transparent LAN TCP arrives at the redir listener as a local socket. When
    Mihomo cannot recover the original destination address from the redirect
    contour, it must still sniff pure-IP/TLS traffic and rewrite the target
    from the observed hostname (HTTP Host / TLS SNI). Without these flags
    selective transparent VPN traffic can materialize as `127.0.0.1:5202`
    instead of the intended upstream domain.
    """

    sniffer = base_config.get("sniffer")
    if not isinstance(sniffer, dict):
        sniffer = {}
    else:
        sniffer = dict(sniffer)

    sniff = sniffer.get("sniff")
    if not isinstance(sniff, dict):
        sniff = {}
    else:
        sniff = dict(sniff)

    http_config = sniff.get("HTTP")
    if not isinstance(http_config, dict):
        http_config = {}
    else:
        http_config = dict(http_config)
    http_config["ports"] = [80, 8080]
    http_config["override-destination"] = True

    tls_config = sniff.get("TLS")
    if not isinstance(tls_config, dict):
        tls_config = {}
    else:
        tls_config = dict(tls_config)
    tls_config["ports"] = [443, 8443]
    tls_config["override-destination"] = True

    quic_config = sniff.get("QUIC")
    if not isinstance(quic_config, dict):
        quic_config = {}
    else:
        quic_config = dict(quic_config)
    quic_config["ports"] = [443, 8443]
    quic_config["override-destination"] = True

    sniff["HTTP"] = http_config
    sniff["TLS"] = tls_config
    sniff["QUIC"] = quic_config

    sniffer["enable"] = True
    sniffer["force-dns-mapping"] = True
    sniffer["parse-pure-ip"] = True
    sniffer["override-destination"] = True
    sniffer["sniff"] = sniff
    base_config["sniffer"] = sniffer


def _sanitize_fwrouter_managed_inbounds(base_config: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Remove legacy/managed inbound state that must not leak from base config."""

    removed_top_level_keys: list[str] = []
    for legacy_key in LEGACY_INBOUND_KEYS:
        if legacy_key in base_config:
            removed_top_level_keys.append(legacy_key)
            base_config.pop(legacy_key, None)

    existing_listeners = base_config.get("listeners") if isinstance(base_config.get("listeners"), list) else []
    preserved_listeners: list[dict[str, Any]] = []
    removed_listener_names: list[str] = []
    for listener in existing_listeners:
        if not isinstance(listener, dict):
            continue
        listener_name = str(listener.get("name") or "")
        if listener_name.startswith(XRAY_MIHOMO_LISTENER_PREFIX):
            removed_listener_names.append(listener_name)
            continue
        if listener_name in {
            EXPLICIT_MIXED_LISTENER_NAME,
            TRANSPARENT_REDIR_LISTENER_NAME,
            TRANSPARENT_TPROXY_LISTENER_NAME,
            FULL_VPN_REDIR_LISTENER_NAME,
            FULL_VPN_TPROXY_LISTENER_NAME,
        }:
            removed_listener_names.append(listener_name)
            continue
        preserved_listeners.append(dict(listener))

    base_config["listeners"] = preserved_listeners
    return base_config, {
        "removed_top_level_keys": removed_top_level_keys,
        "removed_listener_names": removed_listener_names,
    }

