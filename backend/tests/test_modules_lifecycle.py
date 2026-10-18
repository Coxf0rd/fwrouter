from __future__ import annotations
from _test_support import configure_test_state_dir_and_clear_live_probe_cache as _configure_env

from pathlib import Path

from fastapi.testclient import TestClient

from fwrouter_api.db.connection import connect, initialize_database
from fwrouter_api.db.connection import db_session
from fwrouter_api.main import app
from fwrouter_api.services import mihomo_config as mihomo_config_service
from fwrouter_api.services import xray as xray_service
from fwrouter_api.services.modules import (
    get_module_state,
    set_module_lifecycle_mode,
)




def test_modules_schema_defaults_lifecycle_modes(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    schema_state = initialize_database()

    assert schema_state["ok"] is True
    from fwrouter_api.db.migrations import CURRENT_SCHEMA_VERSION

    assert schema_state["actual_schema_version"] == str(CURRENT_SCHEMA_VERSION)

    with connect() as connection:
        rows = {
            row["module_name"]: row["lifecycle_mode"]
            for row in connection.execute(
                "SELECT module_name, lifecycle_mode FROM modules"
            ).fetchall()
        }

    assert rows["core"] == "managed"
    assert rows["vpn"] == "managed"
    assert rows["watchdog"] == "managed"
    assert "xray" not in rows
    assert "tailscale" not in rows


def test_core_explicit_requests_advance_revision_even_when_repeated(monkeypatch, tmp_path: Path) -> None:
    from fwrouter_api.services.core_intent import read_core_intent_revision
    from fwrouter_api.services.modules import set_module_desired_state

    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    with db_session() as connection:
        before = read_core_intent_revision(connection)
    disabled = set_module_desired_state("core", "disabled", requested_by="pytest", run_now=False)
    first_enable = set_module_desired_state("core", "enabled", requested_by="pytest", run_now=False)
    second_enable = set_module_desired_state("core", "enabled", requested_by="pytest", run_now=False)
    with db_session() as connection:
        after = read_core_intent_revision(connection)
    assert disabled["module"]["apply_state"] == "clean"
    assert first_enable["module"]["apply_state"] == "pending"
    assert second_enable["module"]["apply_state"] == "pending"
    assert after == before + 3


def test_malformed_core_revision_fails_closed_without_desired_state_write(monkeypatch, tmp_path: Path) -> None:
    import pytest
    from fwrouter_api.services.core_intent import read_core_intent_revision
    from fwrouter_api.services.modules import set_module_desired_state

    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    with db_session() as connection:
        connection.execute(
            "UPDATE settings SET value_json='true' WHERE key='core.intent_revision'",
        )
    with pytest.raises(ValueError, match="CORE_INTENT_REVISION_INVALID"):
        set_module_desired_state("core", "enabled", requested_by="pytest", run_now=False)
    with db_session() as connection:
        state = connection.execute(
            "SELECT desired_state, apply_state FROM modules WHERE module_name='core'",
        ).fetchone()
        with pytest.raises(ValueError, match="CORE_INTENT_REVISION_INVALID"):
            read_core_intent_revision(connection)
    assert tuple(state) == ("enabled", "clean")


def test_set_module_lifecycle_mode_marks_absent_integration(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    module = set_module_lifecycle_mode("xray", "none")

    assert module["lifecycle_mode"] == "none"
    assert module["installed"] is False
    assert module["runtime_state"] == "not_configured"
    assert module["apply_state"] == "clean"


def test_modules_api_exposes_lifecycle_and_install_fields(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    response = TestClient(app).get("/api/v2/modules")

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    modules = {item["module_name"]: item for item in payload["data"]["modules"]}
    assert "tailscale" not in modules
    assert "xray" not in modules
    assert "installed" in modules["core"]


def test_lifecycle_endpoint_rejects_external_mode_for_core(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    response = TestClient(app).post(
        "/api/v2/modules/core/lifecycle-mode",
        json={"lifecycle_mode": "external"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is False
    assert payload["error"]["code"] == "MODULE_STATE_INVALID"
    assert get_module_state("core")["lifecycle_mode"] == "managed"


def test_external_xray_rejects_managed_runtime_mutation(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    set_module_lifecycle_mode("xray", "external")

    response = TestClient(app).post(
        "/api/v2/xray/clients",
        json={"alias": "External client", "requested_by": "pytest"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is False
    assert payload["error"]["code"] == "XRAY_MANAGED_RUNTIME_REQUIRED"
    assert "lifecycle_mode=managed" in payload["error"]["message"]


def test_external_xray_service_blocks_before_adapter(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    set_module_lifecycle_mode("xray", "external")

    class _UnexpectedAdapter:
        def create_client(self, **kwargs):  # noqa: ANN003, ANN202
            raise AssertionError("external Xray must not call managed adapter mutations")

    monkeypatch.setattr(xray_service, "DEFAULT_XRAY_ADAPTER", _UnexpectedAdapter())

    result = xray_service.create_xray_client(alias="External client", requested_by="pytest")

    assert result["ok"] is False
    assert result["error_code"] == "XRAY_MANAGED_RUNTIME_REQUIRED"


def test_external_mihomo_rejects_managed_runtime_mutation(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    set_module_lifecycle_mode("vpn", "external")

    def _unexpected_validation() -> dict[str, object]:
        raise AssertionError("external Mihomo must not run Docker validation")

    monkeypatch.setattr(
        mihomo_config_service,
        "validate_mihomo_candidate_config",
        _unexpected_validation,
    )

    response = TestClient(app).post("/api/v2/mihomo/config/promote")

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is False
    assert payload["error"]["code"] == "MIHOMO_MANAGED_RUNTIME_REQUIRED"
    assert "lifecycle_mode=managed" in payload["error"]["message"]


def test_external_mihomo_service_blocks_before_validation(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    set_module_lifecycle_mode("vpn", "external")

    def _unexpected_validation() -> dict[str, object]:
        raise AssertionError("external Mihomo must not run managed config validation")

    monkeypatch.setattr(
        mihomo_config_service,
        "validate_mihomo_candidate_config",
        _unexpected_validation,
    )

    result = mihomo_config_service.validate_and_promote_mihomo_candidate_config()

    assert result["ok"] is False
    assert result["error_code"] == "MIHOMO_MANAGED_RUNTIME_REQUIRED"
