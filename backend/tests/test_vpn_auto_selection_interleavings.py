from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace

from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import db_session, initialize_database
from fwrouter_api.services import selector
from fwrouter_api.services.vpn_auto_selection_state import (
    commit_active_selection,
    read_selection_revision,
)
from fwrouter_api.services.servers import ensure_routing_global_state


def _setup(monkeypatch, tmp_path):
    monkeypatch.setenv("FWROUTER_STATE_DIR", str(tmp_path / "state"))
    get_settings.cache_clear()
    initialize_database()
    for server_id in ("srv-1", "srv-2"):
        with db_session() as connection:
            connection.execute(
                "INSERT INTO servers (server_id, server_name, provider_name, inventory_state, raw_json) "
                "VALUES (?, ?, 'test', 'active', ?) ",
                (server_id, server_id, json.dumps({"name": server_id})),
            )
            connection.execute(
                "INSERT INTO server_preferences (server_id, vpn_auto, vpn_auto_priority) VALUES (?, 1, 1)",
                (server_id,),
            )
            connection.execute(
                "INSERT INTO server_ping_state (server_id, status, last_ping_ms) VALUES (?, 'success', 20)",
                (server_id,),
            )
    ensure_routing_global_state()
    with db_session() as connection:
        connection.execute(
            "UPDATE routing_global_state SET desired_mode='vpn', applied_mode='vpn', server_mode='auto' WHERE id=1"
        )


def _runtime(monkeypatch, *, active="srv-1", adapter_id="interleaving-test", apply_override=None, incarnation=True):
    state = {"active": active, "apply_calls": []}

    def apply_server(target):
        state["apply_calls"].append(target)
        result = apply_override(target, state) if apply_override else None
        if result is not None:
            return result
        state["active"] = target
        return SimpleNamespace(
            ok=True,
            active_server_id=target,
            details={"selector_after": target},
            to_dict=lambda: {"ok": True, "active_server_id": target},
        )

    operations = SimpleNamespace(
        health=lambda: SimpleNamespace(
            active_server_id=state["active"],
            runtime_state=SimpleNamespace(value="running"),
            details={"selectors": {"vpn_auto_now": state["active"], "vpn_auto_targets": ["srv-1", "srv-2"]}},
        ),
        list_servers=lambda: [SimpleNamespace(server_id="srv-1"), SimpleNamespace(server_id="srv-2")],
        apply_server=apply_server,
    )
    if incarnation:
        operations.runtime_incarnation = lambda: "test-process|started-at-1"
    monkeypatch.setattr(
        selector,
        "_active_selector_runtime",
        lambda: (
            {
                "adapter_id": adapter_id,
                "capabilities": {"health", "list_servers", "apply_server"},
            },
            operations,
        ),
    )
    monkeypatch.setattr(
        selector,
        "check_server_delays",
        lambda server_ids, **_kwargs: [
            {
                "status": "success",
                "last_ping_ms": 10,
                "error_code": None,
                "error_message": None,
                "latency_label": "ok",
                "updated_state": False,
            }
            for _server_id in server_ids
        ],
    )
    return state


def _active_id() -> str | None:
    with db_session() as connection:
        row = connection.execute("SELECT active_auto_server_id FROM routing_global_state WHERE id=1").fetchone()
    return str(row["active_auto_server_id"] or "") or None


def _revision() -> int:
    with db_session() as connection:
        return read_selection_revision(connection)


def test_same_snapshot_probe_barrier_only_one_operation_applies(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    state = _runtime(monkeypatch)
    barrier = Barrier(2)

    def simultaneous_probe(server_ids, **_kwargs):
        barrier.wait(timeout=5)
        return [
            {"status": "success", "last_ping_ms": 10, "error_code": None,
             "error_message": None, "latency_label": "ok", "updated_state": False}
            for _ in server_ids
        ]

    monkeypatch.setattr(selector, "check_server_delays", simultaneous_probe)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                selector.select_vpn_auto_server,
                apply=True,
                candidate_server_id="srv-2",
                exclude_active=True,
                post_check=False,
            )
            for _ in range(2)
        ]
        results = [future.result(timeout=10) for future in futures]

    assert state["apply_calls"] == ["srv-2"]
    assert sum(result.get("selection_outcome") == "selected" for result in results) == 1
    assert sum(result.get("selection_outcome") == "deferred" for result in results) == 1
    assert _active_id() == "srv-2"


def test_runtime_apply_failure_does_not_confirm_persistent_selection(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)

    def failed_apply(target, state):
        return SimpleNamespace(
            ok=False,
            details={"selector_after": "srv-1"},
            to_dict=lambda: {"ok": False, "error": "injected apply failure"},
        )

    _runtime(monkeypatch, apply_override=failed_apply)
    before = _revision()

    result = selector.select_vpn_auto_server(
        apply=True, candidate_server_id="srv-2", exclude_active=True, post_check=False
    )

    assert result["selection_outcome"] == "failed"
    assert result["applied"] is False
    assert _active_id() is None
    assert _revision() == before


def test_exact_selector_readback_mismatch_does_not_confirm(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)

    def mismatched_apply(_target, state):
        state["active"] = "srv-1"
        return SimpleNamespace(
            ok=True,
            details={"selector_after": "srv-1"},
            to_dict=lambda: {"ok": True},
        )

    _runtime(monkeypatch, apply_override=mismatched_apply)
    before = _revision()

    result = selector.select_vpn_auto_server(
        apply=True, candidate_server_id="srv-2", exclude_active=True, post_check=False
    )

    assert result["applied"] is True
    assert result["selector_readback_matches"] is False
    assert result["selection_outcome"] == "unconfirmed"
    assert _active_id() is None
    assert _revision() == before


