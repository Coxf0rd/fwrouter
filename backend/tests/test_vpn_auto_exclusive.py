from __future__ import annotations

import pytest

import json
from pathlib import Path
from types import SimpleNamespace

from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import db_session, initialize_database
from fwrouter_api.services.auto_eligibility import auto_eligible_sql
from fwrouter_api.services.server_inventory import list_servers
from fwrouter_api.services.server_state import ensure_routing_global_state
from fwrouter_api.services.subscription import _source_id, get_subscription_state
from fwrouter_api.services.vpn_auto_exclusive import (
    get_vpn_auto_exclusive_source_ref,
    save_vpn_auto_exclusive_source_ref,
)


def _setup(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FWROUTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("FWROUTER_MAINTENANCE_SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("FWROUTER_WATCHDOG_SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("FWROUTER_RUNTIME_CONVERGENCE_SCHEDULER_ENABLED", "false")
    get_settings.cache_clear()
    initialize_database()
    ensure_routing_global_state()


def _seed_server(
    server_id: str,
    *,
    vpn_auto: bool = True,
    priority: int = 0,
    global_list: bool = True,
    name: str | None = None,
) -> None:
    with db_session() as connection:
        connection.execute(
            """INSERT INTO servers(server_id,server_name,provider_name,inventory_state,raw_json)
               VALUES(?,?,'pytest','active',?)""",
            (
                server_id,
                name or server_id,
                json.dumps({
                    "name": name or server_id,
                    "type": "vless",
                    "network": "tcp",
                    "tls": True,
                    "reality-opts": {"public-key": "pytest-key"},
                    "uuid": "00000000-0000-4000-8000-000000000001",
                    "server": "server.example",
                    "port": 443,
                }),
            ),
        )
        connection.execute(
            """INSERT INTO server_preferences(server_id,vpn_auto,vpn_auto_priority,global_list)
               VALUES(?,?,?,?)""",
            (server_id, int(vpn_auto), priority, int(global_list)),
        )


def _membership(source_ref: str, server_id: str, *, active: bool = True) -> None:
    with db_session() as connection:
        connection.execute(
            """INSERT INTO subscription_server_memberships
               (source_id,server_id,source_url,entry_identity_hash,is_active)
               VALUES(?,?,?, ?,?)""",
            (source_ref, server_id, f"saved:{source_ref}", f"hash:{source_ref}:{server_id}", int(active)),
        )


def _set_exclusive(source_ref: str | None) -> None:
    with db_session() as connection:
        if source_ref is None:
            connection.execute("DELETE FROM settings WHERE key='vpn_auto_exclusive_source_ref'")
        else:
            connection.execute(
                """INSERT INTO settings(key,value_json) VALUES('vpn_auto_exclusive_source_ref',?)
                   ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json""",
                (json.dumps({"source_ref": source_ref}),),
            )


def _eligible_ids(*, respect_exclusive: bool = True) -> set[str]:
    with db_session() as connection:
        rows = connection.execute(
            f"""SELECT s.server_id FROM servers s JOIN server_preferences p USING(server_id)
                WHERE {auto_eligible_sql(server_alias='s', preferences_alias='p', respect_exclusive=respect_exclusive)}
                ORDER BY s.server_id"""
        ).fetchall()
    return {str(row["server_id"]) for row in rows}


def test_exclusive_setting_is_singleton_replace_disable_and_restart_persistent(monkeypatch, tmp_path: Path) -> None:
    _setup(monkeypatch, tmp_path)
    first = _source_id("https://one.example/sub")
    second = _source_id("https://two.example/sub")
    monkeypatch.setattr(
        "fwrouter_api.services.vpn_auto_exclusive.validate_exclusive_source",
        lambda ref: {"ok": True, "source_ref": ref},
    )

    assert save_vpn_auto_exclusive_source_ref(first)["changed"] is True
    assert save_vpn_auto_exclusive_source_ref(second)["source_ref"] == second
    assert get_vpn_auto_exclusive_source_ref() == second
    with db_session() as connection:
        rows = connection.execute(
            "SELECT key,value_json FROM settings WHERE key='vpn_auto_exclusive_source_ref'"
        ).fetchall()
    assert len(rows) == 1
    assert json.loads(rows[0]["value_json"]) == {"source_ref": second}

    initialize_database()
    assert get_vpn_auto_exclusive_source_ref() == second
    assert get_subscription_state()["vpn_auto_exclusive"] == {"source_ref": second}
    assert save_vpn_auto_exclusive_source_ref(None)["source_ref"] is None
    assert get_vpn_auto_exclusive_source_ref() is None


def test_ordinary_source_pool_uses_shared_canonical_ownership(monkeypatch, tmp_path: Path) -> None:
    _setup(monkeypatch, tmp_path)
    source_a = _source_id("https://one.example/sub")
    source_b = _source_id("https://two.example/sub")
    for server_id in ("shared", "a-only", "b-only", "inactive-a"):
        _seed_server(server_id)
    _membership(source_a, "shared")
    _membership(source_b, "shared")
    _membership(source_a, "a-only")
    _membership(source_b, "b-only")
    _membership(source_a, "inactive-a", active=False)
    _set_exclusive(source_a)

    assert _eligible_ids() == {"a-only", "shared"}
    assert _eligible_ids(respect_exclusive=False) == {"a-only", "b-only", "inactive-a", "shared"}


def test_enabled_provider_source_constrains_to_root_and_disabled_binding_returns_to_ordinary(monkeypatch, tmp_path: Path) -> None:
    _setup(monkeypatch, tmp_path)
    source = _source_id("https://provider.example/sub")
    for server_id in ("provider-root", "provider-retained", "provider-member"):
        _seed_server(server_id)
    with db_session() as connection:
        connection.execute(
            """INSERT INTO provider_bindings(source_ref,provider_id,resource_id,logical_server_id,protocol,enabled,updated_at)
               VALUES(?,'pytest','resource','provider-root','hysteria2',1,0)""",
            (source,),
        )
        connection.execute(
            """INSERT INTO logical_server_topology(logical_server_id,topology_kind,selection_policy)
               VALUES('provider-root','logical_multi','fallback')"""
        )
        connection.execute(
            """INSERT INTO logical_server_members(logical_server_id,member_id,member_runtime_name,member_config_json,
               transport_fingerprint,member_order) VALUES('provider-root','provider-member','provider-member','{}','p',0)"""
        )
    _membership(source, "provider-retained")
    _set_exclusive(source)

    assert _eligible_ids() == {"provider-root"}

    with db_session() as connection:
        connection.execute("UPDATE provider_bindings SET enabled=0 WHERE source_ref=?", (source,))
    _membership(source, "provider-root")
    assert _eligible_ids() == {"provider-member", "provider-retained", "provider-root"}


def test_fixed_xray_exports_ignore_exclusive_pool(monkeypatch, tmp_path: Path) -> None:
    _setup(monkeypatch, tmp_path)
    source_a = _source_id("https://one.example/sub")
    source_b = _source_id("https://two.example/sub")
    _seed_server("a-fixed")
    _seed_server("b-fixed")
    _membership(source_a, "a-fixed")
    _membership(source_b, "b-fixed")
    _set_exclusive(source_a)

    assert _eligible_ids() == {"a-fixed"}
    assert _eligible_ids(respect_exclusive=False) == {"a-fixed", "b-fixed"}

    from fwrouter_api.services.subscription_profiles import _subscription_servers
    from fwrouter_api.services.xray_subscription_service import _vpn_auto_servers_for_xray_subscription

    profile_ids = {item["server_id"] for item in _vpn_auto_servers_for_xray_subscription()}
    exported_ids = {item["server_id"] for item in _subscription_servers()}
    assert {"a-fixed", "b-fixed"}.issubset(profile_ids)
    assert {"a-fixed", "b-fixed"}.issubset(exported_ids)


def test_inventory_marks_every_outside_independent_row_and_auto_edits_are_rejected(monkeypatch, tmp_path: Path) -> None:
    _setup(monkeypatch, tmp_path)
    source = _source_id("https://one.example/sub")
    _seed_server("inside")
    _seed_server("auto-off", vpn_auto=False, priority=-1, global_list=True)
    _membership(source, "inside")
    _set_exclusive(source)

    rows = {item["server_id"]: item for item in list_servers(limit=100)}
    assert rows["auto-off"]["vpn_auto_excluded"] is True
    assert rows["auto-off"]["auto_eligible"] is False
    assert rows["auto-off"]["selectable"] is True
    assert rows["auto-off"]["preferences"]["vpn_auto"] is False

    from fwrouter_api.services import server_preferences

    rejected = server_preferences.update_server_preferences(
        "auto-off", vpn_auto=True, reconcile_mihomo=False
    )
    assert rejected["error_code"] == "VPN_AUTO_EXCLUSIVE_SOURCE"
    monkeypatch.setattr(
        server_preferences,
        "_maybe_reselect_vpn_auto_after_membership_change",
        lambda **_kwargs: {"ok": True, "triggered": False},
    )
    global_update = server_preferences.update_server_preferences(
        "auto-off", global_list=False, reconcile_mihomo=False
    )
    assert global_update["ok"] is True


def test_bulk_auto_replace_preserves_outside_source_preferences(monkeypatch, tmp_path: Path) -> None:
    _setup(monkeypatch, tmp_path)
    source = _source_id("https://one.example/sub")
    _seed_server("inside", vpn_auto=True)
    _seed_server("outside", vpn_auto=True)
    _membership(source, "inside")
    _set_exclusive(source)

    from fwrouter_api.services import server_preferences

    monkeypatch.setattr(
        server_preferences,
        "_maybe_reselect_vpn_auto_after_membership_change",
        lambda **_kwargs: {"ok": True, "triggered": False},
    )
    result = server_preferences.replace_vpn_auto_servers(["inside"], reconcile_mihomo=False)
    assert result["ok"] is True
    with db_session() as connection:
        prefs = {
            row["server_id"]: bool(row["vpn_auto"])
            for row in connection.execute("SELECT server_id,vpn_auto FROM server_preferences")
        }
    assert prefs == {"inside": True, "outside": True}
    rejected = server_preferences.replace_vpn_auto_servers(["outside"], reconcile_mihomo=False)
    assert rejected["error_code"] == "VPN_AUTO_EXCLUSIVE_SOURCE"


def test_startup_selector_restore_skips_persisted_target_outside_exclusive_pool(monkeypatch, tmp_path: Path) -> None:
    _setup(monkeypatch, tmp_path)
    source = _source_id("https://selected.example/sub")
    _seed_server("persisted-outside")
    _seed_server("inside")
    _membership(source, "inside")
    _set_exclusive(source)
    with db_session() as connection:
        connection.execute(
            "UPDATE routing_global_state SET server_mode='auto',active_auto_server_id='persisted-outside' WHERE id=1"
        )

    calls: list[tuple[str, str]] = []

    class _RuntimeOps:
        def list_servers(self):
            return [SimpleNamespace(server_id="persisted-outside"), SimpleNamespace(server_id="inside")]

        def apply_server_to_selector(self, group, target):
            calls.append((group, target))
            return SimpleNamespace(ok=True, to_dict=lambda: {"ok": True})

    monkeypatch.setattr(
        "fwrouter_api.services.selector._active_selector_runtime",
        lambda: ({"adapter_id": "mihomo", "capabilities": ["health", "list_servers", "apply_selector"]}, _RuntimeOps()),
    )
    monkeypatch.setattr(
        "fwrouter_api.services.selector._runtime_health_or_error",
        lambda *_args: (SimpleNamespace(runtime_state=SimpleNamespace(value="running")), None),
    )
    from fwrouter_api.services.selector import restore_mihomo_selector_state

    result = restore_mihomo_selector_state(requested_by="pytest")
    assert result["vpn_auto_restore"]["skip_reason"] == "active_auto_server_outside_current_pool"
    assert calls == [("vpn-global", "vpn-auto")]


def test_provider_recovery_and_reentry_do_not_cross_exclusive_source(monkeypatch, tmp_path: Path) -> None:
    _setup(monkeypatch, tmp_path)
    source_a = _source_id("https://provider-a.example/sub")
    source_b = _source_id("https://provider-b.example/sub")
    _seed_server("provider-a-root")
    _seed_server("provider-b-root")
    with db_session() as connection:
        for source_ref, root in ((source_a, "provider-a-root"), (source_b, "provider-b-root")):
            connection.execute(
                """INSERT INTO provider_bindings(source_ref,provider_id,resource_id,logical_server_id,protocol,enabled,updated_at)
                   VALUES(?,'pytest','resource',?,'hysteria2',1,0)""",
                (source_ref, root),
            )
    _set_exclusive(source_b)
    calls = {"execute": 0, "apply": 0, "probe": 0}
    monkeypatch.setattr(
        "fwrouter_api.services.provider_managed.binding_for_logical",
        lambda _logical_id: {"source_ref": source_a, "binding_revision": 1},
    )
    monkeypatch.setattr(
        "fwrouter_api.services.provider_managed.execute_provider_operation",
        lambda *_args, **_kwargs: calls.__setitem__("execute", calls["execute"] + 1),
    )
    monkeypatch.setattr(
        "fwrouter_api.services.provider_recovery.get_recovery_pending",
        lambda: {"source_ref": source_a, "logical_server_id": "provider-a-root", "provider_managed": True, "emergency_direct": True, "phase": "emergency_direct"},
    )
    monkeypatch.setattr(
        "fwrouter_api.services.provider_recovery._apply_override",
        lambda **_kwargs: calls.__setitem__("apply", calls["apply"] + 1),
    )

    from fwrouter_api.services.provider_recovery import confirmed_provider_recovery, try_verified_reentry

    recovered = confirmed_provider_recovery(
        logical_server_id="provider-a-root", path_key="path", decision_id="d1",
        controller=SimpleNamespace(), timeout_ms=1000, allow_switch=True,
    )
    reentry = try_verified_reentry(
        SimpleNamespace(probe=lambda **_kwargs: calls.__setitem__("probe", calls["probe"] + 1)),
        timeout_ms=1000, reason="pytest",
    )
    assert recovered["status"] == "provider_recovery_suppressed_by_exclusive_source"
    assert reentry["status"] == "provider_reentry_suppressed_by_exclusive_source"
    assert reentry["pending_preserved"] is True
    assert calls == {"execute": 0, "apply": 0, "probe": 0}


def test_exclusive_selector_readback_failure_does_not_restore_now_ineligible_server(monkeypatch, tmp_path: Path) -> None:
    _setup(monkeypatch, tmp_path)
    source = _source_id("https://selected.example/sub")
    _seed_server("inside")
    _seed_server("outside")
    _membership(source, "inside")
    _set_exclusive(source)
    with db_session() as connection:
        connection.execute("UPDATE routing_global_state SET active_auto_server_id='outside' WHERE id=1")
        connection.execute(
            "INSERT INTO settings(key,value_json) VALUES('routing.auto_selection_provenance',?)",
            (json.dumps({"selected_server_id": "outside", "reason_code": "manual"}),),
        )

    exact_results = iter([(True, {"target_set_matches": True}), (False, {"target_set_matches": False})])
    monkeypatch.setattr(
        "fwrouter_api.services.subscription_vpn_auto_exclusive_job._exact_auto_targets",
        lambda: next(exact_results),
    )
    def _select(**_kwargs):
        with db_session() as connection:
            connection.execute("UPDATE routing_global_state SET active_auto_server_id='inside' WHERE id=1")
            connection.execute(
                "UPDATE settings SET value_json=? WHERE key='routing.auto_selection_provenance'",
                (json.dumps({"selected_server_id": "inside", "reason_code": "automatic_selection"}),),
            )
        return {"ok": True, "selected_server_id": "inside"}
    monkeypatch.setattr("fwrouter_api.services.selector.select_vpn_auto_server", _select)

    from fwrouter_api.services.subscription_vpn_auto_exclusive_job import _runtime_verified_callback
    from fwrouter_api.services.vpn_auto_selection_state import read_selection_revision
    with db_session() as connection:
        expected_revision = read_selection_revision(connection)

    result = _runtime_verified_callback(
        enabled=True, source_ref=source, operation_id="exclusive-test",
        expected_selection_revision=expected_revision,
    )
    assert result["ok"] is False
    assert result["selection_state_restored"] is False
    with db_session() as connection:
        routing = connection.execute("SELECT active_auto_server_id FROM routing_global_state WHERE id=1").fetchone()
        provenance = connection.execute("SELECT value_json FROM settings WHERE key='routing.auto_selection_provenance'").fetchone()
    assert routing["active_auto_server_id"] == "inside"
    assert json.loads(provenance["value_json"])["selected_server_id"] == "inside"
    assert get_vpn_auto_exclusive_source_ref() == source


def test_selector_exception_after_mutation_does_not_restore_now_ineligible_server(monkeypatch, tmp_path: Path) -> None:
    _setup(monkeypatch, tmp_path)
    source = _source_id("https://selected.example/sub")
    _seed_server("inside")
    _seed_server("outside")
    _membership(source, "inside")
    _set_exclusive(source)
    with db_session() as connection:
        connection.execute("UPDATE routing_global_state SET active_auto_server_id='outside' WHERE id=1")
        connection.execute(
            "INSERT INTO settings(key,value_json) VALUES('routing.auto_selection_provenance',?)",
            (json.dumps({"selected_server_id": "outside", "reason_code": "manual"}),),
        )

    monkeypatch.setattr(
        "fwrouter_api.services.subscription_vpn_auto_exclusive_job._exact_auto_targets",
        lambda: (True, {"target_set_matches": True}),
    )

    def _select_then_raise(**_kwargs):
        with db_session() as connection:
            connection.execute("UPDATE routing_global_state SET active_auto_server_id='inside' WHERE id=1")
            connection.execute(
                "UPDATE settings SET value_json=? WHERE key='routing.auto_selection_provenance'",
                (json.dumps({"selected_server_id": "inside", "reason_code": "automatic_selection"}),),
            )
        raise RuntimeError("synthetic readback failure after selector mutation")

    monkeypatch.setattr("fwrouter_api.services.selector.select_vpn_auto_server", _select_then_raise)

    from fwrouter_api.services.subscription_vpn_auto_exclusive_job import _runtime_verified_callback
    from fwrouter_api.services.vpn_auto_selection_state import read_selection_revision
    with db_session() as connection:
        expected_revision = read_selection_revision(connection)

    result = _runtime_verified_callback(
        enabled=True, source_ref=source, operation_id="exclusive-test",
        expected_selection_revision=expected_revision,
    )
    assert result["ok"] is False
    assert result["error_code"] == "VPN_AUTO_EXCLUSIVE_SELECTION_READBACK_FAILED"
    assert result["selection_state_restored"] is False
    with db_session() as connection:
        routing = connection.execute("SELECT active_auto_server_id FROM routing_global_state WHERE id=1").fetchone()
        provenance = connection.execute("SELECT value_json FROM settings WHERE key='routing.auto_selection_provenance'").fetchone()
    assert routing["active_auto_server_id"] == "inside"
    assert json.loads(provenance["value_json"])["selected_server_id"] == "inside"


def test_job_enable_applies_pool_and_exact_selector_readback_without_provider_calls(monkeypatch, tmp_path: Path) -> None:
    _setup(monkeypatch, tmp_path)
    source = _source_id("https://selected.example/sub")
    _seed_server("inside")
    _membership(source, "inside")
    monkeypatch.setattr(
        "fwrouter_api.services.subscription_vpn_auto_exclusive_job.validate_exclusive_source",
        lambda ref: {"ok": True, "source_ref": ref},
    )
    monkeypatch.setattr(
        "fwrouter_api.services.vpn_auto_exclusive.validate_exclusive_source",
        lambda ref: {"ok": True, "source_ref": ref},
    )
    monkeypatch.setattr(
        "fwrouter_api.services.jobs.update_job_running_result",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "fwrouter_api.services.subscription_vpn_auto_exclusive_job._exact_auto_targets",
        lambda: (True, {"target_set_matches": True}),
    )
    selector_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        "fwrouter_api.services.selector.select_vpn_auto_server",
        lambda **kwargs: selector_calls.append(kwargs) or {"ok": True, "selected_server_id": "inside", "selector_readback_matches": True},
    )
    monkeypatch.setattr(
        "fwrouter_api.services.provider_managed.execute_provider_operation",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("provider API must not be called")),
    )
    cache_clears: list[bool] = []
    monkeypatch.setattr(
        "fwrouter_api.services.live_probe_cache.clear_live_probe_cache",
        lambda: cache_clears.append(True),
    )

    def _reconcile(**kwargs):
        from fwrouter_api.services.vpn_auto_selection_state import read_selection_revision

        with db_session() as connection:
            expected_revision = read_selection_revision(connection)
        verification = kwargs["verification_callback"](
            operation_id="exclusive-enable",
            expected_selection_revision=expected_revision,
        )
        return {
            "ok": bool(verification.get("ok")),
            "verification_callback_result": verification,
            "last_good_retained": True,
            "reconcile_action": "pytest",
        }

    monkeypatch.setattr("fwrouter_api.services.mihomo_config.reconcile_mihomo_runtime", _reconcile)
    from fwrouter_api.services.subscription_vpn_auto_exclusive_job import run_vpn_auto_exclusive_job

    result = run_vpn_auto_exclusive_job({
        "job_id": "exclusive-enable",
        "input": {"source_ref": source, "enabled": True},
    })
    assert result["job_status"] == "success"
    assert result["runtime_verified"] is True
    assert get_vpn_auto_exclusive_source_ref() == source
    assert selector_calls and selector_calls[0]["allow_provider_fallback"] is False
    assert cache_clears == [True]


