from __future__ import annotations

import hashlib
import json
import os
import secrets
import contextvars
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from fastapi.testclient import TestClient

from fwrouter_api.db.connection import db_session
from fwrouter_api.main import create_app
from fwrouter_api.services import presentation_client_list_admission as admission
from fwrouter_api.services.ui_client_list_settings import (
    CONTRACT_KEY,
    SETTINGS_KEY,
    ClientListSettingsError,
    merge_external_visibility_delta,
    patch_show_inactive,
    read_client_list_settings,
    rebase_imported_display_settings,
    validate_imported_display_settings,
)
from fwrouter_api.services.ui_state_settings import _save_setting


def _api(monkeypatch, tmp_path):
    token = secrets.token_hex(32)
    policy_dir = tmp_path / "etc" / "fwrouter"
    policy_dir.mkdir(mode=0o700, parents=True)
    policy = policy_dir / "presentation-client-list-verifier.json"
    policy.write_text(json.dumps({
        "schema_version": 1,
        "principal": "presentation-operator",
        "scopes": ["ui.client-list.write"],
        "token_sha256": hashlib.sha256(token.encode("ascii")).hexdigest(),
        "canonical_origin": "https://router.example.test",
    }), encoding="utf-8")
    policy.chmod(0o600)
    monkeypatch.setattr(admission, "VERIFIER_PATH", policy)
    monkeypatch.setattr(admission, "_trusted_uid", os.getuid)
    return token, TestClient(
        create_app(enable_startup_tasks=False),
        client=("127.0.0.1", 45678),
        headers={"Host": "router.example.test"},
    )


def _raw_row() -> dict[str, object]:
    with db_session() as connection:
        result = connection.execute("SELECT value_json FROM settings WHERE key=?", (SETTINGS_KEY,)).fetchone()
    return json.loads(result["value_json"]) if result else {}


def _admitted_owner_write():
    from contextlib import contextmanager

    @contextmanager
    def grant():
        value = admission._Grant()
        marker = admission._GRANT.set(value)
        try:
            yield
        finally:
            value.revoke()
            admission._GRANT.reset(marker)

    return grant()


def test_conditional_client_list_patch_is_strict_and_advances_field_revision(monkeypatch, tmp_path):
    token, client = _api(monkeypatch, tmp_path)
    with client:
        before = client.get("/api/v2/ui/settings/client-list")
        assert before.status_code == 200
        resource = before.json()["data"]["client_list"]
        assert resource["show_inactive"] is False
        assert resource["provenance"] == "default"
        assert client.get("/api/v2/ui/settings/client-list",
                          headers={"If-None-Match": resource["etag"]}).status_code == 304

        denied = client.patch(
            "/api/v2/ui/settings/client-list",
            content=b'{"show_inactive":"true"}',
            headers={"Authorization": f"Bearer {token}", "Origin": "https://router.example.test",
                     "Content-Type": "application/json", "If-Match": resource["etag"]},
        )
        assert denied.status_code == 422
        duplicate = client.patch(
            "/api/v2/ui/settings/client-list",
            content=b'{"show_inactive":true,"show_inactive":false}',
            headers={"Authorization": f"Bearer {token}", "Origin": "https://router.example.test",
                     "Content-Type": "application/json", "If-Match": resource["etag"]},
        )
        assert duplicate.status_code == 400
        missing_precondition = client.patch(
            "/api/v2/ui/settings/client-list", json={"show_inactive": True},
            headers={"Authorization": f"Bearer {token}", "Origin": "https://router.example.test"},
        )
        assert missing_precondition.status_code == 428

        saved = client.patch(
            "/api/v2/ui/settings/client-list", json={"show_inactive": True},
            headers={"Authorization": f"Bearer {token}", "Origin": "https://router.example.test",
                     "If-Match": resource["etag"]},
        )
        assert saved.status_code == 200
        current = saved.json()["data"]["client_list"]
        assert current["show_inactive"] is True
        assert current["revision"] == 1
        assert current["incarnation"]
        assert saved.headers["etag"] == current["etag"]
        assert current["persistence_status"] == "persisted"
        assert current["audit_delivery_status"] == "delivered"
        stale_same_value = client.patch(
            "/api/v2/ui/settings/client-list", json={"show_inactive": True},
            headers={"Authorization": f"Bearer {token}", "Origin": "https://router.example.test",
                     "If-Match": resource["etag"]},
        )
        assert stale_same_value.status_code == 412


