from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import time
from threading import Event
import pytest

from fwrouter_api.db.connection import db_session
from fwrouter_api.jobs.manager import get_default_job_manager
from fwrouter_api.services.xray_vpn_auto_pending import (
    XRAY_VPN_AUTO_DEBOUNCE_SECONDS,
    XRAY_VPN_AUTO_PENDING_KEY,
    XRAY_VPN_AUTO_RETRY_SECONDS,
    _finish_revision,
    clear_pending_revision_after_success,
    dispatch_due_xray_vpn_auto_reconcile,
    get_xray_vpn_auto_pending_state,
    mark_xray_vpn_auto_pending,
    pending_is_due,
    run_xray_vpn_auto_reconcile,
)


def _seed_server(server_id: str, *, vpn_auto: bool, priority: int, global_list: bool = True) -> None:
    with db_session() as connection:
        connection.execute(
            "INSERT INTO servers (server_id, server_name, provider_name, inventory_state, raw_json) "
            "VALUES (?, ?, 'pytest', 'active', '{}')",
            (server_id, server_id),
        )
        connection.execute(
            "INSERT INTO server_preferences (server_id, vpn_auto, vpn_auto_priority, vpn_auto_priority_origin, global_list) "
            "VALUES (?, ?, ?, 'manual', ?)",
            (server_id, int(vpn_auto), priority, int(global_list)),
        )


def _mark(trigger: str = "test", *, now: datetime | None = None) -> dict:
    with db_session() as connection:
        return mark_xray_vpn_auto_pending(connection, trigger=trigger, now=now)


def test_trailing_quiet_deadline_persists_and_resets_revision() -> None:
    base = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
    first = _mark(now=base)
    second = _mark(now=base + timedelta(seconds=90))

    assert first["revision"] == 1
    assert second["revision"] == 2
    assert second["due_at"] == "2026-09-29T12:04:30Z"
    assert not pending_is_due(second, now=base + timedelta(seconds=269))
    assert pending_is_due(second, now=base + timedelta(seconds=270))
    # Readback uses the settings row, so a process restart does not lose the deadline.
    assert get_xray_vpn_auto_pending_state()["revision"] == 2
    assert XRAY_VPN_AUTO_DEBOUNCE_SECONDS == 180


def test_concurrent_explicit_markers_get_distinct_monotonic_revisions() -> None:
    def mark_one(index: int) -> int:
        return _mark(f"parallel-{index}")["revision"]

    with ThreadPoolExecutor(max_workers=6) as pool:
        revisions = list(pool.map(mark_one, range(6)))
    assert sorted(revisions) == list(range(1, 7))
    assert get_xray_vpn_auto_pending_state()["revision"] == 6


def test_failure_retains_pending_with_bounded_retry_and_success_is_revision_cas() -> None:
    now = datetime.now(timezone.utc)
    marked = _mark(now=now)
    assert _finish_revision(marked["revision"], ok=False, error_code="TEST_FAILED", now=now)
    failed = get_xray_vpn_auto_pending_state()
    assert failed["pending"] is True
    assert failed["status"] == "failed"
    assert failed["last_error_code"] == "TEST_FAILED"
    assert datetime.fromisoformat(failed["next_retry_at"].replace("Z", "+00:00")) == now + timedelta(seconds=XRAY_VPN_AUTO_RETRY_SECONDS)
    assert not pending_is_due(failed, now=now + timedelta(seconds=XRAY_VPN_AUTO_RETRY_SECONDS - 1))
    assert pending_is_due(failed, now=now + timedelta(seconds=XRAY_VPN_AUTO_RETRY_SECONDS + 1))

    newer = _mark(now=now + timedelta(seconds=XRAY_VPN_AUTO_RETRY_SECONDS + 2))
    assert clear_pending_revision_after_success(marked["revision"]) is False
    assert get_xray_vpn_auto_pending_state()["revision"] == newer["revision"]
    assert get_xray_vpn_auto_pending_state()["pending"] is True
    assert clear_pending_revision_after_success(newer["revision"]) is True
    assert get_xray_vpn_auto_pending_state()["status"] == "applied"


