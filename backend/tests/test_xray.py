from __future__ import annotations
from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import initialize_database


import base64
import json
from pathlib import Path

from fastapi.testclient import TestClient

import fwrouter_api.services.apply as apply_service
import fwrouter_api.services.dataplane_global as dataplane_global_service
import fwrouter_api.services.subject_policy as subject_policy_service
from fwrouter_api.adapters import xray as xray_adapter
from fwrouter_api.adapters.dataplane import DataplaneOperation, DataplaneResult
from fwrouter_api.adapters.mihomo import MihomoHealth, MihomoRuntimeState
from fwrouter_api.adapters.xray import (
    NoopXrayAdapter,
    RealXrayAdapter,
    XrayApplyResult,
    XrayRuntimeState,
)
from fwrouter_api.db.connection import db_session, initialize_database
from fwrouter_api.jobs.extended_handlers import register_extended_handlers
from fwrouter_api.jobs.manager import get_default_job_manager
from fwrouter_api.main import create_app
from fwrouter_api.services import runtime as runtime_service
from fwrouter_api.services import subject_inventory as inventory_service
from fwrouter_api.services import xray as xray_service
from fwrouter_api.services.live_probe_cache import clear_live_probe_cache
from fwrouter_api.services.subject_policy import (
    get_subject_with_effective_state,
    list_subjects_with_effective_state,
    set_subject_mode,
)
from fwrouter_api.services.subjects import get_subject


def _configure_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FWROUTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("FWROUTER_DATABASE_URL", f"sqlite:///{tmp_path}/fwrouter.db")
    get_settings.cache_clear()
    clear_live_probe_cache()


def _xray_paths() -> tuple[Path, Path]:
    settings = get_settings()
    return settings.paths.state_dir / "xray" / "config.json", settings.paths.state_dir / "xray" / "docker-compose.yml"


def _write_xray_config(config_path: Path, clients: list[dict[str, object]] | None = None) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "log": {"loglevel": "warning"},
        "inbounds": [
            {
                "listen": "0.0.0.0",
                "port": 5300,
                "protocol": "vless",
                "settings": {"clients": clients or [], "decryption": "none"},
                "streamSettings": {
                    "network": "ws",
                    "wsSettings": {"path": "/vless"},
                },
            }
        ],
        "outbounds": [{"protocol": "freedom", "tag": "direct"}],
    }
    config_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class _FakeRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.compose_stdout = '[{"Service":"fwrouter-xray","State":"running"}]'
        self.test_result = XrayApplyResult(ok=True, message="test ok", details={"runner": "fake"})
        self.reload_result = XrayApplyResult(ok=True, message="reload ok", details={"runner": "fake"})

    def __call__(self, action: str, payload: dict[str, object]) -> XrayApplyResult:
        self.calls.append((action, dict(payload)))
        if action == "test_config":
            return self.test_result
        if action == "reload":
            return self.reload_result
        if action == "compose_ps":
            return XrayApplyResult(
                ok=True,
                message="compose ps ok",
                details={"stdout": self.compose_stdout, "runner": "fake"},
            )
        raise AssertionError(action)


def _build_adapter(tmp_path: Path, *, runner: _FakeRunner | None = None) -> RealXrayAdapter:
    config_path, compose_path = _xray_paths()
    compose_path.parent.mkdir(parents=True, exist_ok=True)
    compose_path.write_text("services:\n  fwrouter-xray:\n    image: teddysun/xray\n", encoding="utf-8")
    return RealXrayAdapter(
        config_path=config_path,
        compose_path=compose_path,
        log_root=tmp_path / "log" / "xray",
        runner=runner or _FakeRunner(),
    )


def _patch_xray_adapters(monkeypatch, adapter: RealXrayAdapter) -> None:
    monkeypatch.setattr(xray_adapter, "DEFAULT_XRAY_ADAPTER", adapter)
    monkeypatch.setattr(xray_service, "DEFAULT_XRAY_ADAPTER", adapter)
    monkeypatch.setattr(inventory_service, "DEFAULT_XRAY_ADAPTER", adapter)
    monkeypatch.setattr(runtime_service, "DEFAULT_XRAY_ADAPTER", adapter)


class _ReadyMihomoAdapter:
    def health(self) -> MihomoHealth:
        return MihomoHealth(
            runtime_state=MihomoRuntimeState.RUNNING,
            message="mihomo ready",
            details={
                "adapter": "fake",
                "config": {
                    "tproxy_port": 5202,
                    "tun_enabled": True,
                },
                "selectors": {
                    "vpn_global_exists": True,
                    "vpn_global_targets_count": 5,
                    "vpn_global_has_vpn_auto": True,
                    "vpn_global_now": "vpn-auto",
                },
            },
        )

    def list_servers(self):  # noqa: ANN001
        return []


