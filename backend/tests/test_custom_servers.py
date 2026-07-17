from __future__ import annotations
from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import initialize_database


import json
from pathlib import Path

from fastapi.testclient import TestClient

from fwrouter_api.adapters.subscription import SubscriptionRefreshResult, SubscriptionRefreshStatus, SubscriptionServer
from fwrouter_api.db.connection import db_session, initialize_database
from fwrouter_api.main import create_app
from fwrouter_api.services.control_plane_transfer import export_control_plane_snapshot, import_control_plane_snapshot
from fwrouter_api.services.custom_servers import resolve_runtime_proxy_rows
from fwrouter_api.services.mihomo_config import build_mihomo_config
from fwrouter_api.services.subscription import refresh_subscription_inventory


def _configure_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FWROUTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("FWROUTER_DATABASE_URL", f"sqlite:///{tmp_path}/fwrouter.db")
    monkeypatch.setenv("FWROUTER_MAINTENANCE_SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("FWROUTER_WATCHDOG_SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("FWROUTER_RUNTIME_CONVERGENCE_SCHEDULER_ENABLED", "false")
    get_settings.cache_clear()


def _client() -> TestClient:
    return TestClient(create_app(enable_startup_tasks=False))


def test_custom_https_proxy_server_appears_in_servers_list_without_password(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    with _client() as client:
        create_response = client.post(
            "/api/v2/servers/custom/https",
            json={
                "server_name": "Office Proxy",
                "host": "proxy.example.com",
                "port": 8443,
                "username": "alice",
                "password": "secret-pass",
                "tls": True,
                "sni": "proxy.example.com",
                "vpn_auto": True,
            },
        )

        assert create_response.status_code == 200
        created = create_response.json()["data"]["custom_server"]["server"]
        server_id = created["server_id"]
        assert created["origin"]["kind"] == "custom_https_proxy"
        assert created["custom_proxy"]["password_configured"] is True

        list_response = client.get("/api/v2/servers")
        assert list_response.status_code == 200
        listed = next(
            server
            for server in list_response.json()["data"]["servers"]
            if server["server_id"] == server_id
        )
        assert listed["provider_name"] == "custom proxy"
        assert listed["custom_proxy"]["host"] == "proxy.example.com"
        assert listed["custom_proxy"]["proxy_type"] == "http"
        assert listed["custom_proxy"]["password_configured"] is True
        assert '"password":' not in json.dumps(listed, ensure_ascii=False)

        get_response = client.get(f"/api/v2/servers/{server_id}")
        assert get_response.status_code == 200
        fetched = get_response.json()["data"]["server"]
        assert fetched["custom_proxy"]["username"] == "alice"
        assert fetched["custom_proxy"]["password_configured"] is True


def test_custom_https_proxy_server_runtime_raw_includes_credentials(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    with _client() as client:
        response = client.post(
            "/api/v2/servers/custom/https",
            json={
                "server_name": "Secure Proxy",
                "host": "secure.example.com",
                "port": 9443,
                "username": "bob",
                "password": "top-secret",
                "tls": True,
                "skip_cert_verify": True,
                "path": "/connect",
                "global_list": True,
            },
        )
    assert response.status_code == 200
    created = response.json()["data"]["custom_server"]["server"]

    runtime_rows = resolve_runtime_proxy_rows(inventory_state="active", limit=1000)
    custom_row = next(
        row
        for row in runtime_rows
        if row["server_id"] == created["server_id"]
    )
    assert custom_row["raw"]["password"] == "top-secret"
    assert custom_row["raw"]["username"] == "bob"
    assert custom_row["raw"]["type"] == "http"

    config = build_mihomo_config()
    proxy = next(item for item in config["proxies"] if item["name"] == "Secure Proxy")
    assert proxy["server"] == "secure.example.com"
    assert proxy["skip-cert-verify"] is True
    listeners = config["listeners"]
    mixed_listener = next(item for item in listeners if item["type"] == "mixed")
    redir_listener = next(item for item in listeners if item["type"] == "redir")
    tproxy_listener = next(item for item in listeners if item["type"] == "tproxy")
    assert mixed_listener["proxy"] == "vpn-global"
    assert redir_listener["port"] == 5202
    assert redir_listener["rule"] == "fwrouter-transparent"
    assert tproxy_listener["port"] == 5203
    assert tproxy_listener["rule"] == "fwrouter-transparent"
    assert config["rules"] == ["MATCH,DIRECT"]
    assert config["fwrouter"]["resolved_selective_default"] == "direct"
    assert config["fwrouter"]["final_match_rule"] == "MATCH,DIRECT"
    assert config["fwrouter"]["transparent_final_match_rule"] == "MATCH,DIRECT"


def test_custom_socks5_proxy_server_runtime_raw_uses_socks5(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    with _client() as client:
        response = client.post(
            "/api/v2/servers/custom/proxy",
            json={
                "server_name": "Proxy6 SOCKS5",
                "proxy_type": "socks5",
                "host": "161.0.21.33",
                "port": 8000,
                "username": "user1",
                "password": "pw1",
                "vpn_auto": True,
            },
        )
    assert response.status_code == 200
    created = response.json()["data"]["custom_server"]["server"]

    runtime_rows = resolve_runtime_proxy_rows(inventory_state="active", limit=1000)
    custom_row = next(
        row
        for row in runtime_rows
        if row["server_id"] == created["server_id"]
    )
    assert custom_row["raw"]["type"] == "socks5"
    assert custom_row["raw"]["username"] == "user1"
    assert custom_row["raw"]["password"] == "pw1"
    assert "tls" not in custom_row["raw"]


def test_mihomo_config_renders_simple_rules(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    config = build_mihomo_config()
    assert config["rules"] == ["MATCH,DIRECT"]
    assert config["fwrouter"]["resolved_selective_default"] == "direct"



def test_subscription_refresh_does_not_mark_custom_servers_missing(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    with _client() as client:
        response = client.post(
            "/api/v2/servers/custom/https",
            json={
                "server_name": "Keep Me",
                "host": "keep.example.com",
                "port": 8443,
                "password": "pw",
            },
        )
    assert response.status_code == 200
    custom_server_id = response.json()["data"]["custom_server"]["server"]["server_id"]

    monkeypatch.setattr(
        "fwrouter_api.adapters.subscription.DEFAULT_SUBSCRIPTION_ADAPTER.refresh",
        lambda url: SubscriptionRefreshResult(
            status=SubscriptionRefreshStatus.SUCCESS,
            servers=[
                SubscriptionServer(
                    server_id="sub-1",
                    server_name="Subscription Server",
                    provider_name="subscription",
                    raw={"name": "Subscription Server", "type": "http", "server": "sub.example.com", "port": 8080},
                )
            ],
            message="ok",
        ),
    )

    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO subscription_state (id, url, status)
            VALUES (1, 'https://provider.example.test/config', 'idle')
            ON CONFLICT(id) DO UPDATE SET url = excluded.url, status = 'idle'
            """
        )

    result = refresh_subscription_inventory()
    assert result["ok"] is True

    with db_session() as connection:
        row = connection.execute(
            "SELECT inventory_state FROM servers WHERE server_id = ?",
            (custom_server_id,),
        ).fetchone()
    assert row is not None
    assert row["inventory_state"] == "active"


def test_custom_server_transfer_redacts_credentials_by_default_and_imports_with_secrets(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    with _client() as client:
        response = client.post(
            "/api/v2/servers/custom/https",
            json={
                "server_name": "Transfer Proxy",
                "host": "transfer.example.com",
                "port": 443,
                "username": "carol",
                "password": "secret-transfer",
            },
        )
    assert response.status_code == 200

    redacted_snapshot = export_control_plane_snapshot(include_secrets=False, write_file=False)["snapshot"]
    redacted_rows = redacted_snapshot["state"]["server_custom_https_proxy"]
    assert redacted_rows[0]["username"] is None
    assert redacted_rows[0]["password"] is None
    assert redacted_rows[0]["credentials_redacted"] is True
    assert "custom_server_credentials_redacted" in redacted_snapshot["warnings"]

    full_snapshot = export_control_plane_snapshot(include_secrets=True, write_file=False)["snapshot"]

    with db_session() as connection:
        connection.execute("DELETE FROM servers")

    imported = import_control_plane_snapshot(full_snapshot, normalize_runtime_state=True)
    assert imported["ok"] is True

    with db_session() as connection:
        row = connection.execute(
            """
            SELECT username, password
            FROM server_custom_https_proxy
            WHERE server_id IN (SELECT server_id FROM servers WHERE server_name = 'Transfer Proxy')
            """
        ).fetchone()
    assert row is not None
    assert row["username"] == "carol"
    assert row["password"] == "secret-transfer"
