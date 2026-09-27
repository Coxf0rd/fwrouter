from __future__ import annotations

import json

import pytest

from fwrouter_api.db.connection import db_session
from fwrouter_api.services.apply_orchestrator_commits import _stage_subject_admin_mode
from fwrouter_api.services.apply_orchestrator_results import _log_mutation_result
from fwrouter_api.services.events import create_event_context, list_recent_events, write_audit_event
from fwrouter_api.services.modules import set_module_desired_state, set_module_lifecycle_mode
from fwrouter_api.services import server_preferences
from fwrouter_api.services.server_preferences import update_server_preferences
from fwrouter_api.services.server_subject_overrides import set_subject_server_override
from fwrouter_api.services.subjects import update_subject_alias
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
        "New name token=should-not-be-recorded",
        requested_by="pytest",
    )
    update_subject_alias(
        "lan:audit-alias",
        "New name token=should-not-be-recorded",
        requested_by="pytest",
    )

    rows = _audit_rows("client.alias_changed")
    assert len(rows) == 1
    assert rows[0]["level"] == "info"
    assert rows[0]["details"]["actor_attribution"] == "caller_supplied"
    assert rows[0]["details"]["previous_value"] == {"alias_present": True}
    assert rows[0]["details"]["new_value"] == {"alias_present": True}
    assert "should-not-be-recorded" not in json.dumps(rows[0]["details"])


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
    assert mode_rows[0]["details"]["previous_value"] == {"desired_mode": "global"}
    assert mode_rows[0]["details"]["new_value"] == {"desired_mode": "vpn"}

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
    assert assignment_rows[0]["details"]["new_value"] == {"server_id": "audit-assignment-server"}

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
        alias="New alias token=must-not-persist",
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
    assert details["previous_value"] == {"alias_present": True}
    assert details["new_value"] == {"alias_present": True}
    serialized = json.dumps(details)
    assert "sensitive-client-id" not in serialized
    assert "Old alias" not in serialized
    assert "New alias" not in serialized
    assert "must-not-persist" not in serialized


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
