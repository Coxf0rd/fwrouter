from __future__ import annotations

import json
import ipaddress
import subprocess
from pathlib import Path
from typing import Any

from fwrouter_api.adapters.mihomo import DEFAULT_MIHOMO_ADAPTER, MihomoHealth, MihomoRuntimeState
from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import db_session
from fwrouter_api.services.live_probe_cache import get_live_probe_cache
from fwrouter_api.services.modules import get_module_state


DATAPLANE_PROFILE_NAME = "global_v1"
DATAPLANE_CAPABILITY_GLOBAL_POLICY_V1 = "global_policy_v1"
ENFORCEMENT_LEVEL_GLOBAL_POLICY_READY = "global_policy_ready"
ENFORCEMENT_LEVEL_GLOBAL_DIRECT_ONLY = "global_direct_only"
ENFORCEMENT_LEVEL_GLOBAL_DIRECT_ENFORCED = "global_direct_enforced"
ENFORCEMENT_LEVEL_GLOBAL_SELECTIVE_ENFORCED = "global_selective_enforced"
ENFORCEMENT_LEVEL_GLOBAL_VPN_ENFORCED = "global_vpn_enforced"

PROTECTED_IPV4_NETWORKS = (
    "127.0.0.0/8",
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "100.64.0.0/10",
    "169.254.0.0/16",
    "224.0.0.0/4",
)
PROTECTED_IPV6_NETWORKS = (
    "::1/128",
    "fc00::/7",
    "fe80::/10",
    "ff00::/8",
)
PROTECTED_SERVICE_DOMAINS = (
    "localhost",
    "tailscale.com",
    "dl.tailscale.com",
    "pkgs.tailscale.com",
    "vpn.minisk.ru",
)
ANDROID_CONNECTIVITY_DIRECT_DOMAINS = (
    "connectivitycheck.gstatic.com",
    "connectivitycheck.android.com",
    "clients3.google.com",
    "clients.l.google.com",
    "www.google.com",
    "www.gstatic.com",
)


def _is_protected_service_domain(value: str) -> bool:
    normalized = value.strip().lower().rstrip(".")
    if not normalized:
        return False
    return any(
        normalized == protected or normalized.endswith(f".{protected}")
        for protected in PROTECTED_SERVICE_DOMAINS
    )


def _is_android_connectivity_direct_domain(value: str) -> bool:
    normalized = value.strip().lower().rstrip(".")
    if not normalized:
        return False
    return any(
        normalized == direct_domain or normalized.endswith(f".{direct_domain}")
        for direct_domain in ANDROID_CONNECTIVITY_DIRECT_DOMAINS
    )

MISSING_SELECTIVE_DOMAIN_SUPPORT = "domain_selective_enforcement_not_implemented"
MISSING_MIHOMO_TPROXY = "mihomo_tproxy_port_not_configured"
MISSING_MIHOMO_TUN = "mihomo_tun_not_enabled"
MISSING_MIHOMO_GLOBAL_EGRESS = "mihomo_global_egress_binding_unknown"
MISSING_MIHOMO_CONTROLLER = "mihomo_controller_unreachable"
MISSING_MIHOMO_TRANSPARENT_CONTOUR = "mihomo_transparent_contour_not_ready"
MISSING_MIHOMO_EXPLICIT_PROXY_ISOLATION = "mihomo_explicit_proxy_isolation_not_ready"
MISSING_DNSMASQ_DOMAIN_SELECTIVE = "dnsmasq_domain_selective_contract_not_ready"
MISSING_EFFECTIVE_RULES = "effective_rules_artifact_missing"
MISSING_OWNED_TABLE = "fwrouter_owned_table_required_chains_missing"
MISSING_VPN_MODULE_ENABLED = "vpn_module_disabled"
MISSING_VPN_ROUTING_CONTRACT = "vpn_tproxy_contract_not_defined"
MISSING_VPN_VERIFY = "vpn_external_path_not_verified"
MISSING_ACTIVE_DATAPLANE_MODE_MISMATCH = "active_dataplane_mode_mismatch"
MISSING_VPN_TPROXY_HANDOFF = "vpn_tproxy_handoff_not_observed"


def _rules_list(effective_rules_artifact: dict[str, Any] | None) -> list[dict[str, Any]]:
    rules = ((effective_rules_artifact or {}).get("rules") or []) if isinstance(effective_rules_artifact, dict) else []
    return [rule for rule in rules if isinstance(rule, dict)]