def test_manual_preferences_only_debounce_eligibility_shape(monkeypatch) -> None:
    from fwrouter_api.services.server_preferences import update_server_preferences

    _seed_server("eligible", vpn_auto=True, priority=-1)
    _seed_server("manual-only", vpn_auto=False, priority=-1)
    monkeypatch.setattr(
        "fwrouter_api.services.server_preferences._maybe_reselect_vpn_auto_after_membership_change",
        lambda **_: {"ok": True, "triggered": False},
    )
    calls: list[bool] = []

    def callback(*, enabled: bool):
        calls.append(enabled)
        return {"ok": True}

    # Cross the auto-eligibility boundary; intent/audit and pending marker commit together.
    assert update_server_preferences("eligible", vpn_auto_priority=0, reconcile_after_preferences=callback)["ok"]
    state = get_xray_vpn_auto_pending_state()
    assert state["pending"] is True and state["revision"] == 1
    # 0 <-> 1 remains eligible and does not enqueue another Xray batch.
    assert update_server_preferences("eligible", vpn_auto_priority=1, reconcile_after_preferences=callback)["ok"]
    assert get_xray_vpn_auto_pending_state()["revision"] == 1
    # A global-list-only update still uses immediate Mihomo callback, never Xray pending.
    assert update_server_preferences("eligible", global_list=False, reconcile_after_preferences=callback)["ok"]
    assert get_xray_vpn_auto_pending_state()["revision"] == 1
    # Enabling vpn_auto while priority remains -1 is not an eligible Xray identity.
    assert update_server_preferences("manual-only", vpn_auto=True, reconcile_after_preferences=callback)["ok"]
    assert get_xray_vpn_auto_pending_state()["revision"] == 1
    assert calls == [True, False, True, True]


def test_bulk_membership_batches_only_changed_eligible_set(monkeypatch) -> None:
    from fwrouter_api.services.server_preferences import replace_vpn_auto_servers

    _seed_server("a", vpn_auto=True, priority=1)
    _seed_server("b", vpn_auto=False, priority=1)
    monkeypatch.setattr(
        "fwrouter_api.services.server_preferences._maybe_reselect_vpn_auto_after_membership_change",
        lambda **_: {"ok": True, "triggered": False},
    )
    result = replace_vpn_auto_servers(["b"], reconcile_after_preferences=lambda **_: {"ok": True})
    assert result["ok"] is True
    state = get_xray_vpn_auto_pending_state()
    assert state["pending"] is True
    assert state["revision"] == 1
    assert state["trigger"] == "vpn_auto_membership"


def test_scheduler_dispatch_due_and_deferred_state(monkeypatch) -> None:
    from fwrouter_api.services import xray_vpn_auto_pending as pending

    now = datetime.now(timezone.utc)
    monkeypatch.setattr(pending, "_inventory_and_runtime_authoritative", lambda: True)
    monkeypatch.setattr(pending, "_confirmed_applied_config_drift", lambda: False)
    dispatched: list[dict] = []
    monkeypatch.setattr(
        pending,
        "submit_xray_vpn_auto_reconcile",
        lambda **kwargs: dispatched.append(kwargs) or {"ok": True, "status": "dispatched"},
    )
    _mark(now=now)
    monkeypatch.setattr(pending, "_inventory_and_runtime_authoritative", lambda: False)
    assert dispatch_due_xray_vpn_auto_reconcile(now=now)["status"] == "not_due"
    assert get_xray_vpn_auto_pending_state()["status"] == "pending"

    _mark(now=now - timedelta(seconds=XRAY_VPN_AUTO_DEBOUNCE_SECONDS + 1))
    monkeypatch.setattr(pending, "_inventory_and_runtime_authoritative", lambda: True)
    assert dispatch_due_xray_vpn_auto_reconcile(now=now)["status"] == "dispatched"
    assert dispatched == [{
        "requested_by": "runtime_convergence_scheduler",
        "bypass": False,
        "expected_revision": get_xray_vpn_auto_pending_state()["revision"],
    }]

    monkeypatch.setattr(pending, "_inventory_and_runtime_authoritative", lambda: False)
    result = dispatch_due_xray_vpn_auto_reconcile(now=now)
    assert result["status"] == "deferred"
    deferred = get_xray_vpn_auto_pending_state()
    assert deferred["pending"] is True
    assert deferred["status"] == "deferred"
    # Recovery after Xray/provider authority returns bypasses the old quiet deadline.
    monkeypatch.setattr(pending, "_inventory_and_runtime_authoritative", lambda: True)
    result = dispatch_due_xray_vpn_auto_reconcile(now=now)
    assert result["status"] == "dispatched"
    assert dispatched[-1]["bypass"] is True


