from __future__ import annotations

import hashlib
import json

import pytest

from fwrouter_api.db.connection import db_session
from fwrouter_api.services.apply_orchestrator_commits import (
    _commit_global_mode,
    _commit_global_server_mode,
    _commit_selective_default,
    _stage_subject_admin_mode,
)
from fwrouter_api.services.apply_orchestrator_results import _log_mutation_result
from fwrouter_api.services.events import create_event_context, list_recent_events, write_audit_event
from fwrouter_api.services.event_contract import reset_event_context, set_event_context
from fwrouter_api.services.modules import set_module_desired_state, set_module_lifecycle_mode
from fwrouter_api.services import server_preferences
from fwrouter_api.services.server_preferences import update_server_preferences
from fwrouter_api.services.server_subject_overrides import clear_subject_server_override, set_subject_server_override
from fwrouter_api.services.subjects import update_subject_alias
from fwrouter_api.services.rules_state_readmodel import save_manual_draft
from fwrouter_api.services.rules_state_metadata import mark_rules_job_success
from fwrouter_api.services.rules_state_store import get_rules_state
from fwrouter_api.services.server_global_selection import clear_global_fixed_server, set_global_fixed_server
from fwrouter_api.adapters.xray import XrayApplyResult
from fwrouter_api.services import xray_clients