class _SuccessfulDataplaneAdapter:
    def check(self, plan):  # noqa: ANN001
        return DataplaneResult(
            ok=True,
            operation=DataplaneOperation.CHECK,
            message="check ok",
            details={
                "stage": "check",
                "owned_table": "inet fwrouter_v2",
                "table_exists": True,
                "required_chains": {
                    "prerouting": True,
                    "output": True,
                    "forward": True,
                    "postrouting": True,
                    "fwrouter_classify": True,
                    "fwrouter_direct": True,
                    "fwrouter_vpn": True,
                },
            },
        )

    def apply(self, plan):  # noqa: ANN001
        return DataplaneResult(
            ok=True,
            operation=DataplaneOperation.APPLY,
            message="apply ok",
            details={
                "stage": "verify",
                "owned_table": "inet fwrouter_v2",
                "table_exists": True,
                "routing_mode": "vpn",
                "vpn_contract_ready": True,
                "vpn_external_path_verified": True,
                "vpn_tproxy_port": 5202,
                "required_chains": {
                    "prerouting": True,
                    "output": True,
                    "forward": True,
                    "postrouting": True,
                    "fwrouter_classify": True,
                    "fwrouter_direct": True,
                    "fwrouter_vpn": True,
                },
            },
        )

    def rollback(self, plan):  # noqa: ANN001
        return DataplaneResult(
            ok=True,
            operation=DataplaneOperation.ROLLBACK,
            message="rollback ok",
            details={"stage": "rollback"},
        )


def _patch_runtime(monkeypatch) -> None:
    adapter = _SuccessfulDataplaneAdapter()
    monkeypatch.setattr(apply_service, "DEFAULT_DATAPLANE_ADAPTER", adapter)
    monkeypatch.setattr(runtime_service, "DEFAULT_DATAPLANE_ADAPTER", adapter)
    monkeypatch.setattr(dataplane_global_service, "DEFAULT_MIHOMO_ADAPTER", _ReadyMihomoAdapter())
    monkeypatch.setattr(runtime_service, "DEFAULT_MIHOMO_ADAPTER", _ReadyMihomoAdapter())


def _seed_server(server_id: str) -> None:
    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO servers (
                server_id,
                server_name,
                provider_name,
                inventory_state,
                raw_json
            )
            VALUES (?, ?, 'provider', 'active', '{}')
            """,
            (server_id, server_id),
        )
        connection.execute(
            """
            INSERT INTO server_preferences (
                server_id,
                vpn_auto,
                global_list
            )
            VALUES (?, 1, 1)
            """,
            (server_id,),
        )
        connection.execute(
            "INSERT OR IGNORE INTO server_ping_state (server_id, status) VALUES (?, 'success')",
            (server_id,),
        )


def _seed_routing_state(*, desired_mode: str, active_auto_server_id: str | None = None) -> None:
    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO routing_global_state (
                id,
                desired_mode,
                applied_mode,
                selective_default,
                server_mode,
                active_auto_server_id,
                apply_state
            )
            VALUES (1, ?, ?, 'direct', 'auto', ?, 'clean')
            ON CONFLICT(id) DO UPDATE SET
                desired_mode = excluded.desired_mode,
                applied_mode = excluded.applied_mode,
                active_auto_server_id = excluded.active_auto_server_id,
                apply_state = 'clean',
                error_code = NULL,
                error_message = NULL,
                updated_at = CURRENT_TIMESTAMP
            """,
            (desired_mode, desired_mode, active_auto_server_id),
        )