def test_forged_identity_wrong_scope_and_origin_do_not_authorize(monkeypatch, tmp_path):
    token, client = _api(monkeypatch, tmp_path)
    with client:
        etag = client.get("/api/v2/ui/settings/client-list").headers["etag"]
        forged = client.patch(
            "/api/v2/ui/settings/client-list", json={"show_inactive": True},
            headers={"If-Match": etag, "X-Role": "admin", "X-Requested-By": "root",
                     "X-Forwarded-For": "127.0.0.1"},
        )
        assert forged.status_code == 401
        wrong_origin = client.patch(
            "/api/v2/ui/settings/client-list", json={"show_inactive": True},
            headers={"Authorization": f"Bearer {token}", "Origin": "https://attacker.example",
                     "If-Match": etag},
        )
        assert wrong_origin.status_code == 403
        wrong_host = client.patch(
            "/api/v2/ui/settings/client-list", json={"show_inactive": True},
            headers={"Authorization": f"Bearer {token}", "Origin": "https://router.example.test",
                     "Host": "attacker.example", "If-Match": etag},
        )
        assert wrong_host.status_code == 403
        admission.VERIFIER_PATH.write_text(json.dumps({
            "schema_version": 1, "principal": "presentation-operator",
            "scopes": ["control-plane.import"],
            "token_sha256": hashlib.sha256(token.encode("ascii")).hexdigest(),
            "canonical_origin": "https://router.example.test",
        }), encoding="utf-8")
        admission.VERIFIER_PATH.chmod(0o600)
        wrong_scope = client.patch(
            "/api/v2/ui/settings/client-list", json={"show_inactive": True},
            headers={"Authorization": f"Bearer {token}", "Origin": "https://router.example.test",
                     "If-Match": etag},
        )
        assert wrong_scope.status_code == 403
        assert read_client_list_settings()["show_inactive"] is False


def test_partial_legacy_put_returns_latest_preserved_siblings(monkeypatch, tmp_path):
    token, client = _api(monkeypatch, tmp_path)
    with client:
        initial_etag = client.get("/api/v2/ui/settings/client-list").headers["etag"]
        seed = client.put("/api/v2/ui/settings/display", json={
            "hidden_subject_ids": ["lan:kept"],
            "show_internal_vless": False,
        })
        assert seed.status_code == 200
        etag = client.get("/api/v2/ui/settings/client-list").headers["etag"]
        changed = client.patch(
            "/api/v2/ui/settings/client-list", json={"show_inactive": True},
            headers={"Authorization": f"Bearer {token}", "Origin": "https://router.example.test",
                     "If-Match": etag},
        )
        assert changed.status_code == 200
        response = client.put(
            "/api/v2/ui/settings/display", json={"show_internal_vless": True},
        )
        assert response.status_code == 200
        projection = response.json()["data"]["display_settings"]
        persisted = _raw_row()
        assert projection["show_inactive"] is True
        assert projection["show_internal_vless"] is True
        assert projection["hidden_subject_ids"] == ["lan:kept"]
        assert projection["show_inactive"] == persisted["show_inactive"]
        assert projection["hidden_subject_ids"] == persisted["hidden_subject_ids"]
        assert initial_etag != changed.headers["etag"]


def test_legacy_display_put_uses_same_capability_and_stale_token_is_rejected(monkeypatch, tmp_path):
    token, client = _api(monkeypatch, tmp_path)
    with client:
        before = client.get("/api/v2/ui/settings/client-list").headers["etag"]
        denied = client.put("/api/v2/ui/settings/display", json={
            "show_inactive": True, "show_internal_vless": True,
        })
        assert denied.status_code == 401
        assert read_client_list_settings()["show_inactive"] is False
        bad_bool = client.put(
            "/api/v2/ui/settings/display", content=b'{"show_inactive":1}',
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}",
                     "Origin": "https://router.example.test", "If-Match": before},
        )
        assert bad_bool.status_code == 422
        accepted = client.put(
            "/api/v2/ui/settings/display", json={
                "show_inactive": True, "show_internal_vless": True,
            },
            headers={"Authorization": f"Bearer {token}", "Origin": "https://router.example.test",
                     "If-Match": before},
        )
        assert accepted.status_code == 200
        row = _raw_row()
        assert row["show_inactive"] is True
        assert row["show_internal_vless"] is True


