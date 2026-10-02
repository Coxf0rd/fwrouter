from __future__ import annotations

from pathlib import Path
from threading import Event, Thread

import pytest

import fwrouter_api.adapters.subscription as adapter_module
import fwrouter_api.services.subscription_pipeline as pipeline_module
from fwrouter_api.adapters.subscription import (
    SubscriptionRefreshResult,
    SubscriptionRefreshStatus,
    SubscriptionServer,
)
from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import initialize_database
from fwrouter_api.services import subscription as subscription_service
from fwrouter_api.services.server_state import ensure_routing_global_state
from fwrouter_api.services.subscription import (
    _source_id,
    compact_subscription_metadata,
    delete_subscription_source_intent,
    get_subscription_state,
    refresh_subscription_inventory_batch,
)


class _Adapter:
    def __init__(self, payloads: dict[str, tuple[str, ...]]) -> None:
        self.payloads = payloads

    def refresh(self, url: str) -> SubscriptionRefreshResult:
        names = self.payloads[url]
        return SubscriptionRefreshResult(
            status=SubscriptionRefreshStatus.SUCCESS,
            servers=[SubscriptionServer(server_id=n, server_name=n, provider_name="subscription", raw={"name": n, "type": "vless"}) for n in names],
            message="ok", metadata={"url": url},
        )


