from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import base64
import hashlib
import json
import re
from typing import Any
from urllib.parse import parse_qsl, unquote, urlparse

import httpx
import yaml


class SubscriptionRefreshStatus(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"


@dataclass(frozen=True)
class SubscriptionServer:
    """Server parsed from a Mihomo/Clash provider subscription."""

    server_id: str
    server_name: str
    provider_name: str | None = None
    country_code: str | None = None
    region: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    protocol: str | None = None
    host: str | None = None
    port: int | None = None
    transport: str | None = None
    raw_identity: str | None = None
    parser_format: str | None = None
    source_format: str | None = None
    runtime_name: str | None = None


@dataclass(frozen=True)
class SubscriptionRequestProfile:
    """HTTP request profile used to download a provider subscription."""

    name: str
    user_agent: str
    headers: dict[str, str] = field(default_factory=dict)
    accept: str | None = None

    def request_headers(self) -> dict[str, str]:
        result = dict(self.headers)
        result["User-Agent"] = self.user_agent
        if self.accept:
            result["Accept"] = self.accept
        return result


@dataclass(frozen=True)
class SubscriptionPayloadDetection:
    """Sanitized classification of a raw subscription response."""

    detected_format: str
    raw_entry_count: int
    parseable_by_current_parser: bool
    provider_placeholder: bool = False
    unsupported_reason: str | None = None

    def to_metadata(self) -> dict[str, Any]:
        return {
            "detected_format": self.detected_format,
            "raw_entry_count": self.raw_entry_count,
            "parseable_by_current_parser": self.parseable_by_current_parser,
            "provider_placeholder": self.provider_placeholder,
            "unsupported_reason": self.unsupported_reason,
        }


@dataclass(frozen=True)
class SubscriptionFetchResult:
    """Raw fetch result plus sanitized diagnostics."""

    ok: bool
    body: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    detection: SubscriptionPayloadDetection | None = None
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class SubscriptionRefreshResult:
    """Result of provider subscription refresh."""

    status: SubscriptionRefreshStatus
    servers: list[SubscriptionServer] = field(default_factory=list)
    message: str = ""
    error_code: str | None = None
    error_message: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == SubscriptionRefreshStatus.SUCCESS

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "status": self.status.value,
            "servers": [
                {
                    "server_id": server.server_id,
                    "server_name": server.server_name,
                    "provider_name": server.provider_name,
                    "country_code": server.country_code,
                    "region": server.region,
                    "raw": server.raw,
                }
                for server in self.servers
            ],
            "message": self.message,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "metadata": self.metadata,
        }


class SubscriptionAdapter:
    """Base interface for VPN provider subscription refresh."""

    def refresh(self, url: str) -> SubscriptionRefreshResult:
        raise NotImplementedError


DEVICE_HEADERS: dict[str, str] = {
    "x-hwid": "fwrouter-v2-minis",
    "x-device-os": "Linux",
    "x-ver-os": "Debian 12",
    "x-device-model": "FWRouter v2 minis",
}


LEGACY_FLCLASH_PROFILE = SubscriptionRequestProfile(
    name="legacy_flclash",
    user_agent="FlClashX/1.0.0",
    accept="text/plain, application/yaml, application/x-yaml, application/json, */*",
    headers=DEVICE_HEADERS,
)


CLIENT_COMPATIBLE_PROFILE = SubscriptionRequestProfile(
    name="client_compatible",
    user_agent="Happ/3.19.1/Android",
    accept="*/*",
    headers=DEVICE_HEADERS,
)


SUBSCRIPTION_REQUEST_PROFILES: dict[str, SubscriptionRequestProfile] = {
    LEGACY_FLCLASH_PROFILE.name: LEGACY_FLCLASH_PROFILE,
    CLIENT_COMPATIBLE_PROFILE.name: CLIENT_COMPATIBLE_PROFILE,
}


CURRENT_PARSER_FORMAT = "clash_yaml"
FULL_PAYLOAD_FORMAT_RANK: dict[str, int] = {
    "base64_subscription": 40,
    "plain_uri_lines": 40,
    "json_profile": 30,
    "clash_yaml": 20,
}
URI_SCHEME_RE = re.compile(
    r"^(?:vless|vmess|trojan|ss|ssr|hysteria|hysteria2|hy2)://",
    re.IGNORECASE,
)
PLACEHOLDER_MARKERS = (
    "unsupported app",
    "unsupported client",
    "not supported",
    "не поддерживается",
    "передачу hwid",
    "enable hwid",
    "hwid",
)


def _safe_url_metadata(url: str) -> dict[str, Any]:
    parsed = urlparse(url)
    return {
        "scheme": parsed.scheme,
        "host": parsed.netloc,
        "path_present": bool(parsed.path),
        "query_present": bool(parsed.query),
    }