def test_shared_row_writers_preserve_managed_field_and_latest_siblings(monkeypatch, tmp_path):
    import pytest

    from fwrouter_api.services.ui_client_list_settings import ClientListSettingsError

    token, client = _api(monkeypatch, tmp_path)
    with client:
        before = client.get("/api/v2/ui/settings/client-list").headers["etag"]
        written = client.patch(
            "/api/v2/ui/settings/client-list", json={"show_inactive": True},
            headers={"Authorization": f"Bearer {token}", "Origin": "https://router.example.test",
                     "If-Match": before},
        )
        managed = written.json()["data"]["client_list"]
        _save_setting(SETTINGS_KEY, {"system_visibility": {"lan": False}, "show_internal_vless": True})
        row = _raw_row()
        assert row["show_inactive"] is True
        assert row["system_visibility"]["lan"] is False
        assert row[CONTRACT_KEY]["incarnation"] == managed["incarnation"]
        assert row[CONTRACT_KEY]["revision"] == managed["revision"]

        with pytest.raises(ClientListSettingsError, match="PRESENTATION_WRITE_ADMISSION_REQUIRED"):
            _save_setting(SETTINGS_KEY, {"show_inactive": False, "show_internal_vless": False})
        unchanged = _raw_row()
        assert unchanged["show_inactive"] is True
        assert unchanged["show_internal_vless"] is True

        merge_external_visibility_delta({"docker": True})
        merge_external_visibility_delta({"host": True})
        row = _raw_row()
        assert row["show_inactive"] is True
        assert row["system_visibility"]["docker"] is True
        assert row["system_visibility"]["host"] is True
        assert row[CONTRACT_KEY]["incarnation"] == managed["incarnation"]
        assert row[CONTRACT_KEY]["revision"] == managed["revision"]


def test_two_writers_with_one_etag_have_one_fresh_commit(monkeypatch, tmp_path):
    token, client = _api(monkeypatch, tmp_path)
    with client:
        etag = client.get("/api/v2/ui/settings/client-list").headers["etag"]

        def patch(value: bool):
            return client.patch(
                "/api/v2/ui/settings/client-list", json={"show_inactive": value},
                headers={"Authorization": f"Bearer {token}", "Origin": "https://router.example.test",
                         "If-Match": etag},
            ).status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = sorted(pool.map(patch, (True, False)))
        assert statuses == [200, 412]


def test_import_metadata_is_foreign_and_rebased_only_inside_owner_transaction(monkeypatch, tmp_path):
    token, client = _api(monkeypatch, tmp_path)
    with client:
        before = client.get("/api/v2/ui/settings/client-list").headers["etag"]
        created = client.patch(
            "/api/v2/ui/settings/client-list", json={"show_inactive": True},
            headers={"Authorization": f"Bearer {token}", "Origin": "https://router.example.test",
                     "If-Match": before},
        )
        previous = created.json()["data"]["client_list"]

        foreign = {"version": 1, "incarnation": "a" * 32, "revision": 400}
        rows, imported_json = validate_imported_display_settings([{
            "key": SETTINGS_KEY,
            "value": {"show_inactive": True, CONTRACT_KEY: foreign, "show_internal_vless": True},
        }])
        assert CONTRACT_KEY not in rows[0]["value"]
        with db_session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rebase_imported_display_settings(connection, imported_json)
            connection.commit()
        restored = read_client_list_settings()
        assert restored["show_inactive"] is True
        assert restored["incarnation"] != previous["incarnation"]
        assert restored["revision"] == 1
        assert restored["etag"] != previous["etag"]


def test_import_rejects_invalid_selected_field_before_rules_file_preparation():
    from fwrouter_api.services.ui_client_list_settings import ClientListSettingsError
    import pytest

    with pytest.raises(ClientListSettingsError, match="CLIENT_LIST_IMPORT_BOOLEAN_INVALID"):
        validate_imported_display_settings([{
            "key": SETTINGS_KEY, "value": {"show_inactive": "true"},
        }])


def test_import_rejects_null_metadata_and_duplicate_settings_rows():
    import pytest

    with pytest.raises(ClientListSettingsError, match="CLIENT_LIST_IMPORT_METADATA_INVALID"):
        validate_imported_display_settings([{
            "key": SETTINGS_KEY, "value": {"show_inactive": True, CONTRACT_KEY: None},
        }])
    with pytest.raises(ClientListSettingsError, match="CLIENT_LIST_IMPORT_SETTINGS_DUPLICATE"):
        validate_imported_display_settings([
            {"key": SETTINGS_KEY, "value": {"show_inactive": True}},
            {"key": SETTINGS_KEY, "value": {"show_inactive": False}},
        ])
    with pytest.raises(ClientListSettingsError, match="CLIENT_LIST_IMPORT_SETTINGS_INVALID"):
        validate_imported_display_settings([{"key": SETTINGS_KEY, "value": None}])