def _seed_subscription_identity(*, slug: str, token: str, app_type: str = "auto") -> None:
    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO subscription_accounts (
                slug,
                display_name,
                enabled
            )
            VALUES (?, ?, 1)
            """,
            (slug, slug.title()),
        )
        account = connection.execute(
            "SELECT account_id FROM subscription_accounts WHERE slug = ? LIMIT 1",
            (slug,),
        ).fetchone()
        assert account is not None
        connection.execute(
            """
            INSERT INTO subscription_clients (
                account_id,
                token,
                app_type,
                enabled,
                display_name
            )
            VALUES (?, ?, ?, 1, ?)
            """,
            (account["account_id"], token, app_type, token.title()),
        )


def test_noop_xray_health_contract() -> None:
    adapter = NoopXrayAdapter()
    result = adapter.health()

    assert result.runtime_state == XrayRuntimeState.NOT_CONFIGURED
    assert result.details["adapter"] == "noop"


def test_parse_empty_config_clients_returns_empty(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [])
    adapter = _build_adapter(tmp_path)

    assert adapter.list_clients() == []


def test_health_missing_config(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    adapter = _build_adapter(tmp_path)
    result = adapter.health()

    assert result.runtime_state == XrayRuntimeState.NOT_CONFIGURED
    assert result.details["config_path"].replace("\\", "/").endswith("xray/config.json")
    assert result.details["forced_vpn_ready"] is False
    assert result.details["traffic_available"] is False


def test_health_valid_config_reports_forced_vpn_not_ready(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [{"id": "uuid-a", "email": "alice@example.test"}])
    runner = _FakeRunner()
    adapter = _build_adapter(tmp_path, runner=runner)

    result = adapter.health()

    assert result.runtime_state == XrayRuntimeState.RUNNING
    assert result.message == "Xray runtime is up, but forced VPN dataplane is not enabled yet."
    assert result.details["public_host"] == "xray.minisk.ru"
    assert result.details["public_port"] == 443
    assert result.details["public_path"] == "/vless"
    assert result.details["transport"] == "ws"
    assert result.details["clients_count"] == 1
    assert result.details["forced_vpn_ready"] is False
    assert result.details["traffic_available"] is False


def test_create_client_adds_uuid_to_config(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [])
    runner = _FakeRunner()
    adapter = _build_adapter(tmp_path, runner=runner)

    result = adapter.create_client(alias="Alice")
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    clients = payload["inbounds"][0]["settings"]["clients"]

    assert result.ok is True
    assert len(clients) == 1
    assert clients[0]["id"] == result.details["client"]["client_uuid"]
    assert clients[0]["email"] == "alice@fwrouter.local"


def test_create_client_preserves_existing_clients(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    config_path, _ = _xray_paths()
    _write_xray_config(
        config_path,
        [{"id": "uuid-existing", "email": "existing@example.test"}],
    )
    adapter = _build_adapter(tmp_path, runner=_FakeRunner())

    adapter.create_client(alias="Bob")
    clients = json.loads(config_path.read_text(encoding="utf-8"))["inbounds"][0]["settings"]["clients"]

    assert len(clients) == 2
    assert clients[0]["id"] == "uuid-existing"


def test_create_client_writes_atomically(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [])
    runner = _FakeRunner()
    adapter = _build_adapter(tmp_path, runner=runner)
    writes: list[Path] = []
    original_atomic_write = xray_adapter.atomic_write_text

    def _record_atomic_write(path: Path, text: str) -> None:
        writes.append(path)
        original_atomic_write(path, text)

    monkeypatch.setattr(xray_adapter, "atomic_write_text", _record_atomic_write)

    result = adapter.create_client(alias="Atomic")

    assert result.ok is True
    assert config_path in writes
    assert config_path.with_name("config.json.candidate") in writes


def test_duplicate_client_email_is_rejected(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [{"id": "uuid-one", "email": "dup@example.test"}])
    adapter = _build_adapter(tmp_path, runner=_FakeRunner())

    try:
        adapter.create_client(email="dup@example.test")
    except xray_adapter.XrayAdapterError as exc:
        assert exc.code == "XRAY_DUPLICATE_EMAIL"
    else:  # pragma: no cover - safety net
        raise AssertionError("Expected XRAY_DUPLICATE_EMAIL")


def test_delete_client_removes_only_selected(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    config_path, _ = _xray_paths()
    _write_xray_config(
        config_path,
        [
            {"id": "uuid-one", "email": "one@example.test"},
            {"id": "uuid-two", "email": "two@example.test"},
        ],
    )
    adapter = _build_adapter(tmp_path, runner=_FakeRunner())

    result = adapter.delete_client("uuid-one")
    clients = json.loads(config_path.read_text(encoding="utf-8"))["inbounds"][0]["settings"]["clients"]

    assert result.ok is True
    assert [client["id"] for client in clients] == ["uuid-two"]


def test_config_test_failure_returns_structured_error_and_does_not_reload(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    config_path, _ = _xray_paths()
    original_text = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    _write_xray_config(config_path, [])
    original_text = config_path.read_text(encoding="utf-8")
    runner = _FakeRunner()
    runner.test_result = XrayApplyResult(
        ok=False,
        message="test failed",
        error_code="XRAY_CONFIG_TEST_FAILED",
        details={"stderr": "bad config"},
    )
    adapter = _build_adapter(tmp_path, runner=runner)

    result = adapter.create_client(alias="Broken")

    assert result.ok is False
    assert result.error_code == "XRAY_CONFIG_TEST_FAILED"
    assert [call[0] for call in runner.calls] == ["test_config"]
    assert config_path.read_text(encoding="utf-8") == original_text


def test_reload_failure_after_config_save_does_not_rollback(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [])
    runner = _FakeRunner()
    runner.reload_result = XrayApplyResult(
        ok=False,
        message="reload failed",
        error_code="XRAY_RELOAD_FAILED",
        details={"stderr": "compose failed"},
    )
    adapter = _build_adapter(tmp_path, runner=runner)

    result = adapter.create_client(alias="Retry")
    clients = json.loads(config_path.read_text(encoding="utf-8"))["inbounds"][0]["settings"]["clients"]

    assert result.ok is False
    assert result.error_code == "XRAY_RELOAD_FAILED"
    assert len(clients) == 1
    assert clients[0]["email"] == "retry@fwrouter.local"
    assert [call[0] for call in runner.calls] == ["test_config", "reload"]


def test_alias_update_does_not_change_uuid(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [{"id": "uuid-alias", "email": "alias@example.test"}])
    adapter = _build_adapter(tmp_path, runner=_FakeRunner())
    _patch_xray_adapters(monkeypatch, adapter)
    inventory_service.sync_subject_inventory(
        requested_by="pytest",
        discover_docker=False,
        discover_tailscale=False,
        discover_xray=True,
    )

    result = xray_service.update_xray_client_alias("uuid-alias", alias="Display Alias", requested_by="pytest")
    subject = get_subject("xray:uuid-alias")

    assert result["ok"] is True
    assert result["client"]["client_uuid"] == "uuid-alias"
    assert subject is not None
    assert subject["alias"] == "Display Alias"


def test_list_clients_feeds_subject_inventory_discover_xray(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    config_path, _ = _xray_paths()
    _write_xray_config(
        config_path,
        [
            {"id": "uuid-a", "email": "alice@example.test"},
            {"id": "uuid-b", "email": "bob@example.test", "enable": False},
        ],
    )
    adapter = _build_adapter(tmp_path, runner=_FakeRunner())
    _patch_xray_adapters(monkeypatch, adapter)

    result = inventory_service.sync_subject_inventory(
        requested_by="pytest",
        discover_docker=False,
        discover_tailscale=False,
        discover_xray=True,
    )
    alice = get_subject("xray:uuid-a")
    bob = get_subject("xray:uuid-b")

    assert result["sources"]["xray"]["clients_count"] == 2
    assert alice is not None and alice["is_active"] == 1
    assert bob is not None and bob["is_active"] == 0
    assert alice["detail"]["subscription_path"].endswith("/uuid-a/subscription")


def test_subscription_uri_contains_expected_public_parameters(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [{"id": "uuid-sub", "email": "sub@example.test"}])
    adapter = _build_adapter(tmp_path, runner=_FakeRunner())

    result = adapter.export_vless_subscription("uuid-sub")
    uri = result.details["subscription_uri"]

    assert result.ok is True
    assert uri.startswith("vless://uuid-sub@xray.minisk.ru:443")
    assert "encryption=none" in uri
    assert "security=tls" in uri
    assert "sni=xray.minisk.ru" in uri
    assert "type=ws" in uri
    assert "host=xray.minisk.ru" in uri
    assert "path=%2Fvless" in uri
    assert "alpn=http%2F1.1" in uri
    assert "fp=chrome" in uri
    assert "packetEncoding=xudp" in uri


def test_export_xray_subscription_text_contains_alpn_and_fp(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [{"id": "uuid-text", "email": "text@example.test"}])
    adapter = _build_adapter(tmp_path, runner=_FakeRunner())
    _patch_xray_adapters(monkeypatch, adapter)

    exported = xray_service.export_xray_subscription_text("uuid-text", base64_encode=False)

    assert exported["ok"] is True
    assert exported["content"].startswith("vless://uuid-text@xray.minisk.ru:443")
    assert "alpn=http%2F1.1" in exported["content"]
    assert "fp=chrome" in exported["content"]
    assert "packetEncoding=xudp" in exported["content"]


def test_export_xray_subscription_includes_fwrouter_binding_context(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _patch_runtime(monkeypatch)
    monkeypatch.setattr(
        subject_policy_service,
        "build_runtime_enforcement_state",
        lambda: {
            "supported_modes": {"direct": True, "selective": False, "vpn": True},
            "enforcement_level": "global_vpn_enforced",
            "traffic_enforcement_guaranteed": True,
        },
    )
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [{"id": "uuid-export", "email": "export@example.test"}])
    adapter = _build_adapter(tmp_path, runner=_FakeRunner())
    _patch_xray_adapters(monkeypatch, adapter)
    inventory_service.sync_subject_inventory(
        requested_by="pytest",
        discover_docker=False,
        discover_tailscale=False,
        discover_xray=True,
    )
    _seed_server("server-1")
    _seed_routing_state(desired_mode="vpn", active_auto_server_id="server-1")

    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO subject_server_overrides (
                subject_id,
                selected_server_id,
                selected_until,
                apply_state
            )
            VALUES (?, ?, datetime('now', '+24 hours'), 'pending')
            """,
            ("xray:uuid-export", "server-1"),
        )

    exported = xray_service.export_xray_subscription("uuid-export")

    assert exported["ok"] is True
    assert exported["subject_id"] == "xray:uuid-export"


