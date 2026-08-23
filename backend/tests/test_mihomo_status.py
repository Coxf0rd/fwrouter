from __future__ import annotations

import httpx

from fwrouter_api.adapters.mihomo import MihomoHealth, MihomoRuntimeState
from fwrouter_api.services import mihomo as mihomo_service


class _InventoryFailureAdapter:
    def health(self) -> MihomoHealth:
        return MihomoHealth(
            runtime_state=MihomoRuntimeState.RUNNING,
            active_server_id="server-a",
            message="Mihomo controller is reachable.",
            details={"adapter": "test"},
        )

    def list_servers(self) -> list[object]:
        raise httpx.ConnectError("[Errno 111] Connection refused")


def test_get_mihomo_status_degrades_when_server_inventory_is_unreachable(monkeypatch) -> None:
    monkeypatch.setattr(mihomo_service, "DEFAULT_MIHOMO_ADAPTER", _InventoryFailureAdapter())
    monkeypatch.setattr(
        mihomo_service,
        "get_module_state",
        lambda _module_id: {"module_id": "vpn", "lifecycle_mode": "managed"},
    )

    status = mihomo_service.get_mihomo_status()

    assert status["runtime_state"] == "degraded"
    assert status["active_server_id"] == "server-a"
    assert status["servers_count"] == 0
    assert status["servers"] == []
    assert status["details"]["server_inventory"]["ok"] is False
    assert status["details"]["server_inventory"]["error_type"] == "ConnectError"
