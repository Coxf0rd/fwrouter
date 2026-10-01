"""Pinned runtime side of protocol capability intersection.

Only endpoint families with generated-config/native test evidence are admitted.
This is schema support, not connectivity or imported Xray egress capability.
"""
MIHOMO_PIN = "v1.19.31"
MIHOMO_PROTOCOL_FAMILIES = frozenset({"vless", "trojan", "hysteria2", "ss", "wireguard"})
