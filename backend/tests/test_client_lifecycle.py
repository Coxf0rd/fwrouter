from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from fwrouter_api.adapters.xray import RealXrayAdapter, XrayApplyResult, XrayClient
from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import db_session, initialize_database
from fwrouter_api.main import create_app
from fwrouter_api.services import subject_inventory as inventory_service
from fwrouter_api.services import xray as xray_service
from fwrouter_api.services import xray_runtime_state as xray_runtime_state_service
from fwrouter_api.services import xray_subscription_service
from fwrouter_api.adapters import xray as xray_adapter
from fwrouter_api.services.live_probe_cache import clear_live_probe_cache
from fwrouter_api.services.maintenance import repair_orphan_subscription_clients
from fwrouter_api.services.ui_state import list_ui_clients, list_ui_settings_inventory


def _configure_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FWROUTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("FWROUTER_DATABASE_URL", f"sqlite:///{tmp_path}/fwrouter.db")
    monkeypatch.setenv("FWROUTER_XRAY_PUBLIC_HOST", "xray.example.test")
    monkeypatch.setenv("FWROUTER_JOB_RUN_NOW_WAIT_TIMEOUT_SECONDS", "1")
    get_settings.cache_clear()
    clear_live_probe_cache()


def _xray_paths() -> tuple[Path, Path]:
    settings = get_settings()
    return settings.paths.state_dir / "xray" / "config.json", settings.paths.state_dir / "xray" / "docker-compose.yml"