def test_cas_miss_after_put_is_reconciled_from_runtime_without_second_put(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    state = _runtime(monkeypatch)
    real_commit = selector.commit_active_selection
    commit_calls = 0

    def fail_first_cas(*args, **kwargs):
        nonlocal commit_calls
        commit_calls += 1
        if commit_calls == 1:
            return None
        return real_commit(*args, **kwargs)

    monkeypatch.setattr(selector, "commit_active_selection", fail_first_cas)
    first = selector.select_vpn_auto_server(
        apply=True, candidate_server_id="srv-2", exclude_active=True, post_check=False
    )

    assert first["error_code"] == "VPN_AUTO_SELECTION_PERSISTENCE_CAS_FAILED"
    assert first["selection_outcome"] == "unconfirmed"
    assert state["apply_calls"] == ["srv-2"]
    assert _active_id() is None

    repaired = selector.select_vpn_auto_server(
        apply=True, candidate_server_id="srv-2", post_check=False
    )

    assert repaired["selection_outcome"] == "noop"
    assert repaired["canonical_state_repaired"] is True
    assert repaired["active_after"] == "srv-2"
    assert state["apply_calls"] == ["srv-2"]
    assert _active_id() == "srv-2"
    assert _revision() == 1


def test_noop_and_dry_run_telemetry_do_not_advance_selection_epoch(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    state = _runtime(monkeypatch, active="srv-1")
    with db_session() as connection:
        provenance = {
            "decision_id": "existing-decision",
            "selected_server_id": "srv-1",
        }
        assert commit_active_selection(
            connection,
            expected_revision=0,
            expected_active_server_id=None,
            expected_provenance_decision_id=None,
            server_id="srv-1",
            provenance=provenance,
        ) == 1
    before = _revision()

    dry_run = selector.select_vpn_auto_server(
        apply=False, candidate_server_id="srv-2", post_check=False
    )
    noop = selector.select_vpn_auto_server(
        apply=True, candidate_server_id="srv-1", post_check=False
    )

    assert dry_run["apply"] is False
    assert noop["selection_outcome"] == "noop"
    assert state["apply_calls"] == []
    assert _revision() == before
    with db_session() as connection:
        row = connection.execute(
            "SELECT value_json FROM settings WHERE key='routing.auto_selection_provenance'"
        ).fetchone()
    assert json.loads(row["value_json"]) == provenance


def test_old_watchdog_runtime_evidence_defers_after_put_db_cas_gap(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    state = _runtime(monkeypatch, active="srv-1")
    with db_session() as connection:
        assert commit_active_selection(
            connection,
            expected_revision=0,
            expected_active_server_id=None,
            expected_provenance_decision_id=None,
            server_id="srv-1",
            provenance={"decision_id": "decision-a", "selected_server_id": "srv-1"},
        ) == 1

    from fwrouter_api.services import vpn_runtime_control
    from fwrouter_api.services.vpn_runtime_control import MihomoVpnRuntimeController

    operations = selector._active_selector_runtime()[1]
    controller = MihomoVpnRuntimeController(vpn_adapter={"adapter_id": "mihomo", "ready": True})
    monkeypatch.setattr(vpn_runtime_control, "_active_selector_runtime", lambda: ({"adapter_id": "mihomo"}, operations))
    monkeypatch.setattr(vpn_runtime_control, "_runtime_health_or_error", lambda _adapter, ops: (ops.health(), None))
    controller.capture_selection_fence()
    assert controller.selection_fence_args() == {
        "expected_selection_revision": 1,
        "expected_active_server_id": "srv-1",
        "expected_runtime_target": "srv-1",
        "expected_runtime_target_valid": True,
    }

    state["active"] = "srv-2"  # prior runtime PUT succeeded; persistence is still A
    delay_calls: list[list[str]] = []
    monkeypatch.setattr(
        selector,
        "check_server_delays",
        lambda server_ids, **_kwargs: delay_calls.append(list(server_ids)) or [],
    )
    result = controller.failover(
        apply=True,
        reason="stale-watchdog-A-after-CAS-gap",
        update_ping_state=False,
        candidate_limit=2,
        timeout_ms=100,
    )

    assert result["deferred"] is True
    assert result["selector"]["error_code"] == "VPN_AUTO_SELECTION_STALE_RUNTIME_EVIDENCE"
    assert state["apply_calls"] == []
    assert delay_calls == []
    assert _active_id() == "srv-1"


def test_missing_mihomo_incarnation_fails_closed_outside_test_environment(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    state = _runtime(monkeypatch, adapter_id="mihomo", incarnation=False)
    monkeypatch.setattr(
        "fwrouter_api.core.config.get_settings",
        lambda: SimpleNamespace(environment="production"),
    )
    before = _revision()

    result = selector.select_vpn_auto_server(
        apply=True, candidate_server_id="srv-2", exclude_active=True, post_check=False
    )

    assert result["error_code"] == "VPN_AUTO_SELECTION_RUNTIME_IDENTITY_UNAVAILABLE"
    assert result["selection_outcome"] == "deferred"
    assert result["applied"] is False
    assert state["apply_calls"] == []
    assert _active_id() is None
    assert _revision() == before
