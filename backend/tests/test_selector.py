from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import db_session, initialize_database
from fwrouter_api.main import create_app
from fwrouter_api.services.selector import (
    get_vpn_auto_state,
    restore_mihomo_selector_state,
    select_vpn_auto_server,
)
from fwrouter_api.services.servers import (
    apply_global_auto_server,
    ensure_routing_global_state,
    expire_global_fixed_server,
    get_routing_global_state,
    replace_vpn_auto_servers,
    set_global_fixed_server,
    update_server_preferences,
)


def _configure_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FWROUTER_STATE_DIR", str(tmp_path / "state"))
    get_settings.cache_clear()


def _seed_server(
    server_id: str,
    *,
    vpn_auto: bool = True,
    global_list: bool = True,
    vpn_auto_priority: int = 0,
) -> None:
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
                vpn_auto_priority,
                global_list
            )
            VALUES (?, ?, ?, ?)
            """,
            (server_id, 1 if vpn_auto else 0, vpn_auto_priority, 1 if global_list else 0),
        )


def _seed_global_auto_state(active_auto_server_id: str | None = None) -> None:
    ensure_routing_global_state()
    with db_session() as connection:
        connection.execute(
            """
            UPDATE routing_global_state
            SET
                desired_mode = 'vpn',
                applied_mode = 'vpn',
                selective_default = 'direct',
                server_mode = 'auto',
                desired_fixed_server_id = NULL,
                applied_fixed_server_id = NULL,
                active_auto_server_id = ?,
                apply_state = 'clean',
                error_code = NULL,
                error_message = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = 1
            """,
            (active_auto_server_id,),
        )


def _client() -> TestClient:
    return TestClient(create_app(enable_startup_tasks=False))


def test_select_vpn_auto_server_persists_active_auto_server_id_after_apply(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _seed_server("srv-1")
    _seed_server("srv-2")
    _seed_global_auto_state("srv-1")

    monkeypatch.setattr(
        "fwrouter_api.services.selector.DEFAULT_MIHOMO_ADAPTER",
        SimpleNamespace(
            health=lambda: SimpleNamespace(active_server_id="srv-1"),
            list_servers=lambda: [
                SimpleNamespace(server_id="srv-1"),
                SimpleNamespace(server_id="srv-2"),
            ],
            apply_server=lambda server_id: SimpleNamespace(
                ok=True,
                active_server_id=server_id,
                to_dict=lambda: {
                    "ok": True,
                    "active_server_id": server_id,
                },
            ),
        ),
    )

    def _fake_check_server_delay(server_id: str, **kwargs):
        return {
            "ok": True,
            "server_id": server_id,
            "status": "success",
            "last_ping_ms": 25 if server_id == "srv-2" else 50,
            "latency_label": "ok",
            "checked_by": kwargs.get("checked_by"),
            "test_url": "https://example.test/generate_204",
            "timeout_ms": kwargs.get("timeout_ms"),
            "error_code": None,
            "error_message": None,
            "updated_state": kwargs.get("update_state", False),
        }

    monkeypatch.setattr(
        "fwrouter_api.services.selector.check_server_delay",
        _fake_check_server_delay,
    )

    result = select_vpn_auto_server(
        apply=True,
        reason="pytest",
        exclude_active=True,
        post_check=True,
    )

    routing = get_routing_global_state()

    assert result["ok"] is True
    assert result["selected_server_id"] == "srv-2"
    assert result["active_after"] == "srv-2"
    assert routing is not None
    assert routing["active_auto_server_id"] == "srv-2"


def test_restore_mihomo_selector_state_restores_vpn_auto_then_vpn_global(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _seed_server("srv-1")
    _seed_server("srv-2")
    _seed_global_auto_state("srv-2")

    calls: list[tuple[str, str]] = []

    monkeypatch.setattr(
        "fwrouter_api.services.selector.DEFAULT_MIHOMO_ADAPTER",
        SimpleNamespace(
            health=lambda: SimpleNamespace(runtime_state="running"),
            list_servers=lambda: [
                SimpleNamespace(server_id="srv-1"),
                SimpleNamespace(server_id="srv-2"),
            ],
            apply_server_to_selector=lambda selector_name, server_id: (
                calls.append((selector_name, server_id)) or SimpleNamespace(
                    ok=True,
                    active_server_id=server_id,
                    to_dict=lambda: {
                        "ok": True,
                        "selector_name": selector_name,
                        "active_server_id": server_id,
                    },
                )
            ),
        ),
    )

    result = restore_mihomo_selector_state(requested_by="pytest")

    assert result["ok"] is True
    assert calls == [("vpn-auto", "srv-2"), ("vpn-global", "vpn-auto")]


def test_restore_mihomo_selector_state_uses_fixed_server_without_vpn_auto_restore(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _seed_server("srv-fixed")
    _seed_server("srv-auto")
    ensure_routing_global_state()
    with db_session() as connection:
        connection.execute(
            """
            UPDATE routing_global_state
            SET
                desired_mode = 'vpn',
                applied_mode = 'vpn',
                server_mode = 'fixed',
                desired_fixed_server_id = 'srv-fixed',
                applied_fixed_server_id = 'srv-fixed',
                active_auto_server_id = 'srv-auto',
                apply_state = 'clean',
                error_code = NULL,
                error_message = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = 1
            """
        )

    calls: list[tuple[str, str]] = []

    monkeypatch.setattr(
        "fwrouter_api.services.selector.DEFAULT_MIHOMO_ADAPTER",
        SimpleNamespace(
            health=lambda: SimpleNamespace(runtime_state="running"),
            list_servers=lambda: [SimpleNamespace(server_id="srv-fixed")],
            apply_server_to_selector=lambda selector_name, server_id: (
                calls.append((selector_name, server_id)) or SimpleNamespace(
                    ok=True,
                    active_server_id=server_id,
                    to_dict=lambda: {
                        "ok": True,
                        "selector_name": selector_name,
                        "active_server_id": server_id,
                    },
                )
            ),
        ),
    )

    result = restore_mihomo_selector_state(requested_by="pytest-fixed")

    assert result["ok"] is True
    assert calls == [("vpn-global", "srv-fixed")]
    assert result["vpn_auto_restore"]["skipped"] is True


def test_select_vpn_auto_server_prefers_priority_when_ping_within_ratio(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _seed_server("srv-best", vpn_auto_priority=0)
    _seed_server("srv-priority", vpn_auto_priority=3)
    _seed_global_auto_state("srv-best")

    monkeypatch.setattr(
        "fwrouter_api.services.selector.DEFAULT_MIHOMO_ADAPTER",
        SimpleNamespace(
            health=lambda: SimpleNamespace(active_server_id="srv-best"),
            list_servers=lambda: [
                SimpleNamespace(server_id="srv-best"),
                SimpleNamespace(server_id="srv-priority"),
            ],
            apply_server=lambda server_id: SimpleNamespace(
                ok=True,
                active_server_id=server_id,
                to_dict=lambda: {
                    "ok": True,
                    "active_server_id": server_id,
                },
            ),
        ),
    )

    def _fake_check_server_delay(server_id: str, **kwargs):
        return {
            "ok": True,
            "server_id": server_id,
            "status": "success",
            "last_ping_ms": 30 if server_id == "srv-best" else 80,
            "latency_label": "ok",
            "checked_by": kwargs.get("checked_by"),
            "test_url": "https://example.test/generate_204",
            "timeout_ms": kwargs.get("timeout_ms"),
            "error_code": None,
            "error_message": None,
            "updated_state": kwargs.get("update_state", False),
        }

    monkeypatch.setattr(
        "fwrouter_api.services.selector.check_server_delay",
        _fake_check_server_delay,
    )

    result = select_vpn_auto_server(
        apply=False,
        reason="pytest-priority",
        check_on_demand=True,
    )

    assert result["ok"] is True
    assert result["selected_server_id"] == "srv-priority"
    assert result["selected_vpn_auto_priority"] == 3
    assert result["priority_override"] is not None
    assert result["priority_override"]["best_latency_server_id"] == "srv-best"


def test_select_vpn_auto_server_priority_one_allows_one_point_five_x(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _seed_server("srv-best", vpn_auto_priority=0)
    _seed_server("srv-priority-1", vpn_auto_priority=1)
    _seed_global_auto_state("srv-best")

    monkeypatch.setattr(
        "fwrouter_api.services.selector.DEFAULT_MIHOMO_ADAPTER",
        SimpleNamespace(
            health=lambda: SimpleNamespace(active_server_id="srv-best"),
            list_servers=lambda: [
                SimpleNamespace(server_id="srv-best"),
                SimpleNamespace(server_id="srv-priority-1"),
            ],
            apply_server=lambda server_id: SimpleNamespace(
                ok=True,
                active_server_id=server_id,
                to_dict=lambda: {
                    "ok": True,
                    "active_server_id": server_id,
                },
            ),
        ),
    )

    def _fake_check_server_delay(server_id: str, **kwargs):
        return {
            "ok": True,
            "server_id": server_id,
            "status": "success",
            "last_ping_ms": 100 if server_id == "srv-best" else 150,
            "latency_label": "ok",
            "checked_by": kwargs.get("checked_by"),
            "test_url": "https://example.test/generate_204",
            "timeout_ms": kwargs.get("timeout_ms"),
            "error_code": None,
            "error_message": None,
            "updated_state": kwargs.get("update_state", False),
        }

    monkeypatch.setattr(
        "fwrouter_api.services.selector.check_server_delay",
        _fake_check_server_delay,
    )

    result = select_vpn_auto_server(
        apply=False,
        reason="pytest-priority-one",
        check_on_demand=True,
    )

    assert result["ok"] is True
    assert result["selected_server_id"] == "srv-priority-1"
    assert result["selected_vpn_auto_priority"] == 1
    assert result["priority_override"] is not None
    assert result["priority_override"]["best_latency_server_id"] == "srv-best"


def test_select_vpn_auto_server_skips_negative_priority_candidates(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _seed_server("srv-manual-only", vpn_auto_priority=-1)
    _seed_server("srv-auto", vpn_auto_priority=0)
    _seed_global_auto_state("srv-auto")

    monkeypatch.setattr(
        "fwrouter_api.services.selector.DEFAULT_MIHOMO_ADAPTER",
        SimpleNamespace(
            health=lambda: SimpleNamespace(active_server_id="srv-auto"),
            list_servers=lambda: [
                SimpleNamespace(server_id="srv-manual-only"),
                SimpleNamespace(server_id="srv-auto"),
            ],
            apply_server=lambda server_id: SimpleNamespace(
                ok=True,
                active_server_id=server_id,
                to_dict=lambda: {
                    "ok": True,
                    "active_server_id": server_id,
                },
            ),
        ),
    )

    def _fake_check_server_delay(server_id: str, **kwargs):
        return {
            "ok": True,
            "server_id": server_id,
            "status": "success",
            "last_ping_ms": 20 if server_id == "srv-manual-only" else 40,
            "latency_label": "ok",
            "checked_by": kwargs.get("checked_by"),
            "test_url": "https://example.test/generate_204",
            "timeout_ms": kwargs.get("timeout_ms"),
            "error_code": None,
            "error_message": None,
            "updated_state": kwargs.get("update_state", False),
        }

    monkeypatch.setattr(
        "fwrouter_api.services.selector.check_server_delay",
        _fake_check_server_delay,
    )

    result = select_vpn_auto_server(
        apply=False,
        reason="pytest-negative-priority",
        check_on_demand=True,
    )

    assert result["ok"] is True
    assert result["selected_server_id"] == "srv-auto"
    assert result["selected_vpn_auto_priority"] == 0
    assert result["auto_selectable_candidates_count"] == 1
    assert any(
        item["server_id"] == "srv-manual-only"
        for item in result["on_demand"]["results"]
    )


def test_select_vpn_auto_server_on_demand_shortlist_includes_high_priority_candidate_beyond_latency_slice(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    for index in range(12):
        priority = 5 if index == 11 else 0
        _seed_server(f"srv-{index:02d}", vpn_auto_priority=priority)

    _seed_global_auto_state("srv-00")

    monkeypatch.setattr(
        "fwrouter_api.services.selector.DEFAULT_MIHOMO_ADAPTER",
        SimpleNamespace(
            health=lambda: SimpleNamespace(active_server_id="srv-00"),
            list_servers=lambda: [
                SimpleNamespace(server_id=f"srv-{index:02d}")
                for index in range(12)
            ],
            apply_server=lambda server_id: SimpleNamespace(
                ok=True,
                active_server_id=server_id,
                to_dict=lambda: {
                    "ok": True,
                    "active_server_id": server_id,
                },
            ),
        ),
    )

    def _fake_check_server_delay(server_id: str, **kwargs):
        return {
            "ok": True,
            "server_id": server_id,
            "status": "success",
            "last_ping_ms": 35 if server_id == "srv-11" else 60,
            "latency_label": "ok",
            "checked_by": kwargs.get("checked_by"),
            "test_url": "https://example.test/generate_204",
            "timeout_ms": kwargs.get("timeout_ms"),
            "error_code": None,
            "error_message": None,
            "updated_state": kwargs.get("update_state", False),
        }

    monkeypatch.setattr(
        "fwrouter_api.services.selector.check_server_delay",
        _fake_check_server_delay,
    )

    result = select_vpn_auto_server(
        apply=False,
        reason="pytest-shortlist",
        check_on_demand=True,
        on_demand_limit=10,
    )

    assert result["ok"] is True
    assert result["selected_server_id"] == "srv-11"
    assert "srv-11" in result["on_demand"]["candidate_shortlist"]


def test_apply_global_auto_server_persists_active_auto_server_id(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _seed_server("srv-auto")
    _seed_server("srv-fixed")
    _seed_global_auto_state("srv-auto")

    with db_session() as connection:
        connection.execute(
            """
            UPDATE routing_global_state
            SET
                server_mode = 'fixed',
                desired_fixed_server_id = 'srv-fixed',
                applied_fixed_server_id = 'srv-fixed',
                active_auto_server_id = 'srv-auto',
                updated_at = CURRENT_TIMESTAMP
            WHERE id = 1
            """
        )

    monkeypatch.setattr(
        "fwrouter_api.adapters.mihomo.DEFAULT_MIHOMO_ADAPTER",
        SimpleNamespace(
            get_active_server_id=lambda: "srv-auto",
            apply_server_to_selector=lambda selector_name, server_id: SimpleNamespace(
                ok=True,
                active_server_id="srv-auto",
                to_dict=lambda: {
                    "ok": True,
                    "selector_name": selector_name,
                    "requested_server_id": server_id,
                    "active_server_id": "srv-auto",
                },
            ),
        ),
    )

    result = apply_global_auto_server(requested_by="pytest")
    routing = get_routing_global_state()

    assert result["ok"] is True
    assert routing is not None
    assert routing["server_mode"] == "auto"
    assert routing["active_auto_server_id"] == "srv-auto"


def test_global_fixed_server_expires_after_backend_ttl(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _seed_server("srv-fixed")

    selected = set_global_fixed_server("srv-fixed", requested_by="pytest")
    assert selected["ok"] is True
    assert selected["routing"]["server_mode"] == "fixed"
    assert selected["routing"]["desired_fixed_server_id"] == "srv-fixed"
    assert selected["routing"]["fixed_server_until"] is not None

    with db_session() as connection:
        connection.execute(
            """
            UPDATE routing_global_state
            SET
                applied_fixed_server_id = desired_fixed_server_id,
                apply_state = 'clean',
                fixed_server_until = datetime('now', '-1 minute')
            WHERE id = 1
            """
        )

    expired = expire_global_fixed_server(dry_run=False)
    routing = get_routing_global_state()

    assert expired["expired_global_fixed_server_count"] == 1
    assert routing is not None
    assert routing["server_mode"] == "auto"
    assert routing["desired_fixed_server_id"] is None
    assert routing["applied_fixed_server_id"] is None
    assert routing["fixed_server_until"] is None
    assert routing["apply_state"] == "pending"


def test_get_vpn_auto_state_reports_no_candidates(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    ensure_routing_global_state()
    monkeypatch.setattr(
        "fwrouter_api.services.selector.DEFAULT_MIHOMO_ADAPTER",
        SimpleNamespace(
            health=lambda: SimpleNamespace(runtime_state="running", active_server_id=None, details={"selectors": {}})
        ),
    )

    state = get_vpn_auto_state()

    assert state["enabled_candidates_count"] == 0
    assert state["problem_code"] == "vpn_auto_no_candidates"


def test_get_vpn_auto_state_reports_candidates_missing_from_mihomo_group(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _seed_server("srv-hidden", vpn_auto=True, global_list=False)
    _seed_global_auto_state(None)
    monkeypatch.setattr(
        "fwrouter_api.services.selector.DEFAULT_MIHOMO_ADAPTER",
        SimpleNamespace(
            health=lambda: SimpleNamespace(
                runtime_state="running",
                active_server_id=None,
                details={"selectors": {"vpn_auto_targets": ["DIRECT"], "vpn_global_targets": ["vpn-auto", "DIRECT"]}},
            )
        ),
    )

    state = get_vpn_auto_state()

    assert state["enabled_candidates_count"] == 1
    assert state["config_consistent"] is False
    assert state["problem_code"] == "vpn_auto_candidates_not_in_mihomo_config"


def test_get_vpn_auto_state_reports_invalid_active_auto_server(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _seed_server("srv-1")
    _seed_server("srv-missing", vpn_auto=False)
    _seed_global_auto_state("srv-missing")
    monkeypatch.setattr(
        "fwrouter_api.services.selector.DEFAULT_MIHOMO_ADAPTER",
        SimpleNamespace(
            health=lambda: SimpleNamespace(
                runtime_state="running",
                active_server_id="srv-missing",
                details={"selectors": {"vpn_auto_targets": ["srv-1", "DIRECT"], "vpn_global_targets": ["vpn-auto", "DIRECT"]}},
            )
        ),
    )

    state = get_vpn_auto_state()

    assert state["active_auto_server_id"] == "srv-missing"
    assert state["active_auto_server_valid"] is False
    assert state["problem_code"] == "active_auto_server_invalid"


def test_get_vpn_auto_state_reports_stale_traffic_signal(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _seed_server("srv-1")
    _seed_global_auto_state("srv-1")
    monkeypatch.setattr(
        "fwrouter_api.services.selector.DEFAULT_MIHOMO_ADAPTER",
        SimpleNamespace(
            health=lambda: SimpleNamespace(
                runtime_state="running",
                active_server_id="srv-1",
                details={"selectors": {"vpn_auto_targets": ["srv-1", "DIRECT"], "vpn_global_targets": ["vpn-auto", "DIRECT"]}},
            )
        ),
    )
    monkeypatch.setattr(
        "fwrouter_api.services.traffic.get_traffic_accounting_state",
        lambda: {
            "last_collected_at": None,
            "safe_for_watchdog_auto": False,
            "signal_authoritative": False,
            "signal_fresh": False,
        },
    )
    monkeypatch.setattr("fwrouter_api.services.selector._traffic_collector_installed", lambda: False)

    state = get_vpn_auto_state()

    assert state["traffic_signal_fresh"] is False
    assert state["problem_code"] == "traffic_collector_timer_missing"


def test_get_vpn_auto_state_uses_server_name_for_mihomo_target_consistency(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO servers (
                server_id,
                server_name,
                provider_name,
                inventory_state
            )
            VALUES ('custom-https:proxy6:aaaa1111', 'Proxy6', 'pytest', 'active')
            """
        )
        connection.execute(
            """
            INSERT INTO server_preferences (
                server_id,
                vpn_auto,
                global_list
            )
            VALUES ('custom-https:proxy6:aaaa1111', 1, 0)
            """
        )
    _seed_global_auto_state("custom-https:proxy6:aaaa1111")
    monkeypatch.setattr(
        "fwrouter_api.services.selector.DEFAULT_MIHOMO_ADAPTER",
        SimpleNamespace(
            health=lambda: SimpleNamespace(
                runtime_state="running",
                active_server_id="custom-https:proxy6:aaaa1111",
                details={"selectors": {"vpn_auto_targets": ["Proxy6", "DIRECT"], "vpn_global_targets": ["vpn-auto", "DIRECT"]}},
            )
        ),
    )

    state = get_vpn_auto_state()

    assert state["config_consistent"] is True
    assert state["active_auto_server_valid"] is True


