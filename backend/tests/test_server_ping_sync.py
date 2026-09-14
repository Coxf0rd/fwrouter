from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from fwrouter_api.adapters.mihomo import MihomoDelayResult
from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import db_session, initialize_database
from fwrouter_api.main import create_app
from fwrouter_api.services import server_ping


def _configure_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FWROUTER_STATE_DIR", str(tmp_path / "state"))
    get_settings.cache_clear()


def _seed_server(
    server_id: str,
    *,
    server_name: str | None = None,
    raw_json: dict[str, object] | None = None,
) -> None:
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
            VALUES (?, ?, 'pytest', 'active', ?)
            """,
            (
                server_id,
                server_name or server_id,
                json.dumps(raw_json, ensure_ascii=False, sort_keys=True) if raw_json else None,
            ),
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


class _FakeMihomoAdapter:
    def __init__(self, *, ok: bool = True, delay_ms: int | None = 42) -> None:
        self.checked_targets: list[str] = []
        self.ok = ok
        self.delay_ms = delay_ms

    def check_delay(self, server_id: str, *, test_url: str, timeout_ms: int) -> MihomoDelayResult:
        self.checked_targets.append(server_id)
        return MihomoDelayResult(
            ok=self.ok,
            server_id=server_id,
            delay_ms=self.delay_ms,
            test_url=test_url,
            timeout_ms=timeout_ms,
            error_code=None if self.ok else "PING_FAILED",
            error_message=None if self.ok else "Synthetic ping failure.",
        )


def test_server_ping_update_is_visible_through_canonical_servers_state(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _seed_server("server-a")
    monkeypatch.setattr(server_ping, "DEFAULT_MIHOMO_ADAPTER", _FakeMihomoAdapter())

    measured = server_ping.check_server_delay(
        "server-a",
        update_state=True,
        checked_by="pytest-user",
    )
    assert measured["ok"] is True
    assert measured["last_ping_ms"] == 42
    assert measured["latency_ms"] == 42
    assert measured["runtime_target"] == "server-a"
    assert measured["source"] == "manual"

    client = TestClient(create_app(enable_startup_tasks=False))
    payload = client.get("/api/v2/servers?inventory_state=active&limit=1000").json()

    assert payload["ok"] is True
    [server] = payload["data"]["servers"]
    assert server["server_id"] == "server-a"
    assert server["ping"]["status"] == "success"
    assert server["ping"]["last_ping_ms"] == 42
    assert server["ping"]["checked_by"] == "pytest-user"


def test_server_ping_uses_runtime_name_for_subscription_server(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _seed_server(
        "sub:abc123",
        server_name="Duplicate Display",
        raw_json={
            "name": "Duplicate Display [abc123]",
            "_fwrouter_runtime_name": "Duplicate Display [abc123]",
        },
    )
    adapter = _FakeMihomoAdapter()
    monkeypatch.setattr(server_ping, "DEFAULT_MIHOMO_ADAPTER", adapter)

    measured = server_ping.check_server_delay(
        "sub:abc123",
        update_state=True,
        checked_by="pytest-user",
    )

    assert measured["ok"] is True
    assert measured["server_id"] == "sub:abc123"
    assert measured["mihomo_target"] == "Duplicate Display [abc123]"
    assert adapter.checked_targets == ["Duplicate Display [abc123]"]

    with db_session() as connection:
        row = connection.execute(
            "SELECT status, metadata_json FROM server_ping_state WHERE server_id = ?",
            ("sub:abc123",),
        ).fetchone()
    assert row is not None
    assert row["status"] == "success"
    assert json.loads(row["metadata_json"])["mihomo_target"] == "Duplicate Display [abc123]"


def test_server_ping_resolves_custom_server_by_stable_id(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _seed_server(
        "custom-https:proxy:test",
        server_name="Proxy не заходить",
        raw_json={
            "name": "Proxy не заходить",
            "type": "socks5",
            "server": "proxy.example",
            "port": 1080,
        },
    )
    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO server_custom_https_proxy (server_id, proxy_type, host, port)
            VALUES ('custom-https:proxy:test', 'socks5', 'proxy.example', 1080)
            """
        )
    adapter = _FakeMihomoAdapter()
    monkeypatch.setattr(server_ping, "DEFAULT_MIHOMO_ADAPTER", adapter)

    target = server_ping.resolve_server_runtime_target("custom-https:proxy:test")
    measured = server_ping.check_server_delay(
        "custom-https:proxy:test",
        update_state=True,
        checked_by="pytest-user",
    )

    assert target["ok"] is True
    assert target["runtime_target"] == "Proxy не заходить"
    assert measured["runtime_target"] == "Proxy не заходить"
    assert adapter.checked_targets == ["Proxy не заходить"]


