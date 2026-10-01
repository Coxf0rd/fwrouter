"""Shared immutable hooks for endpoint protocol integrations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


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
    """Sanitized result from a format parser or projection hook."""

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
    parse_ini: Callable[[str], ProtocolHookResult] | None = None
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


def capabilities(
    protocol: str,
    security: str,
    formats: tuple[str, ...],
    *,
    transport: str = "per-entry native Mihomo validation required",
) -> ProtocolIntegrationCapabilities:
    return ProtocolIntegrationCapabilities(
        protocol=protocol,
        security=security,
        source_import_formats=formats,
        mihomo_projection="supported",
        mihomo_native_validation_required=True,
        native_xray_egress_projection="not_implemented",
        public_profile_export_formats=(),
        public_profile_export_scope="not_applicable",
        health_latency="canonical_after_apply_via_active_runtime_adapter",
        transport_support=transport,
    )