def _base_fetch_metadata(
    *,
    url: str,
    response: httpx.Response,
    request_profile: SubscriptionRequestProfile,
    payload_size: int,
) -> dict[str, Any]:
    return {
        "source": _safe_url_metadata(url),
        "request_profile": request_profile.name,
        "final_url": _safe_url_metadata(str(response.url)),
        "status": response.status_code,
        "content_type": response.headers.get("content-type"),
        "payload_size": payload_size,
    }


def _non_empty_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def _stable_digest(value: bytes | str) -> str:
    data = value if isinstance(value, bytes) else value.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _subscription_server_id(identity: bytes | str) -> str:
    return f"sub:{_stable_digest(identity)}"


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _runtime_name(display_name: str, server_id: str) -> str:
    suffix = server_id.removeprefix("sub:")[:8]
    return f"{display_name} [{suffix}]" if display_name else f"subscription [{suffix}]"


def _looks_like_placeholder(text: str, *, names: list[str] | None = None) -> bool:
    haystack = text.lower()
    if any(marker in haystack for marker in PLACEHOLDER_MARKERS):
        return True
    if names:
        joined = "\n".join(names).lower()
        if any(marker in joined for marker in PLACEHOLDER_MARKERS):
            return True
    return False


def _decode_base64_subscription(text: str) -> str | None:
    compact = "".join(text.strip().split())
    if not compact:
        return None
    try:
        decoded = base64.b64decode(
            compact + "=" * (-len(compact) % 4),
            validate=False,
        )
        decoded_text = decoded.decode("utf-8", errors="replace")
    except Exception:
        return None
    lines = _non_empty_lines(decoded_text)
    if any(URI_SCHEME_RE.match(line) for line in lines):
        return decoded_text
    return None


def _raw_payload_entries(text: str, detection: SubscriptionPayloadDetection) -> Any:
    if detection.detected_format == "base64_subscription":
        return _non_empty_lines(_decode_base64_subscription(text) or "")
    if detection.detected_format == "plain_uri_lines":
        return _non_empty_lines(text)
    if detection.detected_format == "json_profile":
        return json.loads(text)
    if detection.detected_format == "clash_yaml":
        return yaml.safe_load(text) or {}
    return text


def _country_code_from_display_name(name: str) -> str | None:
    return _country_code_from_regional_indicator_emoji(name)


def _proxy_with_runtime_name(
    proxy: dict[str, Any],
    *,
    display_name: str,
    server_id: str,
    raw_identity: str,
    parser_format: str,
) -> dict[str, Any]:
    runtime_name = _runtime_name(display_name, server_id)
    raw = dict(proxy)
    raw["name"] = runtime_name
    raw["_fwrouter_display_name"] = display_name
    raw["_fwrouter_runtime_name"] = runtime_name
    raw["_fwrouter_server_id"] = server_id
    raw["_fwrouter_raw_identity_sha256"] = _stable_digest(raw_identity)
    raw["_fwrouter_parser_format"] = parser_format
    return raw


def _server_from_proxy_dict(
    proxy: dict[str, Any],
    *,
    identity: str,
    parser_format: str,
    source_format: str,
) -> SubscriptionServer | None:
    display_name = str(proxy.get("name") or "").strip()
    if not display_name:
        return None
    server_id = _subscription_server_id(identity)
    protocol = str(proxy.get("type") or proxy.get("protocol") or "").strip().lower() or None
    host = str(proxy.get("server") or proxy.get("address") or "").strip() or None
    try:
        port = int(proxy["port"]) if proxy.get("port") is not None else None
    except (TypeError, ValueError):
        port = None
    transport = str(proxy.get("network") or proxy.get("type") or "").strip() or None
    raw = _proxy_with_runtime_name(
        proxy,
        display_name=display_name,
        server_id=server_id,
        raw_identity=identity,
        parser_format=parser_format,
    )
    return SubscriptionServer(
        server_id=server_id,
        server_name=display_name,
        provider_name=str(proxy.get("provider") or "subscription"),
        country_code=_country_code_from_display_name(display_name),
        region=None,
        raw=raw,
        protocol=protocol,
        host=host,
        port=port,
        transport=transport,
        raw_identity=identity,
        parser_format=parser_format,
        source_format=source_format,
        runtime_name=str(raw.get("_fwrouter_runtime_name") or ""),
    )


