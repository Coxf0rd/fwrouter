from __future__ import annotations

import ast
import json
from pathlib import Path

from fwrouter_api.adapters.protocol_integration import integration_for_proxy
from fwrouter_api.adapters.subscription import SubscriptionRefreshStatus, parse_subscription_payload


def test_uri_subscription_dispatches_each_mixed_entry_and_keeps_reality_as_vless() -> None:
    payload = "\n".join(
        [
            "vless://client-tls@tls.example:443?security=tls&type=grpc&serviceName=svc#TLS",
            "vless://client-reality@reality.example:443?security=reality&pbk=public-key&sid=abcd#REALITY",
            "trojan://password@trojan.example:443?security=tls&sni=front.example#Trojan",
            "hysteria2://password@hy2.example:8443?sni=front.example#Hysteria2",
            "ss://2022-blake3-aes-128-gcm:YWJjZGVmZ2hpamtsbW5vcA==@ss.example:8388#SS2022",
            "unknown+fixture://ignored@example.invalid:443#Unsupported",
        ]
    )

    result = parse_subscription_payload(payload)

    assert result.status == SubscriptionRefreshStatus.SUCCESS
    assert [server.protocol for server in result.servers] == ["vless", "vless", "trojan", "hysteria2", "ss"]
    assert [server.host for server in result.servers] == [
        "tls.example", "reality.example", "trojan.example", "hy2.example", "ss.example"
    ]
    reality = result.servers[1].raw
    assert reality["type"] == "vless"
    assert reality["security"] == "reality"
    assert integration_for_proxy(reality).capabilities.protocol == "vless"
    assert result.metadata["unsupported_count"] == 1


def test_json_profiles_dispatch_per_profile_and_keep_mixed_supported_profiles() -> None:
    def xray_profile(name: str, outbound: dict) -> dict:
        return {"remarks": name, "outbounds": [outbound]}

    profiles = [
        xray_profile(
            "vless reality",
            {
                "protocol": "vless", "tag": "vless", "settings": {"vnext": [{
                    "address": "reality.example", "port": 443,
                    "users": [{"id": "client-id", "encryption": "none"}],
                }]},
                "streamSettings": {"network": "tcp", "security": "reality", "realitySettings": {
                    "serverName": "front.example", "publicKey": "public-key", "shortId": "abcd",
                }},
            },
        ),
        xray_profile(
            "trojan",
            {"protocol": "trojan", "settings": {"servers": [{
                "address": "trojan.example", "port": 443, "password": "fixture-password",
            }]}, "streamSettings": {"network": "tcp", "security": "tls"}},
        ),
        xray_profile(
            "shadowsocks 2022",
            {"protocol": "shadowsocks", "settings": {"servers": [{
                "address": "ss.example", "port": 8388, "method": "2022-blake3-aes-128-gcm",
                "password": "YWJjZGVmZ2hpamtsbW5vcA==",
            }]}},
        ),
        xray_profile("unsupported", {"protocol": "vmess", "settings": {}}),
    ]

    result = parse_subscription_payload(json.dumps(profiles))

    assert result.status == SubscriptionRefreshStatus.SUCCESS
    assert [server.protocol for server in result.servers] == ["vless", "trojan", "ss"]
    assert result.servers[0].raw["security"] == "reality"
    assert result.metadata["unsupported_count"] == 1
    assert "fixture-password" not in repr(result.metadata)


def test_invalid_entry_uses_existing_source_failure_diagnostics_without_exposing_material() -> None:
    payload = "\n".join(
        [
            "trojan://valid-password@valid.example:443?security=tls#valid",
            "vless://sensitive-id@invalid.example:443?security=xtls&type=quic#invalid",
        ]
    )

    result = parse_subscription_payload(payload)

    assert result.status == SubscriptionRefreshStatus.FAILED
    assert result.error_code == "SUBSCRIPTION_PROTOCOL_ENTRY_INVALID"
    assert result.metadata["parsed_count"] == 1
    assert result.metadata["protocol_validation_failure_count"] == 1
    assert result.metadata["entry_diagnostics"][0]["category"] == "protocol_validation_failure"
    assert "valid-password" not in repr(result.metadata)
    assert "sensitive-id" not in repr(result.metadata)


def test_provider_managed_and_ordinary_inputs_share_the_same_family_normalization() -> None:
    from fwrouter_api.adapters.stealthsurf_protocols import parse_material

    uri = "hysteria2://fixture-password@hy2.example:8443?sni=front.example#node"
    ordinary = parse_subscription_payload(uri)
    provider = parse_material("hysteria2", {
        "protocol": "hysteria2", "connection_url": uri,
    })

    assert ordinary.status == SubscriptionRefreshStatus.SUCCESS
    assert provider.status == SubscriptionRefreshStatus.SUCCESS
    assert len(ordinary.servers) == len(provider.servers) == 1
    assert ordinary.servers[0] == provider.servers[0]
    assert integration_for_proxy(provider.servers[0].raw).capabilities.protocol == "hysteria2"


def test_protocol_adapter_modules_have_no_provider_or_subscription_ownership_imports() -> None:
    root = Path(__file__).parents[1] / "fwrouter_api" / "adapters"
    files = [root / "protocol_integration.py", *sorted((root / "protocols").glob("*.py"))]
    prohibited_roots = {"stealthsurf", "provider_managed", "provider_adapters", "subscription"}

    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported = {alias.name.split(".")[-1] for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                imported = {(node.module or "").split(".")[-1]}
            else:
                continue
            assert not (imported & prohibited_roots), f"provider/source coupling in {path.name}"


def test_mixed_yaml_includes_wireguard_and_amnezia_without_source_protocol() -> None:
    payload = (Path(__file__).parent / 'fixtures/protocol_adapters/native_protocols.yaml').read_text()
    result = parse_subscription_payload(payload)
    assert result.status == SubscriptionRefreshStatus.SUCCESS
    assert [server.protocol for server in result.servers] == [
        'vless', 'trojan', 'hysteria2', 'ss', 'wireguard', 'wireguard'
    ]
    assert all(integration_for_proxy(server.raw) is not None for server in result.servers)
    assert result.servers[-1].raw['amnezia-wg-option']['version'] == 2
