"""Protocol-specific subscription contracts and their small generic dispatcher.

Each protocol implementation lives under ``adapters.protocols``. Runtime
health/latency remains owned by the active runtime adapter.
"""

from __future__ import annotations

from typing import Any

from fwrouter_api.adapters.protocols.base import (
    ProtocolHookResult,
    ProtocolIntegration,
    ProtocolIntegrationCapabilities,
    ProtocolNormalizationResult,
    ProtocolValidationIssue,
)
from fwrouter_api.adapters.protocols.hysteria2 import INTEGRATION as HYSTERIA2
from fwrouter_api.adapters.protocols.shadowsocks2022 import INTEGRATION as SHADOWSOCKS2022
from fwrouter_api.adapters.protocols.trojan import INTEGRATION as TROJAN
from fwrouter_api.adapters.protocols.vless import INTEGRATION as VLESS_REALITY
from fwrouter_api.adapters.protocols.wireguard import INTEGRATION as WIREGUARD


PROTOCOL_INTEGRATIONS: tuple[ProtocolIntegration, ...] = (
    VLESS_REALITY,
    HYSTERIA2,
    TROJAN,
    SHADOWSOCKS2022,
    WIREGUARD,
)


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
        try:
            result = hook(value)
        except (ValueError, TypeError):
            return ProtocolNormalizationResult(
                proxy={}, issues=(ProtocolValidationIssue("invalid_protocol_payload", hook_name),), handled=True
            )
        if not result.handled:
            continue
        if result.issues:
            return ProtocolNormalizationResult(proxy=result.proxy or {}, issues=result.issues, handled=True)
        if result.proxy is None:
            return ProtocolNormalizationResult(
                proxy={}, issues=(ProtocolValidationIssue("invalid_endpoint", hook_name),), handled=True
            )
        normalized = normalize_protocol_proxy(result.proxy, integrations=(integration,))
        return ProtocolNormalizationResult(proxy=normalized.proxy, issues=normalized.issues, handled=True)
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
    return _dispatch_format_hook(outbound, hook_name="parse_xray_outbound", integrations=integrations)


def parse_ini_protocol_payload(
    text: str,
    *,
    integrations: tuple[ProtocolIntegration, ...] | None = None,
) -> ProtocolNormalizationResult | None:
    return _dispatch_format_hook(text, hook_name="parse_ini", integrations=integrations)


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
    "TROJAN",
    "SHADOWSOCKS2022",
    "WIREGUARD",
    "integration_for_proxy",
    "normalize_protocol_proxy",
    "parse_uri_protocol_proxy",
    "parse_xray_protocol_outbound",
    "parse_ini_protocol_payload",
    "protocol_capabilities_for_proxy",
]