def test_get_vpn_auto_state_ignores_negative_priority_candidate_for_mihomo_consistency(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO servers (
                server_id,
                server_name,
                provider_name,
                inventory_state
            )
            VALUES ('custom-https:proxy6:aaaa1111', 'Proxy6', 'pytest', 'active')
            """
        )
        connection.execute(
            """
            INSERT INTO server_preferences (
                server_id,
                vpn_auto,
                vpn_auto_priority,
                global_list
            )
            VALUES ('custom-https:proxy6:aaaa1111', 1, -1, 1)
            """
        )
    _seed_global_auto_state(None)
    monkeypatch.setattr(
        "fwrouter_api.services.selector.DEFAULT_MIHOMO_ADAPTER",
        SimpleNamespace(
            health=lambda: SimpleNamespace(
                runtime_state="running",
                active_server_id=None,
                details={"selectors": {"vpn_auto_targets": ["DIRECT"], "vpn_global_targets": ["vpn-auto", "DIRECT"]}},
            )
        ),
    )

    state = get_vpn_auto_state()

    assert state["enabled_candidates_count"] == 1
    assert state["auto_selectable_candidates_count"] == 0
    assert state["config_consistent"] is True
    assert state["problem_code"] == "vpn_auto_no_auto_selectable_candidates"


def test_vpn_auto_state_endpoint_returns_diagnostics(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    monkeypatch.setattr(
        "fwrouter_api.routes.selector.get_vpn_auto_state",
        lambda: {"enabled_candidates_count": 0, "problem_code": "vpn_auto_no_candidates"},
    )

    with _client() as client:
        response = client.get("/api/v2/selector/vpn-auto/state")

    assert response.status_code == 200
    assert response.json()["data"]["vpn_auto"]["problem_code"] == "vpn_auto_no_candidates"


def test_remove_active_vpn_auto_server_triggers_reselect(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _seed_server("srv-1")
    _seed_server("srv-2")
    _seed_global_auto_state("srv-1")
    monkeypatch.setattr(
        "fwrouter_api.services.servers._reconcile_mihomo_after_server_preferences",
        lambda **kwargs: {"ok": True},
    )

    calls = {"count": 0}

    def _fake_state():
        calls["count"] += 1
        if calls["count"] == 1:
            return {
                "server_mode": "auto",
                "enabled_candidates_count": 1,
                "auto_selectable_candidates_count": 1,
                "active_auto_server_id": "srv-1",
                "active_auto_server_valid": False,
            }
        return {
            "server_mode": "auto",
            "enabled_candidates_count": 1,
            "auto_selectable_candidates_count": 1,
            "active_auto_server_id": "srv-2",
            "active_auto_server_valid": True,
        }

    selector_calls: list[dict[str, object]] = []
    monkeypatch.setattr("fwrouter_api.services.selector.get_vpn_auto_state", _fake_state)
    monkeypatch.setattr(
        "fwrouter_api.services.selector.select_vpn_auto_server",
        lambda **kwargs: selector_calls.append(kwargs) or {"ok": True, "selected_server_id": "srv-2", "active_after": "srv-2"},
    )

    result = update_server_preferences("srv-1", vpn_auto=False, reconcile_mihomo=True)

    assert result["ok"] is True
    assert result["auto_select"]["triggered"] is True
    assert selector_calls


def test_replace_vpn_auto_servers_triggers_reselect_when_active_removed(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _seed_server("srv-1")
    _seed_server("srv-2")
    _seed_global_auto_state("srv-1")
    monkeypatch.setattr(
        "fwrouter_api.services.servers._reconcile_mihomo_after_server_preferences",
        lambda **kwargs: {"ok": True},
    )

    calls = {"count": 0}

    def _fake_state():
        calls["count"] += 1
        if calls["count"] == 1:
            return {
                "server_mode": "auto",
                "enabled_candidates_count": 1,
                "auto_selectable_candidates_count": 1,
                "active_auto_server_id": "srv-1",
                "active_auto_server_valid": False,
            }
        return {
            "server_mode": "auto",
            "enabled_candidates_count": 1,
            "auto_selectable_candidates_count": 1,
            "active_auto_server_id": "srv-2",
            "active_auto_server_valid": True,
        }

    selector_calls: list[dict[str, object]] = []
    monkeypatch.setattr("fwrouter_api.services.selector.get_vpn_auto_state", _fake_state)
    monkeypatch.setattr(
        "fwrouter_api.services.selector.select_vpn_auto_server",
        lambda **kwargs: selector_calls.append(kwargs) or {"ok": True, "selected_server_id": "srv-2", "active_after": "srv-2"},
    )

    result = replace_vpn_auto_servers(["srv-2"], reconcile_mihomo=True)

    assert result["ok"] is True
    assert result["auto_select"]["triggered"] is True
    assert selector_calls


def test_remove_non_active_vpn_auto_server_does_not_unnecessarily_reselect(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _seed_server("srv-1")
    _seed_server("srv-2")
    _seed_global_auto_state("srv-1")
    monkeypatch.setattr(
        "fwrouter_api.services.servers._reconcile_mihomo_after_server_preferences",
        lambda **kwargs: {"ok": True},
    )
    monkeypatch.setattr(
        "fwrouter_api.services.selector.get_vpn_auto_state",
        lambda: {
            "server_mode": "auto",
            "enabled_candidates_count": 1,
            "active_auto_server_id": "srv-1",
            "active_auto_server_valid": True,
        },
    )

    called = {"value": False}
    monkeypatch.setattr(
        "fwrouter_api.services.selector.select_vpn_auto_server",
        lambda **kwargs: called.__setitem__("value", True) or {"ok": True},
    )

    result = update_server_preferences("srv-2", vpn_auto=False, reconcile_mihomo=True)

    assert result["ok"] is True
    assert result["auto_select"]["triggered"] is False
    assert called["value"] is False