def _selective_rules_summary(
    effective_rules_artifact: dict[str, Any] | None,
) -> dict[str, Any]:
    rules = _rules_list(effective_rules_artifact)
    domain_rules = 0
    vpn_rules = 0
    direct_rules = 0
    for rule in rules:
        kind = str(rule.get("kind") or "").lower()
        action = str(rule.get("action") or "").upper()
        value = str(rule.get("value") or "").strip().lower().rstrip(".")
        if kind in {"domain", "domain_suffix"}:
            if _is_protected_service_domain(value):
                continue
            domain_rules += 1
        if action == "VPN":
            vpn_rules += 1
        elif action == "DIRECT":
            direct_rules += 1

    selective_default = str((effective_rules_artifact or {}).get("selective_default") or "direct").lower()
    requires_vpn_runtime = vpn_rules > 0 or selective_default == "vpn"

    return {
        "rules_count": len(rules),
        "domain_rules_count": domain_rules,
        "vpn_rules_count": vpn_rules,
        "direct_rules_count": direct_rules,
        "selective_default": selective_default,
        "requires_vpn_runtime": requires_vpn_runtime,
        "ip_only_ready": bool(effective_rules_artifact is not None) and domain_rules == 0,
        "path_kind": "domain_aware" if domain_rules > 0 else "ip_only",
    }

def build_vpn_steering_contract(
    *,
    redir_port: int | None,
    tproxy_port: int | None,
) -> dict[str, Any] | None:
    if tproxy_port is None or tproxy_port <= 0:
        return None

    return {
        "mode": "tproxy",
        "redir_port": redir_port,
        "tproxy_port": tproxy_port,
        "full_vpn_redir_port": 5204,
        "full_vpn_tproxy_port": 5205,
        "fwmark_hex": "0x00000100",
        "fwmark_value": 256,
        "proxy_bypass_mark_hex": "0x00000200",
        "proxy_bypass_mark_value": 512,
        "routing_table_name": "fwrouter_vpn",
        "routing_table_id": 100,
        "ip_rule_priority": 100,
        "route_target": "local default dev lo",
        "selector_name": "vpn-global",
        "selector_fallback_target": "vpn-auto",
    }


def build_dataplane_profile(
    *,
    redir_port: int | None = None,
    tproxy_port: int | None = None,
    tun_enabled: bool = False,
    selective_path_kind: str = "ip_only",
    transparent_contour_ready: bool = False,
    transparent_tcp_ready: bool = False,
    transparent_udp_ready: bool = False,
    explicit_proxy_preserved: bool = True,
) -> dict[str, Any]:
    vpn_contract = build_vpn_steering_contract(redir_port=redir_port, tproxy_port=tproxy_port)
    return {
        "profile": DATAPLANE_PROFILE_NAME,
        "owned_table": "inet fwrouter_v2",
        "mihomo": {
            "mode": "egress",
            "controller_url": "http://127.0.0.1:5200",
            "mixed_port": 5201,
            "redir_port": redir_port,
            "tproxy_port": tproxy_port,
            "tun_enabled": tun_enabled,
            "contours": {
                "explicit_proxy_preserved": explicit_proxy_preserved,
                "transparent_contour_ready": transparent_contour_ready,
                "transparent_tcp_ready": transparent_tcp_ready,
                "transparent_udp_ready": transparent_udp_ready,
                "selective_path_kind": selective_path_kind,
            },
        },
        "vpn_routing_contract": vpn_contract,
        "supports": {
            "global_direct": True,
            "global_selective": selective_path_kind,
            "global_vpn": bool(vpn_contract),
            "scoped_egress": False,
        },
    }


def _read_effective_rules_artifact_uncached() -> dict[str, Any] | None:
    paths = get_settings().paths
    candidates = (
        paths.generated_dir / "rules" / "effective-rules.json",
        paths.state_dir / "last-good" / "rules" / "effective-rules.json",
    )
    for path in candidates:
        if not path.exists():
            continue
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if isinstance(loaded, dict):
            return loaded
    return None


def read_effective_rules_artifact() -> dict[str, Any] | None:
    return get_live_probe_cache(
        "dataplane_global.effective_rules_artifact",
        ttl_seconds=5.0,
        loader=_read_effective_rules_artifact_uncached,
    )


