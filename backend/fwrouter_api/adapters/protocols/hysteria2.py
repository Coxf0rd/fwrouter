"""Hysteria2 endpoint import and Mihomo projection."""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qsl, unquote, urlparse

from .base import ProtocolHookResult, ProtocolIntegration, ProtocolValidationIssue, capabilities


def _matches(proxy: dict[str, Any]) -> bool:
    return str(proxy.get("type") or proxy.get("protocol") or "").strip().lower() in {"hysteria2", "hy2"}


def _validate(proxy: dict[str, Any]) -> tuple[ProtocolValidationIssue, ...]:
    issues: list[ProtocolValidationIssue] = []
    if not isinstance(proxy.get("server"), str) or not str(proxy.get("server") or "").strip():
        issues.append(ProtocolValidationIssue("invalid_endpoint", "server"))
    port = proxy.get("port")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        issues.append(ProtocolValidationIssue("invalid_endpoint", "port"))
    if not isinstance(proxy.get("password"), str) or not proxy.get("password"):
        issues.append(ProtocolValidationIssue("missing_credential", "password"))
    for field in ("sni", "obfs", "obfs-password", "fingerprint"):
        if field in proxy and not isinstance(proxy[field], str):
            issues.append(ProtocolValidationIssue("invalid_field", field))
    if proxy.get("obfs") and proxy.get("obfs") != "salamander":
        issues.append(ProtocolValidationIssue("unsupported_value", "obfs"))
    if "skip-cert-verify" in proxy and not isinstance(proxy["skip-cert-verify"], bool):
        issues.append(ProtocolValidationIssue("invalid_field", "skip-cert-verify"))
    return tuple(issues)


def _parse_uri(uri: str) -> ProtocolHookResult:
    parsed = urlparse(uri)
    if parsed.scheme.lower() not in {"hysteria2", "hy2"}:
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
    allowed = {"sni", "insecure", "obfs", "obfs-password", "pinSHA256"}
    if any(key not in allowed for key in query):
        return ProtocolHookResult(True, issues=(ProtocolValidationIssue("unsupported_parameter", "uri.query"),))
    proxy: dict[str, Any] = {
        "name": unquote(parsed.fragment or "").strip() or parsed.hostname,
        "type": "hysteria2", "server": parsed.hostname, "port": port,
        "password": unquote(parsed.username),
    }
    if query.get("sni"):
        proxy["sni"] = query["sni"]
    if "insecure" in query:
        if query["insecure"] not in {"0", "1"}:
            return ProtocolHookResult(True, issues=(ProtocolValidationIssue("invalid_field", "skip-cert-verify"),))
        proxy["skip-cert-verify"] = query["insecure"] == "1"
    if query.get("obfs"):
        proxy["obfs"] = query["obfs"]
    if "obfs-password" in query:
        proxy["obfs-password"] = query["obfs-password"]
    if query.get("pinSHA256"):
        proxy["fingerprint"] = query["pinSHA256"]
    return ProtocolHookResult(True, proxy=proxy)


def _project(proxy: dict[str, Any]) -> ProtocolHookResult:
    projected = dict(proxy)
    if str(projected.get("type") or "").lower() == "hy2":
        projected["type"] = "hysteria2"
    return ProtocolHookResult(True, proxy=projected)


INTEGRATION = ProtocolIntegration(
    capabilities=capabilities("hysteria2", "tls", ("plain_uri_lines", "base64_subscription", "clash_yaml"), transport="Mihomo v1.19.31 native validation required"),
    matcher=_matches, normalizer=dict, validator=_validate, parse_uri=_parse_uri, project_mihomo=_project,
)
