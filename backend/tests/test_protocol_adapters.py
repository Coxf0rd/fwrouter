from __future__ import annotations

from fwrouter_api.adapters.protocol_integration import normalize_protocol_proxy, parse_uri_protocol_proxy
from fwrouter_api.adapters.subscription import (
    SubscriptionRefreshStatus,
    detect_subscription_payload,
    parse_subscription_payload,
)


def test_trojan_uri_normalizes_tls_transport_and_rejects_unknown_security() -> None:
    parsed = parse_uri_protocol_proxy(
        "trojan://secret@edge.example:443?security=tls&sni=front.example&type=ws&path=%2Fgate&host=cdn.example#node"
    )
    assert parsed is not None and parsed.issues == ()
    assert parsed.proxy["type"] == "trojan"
    assert parsed.proxy["network"] == "ws"
    assert parsed.proxy["ws-opts"] == {"path": "/gate", "headers": {"Host": "cdn.example"}}
    assert parsed.proxy["sni"] == "front.example"

    rejected = parse_uri_protocol_proxy("trojan://secret@edge.example:443?security=xtls#node")
    assert rejected is not None
    assert [(issue.code, issue.field) for issue in rejected.issues] == [("unsupported_security", "security")]


def test_trojan_and_vless_reality_reject_native_validator_transport_blind_spots() -> None:
    trojan = normalize_protocol_proxy({
        "type": "trojan", "server": "edge.example", "port": 443, "password": "secret", "network": "xhttp"
    })
    vless = normalize_protocol_proxy({
        "type": "vless", "server": "edge.example", "port": 443, "uuid": "id", "security": "reality",
        "network": "quic", "reality-opts": {},
    })
    assert [(issue.code, issue.field) for issue in trojan.issues] == [("unsupported_transport", "network")]
    assert [(issue.code, issue.field) for issue in vless.issues] == [("unsupported_transport", "network")]

    from fwrouter_api.adapters.protocol_integration import parse_xray_protocol_outbound
    xray_trojan = parse_xray_protocol_outbound({
        "protocol": "trojan", "settings": {"servers": [{"address": "edge.example", "port": 443, "password": "secret"}]},
        "streamSettings": {"network": "xhttp", "security": "none"},
    })
    assert xray_trojan is not None
    assert [(issue.code, issue.field) for issue in xray_trojan.issues] == [
        ("unsupported_security", "streamSettings.security")
    ]


def test_shadowsocks2022_uri_projects_to_mihomo_ss() -> None:
    parsed = parse_uri_protocol_proxy(
        "ss://2022-blake3-aes-128-gcm:YWJjZGVmZ2hpamtsbW5vcA==@ss.example:8388#Oslo"
    )
    assert parsed is not None and parsed.issues == ()
    assert parsed.proxy == {
        "type": "ss", "name": "Oslo", "server": "ss.example", "port": 8388,
        "cipher": "2022-blake3-aes-128-gcm", "password": "YWJjZGVmZ2hpamtsbW5vcA==",
    }

    bad_key = parse_uri_protocol_proxy(
        "ss://2022-blake3-aes-128-gcm:YWJj@ss.example:8388#Oslo"
    )
    assert bad_key is not None
    assert [(issue.code, issue.field) for issue in bad_key.issues] == [
        ("invalid_credential", "password")
    ]

    from fwrouter_api.adapters.protocol_integration import parse_xray_protocol_outbound
    xray = parse_xray_protocol_outbound({
        "protocol": "shadowsocks", "tag": "ss", "settings": {"servers": [{
            "address": "ss.example", "port": 8388,
            "method": "2022-blake3-aes-128-gcm", "password": "YWJjZGVmZ2hpamtsbW5vcA==",
        }]},
    })
    assert xray is not None and xray.issues == ()
    assert xray.proxy["type"] == "ss"
    assert xray.proxy["cipher"] == "2022-blake3-aes-128-gcm"


def test_wireguard_yaml_awg2_normalizes_and_rejects_v3_security_fields() -> None:
    source = {
        "type": "wireguard", "name": "wg", "server": "wg.example", "port": 51820,
        "ip": "10.0.0.2/32", "private-key": "private", "public-key": "public", "udp": True,
        "amnezia-wg-option": {"version": 2, "jc": 4, "jmin": 40, "jmax": 80, "s1": 1, "h1": "100-200", "i1": "noise"},
    }
    parsed = normalize_protocol_proxy(source)
    assert parsed.issues == ()
    assert parsed.proxy["amnezia-wg-option"]["version"] == 2

    v3 = normalize_protocol_proxy({**source, "amnezia-wg-option": {"version": 3, "header-protection-key": "sensitive"}})
    assert {issue.code for issue in v3.issues} == {"unsupported_option", "unsupported_security"}
    assert "sensitive" not in repr(v3.issues)


def test_wireguard_ini_dispatch_builds_one_canonical_subscription_server() -> None:
    text = """[Interface]
PrivateKey = private-key
Address = 10.0.0.2/32, fd00::2/128
Jc = 4
Jmin = 40
Jmax = 80
S1 = 1
H1 = 100-200
I1 = noise

[Peer]
PublicKey = public-key
PresharedKey = shared-key
Endpoint = wg.example:51820
AllowedIPs = 0.0.0.0/0, ::/0
PersistentKeepalive = 25
"""
    detection = detect_subscription_payload(text)
    assert detection.detected_format == "wireguard_ini"
    result = parse_subscription_payload(text)
    assert result.status == SubscriptionRefreshStatus.SUCCESS
    assert len(result.servers) == 1
    server = result.servers[0]
    assert (server.protocol, server.host, server.port, server.parser_format) == ("wireguard", "wg.example", 51820, "wireguard_ini")
    assert server.raw["ip"] == "10.0.0.2/32"
    assert server.raw["ipv6"] == "fd00::2/128"
    assert server.raw["amnezia-wg-option"]["version"] == 2
    assert server.raw["amnezia-wg-option"]["h1"] == "100-200"


