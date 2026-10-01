"""Shadowsocks 2022 SIP002 URI and Mihomo mapping integration."""

from __future__ import annotations

import base64
import binascii
from typing import Any
from urllib.parse import unquote, urlparse

from .base import ProtocolHookResult, ProtocolIntegration, ProtocolValidationIssue, capabilities


CIPHERS = {
    "2022-blake3-aes-128-gcm",
    "2022-blake3-aes-256-gcm",
    "2022-blake3-chacha20-poly1305",
}


def _matches(proxy: dict[str, Any]) -> bool:
    return str(proxy.get("type") or proxy.get("protocol") or "").strip().lower() in {"ss", "shadowsocks"} and str(proxy.get("cipher") or proxy.get("method") or "").lower() in CIPHERS


def _normalize(proxy: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(proxy)
    normalized["type"] = "ss"
    if "method" in normalized and "cipher" not in normalized:
        normalized["cipher"] = normalized.pop("method")
    return normalized


def _validate(proxy: dict[str, Any]) -> tuple[ProtocolValidationIssue, ...]:
    issues: list[ProtocolValidationIssue] = []
    if not isinstance(proxy.get("server"), str) or not str(proxy.get("server") or "").strip():
        issues.append(ProtocolValidationIssue("invalid_endpoint", "server"))
    port = proxy.get("port")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        issues.append(ProtocolValidationIssue("invalid_endpoint", "port"))
    cipher = str(proxy.get("cipher") or proxy.get("method") or "").lower()
    if cipher not in CIPHERS:
        issues.append(ProtocolValidationIssue("unsupported_cipher", "cipher"))
    password = proxy.get("password")
    if not isinstance(password, str) or not password:
        issues.append(ProtocolValidationIssue("missing_credential", "password"))
    elif cipher in CIPHERS:
        key_size = 16 if cipher == "2022-blake3-aes-128-gcm" else 32
        try:
            for component in password.split(":"):
                encoded = component + "=" * (-len(component) % 4)
                key = base64.b64decode(encoded, altchars=b"-_", validate=True)
                if len(key) != key_size:
                    raise ValueError("invalid key length")
        except (ValueError, binascii.Error):
            issues.append(ProtocolValidationIssue("invalid_credential", "password"))
    for field in ("tls", "security", "reality-opts", "network"):
        if field in proxy:
            issues.append(ProtocolValidationIssue("unsupported_option", field))
    plugin = proxy.get("plugin")
    if plugin:
        issues.append(ProtocolValidationIssue("unsupported_option", "plugin"))
    return tuple(issues)


def _decode_userinfo(value: str) -> tuple[str, str] | None:
    decoded = unquote(value)
    if ":" not in decoded:
        try:
            decoded = base64.urlsafe_b64decode(decoded + "=" * (-len(decoded) % 4)).decode("utf-8")
        except (ValueError, UnicodeDecodeError, binascii.Error):
            return None
    method, password = decoded.split(":", 1)
    return method.lower(), password


def _parse_uri(uri: str) -> ProtocolHookResult:
    parsed = urlparse(uri)
    if parsed.scheme.lower() != "ss":
        return ProtocolHookResult(False)
    try:
        port = parsed.port
    except ValueError:
        port = None
    if not parsed.hostname or not port or not parsed.username:
        # SIP002 also permits an entirely base64-encoded authority.
        try:
            raw = base64.urlsafe_b64decode(uri.partition("://")[2].split("#", 1)[0] + "=" * (-len(uri.partition("://")[2].split("#", 1)[0]) % 4)).decode("utf-8")
            decoded_uri = "ss://" + raw + ("#" + uri.split("#", 1)[1] if "#" in uri else "")
            parsed = urlparse(decoded_uri)
            port = parsed.port
        except (ValueError, UnicodeDecodeError, binascii.Error):
            return ProtocolHookResult(False)
    if not parsed.hostname or not port or not parsed.username:
        return ProtocolHookResult(False)
    credentials = _decode_userinfo(parsed.username + ((":" + parsed.password) if parsed.password else ""))
    if credentials is None:
        return ProtocolHookResult(True, issues=(ProtocolValidationIssue("invalid_credentials", "userinfo"),))
    method, password = credentials
    if method not in CIPHERS:
        return ProtocolHookResult(False)
    if parsed.query:
        return ProtocolHookResult(True, issues=(ProtocolValidationIssue("unsupported_parameter", "uri.query"),))
    return ProtocolHookResult(True, proxy={
        "name": unquote(parsed.fragment or "").strip() or parsed.hostname,
        "type": "shadowsocks", "server": parsed.hostname, "port": port,
        "cipher": method, "password": password,
    })


def _parse_xray(outbound: dict[str, Any]) -> ProtocolHookResult:
    if str(outbound.get("protocol") or "").lower() != "shadowsocks":
        return ProtocolHookResult(False)
    settings = outbound.get("settings") if isinstance(outbound.get("settings"), dict) else {}
    servers = settings.get("servers") if isinstance(settings.get("servers"), list) else []
    server = servers[0] if servers and isinstance(servers[0], dict) else {}
    cipher = str(server.get("method") or "").lower()
    if cipher not in CIPHERS:
        return ProtocolHookResult(False)
    stream = outbound.get("streamSettings") if isinstance(outbound.get("streamSettings"), dict) else {}
    if str(stream.get("security") or "none").lower() != "none":
        return ProtocolHookResult(True, issues=(ProtocolValidationIssue("unsupported_security", "streamSettings.security"),))
    if str(stream.get("network") or "tcp").lower() != "tcp":
        return ProtocolHookResult(True, issues=(ProtocolValidationIssue("unsupported_transport", "streamSettings.network"),))
    return ProtocolHookResult(True, proxy={
        "name": str(outbound.get("remarks") or outbound.get("name") or outbound.get("tag") or server.get("address") or "").strip(),
        "type": "ss", "server": server.get("address"), "port": server.get("port"),
        "cipher": cipher, "password": server.get("password"),
    })


INTEGRATION = ProtocolIntegration(
    capabilities=capabilities("ss", "2022", ("plain_uri_lines", "base64_subscription", "json_profile", "clash_yaml")),
    matcher=_matches, normalizer=_normalize, validator=_validate, parse_uri=_parse_uri,
    parse_xray_outbound=_parse_xray,
    project_mihomo=lambda proxy: ProtocolHookResult(True, _normalize(proxy)),
)
