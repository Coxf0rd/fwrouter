from __future__ import annotations

from typing import Any

import httpx
import yaml

from fwrouter_api.adapters.mihomo import (
    DEFAULT_MIHOMO_ADAPTER,
    MihomoHealth,
    MihomoRuntimeState,
    MihomoServer,
)
from fwrouter_api.services.modules import get_module_state
from fwrouter_api.services.servers import sync_servers_from_mihomo

_MIHOMO_RUNTIME_ERRORS = (httpx.HTTPError, OSError, ValueError, yaml.YAMLError)


def _server_to_dict(server: MihomoServer) -> dict[str, Any]:
    return {
        "server_id": server.server_id,
        "server_name": server.server_name,
        "provider_name": server.provider_name,
        "raw": server.raw,
    }


def get_mihomo_status() -> dict[str, Any]:
    """Return read-only Mihomo runtime status.

    This does not switch servers, refresh subscriptions or apply dataplane.
    """

    health = DEFAULT_MIHOMO_ADAPTER.health()
    servers: list[MihomoServer] = []
    server_inventory_details: dict[str, Any] = {"ok": True}
    try:
        servers = DEFAULT_MIHOMO_ADAPTER.list_servers()
    except _MIHOMO_RUNTIME_ERRORS as exc:
        server_inventory_details = {
            "ok": False,
            "error": str(exc),
            "error_type": type(exc).__name__,
        }
        health_details = dict(health.details)
        health_details["server_inventory"] = server_inventory_details
        health = MihomoHealth(
            runtime_state=MihomoRuntimeState.DEGRADED,
            active_server_id=health.active_server_id,
            message="Mihomo controller is not reachable while reading server inventory.",
            details=health_details,
        )
    module = get_module_state("vpn")

    return {
        "runtime_state": health.runtime_state.value,
        "active_server_id": health.active_server_id,
        "message": health.message,
        "module": module,
        "details": {
            **health.details,
            "server_inventory": server_inventory_details,
        },
        "servers_count": len(servers),
        "servers": [_server_to_dict(server) for server in servers],
        "read_only": True,
    }


def sync_mihomo_inventory() -> dict[str, Any]:
    """Sync read-only Mihomo server inventory into SQLite."""

    return sync_servers_from_mihomo()
