from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import db_session, initialize_database
from fwrouter_api.services.server_state import ensure_routing_global_state
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
        lambda **_kwargs: {"ok": True, "triggered": False, "status": "skipped_not_auto_mode"},
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


@pytest.mark.parametrize(
    "generation_apply",
    [
        {
            "transition_mihomo": {"stage": "unchanged"},
            "xray": {"stage": "applied"},
            "final_mihomo": {"stage": "unchanged"},
        },
        {
            "transition_mihomo": {"stage": "unchanged"},
            "xray": {"stage": "unchanged"},
            "final_mihomo": {"stage": "unchanged"},
            "public_snapshots_changed": True,
        },
    ],
    ids=["xray-only", "snapshot-only"],
)
def test_managed_xray_generation_runs_before_readback_and_reports_generation_change(
    monkeypatch, generation_apply: dict[str, object],
) -> None:
    events: list[str] = []
    monkeypatch.setattr(
        "fwrouter_api.services.xray_runtime_state._module_state",
        lambda _name: {"desired_state": "enabled", "lifecycle_mode": "managed"},
    )
    monkeypatch.setattr(
        subscription_pipeline,
        "_reconcile_xray_after_authoritative_inventory_refresh",
        lambda _prepared, *, verification_callback=None: events.append("generation") or (
            {"ok": True, "status": "success", "created_count": 0, "deleted_count": 0},
            {
                "ok": True,
                "status": "success",
                "nodes_count": 1,
                "generation_apply": generation_apply,
                "public_profile_promote": {"profiles_count": 1, "nodes_count": 1},
                "pre_publication_verification": verification_callback(operation_id="test-op", expected_selection_revision=0) if verification_callback else {"ok": True},
            },
        ),
    )
    def forbidden_second_apply(**_kwargs):
        events.append("second-apply")
        raise AssertionError("staged generation must not open a second apply window")
    monkeypatch.setattr(subscription_pipeline, "reconcile_mihomo_runtime", forbidden_second_apply)
    monkeypatch.setattr(
        subscription_pipeline,
        "_maybe_select_vpn_auto_after_refresh",
        lambda **_kwargs: events.append("selector") or {"ok": True, "triggered": False, "status": "skipped_not_auto_mode"},
    )
    monkeypatch.setattr(subscription_pipeline, "write_operational_log", lambda **_kwargs: None)
    monkeypatch.setattr(subscription_pipeline, "write_technical_log", lambda **_kwargs: None)

    result = subscription_pipeline.apply_prepared_subscription_refresh({
        "ok": True,
        "stage": "already_current",
        "refresh": {"ok": True, "batch": {"errors": 0}},
        "candidate": {"skipped": True},
        "promoted": False,
        "container_restarted": False,
    })

    assert events == ["generation", "selector"]
    assert result["ok"] is True
    assert result["applied"] is True
    assert result["stage"] == "applied"
    assert result["container_restarted"] is False


def test_managed_xray_generation_failure_skips_followup_mihomo_apply(monkeypatch) -> None:
    monkeypatch.setattr(
        "fwrouter_api.services.xray_runtime_state._module_state",
        lambda _name: {"desired_state": "enabled", "lifecycle_mode": "managed"},
    )
    monkeypatch.setattr(
        subscription_pipeline,
        "_reconcile_xray_after_authoritative_inventory_refresh",
        lambda _prepared, *, verification_callback=None: (
            {"ok": False, "error_code": "XRAY_GENERATION_CANDIDATE_INVALID"},
            {"ok": False, "status": "failed", "error_code": "XRAY_GENERATION_CANDIDATE_INVALID"},
        ),
    )
    monkeypatch.setattr(
        subscription_pipeline,
        "reconcile_mihomo_runtime",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("failed generation must preserve last-good runtime")),
    )
    monkeypatch.setattr(subscription_pipeline, "write_operational_log", lambda **_kwargs: None)
    monkeypatch.setattr(subscription_pipeline, "write_technical_log", lambda **_kwargs: None)

    result = subscription_pipeline.apply_prepared_subscription_refresh({
        "ok": True,
        "stage": "candidate_validated",
        "refresh": {"ok": True},
        "candidate": {},
    })

    assert result["ok"] is False
    assert result["stage"] == "xray_generation"
    assert result["error"]["code"] == "XRAY_GENERATION_CANDIDATE_INVALID"


def test_generation_restore_cas_restores_selector_provenance_after_failed_publication(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("FWROUTER_STATE_DIR", str(tmp_path / "state"))
    get_settings.cache_clear()
    initialize_database()
    ensure_routing_global_state()
    with db_session() as connection:
        connection.execute("INSERT INTO servers (server_id, server_name, inventory_state) VALUES ('last-good', 'Last good', 'active')")
        connection.execute("INSERT INTO servers (server_id, server_name, inventory_state) VALUES ('candidate', 'Candidate', 'active')")
        connection.execute("INSERT INTO server_preferences (server_id, vpn_auto, vpn_auto_priority) VALUES ('last-good', 1, 0)")
        connection.execute("INSERT INTO server_preferences (server_id, vpn_auto, vpn_auto_priority) VALUES ('candidate', 1, 0)")
        connection.execute("UPDATE routing_global_state SET active_auto_server_id='last-good' WHERE id=1")
        connection.execute(
            "INSERT INTO settings (key, value_json) VALUES ('routing.auto_selection_provenance', ?)",
            (json.dumps({"selected_server_id": "last-good", "decision_id": "before"}),),
        )
    before = xray_subscription_service._capture_generation_auto_selection()
    with db_session() as connection:
        from fwrouter_api.services.vpn_auto_selection_state import commit_active_selection
        assert commit_active_selection(
            connection, expected_revision=before["selection_revision"],
            expected_active_server_id="last-good", expected_provenance_decision_id="before",
            server_id="candidate", provenance={"selected_server_id": "candidate", "decision_id": "after"},
        ) == before["selection_revision"] + 1
    runtime = SimpleNamespace(
        health=lambda: SimpleNamespace(details={"selectors": {"vpn_auto_now": "candidate"}}),
        apply_server=lambda target: SimpleNamespace(ok=True, details={"selector_after": target}),
    )
    monkeypatch.setattr("fwrouter_api.services.selector._active_selector_runtime", lambda: (
        {"adapter_id": "mock", "capabilities": ["health", "apply_server"]}, runtime,
    ))
    monkeypatch.setattr("fwrouter_api.services.selector._runtime_generation_identity", lambda *_args, **_kwargs: "generation-test")
    after = xray_subscription_service._capture_generation_auto_selection()

    assert xray_subscription_service._restore_generation_auto_selection(
        before=before, after=after,
    ) is True
    restored = xray_subscription_service._capture_generation_auto_selection()
    assert restored["routing"]["active_auto_server_id"] == "last-good"
    assert restored["selection_revision"] > after["selection_revision"]
    assert json.loads(restored["provenance"]["value_json"])["reason_code"] == "generation_rollback"

    with db_session() as connection:
        connection.execute("INSERT INTO servers (server_id, server_name, inventory_state) VALUES ('external', 'External', 'active')")
        connection.execute("UPDATE routing_global_state SET active_auto_server_id='external' WHERE id=1")
    assert xray_subscription_service._restore_generation_auto_selection(
        before=before, after=after,
    ) is False
