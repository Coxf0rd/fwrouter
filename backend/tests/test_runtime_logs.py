from __future__ import annotations

import json
from pathlib import Path

from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import initialize_database
from fwrouter_api.services import mihomo_config as mihomo_config_service
from fwrouter_api.services import xray as xray_service


def _configure_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FWROUTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("FWROUTER_DATABASE_URL", f"sqlite:///{tmp_path}/fwrouter.db")
    get_settings.cache_clear()


def test_mihomo_reconcile_skip_writes_only_technical_log(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    monkeypatch.setattr(mihomo_config_service, "_collect_xray_handoff_assignments", lambda: [])

    written_operational: list[dict] = []
    written_technical: list[dict] = []

    def _op(**kwargs):
        written_operational.append(kwargs)
        return kwargs

    def _tech(**kwargs):
        written_technical.append(kwargs)
        return kwargs

    candidate_config = mihomo_config_service.build_mihomo_config({"selective_default": "direct"})
    monkeypatch.setattr(mihomo_config_service, "write_operational_log", _op)
    monkeypatch.setattr(mihomo_config_service, "write_technical_log", _tech)
    monkeypatch.setattr(mihomo_config_service, "get_mihomo_config_status", lambda: {
        "base_config": candidate_config,
        "candidate_config": candidate_config,
    })
    monkeypatch.setattr(mihomo_config_service, "write_mihomo_candidate_config", lambda routing=None: {
        "candidate_path": "candidate",
        "rules": candidate_config["rules"],
        "handoff_assignments": [],
        "resolved_selective_default": "direct",
        "final_match_rule": "MATCH,DIRECT",
        "transparent_final_match_rule": "MATCH,DIRECT",
        "config": candidate_config,
    })
    monkeypatch.setattr(mihomo_config_service, "validate_mihomo_candidate_config", lambda routing=None: {
        "ok": True,
        "resolved_selective_default": "direct",
        "final_match_rule": "MATCH,DIRECT",
        "expected_final_match_rule": "MATCH,DIRECT",
        "transparent_final_match_rule": "MATCH,DIRECT",
        "expected_transparent_final_match_rule": "MATCH,DIRECT",
        "state_consistency_ok": True,
        "transparent_state_consistency_ok": True,
    })

    result = mihomo_config_service.reconcile_mihomo_runtime({"selective_default": "direct"})

    assert result["ok"] is True
    assert written_operational == []
    assert written_technical[-1]["event_type"] == "mihomo_reconcile_skipped"


def test_xray_materialize_failure_writes_both_log_types(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    written_operational: list[dict] = []
    written_technical: list[dict] = []

    monkeypatch.setattr(xray_service, "write_operational_log", lambda **kwargs: written_operational.append(kwargs) or kwargs)
    monkeypatch.setattr(xray_service, "write_technical_log", lambda **kwargs: written_technical.append(kwargs) or kwargs)
    monkeypatch.setattr(xray_service, "collect_xray_runtime_bindings", lambda: [])
    monkeypatch.setattr(xray_service, "_write_xray_bindings_state", lambda bindings, applied_ok=False: {"ok": applied_ok})

    class _Result:
        ok = False
        message = "apply failed"
        error_code = "XRAY_BINDINGS_APPLY_FAILED"
        details = {"stage": "reload"}

    class _Adapter:
        def materialize_client_bindings(self, bindings):
            return _Result()

    monkeypatch.setattr(xray_service, "DEFAULT_XRAY_ADAPTER", _Adapter())

    result = xray_service.materialize_xray_runtime_bindings(
        requested_by="pytest",
        prepare_mihomo_handoff=False,
    )

    assert result["ok"] is False
    assert written_operational[-1]["event_type"] == "xray_binding_materialization_failed"
    assert written_technical[-1]["event_type"] == "xray_binding_materialization_failed"
