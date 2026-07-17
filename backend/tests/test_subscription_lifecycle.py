from __future__ import annotations
from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import initialize_database


import json
from pathlib import Path
from subprocess import CompletedProcess

from fastapi.testclient import TestClient

import fwrouter_api.adapters.subscription as subscription_adapter_module
from fwrouter_api.adapters.subscription import (
    SubscriptionRefreshResult,
    SubscriptionRefreshStatus,
    SubscriptionServer,
)
from fwrouter_api.main import create_app
from fwrouter_api.services import subscription as subscription_service
from fwrouter_api.services import subscription_pipeline as pipeline_service
from fwrouter_api.services.subscription import (
    get_subscription_state,
    refresh_subscription_inventory,
    save_subscription_url,
    validate_subscription_url,
)
from fwrouter_api.services.subjects import list_subjects


def _configure_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FWROUTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("FWROUTER_MAINTENANCE_SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("FWROUTER_WATCHDOG_SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("FWROUTER_RUNTIME_CONVERGENCE_SCHEDULER_ENABLED", "false")
    get_settings.cache_clear()


class _FakeSubscriptionAdapter:
    def __init__(self, result: SubscriptionRefreshResult) -> None:
        self.result = result
        self.calls: list[str] = []

    def refresh(self, url: str) -> SubscriptionRefreshResult:
        self.calls.append(url)
        return self.result


def _success_refresh_result(*names: str) -> SubscriptionRefreshResult:
    servers = [
        SubscriptionServer(
            server_id=name,
            server_name=name,
            provider_name="subscription",
            raw={"name": name, "type": "vless"},
        )
        for name in names
    ]
    return SubscriptionRefreshResult(
        status=SubscriptionRefreshStatus.SUCCESS,
        servers=servers,
        message="refresh ok",
        metadata={"url": "https://example.test/sub", "servers_count": len(servers)},
    )


def _failed_refresh_result() -> SubscriptionRefreshResult:
    return SubscriptionRefreshResult(
        status=SubscriptionRefreshStatus.FAILED,
        message="download failed",
        error_code="SUBSCRIPTION_DOWNLOAD_FAILED",
        error_message="download failed",
        metadata={"url": "https://example.test/sub"},
    )


def _client() -> TestClient:
    return TestClient(create_app(enable_startup_tasks=False))


def test_validate_subscription_url_rejects_empty(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    validation = validate_subscription_url("")
    assert validation["valid"] is False
    assert validation["error"]["code"] == "SUBSCRIPTION_URL_EMPTY"


def test_validate_subscription_url_rejects_invalid_scheme(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    validation = validate_subscription_url("ftp://example.test/sub")
    assert validation["valid"] is False
    assert validation["error"]["code"] == "SUBSCRIPTION_URL_INVALID_SCHEME"


def test_validate_subscription_url_accepts_https(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    validation = validate_subscription_url("https://example.test/sub")
    assert validation["valid"] is True
    assert validation["normalized_url"] == "https://example.test/sub"


def test_validate_subscription_url_rejects_placeholder_host(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    validation = validate_subscription_url("https://subscription.example/profile")
    assert validation["valid"] is False
    assert validation["error"]["code"] == "SUBSCRIPTION_URL_PLACEHOLDER_HOST"


def test_save_subscription_url_sets_idle_state(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    result = save_subscription_url("https://example.test/sub", metadata={"name": "test"})
    state = get_subscription_state()

    assert result["saved"] is True
    assert state["status"] == "idle"
    assert state["url"] == "https://example.test/sub"
    assert state["metadata"]["name"] == "test"


def test_save_subscription_url_invalid_keeps_not_configured(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    result = save_subscription_url("bad-url")
    state = get_subscription_state()

    assert result["saved"] is False
    assert state["status"] == "not_configured"


def test_refresh_subscription_inventory_rejects_invalid_saved_url(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    result = refresh_subscription_inventory("ftp://bad")
    state = get_subscription_state()

    assert result["ok"] is False
    assert result["stage"] == "validate"
    assert state["status"] == "failed"
    assert state["error_code"] == "SUBSCRIPTION_URL_INVALID_SCHEME"


def test_refresh_subscription_inventory_reports_saved_placeholder_url(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    with subscription_service.db_session() as connection:
        connection.execute(
            """
            INSERT INTO subscription_state (id, url, status)
            VALUES (1, 'https://subscription.example/profile', 'idle')
            """
        )

    result = refresh_subscription_inventory()
    assert result["ok"] is False
    assert result["diagnostics"]["saved_url_invalid"] is True
    assert result["error"]["code"] == "SUBSCRIPTION_URL_PLACEHOLDER_HOST"


def test_refresh_subscription_inventory_records_adapter_failure(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    save_subscription_url("https://example.test/sub")
    adapter = _FakeSubscriptionAdapter(_failed_refresh_result())
    monkeypatch.setattr(subscription_adapter_module, "DEFAULT_SUBSCRIPTION_ADAPTER", adapter)

    result = refresh_subscription_inventory()
    state = get_subscription_state()

    assert result["ok"] is False
    assert result["stage"] == "download_parse"
    assert adapter.calls == ["https://example.test/sub"]
    assert state["status"] == "failed"
    assert state["error_code"] == "SUBSCRIPTION_DOWNLOAD_FAILED"


def test_refresh_subscription_inventory_syncs_servers(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    save_subscription_url("https://example.test/sub")
    adapter = _FakeSubscriptionAdapter(_success_refresh_result("alpha", "beta"))
    monkeypatch.setattr(subscription_adapter_module, "DEFAULT_SUBSCRIPTION_ADAPTER", adapter)

    result = refresh_subscription_inventory()
    state = get_subscription_state()

    assert result["ok"] is True
    assert result["inventory"]["active_count"] == 2
    assert state["status"] == "success"
    assert state["last_success_at"] is not None


def test_refresh_subscription_inventory_marks_missing_servers(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    save_subscription_url("https://example.test/sub")
    monkeypatch.setattr(subscription_adapter_module, "DEFAULT_SUBSCRIPTION_ADAPTER", _FakeSubscriptionAdapter(_success_refresh_result("alpha", "beta")))
    refresh_subscription_inventory()

    monkeypatch.setattr(subscription_adapter_module, "DEFAULT_SUBSCRIPTION_ADAPTER", _FakeSubscriptionAdapter(_success_refresh_result("beta")))
    result = refresh_subscription_inventory()

    assert result["inventory"]["active_count"] == 1
    assert result["inventory"]["missing_count"] == 1


def test_prepare_subscription_refresh_stops_on_refresh_failure(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    save_subscription_url("https://example.test/sub")
    monkeypatch.setattr(subscription_adapter_module, "DEFAULT_SUBSCRIPTION_ADAPTER", _FakeSubscriptionAdapter(_failed_refresh_result()))

    result = pipeline_service.prepare_subscription_refresh()

    assert result["ok"] is False
    assert result["stage"] == "download_parse"
    assert result["candidate"] is None
    assert result["promoted"] is False
    assert result["container_restarted"] is False


def test_prepare_subscription_refresh_stops_on_config_validation_failure(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    save_subscription_url("https://example.test/sub")
    monkeypatch.setattr(subscription_adapter_module, "DEFAULT_SUBSCRIPTION_ADAPTER", _FakeSubscriptionAdapter(_success_refresh_result("alpha")))
    monkeypatch.setattr(
        pipeline_service,
        "write_mihomo_candidate_config",
        lambda: {"candidate_path": str(tmp_path / "candidate.yaml")},
    )
    monkeypatch.setattr(
        pipeline_service,
        "validate_mihomo_candidate_config",
        lambda: {"ok": False, "returncode": 1, "stdout_tail": "", "stderr_tail": "bad"},
    )

    result = pipeline_service.prepare_subscription_refresh()

    assert result["ok"] is False
    assert result["stage"] == "config_validation"
    assert result["promoted"] is False
    assert result["container_restarted"] is False


def test_prepare_subscription_refresh_success_keeps_candidate_only(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    save_subscription_url("https://example.test/sub")
    monkeypatch.setattr(subscription_adapter_module, "DEFAULT_SUBSCRIPTION_ADAPTER", _FakeSubscriptionAdapter(_success_refresh_result("alpha")))
    monkeypatch.setattr(
        pipeline_service,
        "write_mihomo_candidate_config",
        lambda: {"candidate_path": str(tmp_path / "candidate.yaml"), "active_path": str(tmp_path / "active.yaml")},
    )
    monkeypatch.setattr(
        pipeline_service,
        "validate_mihomo_candidate_config",
        lambda: {"ok": True, "returncode": 0, "stdout_tail": "ok", "stderr_tail": ""},
    )

    result = pipeline_service.prepare_subscription_refresh()

    assert result["ok"] is True
    assert result["stage"] == "candidate_validated"
    assert result["promoted"] is False
    assert result["container_restarted"] is False


def test_apply_subscription_refresh_skips_runtime_when_config_is_unchanged(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    monkeypatch.setattr(
        pipeline_service,
        "prepare_subscription_refresh",
        lambda: {
            "ok": True,
            "stage": "candidate_validated",
            "refresh": {"ok": True},
            "candidate": {"candidate_path": str(tmp_path / "candidate.yaml")},
            "config_validation": {"ok": True},
            "promoted": False,
            "container_restarted": False,
            "error": None,
        },
    )
    monkeypatch.setattr(
        pipeline_service,
        "reconcile_mihomo_runtime",
        lambda: {
            "ok": True,
            "reconcile_action": "none",
            "reconcile_reason": "unchanged_config",
            "promoted": {"ok": True, "promoted": False, "reason": "unchanged_config"},
            "container": {"ok": True, "action": "none", "reason": "unchanged_config"},
        },
    )

    result = pipeline_service.apply_subscription_refresh()

    assert result["ok"] is True
    assert result["stage"] == "already_current"
    assert result["applied"] is False
    assert result["promoted"] is False
    assert result["container_restarted"] is False


def test_apply_subscription_refresh_promotes_and_restarts_when_config_changed(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    monkeypatch.setattr(
        pipeline_service,
        "prepare_subscription_refresh",
        lambda: {
            "ok": True,
            "stage": "candidate_validated",
            "refresh": {"ok": True},
            "candidate": {"candidate_path": str(tmp_path / "candidate.yaml")},
            "config_validation": {"ok": True},
            "promoted": False,
            "container_restarted": False,
            "error": None,
        },
    )
    monkeypatch.setattr(
        pipeline_service,
        "reconcile_mihomo_runtime",
        lambda: {
            "ok": True,
            "reconcile_action": "restart",
            "reconcile_reason": "config_reload_required",
            "promoted": {"ok": True, "promoted": True},
            "container": {"ok": True, "action": "restart"},
        },
    )

    result = pipeline_service.apply_subscription_refresh()

    assert result["ok"] is True
    assert result["stage"] == "applied"
    assert result["applied"] is True
    assert result["promoted"] is True
    assert result["container_restarted"] is True


def test_apply_subscription_refresh_reports_runtime_reconcile_failure(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    monkeypatch.setattr(
        pipeline_service,
        "prepare_subscription_refresh",
        lambda: {
            "ok": True,
            "stage": "candidate_validated",
            "refresh": {"ok": True},
            "candidate": {"candidate_path": str(tmp_path / "candidate.yaml")},
            "config_validation": {"ok": True},
            "promoted": False,
            "container_restarted": False,
            "error": None,
        },
    )
    monkeypatch.setattr(
        pipeline_service,
        "reconcile_mihomo_runtime",
        lambda: {
            "ok": False,
            "reconcile_action": "restart",
            "reconcile_reason": "config_reload_required",
            "promoted": {"ok": True, "promoted": True},
            "container": {"ok": False, "action": "restart", "error_code": "MIHOMO_RESTART_FAILED", "error_message": "restart failed"},
        },
    )

    result = pipeline_service.apply_subscription_refresh()

    assert result["ok"] is False
    assert result["stage"] == "apply_runtime"
    assert result["error"]["code"] == "MIHOMO_RESTART_FAILED"


def test_subscription_refresh_selects_active_auto_when_empty(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    monkeypatch.setattr(
        pipeline_service,
        "prepare_subscription_refresh",
        lambda: {
            "ok": True,
            "stage": "candidate_validated",
            "refresh": {"ok": True},
            "candidate": {"candidate_path": str(tmp_path / "candidate.yaml")},
            "config_validation": {"ok": True},
            "promoted": False,
            "container_restarted": False,
            "error": None,
        },
    )
    monkeypatch.setattr(
        pipeline_service,
        "reconcile_mihomo_runtime",
        lambda: {
            "ok": True,
            "reconcile_action": "none",
            "reconcile_reason": "unchanged_config",
            "promoted": {"ok": True, "promoted": False},
            "container": {"ok": True, "action": "none"},
        },
    )
    monkeypatch.setattr(
        pipeline_service,
        "get_routing_global_state",
        lambda: {"server_mode": "auto"},
    )
    state_calls = {"count": 0}

    def _fake_state():
        state_calls["count"] += 1
        if state_calls["count"] == 1:
            return {
                "server_mode": "auto",
                "enabled_candidates_count": 2,
                "auto_selectable_candidates_count": 2,
                "active_auto_server_id": None,
                "active_auto_server_valid": False,
            }
        return {
            "server_mode": "auto",
            "enabled_candidates_count": 2,
            "auto_selectable_candidates_count": 2,
            "active_auto_server_id": "alpha",
            "active_auto_server_valid": True,
        }

    selector_calls: list[dict[str, object]] = []
    monkeypatch.setattr(pipeline_service, "get_vpn_auto_state", _fake_state)
    monkeypatch.setattr(
        pipeline_service,
        "select_vpn_auto_server",
        lambda **kwargs: selector_calls.append(kwargs) or {"ok": True, "selected_server_id": "alpha", "active_after": "alpha"},
    )

    result = pipeline_service.apply_subscription_refresh()

    assert result["ok"] is True
    assert result["auto_select"]["triggered"] is True
    assert selector_calls


def test_subscription_refresh_reselects_when_active_server_removed(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    monkeypatch.setattr(
        pipeline_service,
        "prepare_subscription_refresh",
        lambda: {
            "ok": True,
            "stage": "candidate_validated",
            "refresh": {"ok": True},
            "candidate": {"candidate_path": str(tmp_path / "candidate.yaml")},
            "config_validation": {"ok": True},
            "promoted": False,
            "container_restarted": False,
            "error": None,
        },
    )
    monkeypatch.setattr(
        pipeline_service,
        "reconcile_mihomo_runtime",
        lambda: {
            "ok": True,
            "reconcile_action": "restart",
            "reconcile_reason": "config_reload_required",
            "promoted": {"ok": True, "promoted": True},
            "container": {"ok": True, "action": "restart"},
        },
    )
    monkeypatch.setattr(
        pipeline_service,
        "get_routing_global_state",
        lambda: {"server_mode": "auto"},
    )
    state_calls = {"count": 0}

    def _fake_state():
        state_calls["count"] += 1
        if state_calls["count"] == 1:
            return {
                "server_mode": "auto",
                "enabled_candidates_count": 1,
                "auto_selectable_candidates_count": 1,
                "active_auto_server_id": "removed",
                "active_auto_server_valid": False,
            }
        return {
            "server_mode": "auto",
            "enabled_candidates_count": 1,
            "auto_selectable_candidates_count": 1,
            "active_auto_server_id": "beta",
            "active_auto_server_valid": True,
        }

    selector_calls: list[dict[str, object]] = []
    monkeypatch.setattr(pipeline_service, "get_vpn_auto_state", _fake_state)
    monkeypatch.setattr(
        pipeline_service,
        "select_vpn_auto_server",
        lambda **kwargs: selector_calls.append(kwargs) or {"ok": True, "selected_server_id": "beta", "active_after": "beta"},
    )

    result = pipeline_service.apply_subscription_refresh()

    assert result["ok"] is True
    assert result["auto_select"]["status"] == "auto_selected"
    assert selector_calls


def test_subscription_refresh_does_not_select_when_server_mode_fixed(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    monkeypatch.setattr(
        pipeline_service,
        "prepare_subscription_refresh",
        lambda: {
            "ok": True,
            "stage": "candidate_validated",
            "refresh": {"ok": True},
            "candidate": {"candidate_path": str(tmp_path / "candidate.yaml")},
            "config_validation": {"ok": True},
            "promoted": False,
            "container_restarted": False,
            "error": None,
        },
    )
    monkeypatch.setattr(
        pipeline_service,
        "reconcile_mihomo_runtime",
        lambda: {
            "ok": True,
            "reconcile_action": "none",
            "reconcile_reason": "unchanged_config",
            "promoted": {"ok": True, "promoted": False},
            "container": {"ok": True, "action": "none"},
        },
    )
    monkeypatch.setattr(
        pipeline_service,
        "get_routing_global_state",
        lambda: {"server_mode": "fixed"},
    )
    monkeypatch.setattr(
        pipeline_service,
        "get_vpn_auto_state",
        lambda: {"server_mode": "fixed"},
    )

    called = {"value": False}
    monkeypatch.setattr(
        pipeline_service,
        "select_vpn_auto_server",
        lambda **kwargs: called.__setitem__("value", True) or {"ok": True},
    )

    result = pipeline_service.apply_subscription_refresh()

    assert result["ok"] is True
    assert result["auto_select"]["triggered"] is False
    assert called["value"] is False


def test_subscription_get_endpoint_redacts_url(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    save_subscription_url("https://example.test/sub")

    response = _client().get("/api/v2/subscription")

    assert response.status_code == 200
    payload = response.json()["data"]["subscription"]
    assert payload["url_saved"] is True
    assert "url" not in payload


def test_subscription_validate_endpoint_returns_error(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    response = _client().post("/api/v2/subscription/validate", json={"url": "ftp://bad"})

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "SUBSCRIPTION_URL_INVALID_SCHEME"


def test_subscription_save_endpoint_redacts_url(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    response = _client().post("/api/v2/subscription", json={"url": "https://example.test/sub", "metadata": {"source": "pytest"}})

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["data"]["subscription"]["url_saved"] is True
    assert "url" not in body["data"]["subscription"]


def test_subscription_refresh_endpoint_returns_error_when_refresh_fails(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    save_subscription_url("https://example.test/sub")
    monkeypatch.setattr(subscription_adapter_module, "DEFAULT_SUBSCRIPTION_ADAPTER", _FakeSubscriptionAdapter(_failed_refresh_result()))

    response = _client().post("/api/v2/subscription/refresh")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "SUBSCRIPTION_DOWNLOAD_FAILED"


def test_subscription_refresh_endpoint_success_applies_changed_candidate(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    save_subscription_url("https://example.test/sub")
    monkeypatch.setattr(
        "fwrouter_api.routes.subscription.apply_subscription_refresh",
        lambda: {
            "ok": True,
            "stage": "applied",
            "validation": {"valid": True, "normalized_url": "https://example.test/sub", "error": None},
            "state": get_subscription_state(),
            "refresh": _success_refresh_result("alpha").to_dict(),
            "candidate": {"candidate_path": str(tmp_path / "candidate.yaml"), "active_path": str(tmp_path / "active.yaml")},
            "config_validation": {"ok": True, "returncode": 0, "stdout_tail": "ok", "stderr_tail": ""},
            "promoted": True,
            "container_restarted": True,
        },
    )

    response = _client().post("/api/v2/subscription/refresh")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["data"]["promoted"] is True
    assert body["data"]["container_restarted"] is True


def test_subscription_refresh_success_does_not_mutate_subject_inventory(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    save_subscription_url("https://example.test/sub")
    monkeypatch.setattr(subscription_adapter_module, "DEFAULT_SUBSCRIPTION_ADAPTER", _FakeSubscriptionAdapter(_success_refresh_result("alpha")))

    refresh_subscription_inventory()
    subjects = list_subjects(limit=100)

    assert subjects == []
