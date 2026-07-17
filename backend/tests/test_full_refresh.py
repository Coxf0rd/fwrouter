from __future__ import annotations

from pathlib import Path

from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import initialize_database
from fwrouter_api.services.live_probe_cache import clear_live_probe_cache
from fwrouter_api.services.full_refresh import run_full_refresh


def _configure_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FWROUTER_STATE_DIR", str(tmp_path / "state"))
    get_settings.cache_clear()
    clear_live_probe_cache()


def test_full_refresh_reports_optional_subscription_failure(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    monkeypatch.setattr(
        "fwrouter_api.services.full_refresh.request_system_subject_sync",
        lambda **kwargs: {"status": "success", "job_id": "system-job"},
    )
    monkeypatch.setattr(
        "fwrouter_api.services.full_refresh._run_subject_inventory_sync",
        lambda **kwargs: {"status": "success", "job_id": "subjects-job"},
    )
    monkeypatch.setattr(
        "fwrouter_api.services.full_refresh.sync_xray_subjects",
        lambda **kwargs: {"ok": True, "stage": "materialized"},
    )
    monkeypatch.setattr(
        "fwrouter_api.services.full_refresh.submit_rules_full_update",
        lambda **kwargs: {"status": "success", "job_id": "rules-job"},
    )
    monkeypatch.setattr(
        "fwrouter_api.services.full_refresh.apply_subscription_refresh",
        lambda: {
            "ok": False,
            "stage": "validate",
            "error": {
                "code": "SUBSCRIPTION_URL_PLACEHOLDER_HOST",
                "message": "placeholder",
            },
        },
    )

    result = run_full_refresh(requested_by="pytest")

    assert result["ok"] is True
    assert result["subscription_optional_failure"]["code"] == "SUBSCRIPTION_URL_PLACEHOLDER_HOST"
    assert result["steps"][-1]["optional"] is True
