from __future__ import annotations

import json
from pathlib import Path

from _test_support import configure_test_state_dir
from fwrouter_api.adapters.xray import RealXrayAdapter
from fwrouter_api.db.connection import initialize_database
from fwrouter_api.services import xray_subscription_service
from test_xray import (
    _FakeRunner,
    _build_adapter,
    _enable_xray_module,
    _patch_runtime,
    _patch_xray_adapters,
    _write_xray_config,
    _xray_paths,
)


class _PythonComponentXrayAdapter:
    """Use the real config adapter while keeping native generation checks separate."""

    def __init__(self, component: RealXrayAdapter) -> None:
        self._component = component

    def __getattr__(self, name: str):
        if name == "stage_subscription_generation":
            raise AttributeError(name)
        return getattr(self._component, name)


def _install_runtime_boundaries(monkeypatch, adapter: _PythonComponentXrayAdapter) -> None:
    monkeypatch.setattr(xray_subscription_service, "_xray_adapter", lambda: adapter)
    monkeypatch.setattr(
        xray_subscription_service,
        "_materialize_xray_runtime_bindings",
        lambda **_kwargs: {"ok": True, "status": "applied"},
    )
    monkeypatch.setattr(
        "fwrouter_api.services.mihomo_config.reconcile_mihomo_runtime",
        lambda **_kwargs: {"ok": True, "reconcile_action": "none"},
    )


def test_vpn_auto_reconcile_removes_stale_identity_and_recreates_one_stable_identity(
    monkeypatch, tmp_path: Path,
) -> None:
    configure_test_state_dir(monkeypatch, tmp_path)
    initialize_database()
    _patch_runtime(monkeypatch)
    _enable_xray_module()

    stale_email = xray_subscription_service._vpn_auto_xray_client_email("server-gone")
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [
        {"id": "stale-uuid", "email": stale_email},
        {"id": "manual-uuid", "email": "manual@example.test"},
    ])
    runner = _FakeRunner()
    component = _build_adapter(tmp_path, runner=runner)
    _patch_xray_adapters(monkeypatch, component)
    adapter = _PythonComponentXrayAdapter(component)
    _install_runtime_boundaries(monkeypatch, adapter)

    removed = xray_subscription_service.reconcile_xray_vpn_auto_subscription(requested_by="pytest")

    assert removed["ok"] is True
    assert removed["deleted_count"] == 1
    clients = json.loads(config_path.read_text(encoding="utf-8"))["inbounds"][0]["settings"]["clients"]
    assert stale_email not in {client["email"] for client in clients}
    assert "manual@example.test" in {client["email"] for client in clients}
    virtual_email = xray_subscription_service._vpn_auto_xray_client_email(
        xray_subscription_service.VIRTUAL_XRAY_VPN_AUTO_SERVER_ID
    )
    first_virtual = next(client for client in clients if client["email"] == virtual_email)

    first_return = xray_subscription_service.reconcile_xray_vpn_auto_subscription(requested_by="pytest")
    second_clients = json.loads(config_path.read_text(encoding="utf-8"))["inbounds"][0]["settings"]["clients"]
    second_virtual = next(client for client in second_clients if client["email"] == virtual_email)

    assert first_return["ok"] is True
    assert first_return["created_count"] == 0
    assert first_return["deleted_count"] == 0
    assert second_virtual["id"] == first_virtual["id"]
    assert len([client for client in second_clients if client["email"] == virtual_email]) == 1
