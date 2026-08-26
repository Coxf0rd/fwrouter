from __future__ import annotations

import ipaddress
from typing import Any

from fwrouter_api.core.config import get_settings
from fwrouter_api.core.network_defaults import (
    DEFAULT_LAN_INTERFACE_DENY_PREFIXES,
    DEFAULT_LOCAL_LAN_HOSTS,
    DEFAULT_PROTECTED_IPV4_NETWORKS,
    DEFAULT_PROTECTED_IPV6_NETWORKS,
    DEFAULT_RULES_EXTRA_PROTECTED_NETWORKS,
    DEFAULT_TRUSTED_CLIENT_IPV4_NETWORKS,
    DEFAULT_TRUSTED_CLIENT_IPV6_NETWORKS,
)


def _normalize_network_strings(
    values: list[str] | tuple[str, ...] | None,
    fallback: tuple[str, ...],
    *,
    version: int | None = None,
) -> tuple[str, ...]:
    normalized: list[str] = []
    seen: set[str] = set()
    candidates = values if values is not None else fallback
    for raw in candidates:
        try:
            network = ipaddress.ip_network(str(raw).strip(), strict=False)
        except ValueError:
            continue
        if version is not None and network.version != version:
            continue
        value = str(network)
        if value not in seen:
            normalized.append(value)
            seen.add(value)
    if normalized:
        return tuple(normalized)
    return fallback


def _normalize_string_list(
    values: list[str] | tuple[str, ...] | None,
    fallback: tuple[str, ...],
) -> tuple[str, ...]:
    normalized: list[str] = []
    seen: set[str] = set()
    candidates = values if values is not None else fallback
    for raw in candidates:
        value = str(raw).strip()
        if value and value not in seen:
            normalized.append(value)
            seen.add(value)
    return tuple(normalized) if normalized else fallback


def protected_ipv4_networks() -> tuple[str, ...]:
    return _normalize_network_strings(
        get_settings().protected_ipv4_networks,
        DEFAULT_PROTECTED_IPV4_NETWORKS,
        version=4,
    )


def protected_ipv6_networks() -> tuple[str, ...]:
    return _normalize_network_strings(
        get_settings().protected_ipv6_networks,
        DEFAULT_PROTECTED_IPV6_NETWORKS,
        version=6,
    )


def rules_extra_protected_networks() -> tuple[str, ...]:
    return _normalize_network_strings(
        get_settings().rules_extra_protected_networks,
        DEFAULT_RULES_EXTRA_PROTECTED_NETWORKS,
    )


def protected_rule_network_strings() -> tuple[str, ...]:
    values = [
        *rules_extra_protected_networks(),
        *protected_ipv4_networks(),
        *protected_ipv6_networks(),
    ]
    return _normalize_network_strings(
        tuple(values),
        (
            *DEFAULT_RULES_EXTRA_PROTECTED_NETWORKS,
            *DEFAULT_PROTECTED_IPV4_NETWORKS,
            *DEFAULT_PROTECTED_IPV6_NETWORKS,
        ),
    )


def protected_rule_ip_networks() -> tuple[ipaddress._BaseNetwork, ...]:
    return tuple(ipaddress.ip_network(value, strict=False) for value in protected_rule_network_strings())


def trusted_client_ipv4_networks() -> tuple[str, ...]:
    return _normalize_network_strings(
        get_settings().trusted_client_ipv4_networks,
        DEFAULT_TRUSTED_CLIENT_IPV4_NETWORKS,
        version=4,
    )


def trusted_client_ipv6_networks() -> tuple[str, ...]:
    return _normalize_network_strings(
        get_settings().trusted_client_ipv6_networks,
        DEFAULT_TRUSTED_CLIENT_IPV6_NETWORKS,
        version=6,
    )


def nft_network_set_literal(values: tuple[str, ...] | list[str]) -> str:
    normalized = _normalize_network_strings(tuple(values), DEFAULT_TRUSTED_CLIENT_IPV4_NETWORKS)
    return "{ " + ", ".join(normalized) + " }"


def trusted_client_ipv4_nft_set() -> str:
    return nft_network_set_literal(trusted_client_ipv4_networks())


def trusted_client_ipv6_nft_set() -> str:
    return nft_network_set_literal(trusted_client_ipv6_networks())


def lan_interface_allowlist() -> tuple[str, ...]:
    return _normalize_string_list(get_settings().lan_interface_allowlist, ())


def lan_interface_deny_prefixes() -> tuple[str, ...]:
    return _normalize_string_list(
        get_settings().lan_interface_deny_prefixes,
        DEFAULT_LAN_INTERFACE_DENY_PREFIXES,
    )


def lan_interface_allowed(ifname: str) -> bool:
    name = str(ifname or "").strip()
    if not name:
        return False
    allowlist = lan_interface_allowlist()
    if allowlist and name not in allowlist:
        return False
    return not any(name.startswith(prefix) for prefix in lan_interface_deny_prefixes())


def local_lan_hosts() -> dict[str, str]:
    configured = get_settings().local_lan_hosts
    if not isinstance(configured, dict):
        return dict(DEFAULT_LOCAL_LAN_HOSTS)
    normalized: dict[str, str] = {}
    for raw_host, raw_description in configured.items():
        hostname = str(raw_host).strip().lower().rstrip(".")
        if not hostname:
            continue
        normalized[hostname] = str(raw_description or "").strip()
    return normalized or dict(DEFAULT_LOCAL_LAN_HOSTS)


def network_contract_manifest() -> dict[str, Any]:
    return {
        "protected_ipv4_networks": list(protected_ipv4_networks()),
        "protected_ipv6_networks": list(protected_ipv6_networks()),
        "rules_extra_protected_networks": list(rules_extra_protected_networks()),
        "trusted_client_ipv4_networks": list(trusted_client_ipv4_networks()),
        "trusted_client_ipv6_networks": list(trusted_client_ipv6_networks()),
        "lan_interface_allowlist": list(lan_interface_allowlist()),
        "lan_interface_deny_prefixes": list(lan_interface_deny_prefixes()),
        "local_lan_hosts": local_lan_hosts(),
    }
