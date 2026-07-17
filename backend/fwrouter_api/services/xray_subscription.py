from __future__ import annotations

from urllib.parse import quote, urlencode


XRAY_PUBLIC_HOST = "xray.minisk.ru"
XRAY_PUBLIC_PATH = "/vless"
XRAY_PUBLIC_PORT = 443
XRAY_TRANSPORT = "ws"
XRAY_SUBSCRIPTION_ALPN = "http/1.1"
XRAY_SUBSCRIPTION_FP = "chrome"
XRAY_SUBSCRIPTION_PACKET_ENCODING = "xudp"


def build_xray_vless_uri(*, client_uuid: str, label: str) -> str:
    params = {
        "encryption": "none",
        "security": "tls",
        "sni": XRAY_PUBLIC_HOST,
        "type": XRAY_TRANSPORT,
        "host": XRAY_PUBLIC_HOST,
        "path": XRAY_PUBLIC_PATH,
        "alpn": XRAY_SUBSCRIPTION_ALPN,
        "fp": XRAY_SUBSCRIPTION_FP,
        "packetEncoding": XRAY_SUBSCRIPTION_PACKET_ENCODING,
    }
    return (
        f"vless://{client_uuid}@{XRAY_PUBLIC_HOST}:{XRAY_PUBLIC_PORT}"
        f"?{urlencode(params)}"
        f"#{quote(label, safe='')}"
    )
