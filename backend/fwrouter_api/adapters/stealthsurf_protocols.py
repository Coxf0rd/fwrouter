"""StealthSurf wire identifiers and material schemas, never Core protocol branches.

Provider variants are opaque profiles. Their actual returned transport/security
must pass the corresponding pure protocol adapter; aliases imply no downgrade.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from fwrouter_api.adapters.provider_base import ProviderError


@dataclass(frozen=True)
class ProviderProtocolProfile:
    key: str
    wire_id: str
    family: str


PROFILES = (
    ProviderProtocolProfile("hysteria2", "hysteria2", "hysteria2"),
    ProviderProtocolProfile("vless", "vless", "vless"),
    ProviderProtocolProfile("vless_variant_2410", "vless-2410", "vless"),
    ProviderProtocolProfile("trojan", "trojan", "trojan"),
    ProviderProtocolProfile("trojan_variant_2901", "trojan-2901", "trojan"),
    ProviderProtocolProfile("shadowsocks2022", "shadowsocks-2022", "ss"),
    ProviderProtocolProfile("wireguard", "wg", "wireguard"),
    ProviderProtocolProfile("amneziawg2", "amnezia-wg-2", "wireguard"),
)


def profile_for(key: str) -> ProviderProtocolProfile:
    for profile in PROFILES:
        if profile.key == key:
            return profile
    raise ProviderError("PROVIDER_PROTOCOL_UNSUPPORTED")


def wire_protocol(key: str) -> str:
    return profile_for(key).wire_id


def normalize_config(row: dict[str, Any]) -> dict[str, Any]:
    """Preserve private transient material, map only actual supplied identity."""
    result = dict(row)
    if "protocol" in row:
        found = next((p for p in PROFILES if p.wire_id == row["protocol"]), None)
        if found is None:
            raise ProviderError("PROVIDER_PROTOCOL_UNSUPPORTED")
        result["protocol"] = found.key
    return result


def supported_protocols() -> tuple[str, ...]:
    # Family registration is the adapter side of the documented, pinned
    # runtime intersection. Native generated tests are the release gate.
    from fwrouter_api.adapters.protocol_integration import PROTOCOL_INTEGRATIONS
    from fwrouter_api.adapters.protocol_runtime import MIHOMO_PROTOCOL_FAMILIES
    families = {i.capabilities.protocol for i in PROTOCOL_INTEGRATIONS
                if i.capabilities.mihomo_projection == "supported"}
    return tuple(p.key for p in PROFILES if p.family in families and p.family in MIHOMO_PROTOCOL_FAMILIES)


def parse_material(key: str, config: dict[str, Any]) -> Any:
    from fwrouter_api.adapters.subscription import parse_subscription_payload
    profile = profile_for(key)
    if config.get("protocol", key) != key:
        raise ProviderError("PROVIDER_PROTOCOL_MISMATCH")
    # Extended material is authoritative. Never fall back to a URI after an
    # invalid/unrepresentable extended profile (that would change semantics).
    field = next((f for f in ("xray_config", "awg_config", "connection_url")
                  if config.get(f) is not None and config.get(f) != ""), None)
    if field is None or not isinstance(config[field], str):
        raise ProviderError("PROVIDER_CONFIG_INCOMPLETE")
    if profile.family == "vless" and config[field].lstrip().startswith("vless://"):
        from urllib.parse import parse_qsl, urlparse
        allowed_query = {"security", "type", "network", "flow", "encryption", "sni", "fp", "pbk", "sid",
                         "serviceName", "grpc-service-name", "path", "host", "mode", "extra"}
        if any(k not in allowed_query for k, _ in parse_qsl(urlparse(config[field]).query, keep_blank_values=True)):
            raise ProviderError("PROVIDER_PROTOCOL_VALIDATION_FAILED")
    parsed = parse_subscription_payload(config[field])
    if not parsed.ok or len(parsed.servers) != 1:
        raise ProviderError("PROVIDER_PROTOCOL_VALIDATION_FAILED")
    server = parsed.servers[0]
    topology = server.raw.get("_fwrouter_topology") or {}
    endpoints = topology.get("endpoints") or [{"runtime": server.raw}]
    if len(endpoints) != 1 or topology.get("rules_count") or topology.get("balancers") or topology.get("unsupported"):
        raise ProviderError("PROVIDER_PROTOCOL_PROFILE_UNSUPPORTED")
    for endpoint in endpoints:
        proxy = endpoint.get("runtime") or {}
        if proxy.get("type") != profile.family:
            raise ProviderError("PROVIDER_PROTOCOL_MISMATCH")
        if profile.family == "wireguard":
            awg = proxy.get("amnezia-wg-option")
            if (key == "amneziawg2") != bool(awg):
                raise ProviderError("PROVIDER_PROTOCOL_MISMATCH")
            if awg and awg.get("version") != 2:
                raise ProviderError("PROVIDER_PROTOCOL_UNSUPPORTED")
        if profile.family == "ss" and not str(proxy.get("cipher", "")).startswith("2022-"):
            raise ProviderError("PROVIDER_PROTOCOL_MISMATCH")
    return parsed


def validate_change_preflight(key: str, current: dict[str, Any]) -> None:
    """Fail closed before PATCH on unknown profiles and retained extended state.

    Actual future material cannot be known before mutation. Extended/custom
    settings need a separately approved exact capability intersection; this
    initial flow does not silently discard or convert them.
    """
    if key not in supported_protocols():
        raise ProviderError("PROVIDER_PROTOCOL_UNSUPPORTED")
    extended = current.get("is_extended_settings_enabled")
    has_structured_material = bool(current.get("xray_config") or current.get("awg_config"))
    if extended or (has_structured_material and extended is not False):
        raise ProviderError("PROVIDER_PROTOCOL_EXTENDED_SETTINGS_UNSUPPORTED")
    if has_structured_material:
        # A confirmed standard JSON/INI config is protocol material, not
        # persistent extended intent. Validate it before replacing the profile.
        parse_material(str(current.get("protocol") or ""), current)
