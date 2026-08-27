from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import db_session, initialize_database
from fwrouter_api.main import create_app
from fwrouter_api.services.external_collectors import (
    external_connection_collector_last_run,
    run_due_external_collectors_once,
)
from fwrouter_api.services.jobs import create_job, mark_job_running
from fwrouter_api.services.live_probe_cache import clear_live_probe_cache, get_live_probe_cache
from fwrouter_api.services.logs import write_operational_log, write_technical_log
from fwrouter_api.services.external_connections_registry import (
    get_external_connection,
    get_external_connection_generated_state,
    list_external_connections,
    mark_external_connection_seen,
    upsert_external_connection_generated_state,
    upsert_external_connection_record,
)
from fwrouter_api.services.ui_display_settings import external_connection_contract
from fwrouter_api.services.ui_display_settings import (
    ExternalConnectionValidationError,
    delete_custom_external_connection,
    preview_custom_external_connection,
    upsert_custom_external_connection,
)
from fwrouter_api.services.ui_state import (
    _month_key,
    _summarize_log_event,
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
                subject_id, subject_type, subject_role, implementation_kind, stable_key, display_name, alias,
                desired_mode, runtime_state, is_active, last_seen_at
            ) VALUES
                ('lan:aa-bb', 'lan', 'lan_client', 'lan', 'lan:aa-bb', 'Desktop', 'Desktop', 'global', 'active', 1, '2026-06-01T10:00:00Z'),
                ('tailscale:node-1', 'tailscale_node', 'external_network_source', 'tailscale_node', 'tailscale:node-1', 'TS Macbook', 'TS Macbook', 'global', 'active', 1, '2026-06-01T09:00:00Z'),
                ('xray:human-1', 'xray', 'vless_client', 'xray', 'xray:human-1', 'stepan', 'Stepan', 'enabled', 'running', 0, '2026-06-01T08:00:00Z'),
                ('xray:internal-1', 'xray', 'vless_client', 'xray', 'xray:internal-1', 'vpn-auto-test', 'vpn-auto-test', 'enabled', 'running', 0, '2026-06-01T07:00:00Z')
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
    assert defaults["show_internal_vless"] is False
    assert defaults["system_visibility"]["lan"] is True
    assert defaults["system_visibility"]["external_network_source"] is True
    assert defaults["hidden_subject_ids"] == []
    assert defaults["subject_traffic_preferences"] == {}

    saved = save_ui_display_settings(
        {
            "system_visibility": {
                "lan": False,
                "external_network_source": True,
                "vless_client": True,
            },
            "show_inactive": True,
            "show_internal_vless": True,
            "hidden_subject_ids": ["lan:aa-bb", "docker:web-1"],
            "subject_traffic_preferences": {
                "lan:aa-bb": ["direct_rx_bytes", "vpn_tx_bytes"],
                "xray:human-1": ["vpn_rx_bytes", "vpn_tx_bytes"],
            },
        }
    )

    assert saved["system_visibility"]["lan"] is False
    assert saved["hidden_subject_ids"] == ["lan:aa-bb", "docker:web-1"]
    assert saved["subject_traffic_preferences"]["lan:aa-bb"] == ["direct_rx_bytes", "vpn_tx_bytes"]
    assert get_ui_display_settings()["show_internal_vless"] is True
    assert get_ui_display_settings()["hidden_subject_ids"] == ["lan:aa-bb", "docker:web-1"]
    assert get_ui_display_settings()["subject_traffic_preferences"]["xray:human-1"] == ["vpn_rx_bytes", "vpn_tx_bytes"]