def test_only_readable_active_config_mismatch_against_last_applied_snapshot_is_critical(monkeypatch) -> None:
    from fwrouter_api.services import xray_materialize, xray_runtime_state
    from fwrouter_api.services import xray_vpn_auto_pending as pending

    monkeypatch.setattr(
        xray_runtime_state,
        "_load_xray_bindings_state",
        lambda: {
            "generated_at": "2026-09-29T00:00:00Z",
            "bindings_count": 1,
            "applied_count": 1,
            "bindings": [{"client_email": "known@fwrouter.local"}],
            "client_modes": [],
        },
    )
    monkeypatch.setattr(xray_materialize, "_load_active_config_payload", lambda: ({"inbounds": []}, None))
    monkeypatch.setattr(
        xray_materialize,
        "_verify_active_config_bindings",
        lambda _: {"ok": False, "missing_clients": [{"client_email": "known@fwrouter.local"}]},
    )
    monkeypatch.setattr(xray_materialize, "_verify_active_config_client_modes", lambda _: {"ok": True})
    assert pending._confirmed_applied_config_drift() is True

    monkeypatch.setattr(xray_materialize, "_load_active_config_payload", lambda: (None, {"reason": "active_config_unreadable"}))
    assert pending._confirmed_applied_config_drift() is False
    monkeypatch.setattr(xray_materialize, "_load_active_config_payload", lambda: ({"inbounds": []}, None))
    monkeypatch.setattr(xray_materialize, "_verify_active_config_bindings", lambda _: {"ok": False, "reason": "active_config_unreadable"})
    assert pending._confirmed_applied_config_drift() is False


def test_scheduler_critical_drift_bypasses_quiet_window(monkeypatch) -> None:
    from fwrouter_api.services import xray_vpn_auto_pending as pending

    _mark()
    monkeypatch.setattr(pending, "_inventory_and_runtime_authoritative", lambda: True)
    monkeypatch.setattr(pending, "_confirmed_applied_config_drift", lambda: True)
    dispatched: list[dict] = []
    monkeypatch.setattr(
        pending,
        "submit_xray_vpn_auto_reconcile",
        lambda **kwargs: dispatched.append(kwargs) or {"ok": True, "status": "dispatched"},
    )
    result = dispatch_due_xray_vpn_auto_reconcile()
    assert result["status"] == "dispatched"
    assert dispatched[0]["bypass"] is True


def test_startup_bypass_clears_only_after_full_success(monkeypatch) -> None:
    from fwrouter_api.services import bootstrap

    marked = _mark()
    success = {
        "ok": True,
        "status": "success",
        "inventory_authority": "persisted_success",
        "final_mihomo_reconcile": {"ok": True},
        "public_profile_promote": {"profiles_count": 1, "nodes_count": 2},
    }
    monkeypatch.setattr(bootstrap, "_recover_startup_xray_subscription_profiles_under_guard", lambda: dict(success))
    result = bootstrap.recover_startup_xray_subscription_profiles()
    assert result["pending_revision_cleared"] is True
    assert get_xray_vpn_auto_pending_state()["pending"] is False

    newer = _mark("startup-failure")
    failed = {**success, "final_mihomo_reconcile": {"ok": False}}
    monkeypatch.setattr(bootstrap, "_recover_startup_xray_subscription_profiles_under_guard", lambda: dict(failed))
    result = bootstrap.recover_startup_xray_subscription_profiles()
    assert "pending_revision_cleared" not in result
    assert get_xray_vpn_auto_pending_state()["revision"] == newer["revision"]
    assert get_xray_vpn_auto_pending_state()["pending"] is True


