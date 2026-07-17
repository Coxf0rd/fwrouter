from __future__ import annotations

from typing import Any

from fwrouter_api.adapters.mihomo import DEFAULT_MIHOMO_ADAPTER, MihomoServer
from fwrouter_api.services.servers import sync_servers_from_mihomo


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
    servers = DEFAULT_MIHOMO_ADAPTER.list_servers()

    return {
        "runtime_state": health.runtime_state.value,
        "active_server_id": health.active_server_id,
        "message": health.message,
        "details": health.details,
        "servers_count": len(servers),
        "servers": [_server_to_dict(server) for server in servers],
        "read_only": True,
    }


def sync_mihomo_inventory() -> dict[str, Any]:
    """Sync read-only Mihomo server inventory into SQLite."""

    return sync_servers_from_mihomo()
