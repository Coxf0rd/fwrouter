"""Trojan URI, Xray JSON, and Mihomo endpoint integration."""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qsl, unquote, urlparse

from .base import ProtocolHookResult, ProtocolIntegration, ProtocolValidationIssue, capabilities


_URI_FIELDS = {"security", "sni", "alpn", "allowInsecure", "type", "path", "host", "serviceName", "pbk", "sid", "fp"}


def _matches(proxy: dict[str, Any]) -> bool:
    return str(proxy.get("type") or proxy.get("protocol") or "").strip().lower() == "trojan"


def _normalize(proxy: dict[str, Any]) -> dict[str, Any]:
    result = dict(proxy)
    result.pop("security", None)
    result.pop("tls", None)
    return result


def _project(proxy: dict[str, Any]) -> ProtocolHookResult:
    security = str(proxy.get("security") or "").lower()
    tls = proxy.get("tls")
    if security and security not in {"tls", "reality"}:
        return ProtocolHookResult(True, issues=(ProtocolValidationIssue("unsupported_security", "security"),))
    if "tls" in proxy and tls is not True:
        return ProtocolHookResult(True, issues=(ProtocolValidationIssue("unsupported_security", "tls"),))
    if security == "reality" and not isinstance(proxy.get("reality-opts"), dict):
        return ProtocolHookResult(True, issues=(ProtocolValidationIssue("missing_security_options", "reality-opts"),))
    if security == "reality" and not proxy["reality-opts"].get("public-key"):
        return ProtocolHookResult(True, issues=(ProtocolValidationIssue("missing_security_options", "reality-opts.public-key"),))
    projected = dict(proxy)
    projected.pop("security", None)
    projected.pop("tls", None)
    return ProtocolHookResult(True, projected)


def _validate(proxy: dict[str, Any]) -> tuple[ProtocolValidationIssue, ...]:
    issues: list[ProtocolValidationIssue] = []
    if not isinstance(proxy.get("server"), str) or not str(proxy.get("server") or "").strip():
        issues.append(ProtocolValidationIssue("invalid_endpoint", "server"))
    port = proxy.get("port")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        issues.append(ProtocolValidationIssue("invalid_endpoint", "port"))
    if not isinstance(proxy.get("password"), str) or not proxy.get("password"):
        issues.append(ProtocolValidationIssue("missing_credential", "password"))
    if "skip-cert-verify" in proxy and not isinstance(proxy["skip-cert-verify"], bool):
        issues.append(ProtocolValidationIssue("invalid_field", "skip-cert-verify"))
    if "alpn" in proxy and (not isinstance(proxy["alpn"], list) or not all(isinstance(v, str) for v in proxy["alpn"])):
        issues.append(ProtocolValidationIssue("invalid_field", "alpn"))
    if "network" in proxy and proxy["network"] not in {"tcp", "ws", "grpc"}:
        issues.append(ProtocolValidationIssue("unsupported_transport", "network"))
    if "reality-opts" in proxy:
        opts = proxy["reality-opts"]
        if not isinstance(opts, dict):
            issues.append(ProtocolValidationIssue("invalid_security_options", "reality-opts"))
        else:
            for key in ("public-key", "short-id"):
                if key in opts and not isinstance(opts[key], str):
                    issues.append(ProtocolValidationIssue("invalid_security_value", f"reality-opts.{key}"))
    return tuple(issues)


def _parse_uri(uri: str) -> ProtocolHookResult:
    parsed = urlparse(uri)
    if parsed.scheme.lower() != "trojan":
        return ProtocolHookResult(False)
    try:
        port = parsed.port
    except ValueError:
        port = None
    if not parsed.hostname or not port or not parsed.username or parsed.password is not None:
        return ProtocolHookResult(True, issues=(ProtocolValidationIssue("invalid_endpoint", "uri"),))
    query: dict[str, str] = {}
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        if key in query:
            return ProtocolHookResult(True, issues=(ProtocolValidationIssue("duplicate_parameter", "uri.query"),))
        query[key] = value
    if set(query) - _URI_FIELDS:
        return ProtocolHookResult(True, issues=(ProtocolValidationIssue("unsupported_parameter", "uri.query"),))
    security = query.get("security", "tls").lower()
    if security not in {"tls", "reality"}:
        return ProtocolHookResult(True, issues=(ProtocolValidationIssue("unsupported_security", "security"),))
    network = query.get("type", "tcp").lower() or "tcp"
    if network not in {"tcp", "ws", "grpc"}:
        return ProtocolHookResult(True, issues=(ProtocolValidationIssue("unsupported_transport", "network"),))
    proxy: dict[str, Any] = {
        "name": unquote(parsed.fragment or "").strip() or parsed.hostname,
        "type": "trojan", "server": parsed.hostname, "port": port,
        "password": unquote(parsed.username),
    }
    if query.get("sni"):
        proxy["sni"] = query["sni"]
    if query.get("alpn"):
        proxy["alpn"] = [item for item in query["alpn"].split(",") if item]
    if "allowInsecure" in query:
        if query["allowInsecure"] not in {"0", "1"}:
            return ProtocolHookResult(True, issues=(ProtocolValidationIssue("invalid_field", "skip-cert-verify"),))
        proxy["skip-cert-verify"] = query["allowInsecure"] == "1"
    if network != "tcp":
        proxy["network"] = network
    if network == "ws":
        proxy["ws-opts"] = {key: value for key, value in {
            "path": query.get("path"), "headers": {"Host": query["host"]} if query.get("host") else None
        }.items() if value}
    elif network == "grpc":
        proxy["grpc-opts"] = {"grpc-service-name": query.get("serviceName", "")}
    if security == "reality":
        proxy["reality-opts"] = {key: value for key, value in {
            "public-key": query.get("pbk"), "short-id": query.get("sid")
        }.items() if value is not None}
        if query.get("fp"):
            proxy["client-fingerprint"] = query["fp"]
    return ProtocolHookResult(True, proxy=proxy)