def test_legacy_truthiness_is_reported_then_adopted_with_strict_boolean(monkeypatch, tmp_path):
    from fwrouter_api.services.ui_client_list_settings import _write_row

    token, client = _api(monkeypatch, tmp_path)
    with client:
        with db_session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            _write_row(connection, {"show_inactive": 1, "show_internal_vless": True})
            connection.commit()
        legacy = client.get("/api/v2/ui/settings/client-list")
        resource = legacy.json()["data"]["client_list"]
        assert resource["show_inactive"] is True
        assert resource["compatibility_status"] == "adoption_required"
        assert resource["provenance"] == "legacy_coercion"
        adopted = client.patch(
            "/api/v2/ui/settings/client-list", json={"show_inactive": True},
            headers={"Authorization": f"Bearer {token}", "Origin": "https://router.example.test",
                     "If-Match": resource["etag"]},
        )
        assert adopted.status_code == 200
        managed = adopted.json()["data"]["client_list"]
        assert managed["compatibility_status"] == "current"
        assert managed["provenance"] == "managed"
        assert managed["revision"] == 1
        assert type(_raw_row()["show_inactive"]) is bool


def test_malformed_metadata_and_revision_overflow_fail_closed(monkeypatch, tmp_path):
    from fwrouter_api.services.ui_client_list_settings import MAX_REVISION

    token, client = _api(monkeypatch, tmp_path)
    with client:
        with db_session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO settings(key,value_json) VALUES(?,?)",
                (SETTINGS_KEY, json.dumps({"show_inactive": True, CONTRACT_KEY: None})),
            )
            connection.commit()
        malformed = client.get("/api/v2/ui/settings/client-list")
        assert malformed.status_code == 409

        with db_session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            _write = {
                "show_inactive": True,
                CONTRACT_KEY: {"version": 1, "incarnation": "d" * 32, "revision": MAX_REVISION},
            }
            connection.execute(
                "UPDATE settings SET value_json=? WHERE key=?",
                (json.dumps(_write), SETTINGS_KEY),
            )
            connection.commit()
        current = client.get("/api/v2/ui/settings/client-list").json()["data"]["client_list"]
        with pytest.raises(ClientListSettingsError, match="CLIENT_LIST_REVISION_EXHAUSTED"):
            with _admitted_owner_write():
                patch_show_inactive(False, current["etag"])


def test_failed_audit_rolls_back_setting_and_reports_failure(monkeypatch, tmp_path):
    token, client = _api(monkeypatch, tmp_path)
    with client:
        before = client.get("/api/v2/ui/settings/client-list").headers["etag"]
        monkeypatch.setattr(
            "fwrouter_api.services.events.write_audit_event",
            lambda **_kwargs: (_ for _ in ()).throw(OSError("test audit failure")),
        )
        response = client.patch(
            "/api/v2/ui/settings/client-list", json={"show_inactive": True},
            headers={"Authorization": f"Bearer {token}", "Origin": "https://router.example.test",
                     "If-Match": before},
        )
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "CLIENT_LIST_AUDIT_WRITE_FAILED"
        assert read_client_list_settings()["show_inactive"] is False


def test_sqlite_full_synchronous_is_set_before_immediate_write_and_noop_does_not_revise(monkeypatch, tmp_path):
    from fwrouter_api.db import connection as db_connection

    token, client = _api(monkeypatch, tmp_path)
    with client:
        before = client.get("/api/v2/ui/settings/client-list").headers["etag"]
        statements: list[str] = []
        original_connect = db_connection.connect

        def traced_connect():
            connection = original_connect()
            connection.set_trace_callback(lambda sql: statements.append(sql.strip().upper()))
            return connection

        monkeypatch.setattr("fwrouter_api.services.ui_client_list_settings.connect", traced_connect)
        first = client.patch(
            "/api/v2/ui/settings/client-list", json={"show_inactive": True},
            headers={"Authorization": f"Bearer {token}", "Origin": "https://router.example.test",
                     "If-Match": before},
        )
        assert first.status_code == 200
        statements.clear()
        item = first.json()["data"]["client_list"]
        second = client.patch(
            "/api/v2/ui/settings/client-list", json={"show_inactive": True},
            headers={"Authorization": f"Bearer {token}", "Origin": "https://router.example.test",
                     "If-Match": item["etag"]},
        )
        assert second.status_code == 200
        second_item = second.json()["data"]["client_list"]
        assert second_item["revision"] == item["revision"]
        sql = "\n".join(statements)
        assert sql.index("PRAGMA SYNCHRONOUS=FULL") < sql.index("BEGIN IMMEDIATE")
        assert "INSERT INTO SETTINGS" not in sql and "UPDATE SETTINGS" not in sql
        assert second_item["persistence_status"] == "unchanged"


