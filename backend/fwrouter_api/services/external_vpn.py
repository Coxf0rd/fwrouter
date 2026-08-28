from __future__ import annotations

import json
import socket
import sqlite3
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen

from fwrouter_api.db.connection import db_session
from fwrouter_api.services.live_probe_cache import get_live_probe_cache


UI_DISPLAY_SETTINGS_KEY = "ui.admin_client_display.v1"


def _json_loads(value: str | None) -> dict[str, Any] | None:
    if not value:
        return None
    try:
        loaded = json.loads(value)
    except json.JSONDecodeError:
        return None
    return loaded if isinstance(loaded, dict) else None


def _slugify_system_id(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    result = []
    previous_dash = False
    for char in normalized:
        if char.isalnum():
            result.append(char)
            previous_dash = False
        elif char == "_":
            result.append("_")
            previous_dash = False
        elif char in {"-", ".", ":"} and not previous_dash:
            result.append("-")
            previous_dash = True
    return "".join(result).strip("-")[:64]


def _int_port(value: Any) -> int | None:
    if isinstance(value, int):
        candidate = value
    else:
        raw = str(value or "").strip()
        if not raw.isdigit():
            return None
        candidate = int(raw)
    return candidate if 1 <= candidate <= 65535 else None


def _load_display_settings() -> dict[str, Any]:
    try:
        with db_session() as connection:
            row = connection.execute(
                "SELECT value_json FROM settings WHERE key = ?",
                (UI_DISPLAY_SETTINGS_KEY,),
            ).fetchone()
    except sqlite3.OperationalError:
        return {}
    return _json_loads(row["value_json"]) if row else {}


def _http_healthcheck_ready(url: str) -> bool:
    try:
        with urlopen(url, timeout=1.0) as response:
            if response.status >= 400:
                return False
            content_type = response.headers.get("content-type", "")
            raw = response.read(8192)
    except (OSError, URLError, ValueError):
        return False
    if "json" not in content_type.lower():
        return True
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    status = str(payload.get("status") or payload.get("state") or "ok").strip().lower()
    return status in {"ok", "ready", "running", "healthy", "degraded"}


def _tcp_port_ready(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def _external_vpn_runtime_ready(module: dict[str, Any]) -> bool:
    endpoints = module.get("endpoints") if isinstance(module.get("endpoints"), dict) else {}
    healthcheck_url = str(endpoints.get("healthcheck_url") or "").strip()
    if healthcheck_url:
        return _http_healthcheck_ready(healthcheck_url)
    redir_port = module.get("redir_port")
    return isinstance(redir_port, int) and _tcp_port_ready(redir_port)


def _active_external_vpn_module_uncached() -> dict[str, Any] | None:
    """Return the first configured external VPN module with transparent ports.

    The adapter intentionally ignores HTTP/SOCKS-only records: FWRouter's owned
    nft dataplane can hand off traffic only to local redir/tproxy-style
    transparent endpoints.
    """

    from fwrouter_api.services.external_connections_registry import list_external_connections

    settings = _load_display_settings()
    systems = list_external_connections(enabled_only=True)
    visibility = settings.get("system_visibility")
    visibility = visibility if isinstance(visibility, dict) else {}

    for item in systems:
        if not isinstance(item, dict):
            continue
        if str(item.get("connection_type") or "").strip().lower() != "external_vpn_module":
            continue
        connection_id = str(item.get("connection_id") or "").strip()
        if not connection_id:
            continue
        system_id = _slugify_system_id(item.get("system_id") or item.get("connection_id"))
        if system_id and visibility.get(system_id) is False:
            continue
        endpoints = item.get("endpoints")
        if not isinstance(endpoints, dict):
            continue

        redir_port = _int_port(endpoints.get("tcp_redir_port"))
        tproxy_port = _int_port(endpoints.get("udp_tproxy_port"))
        if redir_port is None or tproxy_port is None:
            continue

        full_redir_port = _int_port(endpoints.get("full_tcp_redir_port")) or redir_port
        full_tproxy_port = _int_port(endpoints.get("full_udp_tproxy_port")) or tproxy_port
        module = {
            "connection_id": connection_id,
            "system_id": system_id,
            "label": str(item.get("label") or system_id or "External VPN").strip(),
            "runtime_type": str(item.get("runtime_type") or "generic").strip(),
            "location": str(item.get("location") or "manual").strip(),
            "address": str(item.get("address") or "").strip(),
            "capabilities": dict(item.get("capabilities") if isinstance(item.get("capabilities"), dict) else {}),
            "endpoints": dict(endpoints),
            "redir_port": redir_port,
            "tproxy_port": tproxy_port,
            "full_vpn_redir_port": full_redir_port,
            "full_vpn_tproxy_port": full_tproxy_port,
        }
        if _external_vpn_runtime_ready(module):
            return module

    return None


def active_external_vpn_module() -> dict[str, Any] | None:
    return get_live_probe_cache(
        "external_vpn.active_module",
        ttl_seconds=2.0,
        loader=_active_external_vpn_module_uncached,
    )


def build_external_vpn_contour(module: dict[str, Any]) -> dict[str, Any]:
    return {
        "adapter": "external_vpn_module",
        "source": "external_connections",
        "connection_id": module["connection_id"],
        "system_id": module["system_id"],
        "label": module["label"],
        "runtime_type": module["runtime_type"],
        "location": module["location"],
        "address": module["address"],
        "mode": "tproxy",
        "redir_port": module["redir_port"],
        "tproxy_port": module["tproxy_port"],
        "full_vpn_redir_port": module["full_vpn_redir_port"],
        "full_vpn_tproxy_port": module["full_vpn_tproxy_port"],
        "capabilities": dict(module.get("capabilities") if isinstance(module.get("capabilities"), dict) else {}),
        "endpoints": dict(module.get("endpoints") if isinstance(module.get("endpoints"), dict) else {}),
    }


def external_vpn_mihomo_reconcile_skip() -> dict[str, Any] | None:
    module = active_external_vpn_module()
    if module is None:
        return None
    return {
        "ok": True,
        "skipped": True,
        "adapter": "external_vpn_module",
        "reconcile_action": "none",
        "reconcile_reason": "external_vpn_module_owns_vpn_egress",
        "external_vpn_module": build_external_vpn_contour(module),
    }