def test_materialize_client_bindings_enables_xray_stats_api(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [{"id": "uuid-stats", "email": "stats@example.test"}])
    adapter = _build_adapter(tmp_path)

    result = adapter.materialize_client_bindings(
        [
            {
                "subject_id": "xray:uuid-stats",
                "client_id": "uuid-stats",
                "client_uuid": "uuid-stats",
                "client_email": "stats@example.test",
                "selected_server_id": "server-1",
                "selected_server_source": "vpn_auto",
                "status": "applied",
                "match_key": "xray-client-uuid:uuid-stats",
                "applied_at": "2026-06-02T00:00:00+00:00",
            }
        ]
    )

    assert result.ok is True

    payload = json.loads(config_path.read_text(encoding="utf-8"))
    assert payload["api"]["tag"] == "fwrouter-api"
    assert payload["api"]["services"] == ["StatsService"]
    assert payload["stats"] == {}
    assert payload["policy"]["levels"]["0"]["statsUserUplink"] is True
    assert payload["policy"]["levels"]["0"]["statsUserDownlink"] is True

    api_inbound = next(inbound for inbound in payload["inbounds"] if inbound.get("tag") == "fwrouter-api")
    assert api_inbound["listen"] == "127.0.0.1"
    assert api_inbound["port"] == 10085
    assert api_inbound["protocol"] == "dokodemo-door"

    api_outbound = next(outbound for outbound in payload["outbounds"] if outbound.get("tag") == "fwrouter-api")
    assert api_outbound["protocol"] == "freedom"

    api_rule = next(rule for rule in payload["routing"]["rules"] if rule.get("outboundTag") == "fwrouter-api")
    assert api_rule["inboundTag"] == ["fwrouter-api"]


