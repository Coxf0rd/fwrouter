from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import filecmp
import ipaddress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from fwrouter_api.adapters.mihomo import DEFAULT_MIHOMO_ADAPTER
from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import db_session
from fwrouter_api.services.custom_servers import (
    resolve_mihomo_runtime_proxy_rows,
    resolve_runtime_proxy_rows,
)
from fwrouter_api.services.logs import write_operational_log, write_technical_log
from fwrouter_api.services.mihomo_runtime import restart_mihomo_container

MIHOMO_CANDIDATE_CONFIG_PATH = "/var/lib/fwrouter-v2/generated/mihomo/config.next.yaml"
BASE_CONFIG_PATH = "/var/lib/fwrouter-v2/generated/mihomo/config.yaml"
APPLIED_MANIFEST_PATH = "/var/lib/fwrouter-v2/generated/dataplane/applied-manifest.json"
XRAY_MIHOMO_LISTENER_PREFIX = "fwrouter-xray-egress-"
EXPLICIT_MIXED_LISTENER_NAME = "fwrouter-mixed"
EXPLICIT_MIXED_LISTENER_BIND = "127.0.0.1"
EXPLICIT_MIXED_LISTENER_PORT = 5201
TRANSPARENT_BIND_ADDRESS = "0.0.0.0"
MIHOMO_CONTROLLER_ADDRESS = "127.0.0.1:5200"
MAX_BASE_CONFIG_BYTES = 4 * 1024 * 1024
TRANSPARENT_TPROXY_RULE_NAME = "fwrouter-transparent"
TRANSPARENT_TPROXY_PROXY_NAME = "vpn-global"
TRANSPARENT_REDIR_LISTENER_NAME = "fwrouter-redir"
TRANSPARENT_TPROXY_LISTENER_NAME = "fwrouter-tproxy"
FULL_VPN_REDIR_LISTENER_NAME = "fwrouter-full-redir"
FULL_VPN_TPROXY_LISTENER_NAME = "fwrouter-full-tproxy"
LEGACY_INBOUND_KEYS = ("mixed-port", "port", "socks-port", "redir-port", "tproxy-port")
DEFAULT_TRANSPARENT_TCP_REDIR_PORT = 5202
DEFAULT_TRANSPARENT_UDP_TPROXY_PORT = 5203
DEFAULT_FULL_VPN_TCP_REDIR_PORT = 5204
DEFAULT_FULL_VPN_UDP_TPROXY_PORT = 5205


def _uses_state_override() -> bool:
    return bool(os.environ.get("FWROUTER_STATE_DIR") or os.environ.get("STATE_DIR"))


def _resolved_candidate_config_path() -> str:
    if _uses_state_override():
        return str(get_settings().paths.generated_dir / "mihomo" / "config.next.yaml")
    return MIHOMO_CANDIDATE_CONFIG_PATH


def _resolved_base_config_path() -> str:
    if _uses_state_override():
        return str(get_settings().paths.generated_dir / "mihomo" / "config.yaml")
    return BASE_CONFIG_PATH


def _resolved_applied_manifest_path() -> str:
    if _uses_state_override():
        return str(get_settings().paths.generated_dir / "dataplane" / "applied-manifest.json")
    return APPLIED_MANIFEST_PATH


def _resolved_contours_path() -> Path:
    if _uses_state_override():
        return get_settings().paths.generated_dir / "mihomo" / "contours.json"
    return Path("/var/lib/fwrouter-v2/generated/mihomo/contours.json")


def _resolved_last_good_mihomo_dir() -> Path:
    if _uses_state_override():
        return get_settings().paths.state_dir / "last-good" / "mihomo"
    return Path("/var/lib/fwrouter-v2/last-good/mihomo")


def _resolved_debug_dir() -> Path:
    if _uses_state_override():
        return get_settings().paths.state_dir / "debug"
    return Path("/var/lib/fwrouter-v2/debug")


def _write_mihomo_reconcile_logs(
    *,
    ok: bool,
    event_type: str,
    message: str,
    details: dict[str, Any],
    operational_level: str = "info",
    technical_level: str = "info",
) -> None:
    if operational_level != "debug":
        write_operational_log(
            event_type=event_type,
            level=operational_level,
            message=message,
            details=details,
        )
    write_technical_log(
        component="mihomo",
        event_type=event_type,
        level=technical_level,
        message=message,
        details=details,
    )


def _safe_load_yaml(path: str) -> dict[str, Any] | None:
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
    except (OSError, yaml.YAMLError):
        return None
    return data if isinstance(data, dict) else {}


def _count_top_level_yaml_sequence(path: str, key: str) -> int | None:
    """Count items in a top-level YAML sequence without parsing huge configs."""

    if not os.path.exists(path):
        return None
    target = f"{key}:"
    count = 0
    in_sequence = False
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.rstrip("\n")
                if not in_sequence:
                    if line == target:
                        in_sequence = True
                    continue

                if line.startswith("- "):
                    count += 1
                    continue
                if line and not line.startswith((" ", "-")):
                    break
    except OSError:
        return None
    return count


def _scan_fwrouter_config_metadata(path: str) -> dict[str, str]:
    if not os.path.exists(path):
        return {}
    metadata: dict[str, str] = {}
    in_fwrouter = False
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.rstrip("\n")
                stripped = line.strip()
                if not stripped:
                    continue
                if not line.startswith((" ", "\t")):
                    if in_fwrouter and not line.startswith("fwrouter:"):
                        break
                    in_fwrouter = line.startswith("fwrouter:")
                    continue
                if not in_fwrouter or ":" not in stripped:
                    continue
                key, value = stripped.split(":", 1)
                metadata[key.strip()] = value.strip().strip("'\"")
    except OSError:
        return {}
    return metadata


def _iso8601_mtime(path: str) -> str | None:
    if not os.path.exists(path):
        return None
    return datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc).isoformat()


