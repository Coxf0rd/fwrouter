from __future__ import annotations

from threading import Event, Thread

from fwrouter_api.adapters.mihomo import MihomoServer
from fwrouter_api.adapters.xray_common import xray_writer_guard
from fwrouter_api.db.connection import db_session
from fwrouter_api.services import custom_servers, server_inventory
from fwrouter_api.services.vpn_auto_selection_state import (
    commit_active_selection,
    read_selection_revision,
)


def _revision() -> int:
    with db_session() as connection:
        return read_selection_revision(connection)


def _seed_auto_server(server_id: str = "inventory-target") -> None:
    with db_session() as connection:
        connection.execute(
            "INSERT INTO servers (server_id, server_name, provider_name, raw_json, inventory_state) "
            "VALUES (?, ?, 'test', '{\"type\":\"socks5\",\"server\":\"127.0.0.1\",\"port\":1080}', 'active')",
            (server_id, server_id),
        )
        connection.execute(
            "INSERT INTO server_preferences (server_id, vpn_auto, vpn_auto_priority, vpn_auto_priority_origin) "
            "VALUES (?, 1, 1, 'manual')",
            (server_id,),
        )
        connection.execute("INSERT INTO server_ping_state (server_id) VALUES (?)", (server_id,))


def _custom_kwargs(name: str, **kwargs):
    return {
        "server_name": name,
        "host": "proxy.example.test",
        "port": 443,
        "vpn_auto": True,
        "global_list": False,
        **kwargs,
    }


def _stub_custom_runtime(monkeypatch):
    monkeypatch.setattr(custom_servers, "_reconcile_custom_proxy_runtime", lambda **_: None)
    monkeypatch.setattr(
        custom_servers,
        "get_server_api",
        lambda server_id: {
            "server_id": server_id,
            "preferences": {"vpn_auto": True, "global_list": False},
        },
    )


def test_mihomo_sync_marks_missing_eligible_server_and_advances_revision(monkeypatch):
    _seed_auto_server()
    monkeypatch.setattr(
        "fwrouter_api.adapters.mihomo.DEFAULT_MIHOMO_ADAPTER.list_servers",
        lambda: [],
    )
    before = _revision()

    result = server_inventory.sync_servers_from_mihomo()

    assert result["source"] == "mihomo"
    assert _revision() == before + 1
    with db_session() as connection:
        state = connection.execute(
            "SELECT inventory_state FROM servers WHERE server_id='inventory-target'"
        ).fetchone()["inventory_state"]
    assert state == "missing"


def test_mihomo_sync_without_effective_pool_change_keeps_revision(monkeypatch):
    monkeypatch.setattr(
        "fwrouter_api.adapters.mihomo.DEFAULT_MIHOMO_ADAPTER.list_servers",
        lambda: [MihomoServer("new-ineligible", "New", raw={"type": "socks5", "server": "127.0.0.1", "port": 1080})],
    )
    before = _revision()

    result = server_inventory.sync_servers_from_mihomo()

    assert result["source"] == "mihomo"
    assert _revision() == before


def test_custom_update_of_eligible_server_advances_revision(monkeypatch):
    _stub_custom_runtime(monkeypatch)
    created = custom_servers.create_custom_https_proxy_server(**_custom_kwargs("update-me"))
    server_id = created["server"]["server_id"]
    with db_session() as connection:
        connection.execute(
            "UPDATE server_preferences SET vpn_auto_priority=1 WHERE server_id=?", (server_id,)
        )
    before = _revision()

    updated = custom_servers.update_custom_https_proxy_server(
        server_id,
        **_custom_kwargs("update-me", host="changed.example.test"),
    )

    assert updated["ok"] is True
    assert _revision() == before + 1


def test_custom_delete_of_eligible_server_advances_revision(monkeypatch):
    _stub_custom_runtime(monkeypatch)
    created = custom_servers.create_custom_https_proxy_server(**_custom_kwargs("delete-me"))
    server_id = created["server"]["server_id"]
    with db_session() as connection:
        connection.execute(
            "UPDATE server_preferences SET vpn_auto_priority=1 WHERE server_id=?", (server_id,)
        )
    before = _revision()

    deleted = custom_servers.delete_custom_https_proxy_server(server_id)

    assert deleted["ok"] is True
    assert _revision() == before + 1


def test_mihomo_snapshot_is_rejected_if_selector_revision_changes_during_fetch(monkeypatch):
    _seed_auto_server()
    with db_session() as connection:
        connection.execute("INSERT OR IGNORE INTO routing_global_state (id) VALUES (1)")
    entered_fetch = Event()
    release_fetch = Event()
    result_holder = {}

    def blocked_inventory():
        entered_fetch.set()
        assert release_fetch.wait(timeout=5)
        return []

    monkeypatch.setattr(
        "fwrouter_api.adapters.mihomo.DEFAULT_MIHOMO_ADAPTER.list_servers",
        blocked_inventory,
    )

    def run_sync():
        result_holder["result"] = server_inventory.sync_servers_from_mihomo()

    worker = Thread(target=run_sync)
    worker.start()
    assert entered_fetch.wait(timeout=5)
    with xray_writer_guard(timeout_seconds=5.0), db_session() as connection:
        committed = commit_active_selection(
            connection,
            expected_revision=0,
            expected_active_server_id=None,
            expected_provenance_decision_id=None,
            server_id="inventory-target",
            provenance={"decision_id": "concurrent-selector-decision", "selected_server_id": "inventory-target"},
        )
        assert committed == 1
    release_fetch.set()
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert result_holder["result"]["error_code"] == "VPN_AUTO_SELECTION_STALE_SNAPSHOT"
    assert _revision() == 1
    with db_session() as connection:
        state = connection.execute(
            "SELECT inventory_state FROM servers WHERE server_id='inventory-target'"
        ).fetchone()["inventory_state"]
    assert state == "active"
