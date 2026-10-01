from __future__ import annotations

import os
import json
from pathlib import Path
import subprocess

import pytest
import yaml

from fwrouter_api.adapters.protocol_integration import normalize_protocol_proxy
from fwrouter_api.adapters.subscription import parse_subscription_payload
from fwrouter_api.adapters.stealthsurf_protocols import profile_for
from fwrouter_api.services import mihomo_config as mihomo_config_service
from fwrouter_api.services.subscription import _upsert_subscription_servers
from fwrouter_api.services.provider_managed import _normalized_refresh


FIXTURES = Path(__file__).parent / "fixtures" / "protocol_adapters"
MIHOMO = Path(os.environ.get("FWROUTER_TEST_MIHOMO_BINARY", "/tmp/fwrouter-mihomo-v1.19.31/mihomo"))
EXPECTED_BINARY_VERSION = "Mihomo Meta v1.19.31"


@pytest.fixture(scope="module")
def parsed_family_servers():
    payload = (FIXTURES / "native_protocols.yaml").read_text(encoding="utf-8")
    parsed = parse_subscription_payload(payload, metadata={"content_type": "application/yaml"})
    assert parsed.ok, parsed.to_dict()
    return {server.server_name: server for server in parsed.servers}


def _assert_pinned_binary() -> None:
    assert MIHOMO.is_file(), f"pinned Mihomo binary is required: {MIHOMO}"
    result = subprocess.run([str(MIHOMO), "-v"], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith(EXPECTED_BINARY_VERSION + " ")


def _run_native_config(config_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(MIHOMO), "-t", "-d", str(config_path.parent), "-f", str(config_path)],
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
        cwd=str(config_path.parent),
    )


@pytest.mark.parametrize(
    "selection_key,wire_id,family",
    [
        ("vless_variant_2410", "vless-2410", "vless"),
        ("trojan_variant_2901", "trojan-2901", "trojan"),
    ],
)
def test_documented_provider_aliases_map_to_family_without_implied_transport_or_security(
    selection_key: str, wire_id: str, family: str
) -> None:
    profile = profile_for(selection_key)

    assert profile.wire_id == wire_id
    assert profile.family == family



def _provider_material(proxy: dict, format_name: str) -> dict:
    if format_name == "yaml":
        return {"connection_url": yaml.safe_dump({"proxies": [proxy]}, sort_keys=False)}
    protocol = proxy["type"]
    if format_name == "json":
        stream = {"network": "tcp", "security": "tls"}
        if protocol == "vless":
            settings = {"vnext": [{"address": proxy["server"], "port": proxy["port"],
                "users": [{"id": proxy["uuid"], "flow": proxy.get("flow", "")}]}]}
            stream.update(security="reality", realitySettings={"serverName": proxy["servername"],
                "publicKey": proxy["reality-opts"]["public-key"], "shortId": proxy["reality-opts"]["short-id"], "fingerprint": "chrome"})
        else:
            settings = {"servers": [{"address": proxy["server"], "port": proxy["port"], "password": proxy["password"]}]}
            stream["tlsSettings"] = {"serverName": proxy["sni"]}
        return {"xray_config": json.dumps({"outbounds": [{"protocol": protocol,
            "settings": settings, "streamSettings": stream}]})}
    if format_name == "uri":
        if protocol == "hysteria2":
            uri = f"hysteria2://{proxy['password']}@{proxy['server']}:{proxy['port']}?sni={proxy['sni']}"
        else:
            uri = f"ss://{proxy['cipher']}:{proxy['password']}@{proxy['server']}:{proxy['port']}"
        return {"connection_url": uri}
    interface = ["[Interface]", f"PrivateKey = {proxy['private-key']}", f"Address = {proxy['ip']}"]
    for key, value in proxy.get("amnezia-wg-option", {}).items():
        if key != "version":
            interface.append(f"{key.capitalize()} = {value}")
    peer = ["[Peer]", f"PublicKey = {proxy['public-key']}", f"Endpoint = {proxy['server']}:{proxy['port']}",
            "AllowedIPs = 0.0.0.0/0", "PersistentKeepalive = 25"]
    return {"awg_config": "\n".join(interface + peer)}