def _read_applied_manifest_uncached() -> dict[str, Any] | None:
    path = get_settings().paths.generated_dir / "dataplane" / "applied-manifest.json"
    if not path.exists():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return loaded if isinstance(loaded, dict) else None


def read_applied_manifest() -> dict[str, Any] | None:
    return get_live_probe_cache(
        "dataplane_global.applied_manifest",
        ttl_seconds=5.0,
        loader=_read_applied_manifest_uncached,
    )


def _bool_chain_map(required_chains: dict[str, Any] | None) -> dict[str, bool]:
    chain_map = required_chains or {}
    return {str(key): bool(value) for key, value in chain_map.items()}


def _mihomo_health() -> MihomoHealth:
    return get_live_probe_cache(
        "dataplane_global.mihomo_health",
        ttl_seconds=2.0,
        loader=DEFAULT_MIHOMO_ADAPTER.health,
    )


def _details_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _bool_or_false(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def _collapse_network_values(values: list[str], *, version: int) -> list[str]:
    networks: list[ipaddress._BaseNetwork] = []
    for value in values:
        try:
            network = ipaddress.ip_network(value, strict=False)
        except ValueError:
            continue
        if network.version == version:
            networks.append(network)
    return [str(network) for network in ipaddress.collapse_addresses(networks)]


def _load_custom_proxy_protected_networks() -> tuple[list[str], list[str]]:
    try:
        with db_session() as connection:
            rows = connection.execute(
                """
                SELECT host
                FROM server_custom_https_proxy
                WHERE host IS NOT NULL
                  AND trim(host) <> ''
                """
            ).fetchall()
    except Exception:
        return [], []

    protected_ipv4: list[str] = []
    protected_ipv6: list[str] = []

    for row in rows:
        host = str(row["host"] or "").strip()
        if not host:
            continue
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            continue
        network = str(ipaddress.ip_network(f"{address}/{address.max_prefixlen}", strict=False))
        if address.version == 4:
            protected_ipv4.append(network)
        else:
            protected_ipv6.append(network)

    return (
        _collapse_network_values(protected_ipv4, version=4),
        _collapse_network_values(protected_ipv6, version=6),
    )


def _discover_local_interface_protected_networks() -> tuple[list[str], list[str]]:
    try:
        completed = subprocess.run(
            ["ip", "-json", "address", "show"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return [], []

    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return [], []

    if not isinstance(payload, list):
        return [], []

    protected_ipv4: list[str] = []
    protected_ipv6: list[str] = []
    for link in payload:
        if not isinstance(link, dict):
            continue
        addr_info = link.get("addr_info")
        if not isinstance(addr_info, list):
            continue
        for item in addr_info:
            if not isinstance(item, dict):
                continue
            family = str(item.get("family") or "").lower()
            local = str(item.get("local") or "").strip()
            prefixlen = item.get("prefixlen")
            if not local or not isinstance(prefixlen, int):
                continue
            try:
                network = ipaddress.ip_network(f"{local}/{prefixlen}", strict=False)
            except ValueError:
                continue
            if family == "inet":
                protected_ipv4.append(str(network))
            elif family == "inet6":
                protected_ipv6.append(str(network))

    return (
        _collapse_network_values(protected_ipv4, version=4),
        _collapse_network_values(protected_ipv6, version=6),
    )


def build_global_preflight(
    *,
    routing: dict[str, Any] | None = None,
    check_details: dict[str, Any] | None = None,
    mihomo_health: MihomoHealth | None = None,
    effective_rules_artifact: dict[str, Any] | None = None,
    require_runtime_verify: bool = False,
) -> dict[str, Any]:
    normalized_routing = routing or {}
    mode = _routing_mode_from_manifest(normalized_routing)
    rules_artifact = effective_rules_artifact if effective_rules_artifact is not None else read_effective_rules_artifact()
    explicit_mihomo_health = mihomo_health is not None
    resolved_mihomo_health = mihomo_health or _mihomo_health()
    required_chains = _bool_chain_map((check_details or {}).get("required_chains"))
    mihomo_details = _details_dict(resolved_mihomo_health.details)
    mihomo_config = _details_dict(mihomo_details.get("config"))
    mihomo_selectors = _details_dict(mihomo_details.get("selectors"))
    contours = _details_dict(mihomo_config.get("fwrouter_contours"))
    explicit_proxy = _details_dict(contours.get("explicit_proxy"))
    transparent_vpn = _details_dict(contours.get("transparent_vpn"))
    domain_selective = _details_dict(contours.get("domain_selective"))

    redir_port = _int_or_none(
        mihomo_config.get("redir_port", mihomo_details.get("redir_port"))
    )
    if redir_port is None:
        redir_port = _int_or_none(
            transparent_vpn.get("redir_port")
        )

    tproxy_port = _int_or_none(
        mihomo_config.get("tproxy_port", mihomo_details.get("tproxy_port"))
    )
    if tproxy_port is None:
        tproxy_port = _int_or_none(
            transparent_vpn.get("tproxy_port")
        )
    tun_enabled = _bool_or_false(
        mihomo_config.get("tun_enabled", mihomo_details.get("tun_enabled"))
    )
    selective_rules = _selective_rules_summary(rules_artifact)

    transparent_runtime = _details_dict(mihomo_details.get("transparent_runtime"))
    transparent_tcp_listener_present = _bool_or_false(
        transparent_vpn.get(
            "transparent_tcp_listener_present",
            mihomo_config.get("transparent_tcp_listener_present"),
        )
    )
    transparent_udp_listener_present = _bool_or_false(
        transparent_vpn.get(
            "transparent_udp_listener_present",
            mihomo_config.get("transparent_udp_listener_present"),
        )
    )
    transparent_tcp_ready = _bool_or_false(transparent_vpn.get("transparent_tcp_ready"))
    transparent_udp_ready = _bool_or_false(transparent_vpn.get("transparent_udp_ready"))
    transparent_contour_ready = bool(transparent_vpn.get("ready"))
    transparent_contour_complete = transparent_tcp_ready and transparent_udp_ready
    transparent_contour_required = (
        mode in {"selective", "vpn"} or bool(selective_rules["requires_vpn_runtime"])
    )
    transparent_contour_invalid = transparent_contour_required and not transparent_contour_complete
    explicit_proxy_preserved = bool(
        explicit_proxy.get("preserved", True)
        and domain_selective.get("explicit_proxy_preserved", True)
        and transparent_vpn.get("isolated_from_explicit_proxy", True)
    )
    profile = build_dataplane_profile(
        redir_port=redir_port,
        tproxy_port=tproxy_port,
        tun_enabled=tun_enabled,
        selective_path_kind=str(selective_rules["path_kind"]),
        transparent_contour_ready=transparent_contour_complete,
        transparent_tcp_ready=transparent_tcp_ready,
        transparent_udp_ready=transparent_udp_ready,
        explicit_proxy_preserved=explicit_proxy_preserved,
    )
    vpn_contract = _details_dict(profile.get("vpn_routing_contract"))

    direct_missing: list[str] = []
    if required_chains and not all(required_chains.values()):
        direct_missing.append(MISSING_OWNED_TABLE)

    vpn_missing: list[str] = []
    vpn_module = get_module_state("vpn")
    if vpn_module and str(vpn_module.get("desired_state") or "") != "enabled":
        vpn_missing.append(MISSING_VPN_MODULE_ENABLED)

    if tproxy_port is None:
        vpn_missing.append(MISSING_MIHOMO_TPROXY)
    if transparent_contour_invalid:
        vpn_missing.append(MISSING_MIHOMO_TRANSPARENT_CONTOUR)
    if not explicit_proxy_preserved:
        vpn_missing.append(MISSING_MIHOMO_EXPLICIT_PROXY_ISOLATION)
    # In dumb VPN architecture, we prefer TProxy. TUN is optional if TProxy works.
    if not tun_enabled and not vpn_contract:
        vpn_missing.append(MISSING_MIHOMO_TUN)
    if not vpn_contract:
        vpn_missing.append(MISSING_VPN_ROUTING_CONTRACT)
    if require_runtime_verify and resolved_mihomo_health.runtime_state != MihomoRuntimeState.RUNNING:
        vpn_missing.append(MISSING_MIHOMO_CONTROLLER)
    vpn_global_exists = _bool_or_false(mihomo_selectors.get("vpn_global_exists"))
    vpn_global_targets_count = _int_or_none(mihomo_selectors.get("vpn_global_targets_count")) or 0
    vpn_global_has_vpn_auto = _bool_or_false(mihomo_selectors.get("vpn_global_has_vpn_auto"))
    vpn_global_now = mihomo_selectors.get("vpn_global_now")
    vpn_auto_now = mihomo_selectors.get("vpn_auto_now")
    active_server_id = resolved_mihomo_health.active_server_id
    vpn_target_reachable = False
    if vpn_global_exists and bool(active_server_id):
        vpn_target_reachable = True
    elif vpn_global_exists and bool(vpn_global_now) and vpn_global_now != "vpn-auto":
        vpn_target_reachable = True
    elif (
        vpn_global_exists
        and vpn_global_has_vpn_auto
        and vpn_global_targets_count > 0
    ):
        # We allow vpn_auto_now to be None (which means DIRECT in our adapter)
        # to avoid chicken-and-egg failures when Mihomo is empty.
        vpn_target_reachable = True
    if (
        not explicit_mihomo_health
        and not require_runtime_verify
        and tproxy_port is not None
        and resolved_mihomo_health.runtime_state == MihomoRuntimeState.RUNNING
    ):
        vpn_target_reachable = True
    if not vpn_target_reachable:
        vpn_missing.append(MISSING_MIHOMO_GLOBAL_EGRESS)
    vpn_external_path_verified = _bool_or_false((check_details or {}).get("vpn_external_path_verified"))
    transparent_path = _details_dict((check_details or {}).get("transparent_path"))
    transparent_flow_observed = _bool_or_false(transparent_path.get("transparent_flow_observed"))
    transparent_tcp_flow_observed = _bool_or_false(transparent_path.get("transparent_tcp_flow_observed"))
    transparent_udp_flow_observed = _bool_or_false(transparent_path.get("transparent_udp_flow_observed"))
    transparent_tcp_session_materialized = _bool_or_false(
        transparent_runtime.get("transparent_tcp_session_materialized")
    )
    transparent_udp_session_materialized = _bool_or_false(
        transparent_runtime.get("transparent_udp_session_materialized")
    )
    vpn_mark_packets = _int_or_none(transparent_path.get("vpn_mark_packets")) or 0
    vpn_mark_tcp_packets = _int_or_none(transparent_path.get("vpn_mark_tcp_packets")) or 0
    if require_runtime_verify and mode == "vpn" and not vpn_external_path_verified:
        vpn_missing.append(MISSING_VPN_VERIFY)
    if require_runtime_verify and vpn_mark_packets > 0 and not transparent_flow_observed:
        vpn_missing.append(MISSING_VPN_TPROXY_HANDOFF)
    if require_runtime_verify and vpn_mark_tcp_packets > 0 and not (
        transparent_tcp_flow_observed and transparent_tcp_session_materialized
    ):
        vpn_missing.append(MISSING_VPN_TPROXY_HANDOFF)

    selective_missing: list[str] = []
    if rules_artifact is None:
        selective_missing.append(MISSING_EFFECTIVE_RULES)
    from fwrouter_api.services.dnsmasq import inspect_dnsmasq_selective_status

    dnsmasq_selective_status = inspect_dnsmasq_selective_status()
    if (
        str(selective_rules.get("path_kind") or "") == "domain_aware"
        and not bool(dnsmasq_selective_status.get("ok"))
    ):
        selective_missing.append(MISSING_DNSMASQ_DOMAIN_SELECTIVE)
    if direct_missing:
        selective_missing.extend(direct_missing)
    selective_vpn_ready = (not bool(selective_rules["requires_vpn_runtime"])) or len(vpn_missing) == 0
    selective_degraded = bool(selective_rules["requires_vpn_runtime"]) and not selective_vpn_ready

    missing = sorted(set(direct_missing + selective_missing + vpn_missing))

    return {
        "ok": True,
        "profile": profile,
        "owned_table": profile["owned_table"],
        "nft_available": (check_details or {}).get("error_code") != "NFT_NOT_AVAILABLE",
        "owned_table_check": {
            "table_exists": bool((check_details or {}).get("table_exists", False)),
            "required_chains": required_chains,
        },
        "mihomo": {
            "runtime_state": resolved_mihomo_health.runtime_state.value,
            "message": resolved_mihomo_health.message,
            "details": resolved_mihomo_health.details,
        },
        "vpn_contour": vpn_contract,
        "vpn_external_path_verified": vpn_external_path_verified,
        "effective_rules_present": rules_artifact is not None,
        "protected_local_pools_loaded": True,
        "can_enforce_global_direct": len(direct_missing) == 0,
        "can_enforce_global_selective": len(selective_missing) == 0,
        "can_enforce_global_vpn": len(vpn_missing) == 0,
        "selective_vpn_ready": selective_vpn_ready,
        "selective_degraded": selective_degraded,
        "selective_degraded_missing": list(vpn_missing) if selective_degraded else [],
        "missing": missing,
        "missing_by_mode": {
            "direct": direct_missing,
            "selective": selective_missing,
            "vpn": vpn_missing,
        },
        "selective_rules": selective_rules,
        "mihomo_contours": {
            "explicit_proxy": explicit_proxy,
            "transparent_vpn": transparent_vpn,
            "domain_selective": domain_selective,
            "transparent_contour_ready": transparent_contour_ready,
            "transparent_contour_required": transparent_contour_required,
            "transparent_tcp_ready": transparent_tcp_ready,
            "transparent_udp_ready": transparent_udp_ready,
            "transparent_tcp_listener_present": transparent_tcp_listener_present,
            "transparent_udp_listener_present": transparent_udp_listener_present,
            "explicit_proxy_preserved": explicit_proxy_preserved,
        },
        "transparent_path": {
            **transparent_path,
            "transparent_flow_observed": transparent_flow_observed,
            "transparent_tcp_flow_observed": transparent_tcp_flow_observed,
            "transparent_udp_flow_observed": transparent_udp_flow_observed,
            "transparent_tcp_session_materialized": transparent_tcp_session_materialized,
            "transparent_udp_session_materialized": transparent_udp_session_materialized,
        },
        "dnsmasq_selective_status": dnsmasq_selective_status,
    }


def build_nft_rule_sets(effective_rules_artifact: dict[str, Any] | None) -> dict[str, list[str]]:
    direct_ipv4: list[str] = []
    direct_ipv6: list[str] = []
    vpn_ipv4: list[str] = []
    vpn_ipv6: list[str] = []
    direct_domains: list[str] = list(ANDROID_CONNECTIVITY_DIRECT_DOMAINS)
    vpn_domains: list[str] = []

    rules = ((effective_rules_artifact or {}).get("rules") or []) if isinstance(effective_rules_artifact, dict) else []
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        action = str(rule.get("action") or "").upper()
        kind = str(rule.get("kind") or "").lower()
        value = str(rule.get("value") or "").strip()
        if not value:
            continue

        if kind in {"domain", "domain_suffix"}:
            normalized_domain = value.lower().rstrip(".")
            if _is_protected_service_domain(normalized_domain):
                continue
            if _is_android_connectivity_direct_domain(normalized_domain):
                direct_domains.append(normalized_domain)
                continue
            if action == "DIRECT":
                direct_domains.append(normalized_domain)
            elif action == "VPN":
                vpn_domains.append(normalized_domain)
            continue

        if kind in {"ip", "ipv4", "ipv4_cidr"}:
            if action == "DIRECT":
                direct_ipv4.append(value)
            elif action == "VPN":
                vpn_ipv4.append(value)
            continue

        if kind in {"ipv6", "ipv6_cidr"}:
            if action == "DIRECT":
                direct_ipv6.append(value)
            elif action == "VPN":
                vpn_ipv6.append(value)
            continue

        if kind == "cidr":
            try:
                network = ipaddress.ip_network(value, strict=False)
            except ValueError:
                continue
            if network.version == 4:
                if action == "DIRECT":
                    direct_ipv4.append(str(network))
                elif action == "VPN":
                    vpn_ipv4.append(str(network))
            else:
                if action == "DIRECT":
                    direct_ipv6.append(str(network))
                elif action == "VPN":
                    vpn_ipv6.append(str(network))

    proxy_protected_ipv4, proxy_protected_ipv6 = _load_custom_proxy_protected_networks()
    explicit_protected_ipv4 = (
        effective_rules_artifact.get("protected_ipv4")
        if isinstance(effective_rules_artifact, dict)
        else None
    )
    explicit_protected_ipv6 = (
        effective_rules_artifact.get("protected_ipv6")
        if isinstance(effective_rules_artifact, dict)
        else None
    )
    if isinstance(explicit_protected_ipv4, list) and isinstance(explicit_protected_ipv6, list):
        local_protected_ipv4, local_protected_ipv6 = [], []
    else:
        local_protected_ipv4, local_protected_ipv6 = _discover_local_interface_protected_networks()

    return {
        "protected_ipv4": _collapse_network_values(
            [
                *PROTECTED_IPV4_NETWORKS,
                *proxy_protected_ipv4,
                *local_protected_ipv4,
                *(
                    [str(value) for value in explicit_protected_ipv4]
                    if isinstance(explicit_protected_ipv4, list)
                    else []
                ),
            ],
            version=4,
        ),
        "protected_ipv6": _collapse_network_values(
            [
                *PROTECTED_IPV6_NETWORKS,
                *proxy_protected_ipv6,
                *local_protected_ipv6,
                *(
                    [str(value) for value in explicit_protected_ipv6]
                    if isinstance(explicit_protected_ipv6, list)
                    else []
                ),
            ],
            version=6,
        ),
        "direct_ipv4": _collapse_network_values(direct_ipv4, version=4),
        "direct_ipv6": _collapse_network_values(direct_ipv6, version=6),
        "vpn_ipv4": _collapse_network_values(vpn_ipv4, version=4),
        "vpn_ipv6": _collapse_network_values(vpn_ipv6, version=6),
        "direct_domains": sorted(dict.fromkeys(direct_domains)),
        "vpn_domains": sorted(dict.fromkeys(vpn_domains)),
    }


def _routing_mode_from_manifest(routing: dict[str, Any] | None) -> str:
    state = routing or {}
    return str(state.get("applied_mode") or state.get("desired_mode") or "direct")


def build_applied_runtime_enforcement(
    *,
    routing: dict[str, Any] | None,
    preflight: dict[str, Any] | None,
    live_mode_probe: dict[str, Any] | None = None,
    mode_override: str | None = None,
) -> dict[str, Any]:
    resolved_preflight = preflight or build_global_preflight(routing=routing)
    mode = str(mode_override or _routing_mode_from_manifest(routing)).strip().lower()
    selective_default = str((routing or {}).get("selective_default") or "direct").lower()
    supported_modes = {
        "direct": bool(resolved_preflight["can_enforce_global_direct"]),
        "selective": bool(resolved_preflight["can_enforce_global_selective"]),
        "vpn": bool(resolved_preflight["can_enforce_global_vpn"]),
    }
    selective_degraded = bool(resolved_preflight.get("selective_degraded"))
    active_mode_matches_intent = True
    live_mode = None
    live_selective_default = None

    if isinstance(live_mode_probe, dict):
        live_mode = str(live_mode_probe.get("mode") or "unknown").lower()
        live_selective_default = str(
            live_mode_probe.get("selective_default") or "direct"
        ).lower()
        active_mode_matches_intent = (
            bool(live_mode_probe.get("ok"))
            and live_mode == mode
            and (
                mode != "selective"
                or (
                    live_selective_default == selective_default
                    or (selective_degraded and live_selective_default == "direct")
                )
            )
        )

    if not active_mode_matches_intent:
        return {
            "dataplane_capability": DATAPLANE_CAPABILITY_GLOBAL_POLICY_V1,
            "capability": DATAPLANE_CAPABILITY_GLOBAL_POLICY_V1,
            "enforcement_level": ENFORCEMENT_LEVEL_GLOBAL_DIRECT_ONLY,
            "traffic_enforcement_guaranteed": False,
            "supported_modes": supported_modes,
            "missing_runtime_requirements": [MISSING_ACTIVE_DATAPLANE_MODE_MISMATCH],
            "profile": resolved_preflight["profile"],
            "active_mode_matches_intent": False,
            "live_global_mode": live_mode,
            "live_selective_default": live_selective_default,
        }

    if mode == "direct" and supported_modes["direct"]:
        return {
            "dataplane_capability": DATAPLANE_CAPABILITY_GLOBAL_POLICY_V1,
            "capability": DATAPLANE_CAPABILITY_GLOBAL_POLICY_V1,
            "enforcement_level": ENFORCEMENT_LEVEL_GLOBAL_DIRECT_ENFORCED,
            "traffic_enforcement_guaranteed": True,
            "supported_modes": supported_modes,
            "missing_runtime_requirements": [],
            "profile": resolved_preflight["profile"],
            "active_mode_matches_intent": True,
            "live_global_mode": live_mode,
            "live_selective_default": live_selective_default,
        }

    if mode == "vpn" and supported_modes["vpn"]:
        return {
            "dataplane_capability": DATAPLANE_CAPABILITY_GLOBAL_POLICY_V1,
            "capability": DATAPLANE_CAPABILITY_GLOBAL_POLICY_V1,
            "enforcement_level": ENFORCEMENT_LEVEL_GLOBAL_VPN_ENFORCED,
            "traffic_enforcement_guaranteed": True,
            "supported_modes": supported_modes,
            "missing_runtime_requirements": [],
            "profile": resolved_preflight["profile"],
            "active_mode_matches_intent": True,
            "live_global_mode": live_mode,
            "live_selective_default": live_selective_default,
        }

    if mode == "selective" and supported_modes["selective"]:
        return {
            "dataplane_capability": DATAPLANE_CAPABILITY_GLOBAL_POLICY_V1,
            "capability": DATAPLANE_CAPABILITY_GLOBAL_POLICY_V1,
            "enforcement_level": ENFORCEMENT_LEVEL_GLOBAL_SELECTIVE_ENFORCED,
            "traffic_enforcement_guaranteed": True,
            "supported_modes": supported_modes,
            "missing_runtime_requirements": list(
                resolved_preflight.get("selective_degraded_missing", [])
            ),
            "profile": resolved_preflight["profile"],
            "active_mode_matches_intent": True,
            "live_global_mode": live_mode,
            "live_selective_default": live_selective_default,
            "selective_vpn_ready": bool(resolved_preflight.get("selective_vpn_ready")),
            "selective_degraded": bool(resolved_preflight.get("selective_degraded")),
        }

    return {
        "dataplane_capability": DATAPLANE_CAPABILITY_GLOBAL_POLICY_V1,
        "capability": DATAPLANE_CAPABILITY_GLOBAL_POLICY_V1,
        "enforcement_level": ENFORCEMENT_LEVEL_GLOBAL_DIRECT_ONLY,
        "traffic_enforcement_guaranteed": False,
        "supported_modes": supported_modes,
        "missing_runtime_requirements": list(resolved_preflight["missing_by_mode"].get(mode, resolved_preflight["missing"])),
        "profile": resolved_preflight["profile"],
        "active_mode_matches_intent": active_mode_matches_intent,
        "live_global_mode": live_mode,
        "live_selective_default": live_selective_default,
    }


def validate_global_mode_request(
    mode: str,
    *,
    routing: dict[str, Any] | None = None,
    preflight: dict[str, Any] | None = None,
) -> dict[str, Any]:
    resolved_preflight = preflight or build_global_preflight(routing=routing)
    normalized_mode = mode.strip().lower()

    if normalized_mode == "direct":
        if resolved_preflight["can_enforce_global_direct"]:
            return {"ok": True, "mode": normalized_mode, "preflight": resolved_preflight}
        return {
            "ok": False,
            "mode": normalized_mode,
            "stage": "preflight",
            "code": "GLOBAL_DIRECT_ENFORCEMENT_NOT_READY",
            "message": "Global direct enforcement prerequisites are not satisfied.",
            "missing": resolved_preflight["missing_by_mode"]["direct"],
            "preflight": resolved_preflight,
        }

    if normalized_mode == "vpn":
        if resolved_preflight["can_enforce_global_vpn"]:
            return {"ok": True, "mode": normalized_mode, "preflight": resolved_preflight}
        return {
            "ok": False,
            "mode": normalized_mode,
            "stage": "preflight",
            "code": "GLOBAL_VPN_ENFORCEMENT_NOT_READY",
            "message": "Global VPN enforcement is not ready because Mihomo egress steering requirements are unresolved.",
            "missing": resolved_preflight["missing_by_mode"]["vpn"],
            "preflight": resolved_preflight,
        }

    if normalized_mode == "selective":
        if resolved_preflight["can_enforce_global_selective"]:
            return {"ok": True, "mode": normalized_mode, "preflight": resolved_preflight}
        return {
            "ok": False,
            "mode": normalized_mode,
            "stage": "preflight",
            "code": "SELECTIVE_ENFORCEMENT_NOT_READY",
            "message": "Selective enforcement is not ready because the effective rules artifact or owned-table foundation is missing.",
            "missing": resolved_preflight["missing_by_mode"]["selective"],
            "preflight": resolved_preflight,
        }

    return {
        "ok": False,
        "mode": normalized_mode,
        "stage": "validate",
        "code": "GLOBAL_MODE_INVALID",
        "message": "Global mode must be one of: direct, selective, vpn.",
        "missing": [],
        "preflight": resolved_preflight,
    }