def test_wireguard_ini_rejects_unsupported_material_without_echoing_values() -> None:
    result = parse_subscription_payload("""[Interface]
PrivateKey = super-secret
Address = 10.0.0.2/32
HeaderProtectionKey = secret-v3
[Peer]
PublicKey = peer-key
Endpoint = wg.example:51820
""")
    assert result.status == SubscriptionRefreshStatus.FAILED
    assert result.error_code == "SUBSCRIPTION_PROTOCOL_ENTRY_INVALID"
    assert "secret-v3" not in repr(result.metadata)


def test_existing_vless_reality_and_hysteria2_uri_adapters_remain_registered() -> None:
    vless = parse_uri_protocol_proxy(
        "vless://id@edge.example:443?security=reality&pbk=public&sid=1234#node"
    )
    hysteria2 = parse_uri_protocol_proxy("hy2://secret@edge.example:443?sni=front.example#node")
    assert vless is not None and vless.issues == ()
    assert vless.proxy["reality-opts"] == {"public-key": "public", "short-id": "1234"}
    assert hysteria2 is not None and hysteria2.issues == ()
    assert hysteria2.proxy["type"] == "hysteria2"


def test_vless_non_reality_uri_is_owned_by_adapter_and_preserves_tls_transport() -> None:
    parsed = parse_uri_protocol_proxy(
        "vless://client-id@edge.example:443?security=tls&type=grpc&serviceName=svc&encryption=none#ordinary"
    )
    assert parsed is not None and parsed.issues == ()
    assert parsed.proxy["type"] == "vless"
    assert parsed.proxy["tls"] is True
    assert parsed.proxy["network"] == "grpc"
    assert parsed.proxy["grpc-opts"] == {"grpc-service-name": "svc"}
    assert parsed.proxy["security"] == "tls"


def test_vless_xray_json_adapter_handles_tls_and_rejects_unknown_security_and_network() -> None:
    from fwrouter_api.adapters.protocol_integration import parse_xray_protocol_outbound

    outbound = {
        "protocol": "vless", "tag": "out", "settings": {"vnext": [{
            "address": "edge.example", "port": 443,
            "users": [{"id": "client-id", "encryption": "none", "flow": ""}],
        }]},
        "streamSettings": {"network": "ws", "security": "tls", "tlsSettings": {"serverName": "front.example"},
                            "wsSettings": {"path": "/gate", "headers": {"Host": "cdn.example"}}},
    }
    parsed = parse_xray_protocol_outbound(outbound)
    assert parsed is not None and parsed.issues == ()
    assert parsed.proxy["tls"] is True
    assert parsed.proxy["servername"] == "front.example"
    assert parsed.proxy["ws-opts"]["path"] == "/gate"

    unsupported = parse_xray_protocol_outbound({
        **outbound, "streamSettings": {"network": "quic", "security": "xtls"}
    })
    assert unsupported is not None
    assert (unsupported.issues[0].code, unsupported.issues[0].field) == (
        "unsupported_security", "streamSettings.security"
    )


def test_subscription_json_profile_uses_vless_protocol_adapter() -> None:
    payload = {
        "remarks": "ordinary JSON profile",
        "outbounds": [{
            "protocol": "vless", "tag": "main", "settings": {"vnext": [{
                "address": "edge.example", "port": 443, "users": [{"id": "client-id", "encryption": "none"}]
            }]},
            "streamSettings": {"network": "tcp", "security": "tls", "tlsSettings": {"serverName": "front.example"}},
        }],
    }
    import json
    result = parse_subscription_payload(json.dumps(payload))
    assert result.status == SubscriptionRefreshStatus.SUCCESS
    assert result.servers[0].raw["type"] == "vless"
    topology = result.servers[0].raw["_fwrouter_topology"]
    assert topology["endpoints"][0]["runtime"]["tls"] is True


def test_shadowsocks_json_cannot_silently_discard_tls_or_websocket():
    from fwrouter_api.adapters.protocol_integration import parse_xray_protocol_outbound
    outbound={'protocol':'shadowsocks','settings':{'servers':[{'address':'edge.example','port':443,
        'method':'2022-blake3-aes-128-gcm','password':'AAAAAAAAAAAAAAAAAAAAAA=='}]}}
    for stream, field in [({'security':'tls'},'streamSettings.security'),({'network':'ws'},'streamSettings.network')]:
        parsed=parse_xray_protocol_outbound({**outbound,'streamSettings':stream})
        assert parsed.issues[0].field==field


def test_malformed_uri_authority_has_sanitized_protocol_failure():
    for protocol in ('vless','trojan','hysteria2','ss'):
        parsed=parse_uri_protocol_proxy(f'{protocol}://fixture-secret@[broken:443')
        assert parsed.issues[0].code=='invalid_protocol_payload'
        assert 'fixture-secret' not in repr(parsed.issues)