def test_route_smoke_through_testclient(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [])
    adapter = _build_adapter(tmp_path, runner=_FakeRunner())
    _patch_xray_adapters(monkeypatch, adapter)
    app = create_app(enable_startup_tasks=False)

    with TestClient(app) as client:
        status = client.get("/api/v2/xray")
        created = client.post(
            "/api/v2/xray/clients",
            json={"alias": "Portal", "requested_by": "pytest"},
        )
        clients = client.get("/api/v2/xray/clients")
        subscription = client.get(f"/api/v2/xray/clients/{created.json()['data']['xray_client']['client']['client_id']}/subscription")
        synced = client.post("/api/v2/xray/sync-subjects", json={"requested_by": "pytest"})

    assert status.status_code == 200
    assert status.json()["data"]["xray"]["forced_vpn_ready"] is False
    assert created.status_code == 200
    assert clients.status_code == 200
    assert len(clients.json()["data"]["clients"]) == 1
    assert subscription.status_code == 200
    assert "xray.minisk.ru:443" in subscription.json()["data"]["subscription"]["subscription_uri"]
    assert synced.status_code == 200


def test_public_subscription_route_detects_happ(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [])
    adapter = _build_adapter(tmp_path, runner=_FakeRunner())
    _patch_xray_adapters(monkeypatch, adapter)
    _seed_server("server-1")
    _seed_subscription_identity(slug="stepan", token="stepan", app_type="auto")
    app = create_app(enable_startup_tasks=False)

    with TestClient(app) as client:
        response = client.get(
            "/s/stepan",
            headers={"User-Agent": "Happ/3.19.1/Android/test"},
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert response.headers["x-fwrouter-detected-format"] == "happ"
    assert response.headers["x-fwrouter-renderer"] == "happ"
    assert response.headers["profile-update-interval"] == "1"
    assert response.headers["profile-title"] == "Stepan"
    assert response.headers["subscription-userinfo"] == "upload=0; download=0; total=0; expire=0"
    assert not response.text.startswith("vless://")
    assert "#profile" not in response.text
    decoded = base64.b64decode(response.text).decode("utf-8")
    assert decoded.count("vless://") == 2
    assert "alpn=" not in decoded
    assert "fp=" not in decoded
    assert "packetEncoding=" not in decoded


def test_public_subscription_route_explicit_happ_format_wins(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [])
    adapter = _build_adapter(tmp_path, runner=_FakeRunner())
    _patch_xray_adapters(monkeypatch, adapter)
    _seed_server("server-1")
    _seed_subscription_identity(slug="stepan", token="stepan", app_type="auto")
    app = create_app(enable_startup_tasks=False)

    with TestClient(app) as client:
        response = client.get("/s/stepan?format=happ")

    assert response.status_code == 200
    assert response.headers["x-fwrouter-detected-format"] == "happ"
    assert response.headers["x-fwrouter-renderer"] == "happ"
    assert response.headers["profile-update-interval"] == "1"
    assert response.headers["profile-title"] == "Stepan"
    assert not response.text.startswith("vless://")
    assert "#profile" not in response.text
    decoded = base64.b64decode(response.text).decode("utf-8")
    assert decoded.startswith("vless://")
    assert decoded.endswith("\n")
    assert "encryption=none" in decoded
    assert "type=ws" in decoded
    assert "security=tls" in decoded
    assert "sni=xray.minisk.ru" in decoded
    assert "host=xray.minisk.ru" in decoded
    assert "path=%2Fvless" in decoded
    assert "alpn=" not in decoded
    assert "fp=" not in decoded
    assert "packetEncoding=" not in decoded


def test_public_subscription_route_happ_base64_multinode(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [])
    adapter = _build_adapter(tmp_path, runner=_FakeRunner())
    _patch_xray_adapters(monkeypatch, adapter)
    _seed_server("server-1")
    _seed_server("server-2")
    _seed_server("server-3")
    _seed_subscription_identity(slug="stepan", token="stepan", app_type="auto")
    app = create_app(enable_startup_tasks=False)

    with TestClient(app) as client:
        response = client.get(
            "/s/stepan?format=happ",
            headers={"User-Agent": "Happ/3.19.1/Android/test"},
        )

    assert response.status_code == 200
    assert response.headers["profile-update-interval"] == "1"
    assert response.headers["profile-title"] == "Stepan"
    decoded = base64.b64decode(response.text).decode("utf-8")
    lines = [line for line in decoded.splitlines() if line.strip()]
    assert len(lines) == 4
    assert all(line.startswith("vless://") for line in lines)
    assert all("path=%2Fvless" in line for line in lines)
    assert all("alpn=" not in line for line in lines)
    assert all("fp=" not in line for line in lines)
    assert all("packetEncoding=" not in line for line in lines)


def test_public_subscription_route_detects_clash(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [])
    adapter = _build_adapter(tmp_path, runner=_FakeRunner())
    _patch_xray_adapters(monkeypatch, adapter)
    _seed_server("server-1")
    _seed_subscription_identity(slug="stepan", token="device-1", app_type="auto")
    app = create_app(enable_startup_tasks=False)

    with TestClient(app) as client:
        response = client.get(
            "/s/device-1",
            headers={"User-Agent": "FlClash/1.0"},
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/yaml")
    assert "client-fingerprint: chrome" in response.text
    assert "alpn:" in response.text
    assert 'server: "xray.minisk.ru"' in response.text
    assert 'servername: "xray.minisk.ru"' in response.text
    assert 'Host: "xray.minisk.ru"' in response.text
    assert 'path: "/vless"' in response.text


def test_public_subscription_route_explicit_clash_format_wins(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [])
    adapter = _build_adapter(tmp_path, runner=_FakeRunner())
    _patch_xray_adapters(monkeypatch, adapter)
    _seed_server("server-1")
    _seed_subscription_identity(slug="stepan", token="stepan", app_type="auto")
    app = create_app(enable_startup_tasks=False)

    with TestClient(app) as client:
        response = client.get("/s/stepan?format=flclashx")

    assert response.status_code == 200
    assert response.headers["x-fwrouter-detected-format"] == "clash"
    assert response.headers["x-fwrouter-renderer"] == "clash"
    assert response.headers["content-type"].startswith("application/yaml")
    assert "proxies:" in response.text


def test_public_subscription_route_explicit_raw_vless_format_wins(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [])
    adapter = _build_adapter(tmp_path, runner=_FakeRunner())
    _patch_xray_adapters(monkeypatch, adapter)
    _seed_server("server-1")
    _seed_subscription_identity(slug="stepan", token="stepan", app_type="happ")
    app = create_app(enable_startup_tasks=False)

    with TestClient(app) as client:
        response = client.get("/s/stepan?format=raw-vless")

    assert response.status_code == 200
    assert response.headers["x-fwrouter-detected-format"] == "raw-vless"
    assert response.headers["x-fwrouter-renderer"] == "raw-vless"
    assert response.text.startswith("vless://")
    assert "#profile-title:" not in response.text


def test_public_subscription_route_rejects_removed_happ_json_format(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [])
    adapter = _build_adapter(tmp_path, runner=_FakeRunner())
    _patch_xray_adapters(monkeypatch, adapter)
    _seed_server("server-1")
    _seed_server("server-2")
    _seed_server("server-3")
    _seed_subscription_identity(slug="stepan", token="stepan", app_type="auto")
    app = create_app(enable_startup_tasks=False)

    with TestClient(app) as client:
        response = client.get("/s/stepan?format=happ-json")

    assert response.status_code == 200
    assert response.headers["x-fwrouter-detected-format"] == "raw-vless"
    assert response.headers["x-fwrouter-renderer"] == "raw-vless"
    assert response.text.startswith("vless://")
    assert "vpn.minisk.ru" not in response.text


def test_public_subscription_route_rejects_removed_happ_full_json_format(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [])
    adapter = _build_adapter(tmp_path, runner=_FakeRunner())
    _patch_xray_adapters(monkeypatch, adapter)
    _seed_server("server-1")
    _seed_subscription_identity(slug="stepan", token="stepan", app_type="auto")
    app = create_app(enable_startup_tasks=False)

    with TestClient(app) as client:
        response = client.get("/s/stepan?format=happ-full-json")

    assert response.status_code == 200
    assert response.headers["x-fwrouter-detected-format"] == "raw-vless"
    assert response.headers["x-fwrouter-renderer"] == "raw-vless"
    assert response.text.startswith("vless://")
    assert "vpn.minisk.ru" not in response.text


def test_legacy_subscription_txt_route_named_profile_returns_404(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [])
    adapter = _build_adapter(tmp_path, runner=_FakeRunner())
    _patch_xray_adapters(monkeypatch, adapter)
    _seed_server("server-1")
    _seed_subscription_identity(slug="stepan", token="stepan", app_type="auto")
    app = create_app(enable_startup_tasks=False)

    with TestClient(app) as client:
        response = client.get(
            "/api/v2/xray/clients/stepan/subscription.txt",
            headers={"User-Agent": "Happ/3.19.1/Android/test"},
        )

    assert response.status_code == 404


def test_reconcile_xray_subscription_profiles_include_socks_handoff_nodes(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _patch_runtime(monkeypatch)
    monkeypatch.setattr(
        subject_policy_service,
        "build_runtime_enforcement_state",
        lambda: {
            "supported_modes": {"direct": True, "selective": False, "vpn": True},
            "enforcement_level": "global_vpn_enforced",
            "traffic_enforcement_guaranteed": True,
        },
    )
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [])
    adapter = _build_adapter(tmp_path, runner=_FakeRunner())
    _patch_xray_adapters(monkeypatch, adapter)
    _seed_server("server-1")
    _seed_routing_state(desired_mode="vpn", active_auto_server_id="server-1")
    _seed_subscription_identity(slug="stepan", token="device-1", app_type="happ")
    with db_session() as connection:
        connection.execute(
            "UPDATE modules SET desired_state = 'enabled', runtime_state = 'running' WHERE module_name = 'xray'"
        )

    result = xray_service.reconcile_xray_subscription_profile_nodes(
        requested_by="pytest",
        materialize=False,
    )
    materialized = xray_service.materialize_xray_runtime_bindings(
        requested_by="pytest",
        prepare_mihomo_handoff=False,
    )

    assert result["ok"] is True
    assert materialized["ok"] is True
    config_payload = json.loads(config_path.read_text(encoding="utf-8"))
    emails = {
        client.get("email")
        for client in config_payload["inbounds"][0]["settings"]["clients"]
    }
    assert any(str(email).startswith("sub-") for email in emails)
    managed_outbounds = [
        outbound
        for outbound in config_payload["outbounds"]
        if str(outbound.get("tag") or "").startswith("fwrouter-egress-")
    ]
    assert managed_outbounds
    assert all(outbound["protocol"] == "socks" for outbound in managed_outbounds)


def test_runtime_summary_integration(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [{"id": "uuid-runtime", "email": "runtime@example.test"}])
    adapter = _build_adapter(tmp_path, runner=_FakeRunner())
    _patch_xray_adapters(monkeypatch, adapter)

    summary = runtime_service.get_runtime_summary()

    assert summary["xray"]["adapter"] == "xray"
    assert summary["xray"]["runtime_state"] == "running"
    assert summary["xray"]["forced_vpn_ready"] is False
    assert summary["xray"]["traffic_available"] is False


def test_xray_effective_state_for_enabled_subject(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [{"id": "uuid-enabled", "email": "enabled@example.test"}])
    adapter = _build_adapter(tmp_path, runner=_FakeRunner())
    _patch_xray_adapters(monkeypatch, adapter)
    inventory_service.sync_subject_inventory(
        requested_by="pytest",
        discover_docker=False,
        discover_tailscale=False,
        discover_xray=True,
    )

    subject = get_subject_with_effective_state("xray:uuid-enabled")

    assert subject is not None
    assert subject["effective_state"]["effective_mode"] == "forced_vpn"


def test_xray_server_override_exposes_pending_scoped_runtime(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _patch_runtime(monkeypatch)
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [{"id": "uuid-binding", "email": "binding@example.test"}])
    adapter = _build_adapter(tmp_path, runner=_FakeRunner())
    _patch_xray_adapters(monkeypatch, adapter)
    inventory_service.sync_subject_inventory(
        requested_by="pytest",
        discover_docker=False,
        discover_tailscale=False,
        discover_xray=True,
    )
    _seed_server("server-1")
    _seed_routing_state(desired_mode="vpn", active_auto_server_id="server-1")
    monkeypatch.setattr(
        subject_policy_service,
        "build_runtime_enforcement_state",
        lambda: {
            "supported_modes": {"direct": True, "selective": False, "vpn": True},
            "enforcement_level": "global_vpn_enforced",
            "traffic_enforcement_guaranteed": True,
        },
    )

    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO subject_server_overrides (
                subject_id,
                selected_server_id,
                selected_until,
                apply_state
            )
            VALUES (?, ?, datetime('now', '+24 hours'), 'pending')
            """,
            ("xray:uuid-binding", "server-1"),
        )

    subject = get_subject_with_effective_state("xray:uuid-binding")

    assert subject is not None
    scoped_runtime = subject["effective_state"]["scoped_runtime"]
    assert scoped_runtime["tracked"] is True
    assert scoped_runtime["eligible"] is True
    assert scoped_runtime["applied"] is False
    assert scoped_runtime["status"] == "pending_unresolved_subject_match"
    assert scoped_runtime["selected_server_id"] == "server-1"
    assert scoped_runtime["selected_server_source"] == "subject_override"
    assert scoped_runtime["match_key"] == "xray-client-uuid:uuid-binding"
    assert scoped_runtime["resolution_reason"] == "subject_xray_runtime_binding_missing"
    summary = runtime_service.get_runtime_summary()
    assert summary["dataplane"]["scoped_egress"]["state"] == "degraded"
    assert summary["dataplane"]["scoped_egress"]["unresolved_count"] >= 1


def test_xray_server_override_endpoint_persists_pending_runtime_gap(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    register_extended_handlers(get_default_job_manager())
    _patch_runtime(monkeypatch)
    monkeypatch.setattr(
        subject_policy_service,
        "build_runtime_enforcement_state",
        lambda: {
            "supported_modes": {"direct": True, "selective": False, "vpn": True},
            "enforcement_level": "global_vpn_enforced",
            "traffic_enforcement_guaranteed": True,
        },
    )
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [{"id": "uuid-route", "email": "route@example.test"}])
    adapter = _build_adapter(tmp_path, runner=_FakeRunner())
    _patch_xray_adapters(monkeypatch, adapter)
    inventory_service.sync_subject_inventory(
        requested_by="pytest",
        discover_docker=False,
        discover_tailscale=False,
        discover_xray=True,
    )
    _seed_server("server-1")
    _seed_routing_state(desired_mode="vpn", active_auto_server_id="server-1")

    app = create_app(enable_startup_tasks=False)
    with TestClient(app) as client:
        response = client.post(
            "/api/v2/subjects/xray:uuid-route/server-override",
            json={"server_id": "server-1", "requested_by": "pytest"},
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["ok"] is True
        override = payload["data"]["server_override"]
        assert override["apply_state"] == "clean"
        assert override["error_code"] is None

        subject = client.get("/api/v2/subjects/xray:uuid-route").json()["data"]["subject"]
        scoped_runtime = subject["effective_state"]["scoped_runtime"]
        assert scoped_runtime["tracked"] is True
        assert scoped_runtime["status"] == "applied"
        assert scoped_runtime["match_key"] == "xray-client-uuid:uuid-route"
        assert scoped_runtime["materialized_by"] == "xray_runtime_metadata"


def test_xray_binding_materialization_writes_runtime_metadata(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    register_extended_handlers(get_default_job_manager())
    _patch_runtime(monkeypatch)
    monkeypatch.setattr(
        subject_policy_service,
        "build_runtime_enforcement_state",
        lambda: {
            "supported_modes": {"direct": True, "selective": False, "vpn": True},
            "enforcement_level": "global_vpn_enforced",
            "traffic_enforcement_guaranteed": True,
        },
    )
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [{"id": "uuid-materialized", "email": "materialized@example.test"}])
    adapter = _build_adapter(tmp_path, runner=_FakeRunner())
    _patch_xray_adapters(monkeypatch, adapter)
    inventory_service.sync_subject_inventory(
        requested_by="pytest",
        discover_docker=False,
        discover_tailscale=False,
        discover_xray=True,
    )
    _seed_server("server-1")
    _seed_routing_state(desired_mode="vpn", active_auto_server_id="server-1")

    app = create_app(enable_startup_tasks=False)
    with TestClient(app) as client:
        response = client.post(
            "/api/v2/subjects/xray:uuid-materialized/server-override",
            json={"server_id": "server-1", "requested_by": "pytest"},
        )
        assert response.status_code == 200

    materialized_subject = get_subject_with_effective_state("xray:uuid-materialized")
    assert materialized_subject is not None
    assert materialized_subject["effective_state"]["scoped_runtime"]["status"] in [
        "applied",
        "pending_unresolved_subject_match",
    ]
    config_payload = json.loads(config_path.read_text(encoding="utf-8"))
    client_payload = config_payload["inbounds"][0]["settings"]["clients"][0]
    # skip fwrouterBinding check
    assert client_payload["fwrouterBinding"]["subject_id"] == "xray:uuid-materialized"


def test_xray_collects_vpn_auto_bindings_without_active_auto_server_id(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    register_extended_handlers(get_default_job_manager())
    _patch_runtime(monkeypatch)
    monkeypatch.setattr(
        subject_policy_service,
        "build_runtime_enforcement_state",
        lambda: {
            "supported_modes": {"direct": True, "selective": False, "vpn": True},
            "enforcement_level": "global_vpn_enforced",
            "traffic_enforcement_guaranteed": True,
        },
    )
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [{"id": "uuid-auto", "email": "auto@example.test"}])
    adapter = _build_adapter(tmp_path, runner=_FakeRunner())
    _patch_xray_adapters(monkeypatch, adapter)
    inventory_service.sync_subject_inventory(
        requested_by="pytest",
        discover_docker=False,
        discover_tailscale=False,
        discover_xray=True,
    )
    _seed_routing_state(desired_mode="vpn", active_auto_server_id=None)

    bindings = xray_service.collect_xray_runtime_bindings()

    assert len(bindings) == 1
    assert bindings[0]["subject_id"] == "xray:uuid-auto"
    assert bindings[0]["selected_server_id"] == "vpn-global"
    assert bindings[0]["selected_server_source"] == "vpn_auto"
    subject = get_subject_with_effective_state("xray:uuid-auto")
    assert subject is not None
    scoped_runtime = subject["effective_state"]["scoped_runtime"]
    assert scoped_runtime["selected_server_id"] == "vpn-global"
    assert scoped_runtime["status"] == "pending_unresolved_subject_match"


def test_xray_collects_bindings_without_per_subject_runtime_snapshot_calls(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    register_extended_handlers(get_default_job_manager())
    _patch_runtime(monkeypatch)
    monkeypatch.setattr(
        subject_policy_service,
        "build_runtime_enforcement_state",
        lambda: {
            "supported_modes": {"direct": True, "selective": False, "vpn": True},
            "enforcement_level": "global_vpn_enforced",
            "traffic_enforcement_guaranteed": True,
        },
    )
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [{"id": "uuid-fast", "email": "fast@example.test"}])
    adapter = _build_adapter(tmp_path, runner=_FakeRunner())
    _patch_xray_adapters(monkeypatch, adapter)
    inventory_service.sync_subject_inventory(
        requested_by="pytest",
        discover_docker=False,
        discover_tailscale=False,
        discover_xray=True,
    )
    _seed_routing_state(desired_mode="vpn", active_auto_server_id=None)
    monkeypatch.setattr(
        xray_service,
        "get_subject_with_effective_state",
        lambda subject_id: (_ for _ in ()).throw(AssertionError("legacy path must not be used")),
    )

    bindings = xray_service.collect_xray_runtime_bindings()

    assert len(bindings) == 1
    assert bindings[0]["subject_id"] == "xray:uuid-fast"


def test_xray_user_override_is_forbidden(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [{"id": "uuid-forbidden", "email": "forbidden@example.test"}])
    adapter = _build_adapter(tmp_path, runner=_FakeRunner())
    _patch_xray_adapters(monkeypatch, adapter)
    inventory_service.sync_subject_inventory(
        requested_by="pytest",
        discover_docker=False,
        discover_tailscale=False,
        discover_xray=True,
    )

    result = set_subject_mode("xray:uuid-forbidden", "vpn", actor_scope="user", requested_by="pytest")

    assert result["ok"] is False
    assert result["code"] == "SUBJECT_MODE_FORBIDDEN"


def test_list_subjects_with_effective_state_includes_xray(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [{"id": "uuid-list", "email": "list@example.test"}])
    adapter = _build_adapter(tmp_path, runner=_FakeRunner())
    _patch_xray_adapters(monkeypatch, adapter)
    inventory_service.sync_subject_inventory(
        requested_by="pytest",
        discover_docker=False,
        discover_tailscale=False,
        discover_xray=True,
    )

    subjects = list_subjects_with_effective_state(subject_type="xray")

    assert len(subjects) == 1
    assert subjects[0]["lifecycle"]["visible_in_ui"] is True
    assert subjects[0]["effective_state"]["effective_mode"] == "forced_vpn"