def _parse_xray(outbound: dict[str, Any]) -> ProtocolHookResult:
    if str(outbound.get("protocol") or "").lower() != "trojan":
        return ProtocolHookResult(False)
    stream = outbound.get("streamSettings") if isinstance(outbound.get("streamSettings"), dict) else {}
    security = str(stream.get("security") or "").lower()
    if security not in {"tls", "reality"}:
        return ProtocolHookResult(True, issues=(ProtocolValidationIssue("unsupported_security", "streamSettings.security"),))
    settings = outbound.get("settings") if isinstance(outbound.get("settings"), dict) else {}
    servers = settings.get("servers") if isinstance(settings.get("servers"), list) else []
    server = servers[0] if servers and isinstance(servers[0], dict) else {}
    network = str(stream.get("network") or "tcp").lower()
    if network not in {"tcp", "ws", "grpc"}:
        return ProtocolHookResult(True, issues=(ProtocolValidationIssue("unsupported_transport", "streamSettings.network"),))
    proxy: dict[str, Any] = {
        "name": str(outbound.get("remarks") or outbound.get("name") or outbound.get("tag") or server.get("address") or "").strip(),
        "type": "trojan", "server": server.get("address"), "port": server.get("port"),
        "password": server.get("password"),
    }
    tls = stream.get("tlsSettings") if isinstance(stream.get("tlsSettings"), dict) else {}
    reality = stream.get("realitySettings") if isinstance(stream.get("realitySettings"), dict) else {}
    if security == "reality" and not reality:
        return ProtocolHookResult(True, issues=(ProtocolValidationIssue("missing_security_options", "realitySettings"),))
    if security == "tls" and reality:
        return ProtocolHookResult(True, issues=(ProtocolValidationIssue("unsupported_security", "realitySettings"),))
    if tls.get("serverName"):
        proxy["sni"] = tls["serverName"]
    if tls.get("allowInsecure") is not None:
        if not isinstance(tls["allowInsecure"], bool):
            return ProtocolHookResult(True, issues=(ProtocolValidationIssue("invalid_field", "tlsSettings.allowInsecure"),))
        proxy["skip-cert-verify"] = tls["allowInsecure"]
    alpn = tls.get("alpn")
    if isinstance(alpn, list):
        proxy["alpn"] = alpn
    if network in {"ws", "grpc"}:
        proxy["network"] = network
        transport = stream.get("wsSettings" if network == "ws" else "grpcSettings")
        if isinstance(transport, dict):
            if network == "ws":
                host = transport.get("headers", {}).get("Host") if isinstance(transport.get("headers"), dict) else None
                proxy["ws-opts"] = {key: value for key, value in {"path": transport.get("path"), "headers": {"Host": host} if host else None}.items() if value}
            else:
                proxy["grpc-opts"] = {"grpc-service-name": transport.get("serviceName", "")}
    if reality:
        proxy["reality-opts"] = {key: value for key, value in {
            "public-key": reality.get("publicKey"), "short-id": reality.get("shortId")
        }.items() if value is not None}
        proxy["client-fingerprint"] = reality.get("fingerprint")
    return ProtocolHookResult(True, proxy=proxy)


INTEGRATION = ProtocolIntegration(
    capabilities=capabilities("trojan", "tls_or_reality", ("plain_uri_lines", "base64_subscription", "json_profile", "clash_yaml")),
    matcher=_matches, normalizer=_normalize, validator=_validate,
    parse_uri=_parse_uri, parse_xray_outbound=_parse_xray,
    project_mihomo=_project,
)