def test_workspace_cache_warm_cold_and_coalesced_readers_retry_after_mutation(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    from fwrouter_api.services import live_probe_cache, ui_state_summary

    live_probe_cache.clear_live_probe_cache_matching(lambda key: key.startswith("ui_state.settings_workspace."))
    old_etag, new_etag = '"cli1-' + "1" * 64 + '"', '"cli1-' + "2" * 64 + '"'
    changed = Event()
    old_loader_started, let_old_loader_finish, follower_waiting = Event(), Event(), Event()
    old_calls = 0
    call_lock = __import__("threading").Lock()

    def snapshot():
        is_new = changed.is_set()
        return {"etag": new_etag if is_new else old_etag,
                "display_settings": {"show_inactive": is_new}}

    monkeypatch.setattr(ui_state_summary, "get_ui_display_snapshot", snapshot)
    monkeypatch.setattr(ui_state_summary, "read_client_list_settings",
                        lambda: {"etag": new_etag if changed.is_set() else old_etag})

    def build(*, display_settings=None):
        nonlocal old_calls
        with call_lock:
            if not display_settings["show_inactive"]:
                old_calls += 1
                old_loader_started.set()
                block = True
            else:
                block = False
        if block:
            assert let_old_loader_finish.wait(timeout=5)
        return {"display_settings": display_settings}

    monkeypatch.setattr(ui_state_summary, "_build_ui_settings_workspace", build)
    original_get_cache = live_probe_cache.get_live_probe_cache
    cache_arrivals = __import__("threading").Barrier(2)
    cache_call_lock = __import__("threading").Lock()
    cache_call_count = 0

    class SignalingEvent(Event):
        def wait(self, timeout=None):
            follower_waiting.set()
            return super().wait(timeout)

    monkeypatch.setattr(live_probe_cache, "Event", SignalingEvent)

    def concurrent_cache_call(*args, **kwargs):
        nonlocal cache_call_count
        with cache_call_lock:
            cache_call_count += 1
            synchronize = cache_call_count <= 2
        if synchronize:
            cache_arrivals.wait(timeout=5)
        return original_get_cache(*args, **kwargs)

    monkeypatch.setattr(ui_state_summary, "get_live_probe_cache", concurrent_cache_call)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(ui_state_summary.get_ui_settings_workspace) for _ in range(2)]
        assert old_loader_started.wait(timeout=5)
        assert follower_waiting.wait(timeout=5)
        changed.set()
        live_probe_cache.clear_live_probe_cache_matching(
            lambda key: key.startswith("ui_state.settings_workspace."),
        )
        let_old_loader_finish.set()
        values = [future.result(timeout=5) for future in futures]
    assert old_calls == 1
    assert all(value["display_settings"]["show_inactive"] is True for value in values)

    warm = ui_state_summary.get_ui_settings_workspace()
    assert warm["display_settings"]["show_inactive"] is True
    cold_calls = 0

    def new_build(*, display_settings=None):
        nonlocal cold_calls
        cold_calls += 1
        return {"display_settings": display_settings}

    monkeypatch.setattr(ui_state_summary, "_build_ui_settings_workspace", new_build)
    live_probe_cache.clear_live_probe_cache_matching(lambda key: key.startswith("ui_state.settings_workspace."))
    cold = ui_state_summary.get_ui_settings_workspace()
    assert cold["display_settings"]["show_inactive"] is True
    assert cold_calls == 1


