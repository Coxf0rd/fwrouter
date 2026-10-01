"""VLESS + REALITY URI/Xray/Mihomo endpoint integration."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qsl, unquote, urlparse

from .base import ProtocolHookResult, ProtocolIntegration, ProtocolIntegrationCapabilities, ProtocolValidationIssue


def _matches(proxy: dict[str, Any]) -> bool:
    protocol = str(proxy.get("type") or proxy.get("protocol") or "").strip().lower()
    return protocol == "vless"


def _normalize(proxy: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(proxy)
    options = proxy.get("reality-opts")
    if isinstance(options, dict):
        normalized["reality-opts"] = dict(options)
        if normalized["reality-opts"].get("short-id", "present") is None:
            normalized["reality-opts"].pop("short-id", None)
    return normalized


def _project(proxy: dict[str, Any]) -> ProtocolHookResult:
    security = str(proxy.get("security") or "").lower()
    if security not in {"", "none", "tls", "reality"}:
        return ProtocolHookResult(True, issues=(ProtocolValidationIssue("unsupported_security", "security"),))
    projected = dict(proxy)
    if security in {"tls", "reality"}:
        projected["tls"] = True
    elif security == "none":
        projected["tls"] = False
    return ProtocolHookResult(True, proxy=projected)


def _validate(proxy: dict[str, Any]) -> tuple[ProtocolValidationIssue, ...]:
    issues: list[ProtocolValidationIssue] = []
    network = str(proxy.get("network") or "tcp").lower()
    if network not in {"tcp", "ws", "http", "h2", "grpc", "xhttp"}:
        issues.append(ProtocolValidationIssue("unsupported_transport", "network"))
    if "server" in proxy and (not isinstance(proxy.get("server"), str) or not proxy["server"].strip()):
        issues.append(ProtocolValidationIssue("invalid_endpoint", "server"))
    port = proxy.get("port")
    if port is not None and (isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535):
        issues.append(ProtocolValidationIssue("invalid_endpoint", "port"))
    if "uuid" in proxy and (not isinstance(proxy.get("uuid"), str) or not proxy["uuid"].strip()):
        issues.append(ProtocolValidationIssue("missing_credential", "uuid"))
    security = str(proxy.get("security") or "").lower()
    if security not in {"", "none", "tls", "reality"}:
        issues.append(ProtocolValidationIssue("unsupported_security", "security"))
    if "reality-opts" not in proxy:
        return tuple(issues)
    options = proxy["reality-opts"]
    if not isinstance(options, dict):
        issues.append(ProtocolValidationIssue("invalid_security_options", "reality-opts"))
    else:
        value = options.get("short-id", "present")
        if value != "present" and (not isinstance(value, str) or (value and (len(value) > 16 or len(value) % 2 or re.fullmatch(r"[0-9a-fA-F]+", value) is None))):
            issues.append(ProtocolValidationIssue("invalid_security_value", "reality-opts.short-id"))
        if security and security != "reality":
            issues.append(ProtocolValidationIssue("unsupported_security", "reality-opts"))
    return tuple(issues)


def _parse_uri(uri: str) -> ProtocolHookResult:
    parsed = urlparse(uri)
    if parsed.scheme.lower() != "vless":
        return ProtocolHookResult(handled=False)
    query: dict[str, str] = {}
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        if key in query:
            return ProtocolHookResult(True, issues=(ProtocolValidationIssue("duplicate_parameter", "uri.query"),))
        query[key] = value
    security = str(query.get("security") or "").strip().lower()
    if security not in {"", "none", "tls", "reality"}:
        return ProtocolHookResult(True, issues=(ProtocolValidationIssue("unsupported_security", "security"),))
    try:
        port = parsed.port
    except ValueError:
        port = None
    if not parsed.hostname or not port or not parsed.username:
        return ProtocolHookResult(True, issues=(ProtocolValidationIssue("invalid_endpoint", "uri"),))
    network = str(query.get("type") or query.get("network") or "tcp").strip() or "tcp"
    if network not in {"tcp", "ws", "http", "h2", "grpc", "xhttp"}:
        return ProtocolHookResult(True, issues=(ProtocolValidationIssue("unsupported_transport", "network"),))
    proxy: dict[str, Any] = {
        "name": unquote(parsed.fragment or "").strip() or parsed.hostname,
        "type": "vless", "server": parsed.hostname, "port": port,
        "uuid": unquote(parsed.username or ""), "network": network,
    }
    if security:
        proxy["security"] = security
    if security == "reality" or query.get("pbk") or query.get("sid"):
        proxy["reality-opts"] = {key: value for key, value in {
            "public-key": query.get("pbk"), "short-id": query.get("sid")
        }.items() if value is not None}
    for source, target in (("flow", "flow"), ("encryption", "encryption"), ("sni", "servername"), ("fp", "client-fingerprint")):
        if query.get(source):
            proxy[target] = query[source]
    if network == "grpc":
        proxy["grpc-opts"] = {"grpc-service-name": query.get("serviceName") or query.get("grpc-service-name") or ""}
    elif network == "ws":
        proxy["ws-opts"] = {key: value for key, value in {
            "path": query.get("path"), "headers": {"Host": query["host"]} if query.get("host") else None
        }.items() if value}
    elif network == "xhttp":
        proxy["xhttp-opts"] = {key: value for key, value in {
            "path": query.get("path"), "mode": query.get("mode"), "host": query.get("host"), "extra": query.get("extra")
        }.items() if value}
    elif network == "http":
        proxy["http-opts"] = {key: value for key, value in {
            "path": [query.get("path")] if query.get("path") else None,
            "headers": {"Host": [query["host"]]} if query.get("host") else None,
        }.items() if value}
    elif network == "h2":
        proxy["h2-opts"] = {key: value for key, value in {
            "path": query.get("path"), "host": [query["host"]] if query.get("host") else None,
        }.items() if value}
    proxy["_fwrouter_uri_query"] = query
    return ProtocolHookResult(True, proxy=proxy)


def _parse_xray(outbound: dict[str, Any]) -> ProtocolHookResult:
    if str(outbound.get("protocol") or "").lower() != "vless":
        return ProtocolHookResult(False)
    stream = outbound.get("streamSettings") if isinstance(outbound.get("streamSettings"), dict) else {}
    reality = stream.get("realitySettings") if isinstance(stream.get("realitySettings"), dict) else {}
    security = str(stream.get("security") or "").lower()
    if security not in {"", "none", "tls", "reality"}:
        return ProtocolHookResult(True, issues=(ProtocolValidationIssue("unsupported_security", "streamSettings.security"),))
    if security in {"tls", "none"} and reality:
        return ProtocolHookResult(True, issues=(ProtocolValidationIssue("unsupported_security", "realitySettings"),))
    tls_settings = stream.get("tlsSettings")
    if tls_settings and security != "tls":
        return ProtocolHookResult(True, issues=(ProtocolValidationIssue("unsupported_security", "tlsSettings"),))
    settings = outbound.get("settings") if isinstance(outbound.get("settings"), dict) else {}
    vnext = settings.get("vnext") if isinstance(settings.get("vnext"), list) else []
    target = vnext[0] if vnext and isinstance(vnext[0], dict) else {}
    users = target.get("users") if isinstance(target.get("users"), list) else []
    user = users[0] if users and isinstance(users[0], dict) else {}
    network = str(stream.get("network") or "tcp")
    proxy: dict[str, Any] = {
        "name": str(outbound.get("remarks") or outbound.get("name") or outbound.get("tag") or target.get("address") or "").strip(),
        "type": "vless", "server": target.get("address"), "port": target.get("port"),
        "uuid": user.get("id") or user.get("uuid"), "network": network,
        "servername": reality.get("serverName"),
        "client-fingerprint": reality.get("fingerprint"),
        "_fwrouter_json_tag": outbound.get("tag"),
    }
    if reality:
        proxy["reality-opts"] = {key: value for key, value in {
            "public-key": reality.get("publicKey"), "short-id": reality.get("shortId")
        }.items() if value is not None}
    if security:
        proxy["security"] = security
        proxy["tls"] = security in {"tls", "reality"}
    tls = tls_settings if isinstance(tls_settings, dict) else {}
    if tls:
        proxy["servername"] = tls.get("serverName") or proxy.get("servername")
        if tls.get("allowInsecure") is not None:
            if not isinstance(tls["allowInsecure"], bool):
                return ProtocolHookResult(True, issues=(ProtocolValidationIssue("invalid_field", "tlsSettings.allowInsecure"),))
            proxy["skip-cert-verify"] = tls["allowInsecure"]
        if isinstance(tls.get("alpn"), list):
            proxy["alpn"] = tls["alpn"]
    if user.get("flow"):
        proxy["flow"] = user["flow"]
    if user.get("encryption"):
        proxy["encryption"] = user["encryption"]
    if network == "grpc" and isinstance(stream.get("grpcSettings"), dict):
        proxy["grpc-opts"] = stream["grpcSettings"]
    elif network == "ws" and isinstance(stream.get("wsSettings"), dict):
        proxy["ws-opts"] = stream["wsSettings"]
    elif network == "http" and isinstance(stream.get("httpSettings"), dict):
        proxy["http-opts"] = stream["httpSettings"]
    elif network == "h2" and isinstance(stream.get("httpSettings"), dict):
        proxy["h2-opts"] = stream["httpSettings"]
    if network == "xhttp" and isinstance(stream.get("xhttpSettings"), dict):
        proxy["xhttp-opts"] = stream["xhttpSettings"]
    return ProtocolHookResult(True, proxy=proxy)


INTEGRATION = ProtocolIntegration(
    capabilities=ProtocolIntegrationCapabilities(
        protocol="vless", security="tls_or_reality",
        source_import_formats=("plain_uri_lines", "base64_subscription", "json_profile", "clash_yaml"),
        mihomo_projection="supported", mihomo_native_validation_required=True,
        native_xray_egress_projection="not_implemented",
        public_profile_export_formats=("raw_vless", "base64_vless", "clash_mihomo"),
        public_profile_export_scope="FWRouter inbound client profile; independent of imported provider endpoints",
        health_latency="canonical_after_apply_via_active_runtime_adapter",
        transport_support="per-entry projection must pass pinned Mihomo validation",
    ),
    matcher=_matches, normalizer=_normalize, validator=_validate, parse_uri=_parse_uri,
    parse_xray_outbound=_parse_xray, project_mihomo=_project,
)