@pytest.mark.parametrize(
    "fixture_name,expected_type,provider_variant,material_format",
    [
        ("fixture-vless-reality", "vless", "vless", "yaml"),
        ("fixture-vless-reality", "vless", "vless_variant_2410", "yaml"),
        ("fixture-trojan-tls", "trojan", "trojan", "yaml"),
        ("fixture-trojan-tls", "trojan", "trojan_variant_2901", "yaml"),
        ("fixture-hysteria2", "hysteria2", "hysteria2", "yaml"),
        ("fixture-shadowsocks-2022", "ss", "shadowsocks2022", "yaml"),
        ("fixture-wireguard", "wireguard", "wireguard", "yaml"),
        ("fixture-amneziawg-v2", "wireguard", "amneziawg2", "yaml"),
        ("fixture-vless-reality", "vless", "vless_variant_2410", "json"),
        ("fixture-trojan-tls", "trojan", "trojan_variant_2901", "json"),
        ("fixture-hysteria2", "hysteria2", "hysteria2", "uri"),
        ("fixture-shadowsocks-2022", "ss", "shadowsocks2022", "uri"),
        ("fixture-wireguard", "wireguard", "wireguard", "ini"),
        ("fixture-amneziawg-v2", "wireguard", "amneziawg2", "ini"),
    ],
)
def test_parsed_protocol_generates_config_accepted_by_pinned_mihomo(
    fixture_name: str,
    expected_type: str,
    provider_variant: str,
    material_format: str,
    parsed_family_servers,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Exercise parser -> SubscriptionServer -> isolated inventory -> production candidate -> native -t."""
    _assert_pinned_binary()
    server = parsed_family_servers[fixture_name]
    material = _provider_material(server.raw, material_format)
    binding = {"provider_id": "stealthsurf", "protocol": provider_variant,
               "logical_server_id": "provider:" + fixture_name, "source_ref": fixture_name}
    server = _normalized_refresh(binding, {**material, "protocol": provider_variant,
                                          "server_id": 1456}).servers[0]
    assert server.raw.get("type") == expected_type
    if provider_variant is not None:
        assert profile_for(provider_variant).family == expected_type
    if fixture_name == "fixture-amneziawg-v2":
        assert server.raw["amnezia-wg-option"]["version"] == 2
        assert not any(key in server.raw["amnezia-wg-option"] for key in (
            "header-protection-key", "content-padding-addition", "rekey-after-time"
        ))
    if fixture_name == "fixture-trojan-tls":
        assert "network" not in server.raw
        assert server.raw.get("tls", True) is True

    _upsert_subscription_servers([server])
    monkeypatch.setattr(mihomo_config_service, "_collect_xray_handoff_assignments", lambda: [])
    monkeypatch.setattr(
        mihomo_config_service,
        "_resolve_transparent_bind_address",
        lambda: mihomo_config_service.TRANSPARENT_BIND_ADDRESS,
    )

    result = mihomo_config_service.write_mihomo_candidate_config(
        include_internal_config=True,
        candidate_path=tmp_path / f"{fixture_name}.yaml",
        xray_handoff_assignments=[],
    )
    generated = result["_candidate_config"]
    selected = next(
        proxy for proxy in generated["proxies"] if proxy.get("type") == expected_type and proxy.get("server") == server.host
    )
    assert selected["type"] == expected_type
    if fixture_name == "fixture-vless-reality":
        assert selected["reality-opts"]["short-id"] == "0123456789abcdef"
    elif fixture_name == "fixture-trojan-tls":
        assert selected["password"] == "fixture-trojan-password"
    elif fixture_name == "fixture-hysteria2":
        assert selected["sni"] == "hy2.example.invalid"
    elif fixture_name == "fixture-shadowsocks-2022":
        assert selected["cipher"] == "2022-blake3-aes-128-gcm"
    elif fixture_name == "fixture-wireguard":
        assert "amnezia-wg-option" not in selected
    elif fixture_name == "fixture-amneziawg-v2":
        assert selected["amnezia-wg-option"]["version"] == 2

    completed = _run_native_config(Path(result["candidate_path"]))
    assert completed.returncode == 0, f"{completed.stdout}\n{completed.stderr}"
    assert "configuration file" in completed.stdout.lower() or "configuration file" in completed.stderr.lower()


@pytest.mark.parametrize(
    "proxy",
    [
        {"type": "trojan", "server": "node.example", "port": 443, "password": "x", "network": "xhttp"},
        {"type": "vless", "server": "node.example", "port": 443, "uuid": "00000000-0000-4000-8000-000000000243", "tls": True, "reality-opts": {"public-key": "A" * 43, "short-id": "abcd"}, "network": "quic"},
    ],
)
def test_protocol_adapter_rejects_transport_fields_pinned_native_test_would_ignore(proxy: dict) -> None:
    result = normalize_protocol_proxy(proxy)

    assert [(issue.code, issue.field) for issue in result.issues] == [("unsupported_transport", "network")]


@pytest.mark.parametrize(
    "proxy,expected_error",
    [
        (
            {
                "name": "unsupported-legacy-shadowsocks-type",
                "type": "shadowsocks",
                "server": "192.0.2.99",
                "port": 443,
                "cipher": "2022-blake3-aes-128-gcm",
                "password": "AAAAAAAAAAAAAAAAAAAAAA==",
            },
            "unsupport proxy type",
        ),
        (
            {
                "name": "invalid-vless-reality-missing-public-key",
                "type": "vless",
                "server": "192.0.2.98",
                "port": 443,
                "uuid": "00000000-0000-4000-8000-000000000242",
                "tls": True,
                "reality-opts": {"short-id": "0123456789abcdef"},
            },
            "reality-opts",
        ),
    ],
)
def test_pinned_mihomo_rejects_unsupported_schema_or_incomplete_security(
    proxy: dict,
    expected_error: str,
    tmp_path: Path,
) -> None:
    _assert_pinned_binary()
    config_path = tmp_path / f"{proxy['name']}.yaml"
    config_path.write_text(
        yaml.safe_dump({"proxies": [proxy], "proxy-groups": [], "rules": []}, sort_keys=False),
        encoding="utf-8",
    )

    completed = _run_native_config(config_path)

    assert completed.returncode != 0
    diagnostic = f"{completed.stdout}\n{completed.stderr}".lower()
    assert expected_error in diagnostic