def test_manual_ping_state_survives_background_failure(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _seed_server("server-a")
    monkeypatch.setattr(server_ping, "DEFAULT_MIHOMO_ADAPTER", _FakeMihomoAdapter(ok=True, delay_ms=42))

    manual = server_ping.check_server_delay(
        "server-a",
        update_state=True,
        checked_by="pytest-user",
        source="manual",
    )
    server_ping.record_ping_result(
        server_id="server-a",
        runtime_target="server-a",
        source="background",
        status="failed",
        latency_ms=None,
        checked_by="background_sweep",
        error_code="BACKGROUND_TIMEOUT",
        error_message="Background timeout.",
        metadata={"test": "background"},
    )
    state = server_ping.get_server_ping_state("server-a")

    assert manual["latency_ms"] == 42
    assert state["manual"]["status"] == "success"
    assert state["manual"]["latency_ms"] == 42
    assert state["manual"]["checked_by"] == "pytest-user"
    assert state["background"]["status"] == "failed"
    assert state["background"]["error_code"] == "BACKGROUND_TIMEOUT"
    assert state["status"] == "success"
    assert state["latency_ms"] == 42

    client = TestClient(create_app(enable_startup_tasks=False))
    [projected] = client.get("/api/v2/servers?inventory_state=active&limit=1000").json()["data"]["servers"]
    assert projected["ping"]["status"] == "success"
    assert projected["ping"]["last_ping_ms"] == 42
    assert projected["ping"]["source"] == "manual"
    assert projected["ping"]["manual"]["latency_ms"] == 42
    assert projected["ping"]["background"]["status"] == "failed"
    assert projected["ping"]["background"]["source"] == "background"


def test_manual_ping_failure_replaces_manual_observation(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _seed_server("server-a")
    monkeypatch.setattr(server_ping, "DEFAULT_MIHOMO_ADAPTER", _FakeMihomoAdapter(ok=True, delay_ms=42))
    server_ping.check_server_delay(
        "server-a",
        update_state=True,
        checked_by="pytest-user",
        source="manual",
    )
    monkeypatch.setattr(server_ping, "DEFAULT_MIHOMO_ADAPTER", _FakeMihomoAdapter(ok=False, delay_ms=None))

    failed = server_ping.check_server_delay(
        "server-a",
        update_state=True,
        checked_by="pytest-user",
        source="manual",
    )
    state = server_ping.get_server_ping_state("server-a")

    assert failed["ok"] is False
    assert state["manual"]["status"] == "failed"
    assert state["manual"]["latency_ms"] is None
    assert state["manual"]["error_code"] == "PING_FAILED"


def test_ping_sources_coexist_without_overwriting_manual(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _seed_server("server-a")

    server_ping.record_ping_result(
        server_id="server-a",
        runtime_target="server-a",
        source="manual",
        status="success",
        latency_ms=42,
        checked_by="ui",
        error_code=None,
        error_message=None,
        metadata={},
    )
    server_ping.record_ping_result(
        server_id="server-a",
        runtime_target="server-a",
        source="watchdog",
        status="failed",
        latency_ms=None,
        checked_by="watchdog_active_check:pytest",
        error_code="WATCHDOG_TIMEOUT",
        error_message="Watchdog timeout.",
        metadata={},
    )
    state = server_ping.get_server_ping_state("server-a")

    assert state["manual"]["status"] == "success"
    assert state["manual"]["latency_ms"] == 42
    assert state["runtime"]["source"] == "watchdog"
    assert state["runtime"]["status"] == "failed"


def test_arbitrary_checked_by_does_not_create_semantic_source(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _seed_server("server-a")
    monkeypatch.setattr(server_ping, "DEFAULT_MIHOMO_ADAPTER", _FakeMihomoAdapter(ok=True, delay_ms=42))

    measured = server_ping.check_server_delay(
        "server-a",
        update_state=True,
        checked_by="phase5a_manual",
    )
    state = server_ping.get_server_ping_state("server-a")

    assert measured["source"] == "manual"
    assert state["source"] == "manual"
    assert state["manual"]["checked_by"] == "phase5a_manual"