def _mihomo_proxy_from_vless_uri(uri: str) -> dict[str, Any] | None:
    parsed = urlparse(uri)
    if parsed.scheme.lower() != "vless" or not parsed.hostname or not parsed.port:
        return None
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    name = unquote(parsed.fragment or "").strip() or parsed.hostname
    network = str(query.get("type") or query.get("network") or "tcp").strip() or "tcp"
    security = str(query.get("security") or "").strip()
    proxy: dict[str, Any] = {
        "name": name,
        "type": "vless",
        "server": parsed.hostname,
        "port": parsed.port,
        "uuid": unquote(parsed.username or ""),
        "network": network,
    }
    if query.get("flow"):
        proxy["flow"] = query["flow"]
    if query.get("encryption"):
        proxy["encryption"] = query["encryption"]
    if security:
        proxy["tls"] = security in {"tls", "reality"}
        proxy["security"] = security
    if query.get("sni"):
        proxy["servername"] = query["sni"]
    if query.get("fp"):
        proxy["client-fingerprint"] = query["fp"]
    if query.get("pbk") or query.get("sid") or security == "reality":
        proxy["reality-opts"] = {
            key: value
            for key, value in {
                "public-key": query.get("pbk"),
                "short-id": query.get("sid"),
            }.items()
            if value is not None
        }
    if network == "grpc":
        proxy["grpc-opts"] = {
            "grpc-service-name": query.get("serviceName") or query.get("serviceName".lower()) or query.get("grpc-service-name") or "",
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
    return proxy


def _proxy_from_uri(uri: str) -> tuple[dict[str, Any] | None, str | None]:
    scheme = urlparse(uri).scheme.lower()
    if scheme == "vless":
        proxy = _mihomo_proxy_from_vless_uri(uri)
        return proxy, None if proxy is not None else "invalid_vless_uri"
    return None, f"unsupported_uri_scheme:{scheme or 'unknown'}"


def _servers_from_uri_lines(lines: list[str], *, source_format: str) -> tuple[list[SubscriptionServer], list[dict[str, Any]]]:
    servers: list[SubscriptionServer] = []
    diagnostics: list[dict[str, Any]] = []
    seen_identities: set[str] = set()
    for index, line in enumerate(lines, start=1):
        identity = line.strip().replace("\r", "").replace("\n", "")
        if not identity:
            continue
        if identity in seen_identities:
            diagnostics.append({"index": index, "category": "duplicate_exact", "reason": "exact_duplicate_entry"})
            continue
        seen_identities.add(identity)
        proxy, error = _proxy_from_uri(identity)
        if proxy is None:
            diagnostics.append({"index": index, "category": "unsupported", "reason": error or "unsupported_uri"})
            continue
        server = _server_from_proxy_dict(
            proxy,
            identity=identity,
            parser_format="uri",
            source_format=source_format,
        )
        if server is None:
            diagnostics.append({"index": index, "category": "invalid", "reason": "missing_display_name"})
            continue
        servers.append(server)
    return servers, diagnostics


def _xray_outbound_to_proxy(outbound: dict[str, Any]) -> dict[str, Any] | None:
    if str(outbound.get("protocol") or "").lower() != "vless":
        return None
    settings = outbound.get("settings") if isinstance(outbound.get("settings"), dict) else {}
    vnext = settings.get("vnext") if isinstance(settings.get("vnext"), list) else []
    if not vnext or not isinstance(vnext[0], dict):
        return None
    target = vnext[0]
    users = target.get("users") if isinstance(target.get("users"), list) else []
    user = users[0] if users and isinstance(users[0], dict) else {}
    stream = outbound.get("streamSettings") if isinstance(outbound.get("streamSettings"), dict) else {}
    reality = stream.get("realitySettings") if isinstance(stream.get("realitySettings"), dict) else {}
    network = str(stream.get("network") or "tcp")
    name = str(outbound.get("remarks") or outbound.get("name") or outbound.get("tag") or target.get("address") or "").strip()
    proxy: dict[str, Any] = {
        "name": name,
        "type": "vless",
        "server": target.get("address"),
        "port": target.get("port"),
        "uuid": user.get("id") or user.get("uuid"),
        "network": network,
    }
    if user.get("flow"):
        proxy["flow"] = user["flow"]
    if stream.get("security"):
        proxy["security"] = stream["security"]
        proxy["tls"] = str(stream["security"]).lower() in {"tls", "reality"}
    if reality:
        proxy["servername"] = reality.get("serverName")
        proxy["client-fingerprint"] = reality.get("fingerprint")
        proxy["reality-opts"] = {
            key: value
            for key, value in {
                "public-key": reality.get("publicKey"),
                "short-id": reality.get("shortId"),
            }.items()
            if value is not None
        }
    if network == "grpc" and isinstance(stream.get("grpcSettings"), dict):
        proxy["grpc-opts"] = stream["grpcSettings"]
    if network == "xhttp" and isinstance(stream.get("xhttpSettings"), dict):
        proxy["xhttp-opts"] = stream["xhttpSettings"]
    proxy["_fwrouter_json_tag"] = outbound.get("tag")
    return proxy


def _walk_json_vless_outbounds(payload: Any) -> list[dict[str, Any]]:
    outbounds: list[dict[str, Any]] = []
    def walk(value: Any) -> None:
        if isinstance(value, dict):
            if isinstance(value.get("outbounds"), list):
                for outbound in value["outbounds"]:
                    if isinstance(outbound, dict):
                        outbounds.append(outbound)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    walk(payload)
    return outbounds


def _json_profile_objects(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        return [payload] if isinstance(payload.get("outbounds"), list) else []
    if isinstance(payload, list):
        return [
            item
            for item in payload
            if isinstance(item, dict) and isinstance(item.get("outbounds"), list)
        ]
    return []


def _service_outbound_protocol(protocol: str) -> bool:
    return protocol in {"freedom", "blackhole", "dns", "api", "direct", "block"}


def _endpoint_summary_from_vless_outbound(outbound: dict[str, Any]) -> dict[str, Any] | None:
    proxy = _xray_outbound_to_proxy(outbound)
    if proxy is None:
        return None
    return {
        "identity": _subscription_server_id(_canonical_json(outbound)),
        "tag": outbound.get("tag"),
        "protocol": "vless",
        "host": proxy.get("server"),
        "port": proxy.get("port"),
        "network": proxy.get("network"),
        "security": proxy.get("security"),
        "runtime": proxy,
    }


def _logical_proxy_from_json_profile(profile: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    outbounds = profile.get("outbounds") if isinstance(profile.get("outbounds"), list) else []
    routing = profile.get("routing") if isinstance(profile.get("routing"), dict) else {}
    endpoints: list[dict[str, Any]] = []
    service_outbounds: list[dict[str, Any]] = []
    unsupported: list[dict[str, Any]] = []
    for index, outbound in enumerate(outbounds, start=1):
        if not isinstance(outbound, dict):
            unsupported.append({"index": index, "category": "unsupported", "reason": "outbound_not_object"})
            continue
        protocol = str(outbound.get("protocol") or "").lower()
        if protocol == "vless":
            endpoint = _endpoint_summary_from_vless_outbound(outbound)
            if endpoint is not None:
                endpoints.append(endpoint)
            else:
                unsupported.append({"index": index, "category": "unsupported", "reason": "invalid_vless_outbound"})
        elif _service_outbound_protocol(protocol):
            service_outbounds.append({
                "tag": outbound.get("tag"),
                "protocol": protocol,
            })
        else:
            unsupported.append({"index": index, "category": "unsupported", "reason": f"unsupported_protocol:{protocol or 'unknown'}"})
    display_name = str(profile.get("remarks") or profile.get("name") or "").strip()
    if not display_name:
        display_name = str((endpoints[0] if endpoints else {}).get("tag") or "JSON subscription profile")
    if not endpoints:
        return None, {
            "display_name": display_name,
            "endpoints": endpoints,
            "service_outbounds": service_outbounds,
            "unsupported": unsupported,
            "reason": "no_vpn_endpoints",
        }
    primary = endpoints[0]
    balancers = routing.get("balancers") if isinstance(routing.get("balancers"), list) else []
    strategies = {
        str(item.get("strategy", {}).get("type") or "").strip().lower()
        for item in balancers
        if isinstance(item, dict) and isinstance(item.get("strategy"), dict)
    }
    source_semantic = "least_load" if "leastload" in strategies else "unspecified"
    proxy = {
        "name": display_name,
        "type": str(primary.get("protocol") or "vless"),
        "server": primary.get("host"),
        "port": primary.get("port"),
        "network": primary.get("network"),
        "_fwrouter_topology": {
            "kind": "logical_profile",
            "endpoints": endpoints,
            "service_outbounds": service_outbounds,
            "balancers": balancers,
            "source_semantic": source_semantic,
            "runtime_policy": "fallback",
            "rules_count": len(routing.get("rules") or []) if isinstance(routing.get("rules"), list) else 0,
            "unsupported": unsupported,
        },
    }
    if primary.get("security"):
        proxy["security"] = primary.get("security")
        proxy["tls"] = primary.get("security") in {"tls", "reality"}
    runtime = primary.get("runtime") if isinstance(primary.get("runtime"), dict) else {}
    for key in ("uuid", "flow", "servername", "client-fingerprint", "reality-opts", "grpc-opts", "ws-opts", "xhttp-opts"):
        if key in runtime:
            proxy[key] = runtime[key]
    return proxy, proxy["_fwrouter_topology"]


def _servers_from_json_profile(payload: Any) -> tuple[list[SubscriptionServer], list[dict[str, Any]]]:
    servers: list[SubscriptionServer] = []
    diagnostics: list[dict[str, Any]] = []
    seen_identities: set[str] = set()
    for index, profile in enumerate(_json_profile_objects(payload), start=1):
        proxy, topology = _logical_proxy_from_json_profile(profile)
        if proxy is None:
            diagnostics.append({
                "index": index,
                "category": "unsupported",
                "reason": topology.get("reason") or "unsupported_json_profile",
            })
            continue
        identity = _canonical_json(profile)
        if identity in seen_identities:
            diagnostics.append({"index": index, "category": "duplicate_exact", "reason": "exact_duplicate_entry"})
            continue
        seen_identities.add(identity)
        server = _server_from_proxy_dict(
            proxy,
            identity=identity,
            parser_format="json_profile",
            source_format="json_profile",
        )
        if server is None:
            diagnostics.append({"index": index, "category": "invalid", "reason": "missing_display_name"})
            continue
        servers.append(server)
        diagnostics.extend(
            {
                "index": index,
                "category": "internal_endpoint",
                "reason": "json_profile_member_endpoint",
                "tag": endpoint.get("tag"),
            }
            for endpoint in topology.get("endpoints", [])
        )
        diagnostics.extend(
            {
                "index": index,
                "category": "service_outbound",
                "reason": "service_outbound_not_user_server",
                "tag": outbound.get("tag"),
                "protocol": outbound.get("protocol"),
            }
            for outbound in topology.get("service_outbounds", [])
        )
        diagnostics.extend(topology.get("unsupported", []))
    return servers, diagnostics


def parse_subscription_payload(
    text: str,
    *,
    metadata: dict[str, Any] | None = None,
) -> SubscriptionRefreshResult:
    detection = detect_subscription_payload(
        text,
        content_type=(metadata or {}).get("content_type"),
    )
    if detection.provider_placeholder:
        return SubscriptionRefreshResult(
            status=SubscriptionRefreshStatus.FAILED,
            message="Subscription provider returned a placeholder payload.",
            error_code="SUBSCRIPTION_PROVIDER_PLACEHOLDER",
            error_message="Provider returned placeholder or unsupported-client content.",
            metadata={**(metadata or {}), **detection.to_metadata()},
        )
    if detection.detected_format == "clash_yaml":
        return parse_mihomo_subscription_yaml(text, metadata={**(metadata or {}), **detection.to_metadata()})
    if detection.detected_format == "base64_subscription":
        servers, diagnostics = _servers_from_uri_lines(
            _non_empty_lines(_decode_base64_subscription(text) or ""),
            source_format="base64_subscription",
        )
    elif detection.detected_format == "plain_uri_lines":
        servers, diagnostics = _servers_from_uri_lines(_non_empty_lines(text), source_format="plain_uri_lines")
    elif detection.detected_format == "json_profile":
        servers, diagnostics = _servers_from_json_profile(json.loads(text))
    else:
        return SubscriptionRefreshResult(
            status=SubscriptionRefreshStatus.FAILED,
            message="Subscription format is unsupported.",
            error_code="SUBSCRIPTION_FORMAT_UNSUPPORTED",
            error_message=detection.unsupported_reason or "unsupported_format",
            metadata={**(metadata or {}), **detection.to_metadata()},
        )
    if not servers:
        return SubscriptionRefreshResult(
            status=SubscriptionRefreshStatus.FAILED,
            message="Subscription contains no usable servers.",
            error_code="SUBSCRIPTION_SERVERS_EMPTY",
            error_message="No supported entries were parsed.",
            metadata={
                **(metadata or {}),
                **detection.to_metadata(),
                "parsed_count": 0,
                "unsupported_count": len(diagnostics),
                "entry_diagnostics": diagnostics,
            },
        )
    exact_duplicates = sum(1 for item in diagnostics if item.get("category") == "duplicate_exact")
    internal_endpoint_count = sum(1 for item in diagnostics if item.get("category") == "internal_endpoint")
    service_outbound_count = sum(1 for item in diagnostics if item.get("category") == "service_outbound")
    return SubscriptionRefreshResult(
        status=SubscriptionRefreshStatus.SUCCESS,
        servers=servers,
        message="Subscription parsed successfully.",
        metadata={
            **(metadata or {}),
            **detection.to_metadata(),
            "parsed_count": len(servers),
            "servers_count": len(servers),
            "unsupported_count": sum(1 for item in diagnostics if item.get("category") == "unsupported"),
            "invalid_count": sum(1 for item in diagnostics if item.get("category") == "invalid"),
            "exact_duplicate_count": exact_duplicates,
            "internal_endpoint_count": internal_endpoint_count,
            "service_outbound_count": service_outbound_count,
            "entry_diagnostics": diagnostics[:50],
        },
    )


def _json_profile_entry_count(payload: Any) -> int:
    if isinstance(payload, list):
        return len(payload)
    if isinstance(payload, dict):
        outbounds = payload.get("outbounds")
        if isinstance(outbounds, list):
            return len(outbounds)
    return 1


def detect_subscription_payload(text: str, *, content_type: str | None = None) -> SubscriptionPayloadDetection:
    """Detect subscription payload format without exposing raw credentials."""

    stripped = text.strip()
    if not stripped:
        return SubscriptionPayloadDetection(
            detected_format="empty",
            raw_entry_count=0,
            parseable_by_current_parser=False,
            provider_placeholder=False,
            unsupported_reason="empty_payload",
        )

    try:
        parsed_yaml = yaml.safe_load(text)
    except yaml.YAMLError:
        parsed_yaml = None
    if isinstance(parsed_yaml, dict) and isinstance(parsed_yaml.get("proxies"), list):
        proxies = parsed_yaml.get("proxies") or []
        names = [
            str(proxy.get("name") or "")
            for proxy in proxies
            if isinstance(proxy, dict)
        ]
        placeholder = _looks_like_placeholder(text, names=names)
        return SubscriptionPayloadDetection(
            detected_format=CURRENT_PARSER_FORMAT,
            raw_entry_count=len(proxies),
            parseable_by_current_parser=not placeholder,
            provider_placeholder=placeholder,
            unsupported_reason="provider_placeholder" if placeholder else None,
        )

    lines = _non_empty_lines(text)
    if lines and any(URI_SCHEME_RE.match(line) for line in lines):
        placeholder = _looks_like_placeholder(text)
        return SubscriptionPayloadDetection(
            detected_format="plain_uri_lines",
            raw_entry_count=len(lines),
            parseable_by_current_parser=False,
            provider_placeholder=placeholder,
            unsupported_reason="provider_placeholder" if placeholder else "format_not_supported_by_current_parser",
        )

    decoded_text = _decode_base64_subscription(text)
    if decoded_text is not None:
        decoded_lines = _non_empty_lines(decoded_text)
        placeholder = _looks_like_placeholder(decoded_text)
        return SubscriptionPayloadDetection(
            detected_format="base64_subscription",
            raw_entry_count=len(decoded_lines),
            parseable_by_current_parser=False,
            provider_placeholder=placeholder,
            unsupported_reason="provider_placeholder" if placeholder else "format_not_supported_by_current_parser",
        )

    try:
        parsed_json = json.loads(text)
    except json.JSONDecodeError:
        parsed_json = None
    if parsed_json is not None:
        placeholder = _looks_like_placeholder(text)
        return SubscriptionPayloadDetection(
            detected_format="json_profile",
            raw_entry_count=_json_profile_entry_count(parsed_json),
            parseable_by_current_parser=False,
            provider_placeholder=placeholder,
            unsupported_reason="provider_placeholder" if placeholder else "format_not_supported_by_current_parser",
        )

    placeholder = _looks_like_placeholder(text)
    return SubscriptionPayloadDetection(
        detected_format="unsupported",
        raw_entry_count=len(lines),
        parseable_by_current_parser=False,
        provider_placeholder=placeholder,
        unsupported_reason="provider_placeholder" if placeholder else "unsupported_format",
    )


class HttpMihomoSubscriptionAdapter(SubscriptionAdapter):
    """Download and parse Mihomo/Clash YAML subscriptions.

    This adapter only downloads and parses provider data. It does not write
    Mihomo config, does not update SQLite and does not restart containers.
    """

    def __init__(self, *, timeout_seconds: float = 20.0) -> None:
        self.timeout_seconds = timeout_seconds

    def fetch_with_profile(
        self,
        url: str,
        *,
        request_profile: SubscriptionRequestProfile | None = None,
    ) -> SubscriptionFetchResult:
        normalized_url = url.strip()
        profile = request_profile or LEGACY_FLCLASH_PROFILE

        if not normalized_url:
            return SubscriptionFetchResult(
                ok=False,
                error_code="SUBSCRIPTION_URL_EMPTY",
                error_message="SUBSCRIPTION_URL_EMPTY",
                metadata={"request_profile": profile.name},
            )

        try:
            with httpx.Client(
                timeout=self.timeout_seconds,
                follow_redirects=True,
                headers=profile.request_headers(),
            ) as client:
                response = client.get(normalized_url)
                response.raise_for_status()
                body = response.text
        except httpx.HTTPError as exc:
            return SubscriptionFetchResult(
                ok=False,
                error_code="SUBSCRIPTION_DOWNLOAD_FAILED",
                error_message=str(exc),
                metadata={
                    "source": _safe_url_metadata(normalized_url),
                    "request_profile": profile.name,
                },
            )

        detection = detect_subscription_payload(
            body,
            content_type=response.headers.get("content-type"),
        )
        metadata = {
            **_base_fetch_metadata(
                url=normalized_url,
                response=response,
                request_profile=profile,
                payload_size=len(response.content),
            ),
            **detection.to_metadata(),
        }
        if detection.provider_placeholder:
            return SubscriptionFetchResult(
                ok=False,
                body=body,
                metadata=metadata,
                detection=detection,
                error_code="SUBSCRIPTION_PROVIDER_PLACEHOLDER",
                error_message="Provider returned a placeholder or unsupported-client response.",
            )
        return SubscriptionFetchResult(
            ok=True,
            body=body,
            metadata=metadata,
            detection=detection,
        )

    def inspect_request_profiles(
        self,
        url: str,
        *,
        request_profiles: list[SubscriptionRequestProfile] | None = None,
    ) -> list[SubscriptionFetchResult]:
        """Fetch explicit profiles for diagnostics or future parser rollout.

        Normal refresh does not call this method, so ordinary production refresh
        remains a single legacy-profile network fetch until Phase 2C can parse
        and persist non-YAML payloads.
        """

        profiles = request_profiles or [
            LEGACY_FLCLASH_PROFILE,
            CLIENT_COMPATIBLE_PROFILE,
        ]
        return [
            self.fetch_with_profile(url, request_profile=profile)
            for profile in profiles
        ]

    def refresh(self, url: str) -> SubscriptionRefreshResult:
        normalized_url = url.strip()

        if not normalized_url:
            return SubscriptionRefreshResult(
                status=SubscriptionRefreshStatus.FAILED,
                message="Subscription URL is empty.",
                error_code="SUBSCRIPTION_URL_EMPTY",
                error_message="SUBSCRIPTION_URL_EMPTY",
            )

        inspected = self.inspect_request_profiles(normalized_url)
        fetch_result = choose_preferred_fetch_result(inspected) or (
            inspected[0] if inspected else self.fetch_with_profile(normalized_url)
        )
        if not fetch_result.ok:
            return SubscriptionRefreshResult(
                status=SubscriptionRefreshStatus.FAILED,
                message="Subscription download failed.",
                error_code=fetch_result.error_code,
                error_message=fetch_result.error_message,
                metadata=fetch_result.metadata,
            )

        return parse_subscription_payload(
            fetch_result.body,
            metadata=fetch_result.metadata,
        )


def choose_preferred_fetch_result(
    results: list[SubscriptionFetchResult],
) -> SubscriptionFetchResult | None:
    """Choose the best valid payload without trusting raw line count alone."""

    candidates = [
        result
        for result in results
        if result.ok
        and result.detection is not None
        and not result.detection.provider_placeholder
        and result.detection.detected_format in FULL_PAYLOAD_FORMAT_RANK
        and result.detection.raw_entry_count > 0
    ]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda result: (
            FULL_PAYLOAD_FORMAT_RANK[result.detection.detected_format]
            if result.detection
            else 0,
            result.detection.raw_entry_count if result.detection else 0,
        ),
    )


def _country_code_from_regional_indicator_emoji(text: str) -> str | None:
    letters: list[str] = []
    for char in text:
        codepoint = ord(char)
        if 0x1F1E6 <= codepoint <= 0x1F1FF:
            letters.append(chr(ord("A") + codepoint - 0x1F1E6))
            if len(letters) == 2:
                return "".join(letters)
        else:
            letters = []
    return None


def parse_mihomo_subscription_yaml(
    text: str,
    *,
    metadata: dict[str, Any] | None = None,
) -> SubscriptionRefreshResult:
    """Parse Mihomo/Clash YAML subscription text."""

    try:
        parsed = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        return SubscriptionRefreshResult(
            status=SubscriptionRefreshStatus.FAILED,
            message="Subscription YAML parse failed.",
            error_code="SUBSCRIPTION_YAML_INVALID",
            error_message=str(exc),
            metadata=metadata or {},
        )

    if not isinstance(parsed, dict):
        return SubscriptionRefreshResult(
            status=SubscriptionRefreshStatus.FAILED,
            message="Subscription payload must be a YAML object.",
            error_code="SUBSCRIPTION_FORMAT_UNSUPPORTED",
            error_message="Top-level YAML value is not an object.",
            metadata=metadata or {},
        )

    proxies = parsed.get("proxies")
    if not isinstance(proxies, list):
        return SubscriptionRefreshResult(
            status=SubscriptionRefreshStatus.FAILED,
            message="Subscription does not contain a proxies list.",
            error_code="SUBSCRIPTION_PROXIES_MISSING",
            error_message="Expected top-level 'proxies' list in Mihomo/Clash YAML.",
            metadata={
                **(metadata or {}),
                "top_level_keys": sorted(str(key) for key in parsed.keys()),
            },
        )

    servers: list[SubscriptionServer] = []
    diagnostics: list[dict[str, Any]] = []
    seen_identities: set[str] = set()

    # Simple mapping for common countries based on names, can be expanded
    emoji_to_country_code = {
        "🇫🇮": "FI", "🇳🇱": "NL", "🇳🇴": "NO", "🇩🇪": "DE", "🇵🇱": "PL", "🇨🇿": "CZ",
        "🇷🇴": "RO", "🇬🇧": "GB", "🇸🇪": "SE", "🇸🇬": "SG", "🇺🇸": "US", "🇫🇷": "FR",
        "🇯🇵": "JP", "🇨🇦": "CA", "🇦🇺": "AU", "🇧🇷": "BR", "🇮🇳": "IN", "🇮🇩": "ID",
        "🇭🇰": "HK", "🇰🇷": "KR", "🇷🇺": "RU", "🇺🇦": "UA", "🇨🇭": "CH", "🇨🇾": "CY",
        "🇩🇰": "DK", "🇪🇪": "EE", "🇭🇺": "HU", "🇲🇩": "MD", "🇵🇰": "PK", "🇷🇸": "RS",
        "🇿🇦": "ZA", "🇹🇭": "TH", "🇹🇷": "TR", "🇺🇿": "UZ", "🇻🇳": "VN", "🇧🇦": "BA",
        "🇧🇬": "BG", "🇦🇲": "AM", "🇦🇿": "AZ", "🇰🇿": "KZ", "🇱🇻": "LV", "🇱🇹": "LT",
        "🇳🇬": "NG", "🇮🇱": "IL", "🇪🇸": "ES", "🇬🇪": "GE", "🇦🇹": "AT", "🇮🇹": "IT",
    }
    
    country_name_to_code = {
        "finland": "FI", "netherlands": "NL", "norway": "NO", "germany": "DE", "poland": "PL",
        "czech republic": "CZ", "romania": "RO", "united kingdom": "GB", "sweden": "SE",
        "singapore": "SG", "usa": "US", "france": "FR", "japan": "JP", "canada": "CA",
        "australia": "AU", "brazil": "BR", "india": "IN", "indonesia": "ID", "hong kong": "HK",
        "south korea": "KR", "russia": "RU", "ukraine": "UA", "switzerland": "CH", "cyprus": "CY",
        "denmark": "DK", "estonia": "EE", "hungary": "HU", "moldova": "MD", "pakistan": "PK",
        "serbia": "RS", "south africa": "ZA", "thailand": "TH", "turkey": "TR", "uzbekistan": "UZ",
        "vietnam": "VN", "bosnia and herzegovina": "BA", "bulgaria": "BG", "armenia": "AM",
        "azerbaijan": "AZ", "kazakhstan": "KZ", "latvia": "LV", "lithuania": "LT", "nigeria": "NG",
        "israel": "IL", "spain": "ES", "georgia": "GE", "austria": "AT", "italy": "IT",
    }

    for index, proxy in enumerate(proxies, start=1):
        if not isinstance(proxy, dict):
            diagnostics.append({"index": index, "category": "invalid", "reason": "proxy_entry_not_object"})
            continue

        name = str(proxy.get("name") or "").strip()
        if not name:
            diagnostics.append({"index": index, "category": "invalid", "reason": "missing_display_name"})
            continue

        identity = _canonical_json(proxy)
        if identity in seen_identities:
            diagnostics.append({"index": index, "category": "duplicate_exact", "reason": "exact_duplicate_entry"})
            continue
        seen_identities.add(identity)
        
        extracted_country_code = _country_code_from_regional_indicator_emoji(name)

        # 1. Try to extract from known emoji fallbacks.
        if not extracted_country_code:
            for emoji, code in emoji_to_country_code.items():
                if emoji in name:
                    extracted_country_code = code
                    break
        
        # 2. If not found by emoji, try to extract from country name
        if not extracted_country_code:
            lower_name = name.lower()
            for country_name, code in country_name_to_code.items():
                if country_name in lower_name:
                    extracted_country_code = code
                    break
        
        server = _server_from_proxy_dict(
            proxy,
            identity=identity,
            parser_format="clash_yaml",
            source_format="clash_yaml",
        )
        if server is None:
            diagnostics.append({"index": index, "category": "invalid", "reason": "invalid_proxy"})
            continue
        if extracted_country_code and not server.country_code:
            server = SubscriptionServer(
                **{**server.__dict__, "country_code": extracted_country_code}
            )
        servers.append(server)

    if not servers:
        return SubscriptionRefreshResult(
            status=SubscriptionRefreshStatus.FAILED,
            message="Subscription contains no usable proxies.",
            error_code="SUBSCRIPTION_SERVERS_EMPTY",
            error_message="No proxy entries with a non-empty name were found.",
            metadata={
                **(metadata or {}),
                "proxies_count": len(proxies),
                "entry_diagnostics": diagnostics[:50],
            },
        )

    return SubscriptionRefreshResult(
        status=SubscriptionRefreshStatus.SUCCESS,
        servers=servers,
        message="Subscription parsed successfully.",
        metadata={
            **(metadata or {}),
            "proxies_count": len(proxies),
            "servers_count": len(servers),
            "parsed_count": len(servers),
            "invalid_count": sum(1 for item in diagnostics if item.get("category") == "invalid"),
            "exact_duplicate_count": sum(1 for item in diagnostics if item.get("category") == "duplicate_exact"),
            "entry_diagnostics": diagnostics[:50],
        },
    )


DEFAULT_SUBSCRIPTION_ADAPTER = HttpMihomoSubscriptionAdapter()
