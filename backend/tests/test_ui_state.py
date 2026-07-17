from __future__ import annotations

from pathlib import Path

from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import db_session, initialize_database
from fwrouter_api.services.jobs import create_job, mark_job_running
from fwrouter_api.services.live_probe_cache import clear_live_probe_cache
from fwrouter_api.services.logs import write_operational_log, write_technical_log
from fwrouter_api.services.ui_state import (
    _month_key,
    filter_ui_clients,
    get_ui_display_settings,
    get_ui_settings_workspace,
    list_ui_settings_inventory,
    list_ui_clients,
    save_ui_display_settings,
)
from fwrouter_api.routes.subjects import SetSubjectModeRequest, set_subject_mode_endpoint


def _configure_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FWROUTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("FWROUTER_DATABASE_URL", f"sqlite:///{tmp_path}/fwrouter.db")
    get_settings.cache_clear()
    clear_live_probe_cache()


def _seed_ui_clients() -> None:
    current_month = _month_key()
    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO subjects (
                subject_id, subject_type, stable_key, display_name, alias,
                desired_mode, runtime_state, is_active, last_seen_at
            ) VALUES
                ('lan:aa-bb', 'lan', 'lan:aa-bb', 'Desktop', 'Desktop', 'global', 'active', 1, '2026-06-01T10:00:00Z'),
                ('tailscale:node-1', 'tailscale', 'tailscale:node-1', 'TS Macbook', 'TS Macbook', 'global', 'active', 1, '2026-06-01T09:00:00Z'),
                ('xray:human-1', 'xray', 'xray:human-1', 'stepan', 'Stepan', 'enabled', 'running', 0, '2026-06-01T08:00:00Z'),
                ('xray:internal-1', 'xray', 'xray:internal-1', 'vpn-auto-test', 'vpn-auto-test', 'enabled', 'running', 0, '2026-06-01T07:00:00Z')
            """
        )
        connection.execute(
            """
            INSERT INTO subject_lan (subject_id, mac_address, ip_address, hostname)
            VALUES ('lan:aa-bb', 'AA:BB', '192.168.0.10', 'desktop')
            """
        )
        connection.execute(
            """
            INSERT INTO subject_tailscale (subject_id, node_id, tailscale_ip, hostname, user_name, online)
            VALUES ('tailscale:node-1', 'node-1', '100.64.0.20', 'macbook', 'sergey', 1)
            """
        )
        connection.execute(
            """
            INSERT INTO subject_xray (subject_id, client_id, client_uuid, email, enabled)
            VALUES
                ('xray:human-1', 'human-1', 'human-1', 'stepan@fwrouter.local', 1),
                ('xray:internal-1', 'internal-1', 'internal-1', 'vpn-auto-abcd@fwrouter.local', 1)
            """
        )
        connection.execute(
            """
            INSERT INTO subscription_accounts (account_id, slug, display_name, enabled)
            VALUES (1, 'stepan', 'Stepan', 1)
            """
        )
        connection.execute(
            """
            INSERT INTO subscription_clients (client_id, account_id, token, app_type, enabled, display_name, last_seen_at, last_user_agent)
            VALUES (1, 1, 'stepan', 'auto', 1, 'Stepan', '2026-06-01 11:00:00', 'TestAgent')
            """
        )
        connection.execute(
            """
            INSERT INTO traffic_monthly (
                subject_id, period_month, direct_rx_bytes, direct_tx_bytes, vpn_rx_bytes, vpn_tx_bytes
            ) VALUES
                ('lan:aa-bb', ?, 1000, 2000, 0, 0),
                ('tailscale:node-1', ?, 0, 0, 4000, 5000),
                ('xray:human-1', ?, 0, 0, 6000, 7000)
            """,
            (current_month, current_month, current_month),
        )


def test_ui_display_settings_roundtrip(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    defaults = get_ui_display_settings()
    assert defaults["show_internal_xray"] is False
    assert defaults["show_lan"] is True
    assert defaults["hidden_subject_ids"] == []
    assert defaults["subject_traffic_preferences"] == {}

    saved = save_ui_display_settings(
        {
            "show_lan": False,
            "show_tailscale": True,
            "show_xray": True,
            "show_inactive": True,
            "show_internal_xray": True,
            "hidden_subject_ids": ["lan:aa-bb", "docker:web-1"],
            "subject_traffic_preferences": {
                "lan:aa-bb": ["direct_rx_bytes", "vpn_tx_bytes"],
                "xray:human-1": ["vpn_rx_bytes", "vpn_tx_bytes"],
            },
        }
    )

    assert saved["show_lan"] is False
    assert saved["hidden_subject_ids"] == ["lan:aa-bb", "docker:web-1"]
    assert saved["subject_traffic_preferences"]["lan:aa-bb"] == ["direct_rx_bytes", "vpn_tx_bytes"]
    assert get_ui_display_settings()["show_internal_xray"] is True
    assert get_ui_display_settings()["hidden_subject_ids"] == ["lan:aa-bb", "docker:web-1"]
    assert get_ui_display_settings()["subject_traffic_preferences"]["xray:human-1"] == ["vpn_rx_bytes", "vpn_tx_bytes"]


def test_list_ui_clients_includes_traffic_and_filters_internal_xray(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _seed_ui_clients()

    clients = list_ui_clients()
    by_subject = {item["subject_id"]: item for item in clients}

    assert by_subject["lan:aa-bb"]["traffic_total_bytes"] == 3000
    assert by_subject["tailscale:node-1"]["traffic_total_bytes"] == 9000
    assert by_subject["lan:aa-bb"]["traffic_month"]["direct_rx_bytes"] == 1000
    assert by_subject["lan:aa-bb"]["traffic_month"]["direct_tx_bytes"] == 2000
    assert by_subject["tailscale:node-1"]["traffic_panel_metrics"][0]["key"] == "vpn_rx_bytes"
    assert "xray:human-1" not in by_subject
    assert "xray:internal-1" not in by_subject
    assert by_subject["lan:aa-bb"]["effective_mode"] == "DIRECT"
    assert by_subject["lan:aa-bb"]["mode_source"] == "GLOBAL"

    filtered = filter_ui_clients(clients)
    filtered_ids = {item["subject_id"] for item in filtered}

    assert "xray:human-1" not in filtered_ids
    assert "xray:internal-1" not in filtered_ids

    visible = filter_ui_clients(
        clients,
        display_settings={
            "show_lan": True,
            "show_tailscale": True,
            "show_xray": True,
            "show_inactive": True,
            "show_internal_xray": False,
        },
    )
    visible_ids = {item["subject_id"] for item in visible}

    assert "xray:human-1" not in visible_ids
    assert "xray:internal-1" not in visible_ids

    hidden = filter_ui_clients(
        clients,
        display_settings={
            "show_lan": True,
            "show_tailscale": True,
            "show_xray": True,
            "show_inactive": True,
            "show_internal_xray": True,
            "hidden_subject_ids": ["lan:aa-bb"],
        },
    )
    hidden_ids = {item["subject_id"] for item in hidden}

    assert "lan:aa-bb" not in hidden_ids
    assert "tailscale:node-1" in hidden_ids
    assert "xray:internal-1" not in hidden_ids


def test_ui_settings_workspace_exposes_active_apply_job_and_logs(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _seed_ui_clients()

    job = create_job(
        "apply_mutation",
        lock_key="apply",
        requested_by="pytest",
        input_data={"intent": "set_global_mode"},
    )
    mark_job_running(job["job_id"])

    write_operational_log(
        event_type="routing_changed",
        message="Routing updated.",
        details={"source": "pytest"},
    )
    write_technical_log(
        component="pytest",
        event_type="job_debug",
        message="Debug event.",
        details={"job_id": job["job_id"]},
    )

    clear_live_probe_cache()
    workspace = get_ui_settings_workspace()

    assert workspace["router"]["active_job"]["job_id"] == job["job_id"]
    assert workspace["logs"]["operational_count"] >= 1
    assert workspace["logs"]["technical_count"] >= 1
    assert "clients" not in workspace
    assert "system_subjects" not in workspace


def test_ui_settings_inventory_is_loaded_separately(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _seed_ui_clients()

    all_items = list_ui_settings_inventory(kind="all", query="", limit=50)
    docker_items = list_ui_settings_inventory(kind="docker", query="", limit=50)
    lan_items = list_ui_settings_inventory(kind="lan", query="desk", limit=50)
    xray_items = list_ui_settings_inventory(kind="xray", query="", limit=50)

    assert any(item["subject_id"] == "lan:aa-bb" for item in all_items)
    assert all(item["subject_id"] != "xray:human-1" for item in xray_items)
    assert docker_items == []
    assert len(lan_items) == 1
    assert lan_items[0]["subject_id"] == "lan:aa-bb"


def test_xray_subscription_profiles_are_grouped_by_client(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    current_month = _month_key()

    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO subjects (
                subject_id, subject_type, stable_key, display_name, alias,
                desired_mode, runtime_state, is_active, last_seen_at
            ) VALUES
                ('xray:sub-nina-de', 'xray', 'xray:sub-nina-de', 'Nina / Nina / Germany', NULL, 'enabled', 'running', 0, '2026-06-01T08:00:00Z'),
                ('xray:sub-nina-nl', 'xray', 'xray:sub-nina-nl', 'Nina / Nina / Netherlands', NULL, 'enabled', 'running', 0, '2026-06-01T09:00:00Z')
            """
        )
        connection.execute(
            """
            INSERT INTO subject_xray (subject_id, client_id, client_uuid, email, enabled)
            VALUES
                ('xray:sub-nina-de', 'nina-de', 'nina-de', 'sub-nina-de@fwrouter.local', 1),
                ('xray:sub-nina-nl', 'nina-nl', 'nina-nl', 'sub-nina-nl@fwrouter.local', 1)
            """
        )
        connection.execute(
            """
            INSERT INTO subscription_accounts (account_id, slug, display_name, enabled)
            VALUES (2, 'nina', 'Nina', 1)
            """
        )
        connection.execute(
            """
            INSERT INTO subscription_clients (
                client_id, account_id, token, app_type, enabled, display_name, last_seen_at, last_user_agent
            )
            VALUES (2, 2, 'nina', 'auto', 1, 'Nina', CURRENT_TIMESTAMP, 'TestAgent')
            """
        )
        connection.execute(
            """
            INSERT INTO traffic_monthly (
                subject_id, period_month, direct_rx_bytes, direct_tx_bytes, vpn_rx_bytes, vpn_tx_bytes
            ) VALUES
                ('xray:sub-nina-de', ?, 0, 0, 100, 200),
                ('xray:sub-nina-nl', ?, 0, 0, 300, 400)
            """,
            (current_month, current_month),
        )

    clients = list_ui_clients()
    xray_clients = [item for item in clients if item["kind"] == "xray"]

    assert len(xray_clients) == 1
    grouped = xray_clients[0]
    assert grouped["subject_id"] == "xray-subscription:nina"
    assert grouped["subject_ids"] == ["xray:sub-nina-nl", "xray:sub-nina-de"]
    assert grouped["member_count"] == 2
    assert grouped["display_name"] == "Nina"
    assert grouped["is_internal"] is False
    assert grouped["is_active"] is True
    assert grouped["activity_reason"] == "profile_seen_24h"
    assert grouped["traffic_month"]["vpn_rx_bytes"] == 400
    assert grouped["traffic_month"]["vpn_tx_bytes"] == 600
    assert grouped["traffic_month_bytes"] == 1000

    panel_ids = {item["subject_id"] for item in filter_ui_clients(clients)}
    assert "xray-subscription:nina" in panel_ids

    inventory = list_ui_settings_inventory(kind="xray", query="", limit=50)
    assert [item["subject_id"] for item in inventory] == ["xray-subscription:nina"]
    assert inventory[0]["is_internal"] is False
    assert inventory[0]["is_active"] is True
    assert inventory[0]["activity_reason"] == "profile_seen_24h"
    assert inventory[0]["traffic_month_bytes"] == 1000