@pytest.mark.parametrize("reconcile_result", [
    {"ok": False, "stage": "verification", "last_good_retained": True},
    {"ok": False, "reconcile_reason": "validation_failed", "promoted": {"ok": False, "promoted": False}},
])
def test_apply_failure_keeps_exclusive_intent_and_last_good_and_source_delete_is_blocked(monkeypatch, tmp_path: Path, reconcile_result: dict) -> None:
    _setup(monkeypatch, tmp_path)
    source = _source_id("https://selected.example/sub")
    _seed_server("inside")
    _membership(source, "inside")
    _set_exclusive(source)
    monkeypatch.setattr(
        "fwrouter_api.services.subscription_vpn_auto_exclusive_job.validate_exclusive_source",
        lambda ref: {"ok": True, "source_ref": ref},
    )
    monkeypatch.setattr(
        "fwrouter_api.services.vpn_auto_exclusive.validate_exclusive_source",
        lambda ref: {"ok": True, "source_ref": ref},
    )
    monkeypatch.setattr("fwrouter_api.services.jobs.update_job_running_result", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "fwrouter_api.services.mihomo_config.reconcile_mihomo_runtime",
        lambda **_kwargs: reconcile_result,
    )
    from fwrouter_api.services.subscription_vpn_auto_exclusive_job import run_vpn_auto_exclusive_job

    failed = run_vpn_auto_exclusive_job({
        "job_id": "exclusive-failed",
        "input": {"source_ref": source, "enabled": True},
    })
    assert failed["job_status"] == "failed"
    assert failed["intent_saved"] is True
    assert failed["last_good_retained"] is True
    assert get_vpn_auto_exclusive_source_ref() == source

    from fwrouter_api.services.subscription_refresh_job import run_subscription_source_delete_job
    monkeypatch.setattr("fwrouter_api.services.jobs.update_job_running_result", lambda *_a, **_k: None)
    deleted = run_subscription_source_delete_job({
        "job_id": "exclusive-delete",
        "input": {"source_ref": source},
    })
    assert deleted["job_status"] == "failed"
    assert deleted["error_code"] == "SUBSCRIPTION_SOURCE_IS_VPN_AUTO_EXCLUSIVE"
    assert get_vpn_auto_exclusive_source_ref() == source


