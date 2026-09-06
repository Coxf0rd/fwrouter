from __future__ import annotations

from urllib.parse import quote, urlencode

from fwrouter_api.core.config import get_settings

XRAY_PUBLIC_PATH = "/vless"
XRAY_PUBLIC_PORT = 443
XRAY_TRANSPORT = "ws"
XRAY_SUBSCRIPTION_ALPN = "http/1.1"
XRAY_SUBSCRIPTION_FP = "chrome"
XRAY_SUBSCRIPTION_PACKET_ENCODING = "xudp"


def configured_xray_public_endpoint() -> dict[str, object]:
    settings = get_settings()
    public_path = str(settings.xray_public_path or XRAY_PUBLIC_PATH).strip() or XRAY_PUBLIC_PATH
    if not public_path.startswith("/"):
        public_path = f"/{public_path}"
    return {
        "host": str(settings.xray_public_host or "").strip() or "localhost",
        "port": int(settings.xray_public_port or XRAY_PUBLIC_PORT),
        "path": public_path,
    }


def build_xray_vless_uri(
    *,
    client_uuid: str,
    label: str,
    public_host: str | None = None,
    public_port: int | None = None,
    public_path: str | None = None,
) -> str:
    configured = configured_xray_public_endpoint()
    host = str(public_host or configured["host"]).strip() or "localhost"
    port = int(public_port or configured["port"] or XRAY_PUBLIC_PORT)
    path = str(public_path or configured["path"] or XRAY_PUBLIC_PATH).strip() or XRAY_PUBLIC_PATH
    if not path.startswith("/"):
        path = f"/{path}"
    params = {
        "encryption": "none",
        "security": "tls",
        "sni": host,
        "type": XRAY_TRANSPORT,
        "host": host,
        "path": path,
        "alpn": XRAY_SUBSCRIPTION_ALPN,
        "fp": XRAY_SUBSCRIPTION_FP,
        "packetEncoding": XRAY_SUBSCRIPTION_PACKET_ENCODING,
    }
    return (
        f"vless://{client_uuid}@{host}:{port}"
        f"?{urlencode(params)}"
        f"#{quote(label, safe='')}"
    )