def test_workspace_cache_retries_once_then_returns_stale_error(monkeypatch):
    from fwrouter_api.services import live_probe_cache, ui_state_summary
    from fwrouter_api.services.ui_client_list_settings import ClientListSettingsError

    live_probe_cache.clear_live_probe_cache_matching(lambda key: key.startswith("ui_state.settings_workspace."))
    snapshots = iter([
        {"etag": '"cli1-' + "3" * 64 + '"', "display_settings": {"show_inactive": False}},
        {"etag": '"cli1-' + "4" * 64 + '"', "display_settings": {"show_inactive": True}},
    ])
    monkeypatch.setattr(ui_state_summary, "get_ui_display_snapshot", lambda: next(snapshots))
    monkeypatch.setattr(ui_state_summary, "read_client_list_settings", lambda: {"etag": '"cli1-' + "5" * 64 + '"'})
    monkeypatch.setattr(ui_state_summary, "_build_ui_settings_workspace", lambda *, display_settings: {"display_settings": display_settings})
    with pytest.raises(ClientListSettingsError, match="CONFIG_READ_MODEL_STALE"):
        ui_state_summary.get_ui_settings_workspace()


def test_workspace_counts_and_panel_filter_use_same_display_snapshot(monkeypatch):
    from fwrouter_api.services.ui_state_clients import _ui_workspace_counts, filter_ui_clients

    clients = [
        {"subject_id": "lan:active", "kind": "lan", "inventory_role": "lan_client", "is_active": True},
        {"subject_id": "lan:inactive", "kind": "lan", "inventory_role": "lan_client", "is_active": False},
    ]
    monkeypatch.setattr("fwrouter_api.services.ui_state_clients._list_ui_client_presence", lambda: clients)
    display = {
        "hidden_subject_ids": [], "system_visibility": {"lan": True},
        "show_inactive": False, "show_internal_vless": False,
    }
    counts = _ui_workspace_counts(display_settings=display)
    panel = filter_ui_clients(clients, display_settings=display)
    assert counts["panel"] == len(panel) == 1
    display["show_inactive"] = True
    counts = _ui_workspace_counts(display_settings=display)
    panel = filter_ui_clients(clients, display_settings=display)
    assert counts["panel"] == len(panel) == 2


def test_queued_protected_write_rechecks_revocable_grant_after_sqlite_writer_lock(monkeypatch, tmp_path):
    from fwrouter_api.services import ui_client_list_settings as owner

    token, client = _api(monkeypatch, tmp_path)
    with client:
        etag = client.get("/api/v2/ui/settings/client-list").headers["etag"]
    locker = owner.connect()
    locker.execute("BEGIN IMMEDIATE")
    begin_seen = Event()
    original_connect = owner.connect

    def traced_connect():
        connection = original_connect()
        connection.set_trace_callback(
            lambda statement: begin_seen.set()
            if statement.strip().upper() == "BEGIN IMMEDIATE" else None
        )
        return connection

    monkeypatch.setattr(owner, "connect", traced_connect)
    grant = admission._Grant()
    marker = admission._GRANT.set(grant)
    queued_context = contextvars.copy_context()
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(queued_context.run, patch_show_inactive, True, etag)
            assert begin_seen.wait(timeout=5)
            grant.revoke()
            locker.commit()
            with pytest.raises(ClientListSettingsError, match="PRESENTATION_WRITE_ADMISSION_REQUIRED"):
                pending.result(timeout=5)
        assert read_client_list_settings()["show_inactive"] is False
    finally:
        grant.revoke()
        admission._GRANT.reset(marker)
        locker.close()


def test_sqlite_writer_busy_fails_without_mutation(monkeypatch, tmp_path):
    from fwrouter_api.db.connection import connect
    from fwrouter_api.services import ui_client_list_settings as owner

    token, client = _api(monkeypatch, tmp_path)
    monkeypatch.setattr(owner, "SQLITE_BUSY_TIMEOUT_MS", 20)
    with client:
        before_response = client.get("/api/v2/ui/settings/client-list")
        before = before_response.json()["data"]["client_list"]
        blocker = connect()
        blocker.execute("BEGIN IMMEDIATE")
        try:
            response = client.patch(
                "/api/v2/ui/settings/client-list", json={"show_inactive": True},
                headers={"Authorization": f"Bearer {token}", "Origin": "https://router.example.test",
                         "If-Match": before["etag"]},
            )
        finally:
            blocker.rollback()
            blocker.close()
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "CLIENT_LIST_WRITER_BUSY"
        after = client.get("/api/v2/ui/settings/client-list").json()["data"]["client_list"]
        assert after["etag"] == before["etag"]
        assert after["show_inactive"] is before["show_inactive"]