def _resolve_proxy_bypass_mark_value() -> int:
    manifest = _safe_load_yaml(_resolved_applied_manifest_path())
    if isinstance(manifest, dict):
        contour = manifest.get("vpn_contour")
        if isinstance(contour, dict):
            value = contour.get("proxy_bypass_mark_value")
            if isinstance(value, int) and value > 0:
                return value
    return 512


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
            "proxy": "vpn-global",
        },
        {
            "name": FULL_VPN_TPROXY_LISTENER_NAME,
            "type": "tproxy",
            "listen": bind_address,
            "port": _managed_full_vpn_tproxy_port(),
            "proxy": "vpn-global",
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


def _resolved_selective_default(routing: dict[str, Any] | None = None) -> str:
    if isinstance(routing, dict):
        candidate = str(routing.get("selective_default") or "").strip().lower()
        if candidate in {"direct", "vpn"}:
            return candidate
    from fwrouter_api.services.rules import get_rules_state

    rules_state = get_rules_state()
    candidate = str(rules_state.get("selective_default") or "").strip().lower()
    return candidate if candidate in {"direct", "vpn"} else "direct"


def _build_fallback_rule(routing: dict[str, Any] | None = None) -> str:
    return "MATCH,DIRECT"


def _build_transparent_fallback_rule(routing: dict[str, Any] | None = None) -> str:
    """Return the fallback rule for FWRouter-managed transparent ingress.

    nft/dnsmasq remain the first decision layer, but IP sets can contain shared
    CDN addresses. The transparent listener must therefore re-apply domain
    rules after sniffing SNI/Host and only use the configured fallback when no
    domain rule matches.
    """

    mode = str((routing or {}).get("desired_mode") or (routing or {}).get("applied_mode") or "").strip().lower()
    if mode == "vpn" or _resolved_selective_default(routing) == "vpn":
        return "MATCH,vpn-global"
    return "MATCH,DIRECT"


def _build_effective_rules(routing: dict[str, Any] | None = None) -> tuple[list[str], dict[str, Any]]:
    from fwrouter_api.services.dataplane_global import read_effective_rules_artifact

    artifact = read_effective_rules_artifact()
    if not isinstance(artifact, dict):
        return [], {"rendered_rules_count": 0, "path_kind": "ip_only"}

    artifact_rules = artifact.get("rules")
    if not isinstance(artifact_rules, list):
        return [], {"rendered_rules_count": 0, "path_kind": "ip_only"}

    rendered_rules: list[str] = []
    domain_rule_count = 0
    for item in artifact_rules:
        if not isinstance(item, dict):
            continue

        action = str(item.get("action") or "").strip().upper()
        kind = str(item.get("kind") or "").strip().lower()
        raw_value = str(item.get("value") or "").strip()
        if not action or not kind or not raw_value:
            continue

        target = "vpn-global" if action == "VPN" else "DIRECT" if action == "DIRECT" else None
        if target is None:
            continue

        if kind == "domain":
            rendered_rules.append(f"DOMAIN,{raw_value.lower().rstrip('.')},{target}")
            domain_rule_count += 1
        elif kind == "domain_suffix":
            rendered_rules.append(f"DOMAIN-SUFFIX,{raw_value.lower().strip('.')},{target}")
            domain_rule_count += 1
        elif kind == "cidr":
            if ":" in raw_value:
                rendered_rules.append(f"IP-CIDR6,{raw_value},{target}")
            else:
                rendered_rules.append(f"IP-CIDR,{raw_value},{target}")

    return rendered_rules, {
        "rendered_rules_count": len(rendered_rules),
        "path_kind": "domain_aware" if domain_rule_count > 0 else "ip_only",
    }


def _format_source_ip_cidr_rule_value(value: str) -> str | None:
    """Return Mihomo-compatible source CIDR rule value for a host address."""

    raw_value = str(value or "").strip()
    if not raw_value:
        return None
    try:
        address = ipaddress.ip_address(raw_value)
    except ValueError:
        return None
    return f"{address}/{address.max_prefixlen}"


def _runtime_proxy_inventory_count() -> int:
    rows = resolve_mihomo_runtime_proxy_rows(inventory_state="active", limit=1000)
    return sum(
        1
        for row in rows
        if isinstance(row, dict)
        and isinstance(row.get("raw"), dict)
        and str((row.get("raw") or {}).get("name") or "").strip()
    )


def _config_structural_fingerprint(config: dict[str, Any]) -> dict[str, Any]:
    listeners = config.get("listeners") if isinstance(config.get("listeners"), list) else []
    normalized_listeners = [
        {
            "name": str(listener.get("name") or ""),
            "type": str(listener.get("type") or ""),
            "listen": str(listener.get("listen") or ""),
            "port": int(listener.get("port") or 0),
            "proxy": str(listener.get("proxy") or ""),
            "rule": str(listener.get("rule") or ""),
        }
        for listener in listeners
        if isinstance(listener, dict)
    ]
    return {
        "mixed-port": int(config.get("mixed-port") or 0),
        "routing-mark": int(config.get("routing-mark") or 0),
        "allow-lan": bool(config.get("allow-lan", False)),
        "mode": str(config.get("mode") or ""),
        "listeners": normalized_listeners,
        "tun_enabled": bool((config.get("tun") or {}).get("enable")) if isinstance(config.get("tun"), dict) else False,
    }


def _configs_equal(left: dict[str, Any] | None, right: dict[str, Any] | None) -> bool:
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    return json.dumps(left, ensure_ascii=False, sort_keys=True) == json.dumps(right, ensure_ascii=False, sort_keys=True)


def _summarize_candidate(candidate: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(candidate, dict):
        return {}
    summary = dict(candidate)
    summary.pop("config", None)
    summary["rules_count"] = len(candidate.get("rules") or [])
    return summary


def _summarize_config_status(status: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(status, dict):
        return {}
    summary = dict(status)
    summary.pop("base_config", None)
    summary.pop("candidate_config", None)
    return summary


def _merge_runtime_proxies(base_config: dict[str, Any]) -> list[dict[str, Any]]:
    runtime_proxy_rows = resolve_mihomo_runtime_proxy_rows(inventory_state="active", limit=1000)
    runtime_proxies: list[dict[str, Any]] = []
    seen_names: set[str] = set()

    for item in runtime_proxy_rows:
        if not isinstance(item, dict) or not isinstance(item.get("raw"), dict):
            continue
        proxy = _normalize_proxy_list({"proxies": [dict(item["raw"])]}).get("proxies")[0]
        name = str(proxy.get("name") or "").strip()
        proxy_type = str(proxy.get("type") or "").strip().lower()
        if not name or not proxy_type:
            continue
        if proxy_type != "http" and not str(proxy.get("server") or "").strip():
            continue
        if name in seen_names:
            continue
        runtime_proxies.append(proxy)
        seen_names.add(name)

    return runtime_proxies


def _load_vpn_auto_proxy_names() -> list[str]:
    with db_session() as connection:
        rows = connection.execute(
            """
            SELECT s.server_name
            FROM servers AS s
            JOIN server_preferences AS p ON p.server_id = s.server_id
            WHERE s.inventory_state = 'active'
              AND COALESCE(p.vpn_auto, 0) = 1
              AND COALESCE(p.manually_deleted_at, '') = ''
            ORDER BY s.server_name, s.server_id
            """
        ).fetchall()
    return [str(row["server_name"]) for row in rows if str(row["server_name"] or "").strip()]


def _load_custom_proxy_names() -> set[str]:
    with db_session() as connection:
        rows = connection.execute(
            """
            SELECT s.server_name
            FROM servers AS s
            JOIN server_custom_https_proxy AS c ON c.server_id = s.server_id
            WHERE s.inventory_state = 'active'
            """
        ).fetchall()
    return {str(row["server_name"]) for row in rows if str(row["server_name"] or "").strip()}


def _ensure_selector_groups(base_config: dict[str, Any]) -> list[dict[str, Any]]:
    existing_groups = base_config.get("proxy-groups") if isinstance(base_config.get("proxy-groups"), list) else []
    groups_by_name: dict[str, dict[str, Any]] = {}
    for group in existing_groups:
        if not isinstance(group, dict):
            continue
        name = str(group.get("name") or "").strip()
        if not name:
            continue
        groups_by_name[name] = dict(group)

    proxy_names = [
        str(proxy.get("name"))
        for proxy in (base_config.get("proxies") or [])
        if isinstance(proxy, dict) and str(proxy.get("name") or "").strip()
    ]
    proxy_name_set = set(proxy_names)
    vpn_auto_names = [
        name
        for name in _load_vpn_auto_proxy_names()
        if name in proxy_name_set
    ]
    global_list_proxy_names = [
        str((row.get("raw") or {}).get("name") or row.get("server_name") or "").strip()
        for row in resolve_runtime_proxy_rows(inventory_state="active", global_list=True, limit=1000)
        if isinstance(row, dict)
    ]
    global_list_proxy_names = [
        name
        for name in global_list_proxy_names
        if name and name in proxy_name_set
    ]

    groups_by_name["vpn-auto"] = {
        "name": "vpn-auto",
        "type": "select",
        "proxies": [*vpn_auto_names, "DIRECT"],
    }

    vpn_global_proxies = ["vpn-auto"]
    for name in global_list_proxy_names:
        if name not in vpn_global_proxies:
            vpn_global_proxies.append(name)
    vpn_global_proxies.append("DIRECT")
    groups_by_name["vpn-global"] = {
        "name": "vpn-global",
        "type": "select",
        "proxies": vpn_global_proxies,
    }

    ordered_names = ["vpn-auto", "vpn-global"]
    ordered_names.extend(
        name for name in groups_by_name.keys()
        if name not in {"vpn-auto", "vpn-global"}
    )
    return [groups_by_name[name] for name in ordered_names]


def _build_mihomo_config_with_source(routing: dict[str, Any] | None = None) -> tuple[list[str], dict[str, Any]]:
    transparent_rules = []

    with db_session() as connection:
        cursor = connection.execute("""
            SELECT
                coalesce(l.ip_address, d.ip_address) as ip,
                srv.server_name
            FROM subject_server_overrides o
            JOIN subjects s ON o.subject_id = s.subject_id
            JOIN servers srv ON o.selected_server_id = srv.server_id
            LEFT JOIN subject_lan l ON s.subject_id = l.subject_id
            LEFT JOIN subject_docker d ON s.subject_id = d.subject_id
            WHERE s.is_active = 1 AND s.is_deleted = 0 AND ip IS NOT NULL
        """)
        rows = cursor.fetchall()
        
    for row in rows:
        safe_name = "".join(c for c in row["server_name"] if c.isalnum() or c in " -_")
        source_cidr = _format_source_ip_cidr_rule_value(str(row["ip"] or ""))
        if source_cidr:
            transparent_rules.append(f"SRC-IP-CIDR,{source_cidr},{safe_name}")

    effective_rules, effective_metadata = _build_effective_rules(routing)
    final_match_rule = _build_fallback_rule(routing)
    transparent_final_match_rule = _build_transparent_fallback_rule(routing)
    base_rules = [*effective_rules, final_match_rule]
    transparent_rules.extend(effective_rules)
    transparent_rules.append(transparent_final_match_rule)
    return base_rules, {
        "rules": base_rules,
        "transparent_rules": transparent_rules,
        "scoped_vpn_source_rules_count": 0,
        "resolved_selective_default": _resolved_selective_default(routing),
        "final_match_rule": final_match_rule,
        "transparent_final_match_rule": transparent_final_match_rule,
        "rendered_rules_count": int(effective_metadata.get("rendered_rules_count") or 0),
        "path_kind": str(effective_metadata.get("path_kind") or "core_routed"),
    }


def build_mihomo_config(routing: dict[str, Any] | None = None) -> dict[str, Any]:
    rules, metadata = _build_mihomo_config_with_source(routing)

    base_config = _load_base_config()
    base_config, sanitized_inbounds = _sanitize_fwrouter_managed_inbounds(base_config)
    base_config["rules"] = list(rules)
    base_config["proxies"] = _merge_runtime_proxies(base_config)
    base_config["proxy-groups"] = _ensure_selector_groups(base_config)
    sub_rules = base_config.get("sub-rules")
    if not isinstance(sub_rules, dict):
        sub_rules = {}
    else:
        sub_rules = dict(sub_rules)
    sub_rules[TRANSPARENT_TPROXY_RULE_NAME] = list(metadata["transparent_rules"])
    base_config["sub-rules"] = sub_rules
    handoff_assignments = _collect_xray_handoff_assignments()
    transparent_bind_address = _resolve_transparent_bind_address()
    managed_transparent_listeners = _build_managed_transparent_listeners(transparent_bind_address)
    base_config["listeners"] = (
        [_build_explicit_mixed_listener()]
        + managed_transparent_listeners
        + list(base_config.get("listeners") or [])
        + _build_xray_handoff_listeners(handoff_assignments)
    )
    transparent_redir_port = _managed_transparent_redir_port()
    transparent_tproxy_port = _managed_transparent_tproxy_port()
    full_vpn_redir_port = _managed_full_vpn_redir_port()
    full_vpn_tproxy_port = _managed_full_vpn_tproxy_port()
    if "redir-port" in base_config:
        del base_config["redir-port"]
    if "tproxy-port" in base_config:
        del base_config["tproxy-port"]
    base_config["bind-address"] = transparent_bind_address
    base_config["external-controller"] = MIHOMO_CONTROLLER_ADDRESS
    base_config["allow-lan"] = True
    # FWRouter transparent ingress is currently IPv4-only. Leaving Mihomo
    # IPv6 enabled here can make the tproxy listener bind as IPv6-only
    # (`[::]:5202`), which breaks LAN transparent interception for IPv4
    # clients even though the YAML still says `listen: 0.0.0.0`.
    base_config["ipv6"] = False
    base_config.setdefault("mode", "rule")
    _ensure_fwrouter_sniffer(base_config)
    base_config.setdefault("fwrouter", {})
    if isinstance(base_config["fwrouter"], dict):
        base_config["fwrouter"].update(
            {
                "resolved_selective_default": metadata["resolved_selective_default"],
                "final_match_rule": metadata["final_match_rule"],
                "transparent_final_match_rule": metadata["transparent_final_match_rule"],
                "rendered_rules_count": metadata["rendered_rules_count"],
                "scoped_vpn_source_rules_count": metadata["scoped_vpn_source_rules_count"],
                "transparent_rule_name": TRANSPARENT_TPROXY_RULE_NAME,
                "path_kind": metadata["path_kind"],
                "state_consistency_ok": True,
                "transparent_mechanism": "split_redir_tproxy_ports",
                "mixed_listener_name": EXPLICIT_MIXED_LISTENER_NAME,
                "mixed_listener_bind": EXPLICIT_MIXED_LISTENER_BIND,
                "mixed_listener_port": EXPLICIT_MIXED_LISTENER_PORT,
                "transparent_listener_name": TRANSPARENT_TPROXY_LISTENER_NAME,
                "transparent_listener_bind": transparent_bind_address,
                "transparent_redir_port": transparent_redir_port,
                "transparent_tproxy_port": transparent_tproxy_port,
                "full_vpn_redir_port": full_vpn_redir_port,
                "full_vpn_tproxy_port": full_vpn_tproxy_port,
                "transparent_listener_port": transparent_tproxy_port,
                "transparent_inbound_rules": [],
                "sanitized_legacy_inbound_keys": list(sanitized_inbounds["removed_top_level_keys"]),
                "sanitized_managed_listeners": list(sanitized_inbounds["removed_listener_names"]),
            }
        )
    return base_config


def _candidate_runtime_proxies(candidate_config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    proxies = candidate_config.get("proxies")
    if not isinstance(proxies, list):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for proxy in proxies:
        if not isinstance(proxy, dict):
            continue
        name = str(proxy.get("name") or "").strip()
        if not name:
            continue
        result[name] = proxy
    return result


def _candidate_group_names(candidate_config: dict[str, Any]) -> set[str]:
    groups = candidate_config.get("proxy-groups")
    if not isinstance(groups, list):
        return set()
    names: set[str] = set()
    for group in groups:
        if not isinstance(group, dict):
            continue
        name = str(group.get("name") or "").strip()
        if name:
            names.add(name)
    return names


def _validate_candidate_with_binary(candidate_path: str) -> dict[str, Any]:
    for binary_name in ("mihomo", "clash-meta", "clash"):
        binary_path = shutil.which(binary_name)
        if not binary_path:
            continue
        completed = subprocess.run(
            [binary_path, "-t", "-f", candidate_path],
            capture_output=True,
            text=True,
            check=False,
        )
        stdout_tail = (completed.stdout or "")[-4000:]
        stderr_tail = (completed.stderr or "")[-4000:]
        return {
            "available": True,
            "binary": binary_path,
            "ok": completed.returncode == 0,
            "returncode": completed.returncode,
            "stdout_tail": stdout_tail,
            "stderr_tail": stderr_tail,
        }

    return {
        "available": False,
        "binary": None,
        "ok": True,
        "returncode": 0,
        "stdout_tail": "",
        "stderr_tail": "",
    }


def _validate_candidate_structure(
    candidate_config: dict[str, Any],
    *,
    routing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    rules = candidate_config.get("rules") if isinstance(candidate_config.get("rules"), list) else []
    proxies = candidate_config.get("proxies") if isinstance(candidate_config.get("proxies"), list) else []
    groups = candidate_config.get("proxy-groups") if isinstance(candidate_config.get("proxy-groups"), list) else []
    listeners = candidate_config.get("listeners") if isinstance(candidate_config.get("listeners"), list) else []
    sub_rules = candidate_config.get("sub-rules") if isinstance(candidate_config.get("sub-rules"), dict) else {}

    final_match_rule = str(rules[-1]) if rules else ""
    expected_final_match_rule = _build_fallback_rule(routing)
    state_consistency_ok = final_match_rule == expected_final_match_rule

    runtime_proxy_count = _runtime_proxy_inventory_count()
    proxy_inventory_ok = not (runtime_proxy_count > 0 and len(proxies) == 0)
    allow_lan_enabled = bool(candidate_config.get("allow-lan"))
    routing_mark_value = int(candidate_config.get("routing-mark") or 0)
    expected_routing_mark_value = _resolve_proxy_bypass_mark_value()
    legacy_inbound_keys_present = [key for key in LEGACY_INBOUND_KEYS if key in candidate_config]
    managed_redir_port = candidate_config.get("redir-port")
    if isinstance(managed_redir_port, str) and managed_redir_port.isdigit():
        managed_redir_port = int(managed_redir_port)
    if not isinstance(managed_redir_port, int):
        managed_redir_port = None

    managed_tproxy_port = candidate_config.get("tproxy-port")
    if isinstance(managed_tproxy_port, str) and managed_tproxy_port.isdigit():
        managed_tproxy_port = int(managed_tproxy_port)
    if not isinstance(managed_tproxy_port, int):
        managed_tproxy_port = None
    top_level_bind_address = str(candidate_config.get("bind-address") or "").strip() or None

    proxy_names = set(_candidate_runtime_proxies(candidate_config).keys())
    group_names = _candidate_group_names(candidate_config)
    vpn_auto_present = "vpn-auto" in group_names
    vpn_global_present = "vpn-global" in group_names

    vpn_global_has_vpn_auto = False
    for group in groups:
        if not isinstance(group, dict):
            continue
        if str(group.get("name") or "").strip() != "vpn-global":
            continue
        targets = group.get("proxies")
        if isinstance(targets, list):
            vpn_global_has_vpn_auto = "vpn-auto" in [str(item) for item in targets]
        break

    mixed_listeners = [
        listener
        for listener in listeners
        if isinstance(listener, dict)
        and str(listener.get("name") or "").strip() == EXPLICIT_MIXED_LISTENER_NAME
    ]
    mixed_listener = mixed_listeners[0] if mixed_listeners else None
    mixed_listener_proxy = (
        str(mixed_listener.get("proxy") or "").strip()
        if isinstance(mixed_listener, dict)
        else None
    )
    mixed_listener_bind = (
        str(mixed_listener.get("listen") or "").strip()
        if isinstance(mixed_listener, dict)
        else None
    )
    mixed_listener_port = (
        int(mixed_listener.get("port") or 0)
        if isinstance(mixed_listener, dict)
        else None
    )

    transparent_redir_listener = next(
        (
            listener
            for listener in listeners
            if isinstance(listener, dict)
            and str(listener.get("name") or "").strip() == TRANSPARENT_REDIR_LISTENER_NAME
            and str(listener.get("type") or "").strip().lower() == "redir"
        ),
        None,
    )
    transparent_tproxy_listener = next(
        (
            listener
            for listener in listeners
            if isinstance(listener, dict)
            and str(listener.get("name") or "").strip() == TRANSPARENT_TPROXY_LISTENER_NAME
            and str(listener.get("type") or "").strip().lower() == "tproxy"
        ),
        None,
    )
    transparent_rule_name = TRANSPARENT_TPROXY_RULE_NAME
    transparent_listener_proxy = TRANSPARENT_TPROXY_PROXY_NAME
    transparent_listener_bind = None
    if isinstance(transparent_redir_listener, dict):
        transparent_listener_bind = str(transparent_redir_listener.get("listen") or "").strip() or None
        if not isinstance(managed_redir_port, int):
            port_value = transparent_redir_listener.get("port")
            if isinstance(port_value, int):
                managed_redir_port = port_value
    if isinstance(transparent_tproxy_listener, dict):
        if not transparent_listener_bind:
            transparent_listener_bind = str(transparent_tproxy_listener.get("listen") or "").strip() or None
        if not isinstance(managed_tproxy_port, int):
            port_value = transparent_tproxy_listener.get("port")
            if isinstance(port_value, int):
                managed_tproxy_port = port_value
    if not transparent_listener_bind:
        transparent_listener_bind = top_level_bind_address
    transparent_listener_bind_valid = _transparent_bind_address_valid(transparent_listener_bind)
    transparent_subrules = sub_rules.get(transparent_rule_name) if transparent_rule_name else None
    transparent_subrules_ok = isinstance(transparent_subrules, list) and bool(transparent_subrules)
    transparent_final_match_rule = (
        str(transparent_subrules[-1])
        if isinstance(transparent_subrules, list) and transparent_subrules
        else ""
    )
    expected_transparent_final_match_rule = _build_transparent_fallback_rule(routing)
    transparent_direct_proxy_ok = (
        isinstance(transparent_redir_listener, dict)
        and isinstance(transparent_tproxy_listener, dict)
        and str(transparent_redir_listener.get("proxy") or "").strip() == TRANSPARENT_TPROXY_PROXY_NAME
        and str(transparent_tproxy_listener.get("proxy") or "").strip() == TRANSPARENT_TPROXY_PROXY_NAME
    )
    transparent_listener_rule_ok = (
        isinstance(transparent_redir_listener, dict)
        and isinstance(transparent_tproxy_listener, dict)
        and str(transparent_redir_listener.get("rule") or "").strip() == transparent_rule_name
        and str(transparent_tproxy_listener.get("rule") or "").strip() == transparent_rule_name
    )
    transparent_inbound_rule_ok = transparent_direct_proxy_ok or transparent_listener_rule_ok
    transparent_state_consistency_ok = (
        transparent_direct_proxy_ok
        or transparent_final_match_rule == expected_transparent_final_match_rule
    )
    contours = _load_contours()
    transparent_contour = contours.get("transparent_vpn") if isinstance(contours, dict) else None
    transparent_required = bool(
        isinstance(transparent_contour, dict)
        and transparent_contour.get("ready")
        and transparent_contour.get("tproxy_port")
        and transparent_contour.get("redir_port")
    )
    desired_mode = str((routing or {}).get("desired_mode") or "").strip().lower()
    if desired_mode in {"selective", "vpn"}:
        transparent_required = True

    handoff_targets_missing: list[str] = []
    for listener in listeners:
        if not isinstance(listener, dict):
            continue
        name = str(listener.get("name") or "").strip()
        if not name.startswith(XRAY_MIHOMO_LISTENER_PREFIX):
            continue
        proxy_target = str(listener.get("proxy") or "").strip()
        if proxy_target and proxy_target not in proxy_names and proxy_target not in group_names and proxy_target != "DIRECT":
            handoff_targets_missing.append(name)

    return {
        "final_match_rule": final_match_rule,
        "expected_final_match_rule": expected_final_match_rule,
        "state_consistency_ok": state_consistency_ok,
        "runtime_proxy_inventory_count": runtime_proxy_count,
        "candidate_proxies_count": len(proxies),
        "proxy_inventory_ok": proxy_inventory_ok,
        "allow_lan_enabled": allow_lan_enabled,
        "routing_mark_value": routing_mark_value,
        "expected_routing_mark_value": expected_routing_mark_value,
        "legacy_inbound_keys_present": legacy_inbound_keys_present,
        "vpn_auto_present": vpn_auto_present,
        "vpn_global_present": vpn_global_present,
        "vpn_global_has_vpn_auto": vpn_global_has_vpn_auto,
        "mixed_listener_count": len(mixed_listeners),
        "mixed_listener_bind": mixed_listener_bind,
        "mixed_listener_port": mixed_listener_port,
        "mixed_listener_proxy": mixed_listener_proxy,
        "transparent_required": transparent_required,
        "transparent_listener_present": isinstance(transparent_redir_listener, dict) and isinstance(transparent_tproxy_listener, dict),
        "transparent_listener_count": int(isinstance(transparent_redir_listener, dict)) + int(isinstance(transparent_tproxy_listener, dict)),
        "transparent_listener_bind": transparent_listener_bind,
        "transparent_redir_port": managed_redir_port,
        "transparent_listener_port": managed_tproxy_port,
        "transparent_listener_bind_valid": transparent_listener_bind_valid,
        "transparent_listener_proxy": transparent_listener_proxy or None,
        "transparent_rule_name": transparent_rule_name or None,
        "transparent_direct_proxy_ok": transparent_direct_proxy_ok,
        "transparent_listener_rule_ok": transparent_listener_rule_ok,
        "transparent_inbound_rule": None,
        "transparent_inbound_rules": [],
        "transparent_inbound_rule_ok": transparent_inbound_rule_ok,
        "transparent_subrules_ok": transparent_subrules_ok,
        "transparent_final_match_rule": transparent_final_match_rule,
        "expected_transparent_final_match_rule": expected_transparent_final_match_rule,
        "transparent_state_consistency_ok": transparent_state_consistency_ok,
        "xray_handoff_targets_missing": handoff_targets_missing,
    }

def write_mihomo_candidate_config(routing: dict[str, Any] | None = None) -> dict[str, Any]:
    """Генерация и запись конфига Mihomo."""
    base_config = build_mihomo_config(routing)
    rules = list(base_config.get("rules") or [])
    handoff_assignments = _collect_xray_handoff_assignments()

    candidate_path = _resolved_candidate_config_path()
    os.makedirs(Path(candidate_path).parent, exist_ok=True)
    candidate_dir = Path(candidate_path).parent
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=candidate_dir, delete=False) as handle:
        yaml.dump(base_config, handle, sort_keys=False)
        temp_path = handle.name
    os.replace(temp_path, candidate_path)

    fwrouter_meta = base_config.get("fwrouter") if isinstance(base_config.get("fwrouter"), dict) else {}
    result = {
        "candidate_path": candidate_path,
        "rules": rules,
        "handoff_assignments": handoff_assignments,
        "resolved_selective_default": fwrouter_meta.get("resolved_selective_default"),
        "final_match_rule": fwrouter_meta.get("final_match_rule"),
        "transparent_final_match_rule": fwrouter_meta.get("transparent_final_match_rule"),
        "config": base_config,
    }
    write_technical_log(
        component="mihomo",
        event_type="mihomo_candidate_config_written",
        level="info",
        message="Mihomo candidate config generated.",
        details={
            "candidate_path": result["candidate_path"],
            "rules_count": len(result["rules"]),
            "resolved_selective_default": result["resolved_selective_default"],
            "final_match_rule": result["final_match_rule"],
            "transparent_final_match_rule": result["transparent_final_match_rule"],
            "handoff_assignments_count": len(result["handoff_assignments"]),
        },
    )
    return result


def validate_mihomo_candidate_config(routing: dict[str, Any] | None = None) -> dict[str, Any]:
    candidate_path = _resolved_candidate_config_path()
    candidate_config = _safe_load_yaml(candidate_path)
    if not isinstance(candidate_config, dict):
        return {
            "ok": False,
            "returncode": 1,
            "stdout_tail": "",
            "stderr_tail": "Candidate config is missing or invalid.",
            "error_code": "MIHOMO_CANDIDATE_INVALID",
        }

    selective_default = _resolved_selective_default(routing)
    structural = _validate_candidate_structure(candidate_config, routing=routing)
    binary_validation = _validate_candidate_with_binary(candidate_path)

    error_code = None
    stderr_tail = ""
    if not structural["allow_lan_enabled"]:
        error_code = "MIHOMO_ALLOW_LAN_REQUIRED"
        stderr_tail = "FWRouter Mihomo contour requires allow-lan=true."
    elif structural["routing_mark_value"] != structural["expected_routing_mark_value"]:
        error_code = "MIHOMO_ROUTING_MARK_MISMATCH"
        stderr_tail = (
            "Mihomo routing-mark does not match the FWRouter bypass mark contract."
        )
    elif structural["legacy_inbound_keys_present"]:
        error_code = "MIHOMO_LEGACY_INBOUND_CONFLICT"
        stderr_tail = (
            "Candidate Mihomo config still contains legacy top-level inbound keys: "
            + ", ".join(structural["legacy_inbound_keys_present"])
        )
    elif structural["mixed_listener_count"] != 1:
        error_code = "MIHOMO_MIXED_LISTENER_CONFLICT"
        stderr_tail = "FWRouter explicit mixed listener must exist exactly once."
    elif (
        structural["mixed_listener_bind"] != EXPLICIT_MIXED_LISTENER_BIND
        or structural["mixed_listener_port"] != EXPLICIT_MIXED_LISTENER_PORT
        or structural["mixed_listener_proxy"] != "vpn-global"
    ):
        error_code = "MIHOMO_MIXED_LISTENER_INVALID"
        stderr_tail = "FWRouter explicit mixed listener does not match the expected 127.0.0.1:5201 -> vpn-global contract."
    elif not structural["proxy_inventory_ok"]:
        error_code = "MIHOMO_PROXYSET_EMPTY"
        stderr_tail = "Candidate Mihomo config has no proxies while runtime VPN inventory is non-empty."
    elif not structural["vpn_auto_present"]:
        error_code = "MIHOMO_VPN_AUTO_MISSING"
        stderr_tail = "Candidate Mihomo config is missing vpn-auto selector."
    elif not structural["vpn_global_present"]:
        error_code = "MIHOMO_VPN_GLOBAL_MISSING"
        stderr_tail = "Candidate Mihomo config is missing vpn-global selector."
    elif not structural["vpn_global_has_vpn_auto"]:
        error_code = "MIHOMO_VPN_GLOBAL_MISWIRED"
        stderr_tail = "vpn-global selector does not include vpn-auto."
    elif structural["transparent_required"] and structural["transparent_listener_count"] != 2:
        error_code = "MIHOMO_TRANSPARENT_LISTENER_CONFLICT"
        stderr_tail = "FWRouter transparent ingress must expose exactly one REDIR port and one TPROXY port."
    elif structural["transparent_required"] and not structural["transparent_listener_present"]:
        error_code = "MIHOMO_TRANSPARENT_LISTENER_MISSING"
        stderr_tail = "Transparent REDIR/TPROXY ports are missing from candidate config."
    elif structural["transparent_required"] and not structural["transparent_listener_bind_valid"]:
        error_code = "MIHOMO_TRANSPARENT_LISTENER_BIND_INVALID"
        stderr_tail = (
            "Transparent TPROXY ingress must bind to the wildcard address or a real router IPv4, "
            f"got {structural['transparent_listener_bind'] or 'missing'}."
        )
    elif structural["transparent_required"] and not structural["transparent_inbound_rule_ok"]:
        error_code = "MIHOMO_TRANSPARENT_TARGET_MISSING"
        stderr_tail = "Transparent REDIR/TPROXY listeners must target vpn-global directly."
    elif (
        structural["transparent_required"]
        and not structural["transparent_direct_proxy_ok"]
        and not structural["transparent_subrules_ok"]
    ):
        error_code = "MIHOMO_TRANSPARENT_TARGET_MISSING"
        stderr_tail = "Transparent REDIR/TPROXY ingress must route to vpn-global directly or reference valid fwrouter sub-rules."
    elif not structural["state_consistency_ok"]:
        error_code = "MIHOMO_SELECTIVE_DEFAULT_MISMATCH"
        stderr_tail = "Resolved selective_default does not match candidate fallback rule."
    elif structural["transparent_required"] and not structural["transparent_state_consistency_ok"]:
        error_code = "MIHOMO_TRANSPARENT_FALLBACK_MISMATCH"
        stderr_tail = "Transparent listener sub-rules fallback does not match the resolved selective_default."
    elif structural["xray_handoff_targets_missing"]:
        error_code = "MIHOMO_XRAY_HANDOFF_TARGET_MISSING"
        stderr_tail = "One or more Xray handoff listeners reference missing proxy or selector targets."
    elif not binary_validation["ok"]:
        error_code = "MIHOMO_BINARY_VALIDATION_FAILED"
        stderr_tail = binary_validation["stderr_tail"] or "Mihomo binary validation failed."
    result = {
        "ok": error_code is None,
        "returncode": 0 if error_code is None else 1,
        "stdout_tail": binary_validation["stdout_tail"],
        "stderr_tail": stderr_tail,
        "resolved_selective_default": selective_default,
        "final_match_rule": structural["final_match_rule"],
        "expected_final_match_rule": structural["expected_final_match_rule"],
        "state_consistency_ok": structural["state_consistency_ok"],
        "runtime_proxy_inventory_count": structural["runtime_proxy_inventory_count"],
        "candidate_proxies_count": structural["candidate_proxies_count"],
        "proxy_inventory_ok": structural["proxy_inventory_ok"],
        "allow_lan_enabled": structural["allow_lan_enabled"],
        "routing_mark_value": structural["routing_mark_value"],
        "expected_routing_mark_value": structural["expected_routing_mark_value"],
        "legacy_inbound_keys_present": structural["legacy_inbound_keys_present"],
        "vpn_auto_present": structural["vpn_auto_present"],
        "vpn_global_present": structural["vpn_global_present"],
        "vpn_global_has_vpn_auto": structural["vpn_global_has_vpn_auto"],
        "mixed_listener_count": structural["mixed_listener_count"],
        "mixed_listener_bind": structural["mixed_listener_bind"],
        "mixed_listener_port": structural["mixed_listener_port"],
        "mixed_listener_proxy": structural["mixed_listener_proxy"],
        "transparent_required": structural["transparent_required"],
        "transparent_listener_present": structural["transparent_listener_present"],
        "transparent_listener_count": structural["transparent_listener_count"],
        "transparent_listener_bind": structural["transparent_listener_bind"],
        "transparent_redir_port": structural["transparent_redir_port"],
        "transparent_listener_port": structural["transparent_listener_port"],
        "transparent_listener_bind_valid": structural["transparent_listener_bind_valid"],
        "transparent_listener_proxy": structural["transparent_listener_proxy"],
        "transparent_rule_name": structural["transparent_rule_name"],
        "transparent_direct_proxy_ok": structural["transparent_direct_proxy_ok"],
        "transparent_inbound_rule": structural["transparent_inbound_rule"],
        "transparent_inbound_rules": structural["transparent_inbound_rules"],
        "transparent_inbound_rule_ok": structural["transparent_inbound_rule_ok"],
        "transparent_subrules_ok": structural["transparent_subrules_ok"],
        "transparent_final_match_rule": structural["transparent_final_match_rule"],
        "expected_transparent_final_match_rule": structural["expected_transparent_final_match_rule"],
        "transparent_state_consistency_ok": structural["transparent_state_consistency_ok"],
        "xray_handoff_targets_missing": structural["xray_handoff_targets_missing"],
        "binary_validation": binary_validation,
        "error_code": error_code,
    }
    write_technical_log(
        component="mihomo",
        event_type="mihomo_candidate_config_validated",
        level="info" if result["ok"] else "warning",
        message="Mihomo candidate config validation completed." if result["ok"] else "Mihomo candidate config validation failed.",
        details=result,
    )
    return result


def get_mihomo_config_status(*, include_config: bool = False) -> dict[str, Any]:
    base_path = _resolved_base_config_path()
    candidate_path = _resolved_candidate_config_path()
    current_config = _safe_load_yaml(base_path) if include_config else None
    candidate_config = _safe_load_yaml(candidate_path) if include_config else None

    status = {
        "base_path": base_path,
        "candidate_path": candidate_path,
        "base_exists": os.path.exists(base_path),
        "candidate_exists": os.path.exists(candidate_path),
        "base_updated_at": _iso8601_mtime(base_path),
        "candidate_updated_at": _iso8601_mtime(candidate_path),
        "base_rules_count": (
            len((current_config or {}).get("rules") or [])
            if include_config
            else _count_top_level_yaml_sequence(base_path, "rules")
        ),
        "candidate_rules_count": (
            len((candidate_config or {}).get("rules") or [])
            if include_config
            else _count_top_level_yaml_sequence(candidate_path, "rules")
        ),
    }
    if include_config:
        status["base_config"] = current_config
        status["candidate_config"] = candidate_config
    return status


def mihomo_runtime_satisfies_routing(routing: dict[str, Any] | None = None) -> dict[str, Any]:
    """Cheaply verify that live Mihomo already satisfies routing-owned config.

    This intentionally avoids generating and validating the full 100k+ rule
    candidate YAML. Any uncertainty returns ok=False so callers can fall back to
    the full reconcile path.
    """

    routing_dict = routing if isinstance(routing, dict) else {}
    base_path = _resolved_base_config_path()
    metadata = _scan_fwrouter_config_metadata(base_path)
    expected_final_match_rule = _build_fallback_rule(routing_dict)
    expected_transparent_final_match_rule = _build_transparent_fallback_rule(routing_dict)
    expected_selective_default = _resolved_selective_default(routing_dict)

    metadata_ok = (
        metadata.get("resolved_selective_default") == expected_selective_default
        and metadata.get("final_match_rule") == expected_final_match_rule
        and metadata.get("transparent_final_match_rule") == expected_transparent_final_match_rule
    )
    if not metadata_ok:
        return {
            "ok": False,
            "reason": "fwrouter_metadata_mismatch",
            "metadata": metadata,
            "expected": {
                "resolved_selective_default": expected_selective_default,
                "final_match_rule": expected_final_match_rule,
                "transparent_final_match_rule": expected_transparent_final_match_rule,
            },
        }

    try:
        health = DEFAULT_MIHOMO_ADAPTER.health()
    except Exception as exc:
        return {"ok": False, "reason": "mihomo_health_failed", "error": str(exc)}

    runtime_state = str(getattr(health.runtime_state, "value", health.runtime_state))
    details = health.details if isinstance(health.details, dict) else {}
    selectors = details.get("selectors") if isinstance(details.get("selectors"), dict) else {}
    config = details.get("config") if isinstance(details.get("config"), dict) else {}
    contours = config.get("fwrouter_contours") if isinstance(config.get("fwrouter_contours"), dict) else {}
    transparent_vpn = contours.get("transparent_vpn") if isinstance(contours.get("transparent_vpn"), dict) else {}

    server_mode = str(routing_dict.get("server_mode") or "auto").strip().lower()
    expected_auto_server = str(routing_dict.get("active_auto_server_id") or "").strip()
    selector_ok = bool(
        selectors.get("vpn_global_exists")
        and selectors.get("vpn_global_has_vpn_auto")
        and str(selectors.get("vpn_global_now") or "").strip() == "vpn-auto"
    )
    if server_mode == "auto" and expected_auto_server:
        selector_ok = selector_ok and str(selectors.get("vpn_auto_now") or "").strip() == expected_auto_server
    else:
        selector_ok = False

    transparent_ok = bool(
        transparent_vpn.get("transparent_tcp_ready")
        and transparent_vpn.get("transparent_udp_ready")
        and transparent_vpn.get("transparent_tcp_listener_socket_present")
        and transparent_vpn.get("transparent_udp_listener_socket_present")
    )
    ok = runtime_state == "running" and selector_ok and transparent_ok
    return {
        "ok": ok,
        "reason": "active_mihomo_runtime_matches_routing" if ok else "active_mihomo_runtime_mismatch",
        "runtime_state": runtime_state,
        "selector_ok": selector_ok,
        "transparent_ok": transparent_ok,
        "metadata_ok": metadata_ok,
        "active_server_id": health.active_server_id,
        "vpn_auto_now": selectors.get("vpn_auto_now"),
        "vpn_global_now": selectors.get("vpn_global_now"),
    }


def _build_config_status_summary(
    *,
    base_path: str,
    candidate_path: str,
    base_rules_count: int | None = None,
    candidate_rules_count: int | None = None,
) -> dict[str, Any]:
    return {
        "base_path": base_path,
        "candidate_path": candidate_path,
        "base_exists": os.path.exists(base_path),
        "candidate_exists": os.path.exists(candidate_path),
        "base_updated_at": _iso8601_mtime(base_path),
        "candidate_updated_at": _iso8601_mtime(candidate_path),
        "base_rules_count": base_rules_count,
        "candidate_rules_count": candidate_rules_count,
    }


def promote_mihomo_candidate_config() -> dict[str, Any]:
    candidate_path = _resolved_candidate_config_path()
    base_path = _resolved_base_config_path()
    if not os.path.exists(candidate_path):
        result = {
            "ok": False,
            "promoted": False,
            "error_code": "MIHOMO_CANDIDATE_MISSING",
            "error_message": "Mihomo candidate config does not exist.",
        }
        write_technical_log(
            component="mihomo",
            event_type="mihomo_candidate_promote_failed",
            level="warning",
            message=result["error_message"],
            details=result,
        )
        return result

    os.makedirs(os.path.dirname(base_path), exist_ok=True)
    shutil.copyfile(candidate_path, base_path)

    result = {
        "ok": True,
        "promoted": True,
        "base_path": base_path,
        "candidate_path": candidate_path,
        "status": _build_config_status_summary(
            base_path=base_path,
            candidate_path=candidate_path,
        ),
    }
    write_technical_log(
        component="mihomo",
        event_type="mihomo_candidate_promoted",
        level="info",
        message="Mihomo candidate config promoted to active config.",
        details=result,
    )
    return result


def reconcile_mihomo_runtime(routing: Any = None, job_id: str = "manual") -> dict[str, Any]:
    routing_dict = routing if isinstance(routing, dict) else None
    candidate = write_mihomo_candidate_config(routing_dict)
    config_validation = validate_mihomo_candidate_config(routing_dict)
    candidate_summary = _summarize_candidate(candidate)
    candidate_path = str(candidate.get("candidate_path") or _resolved_candidate_config_path())
    base_path = _resolved_base_config_path()
    status_summary = _build_config_status_summary(
        base_path=base_path,
        candidate_path=candidate_path,
        candidate_rules_count=int(candidate_summary.get("rules_count") or 0),
    )

    if not config_validation.get("ok"):
        result = {
            "ok": False,
            "job_id": job_id,
            "candidate": candidate_summary,
            "config_validation": config_validation,
            "promoted": {
                "ok": False,
                "promoted": False,
                "reason": "validation_failed",
            },
            "container": {
                "ok": False,
                "action": "none",
                "reason": "validation_failed",
            },
            "reconcile_action": "none",
            "reconcile_reason": "validation_failed",
            "config": status_summary,
        }
        _write_mihomo_reconcile_logs(
            ok=False,
            event_type="mihomo_reconcile_failed",
            operational_level="warning",
            technical_level="warning",
            message="Mihomo reconcile failed during candidate validation.",
            details=result,
        )
        return result

    files_match = False
    if os.path.exists(base_path) and os.path.exists(candidate_path):
        try:
            files_match = filecmp.cmp(base_path, candidate_path, shallow=False)
        except OSError:
            files_match = False
    else:
        try:
            status = get_mihomo_config_status(include_config=True)
        except TypeError:
            status = get_mihomo_config_status()
        active_config = status.get("base_config") if isinstance(status, dict) else None
        candidate_config = status.get("candidate_config") if isinstance(status, dict) else None
        files_match = _configs_equal(active_config, candidate_config)
        status_summary = _summarize_config_status(status)

    if files_match:
        result = {
            "ok": True,
            "job_id": job_id,
            "candidate": candidate_summary,
            "config_validation": config_validation,
            "promoted": {
                "ok": True,
                "promoted": False,
                "reason": "unchanged_config",
            },
            "container": {
                "ok": True,
                "action": "none",
                "reason": "unchanged_config",
            },
            "reconcile_action": "none",
            "reconcile_reason": "unchanged_config",
            "state_consistency_ok": True,
            "config": status_summary,
        }
        _write_mihomo_reconcile_logs(
            ok=True,
            event_type="mihomo_reconcile_skipped",
            message="Mihomo reconcile skipped because active config already matches candidate.",
            details=result,
            operational_level="debug",
        )
        return result

    restart_action = "force_recreate"

    promoted = promote_mihomo_candidate_config()
    restarted = restart_mihomo_container(action=restart_action)
    result = {
        "ok": bool(promoted.get("ok")) and bool(restarted.get("ok")),
        "job_id": job_id,
        "candidate": candidate_summary,
        "config_validation": config_validation,
        "promoted": promoted,
        "container": restarted,
        "reconcile_action": restart_action,
        "reconcile_reason": "structural_change",
        "state_consistency_ok": True,
        "config": _build_config_status_summary(
            base_path=base_path,
            candidate_path=candidate_path,
            candidate_rules_count=int(candidate_summary.get("rules_count") or 0),
        ),
    }
    _write_mihomo_reconcile_logs(
        ok=bool(result["ok"]),
        event_type="mihomo_reconciled" if result["ok"] else "mihomo_reconcile_failed",
        operational_level="info" if result["ok"] else "warning",
        technical_level="info" if result["ok"] else "warning",
        message="Mihomo runtime reconciled." if result["ok"] else "Mihomo runtime reconcile failed after promote/restart.",
        details=result,
    )
    return result
