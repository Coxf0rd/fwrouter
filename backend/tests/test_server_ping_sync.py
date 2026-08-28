from __future__ import annotations

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


def _seed_server(server_id: str) -> None:
    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO servers (
                server_id,
                server_name,
                provider_name,
                inventory_state
            )
            VALUES (?, ?, 'pytest', 'active')
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


class _FakeMihomoAdapter:
    def check_delay(self, server_id: str, *, test_url: str, timeout_ms: int) -> MihomoDelayResult:
        return MihomoDelayResult(
            ok=True,
            server_id=server_id,
            delay_ms=42,
            test_url=test_url,
            timeout_ms=timeout_ms,
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

    client = TestClient(create_app(enable_startup_tasks=False))
    payload = client.get("/api/v2/servers?inventory_state=active&limit=1000").json()

    assert payload["ok"] is True
    [server] = payload["data"]["servers"]
    assert server["server_id"] == "server-a"
    assert server["ping"]["status"] == "success"
    assert server["ping"]["last_ping_ms"] == 42
    assert server["ping"]["checked_by"] == "pytest-user"
