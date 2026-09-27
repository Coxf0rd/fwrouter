from __future__ import annotations

from fwrouter_api.adapters.xray import XrayApplyResult, XrayClient
from fwrouter_api.services import xray_subscription_service


class _MemoryXrayAdapter:
    def __init__(self, clients: list[XrayClient] | None = None) -> None:
        self.clients = {str(client.email): client for client in clients or []}
        self.deleted: list[str] = []
        self.created: list[str] = []

    def list_clients(self) -> list[XrayClient]:
        return list(self.clients.values())

    def delete_client(self, client_id: str) -> XrayApplyResult:
        client = next(
            (item for item in self.clients.values() if item.client_id == client_id or item.client_uuid == client_id),
            None,
        )
        if client is None:
            return XrayApplyResult(ok=False, message="missing", error_code="XRAY_CLIENT_NOT_FOUND")
        self.deleted.append(str(client.email))
        self.clients.pop(str(client.email), None)
        return XrayApplyResult(ok=True, message="deleted")

    def create_client(self, *, alias: str, email: str) -> XrayApplyResult:
        self.created.append(email)
        stable_id = f"uuid:{email}"
        self.clients[email] = XrayClient(
            client_id=stable_id,
            client_uuid=stable_id,
            email=email,
            alias=alias,
        )
        return XrayApplyResult(
            ok=True,
            message="created",
            details={"client": {"client_id": stable_id, "client_uuid": stable_id}},
        )


def _install_reconcile_stubs(monkeypatch, adapter: _MemoryXrayAdapter, desired: list[dict[str, str]]) -> None:
    monkeypatch.setattr(
        xray_subscription_service,
        "_module_state",
        lambda _name: {"desired_state": "enabled", "lifecycle_mode": "managed"},
    )
    monkeypatch.setattr(xray_subscription_service, "_vpn_auto_servers_for_xray_subscription", lambda: list(desired))
    monkeypatch.setattr(xray_subscription_service, "_xray_adapter", lambda: adapter)
    monkeypatch.setattr(xray_subscription_service, "_sync_xray_inventory", lambda *_args, **_kwargs: {"ok": True})
    monkeypatch.setattr(xray_subscription_service, "_set_local_alias", lambda *_args: None)
    monkeypatch.setattr(
        xray_subscription_service,
        "_xray_subject_for_client",
        lambda client_id: {"subject_id": f"subject:{client_id}"},
    )
    monkeypatch.setattr(xray_subscription_service, "_upsert_xray_subject_server_override", lambda **_kwargs: None)
    monkeypatch.setattr(
        xray_subscription_service,
        "cleanup_xray_client_projection",
        lambda client_id: {"subjects_deleted": 1, "subject_ids": [f"subject:{client_id}"]},
    )
    monkeypatch.setattr(
        xray_subscription_service,
        "reconcile_xray_subscription_profile_nodes",
        lambda **_kwargs: {"ok": True, "status": "success", "nodes_count": 1},
    )
    monkeypatch.setattr(
        "fwrouter_api.services.mihomo_config.reconcile_mihomo_runtime",
        lambda **_kwargs: {"ok": True, "reconcile_action": "none"},
    )
    monkeypatch.setattr(
        xray_subscription_service,
        "_materialize_xray_runtime_bindings",
        lambda **_kwargs: {"ok": True, "status": "applied"},
    )


def test_vpn_auto_reconcile_removes_stale_identity_and_recreates_one_stable_identity(monkeypatch) -> None:
    stale_email = xray_subscription_service._vpn_auto_xray_client_email("server-gone")
    profile = XrayClient(
        client_id="profile-uuid",
        client_uuid="profile-uuid",
        email="sub-profile@example.test",
        alias="Working profile",
    )
    adapter = _MemoryXrayAdapter(
        [
            XrayClient(
                client_id="stale-uuid",
                client_uuid="stale-uuid",
                email=stale_email,
                alias="Stale server",
            ),
            profile,
        ]
    )
    desired: list[dict[str, str]] = []
    _install_reconcile_stubs(monkeypatch, adapter, desired)

    removed = xray_subscription_service.reconcile_xray_vpn_auto_subscription(requested_by="pytest")

    assert removed["ok"] is True
    assert removed["deleted_count"] == 1
    assert list(adapter.clients) == [profile.email]

    desired.append({"server_id": "server-gone", "server_name": "Returned server"})
    first_return = xray_subscription_service.reconcile_xray_vpn_auto_subscription(requested_by="pytest")
    second_return = xray_subscription_service.reconcile_xray_vpn_auto_subscription(requested_by="pytest")

    assert first_return["ok"] is True
    assert first_return["created_count"] == 1
    assert second_return["ok"] is True
    assert second_return["created_count"] == 0
    assert second_return["deleted_count"] == 0
    assert adapter.created == [stale_email]
    assert list(adapter.clients).count(stale_email) == 1
    assert adapter.clients[profile.email] == profile