def test_authoritative_subscription_bypass_clears_only_after_final_profile_promotion(monkeypatch) -> None:
    from fwrouter_api.services import subscription_pipeline

    marked = _mark()
    full_success = {
        "ok": True,
        "xray_vpn_auto_reconcile": {"ok": True, "status": "success"},
        "xray_profile_reconcile": {"ok": True, "status": "success"},
        "final_mihomo_reconcile": {"ok": True},
        "public_profile_promote": {"profiles_count": 1, "nodes_count": 2},
    }
    monkeypatch.setattr(
        subscription_pipeline,
        "_apply_prepared_subscription_refresh_under_xray_guard",
        lambda _: dict(full_success),
    )
    result = subscription_pipeline.apply_prepared_subscription_refresh({"ok": True})
    assert result["pending_revision_cleared"] is True
    assert get_xray_vpn_auto_pending_state()["status"] == "applied"

    next_revision = _mark("provider-final-failure")
    failed = {**full_success, "final_mihomo_reconcile": {"ok": False}}
    monkeypatch.setattr(
        subscription_pipeline,
        "_apply_prepared_subscription_refresh_under_xray_guard",
        lambda _: dict(failed),
    )
    result = subscription_pipeline.apply_prepared_subscription_refresh({"ok": True})
    assert "pending_revision_cleared" not in result
    assert get_xray_vpn_auto_pending_state()["revision"] == next_revision["revision"]
    assert get_xray_vpn_auto_pending_state()["pending"] is True


def test_explicit_apply_now_records_immediate_revision_and_bypasses_quiet_window(monkeypatch) -> None:
    from fwrouter_api.services import xray_vpn_auto_pending as pending

    calls: list[dict] = []
    monkeypatch.setattr(
        pending,
        "submit_xray_vpn_auto_reconcile",
        lambda **kwargs: calls.append(kwargs) or {"ok": True, "status": "dispatched"},
    )
    result = pending.request_xray_vpn_auto_apply_now(requested_by="operator")
    state = get_xray_vpn_auto_pending_state()
    assert result["status"] == "dispatched"
    assert state["pending"] is True
    assert state["trigger"] == "explicit_apply_now"
    assert pending.pending_is_due(state)
    assert calls == [{"requested_by": "operator", "bypass": True, "expected_revision": state["revision"]}]


def test_stale_queued_bypass_cannot_skip_newer_manual_quiet_window(monkeypatch) -> None:
    from fwrouter_api.services import xray_vpn_auto_pending as pending

    old = pending.mark_xray_vpn_auto_pending  # capture the public transaction writer
    with db_session() as connection:
        queued = old(connection, trigger="explicit", immediate=True)
    newer = _mark("manual", now=datetime.now(timezone.utc))
    monkeypatch.setattr(pending, "_inventory_and_runtime_authoritative", lambda: True)
    calls = {"value": 0}
    monkeypatch.setattr(
        pending,
        "_finalize_xray_vpn_auto",
        lambda **_: calls.__setitem__("value", calls["value"] + 1) or {"ok": True, "status": "success"},
    )

    result = run_xray_vpn_auto_reconcile(
        requested_by="stale-queued-job",
        bypass=True,
        expected_revision=queued["revision"],
    )
    assert result["status"] == "pending"
    assert calls["value"] == 0
    state = get_xray_vpn_auto_pending_state()
    assert state["revision"] == newer["revision"]
    assert state["pending"] is True
    assert state["status"] == "pending"