def test_stale_disable_is_noop_and_fingerprint_tracks_singleton(monkeypatch, tmp_path: Path) -> None:
    _setup(monkeypatch, tmp_path)
    first = _source_id("https://one.example/sub")
    second = _source_id("https://two.example/sub")
    _set_exclusive(second)
    monkeypatch.setattr("fwrouter_api.services.jobs.update_job_running_result", lambda *_a, **_k: None)
    from fwrouter_api.services.subscription_vpn_auto_exclusive_job import run_vpn_auto_exclusive_job
    from fwrouter_api.services.mihomo_reconcile_fingerprint import current_mihomo_input_fingerprint

    before = current_mihomo_input_fingerprint()["hash"]
    stale = run_vpn_auto_exclusive_job({
        "job_id": "stale-disable",
        "input": {"source_ref": first, "enabled": False},
    })
    assert stale["changed"] is False
    assert stale["runtime_verified"] is None
    assert get_vpn_auto_exclusive_source_ref() == second
    _set_exclusive(first)
    after = current_mihomo_input_fingerprint()["hash"]
    assert before != after


def test_provider_phase_three_emergency_direct_preserves_exclusive_intent(monkeypatch, tmp_path: Path) -> None:
    _setup(monkeypatch, tmp_path)
    source = _source_id("https://provider.example/sub")
    _seed_server("provider-root")
    with db_session() as connection:
        connection.execute(
            """INSERT INTO provider_bindings(source_ref,provider_id,resource_id,logical_server_id,protocol,enabled,updated_at)
               VALUES(?,'pytest','resource','provider-root','hysteria2',1,0)""",
            (source,),
        )
    _set_exclusive(source)
    pending = {
        "provider_managed": True,
        "source_ref": source,
        "binding_revision": 1,
        "logical_server_id": "provider-root",
        "phase": "provider_recovery",
        "provider_confirmation": 2,
        "traffic_decision_id": "previous",
    }
    writes: list[dict[str, object] | None] = []
    state = {"pending": pending}
    def get_pending():
        return state["pending"]
    def cas(expected, value):
        if state["pending"] != expected:
            return False
        state["pending"] = value
        writes.append(value)
        return True
    monkeypatch.setattr("fwrouter_api.services.provider_recovery.get_recovery_pending", get_pending)
    monkeypatch.setattr("fwrouter_api.services.provider_recovery._cas_pending", cas)
    monkeypatch.setattr("fwrouter_api.services.provider_recovery._capture_recovery_context", lambda _controller, item: {
        "pending": dict(item), "active_target_id": "provider-root",
        "selection_fence": {"revision": 0}, "pool": "pytest-pool", "runtime_incarnation": "pytest-runtime",
    })
    monkeypatch.setattr("fwrouter_api.services.provider_recovery._recovery_context_matches", lambda _controller, snapshot, *, expected_pending=None, expected_target=None: (
        state["pending"] == (expected_pending if expected_pending is not None else snapshot["pending"])
    ))
    monkeypatch.setattr(
        "fwrouter_api.services.provider_managed.binding_for_logical",
        lambda _logical: {"source_ref": source, "binding_revision": 1},
    )
    monkeypatch.setattr("fwrouter_api.services.provider_recovery._apply_override", lambda **_kwargs: {"ok": True})

    from fwrouter_api.services.provider_recovery import confirmed_provider_recovery

    result = confirmed_provider_recovery(
        logical_server_id="provider-root",
        path_key="path",
        decision_id="new-confirmation",
        controller=SimpleNamespace(probe=lambda **_kwargs: {"ok": False}),
        timeout_ms=1000,
        allow_switch=True,
    )
    assert result["status"] == "emergency_direct"
    assert result["effective_override"] == "emergency_direct"
    assert get_vpn_auto_exclusive_source_ref() == source
    assert writes and writes[-1]["emergency_direct"] is True