def test_ui_display_settings_system_visibility_and_custom_external(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    upsert_external_connection_record(
        {
            "connection_id": "custom-monitor",
            "system_id": "custom-monitor",
            "label": "Custom Monitor",
            "description": "External display-only system.",
        }
    )

    saved = save_ui_display_settings(
        {
            "system_visibility": {
                "lan": True,
                "external_network_source": False,
                "vless_client": True,
                "custom-monitor": True,
            },
        }
    )

    assert saved["system_visibility"]["external_network_source"] is False
    assert len(saved["custom_external_systems"]) == 1
    custom = saved["custom_external_systems"][0]
    assert custom["connection_id"] == "custom-monitor"
    assert custom["system_id"] == "custom-monitor"
    assert custom["label"] == "Custom Monitor"
    assert custom["connection_type"] == "external_management"
    assert custom["collector_config"]["trigger"] == "external_system_pushes_on_change"

    workspace = get_ui_settings_workspace()
    systems = {item["system_id"]: item for item in workspace["display_systems"]}

    assert systems["lan"]["kind"] == "core"
    assert "external_network_source" not in systems
    assert systems["custom-monitor"]["kind"] == "external"
    assert systems["custom-monitor"]["manageable_actions"] == []
    assert systems["custom-monitor"]["external_system_id"] == "custom-monitor"
    assert systems["custom-monitor"]["requested_by"] == "external_client:custom-monitor"
    assert systems["custom-monitor"]["collector"] == "external_connection:custom-monitor"
    assert systems["custom-monitor"]["integration_mode"] == "api_push"
    assert systems["custom-monitor"]["refresh_mode"] == "on_change"


def test_external_connection_preview_normalizes_refresh_contract(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    interval = preview_custom_external_connection(
        {
            "connection_id": "connection-a",
            "system_id": "connection-a",
            "label": "Connection A",
            "connection_type": "external_network_source",
            "location": "host",
            "runtime_type": "provider-a",
            "integration_mode": "http_poll",
            "refresh_mode": "interval",
            "collector_config": {
                "url": "http://127.0.0.1:8080/status",
                "interval_seconds": 120,
                "timeout_seconds": 7,
            },
        }
    )["external_connection"]
    assert interval["connection_id"] == "connection-a"
    assert interval["system_id"] == "connection-a"
    assert interval["refresh_mode"] == "interval"
    assert interval["collector_config"]["trigger"] == "poll_interval"
    assert interval["collector_config"]["interval_seconds"] == 120
    assert interval["api_guide"]["collection"]["refresh_mode"] == "interval"

    manual = preview_custom_external_connection(
        {
            "connection_id": "connection-b",
            "system_id": "connection-b",
            "label": "Connection B",
            "connection_type": "external_network_source",
            "location": "host",
            "runtime_type": "provider-b",
            "integration_mode": "http_poll",
            "refresh_mode": "manual",
            "collector_config": {
                "url": "http://127.0.0.1:8080/status",
            },
        }
    )["external_connection"]
    assert manual["refresh_mode"] == "manual"
    assert manual["collector_config"]["trigger"] == "manual_refresh"


def test_external_connection_upsert_and_patch_are_validated(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    result = upsert_custom_external_connection(
        "connection-a",
        {
            "connection_id": "connection-a",
            "system_id": "connection-a",
            "label": "Connection A",
            "connection_type": "external_network_source",
            "location": "host",
            "runtime_type": "provider-a",
            "integration_mode": "file_read",
            "refresh_mode": "interval",
            "collector_config": {
                "path": "/var/lib/fwrouter-v2/external-collectors/connection-a.json",
                "interval_seconds": 300,
            },
        },
    )
    stored = result["external_connection"]
    assert stored["connection_id"] == "connection-a"
    assert stored["system_id"] == "connection-a"
    assert stored["integration_mode"] == "file_read"
    assert stored["collector_config"]["path"].endswith("connection-a.json")
    assert result["display_settings"]["system_visibility"]["connection-a"] is True

    patched = upsert_custom_external_connection(
        "connection-a",
        {"label": "Connection A API", "address": "provider-a.local"},
        partial=True,
    )["external_connection"]
    assert patched["label"] == "Connection A API"
    assert patched["connection_type"] == "external_network_source"

    try:
        upsert_custom_external_connection(
            "connection-a",
            {"connection_type": "external_management"},
            partial=True,
        )
    except ExternalConnectionValidationError as exc:
        assert exc.field_errors["connection_type"] == "immutable"
    else:  # pragma: no cover - defensive
        raise AssertionError("connection_type patch must be rejected")

    try:
        preview_custom_external_connection(
            {
                "connection_id": "connection-b",
                "system_id": "connection-b",
                "label": "Broken poll",
                "connection_type": "external_network_source",
                "integration_mode": "http_poll",
                "refresh_mode": "interval",
                "collector_config": {"interval_seconds": 300},
            }
        )
    except ExternalConnectionValidationError as exc:
        assert exc.field_errors["collector_config.url"] == "required"
    else:  # pragma: no cover - defensive
        raise AssertionError("http_poll without url must be rejected")


def test_ui_display_settings_drops_unknown_builtin_visibility_keys(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    upsert_external_connection_record(
        {
            "connection_id": "custom-monitor",
            "system_id": "custom-monitor",
            "label": "Custom Monitor",
        }
    )

    saved = save_ui_display_settings(
        {
            "system_visibility": {
                "lan": True,
                "tailscale": True,
                "xray": False,
                "mihomo": True,
                "custom-monitor": False,
                "external-management-homeassistant": True,
            },
        }
    )

    assert "tailscale" not in saved["system_visibility"]
    assert "xray" not in saved["system_visibility"]
    assert "mihomo" not in saved["system_visibility"]
    assert saved["system_visibility"]["custom-monitor"] is False
    assert saved["system_visibility"]["external-management-homeassistant"] is True


def test_external_vpn_connection_exposes_identity_replacement_and_readiness(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    upsert_external_connection_record(
        {
            "connection_id": "connection-a",
            "system_id": "connection-a",
            "label": "Connection A",
            "connection_type": "external_vpn_module",
            "runtime_type": "provider-a",
            "replacement_target": "mihomo",
            "location": "host",
            "endpoints": {
                "tcp_redir_port": "16080",
                "udp_tproxy_port": "16081",
            },
            "capabilities": {
                "supports_transparent_proxy": True,
            },
        }
    )

    workspace = get_ui_settings_workspace()
    systems = {item["system_id"]: item for item in workspace["display_systems"]}
    system = systems["connection-a"]

    assert system["external_system_id"] == "connection-a"
    assert system["requested_by"] == "external_client:connection-a"
    assert system["collector"] == "external_connection:connection-a"
    assert system["replacement_target"] == "mihomo"
    assert system["readiness"]["state"] in {"ready", "active"}
    assert system["readiness"]["details"]["replacement_target"] == "mihomo"
    assert system["readiness"]["details"]["tcp_redir_port_present"] is True
    assert system["readiness"]["details"]["udp_tproxy_port_present"] is True
    assert system["readiness"]["details"]["runtime_adapter_role"] == "vpn_dataplane"
    assert system["api_guide"]["identity"]["external_system_id"] == "connection-a"
    assert system["api_guide"]["replacement_target"] == "mihomo"
    assert system["api_guide"]["collection"]["integration_mode"] == "api_push"
    assert system["api_guide"]["collection"]["refresh_mode"] == "on_change"
    assert system["api_guide"]["traffic_accounting"]["path"] == "/traffic/collect"


def test_external_connection_collector_file_read_manual_refresh(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    import fwrouter_api.services.external_collectors as collectors

    collector_root = (tmp_path / "external-collectors").resolve()
    collector_root.mkdir()
    payload_path = collector_root / "status.json"
    payload_path.write_text(
        """
        {
          "status": "ok",
          "clients": [{"id": "client-1", "label": "Client 1", "address": "100.64.0.10"}],
          "traffic_samples": []
        }
        """,
        encoding="utf-8",
    )
    monkeypatch.setattr(collectors, "ALLOWED_FILE_ROOT", collector_root)

    upsert_external_connection_record(
        {
            "connection_id": "connection-a",
            "system_id": "connection-a",
            "label": "Connection A",
            "connection_type": "external_network_source",
            "runtime_type": "provider-a",
            "integration_mode": "file_read",
            "refresh_mode": "manual",
            "collector_config": {
                "path": str(payload_path),
                "timeout_seconds": 3,
            },
            "endpoints": {
                "client_cidr": "100.64.0.0/10",
            },
            "capabilities": {
                "supports_client_inventory": True,
            },
        }
    )

    client = TestClient(create_app(enable_startup_tasks=False))
    response = client.post("/api/v2/ui/external-connections/connection-a/collect", json={"dry_run": True})
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    collector = body["data"]["collector"]
    assert collector["integration_mode"] == "file_read"
    assert collector["refresh_mode"] == "manual"
    assert collector["payload_summary"]["status"] == "ok"
    assert collector["payload_summary"]["clients_count"] == 1


def test_external_connection_interval_collector_only_runs_when_due(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    import fwrouter_api.services.external_collectors as collectors

    collector_root = (tmp_path / "external-collectors").resolve()
    collector_root.mkdir()
    payload_path = collector_root / "status.json"
    payload_path.write_text('{"status": "ok", "traffic_samples": []}', encoding="utf-8")
    monkeypatch.setattr(collectors, "ALLOWED_FILE_ROOT", collector_root)
    collectors._LAST_RUN_AT.clear()

    upsert_external_connection_record(
        {
            "connection_id": "connection-a",
            "system_id": "connection-a",
            "label": "Connection A",
            "connection_type": "external_network_source",
            "runtime_type": "provider-a",
            "integration_mode": "file_read",
            "refresh_mode": "interval",
            "collector_config": {
                "path": str(payload_path),
                "interval_seconds": 300,
            },
            "endpoints": {
                "client_cidr": "100.64.0.0/10",
            },
            "capabilities": {
                "supports_client_inventory": True,
            },
        }
    )
    upsert_external_connection_record(
        {
            "connection_id": "connection-b",
            "system_id": "connection-b",
            "label": "Connection B",
            "connection_type": "external_network_source",
            "runtime_type": "provider-a",
            "integration_mode": "api_push",
            "refresh_mode": "on_change",
        }
    )

    first = collectors.run_due_external_collectors_once(now=1000.0)
    second = collectors.run_due_external_collectors_once(now=1100.0)
    third = collectors.run_due_external_collectors_once(now=1301.0)

    assert [item["connection_id"] for item in first] == ["connection-a"]
    assert second == []
    assert [item["connection_id"] for item in third] == ["connection-a"]


def test_external_vpn_xray_replacement_contract_endpoint(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    upsert_external_connection_record(
        {
            "connection_id": "connection-a",
            "system_id": "connection-a",
            "label": "Connection A",
            "connection_type": "external_vpn_module",
            "runtime_type": "provider-a",
            "replacement_target": "xray",
            "location": "ip",
            "address": "127.0.0.1:18080",
            "endpoints": {
                "controller_url": "http://127.0.0.1:18080/api",
                "subscription_base_url": "http://127.0.0.1:18080/sub",
                "traffic_stats_url": "http://127.0.0.1:18080/stats",
            },
            "capabilities": {
                "supports_client_api": True,
                "supports_subscription_api": True,
                "supports_traffic_stats": True,
            },
        }
    )

    contract = external_connection_contract("connection-a")
    assert contract is not None
    assert contract["readiness"]["details"]["replacement_target"] == "xray"
    assert contract["readiness"]["details"]["replacement_support"] == "explicit_client_runtime_contract"
    assert contract["readiness"]["details"]["runtime_adapter_role"] == "explicit_client_runtime"
    assert contract["api_guide"]["replacement_target"] == "xray"
    assert contract["api_guide"]["explicit_client_runtime"]["supported"] == "external_explicit_client_runtime_contract"
    assert "subscription_base_url" in contract["api_guide"]["available_elements"]["endpoints"]
    assert "supports_client_api" in contract["api_guide"]["available_elements"]["capabilities"]

    response = TestClient(create_app(enable_startup_tasks=False)).get(
        "/api/v2/ui/external-connections/connection-a/contract"
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["data"]["contract"]["replacement_target"] == "xray"
    assert payload["data"]["external_connection"]["external_system_id"] == "connection-a"


def test_external_management_contract_endpoint_requires_registered_connection_id(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    write_operational_log(
        event_type="external_action",
        message="External client action.",
        details={
            "management_attribution": {
                "source_type": "external_client",
                "client_name": "homeassistant",
                "channel": "local_api",
                "action": "set_global_mode",
            }
        },
    )

    response = TestClient(create_app(enable_startup_tasks=False)).get(
        "/api/v2/ui/external-connections/external-management-homeassistant/contract"
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is False
    assert payload["error"]["code"] == "EXTERNAL_CONNECTION_NOT_FOUND"
    assert get_external_connection("external-management-homeassistant") is None


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
            "system_visibility": {
                "lan": True,
                "external_network_source": True,
                "vless_client": True,
            },
            "show_inactive": True,
            "show_internal_vless": False,
        },
    )
    visible_ids = {item["subject_id"] for item in visible}

    assert "xray:human-1" not in visible_ids
    assert "xray:internal-1" not in visible_ids

    hidden = filter_ui_clients(
        clients,
        display_settings={
            "system_visibility": {
                "lan": True,
                "external_network_source": True,
                "vless_client": True,
            },
            "show_inactive": True,
            "show_internal_vless": True,
            "hidden_subject_ids": ["lan:aa-bb"],
        },
    )
    hidden_ids = {item["subject_id"] for item in hidden}

    assert "lan:aa-bb" not in hidden_ids
    assert "tailscale:node-1" in hidden_ids
    assert "xray:internal-1" not in hidden_ids


def test_system_visibility_filters_ui_clients_and_inventory(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _seed_ui_clients()

    settings = save_ui_display_settings(
        {
            "system_visibility": {
                "lan": True,
                "external_network_source": False,
                "vless_client": True,
                "docker": True,
                "host": True,
            },
            "show_inactive": True,
        }
    )

    filtered_ids = {item["subject_id"] for item in filter_ui_clients(list_ui_clients(), display_settings=settings)}
    inventory_ids = {item["subject_id"] for item in list_ui_settings_inventory(role="all", query="", limit=50)}

    assert "lan:aa-bb" in filtered_ids
    assert "tailscale:node-1" not in filtered_ids
    assert "lan:aa-bb" in inventory_ids
    assert "tailscale:node-1" not in inventory_ids


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


def test_external_management_selector_log_is_ui_visible() -> None:
    event = {
        "event_id": "event-1",
        "created_at": "2026-07-18 10:00:00",
        "level": "info",
        "event_type": "vpn_auto_server_switched",
        "subject_id": None,
        "message": "VPN-auto server was switched.",
        "details": {
            "requested_by": "external_client",
            "active_before": "srv-old",
            "active_after": "srv-new",
            "selected_server_name": "Norway",
            "selected_ping": {"last_ping_ms": 42},
        },
    }

    summarized = _summarize_log_event(event)

    assert summarized["ui_visible"] is True
    assert summarized["category"] == "server"
    assert summarized["message"] == "Auto VPN-сервер выбран"
    assert summarized["details"]["Инициатор"] == "external_client"
    assert summarized["details"]["Сервер"] == "Norway"
    assert summarized["details"]["Ping"] == "42 ms"


def test_watchdog_selector_log_is_categorized_as_watchdog() -> None:
    event = {
        "event_id": "event-1",
        "created_at": "2026-08-22 10:00:00",
        "level": "info",
        "event_type": "vpn_auto_server_switched",
        "subject_id": None,
        "message": "VPN-auto server was switched.",
        "details": {
            "requested_by": "fwrouter_watchdog",
            "reason": "watchdog_failover:scheduler_watchdog_check",
            "active_before": "srv-old",
            "active_after": "srv-new",
            "selected_server_name": "Norway",
            "selected_ping": {"last_ping_ms": 42},
        },
    }

    summarized = _summarize_log_event(event)

    assert summarized["ui_visible"] is True
    assert summarized["category"] == "watchdog"
    assert summarized["message"] == "Auto VPN-сервер выбран"
    assert summarized["details"]["Инициатор"] == "fwrouter_watchdog"
    assert summarized["details"]["Сервер"] == "Norway"


def test_rules_validation_log_uses_operator_friendly_reason() -> None:
    event = {
        "event_id": "event-1",
        "created_at": "2026-08-20 09:27:59",
        "level": "error",
        "event_type": "mutation_apply_manual_rules_failed",
        "subject_id": None,
        "message": "Manual rules validation failed.",
        "details": {
            "code": "RULES_VALIDATION_FAILED",
            "message": "Manual rules validation failed.",
        },
    }

    summarized = _summarize_log_event(event)

    assert summarized["ui_visible"] is True
    assert summarized["message"] == "Не удалось применить правила маршрутизации"
    assert summarized["details"]["Код"] == "RULES_VALIDATION_FAILED"
    assert summarized["details"]["Причина"] == (
        "В правилах маршрутизации есть некорректная строка или неподдерживаемый формат."
    )


def test_ui_settings_inventory_is_loaded_separately(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _seed_ui_clients()

    all_items = list_ui_settings_inventory(role="all", query="", limit=50)
    docker_items = list_ui_settings_inventory(role="docker_runtime", query="", limit=50)
    lan_items = list_ui_settings_inventory(role="lan_client", query="desk", limit=50)
    vless_hidden_items = list_ui_settings_inventory(role="vless_client", query="", limit=50)
    external_network_items = list_ui_settings_inventory(role="external_network_source", query="", limit=50)
    vless_items = list_ui_settings_inventory(role="vless_client", query="", limit=50)

    assert any(item["subject_id"] == "lan:aa-bb" for item in all_items)
    assert all(item["subject_id"] != "xray:human-1" for item in vless_hidden_items)
    assert [item["subject_id"] for item in external_network_items] == ["tailscale:node-1"]
    assert external_network_items[0]["inventory_role"] == "external_network_source"
    assert external_network_items[0]["kind"] == "external_network_source"
    assert external_network_items[0]["implementation_kind"] == "tailscale_node"
    assert external_network_items[0]["display_system_id"] == "external-network-tailscale"
    assert all(item["inventory_role"] == "vless_client" for item in vless_items)
    assert all(item["kind"] == "vless_client" for item in vless_items)
    assert all(item["implementation_kind"] == "xray" for item in vless_items)
    assert docker_items == []
    assert len(lan_items) == 1
    assert lan_items[0]["subject_id"] == "lan:aa-bb"

    workspace = get_ui_settings_workspace()
    assert workspace["counts"]["external_network_source"] == 1
    assert workspace["counts"]["vless_client"] == 0


def test_discovered_external_network_source_does_not_create_connection_instance(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO subjects (
                subject_id, subject_type, subject_role, implementation_kind, stable_key,
                display_name, alias, desired_mode, runtime_state, is_active, last_seen_at
            )
            VALUES (
                'external-node:1', 'host', 'external_network_source',
                'provider-a', 'provider-a:node-1', 'Provider A node',
                'Provider A node', 'global', 'active', 1, '2026-06-01T09:00:00Z'
            )
            """
        )

    workspace = get_ui_settings_workspace()
    systems = {item["system_id"]: item for item in workspace["display_systems"]}
    assert "external-network-host" not in systems
    assert external_connection_contract("connection-a") is None

    created = upsert_custom_external_connection(
        "connection-a",
        {
            "connection_id": "connection-a",
            "system_id": "connection-a",
            "label": "Provider A",
            "connection_type": "external_network_source",
            "runtime_type": "provider-a",
            "integration_mode": "command_probe",
            "refresh_mode": "interval",
            "address": "provider-a0",
            "collector_config": {
                "script_id": "provider_a_status",
                "interval_seconds": 3600,
            },
        },
        partial=False,
    )["external_connection"]
    assert created["connection_id"] == "connection-a"
    assert created["system_id"] == "connection-a"
    assert created["label"] == "Provider A"
    assert created["custom"] is True
    assert created["connection_type"] == "external_network_source"
    assert created["runtime_type"] == "provider-a"
    assert created["collector_config"]["script_id"] == "provider_a_status"

    contract = external_connection_contract("connection-a")
    assert contract is not None
    assert contract["label"] == "Provider A"
    assert contract["custom"] is True


def test_external_connection_identity_is_not_derived_from_label(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    try:
        preview_custom_external_connection(
            {
                "label": "Connection A",
                "connection_type": "external_network_source",
                "runtime_type": "provider-a",
            }
        )
    except ExternalConnectionValidationError as exc:
        assert exc.code == "INVALID_EXTERNAL_CONNECTION"
        assert exc.field_errors["connection_id"] == "required"
    else:  # pragma: no cover - defensive
        raise AssertionError("external connection identity must not be derived from label")


def test_external_connection_migration_does_not_create_implicit_instances(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    with db_session() as connection:
        connection.execute(
            """
            INSERT OR REPLACE INTO settings (key, value_json)
            VALUES ('ui.admin_client_display.v1', ?)
            """,
            (
                json.dumps(
                    {
                        "custom_external_systems": [
                            {
                                "label": "Connection A",
                                "connection_type": "external_network_source",
                                "runtime_type": "provider-a",
                            }
                        ]
                    },
                    sort_keys=True,
                ),
            ),
        )
        connection.execute(
            """
            INSERT INTO subjects (
                subject_id, subject_type, subject_role, implementation_kind, stable_key,
                display_name, desired_mode, runtime_state, is_active
            ) VALUES (
                'tailscale:node-1', 'tailscale_node', 'external_network_source', 'tailscale_node',
                'tailscale:node-1', 'TS node', 'global', 'active', 1
            )
            """
        )

    initialize_database()

    assert list_external_connections() == []
    assert get_external_connection("connection-a") is None
    assert get_external_connection("external-network-tailscale") is None


def test_two_external_network_connections_same_provider_are_independent(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    first = upsert_custom_external_connection(
        "connection-a",
        {
            "connection_id": "connection-a",
            "system_id": "connection-a",
            "label": "Connection A",
            "connection_type": "external_network_source",
            "runtime_type": "provider-a",
            "integration_mode": "file_read",
            "refresh_mode": "manual",
            "collector_config": {"path": "/var/lib/fwrouter-v2/external-collectors/connection-a.json"},
        },
    )["external_connection"]
    second = upsert_custom_external_connection(
        "connection-b",
        {
            "connection_id": "connection-b",
            "system_id": "connection-b",
            "label": "Connection B",
            "connection_type": "external_network_source",
            "runtime_type": "provider-a",
            "integration_mode": "file_read",
            "refresh_mode": "manual",
            "collector_config": {"path": "/var/lib/fwrouter-v2/external-collectors/connection-b.json"},
        },
    )["external_connection"]

    assert first["connection_id"] != second["connection_id"]
    assert first["runtime_type"] == second["runtime_type"] == "provider-a"
    assert get_external_connection_generated_state(first["connection_id"]) is not None
    assert get_external_connection_generated_state(second["connection_id"]) is not None

    patched = upsert_custom_external_connection(
        first["connection_id"],
        {"label": "Connection A Updated", "address": "provider-a-home"},
        partial=True,
    )["external_connection"]

    assert patched["label"] == "Connection A Updated"
    assert get_external_connection(second["connection_id"])["label"] == "Connection B"
    assert get_external_connection(second["connection_id"])["collector_config"]["path"].endswith("connection-b.json")

    delete_custom_external_connection(first["connection_id"])

    assert get_external_connection(first["connection_id"]) is None
    assert get_external_connection_generated_state(first["connection_id"]) is None
    assert get_external_connection(second["connection_id"]) is not None
    assert get_external_connection_generated_state(second["connection_id"]) is not None


def test_external_management_connections_do_not_collapse_on_secondary_fields(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    first = upsert_custom_external_connection(
        "connection-a",
        {
            "connection_id": "connection-a",
            "system_id": "shared-system",
            "label": "Shared Label",
            "connection_type": "external_management",
            "runtime_type": "provider-a",
        },
    )["external_connection"]
    second = upsert_custom_external_connection(
        "connection-b",
        {
            "connection_id": "connection-b",
            "system_id": "shared-system",
            "label": "Shared Label",
            "connection_type": "external_management",
            "runtime_type": "provider-a",
        },
    )["external_connection"]

    assert first["connection_id"] == "connection-a"
    assert second["connection_id"] == "connection-b"
    assert len([item for item in list_external_connections() if item["system_id"] == "shared-system"]) == 2

    patched = upsert_custom_external_connection(
        "connection-a",
        {"label": "Renamed Label"},
        partial=True,
    )["external_connection"]

    assert patched["connection_id"] == "connection-a"
    assert patched["label"] == "Renamed Label"
    assert get_external_connection("connection-b")["label"] == "Shared Label"

    delete_custom_external_connection("connection-a")

    assert get_external_connection("connection-a") is None
    assert get_external_connection("connection-b") is not None


def test_external_network_connections_do_not_collapse_on_provider_label_or_system_id(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    for connection_id in ("connection-a", "connection-b"):
        upsert_external_connection_record(
            {
                "connection_id": connection_id,
                "system_id": "shared-system",
                "label": "Shared Label",
                "connection_type": "external_network_source",
                "runtime_type": "provider-a",
                "integration_mode": "file_read",
                "refresh_mode": "manual",
                "collector_config": {
                    "path": f"/var/lib/fwrouter-v2/external-collectors/{connection_id}.json"
                },
            }
        )

    connections = list_external_connections()
    shared = [item for item in connections if item["system_id"] == "shared-system"]
    assert [item["connection_id"] for item in shared] == ["connection-a", "connection-b"]
    assert {item["runtime_type"] for item in shared} == {"provider-a"}
    assert {item["label"] for item in shared} == {"Shared Label"}

    upsert_custom_external_connection("connection-b", {"label": "Connection B"}, partial=True)
    delete_custom_external_connection("connection-a")

    assert get_external_connection("connection-a") is None
    assert get_external_connection("connection-b")["label"] == "Connection B"


def test_external_connection_operations_reject_duplicate_system_id_identity(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    for connection_id in ("connection-a", "connection-b"):
        upsert_external_connection_record(
            {
                "connection_id": connection_id,
                "system_id": "shared-system",
                "label": f"Connection {connection_id[-1].upper()}",
                "connection_type": "external_network_source",
                "runtime_type": "provider-a",
                "integration_mode": "file_read",
                "refresh_mode": "manual",
                "collector_config": {
                    "path": f"/var/lib/fwrouter-v2/external-collectors/{connection_id}.json"
                },
            }
        )

    assert get_external_connection("shared-system") is None
    assert external_connection_contract("shared-system") is None

    client = TestClient(create_app(enable_startup_tasks=False))
    contract_response = client.get("/api/v2/ui/external-connections/shared-system/contract")
    collect_response = client.post(
        "/api/v2/ui/external-connections/shared-system/collect",
        json={"dry_run": True},
    )
    delete_response = client.delete("/api/v2/ui/external-connections/shared-system")

    assert contract_response.json()["ok"] is False
    assert contract_response.json()["error"]["code"] == "EXTERNAL_CONNECTION_NOT_FOUND"
    assert collect_response.json()["ok"] is False
    assert collect_response.json()["error"]["code"] == "EXTERNAL_CONNECTION_NOT_FOUND"
    assert delete_response.json()["ok"] is False
    assert delete_response.json()["error"]["code"] == "EXTERNAL_CONNECTION_NOT_FOUND"

    mark_external_connection_seen("shared-system", details={"event": "ignored"})
    try:
        upsert_external_connection_generated_state("shared-system", {"artifact": "wrong"})
    except ExternalConnectionValidationError as exc:
        assert exc.code == "EXTERNAL_CONNECTION_NOT_FOUND"
    else:  # pragma: no cover - defensive
        raise AssertionError("generated-state update must require connection_id")

    assert get_external_connection("connection-a")["last_seen_at"] is None
    assert get_external_connection("connection-b")["last_seen_at"] is None
    assert get_external_connection_generated_state("connection-a") is not None
    assert get_external_connection_generated_state("connection-b") is not None


def test_external_connection_schema_migration_allows_duplicate_system_id(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    db_path = tmp_path / "state" / "fwrouter.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.unlink(missing_ok=True)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE external_connections (
                connection_id TEXT PRIMARY KEY,
                system_id TEXT NOT NULL UNIQUE,
                label TEXT NOT NULL,
                connection_type TEXT NOT NULL,
                runtime_type TEXT,
                replacement_target TEXT,
                location TEXT NOT NULL DEFAULT 'manual',
                address TEXT,
                integration_mode TEXT NOT NULL DEFAULT 'api_push',
                refresh_mode TEXT NOT NULL DEFAULT 'on_change',
                enabled INTEGER NOT NULL DEFAULT 1,
                value_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_seen_at TEXT
            )
            """
        )
        connection.execute(
            """
            INSERT INTO external_connections (
                connection_id, system_id, label, connection_type, runtime_type,
                replacement_target, location, address, integration_mode, refresh_mode,
                enabled, value_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "connection-a",
                "shared-system",
                "Connection A",
                "external_management",
                "provider-a",
                "",
                "manual",
                "",
                "api_push",
                "on_change",
                1,
                json.dumps(
                    {
                        "connection_id": "connection-a",
                        "system_id": "shared-system",
                        "label": "Connection A",
                        "connection_type": "external_management",
                        "runtime_type": "provider-a",
                    }
                ),
            ),
        )
        connection.execute(
            """
            CREATE TABLE external_connection_generated_state (
                connection_id TEXT PRIMARY KEY,
                state_json TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            """
            INSERT INTO external_connection_generated_state (connection_id, state_json)
            VALUES (?, ?)
            """,
            ("connection-a", json.dumps({"connection_id": "connection-a", "artifact": "old"})),
        )

    initialize_database()
    upsert_external_connection_record(
        {
            "connection_id": "connection-b",
            "system_id": "shared-system",
            "label": "Connection B",
            "connection_type": "external_management",
            "runtime_type": "provider-a",
        }
    )

    shared = [item for item in list_external_connections() if item["system_id"] == "shared-system"]
    assert [item["connection_id"] for item in shared] == ["connection-a", "connection-b"]
    assert get_external_connection_generated_state("connection-a")["artifact"] == "old"
    with db_session() as connection:
        table_sql = connection.execute(
            """
            SELECT sql
            FROM sqlite_master
            WHERE type = 'table' AND name = 'external_connections'
            """
        ).fetchone()["sql"].lower()
    assert "system_id text not null unique" not in table_sql


def test_external_connection_cannot_be_patched_by_compat_system_id(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    upsert_external_connection_record(
        {
            "connection_id": "connection-a",
            "system_id": "compat-connection-a",
            "label": "Connection A",
            "connection_type": "external_network_source",
            "runtime_type": "provider-a",
        }
    )

    try:
        upsert_custom_external_connection(
            "compat-connection-a",
            {"label": "Connection A Updated"},
            partial=True,
        )
    except ExternalConnectionValidationError as exc:
        assert exc.code == "EXTERNAL_CONNECTION_NOT_FOUND"
        assert exc.field_errors["connection_id"] == "not_found"
    else:  # pragma: no cover - defensive
        raise AssertionError("external connection update must require connection_id")

    assert get_external_connection("connection-a")["label"] == "Connection A"
    assert get_external_connection("compat-connection-a") is None


def test_external_connections_survive_settings_save_restart_logs_and_cache_cleanup(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    records = [
        {
            "connection_id": "connection-management",
            "system_id": "connection-management",
            "label": "Connection Management",
            "connection_type": "external_management",
            "integration_mode": "api_push",
            "refresh_mode": "on_change",
        },
        {
            "connection_id": "connection-network",
            "system_id": "connection-network",
            "label": "Connection Network",
            "connection_type": "external_network_source",
            "runtime_type": "provider-a",
            "integration_mode": "file_read",
            "refresh_mode": "manual",
            "collector_config": {"path": "/var/lib/fwrouter-v2/external-collectors/connection-network.json"},
        },
        {
            "connection_id": "connection-vpn",
            "system_id": "connection-vpn",
            "label": "Connection VPN",
            "connection_type": "external_vpn_module",
            "runtime_type": "provider-b",
            "replacement_target": "mihomo",
            "integration_mode": "api_push",
            "refresh_mode": "on_change",
            "endpoints": {
                "tcp_redir_port": "16080",
                "udp_tproxy_port": "16081",
            },
        },
    ]
    for record in records:
        upsert_external_connection_record(record)

    write_operational_log(
        event_type="external_action",
        message="External client action.",
        details={
            "management_attribution": {
                "source_type": "external_client",
                "client_name": "log-only-client",
                "action": "switch",
            }
        },
    )
    workspace = get_ui_settings_workspace()
    systems = {item["system_id"]: item for item in workspace["display_systems"]}
    assert "external-management-log-only-client" in systems
    assert get_external_connection("external-management-log-only-client") is None

    saved = save_ui_display_settings(
        {
            "system_visibility": {
                "connection-management": True,
                "connection-network": True,
                "connection-vpn": True,
                "external-management-log-only-client": True,
            },
            "custom_external_systems": [
                {
                    "connection_id": "connection-from-settings",
                    "system_id": "connection-from-settings",
                    "label": "Connection From Settings",
                    "connection_type": "external_management",
                }
            ],
        }
    )

    assert {item["connection_id"] for item in saved["custom_external_systems"]} == {
        "connection-management",
        "connection-network",
        "connection-vpn",
    }
    assert get_external_connection("connection-from-settings") is None
    with db_session() as connection:
        raw = connection.execute(
            "SELECT value_json FROM settings WHERE key = 'ui.admin_client_display.v1'"
        ).fetchone()
        stored_settings = json.loads(raw["value_json"])
        connection.execute("DELETE FROM operational_logs")
    assert "custom_external_systems" not in stored_settings

    clear_live_probe_cache()
    initialize_database()
    for record in records:
        restored = get_external_connection(record["connection_id"])
        assert restored is not None
        assert restored["label"] == record["label"]
        assert restored["connection_id"] == record["connection_id"]
        assert get_external_connection_generated_state(record["connection_id"]) is not None
    assert get_external_connection("external-management-log-only-client") is None
    assert get_external_connection("connection-from-settings") is None


def test_external_connection_generated_state_is_updated_and_cleaned(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    upsert_external_connection_record(
        {
            "connection_id": "connection-a",
            "system_id": "connection-a",
            "label": "Connection A",
            "connection_type": "external_network_source",
            "runtime_type": "provider-a",
        }
    )
    generated = upsert_external_connection_generated_state(
        "connection-a",
        {"artifact": "collector-contract", "version": 1},
    )
    assert generated["artifact"] == "collector-contract"
    assert generated["connection_id"] == "connection-a"

    delete_custom_external_connection("connection-a")

    assert get_external_connection("connection-a") is None
    assert get_external_connection_generated_state("connection-a") is None


def test_external_connection_delete_cleans_only_own_runtime_artifacts(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    clear_live_probe_cache()

    for connection_id in ("connection-a", "connection-b"):
        upsert_external_connection_record(
            {
                "connection_id": connection_id,
                "system_id": connection_id,
                "label": connection_id,
                "connection_type": "external_network_source",
                "runtime_type": "provider-a",
                "integration_mode": "http_poll",
                "refresh_mode": "interval",
                "collector_config": {
                    "url": "",
                    "interval_seconds": 60,
                },
            }
        )

    run_due_external_collectors_once(now=100.0)
    assert external_connection_collector_last_run("connection-a") == 100.0
    assert external_connection_collector_last_run("connection-b") == 100.0

    cache_calls: list[str] = []

    def _load_a() -> str:
        cache_calls.append("connection-a")
        return "cached-a"

    def _load_b() -> str:
        cache_calls.append("connection-b")
        return "cached-b"

    assert get_live_probe_cache(
        "external_ingress.runtime.provider-a.connection-a",
        ttl_seconds=30,
        loader=_load_a,
    ) == "cached-a"
    assert get_live_probe_cache(
        "external_ingress.runtime.provider-a.connection-b",
        ttl_seconds=30,
        loader=_load_b,
    ) == "cached-b"

    delete_custom_external_connection("connection-a")

    assert get_external_connection("connection-a") is None
    assert get_external_connection_generated_state("connection-a") is None
    assert external_connection_collector_last_run("connection-a") is None
    assert external_connection_collector_last_run("connection-b") == 100.0
    assert get_external_connection("connection-b") is not None
    assert get_external_connection_generated_state("connection-b") is not None
    assert get_live_probe_cache(
        "external_ingress.runtime.provider-a.connection-b",
        ttl_seconds=30,
        loader=_load_b,
    ) == "cached-b"
    assert cache_calls == ["connection-a", "connection-b"]


def test_external_connection_generated_state_regeneration_is_idempotent(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    payload = {
        "connection_id": "connection-a",
        "system_id": "connection-a",
        "label": "Connection A",
        "connection_type": "external_network_source",
        "runtime_type": "provider-a",
        "integration_mode": "file_read",
        "refresh_mode": "manual",
        "collector_config": {"path": "/var/lib/fwrouter-v2/external-collectors/connection-a.json"},
    }
    upsert_external_connection_record(payload)
    initial = get_external_connection_generated_state("connection-a")
    upsert_external_connection_record(payload)
    regenerated = get_external_connection_generated_state("connection-a")

    assert initial is not None
    assert regenerated is not None
    assert {
        key: value
        for key, value in initial.items()
        if key != "updated_at"
    } == {
        key: value
        for key, value in regenerated.items()
        if key != "updated_at"
    }

    upsert_external_connection_record({**payload, "refresh_mode": "interval"})
    updated = get_external_connection_generated_state("connection-a")

    assert updated["connection_id"] == "connection-a"
    assert updated["refresh_mode"] == "interval"
    initialize_database()
    assert get_external_connection_generated_state("connection-a")["refresh_mode"] == "interval"


def test_xray_subscription_profiles_are_grouped_by_client(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    current_month = _month_key()

    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO subjects (
                subject_id, subject_type, subject_role, implementation_kind, stable_key, display_name, alias,
                desired_mode, runtime_state, is_active, last_seen_at
            ) VALUES
                ('xray:sub-nina-de', 'xray', 'vless_client', 'xray', 'xray:sub-nina-de', 'Nina / Nina / Germany', NULL, 'enabled', 'running', 0, '2026-06-01T08:00:00Z'),
                ('xray:sub-nina-nl', 'xray', 'vless_client', 'xray', 'xray:sub-nina-nl', 'Nina / Nina / Netherlands', NULL, 'enabled', 'running', 0, '2026-06-01T09:00:00Z'),
                ('xray:sub-alex-de', 'xray', 'vless_client', 'xray', 'xray:sub-alex-de', 'Alex / Alex / Germany', NULL, 'enabled', 'running', 0, NULL)
            """
        )
        connection.execute(
            """
            INSERT INTO subject_xray (subject_id, client_id, client_uuid, email, enabled)
            VALUES
                ('xray:sub-nina-de', 'nina-de', 'nina-de', 'sub-nina-de@fwrouter.local', 1),
                ('xray:sub-nina-nl', 'nina-nl', 'nina-nl', 'sub-nina-nl@fwrouter.local', 1),
                ('xray:sub-alex-de', 'alex-de', 'alex-de', 'sub-alex-de@fwrouter.local', 1)
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
    active_xray_clients = [item for item in xray_clients if item["is_active"]]

    assert len(active_xray_clients) == 1
    grouped = active_xray_clients[0]
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

    inventory = list_ui_settings_inventory(role="vless_client", query="", limit=50)
    assert [item["subject_id"] for item in inventory] == ["xray-subscription:nina"]
    assert inventory[0]["kind"] == "vless_client"
    assert inventory[0]["implementation_kind"] == "xray"
    assert inventory[0]["is_internal"] is False
    assert inventory[0]["is_active"] is True
    assert inventory[0]["activity_reason"] == "profile_seen_24h"
    assert inventory[0]["traffic_month_bytes"] == 1000

    settings_inventory = list_ui_settings_inventory(role="vless_client", query="", limit=50, include_inactive=True)
    assert {item["subject_id"] for item in settings_inventory} == {
        "xray-subscription:alex",
        "xray-subscription:nina",
    }


def test_opaque_xray_subscription_profile_nodes_are_hidden(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO subjects (
                subject_id, subject_type, subject_role, implementation_kind, stable_key, display_name, alias,
                desired_mode, runtime_state, is_active
            ) VALUES
                ('xray:sub-opaque-server', 'xray', 'vless_client', 'xray', 'xray:sub-opaque-server', 'sub-token-server', NULL, 'enabled', 'active', 1)
            """
        )
        connection.execute(
            """
            INSERT INTO subject_xray (subject_id, client_id, client_uuid, email, enabled)
            VALUES ('xray:sub-opaque-server', 'opaque', 'opaque', 'sub-token-server@fwrouter.local', 1)
            """
        )

    clients = list_ui_clients()
    inventory = list_ui_settings_inventory(role="vless_client", query="", limit=50)

    assert all("sub-token-server" not in str(item) for item in clients)
    assert all("sub-token-server" not in str(item) for item in inventory)


def test_xray_subscription_group_mode_route_expands_subject_ids(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO subjects (
                subject_id, subject_type, subject_role, implementation_kind, stable_key, display_name, alias,
                desired_mode, runtime_state, is_active
            ) VALUES
                ('xray:sub-nina-de', 'xray', 'vless_client', 'xray', 'xray:sub-nina-de', 'Nina / Nina / Germany', NULL, 'enabled', 'running', 1),
                ('xray:sub-nina-nl', 'xray', 'vless_client', 'xray', 'xray:sub-nina-nl', 'Nina / Nina / Netherlands', NULL, 'enabled', 'running', 1)
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
