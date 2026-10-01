"""Small endpoint protocol contracts used by subscription importers.

This metadata describes endpoint parsing/projection support. Runtime operations
such as health and latency remain owned by the active runtime adapter.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Callable
from urllib.parse import parse_qsl, unquote, urlparse


@dataclass(frozen=True)
class ProtocolIntegrationCapabilities:
    protocol: str
    security: str
    source_import_formats: tuple[str, ...]
    mihomo_projection: str
    mihomo_native_validation_required: bool
    native_xray_egress_projection: str
    public_profile_export_formats: tuple[str, ...]
    public_profile_export_scope: str
    health_latency: str
    transport_support: str


@dataclass(frozen=True)
class ProtocolNormalizationResult:
    proxy: dict[str, Any]
    issues: tuple["ProtocolValidationIssue", ...] = ()
    handled: bool = False


@dataclass(frozen=True)
class ProtocolValidationIssue:
    code: str
    field: str


@dataclass(frozen=True)
class ProtocolHookResult:
    """Sanitized result from a format parser or projection hook.

    `handled=False` lets the generic dispatcher preserve an existing parser
    path. Errors contain stable codes/field names only, never source values.
    """

    handled: bool
    proxy: dict[str, Any] | None = None
    issues: tuple[ProtocolValidationIssue, ...] = ()


@dataclass(frozen=True)
class ProtocolIntegration:
    capabilities: ProtocolIntegrationCapabilities
    matcher: Callable[[dict[str, Any]], bool]
    normalizer: Callable[[dict[str, Any]], dict[str, Any]]
    validator: Callable[[dict[str, Any]], tuple[ProtocolValidationIssue, ...]]
    parse_uri: Callable[[str], ProtocolHookResult] | None = None
    parse_xray_outbound: Callable[[dict[str, Any]], ProtocolHookResult] | None = None
    project_mihomo: Callable[[dict[str, Any]], ProtocolHookResult] | None = None

    def matches(self, proxy: dict[str, Any]) -> bool:
        return self.matcher(proxy)

    def normalize(self, proxy: dict[str, Any]) -> ProtocolNormalizationResult:
        projected = dict(proxy)
        if self.project_mihomo is not None:
            projection = self.project_mihomo(projected)
            if projection.issues:
                return ProtocolNormalizationResult(proxy=projected, issues=projection.issues, handled=True)
            if not projection.handled or projection.proxy is None:
                issue = ProtocolValidationIssue("projection_unavailable", "mihomo_projection")
                return ProtocolNormalizationResult(proxy=projected, issues=(issue,), handled=True)
            projected = projection.proxy
        normalized = self.normalizer(projected)
        return ProtocolNormalizationResult(proxy=normalized, issues=self.validator(normalized), handled=True)


def _valid_short_id(value: str) -> bool:
    return value == "" or (
        len(value) <= 16
        and len(value) % 2 == 0
        and re.fullmatch(r"[0-9a-fA-F]*", value) is not None
    )


def _matches_vless_reality(proxy: dict[str, Any]) -> bool:
    protocol = str(proxy.get("type") or proxy.get("protocol") or "").strip().lower()
    security = str(proxy.get("security") or "").strip().lower()
    return protocol == "vless" and (security == "reality" or "reality-opts" in proxy)


def _normalize_vless_reality(proxy: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(proxy)
    options = proxy.get("reality-opts")
    if isinstance(options, dict):
        normalized["reality-opts"] = dict(options)
        if "short-id" in normalized["reality-opts"] and normalized["reality-opts"]["short-id"] is None:
            # A source null is distinct from the literal string "null".
            # Mihomo's optional field is omitted when there is no value.
            normalized["reality-opts"].pop("short-id", None)
    return normalized


def _validate_vless_reality(proxy: dict[str, Any]) -> tuple[ProtocolValidationIssue, ...]:
    if "reality-opts" not in proxy:
        return ()
    options = proxy["reality-opts"]
    if not isinstance(options, dict):
        return (ProtocolValidationIssue("invalid_security_options", "reality-opts"),)
    if "short-id" not in options:
        return ()
    short_id = options["short-id"]
    if not isinstance(short_id, str) or not _valid_short_id(short_id):
        return (ProtocolValidationIssue("invalid_security_value", "reality-opts.short-id"),)
    return ()


def _parse_vless_reality_uri(uri: str) -> ProtocolHookResult:
    parsed = urlparse(uri)
    if parsed.scheme.lower() != "vless":
        return ProtocolHookResult(handled=False)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    security = str(query.get("security") or "").strip().lower()
    if security != "reality" and not (query.get("pbk") or query.get("sid")):
        return ProtocolHookResult(handled=False)
    try:
        port = parsed.port
    except ValueError:
        port = None
    if not parsed.hostname or not port:
        return ProtocolHookResult(
            handled=True,
            issues=(ProtocolValidationIssue("invalid_endpoint", "uri"),),
        )
    name = unquote(parsed.fragment or "").strip() or parsed.hostname
    network = str(query.get("type") or query.get("network") or "tcp").strip() or "tcp"
    proxy: dict[str, Any] = {
        "name": name,
        "type": "vless",
        "server": parsed.hostname,
        "port": port,
        "uuid": unquote(parsed.username or ""),
        "network": network,
        "security": security or "reality",
        "tls": True,
        "reality-opts": {
            key: value
            for key, value in {
                "public-key": query.get("pbk"),
                "short-id": query.get("sid"),
            }.items()
            if value is not None
        },
    }
    if query.get("flow"):
        proxy["flow"] = query["flow"]
    if query.get("encryption"):
        proxy["encryption"] = query["encryption"]
    if query.get("sni"):
        proxy["servername"] = query["sni"]
    if query.get("fp"):
        proxy["client-fingerprint"] = query["fp"]
    if network == "grpc":
        proxy["grpc-opts"] = {
            "grpc-service-name": query.get("serviceName") or query.get("grpc-service-name") or "",
        }
    elif network == "ws":
        proxy["ws-opts"] = {
            key: value
            for key, value in {
                "path": query.get("path"),
                "headers": {"Host": query.get("host")} if query.get("host") else None,
            }.items()
            if value
        }
    elif network == "xhttp":
        proxy["xhttp-opts"] = {
            key: value
            for key, value in {
                "path": query.get("path"),
                "mode": query.get("mode"),
                "host": query.get("host"),
                "extra": query.get("extra"),
            }.items()
            if value
        }
    proxy["_fwrouter_uri_query"] = query
    return ProtocolHookResult(handled=True, proxy=proxy)


def _parse_vless_reality_xray_outbound(outbound: dict[str, Any]) -> ProtocolHookResult:
    if str(outbound.get("protocol") or "").lower() != "vless":
        return ProtocolHookResult(handled=False)
    stream = outbound.get("streamSettings") if isinstance(outbound.get("streamSettings"), dict) else {}
    reality = stream.get("realitySettings") if isinstance(stream.get("realitySettings"), dict) else {}
    if str(stream.get("security") or "").lower() != "reality" and not reality:
        return ProtocolHookResult(handled=False)
    settings = outbound.get("settings") if isinstance(outbound.get("settings"), dict) else {}
    vnext = settings.get("vnext") if isinstance(settings.get("vnext"), list) else []
    target = vnext[0] if vnext and isinstance(vnext[0], dict) else {}
    users = target.get("users") if isinstance(target.get("users"), list) else []
    user = users[0] if users and isinstance(users[0], dict) else {}
    network = str(stream.get("network") or "tcp")
    name = str(outbound.get("remarks") or outbound.get("name") or outbound.get("tag") or target.get("address") or "").strip()
    proxy: dict[str, Any] = {
        "name": name,
        "type": "vless",
        "server": target.get("address"),
        "port": target.get("port"),
        "uuid": user.get("id") or user.get("uuid"),
        "network": network,
        "security": "reality",
        "tls": True,
        "servername": reality.get("serverName"),
        "client-fingerprint": reality.get("fingerprint"),
        "reality-opts": {
            key: value
            for key, value in {
                "public-key": reality.get("publicKey"),
                "short-id": reality.get("shortId"),
            }.items()
            if value is not None
        },
        "_fwrouter_json_tag": outbound.get("tag"),
    }
    if user.get("flow"):
        proxy["flow"] = user["flow"]
    if network == "grpc" and isinstance(stream.get("grpcSettings"), dict):
        proxy["grpc-opts"] = stream["grpcSettings"]
    if network == "xhttp" and isinstance(stream.get("xhttpSettings"), dict):
        proxy["xhttp-opts"] = stream["xhttpSettings"]
    return ProtocolHookResult(handled=True, proxy=proxy)


def _project_vless_reality_mihomo(proxy: dict[str, Any]) -> ProtocolHookResult:
    # URI and Xray JSON parsers already produce Mihomo's endpoint projection;
    # Clash/Mihomo YAML entries are already in that shape as well.
    return ProtocolHookResult(handled=True, proxy=dict(proxy))


def _matches_hysteria2(proxy: dict[str, Any]) -> bool:
    protocol = str(proxy.get("type") or proxy.get("protocol") or "").strip().lower()
    return protocol in {"hysteria2", "hy2"}


def _validate_hysteria2(proxy: dict[str, Any]) -> tuple[ProtocolValidationIssue, ...]:
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


def _parse_hysteria2_uri(uri: str) -> ProtocolHookResult:
    parsed = urlparse(uri)
    if parsed.scheme.lower() not in {"hysteria2", "hy2"}:
        return ProtocolHookResult(handled=False)
    try:
        port = parsed.port
    except ValueError:
        port = None
    if not parsed.hostname or not port or not parsed.username or parsed.password is not None:
        return ProtocolHookResult(
            handled=True,
            issues=(ProtocolValidationIssue("invalid_endpoint", "uri"),),
        )
    query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
    query: dict[str, str] = {}
    for key, value in query_pairs:
        if key in query:
            return ProtocolHookResult(
                handled=True,
                issues=(ProtocolValidationIssue("duplicate_parameter", "uri.query"),),
            )
        query[key] = value
    allowed = {"sni", "insecure", "obfs", "obfs-password", "pinSHA256"}
    if any(key not in allowed for key in query):
        return ProtocolHookResult(
            handled=True,
            issues=(ProtocolValidationIssue("unsupported_parameter", "uri.query"),),
        )
    proxy: dict[str, Any] = {
        "name": unquote(parsed.fragment or "").strip() or parsed.hostname,
        "type": "hysteria2",
        "server": parsed.hostname,
        "port": port,
        "password": unquote(parsed.username),
    }
    if query.get("sni"):
        proxy["sni"] = query["sni"]
    if "insecure" in query:
        if query["insecure"] not in {"0", "1"}:
            return ProtocolHookResult(
                handled=True,
                issues=(ProtocolValidationIssue("invalid_field", "skip-cert-verify"),),
            )
        proxy["skip-cert-verify"] = query["insecure"] == "1"
    if query.get("obfs"):
        proxy["obfs"] = query["obfs"]
    if "obfs-password" in query:
        proxy["obfs-password"] = query["obfs-password"]
    if query.get("pinSHA256"):
        proxy["fingerprint"] = query["pinSHA256"]
    return ProtocolHookResult(handled=True, proxy=proxy)


def _project_hysteria2_mihomo(proxy: dict[str, Any]) -> ProtocolHookResult:
    projected = dict(proxy)
    if str(projected.get("type") or "").lower() == "hy2":
        projected["type"] = "hysteria2"
    return ProtocolHookResult(handled=True, proxy=projected)


VLESS_REALITY = ProtocolIntegration(
    capabilities=ProtocolIntegrationCapabilities(
        protocol="vless",
        security="reality",
        source_import_formats=("plain_uri_lines", "base64_subscription", "json_profile", "clash_yaml"),
        mihomo_projection="supported",
        mihomo_native_validation_required=True,
        native_xray_egress_projection="not_implemented",
        public_profile_export_formats=("raw_vless", "base64_vless", "clash_mihomo"),
        public_profile_export_scope="FWRouter inbound client profile; independent of imported provider endpoints",
        health_latency="canonical_after_apply_via_active_runtime_adapter",
        transport_support="per-entry projection must pass pinned Mihomo validation",
    ),
    matcher=_matches_vless_reality,
    normalizer=_normalize_vless_reality,
    validator=_validate_vless_reality,
    parse_uri=_parse_vless_reality_uri,
    parse_xray_outbound=_parse_vless_reality_xray_outbound,
    project_mihomo=_project_vless_reality_mihomo,
)
HYSTERIA2 = ProtocolIntegration(
    capabilities=ProtocolIntegrationCapabilities(
        protocol="hysteria2",
        security="tls",
        source_import_formats=("plain_uri_lines", "base64_subscription", "clash_yaml"),
        mihomo_projection="supported",
        mihomo_native_validation_required=True,
        native_xray_egress_projection="not_implemented",
        public_profile_export_formats=(),
        public_profile_export_scope="not_applicable",
        health_latency="canonical_after_apply_via_active_runtime_adapter",
        transport_support="Mihomo v1.19.31 native validation required",
    ),
    matcher=_matches_hysteria2,
    normalizer=lambda proxy: dict(proxy),
    validator=_validate_hysteria2,
    parse_uri=_parse_hysteria2_uri,
    project_mihomo=_project_hysteria2_mihomo,
)
PROTOCOL_INTEGRATIONS: tuple[ProtocolIntegration, ...] = (VLESS_REALITY, HYSTERIA2)


def integration_for_proxy(
    proxy: dict[str, Any],
    *,
    integrations: tuple[ProtocolIntegration, ...] | None = None,
) -> ProtocolIntegration | None:
    for integration in integrations if integrations is not None else PROTOCOL_INTEGRATIONS:
        if integration.matches(proxy):
            return integration
    return None


def normalize_protocol_proxy(
    proxy: dict[str, Any],
    *,
    integrations: tuple[ProtocolIntegration, ...] | None = None,
) -> ProtocolNormalizationResult:
    integration = integration_for_proxy(proxy, integrations=integrations)
    if integration is None:
        return ProtocolNormalizationResult(proxy=dict(proxy))
    return integration.normalize(proxy)


def _dispatch_format_hook(
    value: Any,
    *,
    hook_name: str,
    integrations: tuple[ProtocolIntegration, ...] | None,
) -> ProtocolNormalizationResult | None:
    for integration in integrations if integrations is not None else PROTOCOL_INTEGRATIONS:
        hook = getattr(integration, hook_name)
        if hook is None:
            continue
        result = hook(value)
        if not result.handled:
            continue
        if result.issues:
            return ProtocolNormalizationResult(
                proxy=result.proxy or {}, issues=result.issues, handled=True
            )
        if result.proxy is None:
            return ProtocolNormalizationResult(
                proxy={},
                issues=(ProtocolValidationIssue("invalid_endpoint", hook_name),),
                handled=True,
            )
        normalized = normalize_protocol_proxy(result.proxy, integrations=(integration,))
        return ProtocolNormalizationResult(
            proxy=normalized.proxy,
            issues=normalized.issues,
            handled=True,
        )
    return None


def parse_uri_protocol_proxy(
    uri: str,
    *,
    integrations: tuple[ProtocolIntegration, ...] | None = None,
) -> ProtocolNormalizationResult | None:
    return _dispatch_format_hook(uri, hook_name="parse_uri", integrations=integrations)


def parse_xray_protocol_outbound(
    outbound: dict[str, Any],
    *,
    integrations: tuple[ProtocolIntegration, ...] | None = None,
) -> ProtocolNormalizationResult | None:
    return _dispatch_format_hook(
        outbound, hook_name="parse_xray_outbound", integrations=integrations
    )


def protocol_capabilities_for_proxy(proxy: dict[str, Any]) -> ProtocolIntegrationCapabilities | None:
    integration = integration_for_proxy(proxy)
    return integration.capabilities if integration else None


__all__ = [
    "ProtocolIntegration",
    "ProtocolIntegrationCapabilities",
    "ProtocolHookResult",
    "ProtocolNormalizationResult",
    "ProtocolValidationIssue",
    "PROTOCOL_INTEGRATIONS",
    "VLESS_REALITY",
    "HYSTERIA2",
    "integration_for_proxy",
    "normalize_protocol_proxy",
    "parse_uri_protocol_proxy",
    "parse_xray_protocol_outbound",
    "protocol_capabilities_for_proxy",
]