def test_critical_drift_does_not_bypass_failure_retry_deadline(monkeypatch) -> None:
    from fwrouter_api.services import xray_vpn_auto_pending as pending

    now = datetime.now(timezone.utc)
    marked = _mark(now=now - timedelta(seconds=XRAY_VPN_AUTO_DEBOUNCE_SECONDS + 1))
    _finish_revision(marked["revision"], ok=False, error_code="TEMP", now=now)
    monkeypatch.setattr(pending, "_inventory_and_runtime_authoritative", lambda: True)
    monkeypatch.setattr(pending, "_confirmed_applied_config_drift", lambda: True)
    dispatched: list[dict] = []
    monkeypatch.setattr(
        pending,
        "submit_xray_vpn_auto_reconcile",
        lambda **kwargs: dispatched.append(kwargs) or {"ok": True, "status": "dispatched"},
    )
    assert dispatch_due_xray_vpn_auto_reconcile(now=now + timedelta(seconds=XRAY_VPN_AUTO_RETRY_SECONDS - 1))["status"] == "not_due"
    assert not dispatched
    assert dispatch_due_xray_vpn_auto_reconcile(now=now + timedelta(seconds=XRAY_VPN_AUTO_RETRY_SECONDS + 1))["status"] == "dispatched"
    assert dispatched[0]["bypass"] is True


def test_preference_and_audit_rollback_together_if_pending_marker_write_fails(monkeypatch) -> None:
    from fwrouter_api.services import server_preferences

    _seed_server("atomic", vpn_auto=True, priority=-1)
    monkeypatch.setattr(
        server_preferences,
        "_maybe_reselect_vpn_auto_after_membership_change",
        lambda **_: {"ok": True, "triggered": False},
    )

    def fail_marker(*args, **kwargs):
        raise RuntimeError("marker failure")

    monkeypatch.setattr("fwrouter_api.services.xray_vpn_auto_pending.mark_xray_vpn_auto_pending", fail_marker)
    with pytest.raises(RuntimeError, match="marker failure"):
        server_preferences.update_server_preferences("atomic", vpn_auto_priority=0)

    with db_session() as connection:
        row = connection.execute(
            "SELECT vpn_auto_priority FROM server_preferences WHERE server_id = 'atomic'"
        ).fetchone()
    assert row["vpn_auto_priority"] == -1
    assert get_xray_vpn_auto_pending_state()["pending"] is False


def test_worker_cas_keeps_newer_revision_created_during_reconcile(monkeypatch) -> None:
    from fwrouter_api.services import xray_vpn_auto_pending as pending

    now = datetime.now(timezone.utc)
    first = _mark(now=now - timedelta(seconds=XRAY_VPN_AUTO_DEBOUNCE_SECONDS + 1))
    monkeypatch.setattr(pending, "_inventory_and_runtime_authoritative", lambda: True)

    def reconcile(*, requested_by: str):
        _mark("newer", now=now + timedelta(seconds=1))
        return {"ok": True, "status": "success"}

    monkeypatch.setattr(pending, "_finalize_xray_vpn_auto", reconcile)
    result = run_xray_vpn_auto_reconcile(requested_by="test", bypass=False)
    assert result["revision"] == first["revision"]
    assert result["cleared"] is False
    state = get_xray_vpn_auto_pending_state()
    assert state["revision"] == first["revision"] + 1
    assert state["pending"] is True


def test_cas_sql_update_cannot_overwrite_revision_committed_between_read_and_write(monkeypatch) -> None:
    from fwrouter_api.services import xray_vpn_auto_pending as pending

    original = _mark(now=datetime.now(timezone.utc) - timedelta(seconds=XRAY_VPN_AUTO_DEBOUNCE_SECONDS + 1))
    loaded = Event()
    resume = Event()
    save = pending._save_if_revision

    def paused_save(connection, value, revision):
        loaded.set()
        assert resume.wait(timeout=2)
        return save(connection, value, revision)

    monkeypatch.setattr(pending, "_save_if_revision", paused_save)
    with ThreadPoolExecutor(max_workers=1) as pool:
        old_finish = pool.submit(_finish_revision, original["revision"], ok=True)
        assert loaded.wait(timeout=2)
        newer = _mark("concurrent-intent")
        resume.set()
        assert old_finish.result(timeout=2) is False

    state = get_xray_vpn_auto_pending_state()
    assert state["revision"] == newer["revision"]
    assert state["pending"] is True