def _setup(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FWROUTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("FWROUTER_MAINTENANCE_SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("FWROUTER_WATCHDOG_SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("FWROUTER_RUNTIME_CONVERGENCE_SCHEDULER_ENABLED", "false")
    get_settings.cache_clear()
    initialize_database()
    ensure_routing_global_state()


def _import(monkeypatch, sources: dict[str, tuple[str, ...]]) -> None:
    monkeypatch.setattr(adapter_module, "DEFAULT_SUBSCRIPTION_ADAPTER", _Adapter(sources))
    refresh_subscription_inventory_batch(list(sources))


def test_source_delete_deactivates_only_exact_source_and_preserves_shared_server(monkeypatch, tmp_path: Path) -> None:
    _setup(monkeypatch, tmp_path)
    first, second = "https://one.example/sub?token=secret", "https://two.example/sub"
    _import(monkeypatch, {first: ("orphan", "shared"), second: ("shared", "other")})
    with subscription_service.db_session() as connection:
        connection.execute(
            "UPDATE subscription_server_memberships SET is_active=0 WHERE source_id=? AND server_id='orphan'",
            (_source_id(first),),
        )

    result = delete_subscription_source_intent(_source_id(first))

    assert result["ok"] is True
    assert result["remaining_source_refs"] == [_source_id(second)]
    state = get_subscription_state()
    assert state["url"] == second
    assert all(_source_id(first) != item.get("source_ref") for item in compact_subscription_metadata(state["metadata"], redact_urls=True)["subscriptions"]["items"])
    with subscription_service.db_session() as connection:
        memberships = connection.execute("SELECT server_id, is_active, source_url FROM subscription_server_memberships WHERE source_id=?", (_source_id(first),)).fetchall()
        inventory = {row["server_id"]: row["inventory_state"] for row in connection.execute("SELECT server_id, inventory_state FROM servers")}
    assert {row["server_id"] for row in memberships if not row["is_active"]} == {"orphan", "shared"}
    assert all(row["source_url"] == f"deleted:{_source_id(first)}" for row in memberships)
    assert inventory == {"orphan": "missing", "shared": "active", "other": "active"}


def test_source_delete_last_source_never_keeps_its_last_good_snapshot(monkeypatch, tmp_path: Path) -> None:
    _setup(monkeypatch, tmp_path)
    url = "https://one.example/sub"
    _import(monkeypatch, {url: ("only",)})
    before = get_subscription_state()
    result = delete_subscription_source_intent(_source_id(url))

    assert result["ok"] is True
    state = get_subscription_state()
    assert state["url"] is None
    assert state["metadata"]["subscriptions"]["items"] == []
    assert state["last_success_at"] == before["last_success_at"]
    assert state["last_refresh_at"] == before["last_refresh_at"]
    assert result["remaining_source_refs"] == []
    with subscription_service.db_session() as connection:
        row = connection.execute("SELECT inventory_state FROM servers WHERE server_id='only'").fetchone()
        membership = connection.execute("SELECT is_active FROM subscription_server_memberships WHERE source_id=?", (_source_id(url),)).fetchone()
    assert row["inventory_state"] == "missing"
    assert membership["is_active"] == 0


def test_source_delete_selector_probe_is_outside_guard_and_stale_snapshot_preserves_source(monkeypatch, tmp_path: Path) -> None:
    _setup(monkeypatch, tmp_path)
    url = "https://one.example/sub"
    _import(monkeypatch, {url: ("only",)})
    with subscription_service.db_session() as connection:
        connection.execute("UPDATE routing_global_state SET server_mode='auto', active_auto_server_id='only' WHERE id=1")

    probe_guard_states: list[bool] = []
    def stale_probe(**_kwargs):
        from fwrouter_api.adapters.xray_common import xray_writer_guard_is_held
        probe_guard_states.append(xray_writer_guard_is_held())
        from fwrouter_api.services.vpn_auto_selection_state import advance_selection_revision
        with subscription_service.db_session() as connection:
            advance_selection_revision(connection)
        return {"auto_selectable_candidate_ids": ["only", "other"]}

    monkeypatch.setattr("fwrouter_api.services.selector.get_vpn_auto_state", stale_probe)
    result = delete_subscription_source_intent(_source_id(url))

    assert probe_guard_states == [False]
    assert result["ok"] is False
    assert result["error_code"] == "VPN_AUTO_SELECTION_SNAPSHOT_STALE"
    assert get_subscription_state()["url"] == url


def test_source_delete_remaining_inventory_rejects_superseded_metadata_snapshot(monkeypatch, tmp_path: Path) -> None:
    _setup(monkeypatch, tmp_path)
    url = "https://one.example/sub"
    _import(monkeypatch, {url: ("only",)})
    state_snapshot = get_subscription_state()
    from fwrouter_api.services.subscription import _upsert_subscription_servers, subscription_state_fingerprint
    from fwrouter_api.services.vpn_auto_selection_state import read_selection_revision
    with subscription_service.db_session() as connection:
        expected_revision = read_selection_revision(connection)
        connection.execute(
            "UPDATE subscription_state SET metadata_json='{}', updated_at='later' WHERE id=1"
        )

    with pytest.raises(RuntimeError, match="SUBSCRIPTION_STATE_SNAPSHOT_STALE"):
        _upsert_subscription_servers(
            [SubscriptionServer(server_id="stale", server_name="stale", provider_name="subscription", raw={})],
            expected_selection_revision=expected_revision,
            expected_subscription_state_fingerprint=subscription_state_fingerprint(state_snapshot),
        )
    with subscription_service.db_session() as connection:
        assert connection.execute("SELECT 1 FROM servers WHERE server_id='stale'").fetchone() is None


def test_source_delete_preserves_custom_local_ownership(monkeypatch, tmp_path: Path) -> None:
    _setup(monkeypatch, tmp_path)
    url = "https://one.example/sub"
    _import(monkeypatch, {url: ("custom-owned",)})
    with subscription_service.db_session() as connection:
        connection.execute("INSERT INTO server_custom_https_proxy (server_id, host, port) VALUES ('custom-owned', 'proxy.example', 443)")
    result = delete_subscription_source_intent(_source_id(url))
    assert result["ok"] is True
    assert result["orphaned_server_ids"] == []
    with subscription_service.db_session() as connection:
        row = connection.execute("SELECT inventory_state FROM servers WHERE server_id='custom-owned'").fetchone()
    assert row["inventory_state"] == "active"


def test_unknown_and_repeated_source_refs_fail_without_mutation(monkeypatch, tmp_path: Path) -> None:
    _setup(monkeypatch, tmp_path)
    url = "https://one.example/sub"
    _import(monkeypatch, {url: ("only",)})
    unknown = delete_subscription_source_intent(_source_id("https://unknown.example/sub"))
    assert unknown["ok"] is False
    assert unknown["error_code"] == "SUBSCRIPTION_SOURCE_NOT_FOUND"
    assert delete_subscription_source_intent(_source_id(url))["ok"] is True
    repeated = delete_subscription_source_intent(_source_id(url))
    assert repeated["ok"] is False
    assert repeated["error_code"] == "SUBSCRIPTION_SOURCE_NOT_FOUND"


def test_source_delete_refuses_current_fixed_target_before_intent_change(monkeypatch, tmp_path: Path) -> None:
    _setup(monkeypatch, tmp_path)
    url = "https://one.example/sub"
    _import(monkeypatch, {url: ("fixed",)})
    with subscription_service.db_session() as connection:
        connection.execute("UPDATE routing_global_state SET server_mode='fixed', desired_fixed_server_id='fixed' WHERE id=1")
    result = delete_subscription_source_intent(_source_id(url))
    assert result["ok"] is False
    assert result["error_code"] == "SUBSCRIPTION_DELETE_CURRENT_FIXED_SERVER"
    assert get_subscription_state()["url"] == url


def test_source_delete_refuses_current_auto_without_alternative(monkeypatch, tmp_path: Path) -> None:
    _setup(monkeypatch, tmp_path)
    url = "https://one.example/sub"
    _import(monkeypatch, {url: ("current",)})
    with subscription_service.db_session() as connection:
        connection.execute("UPDATE routing_global_state SET server_mode='auto', active_auto_server_id='current' WHERE id=1")
    monkeypatch.setattr("fwrouter_api.services.selector.get_vpn_auto_state", lambda **_kwargs: {"auto_selectable_candidate_ids": ["current"]})
    result = delete_subscription_source_intent(_source_id(url))
    assert result["ok"] is False
    assert result["error_code"] == "SUBSCRIPTION_DELETE_NO_AUTO_ALTERNATIVE"
    assert get_subscription_state()["url"] == url


def test_source_delete_allows_preflight_when_another_eligible_auto_target_exists(monkeypatch, tmp_path: Path) -> None:
    _setup(monkeypatch, tmp_path)
    url = "https://one.example/sub"
    _import(monkeypatch, {url: ("current",)})
    with subscription_service.db_session() as connection:
        connection.execute("UPDATE routing_global_state SET server_mode='auto', active_auto_server_id='current' WHERE id=1")
    monkeypatch.setattr("fwrouter_api.services.selector.get_vpn_auto_state", lambda **_kwargs: {"auto_selectable_candidate_ids": ["current", "alternative"]})

    result = delete_subscription_source_intent(_source_id(url))

    assert result["ok"] is True
    assert result["current_auto_server_id"] == "current"


def test_source_delete_apply_failure_is_partial_and_never_exposes_source_url(monkeypatch, tmp_path: Path) -> None:
    _setup(monkeypatch, tmp_path)
    url = "https://one.example/sub?token=private"
    _import(monkeypatch, {url: ("only",)})
    monkeypatch.setattr("fwrouter_api.services.jobs.update_job_running_result", lambda *_args, **_kwargs: None)
    apply_guard_state: list[bool] = []
    def apply_without_outer_guard(_result):
        from fwrouter_api.adapters.xray_common import xray_writer_guard_is_held
        apply_guard_state.append(xray_writer_guard_is_held())
        return {"ok": False, "stage": "apply_runtime", "error": {"code": "MIHOMO_APPLY_FAILED", "message": "candidate failed"}}
    monkeypatch.setattr(
        "fwrouter_api.services.subscription_refresh_job.apply_subscription_import_result",
        apply_without_outer_guard,
    )

    from fwrouter_api.services.subscription_refresh_job import run_subscription_source_delete_job
    result = run_subscription_source_delete_job({"job_id": "test-delete", "input": {"source_ref": _source_id(url)}})

    assert result["job_status"] == "failed"
    assert result["reconcile_pending"] is True
    assert result["source"] == {"source_ref": _source_id(url), "deleted": True}
    assert apply_guard_state == [False]
    assert "token=private" not in repr(result)
    state = get_subscription_state()
    assert state["url"] is None
    assert state["metadata"]["subscriptions"]["items"] == []


def test_source_delete_reselects_auto_and_verifies_logical_and_effective_target(monkeypatch, tmp_path: Path) -> None:
    _setup(monkeypatch, tmp_path)
    removed_url, remaining_url = "https://one.example/sub", "https://two.example/sub"
    _import(monkeypatch, {removed_url: ("current",), remaining_url: ("alternative",)})
    with subscription_service.db_session() as connection:
        connection.execute("UPDATE routing_global_state SET server_mode='auto', active_auto_server_id='current' WHERE id=1")

    selector_reads = {"count": 0}
    def read_selector(**_kwargs):
        selector_reads["count"] += 1
        if selector_reads["count"] == 1:  # preflight before intent mutation
            return {"auto_selectable_candidate_ids": ["current", "alternative"]}
        return {
            "active_auto_server_id": "alternative",
            "active_auto_server_valid": True,
            "auto_selectable_candidate_ids": ["alternative"],
            "auto_selectable_candidate_target_names": ["alternative-runtime"],
            "runtime_vpn_auto_targets": ["alternative-runtime"],
            "selector_runtime": {"vpn_auto_now": "alternative-runtime"},
        }
    monkeypatch.setattr("fwrouter_api.services.selector.get_vpn_auto_state", read_selector)
    monkeypatch.setattr("fwrouter_api.services.jobs.update_job_running_result", lambda *_args, **_kwargs: None)

    selection_reads = {"count": 0}
    def pipeline_state():
        selection_reads["count"] += 1
        if selection_reads["count"] == 1:
            return {"server_mode": "auto", "auto_selectable_candidates_count": 1, "active_auto_server_id": None, "active_auto_server_valid": False}
        return {"server_mode": "auto", "auto_selectable_candidates_count": 1, "auto_selectable_candidate_ids": ["alternative"], "auto_selectable_candidate_target_names": ["alternative-runtime"], "selector_runtime": {"vpn_auto_now": "alternative-runtime"}, "active_auto_server_id": "alternative", "active_auto_server_valid": True}
    monkeypatch.setattr(pipeline_module, "get_routing_global_state", lambda: {"server_mode": "auto"})
    monkeypatch.setattr(pipeline_module, "get_vpn_auto_state", pipeline_state)
    selector_calls = []
    def select_alternative(**kwargs):
        selector_calls.append(kwargs)
        with subscription_service.db_session() as connection:
            connection.execute("UPDATE routing_global_state SET active_auto_server_id='alternative' WHERE id=1")
        return {"ok": True, "selected_server_id": "alternative"}
    monkeypatch.setattr(pipeline_module, "select_vpn_auto_server", select_alternative)
    monkeypatch.setattr(
        "fwrouter_api.services.subscription_refresh_job.apply_subscription_import_result",
        lambda _result: {"ok": pipeline_module._maybe_select_vpn_auto_after_refresh()["ok"], "stage": "verified"},
    )

    from fwrouter_api.services.subscription_refresh_job import run_subscription_source_delete_job
    result = run_subscription_source_delete_job({"job_id": "test-auto-delete", "input": {"source_ref": _source_id(removed_url)}})

    assert result["job_status"] == "success"
    assert result["runtime_verified"] is True
    assert result["selection"] == {"logical_server_id": "alternative", "effective_server_target": "alternative-runtime", "verified": True}
    assert selector_calls and selector_calls[0]["apply"] is True


def test_refresh_preparation_runs_outside_shared_writer_guard(monkeypatch, tmp_path: Path) -> None:
    _setup(monkeypatch, tmp_path)
    entered_prepare = Event()
    release_prepare = Event()
    attempted_writer = Event()
    acquired_writer = Event()

    def prepare():
        entered_prepare.set()
        assert release_prepare.wait(2)
        return {"ok": False, "stage": "download_parse"}

    monkeypatch.setattr("fwrouter_api.services.subscription_pipeline.prepare_subscription_refresh", prepare)
    refresh_thread = Thread(target=pipeline_module.apply_subscription_refresh)
    refresh_thread.start()
    assert entered_prepare.wait(1)

    def competing_writer():
        from fwrouter_api.adapters.xray_common import xray_writer_guard
        attempted_writer.set()
        with xray_writer_guard():
            acquired_writer.set()

    writer_thread = Thread(target=competing_writer)
    writer_thread.start()
    assert attempted_writer.wait(1)
    assert acquired_writer.wait(1)
    release_prepare.set()
    refresh_thread.join(2)
    writer_thread.join(2)
    assert acquired_writer.is_set()


def test_delete_endpoint_rejects_malformed_source_ref_before_job_creation(monkeypatch) -> None:
    from fwrouter_api.routes.subscription import delete_subscription_source_endpoint

    monkeypatch.setattr("fwrouter_api.routes.subscription.get_default_job_manager", lambda: (_ for _ in ()).throw(AssertionError("job manager should not be touched")))
    response = delete_subscription_source_endpoint("https://example.test/sub?token=private")
    assert response.ok is False
    assert response.error["code"] == "SUBSCRIPTION_SOURCE_REF_INVALID"
    assert "token=private" not in repr(response.model_dump())


def test_subscription_public_and_job_projections_redact_provider_urls_and_tokens() -> None:
    from fwrouter_api.routes.subscription import _redact_batch_response, _redact_subscription_state
    from fwrouter_api.services.subscription_refresh_job import _redact_subscription_refresh_result

    secret_url = "https://user:password@provider.example/list?token=private-token"
    batch_public = _redact_batch_response({
        "state": {"error_message": f"download failed: {secret_url}"},
        "batch": {"items": [{
            "url": secret_url,
            "error": {"message": f"request failed for {secret_url}"},
            "refresh": {"error_message": f"timeout at {secret_url}", "metadata": {"final_url": secret_url}},
        }]},
    })
    state_public = _redact_subscription_state({
        "url": secret_url,
        "error_message": f"download failed: {secret_url}",
        "metadata": {"subscriptions": {"items": [{"url": secret_url, "metadata": {"final_url": secret_url}}]}},
    })
    job_public = _redact_subscription_refresh_result({
        "refresh": {
            "error": {"message": f"request failed for {secret_url}"},
            "refresh": {"error_message": f"timeout at {secret_url}", "metadata": {"final_url": secret_url}},
        },
    })
    for public in (batch_public, state_public, job_public):
        rendered = repr(public)
        assert secret_url not in rendered
        assert "private-token" not in rendered
        assert "password" not in rendered
        assert "[subscription URL redacted]" in rendered
    assert batch_public["batch"]["items"][0]["refresh"]["metadata"]["final_url"] == "[REDACTED]"
    assert state_public["metadata"]["subscriptions"]["items"][0]["metadata"]["final_url"] == "[REDACTED]"