def test_subscription_action_uses_shared_refresh_job_lock_and_get_projection(monkeypatch, tmp_path: Path) -> None:
    _setup(monkeypatch, tmp_path)
    source = _source_id("https://selected.example/sub")
    _set_exclusive(source)
    created: list[dict[str, object]] = []

    class _Manager:
        def register_handler(self, operation, handler):
            return None

        def create(self, operation, *, lock_key, requested_by, input_data):
            created.append({
                "operation": operation,
                "lock_key": lock_key,
                "requested_by": requested_by,
                "input_data": input_data,
            })
            return {"job_id": "exclusive-api-job", "job_type": operation, "status": "queued", "input": input_data}

        def start_job(self, _job_id):
            return {"job_id": "exclusive-api-job", "job_type": created[-1]["operation"], "status": "running"}

    monkeypatch.setattr(
        "fwrouter_api.routes.subscription.get_default_job_manager",
        lambda: _Manager(),
    )
    from fwrouter_api.routes.subscription import (
        SubscriptionVpnAutoExclusiveRequest,
        get_subscription_endpoint,
        set_subscription_vpn_auto_exclusive_endpoint,
    )

    accepted = set_subscription_vpn_auto_exclusive_endpoint(
        source, SubscriptionVpnAutoExclusiveRequest(enabled=True)
    )
    assert accepted.data["accepted"] is True
    assert created[0]["lock_key"] == "subscription_refresh"
    assert created[0]["input_data"] == {"source_ref": source, "enabled": True}
    projected = get_subscription_endpoint()
    assert projected.data["subscription"]["vpn_auto_exclusive"] == {"source_ref": source}