def test_worker_serializes_overlap_and_does_not_clear_skipped_module(monkeypatch) -> None:
    from fwrouter_api.services import xray_vpn_auto_pending as pending

    now = datetime.now(timezone.utc)
    _mark(now=now - timedelta(seconds=XRAY_VPN_AUTO_DEBOUNCE_SECONDS + 1))
    monkeypatch.setattr(pending, "_inventory_and_runtime_authoritative", lambda: True)
    count = {"value": 0}

    def slow_success(*, requested_by: str):
        count["value"] += 1
        time.sleep(0.1)
        return {"ok": True, "status": "success"}

    monkeypatch.setattr(pending, "_finalize_xray_vpn_auto", slow_success)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: run_xray_vpn_auto_reconcile(requested_by="test"), range(2)))
    assert count["value"] == 1
    assert any(result.get("cleared") for result in results)

    skipped = _mark("disabled", now=now - timedelta(seconds=XRAY_VPN_AUTO_DEBOUNCE_SECONDS + 1))
    monkeypatch.setattr(pending, "_finalize_xray_vpn_auto", lambda **_: {"ok": True, "status": "skipped"})
    result = run_xray_vpn_auto_reconcile(requested_by="test")
    state = get_xray_vpn_auto_pending_state()
    assert result["status"] == "skipped"
    assert state["revision"] == skipped["revision"]
    assert state["pending"] is True
    assert state["status"] == "deferred"
    assert pending_is_due(state)  # scheduler dispatch remains bounded to its existing 60s tick


def test_job_registration_uses_single_resource_lock() -> None:
    from fwrouter_api.jobs.extended_handlers import register_extended_handlers
    from fwrouter_api.services.xray_vpn_auto_pending import XRAY_VPN_AUTO_RECONCILE_JOB

    manager = get_default_job_manager()
    register_extended_handlers(manager)
    assert manager._get_handler(XRAY_VPN_AUTO_RECONCILE_JOB) is not None


def test_existing_xray_status_route_projects_pending_read_only_and_apply_now_route_is_additive(monkeypatch) -> None:
    from fwrouter_api.routes import xray as xray_routes

    marked = _mark("route-test")
    monkeypatch.setattr(xray_routes, "xray_service_call", lambda _: (True, {"runtime_state": "running"}))
    status = xray_routes.get_xray_endpoint()
    assert status.data["xray"]["vpn_auto_reconcile"]["revision"] == marked["revision"]
    assert status.data["xray"]["vpn_auto_reconcile"]["pending"] is True

    calls: list[dict] = []
    monkeypatch.setattr(
        "fwrouter_api.services.xray_vpn_auto_pending.request_xray_vpn_auto_apply_now",
        lambda **kwargs: calls.append(kwargs) or {"ok": True, "status": "dispatched"},
    )
    response = xray_routes.apply_xray_vpn_auto_reconcile_endpoint(
        xray_routes.XrayRequestedByRequest(requested_by="operator")
    )
    assert response.ok is True
    assert calls == [{"requested_by": "operator"}]


def test_pending_setting_payload_is_bounded_and_non_sensitive() -> None:
    _mark("server_preferences")
    with db_session() as connection:
        row = connection.execute(
            "SELECT value_json FROM settings WHERE key = ?",
            (XRAY_VPN_AUTO_PENDING_KEY,),
        ).fetchone()
    payload = json.loads(row["value_json"])
    assert set(payload) <= {
        "revision", "pending", "status", "due_at", "trigger", "updated_at",
        "attempt_count", "last_attempt_at", "last_error_code", "next_retry_at", "applied_revision",
    }
    assert "server_id" not in payload and "url" not in payload
