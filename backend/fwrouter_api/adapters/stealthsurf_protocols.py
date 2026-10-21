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

_STEALTHSURF_SERVER_ENVELOPE_KEYS = frozenset({
    "custom_dns_url", "dns_preset", "is_block_bittorrent", "is_family_filter_enabled",
    "is_fingerprint_randomized", "is_grpc_enabled", "is_grpc_multi_enabled",
    "is_loopbacks_enabled", "is_pass_all_traffic_through_vpn", "is_reality_disabled",
    "is_sni_changed", "is_sni_disabled", "is_warp_enabled", "is_xhttp_enabled",
    "port", "public_key", "random_id", "xray_config",
})
_STEALTHSURF_VLESS_FEATURE_FLAGS = {
    "is_block_bittorrent": False,
    "is_family_filter_enabled": False,
    "is_fingerprint_randomized": False,
    "is_grpc_enabled": True,
    "is_grpc_multi_enabled": False,
    "is_loopbacks_enabled": False,
    "is_pass_all_traffic_through_vpn": False,
    "is_reality_disabled": False,
    "is_sni_changed": False,
    "is_sni_disabled": True,
    "is_warp_enabled": False,
    "is_xhttp_enabled": False,
}


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
    # This provider envelope is a server-side profile, not a client outbound.
    # Accept its separately returned URI only after proving the URI's endpoint,
    # VLESS identity, Reality key/short-id, and gRPC service match the server
    # configuration. All other extended material stays authoritative and
    # fail-closed; there is no generic URI fallback.
    if profile.family == "vless" and config.get("xray_config"):
        envelope = _stealthsurf_vless_server_envelope(config["xray_config"])
        if envelope is not None:
            if not isinstance(config.get("connection_url"), str):
                raise ProviderError("PROVIDER_PROTOCOL_VALIDATION_FAILED")
            _validate_stealthsurf_vless_uri(config["connection_url"], envelope)
            material = config["connection_url"]
        else:
            material = None
    else:
        material = None
    # Extended material is authoritative. Never fall back to a URI after an
    # invalid/unrepresentable extended profile (that would change semantics).
    field = next((f for f in ("xray_config", "awg_config", "connection_url")
                  if config.get(f) is not None and config.get(f) != ""), None)
    if material is None and (field is None or not isinstance(config[field], str)):
        raise ProviderError("PROVIDER_CONFIG_INCOMPLETE")
    if material is None:
        material = config[field]
    if profile.family == "vless" and material.lstrip().startswith("vless://"):
        from urllib.parse import parse_qsl, urlparse
        allowed_query = {"security", "type", "network", "flow", "encryption", "sni", "fp", "pbk", "sid", "udp",
                         "serviceName", "grpc-service-name", "path", "host", "mode", "extra", "spx", "seed"}
        if any(k not in allowed_query for k, _ in parse_qsl(urlparse(material).query, keep_blank_values=True)):
            raise ProviderError("PROVIDER_PROTOCOL_VALIDATION_FAILED")
    parsed = parse_subscription_payload(material)
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


