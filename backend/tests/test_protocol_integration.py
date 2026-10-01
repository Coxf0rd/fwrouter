from __future__ import annotations

from fwrouter_api.adapters.protocol_integration import (
    PROTOCOL_INTEGRATIONS,
    ProtocolHookResult,
    ProtocolIntegration,
    ProtocolIntegrationCapabilities,
    integration_for_proxy,
    normalize_protocol_proxy,
    parse_uri_protocol_proxy,
    parse_xray_protocol_outbound,
)


def test_vless_reality_contract_keeps_projection_and_health_capabilities_separate() -> None:
    capabilities = integration_for_proxy({"type": "vless", "security": "reality"}).capabilities

    assert "plain_uri_lines" in capabilities.source_import_formats
    assert "json_profile" in capabilities.source_import_formats
    assert capabilities.mihomo_projection == "supported"
    assert capabilities.mihomo_native_validation_required is True
    assert capabilities.native_xray_egress_projection == "not_implemented"
    assert capabilities.health_latency == "canonical_after_apply_via_active_runtime_adapter"
    assert "independent" in capabilities.public_profile_export_scope


def test_protocol_dispatch_accepts_a_new_local_definition_without_dispatch_changes() -> None:
    def matches_custom(proxy: dict) -> bool:
        return proxy.get("type") == "custom-test"

    def normalize_custom(proxy: dict) -> dict:
        return {**proxy, "normalized": True}

    custom = ProtocolIntegration(
        capabilities=ProtocolIntegrationCapabilities(
            protocol="custom-test",
            security="custom",
            source_import_formats=("test",),
            mihomo_projection="supported",
            mihomo_native_validation_required=True,
            native_xray_egress_projection="not_implemented",
            public_profile_export_formats=(),
            public_profile_export_scope="not_applicable",
            health_latency="delegated_to_active_runtime_adapter",
            transport_support="per-entry native validation required",
        ),
        matcher=matches_custom,
        normalizer=normalize_custom,
        validator=lambda _proxy: (),
    )

    selected = integration_for_proxy(
        {"type": "custom-test"}, integrations=PROTOCOL_INTEGRATIONS + (custom,)
    )
    normalized = selected.normalize({"type": "custom-test"})

    assert selected is custom
    assert normalized.proxy["normalized"] is True
    assert normalized.issues == ()


def test_vless_reality_normalizer_preserves_optional_string_values() -> None:
    source = {"type": "vless", "security": "reality", "reality-opts": {"short-id": "123e4567"}}
    result = normalize_protocol_proxy(source)

    assert result.proxy["reality-opts"]["short-id"] == "123e4567"
    assert source["reality-opts"]["short-id"] == "123e4567"
    assert result.issues == ()


def test_vless_reality_uri_parser_reports_safe_invalid_endpoint_issue() -> None:
    result = parse_uri_protocol_proxy("vless://secret@example.test:not-a-port?security=reality")

    assert result is not None and result.handled
    assert result.proxy == {}
    assert [(issue.code, issue.field) for issue in result.issues] == [("invalid_endpoint", "uri")]


def test_local_integration_hooks_cover_uri_json_and_mihomo_projection() -> None:
    def matches(proxy: dict) -> bool:
        return proxy.get("type") == "custom-test"

    def parse_uri(uri: str) -> ProtocolHookResult:
        if not uri.startswith("custom-test://"):
            return ProtocolHookResult(handled=False)
        return ProtocolHookResult(
            handled=True,
            proxy={"type": "custom-test", "server": "uri.example", "name": "from uri"},
        )

    def parse_xray(outbound: dict) -> ProtocolHookResult:
        if outbound.get("protocol") != "custom-test":
            return ProtocolHookResult(handled=False)
        return ProtocolHookResult(
            handled=True,
            proxy={"type": "custom-test", "server": "json.example", "name": "from json"},
        )

    def project(proxy: dict) -> ProtocolHookResult:
        return ProtocolHookResult(handled=True, proxy={**proxy, "projected": "mihomo"})

    custom = ProtocolIntegration(
        capabilities=ProtocolIntegrationCapabilities(
            protocol="custom-test",
            security="custom",
            source_import_formats=("plain_uri_lines", "json_profile", "clash_yaml"),
            mihomo_projection="supported",
            mihomo_native_validation_required=True,
            native_xray_egress_projection="not_implemented",
            public_profile_export_formats=(),
            public_profile_export_scope="not_applicable",
            health_latency="delegated_to_active_runtime_adapter",
            transport_support="per-entry native validation required",
        ),
        matcher=matches,
        normalizer=lambda proxy: dict(proxy),
        validator=lambda _proxy: (),
        parse_uri=parse_uri,
        parse_xray_outbound=parse_xray,
        project_mihomo=project,
    )
    integrations = (custom,)

    uri = parse_uri_protocol_proxy("custom-test://endpoint", integrations=integrations)
    json = parse_xray_protocol_outbound({"protocol": "custom-test"}, integrations=integrations)

    assert uri is not None and uri.handled and uri.issues == ()
    assert uri.proxy["server"] == "uri.example"
    assert uri.proxy["projected"] == "mihomo"
    assert json is not None and json.handled and json.issues == ()
    assert json.proxy["server"] == "json.example"
    assert json.proxy["projected"] == "mihomo"


def test_hysteria2_uri_projects_only_supported_uri_fields_to_mihomo() -> None:
    result = parse_uri_protocol_proxy(
        "hysteria2://test-password@hy2.example:8443/?sni=edge.example#Norway%20%F0%9F%87%B3%F0%9F%87%B4"
    )

    assert result is not None and result.handled and result.issues == ()
    assert result.proxy == {
        "name": "Norway 🇳🇴",
        "type": "hysteria2",
        "server": "hy2.example",
        "port": 8443,
        "password": "test-password",
        "sni": "edge.example",
    }
    capabilities = integration_for_proxy(result.proxy).capabilities
    assert capabilities.mihomo_projection == "supported"
    assert capabilities.mihomo_native_validation_required is True
    assert capabilities.native_xray_egress_projection == "not_implemented"
    assert capabilities.public_profile_export_formats == ()

    from fwrouter_api.adapters.subscription import _proxy_from_uri

    imported, error = _proxy_from_uri(
        "hysteria2://test-password@hy2.example:8443/?sni=edge.example#Norway"
    )
    assert error is None
    assert imported == {
        "name": "Norway",
        "type": "hysteria2",
        "server": "hy2.example",
        "port": 8443,
        "password": "test-password",
        "sni": "edge.example",
    }


def test_hysteria2_uri_rejects_unknown_parameters_without_echoing_values() -> None:
    result = parse_uri_protocol_proxy(
        "hy2://test-password@hy2.example:443/?sni=edge.example&token=private-value#node"
    )

    assert result is not None and result.handled
    assert [(issue.code, issue.field) for issue in result.issues] == [
        ("unsupported_parameter", "uri.query")
    ]
    assert "private-value" not in repr(result.issues)


def test_hysteria2_normalization_rejects_missing_password_and_bad_port() -> None:
    from fwrouter_api.adapters.protocol_integration import normalize_protocol_proxy

    missing_password = normalize_protocol_proxy(
        {"type": "hysteria2", "server": "hy2.example", "port": 443}
    )
    bad_port = normalize_protocol_proxy(
        {"type": "hysteria2", "server": "hy2.example", "port": 70000, "password": "x"}
    )

    assert [(issue.code, issue.field) for issue in missing_password.issues] == [
        ("missing_credential", "password")
    ]
    assert [(issue.code, issue.field) for issue in bad_port.issues] == [
        ("invalid_endpoint", "port")
    ]
