from __future__ import annotations

from fwrouter_api.services import subscription_pipeline
from fwrouter_api.services import xray_runtime_state
from fwrouter_api.services import xray_subscription_service


def test_successful_partial_refresh_runs_vpn_auto_reconcile(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        xray_runtime_state,
        "_module_state",
        lambda _name: {"desired_state": "enabled", "lifecycle_mode": "managed"},
    )
    monkeypatch.setattr(
        xray_subscription_service,
        "reconcile_xray_vpn_auto_subscription",
        lambda **_kwargs: calls.append("vpn-auto")
        or {
            "ok": True,
            "status": "success",
            "created_count": 0,
            "deleted_count": 1,
            "nodes_count": 2,
            "created": [{"client_uuid": "must-not-escape"}],
            "profile_reconcile": {"ok": True, "status": "success", "nodes_count": 4},
        },
    )

    auto, profiles = subscription_pipeline._reconcile_xray_after_authoritative_inventory_refresh(
        {
            "ok": True,
            "refresh": {"ok": True, "batch": {"errors": 1}},
        }
    )

    assert calls == ["vpn-auto"]
    assert auto["deleted_count"] == 1
    assert "created" not in auto
    assert profiles == {"ok": True, "status": "success", "nodes_count": 4}


def test_all_failed_or_synthetic_refresh_never_runs_vpn_auto_pruning(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        xray_subscription_service,
        "reconcile_xray_vpn_auto_subscription",
        lambda **_kwargs: calls.append("vpn-auto") or {"ok": True, "status": "success"},
    )
    monkeypatch.setattr(
        subscription_pipeline,
        "_reconcile_xray_subscription_profiles_after_refresh",
        lambda **_kwargs: {"ok": True, "status": "success", "nodes_count": 1},
    )

    all_failed, _ = subscription_pipeline._reconcile_xray_after_authoritative_inventory_refresh(
        {"ok": False, "refresh": {"ok": False}}
    )
    synthetic, _ = subscription_pipeline._reconcile_xray_after_authoritative_inventory_refresh(
        {"ok": True, "candidate": {"validated": True}}
    )

    assert calls == []
    assert all_failed["status"] == "skipped"
    assert synthetic["status"] == "skipped"


def test_non_managed_xray_keeps_existing_profile_only_refresh_path(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        xray_runtime_state,
        "_module_state",
        lambda _name: {"desired_state": "enabled", "lifecycle_mode": "external"},
    )
    monkeypatch.setattr(
        xray_subscription_service,
        "reconcile_xray_vpn_auto_subscription",
        lambda **_kwargs: calls.append("vpn-auto") or {"ok": True, "status": "success"},
    )
    monkeypatch.setattr(
        subscription_pipeline,
        "_reconcile_xray_subscription_profiles_after_refresh",
        lambda **_kwargs: calls.append("profiles") or {"ok": True, "status": "skipped"},
    )

    auto, profiles = subscription_pipeline._reconcile_xray_after_authoritative_inventory_refresh(
        {"ok": True, "refresh": {"ok": True}}
    )

    assert calls == ["profiles"]
    assert auto["reason"] == "managed_xray_runtime_unavailable"
    assert profiles["status"] == "skipped"


def test_already_current_successful_refresh_still_reconciles_vpn_auto(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        subscription_pipeline,
        "reconcile_mihomo_runtime",
        lambda **_kwargs: {
            "ok": True,
            "reconcile_action": "none",
            "reconcile_reason": "unchanged_config",
            "promoted": {"ok": True, "promoted": False},
            "container": {"ok": True, "action": "none"},
        },
    )
    monkeypatch.setattr(
        subscription_pipeline,
        "_maybe_select_vpn_auto_after_refresh",
        lambda: {"ok": True, "triggered": False, "status": "skipped_not_auto_mode"},
    )
    monkeypatch.setattr(
        subscription_pipeline,
        "_reconcile_xray_after_authoritative_inventory_refresh",
        lambda prepared: calls.append("vpn-auto")
        or (
            {"ok": True, "status": "success", "created_count": 0, "deleted_count": 1},
            {"ok": True, "status": "success", "nodes_count": 1},
        ),
    )
    monkeypatch.setattr(
        "fwrouter_api.services.subscription_profiles.list_desired_subscription_xray_clients",
        lambda: [{"client_uuid": "profile-uuid"}],
    )
    monkeypatch.setattr(
        "fwrouter_api.services.subscription_profiles.promote_runtime_verified_subscription_nodes",
        lambda nodes: {"profiles_count": 1, "nodes_count": len(nodes)},
    )
    monkeypatch.setattr(subscription_pipeline, "write_operational_log", lambda **_kwargs: None)
    monkeypatch.setattr(subscription_pipeline, "write_technical_log", lambda **_kwargs: None)

    result = subscription_pipeline.apply_prepared_subscription_refresh(
        {
            "ok": True,
            "stage": "already_current",
            "refresh": {"ok": True, "batch": {"errors": 0}},
            "candidate": {"skipped": True},
            "promoted": False,
            "container_restarted": False,
        }
    )

    assert calls == ["vpn-auto"]
    assert result["ok"] is True
    assert result["stage"] == "already_current"
    assert result["xray_vpn_auto_reconcile"]["deleted_count"] == 1