def _insert_subject(subject_id: str, *, alias: str | None = None) -> None:
    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO subjects (
                subject_id, subject_type, subject_role, implementation_kind,
                stable_key, display_name, alias, desired_mode, applied_mode,
                is_active, is_deleted
            ) VALUES (?, 'lan', 'lan_client', 'native', ?, ?, ?, 'global', 'global', 1, 0)
            """,
            (subject_id, subject_id, subject_id, alias),
        )


def _audit_rows(event_code: str) -> list[dict[str, object]]:
    with db_session() as connection:
        rows = connection.execute(
            "SELECT event_id, subject_id, level, event_type, details_json FROM operational_logs ORDER BY created_at DESC"
        ).fetchall()
    matching = []
    for row in rows:
        details = json.loads(row["details_json"] or "{}")
        if details.get("event_code") == event_code:
            matching.append({"event_id": row["event_id"], "subject_id": row["subject_id"], "level": row["level"], "event_type": row["event_type"], "details": details})
    return matching


def _event_rows(event_code: str) -> list[dict[str, object]]:
    with db_session() as connection:
        rows = connection.execute(
            "SELECT event_id, level, event_type, message, details_json FROM operational_logs ORDER BY created_at DESC"
        ).fetchall()
    return [
        {
            "event_id": row["event_id"],
            "level": row["level"],
            "event_type": row["event_type"],
            "message": row["message"],
            "details": json.loads(row["details_json"] or "{}"),
        }
        for row in rows
        if json.loads(row["details_json"] or "{}").get("event_code") == event_code
    ]


def test_typed_audit_writer_sanitizes_actor_and_values_and_rolls_back_atomically() -> None:
    with pytest.raises(RuntimeError):
        with db_session() as connection:
            write_audit_event(
                actor="https://example.invalid/token=do-not-store",
                actor_attribution="caller_supplied",
                source="api",
                action="preferences_changed",
                event_code="server.preferences_changed",
                entity_type="server",
                entity_id="server-1",
                previous_value={"mode": "direct", "password": "secret-value"},
                new_value={"mode": "vpn", "private_key": "private-value"},
                context=create_event_context(request_id="req-audit-1", job_id="job-audit-1"),
                connection=connection,
            )
            raise RuntimeError("force transaction rollback")

    assert _audit_rows("server.preferences_changed") == []

    event = write_audit_event(
        actor="pytest:admin",
        actor_attribution="caller_supplied",
        source="api",
        action="preferences_changed",
        event_code="server.preferences_changed",
        entity_type="server",
        entity_id="server-1",
        previous_value={"mode": "direct", "password": "secret-value"},
        new_value={"mode": "vpn", "private_key": "private-value"},
        context=create_event_context(request_id="req-audit-2", job_id="job-audit-2"),
    )

    row = _audit_rows("server.preferences_changed")[0]
    serialized = json.dumps(row["details"])
    assert row["level"] == "info"
    assert event.actor == "pytest:admin"
    assert event.actor_attribution == "caller_supplied"
    assert event.request_id == "req-audit-2"
    assert event.job_id == "job-audit-2"
    assert event.details["event_category"] == "audit"
    assert "secret-value" not in serialized
    assert "private-value" not in serialized
    assert "https://example.invalid" not in serialized


def test_typed_audit_writer_fingerprints_xray_subject_ids_everywhere() -> None:
    raw_client_id = "client-uuid-that-is-a-subscription-token"
    event = write_audit_event(
        actor="pytest",
        actor_attribution="caller_supplied",
        source="api",
        action="mode_changed",
        event_code="client.mode_changed",
        entity_type="subject",
        entity_id=f"xray:{raw_client_id}",
        previous_value={"client_id": raw_client_id, "desired_mode": "global"},
        new_value={"desired_mode": "vpn"},
        details={"subject_id": f"xray:{raw_client_id}"},
    )

    row = next(item for item in _audit_rows("client.mode_changed") if item["event_id"] == event.event_id)
    serialized = json.dumps(row["details"])
    assert raw_client_id not in serialized
    assert event.entity_id.startswith("xray-client:")
    assert row["subject_id"] == event.entity_id
    assert row["details"]["entity_id"] == event.entity_id
    assert row["details"]["previous_value"]["client_id"] == "[REDACTED]"


def test_subject_alias_audit_is_atomic_safe_and_skips_noop() -> None:
    _insert_subject("lan:audit-alias", alias="Old name")

    update_subject_alias(
        "lan:audit-alias",
        "New name",
        requested_by="pytest",
    )
    update_subject_alias(
        "lan:audit-alias",
        "New name",
        requested_by="pytest",
    )

    rows = _audit_rows("client.alias_changed")
    assert len(rows) == 1
    assert rows[0]["level"] == "info"
    assert rows[0]["details"]["actor_attribution"] == "caller_supplied"
    assert rows[0]["details"]["previous_value"] == {"alias_present": True, "alias_label": "Old name"}
    assert rows[0]["details"]["new_value"] == {"alias_present": True, "alias_label": "New name"}
    assert rows[0]["details"]["entity_label"] == "New name"


def test_subject_alias_audit_omits_unsafe_alias_snapshots() -> None:
    unsafe_aliases = [
        "https://example.test/client/token",
        "access token secret",
        "123e4567-e89b-12d3-a456-426614174000",
    ]
    for index, alias in enumerate(unsafe_aliases):
        subject_id = f"lan:audit-alias-unsafe-{index}"
        _insert_subject(subject_id, alias="Safe old name")
        update_subject_alias(subject_id, alias, requested_by="pytest")
        row = _audit_rows("client.alias_changed")[-1]
        assert row["details"]["previous_value"]["alias_label"] == "Safe old name"
        assert "alias_label" not in row["details"]["new_value"]
        assert "entity_label" not in row["details"]
        assert alias not in json.dumps(row["details"])


def test_server_preferences_audit_is_atomic_and_contains_only_preference_fields(monkeypatch) -> None:
    with db_session() as connection:
        connection.execute(
            "INSERT INTO servers (server_id, server_name, inventory_state) VALUES ('audit-server', 'Audit server', 'active')"
        )
    result = update_server_preferences(
        "audit-server",
        vpn_auto_priority=3,
        requested_by="pytest",
        reconcile_after_preferences=lambda *, enabled: None,
    )

    assert result["ok"] is True
    rows = _audit_rows("server.preferences_changed")
    assert len(rows) == 1
    details = rows[0]["details"]
    assert rows[0]["level"] == "info"
    assert details["actor"] == "pytest"
    assert details["actor_attribution"] == "caller_supplied"
    assert details["previous_value"]["vpn_auto_priority"] == 0
    assert details["new_value"]["vpn_auto_priority"] == 3
    assert "raw_json" not in json.dumps(details)


def test_server_preference_mutation_rolls_back_when_audit_write_fails(monkeypatch) -> None:
    with db_session() as connection:
        connection.execute(
            "INSERT INTO servers (server_id, server_name, inventory_state) VALUES ('audit-rollback-server', 'Rollback server', 'active')"
        )

    def fail_audit(**kwargs):
        raise RuntimeError("audit insert failed")

    monkeypatch.setattr(server_preferences, "write_audit_event", fail_audit)
    with pytest.raises(RuntimeError, match="audit insert failed"):
        update_server_preferences(
            "audit-rollback-server",
            vpn_auto_priority=4,
            requested_by="pytest",
            reconcile_after_preferences=lambda *, enabled: None,
        )

    with db_session() as connection:
        preference = connection.execute(
            "SELECT vpn_auto_priority FROM server_preferences WHERE server_id = 'audit-rollback-server'"
        ).fetchone()
    assert preference is None


def test_rules_manual_draft_audit_is_safe_and_identical_save_is_a_true_noop() -> None:
    draft = "VPN example.invalid\n"
    save_manual_draft(draft, requested_by="pytest:ui")
    state_after_change = get_rules_state()
    save_manual_draft(draft, requested_by="pytest:ui")
    state_after_noop = get_rules_state()

    rows = _audit_rows("rules.manual_draft_changed")
    assert len(rows) == 1
    event = rows[0]["details"]
    assert event["actor"] == "pytest:ui"
    assert event["actor_attribution"] == "caller_supplied"
    assert event["previous_value"]["sha256"] != event["new_value"]["sha256"]
    assert event["new_value"]["rule_count"] == 1
    assert "example.invalid" not in json.dumps(event)
    assert state_after_noop["updated_at"] == state_after_change["updated_at"]


def test_manual_active_set_audit_records_changed_set_once_without_rule_text() -> None:
    old_text = "DIRECT old.example\n"
    new_text = "VPN new.example\n"
    old_hash = hashlib.sha256(old_text.encode("utf-8")).hexdigest()
    new_hash = hashlib.sha256(new_text.encode("utf-8")).hexdigest()
    with db_session() as connection:
        connection.execute(
            "INSERT INTO jobs (job_id, job_type, status) VALUES ('job-manual-apply-audit', 'apply', 'success')"
        )

    mark_rules_job_success(
        job_id="job-manual-apply-audit",
        update_type="manual_apply",
        audit_change={
            "changed": True,
            "requested_by": "pytest:ui",
            "apply_id": "apply-manual-apply-audit",
            "previous_value": {"sha256": old_hash, "rule_count": 1},
            "new_value": {"sha256": new_hash, "rule_count": 1},
        },
    )
    mark_rules_job_success(
        job_id="job-manual-apply-audit",
        update_type="manual_apply",
        audit_change={"changed": False, "requested_by": "pytest:ui"},
    )

    rows = _audit_rows("rules.manual_set_activated")
    assert len(rows) == 1
    details = rows[0]["details"]
    assert details["actor"] == "pytest:ui"
    assert details["actor_attribution"] == "caller_supplied"
    assert details["job_id"] == "job-manual-apply-audit"
    assert details["apply_id"] == "apply-manual-apply-audit"
    assert details["previous_value"] == {"sha256": old_hash, "rule_count": 1}
    assert details["new_value"] == {"sha256": new_hash, "rule_count": 1}
    assert old_text not in json.dumps(details)
    assert new_text not in json.dumps(details)


def test_global_mode_audit_records_only_actual_persistent_change() -> None:
    _commit_global_mode(mode="direct", requested_by="pytest", job_id="job-noop")
    assert _audit_rows("routing.global_mode_changed") == []

    _commit_global_mode(mode="vpn", requested_by="pytest", job_id="job-change")
    _commit_global_mode(mode="vpn", requested_by="pytest", job_id="job-noop-again")
    rows = _audit_rows("routing.global_mode_changed")
    assert len(rows) == 1
    details = rows[0]["details"]
    assert details["actor_attribution"] == "caller_supplied"
    assert details["previous_value"]["mode"] == "direct"
    assert details["new_value"]["mode"] == "vpn"


def test_global_server_mode_and_selective_default_audit_only_changes() -> None:
    _commit_global_server_mode(server_mode="auto", requested_by="pytest")
    assert _audit_rows("routing.server_mode_changed") == []
    _commit_global_server_mode(server_mode="fixed", requested_by="pytest", job_id="job-server-mode")
    _commit_global_server_mode(server_mode="fixed", requested_by="pytest")
    server_mode_rows = _audit_rows("routing.server_mode_changed")
    assert len(server_mode_rows) == 1
    assert server_mode_rows[0]["details"]["job_id"] == "job-server-mode"

    _commit_selective_default(selective_default="direct", requested_by="pytest")
    assert _audit_rows("routing.selective_default_changed") == []
    _commit_selective_default(selective_default="vpn", requested_by="pytest", job_id="job-default")
    _commit_selective_default(selective_default="vpn", requested_by="pytest")
    default_rows = _audit_rows("routing.selective_default_changed")
    assert len(default_rows) == 1
    assert default_rows[0]["details"]["previous_value"]["selective_default"] == "direct"
    assert default_rows[0]["details"]["new_value"]["selective_default"] == "vpn"


def test_global_fixed_server_audit_uses_hashed_reference_and_skips_noop() -> None:
    server_id = "sensitive-internal-server-reference"
    with db_session() as connection:
        connection.execute(
            "INSERT INTO servers (server_id, server_name, inventory_state) VALUES (?, 'Audit server', 'active')",
            (server_id,),
        )

    assert set_global_fixed_server(server_id, requested_by="pytest", job_id="job-fixed-server")["ok"]
    assert set_global_fixed_server(server_id, requested_by="pytest")["ok"]
    with db_session() as connection:
        connection.execute("UPDATE servers SET server_name = 'Renamed audit server' WHERE server_id = ?", (server_id,))
    assert clear_global_fixed_server(requested_by="pytest", job_id="job-clear-server")["ok"]
    assert clear_global_fixed_server(requested_by="pytest")["ok"]

    rows = _audit_rows("routing.global_fixed_server_changed")
    assert len(rows) == 2
    serialized = json.dumps([row["details"] for row in rows])
    assert server_id not in serialized
    assert all(row["details"]["actor_attribution"] == "caller_supplied" for row in rows)
    assert any(row["details"]["new_value"].get("server_ref") for row in rows)
    assert any(row["details"]["new_value"].get("server_label") == "Audit server" for row in rows)
    assert any(row["details"]["previous_value"].get("server_label") == "Renamed audit server" for row in rows)


def test_manual_rules_operational_log_is_allowlisted_recursively() -> None:
    full_text = "VPN sensitive.example\n"
    _log_mutation_result(
        {
            "intent": "apply_manual_rules",
            "ok": True,
            "requested_by": "pytest",
            "job_id": "job-rules-safe",
            "apply_id": "apply-rules-safe",
            "stage": "commit",
            "details": {
                "nested": {
                    "rules": {
                        "active_text": full_text,
                        "effective_counts": {"total": 1, "vpn": 1},
                    }
                },
                "validation": {"valid": True, "errors": []},
                "provider_url": "https://private.invalid/token-secret",
            },
        }
    )

    rows = _event_rows("manual_rules_apply_completed")
    assert len(rows) == 1
    details = rows[0]["details"]
    serialized = json.dumps(details)
    assert full_text not in serialized
    assert "private.invalid" not in serialized
    assert "token-secret" not in serialized
    assert details["job_id"] == "job-rules-safe"
    assert details["apply_id"] == "apply-rules-safe"
    assert details["stage"] == "commit"
    assert details["effective_counts"] == {"total": 1, "vpn": 1}
    assert details["manual_rule_set"]["sha256"]


def test_manual_rules_failed_apply_keeps_safe_error_and_request_context() -> None:
    context_token = set_event_context(request_id="req-manual-rules-apply")
    try:
        _log_mutation_result(
            {
                "intent": "apply_manual_rules",
                "ok": False,
                "requested_by": "pytest",
                "job_id": "job-rules-failed",
                "apply_id": "apply-rules-failed",
                "stage": "runtime_apply",
                "code": "RULES_APPLY_FAILED",
                "details": {"nested": {"debug": "private path /etc/fwrouter/secret"}},
            }
        )
    finally:
        reset_event_context(context_token)

    rows = _event_rows("manual_rules_apply_failed")
    assert len(rows) == 1
    details = rows[0]["details"]
    assert rows[0]["level"] == "error"
    assert details["error_code"] == "RULES_APPLY_FAILED"
    assert details["stage"] == "runtime_apply"
    assert details["request_id"] == "req-manual-rules-apply"
    assert details["job_id"] == "job-rules-failed"
    assert details["apply_id"] == "apply-rules-failed"
    assert "/etc/fwrouter/secret" not in json.dumps(details)


def test_module_mutations_emit_atomic_audit_events_only_for_changes() -> None:
    set_module_desired_state("watchdog", "disabled", requested_by="pytest", run_now=False)
    set_module_desired_state("watchdog", "disabled", requested_by="pytest", run_now=False)
    set_module_lifecycle_mode("vpn", "external", requested_by="pytest")

    desired = _audit_rows("module.desired_state_changed")
    lifecycle = _audit_rows("module.lifecycle_changed")
    assert len(desired) == 1
    assert len(lifecycle) == 1
    assert desired[0]["level"] == lifecycle[0]["level"] == "info"
    assert desired[0]["details"]["previous_value"] == {"desired_state": "enabled"}
    assert desired[0]["details"]["new_value"] == {"desired_state": "disabled"}
    assert lifecycle[0]["details"]["previous_value"] == {"lifecycle_mode": "managed"}
    assert lifecycle[0]["details"]["new_value"] == {"lifecycle_mode": "external"}


def test_subject_mode_and_server_assignment_audit_include_job_context_and_no_duplicate() -> None:
    _insert_subject("lan:audit-mode")
    _stage_subject_admin_mode(
        subject_id="lan:audit-mode",
        mode="vpn",
        requested_by="pytest",
        job_id="job-mode-audit",
    )
    mode_rows = _audit_rows("client.mode_changed")
    assert len(mode_rows) == 1
    assert mode_rows[0]["details"]["job_id"] == "job-mode-audit"
    assert mode_rows[0]["details"]["previous_value"]["desired_mode"] == "global"
    assert mode_rows[0]["details"]["new_value"]["desired_mode"] == "vpn"

    with db_session() as connection:
        connection.execute(
            "INSERT INTO servers (server_id, server_name, inventory_state) VALUES ('audit-assignment-server', 'Assignment server', 'active')"
        )
    assigned = set_subject_server_override(
        "lan:audit-mode",
        "audit-assignment-server",
        requested_by="pytest",
        job_id="job-assignment-audit",
    )
    assert assigned["ok"] is True
    assignment_rows = _audit_rows("server.assignment_changed")
    assert len(assignment_rows) == 1
    assert assignment_rows[0]["details"]["job_id"] == "job-assignment-audit"
    assert assignment_rows[0]["details"]["previous_value"] == {
        "server_id": None, "server_mode": "global", "server_label": None,
    }
    assert assignment_rows[0]["details"]["new_value"] == {
        "server_id": "audit-assignment-server", "server_mode": "fixed", "server_label": "Assignment server",
    }
    cleared = clear_subject_server_override("lan:audit-mode", requested_by="pytest")
    assert cleared["ok"] is True
    assignment_rows = _audit_rows("server.assignment_changed")
    clear_row = next(row for row in assignment_rows if row["details"]["new_value"].get("server_mode") == "global")
    assert clear_row["details"]["previous_value"] == {
        "server_id": "audit-assignment-server", "server_mode": "fixed", "server_label": "Assignment server",
    }
    assert clear_row["details"]["new_value"] == {
        "server_id": None, "server_mode": "global", "server_label": None,
    }

    before = len(_audit_rows("client.mode_changed"))
    _log_mutation_result({
        "ok": True,
        "intent": "set_subject_admin_mode",
        "job_id": "job-mode-audit",
        "requested_by": "pytest",
        "audit_context": {"action": "mode_changed", "entity_id": "lan:audit-mode"},
    })
    assert len(_audit_rows("client.mode_changed")) == before


def test_runtime_apply_failure_after_committed_intent_is_a_separate_operational_event() -> None:
    job_id = "job-mode-runtime-failed-audit"
    _log_mutation_result({
        "ok": False,
        "intent": "set_subject_admin_mode",
        "job_id": job_id,
        "requested_by": "pytest",
        "stage": "runtime_apply",
        "code": "RUNTIME_APPLY_FAILED",
        "audit_context": {
            "action": "mode_changed",
            "entity_type": "subject",
            "entity_id": "lan:audit-runtime-failure",
            "previous_value": {"desired_mode": "global"},
            "new_value": {"desired_mode": "vpn"},
        },
    })

    assert not any(item["details"].get("job_id") == job_id for item in _audit_rows("client.mode_changed"))
    with db_session() as connection:
        failure = connection.execute(
            "SELECT level, details_json FROM operational_logs WHERE details_json LIKE ?",
            (f'%"job_id": "{job_id}"%',),
        ).fetchall()
    failure_events = [json.loads(row["details_json"]) for row in failure]
    operational = {event.get("event_code"): event for event in failure_events}
    assert operational["admin.audit_event_missing"]["outcome"] == "committed_intent_missing_audit"
    assert operational["client.mode_apply_failed"]["outcome"] == "runtime_apply_failed_after_intent_commit"


def test_xray_client_alias_success_normalizes_existing_event_without_raw_alias(monkeypatch) -> None:
    class FakeAdapter:
        def update_client_alias(self, client_id: str, alias: str | None) -> XrayApplyResult:
            return XrayApplyResult(ok=True, message="Alias updated.", details={})

    monkeypatch.setattr(xray_clients, "_xray_managed_runtime_blocked", lambda operation: None)
    monkeypatch.setattr(xray_clients, "_xray_adapter", lambda: FakeAdapter())
    monkeypatch.setattr(xray_clients, "_client_alias_map", lambda: {"sensitive-client-id": "Old alias"})
    monkeypatch.setattr(xray_clients, "_set_local_alias", lambda client_id, alias: None)

    result = xray_clients.update_xray_client_alias(
        "sensitive-client-id",
        alias="New alias",
        requested_by="pytest",
    )

    assert result["ok"] is True
    rows = _audit_rows("client.alias_changed")
    assert len(rows) == 1
    row = rows[0]
    assert row["event_type"] == "xray_client_alias_updated"
    details = row["details"]
    assert details["actor"] == "pytest"
    assert details["actor_attribution"] == "caller_supplied"
    assert details["previous_value"] == {"alias_present": True, "alias_label": "Old alias"}
    assert details["new_value"] == {"alias_present": True, "alias_label": "New alias"}
    serialized = json.dumps(details)
    assert "sensitive-client-id" not in serialized
    assert "Old alias" in serialized
    assert "New alias" in serialized


def test_xray_client_alias_audit_omits_unsafe_alias_snapshots(monkeypatch) -> None:
    class FakeAdapter:
        def update_client_alias(self, client_id: str, alias: str | None) -> XrayApplyResult:
            return XrayApplyResult(ok=True, message="Alias updated.", details={})

    monkeypatch.setattr(xray_clients, "_xray_managed_runtime_blocked", lambda operation: None)
    monkeypatch.setattr(xray_clients, "_xray_adapter", lambda: FakeAdapter())
    monkeypatch.setattr(xray_clients, "_client_alias_map", lambda: {"safe-client-id": "Safe old alias"})
    monkeypatch.setattr(xray_clients, "_set_local_alias", lambda client_id, alias: None)
    raw_alias = "https://example.test/client/token"

    xray_clients.update_xray_client_alias("safe-client-id", alias=raw_alias, requested_by="pytest")

    row = _audit_rows("client.alias_changed")[-1]
    assert row["details"]["previous_value"]["alias_label"] == "Safe old alias"
    assert "alias_label" not in row["details"]["new_value"]
    assert "entity_label" not in row["details"]
    assert raw_alias not in json.dumps(row["details"])


def test_xray_client_create_audit_preserves_legacy_type_without_client_id(monkeypatch) -> None:
    raw_client_id = "create-client-uuid-secret"

    class FakeAdapter:
        def create_client(self, *, alias: str | None, email: str | None) -> XrayApplyResult:
            return XrayApplyResult(
                ok=True,
                message="Created.",
                details={
                    "stage": "completed",
                    "client": {
                        "client_id": raw_client_id,
                        "client_uuid": raw_client_id,
                        "email": None,
                        "enabled": True,
                        "raw": {},
                    },
                },
            )

    subscription_service = __import__("fwrouter_api.services.xray_subscription_service", fromlist=["export_xray_subscription"])
    monkeypatch.setattr(xray_clients, "_xray_managed_runtime_blocked", lambda operation: None)
    monkeypatch.setattr(xray_clients, "_xray_client_create_preflight", lambda **kwargs: {"ok": True})
    monkeypatch.setattr(xray_clients, "_xray_adapter", lambda: FakeAdapter())
    monkeypatch.setattr(xray_clients, "_sync_xray_inventory", lambda requested_by: {"ok": True})
    monkeypatch.setattr(xray_clients, "_set_local_alias", lambda client_id, alias: None)
    monkeypatch.setattr(xray_clients, "_materialize_xray_runtime_bindings", lambda **kwargs: {"ok": True})
    monkeypatch.setattr(xray_clients, "verify_xray_client_runtime_convergence", lambda **kwargs: {"ok": True})
    monkeypatch.setattr(subscription_service, "export_xray_subscription", lambda client_id: {"ok": True, "subscription_uri": None})

    payload = xray_clients.create_xray_client(alias="Test client", requested_by="pytest")

    assert payload["ok"] is True
    expected_entity_id = xray_clients._xray_audit_entity_ref(raw_client_id)
    row = next(item for item in _audit_rows("client.created") if item["details"].get("entity_id") == expected_entity_id)
    assert row["event_type"] == "external_client.created"
    assert row["level"] == "info"
    assert row["subject_id"] is None
    assert row["details"]["event_category"] == "audit"
    assert row["details"]["entity_id"] == expected_entity_id
    assert raw_client_id not in json.dumps(row["details"])


def test_xray_client_delete_audit_preserves_legacy_type_without_client_id(monkeypatch) -> None:
    raw_client_id = "delete-client-uuid-secret"

    class FakeAdapter:
        def delete_client(self, client_id: str) -> XrayApplyResult:
            return XrayApplyResult(
                ok=True,
                message="Deleted.",
                details={"stage": "completed", "client": {"client_id": raw_client_id, "email": None}},
            )

    monkeypatch.setattr(xray_clients, "_xray_managed_runtime_blocked", lambda operation: None)
    monkeypatch.setattr(xray_clients, "_xray_adapter", lambda: FakeAdapter())
    monkeypatch.setattr(xray_clients, "_sync_xray_inventory", lambda requested_by: {"ok": True})
    monkeypatch.setattr(xray_clients, "cleanup_xray_client_projection", lambda client_id: {
        "subject_ids": [], "subjects_deleted": 0, "server_overrides_deleted": 0, "user_overrides_deleted": 0,
    })
    monkeypatch.setattr(xray_clients, "_materialize_xray_runtime_bindings", lambda **kwargs: {"ok": True})
    monkeypatch.setattr(xray_clients, "verify_xray_client_runtime_convergence", lambda **kwargs: {"ok": True})

    payload = xray_clients.delete_xray_client(raw_client_id, requested_by="pytest")

    assert payload["ok"] is True
    expected_entity_id = xray_clients._xray_audit_entity_ref(raw_client_id)
    row = next(item for item in _audit_rows("client.deleted") if item["details"].get("entity_id") == expected_entity_id)
    assert row["event_type"] == "external_client.deleted"
    assert row["level"] == "info"
    assert row["subject_id"] is None
    assert row["details"]["event_category"] == "audit"
    assert row["details"]["entity_id"] == expected_entity_id
    assert raw_client_id not in json.dumps(row["details"])