def test_opaque_xray_subscription_profile_nodes_are_hidden(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO subjects (
                subject_id, subject_type, stable_key, display_name, alias,
                desired_mode, runtime_state, is_active
            ) VALUES
                ('xray:sub-opaque-server', 'xray', 'xray:sub-opaque-server', 'sub-token-server', NULL, 'enabled', 'active', 1)
            """
        )
        connection.execute(
            """
            INSERT INTO subject_xray (subject_id, client_id, client_uuid, email, enabled)
            VALUES ('xray:sub-opaque-server', 'opaque', 'opaque', 'sub-token-server@fwrouter.local', 1)
            """
        )

    clients = list_ui_clients()
    inventory = list_ui_settings_inventory(kind="xray", query="", limit=50)

    assert all("sub-token-server" not in str(item) for item in clients)
    assert all("sub-token-server" not in str(item) for item in inventory)


def test_xray_subscription_group_mode_route_expands_subject_ids(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO subjects (
                subject_id, subject_type, stable_key, display_name, alias,
                desired_mode, runtime_state, is_active
            ) VALUES
                ('xray:sub-nina-de', 'xray', 'xray:sub-nina-de', 'Nina / Nina / Germany', NULL, 'enabled', 'running', 1),
                ('xray:sub-nina-nl', 'xray', 'xray:sub-nina-nl', 'Nina / Nina / Netherlands', NULL, 'enabled', 'running', 1)
            """
        )
        connection.execute(
            """
            INSERT INTO subject_xray (subject_id, client_id, client_uuid, email, enabled)
            VALUES
                ('xray:sub-nina-de', 'nina-de', 'nina-de', 'sub-nina-de@fwrouter.local', 1),
                ('xray:sub-nina-nl', 'nina-nl', 'nina-nl', 'sub-nina-nl@fwrouter.local', 1)
            """
        )

    captured: dict[str, object] = {}

    def fake_submit_apply_mutation(**kwargs):
        captured.update(kwargs)
        return {"job_id": "job-1", "status": "queued", "result_json": None}

    monkeypatch.setattr("fwrouter_api.routes.subjects.submit_apply_mutation", fake_submit_apply_mutation)

    response = set_subject_mode_endpoint(
        "xray-subscription:nina",
        SetSubjectModeRequest(mode="vpn", actor_scope="admin", requested_by="pytest", run_now=False),
    )

    assert response.ok is True
    assert captured["intent"] == "set_subject_admin_mode"
    payload = captured["payload"]
    assert payload["subject_id"] == "xray-subscription:nina"
    assert set(payload["subject_ids"]) == {"xray:sub-nina-nl", "xray:sub-nina-de"}
    assert payload["mode"] == "vpn"


def test_list_ui_clients_reuses_cached_traffic_and_effective_state(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _seed_ui_clients()
    traffic_calls: list[int] = []
    effective_calls: list[int] = []

    original_effective = "fwrouter_api.services.ui_state.list_subjects_with_effective_state"

    def _traffic_maps():
        traffic_calls.append(1)
        return (
            {"lan:aa-bb": 3000},
            {"lan:aa-bb": 3000},
            {"lan:aa-bb": {"direct_rx_bytes": 1000, "direct_tx_bytes": 2000, "vpn_rx_bytes": 0, "vpn_tx_bytes": 0}},
        )

    def _effective(*, include_deleted=False, limit=1000):
        effective_calls.append(1)
        return [
            {"subject_id": "lan:aa-bb", "effective_state": {"effective_mode": "direct", "mode_source": "global"}},
        ]

    monkeypatch.setattr("fwrouter_api.services.ui_state._load_traffic_maps", _traffic_maps)
    monkeypatch.setattr(original_effective, _effective)

    first = list_ui_clients()
    second = list_ui_clients()

    assert first
    assert second
    assert len(traffic_calls) == 1
    assert len(effective_calls) == 1
