from __future__ import annotations

from datetime import datetime, timezone


OWNED_TABLE = "inet fwrouter_v2"
REQUIRED_CHAINS = (
    "prerouting",
    "input",
    "output",
    "forward",
    "postrouting",
    "fwrouter_classify",
    "fwrouter_direct",
    "fwrouter_vpn",
    "fwrouter_vpn_full",
)

STATIC_SECURE_DNS_BYPASS_IPV4 = (
    "1.1.1.1",
    "1.0.0.1",
    "1.1.1.2",
    "1.0.0.2",
    "162.159.61.3",
    "162.159.61.4",
    "172.64.41.3",
    "172.64.41.4",
    "8.8.8.8",
    "8.8.4.4",
    "9.9.9.9",
    "149.112.112.112",
    "94.140.14.14",
    "94.140.15.15",
    "208.67.222.222",
    "208.67.220.220",
)
CONTROL_PLANE_INPUT_PORTS = frozenset({22, 53, 67, 68, 5000, 5055, 5200, 5201, 5202, 5203, 5204, 5205})
ROOT_UID = 0


def _derive_tcp_redirect_mark_hex(vpn_fwmark_hex: str) -> str:
    return _derive_mark_hex(vpn_fwmark_hex, offset=1)


def _derive_mark_hex(vpn_fwmark_hex: str, *, offset: int) -> str:
    try:
        value = int(str(vpn_fwmark_hex), 16)
    except ValueError:
        value = 0x100
    return f"0x{value + offset:08x}"


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()