def _stealthsurf_vless_server_envelope(raw: str) -> dict[str, Any] | None:
    """Recognize the observed provider server-only Xray envelope shape."""
    import json

    try:
        outer = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(outer, dict) or not {"public_key", "port", "xray_config"}.issubset(outer):
        return None
    def invalid() -> None:
        raise ProviderError("PROVIDER_PROTOCOL_VALIDATION_FAILED")

    # The wrapper flags describe provider-side configuration, not Core intent.
    # Only the exact observed gRPC + REALITY/no-SNI feature combination is
    # qualified here; a changed feature would need an explicit client mapping.
    if (set(outer) - _STEALTHSURF_SERVER_ENVELOPE_KEYS
        or any(outer.get(key) is not expected for key, expected in _STEALTHSURF_VLESS_FEATURE_FLAGS.items())
        or outer.get("custom_dns_url") not in (None, "") or outer.get("dns_preset") not in (None, "")):
        invalid()

    core = outer.get("xray_config")
    if not isinstance(core, dict) or set(core) - {"dns", "inbounds", "log", "outbounds", "policy", "routing"}:
        invalid()
    inbounds = core.get("inbounds")
    outbounds = core.get("outbounds")
    if not isinstance(inbounds, list) or len(inbounds) != 1 or not isinstance(inbounds[0], dict):
        invalid()
    if not isinstance(outbounds, list) or not outbounds or any(
        not isinstance(item, dict) or str(item.get("protocol") or "").lower() not in {"freedom", "blackhole"}
        for item in outbounds
    ):
        invalid()
    inbound = inbounds[0]
    settings = inbound.get("settings") if isinstance(inbound.get("settings"), dict) else {}
    clients = settings.get("clients")
    stream = inbound.get("streamSettings") if isinstance(inbound.get("streamSettings"), dict) else {}
    reality = stream.get("realitySettings") if isinstance(stream.get("realitySettings"), dict) else {}
    grpc = stream.get("grpcSettings") if isinstance(stream.get("grpcSettings"), dict) else {}
    if (set(inbound) - {"listen", "port", "protocol", "settings", "sniffing", "streamSettings", "tag"}
        or set(settings) - {"clients", "decryption"} or set(stream) - {"network", "security", "grpcSettings", "realitySettings"}
        or set(grpc) - {"authority", "multiMode", "serviceName"}
        or set(reality) - {"dest", "maxTimeDiff", "privateKey", "serverNames", "shortIds", "show"}):
        invalid()
    server_names = reality.get("serverNames")
    short_ids = reality.get("shortIds")
    if (str(inbound.get("protocol") or "").lower() != "vless" or not isinstance(clients, list) or len(clients) != 1
        or str(stream.get("network") or "").lower() != "grpc" or str(stream.get("security") or "").lower() != "reality"
        or not isinstance(short_ids, list) or not short_ids or any(
            not isinstance(value, str) or not value or len(value) > 16 or len(value) % 2
            or any(ch not in "0123456789abcdefABCDEF" for ch in value) for value in short_ids
        )
        # An empty sole serverName is the provider's explicit disabled-SNI
        # representation (`is_sni_disabled=true`); do not synthesize SNI.
        or not isinstance(server_names, list) or not server_names
        or any(not isinstance(value, str) for value in server_names)
        or any(not value.strip() for value in server_names) and server_names != [""]
        or (server_names == [""]) is not (outer.get("is_sni_disabled") is True)
        or not grpc.get("serviceName") or grpc.get("authority", "") != ""
        or not isinstance(grpc.get("multiMode", False), bool) or grpc.get("multiMode", False) is not outer.get("is_grpc_multi_enabled")
        or isinstance(outer.get("port"), bool) or not isinstance(outer.get("port"), int)
        or inbound.get("port") != outer.get("port") or settings.get("decryption") != "none"):
        invalid()
    client = clients[0]
    if (not isinstance(client, dict) or set(client) - {"id", "email", "seed"} or not client.get("id")
        or not isinstance(client.get("seed"), str) or len(client["seed"]) != 16
        or any(ch not in "0123456789abcdefABCDEF" for ch in client["seed"])):
        invalid()
    return {"outer": outer, "client": client, "reality": reality, "grpc": grpc}


def _validate_stealthsurf_vless_uri(uri: str, envelope: dict[str, Any]) -> None:
    from urllib.parse import parse_qsl, urlparse

    parsed = urlparse(uri)
    query_items = parse_qsl(parsed.query, keep_blank_values=True)
    query = dict(query_items)
    client = envelope["client"]
    outer = envelope["outer"]
    reality = envelope["reality"]
    grpc = envelope["grpc"]
    try:
        port = parsed.port
    except ValueError:
        port = None
    allowed_query = {"security", "type", "pbk", "sid", "serviceName", "grpc-service-name", "spx", "seed", "fp"}
    service_names_match = ("serviceName" not in query or "grpc-service-name" not in query
                           or query["serviceName"] == query["grpc-service-name"])
    valid = (
        len(query_items) == len(query) and not (set(query) - allowed_query) and service_names_match
        and parsed.scheme.lower() == "vless" and parsed.hostname and parsed.username == str(client.get("id"))
        and port == outer.get("port") and query.get("security") == "reality"
        and query.get("type") == "grpc" and query.get("pbk") == outer.get("public_key")
        and query.get("sid") in reality.get("shortIds", [])
        and query.get("seed") == client.get("seed") and query.get("spx") == "/"
        and (query.get("serviceName") or query.get("grpc-service-name")) == grpc.get("serviceName")
        and not query.get("flow") and not query.get("encryption")
    )
    if not valid:
        raise ProviderError("PROVIDER_PROTOCOL_VALIDATION_FAILED")


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