def _write_xray_config(config_path: Path, clients: list[dict[str, object]] | None = None) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(
            {
                "inbounds": [
                    {
                        "protocol": "vless",
                        "settings": {"clients": clients or [], "decryption": "none"},
                        "streamSettings": {"network": "ws", "wsSettings": {"path": "/vless"}},
                    }
                ],
                "outbounds": [{"protocol": "freedom", "tag": "direct"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


class _FakeRunner:
    def __init__(self, *, release: threading.Event | None = None) -> None:
        self.release = release
        self.calls: list[str] = []

    def __call__(self, action: str, payload: dict[str, object]) -> XrayApplyResult:
        self.calls.append(action)
        if action == "reload" and self.release is not None:
            self.release.wait(timeout=5)
        if action == "compose_ps":
            return XrayApplyResult(
                ok=True,
                message="compose ps ok",
                details={"stdout": '[{"Service":"fwrouter-xray","State":"running"}]'},
            )
        return XrayApplyResult(ok=True, message=f"{action} ok", details={"action": action})


def _patch_xray(monkeypatch, tmp_path: Path, runner: _FakeRunner | None = None) -> RealXrayAdapter:
    config_path, compose_path = _xray_paths()
    compose_path.parent.mkdir(parents=True, exist_ok=True)
    compose_path.write_text("services:\n  fwrouter-xray:\n    image: teddysun/xray\n", encoding="utf-8")
    adapter = RealXrayAdapter(
        config_path=config_path,
        compose_path=compose_path,
        log_root=tmp_path / "log" / "xray",
        runner=runner or _FakeRunner(),
    )
    monkeypatch.setattr(xray_adapter, "DEFAULT_XRAY_ADAPTER", adapter)
    monkeypatch.setattr(xray_service, "DEFAULT_XRAY_ADAPTER", adapter)
    monkeypatch.setattr(inventory_service, "DEFAULT_XRAY_ADAPTER", adapter)
    monkeypatch.setattr(xray_runtime_state_service, "DEFAULT_XRAY_ADAPTER", adapter)
    return adapter


def _utc(seconds_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).replace(microsecond=0).isoformat()


def _seed_xray_subject(
    subject_id: str,
    *,
    email: str,
    enabled: bool = True,
    runtime_present: bool = True,
    last_seen_at: str | None = None,
    last_traffic_at: str | None = None,
) -> None:
    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO subjects (
                subject_id, subject_type, subject_role, implementation_kind, stable_key,
                display_name, desired_mode, runtime_state, is_active, last_seen_at, last_traffic_at,
                metadata_json
            )
            VALUES (?, 'explicit_external_client', 'vless_client', 'xray', ?, ?, 'enabled', ?, ?, ?, ?, json(?))
            """,
            (
                subject_id,
                subject_id,
                email,
                "active" if runtime_present else "inactive",
                1 if runtime_present else 0,
                last_seen_at,
                last_traffic_at,
                json.dumps(
                    {
                        "provider": "xray",
                        "detail": {
                            "client_id": subject_id.split(":", 1)[-1],
                            "client_uuid": subject_id.split(":", 1)[-1],
                            "email": email,
                            "enabled": enabled,
                        },
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            ),
        )


def test_xray_online_requires_confirmed_activity(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _seed_xray_subject("xray:fresh", email="fresh", last_traffic_at=_utc(60))
    _seed_xray_subject("xray:stale", email="stale", last_traffic_at=_utc(25 * 60 * 60))
    _seed_xray_subject("xray:runtime-only", email="runtime-only")
    _seed_xray_subject("xray:disabled", email="disabled", enabled=False, last_traffic_at=_utc(60))
    _seed_xray_subject("xray:deleted", email="deleted", last_traffic_at=_utc(60))
    with db_session() as connection:
        connection.execute("UPDATE subjects SET is_deleted = 1 WHERE subject_id = 'xray:deleted'")

    clients = {item["subject_id"]: item for item in list_ui_clients()}

    assert clients["xray:fresh"]["online"] is True
    assert clients["xray:fresh"]["is_active"] is True
    assert clients["xray:fresh"]["activity_reason"] == "traffic_seen"
    assert clients["xray:stale"]["online"] is False
    assert clients["xray:stale"]["activity_reason"] == "stale_seen"
    assert clients["xray:runtime-only"]["online"] is False
    assert clients["xray:runtime-only"]["activity_reason"] == "runtime_present"
    assert clients["xray:disabled"]["online"] is False
    assert "xray:deleted" not in clients


def test_misha_like_projection_uses_newest_confirmed_activity(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _seed_xray_subject("xray:sub-misha-ee", email="sub-misha-ee@fwrouter.local", last_traffic_at="2026-09-09T15:39:50+00:00")
    with db_session() as connection:
        connection.execute(
            "UPDATE subjects SET display_name = 'Misha / Misha / Estonia' WHERE subject_id = 'xray:sub-misha-ee'"
        )
    with db_session() as connection:
        connection.execute("INSERT INTO subscription_accounts (account_id, slug, display_name, enabled) VALUES (10, 'misha', 'Misha', 1)")
        connection.execute(
            """
            INSERT INTO subscription_clients (client_id, account_id, token, app_type, enabled, display_name, last_seen_at)
            VALUES (10, 10, 'misha', 'auto', 1, 'Misha', '2026-09-07 16:57:29')
            """
        )

    inventory = {item["subject_id"]: item for item in list_ui_settings_inventory(role="vless_client", include_inactive=True, live_observations=False)}

    assert inventory["xray-subscription:misha"]["last_activity_at"] == "2026-09-09T15:39:50+00:00"
    assert inventory["xray-subscription:misha"]["last_seen_at"] == "2026-09-09T15:39:50+00:00"
    assert inventory["xray-subscription:misha"]["online"] is False
    assert inventory["xray-subscription:misha"]["activity_reason"] == "stale_seen"


def test_xray_inventory_presence_does_not_update_semantic_last_seen(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [{"id": "uuid-presence", "email": "presence@example.test"}])
    _patch_xray(monkeypatch, tmp_path)

    inventory_service.sync_subject_inventory(requested_by="pytest", discover_docker=False, discover_tailscale=False, discover_xray=True)

    with db_session() as connection:
        row = connection.execute("SELECT is_active, last_seen_at FROM subjects WHERE subject_id = 'xray:uuid-presence'").fetchone()
    assert row is not None
    assert row["is_active"] == 1
    assert row["last_seen_at"] is None


def test_xray_create_route_returns_running_job_when_runtime_is_slow(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [])
    release = threading.Event()
    _patch_xray(monkeypatch, tmp_path, _FakeRunner(release=release))

    app = create_app(enable_startup_tasks=False)
    started = time.monotonic()
    with TestClient(app) as client:
        response = client.post("/api/v2/xray/clients", json={"alias": "Slow", "email": "slow", "requested_by": "pytest"})
    elapsed = time.monotonic() - started
    release.set()

    assert elapsed < 2.5
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["data"]["job"]["status"] == "running"
    assert payload["data"]["job"]["job_type"] == "xray_client_create"


def test_xray_create_retry_returns_active_job(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [])
    release = threading.Event()
    _patch_xray(monkeypatch, tmp_path, _FakeRunner(release=release))

    app = create_app(enable_startup_tasks=False)
    with TestClient(app) as client:
        first = client.post("/api/v2/xray/clients", json={"alias": "Slow", "email": "slow", "requested_by": "pytest"})
        second = client.post("/api/v2/xray/clients", json={"alias": "Slow", "email": "slow", "requested_by": "pytest"})
    release.set()

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["data"]["job"]["job_id"] == second.json()["data"]["job"]["job_id"]


def test_orphan_subscription_client_repair_preserves_history(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    with db_session() as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute(
            """
            INSERT INTO subscription_clients (client_id, account_id, token, app_type, enabled, display_name, last_seen_at)
            VALUES (5, 5, 'sveta', 'auto', 1, 'Sveta', '2026-05-28 00:00:00')
            """
        )

    dry_run = repair_orphan_subscription_clients(dry_run=True)
    repaired = repair_orphan_subscription_clients(dry_run=False)

    assert dry_run["repairable_count"] == 1
    assert repaired["repaired_accounts_count"] == 1
    assert repaired["disabled_clients_count"] == 1
    with db_session() as connection:
        fk_rows = connection.execute("PRAGMA foreign_key_check").fetchall()
        account = connection.execute("SELECT enabled FROM subscription_accounts WHERE account_id = 5").fetchone()
        client = connection.execute("SELECT enabled FROM subscription_clients WHERE client_id = 5").fetchone()
    assert fk_rows == []
    assert account is not None and account["enabled"] == 0
    assert client is not None and client["enabled"] == 0
