from __future__ import annotations

import json
from pathlib import Path

from fwrouter_api.adapters.subscription import DEFAULT_SUBSCRIPTION_ADAPTER
from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import db_session, initialize_database
from fwrouter_api.services.core_bypass import _save_bypass_setting, enable_core_bypass
from fwrouter_api.services.subscription import refresh_subscription_inventory_batch, save_subscription_url
from fwrouter_api.services.subscription_profiles import disable_subscription_identity, ensure_subscription_identity


def _configure_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FWROUTER_STATE_DIR", str(tmp_path / "state"))
    get_settings.cache_clear()
    initialize_database()


def _events() -> list[dict[str, object]]:
    with db_session() as connection:
        rows = connection.execute(
            "SELECT event_type, details_json FROM operational_logs ORDER BY created_at, rowid"
        ).fetchall()
    return [
        {"event_type": row["event_type"], "details": json.loads(row["details_json"] or "{}")}
        for row in rows
    ]


def test_subscription_source_audit_is_safe_and_noop_is_silent(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    url = "https://feeds.example/sub?token=private-source-token"

    save_subscription_url(url, requested_by="operator")
    save_subscription_url(url, requested_by="operator")

    audit = [event for event in _events() if event["details"].get("event_code") == "subscription.source_added"]
    assert len(audit) == 1
    details = audit[0]["details"]
    assert details["event_category"] == "audit"
    assert details["actor"] == "operator"
    assert details["actor_attribution"] == "caller_supplied"
    assert details["new_value"]["selected_as_primary"] is True
    assert "private-source-token" not in json.dumps(audit)
    assert url not in json.dumps(audit)


def test_subscription_admin_metadata_audits_only_changed_field_names(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    url = "https://feeds.example/sub"
    save_subscription_url(url, metadata={"name": "Initial"})
    save_subscription_url(
        url,
        metadata={"name": "Changed", "private_key": "must-not-be-copied"},
        requested_by="operator",
    )
    save_subscription_url(
        url,
        metadata={"name": "Changed", "private_key": "must-not-be-copied"},
        requested_by="operator",
    )

    audit = [
        event for event in _events()
        if event["details"].get("event_code") == "subscription.configuration_changed"
    ]
    assert len(audit) == 1
    details = audit[0]["details"]
    assert details["new_value"] == {"metadata_changed": True, "changed_fields": ["name"]}
    assert "Changed" not in json.dumps(details)
    assert "must-not-be-copied" not in json.dumps(details)


def test_subscription_unknown_sensitive_metadata_change_is_audited_safely(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    url = "https://feeds.example/config"
    save_subscription_url(url, metadata={"private_key": "old-private-value"})
    save_subscription_url(
        url,
        metadata={"private_key": "new-private-value"},
        requested_by="operator",
    )
    save_subscription_url(
        url,
        metadata={"private_key": "new-private-value"},
        requested_by="operator",
    )

    audit = [
        event for event in _events()
        if event["details"].get("event_code") == "subscription.configuration_changed"
    ]
    assert len(audit) == 1
    details = audit[0]["details"]
    assert details["metadata_changed"] is True
    assert details["new_value"]["metadata_changed"] is True
    assert details["new_value"]["changed_fields"] == []
    serialized = json.dumps(details)
    assert "private_key" not in serialized
    assert "old-private-value" not in serialized
    assert "new-private-value" not in serialized


def test_subscription_primary_source_change_uses_hash_references(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    first = "https://feeds.example/first?token=first-secret"
    second = "https://feeds.example/second?token=second-secret"
    save_subscription_url(first)
    save_subscription_url(second, requested_by="operator")

    source_events = [
        event for event in _events()
        if event["details"].get("event_code") == "subscription.source_added"
    ]
    config_events = [
        event for event in _events()
        if event["details"].get("event_code") == "subscription.configuration_changed"
    ]
    assert len(source_events) == 1
    assert config_events == []
    details = source_events[0]["details"]
    assert details["previous_primary_source_ref"].startswith("src:")
    assert details["new_value"]["selected_as_primary"] is True
    assert "first-secret" not in json.dumps(details)
    assert "second-secret" not in json.dumps(details)
    assert "feeds.example" not in json.dumps(details)


def test_batch_persisted_sources_audit_even_when_fetch_fails(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)

    def failed_refresh(url: str):
        from fwrouter_api.adapters.subscription import SubscriptionRefreshResult, SubscriptionRefreshStatus

        return SubscriptionRefreshResult(
            status=SubscriptionRefreshStatus.FAILED,
            message="fetch failed",
            error_code="SUBSCRIPTION_DOWNLOAD_FAILED",
            error_message="fetch failed",
            metadata={"url": url},
        )

    monkeypatch.setattr(DEFAULT_SUBSCRIPTION_ADAPTER, "refresh", failed_refresh)
    result = refresh_subscription_inventory_batch(
        ["https://feeds.example/batch?token=batch-secret"],
        requested_by="operator",
    )

    assert result["ok"] is False
    audit = [event for event in _events() if event["details"].get("event_code") == "subscription.source_added"]
    assert len(audit) == 1
    serialized = json.dumps(audit)
    assert "batch-secret" not in serialized
    assert "https://feeds.example" not in serialized


def test_automatic_batch_refresh_does_not_audit_source_persistence(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)

    def failed_refresh(url: str):
        from fwrouter_api.adapters.subscription import SubscriptionRefreshResult, SubscriptionRefreshStatus

        return SubscriptionRefreshResult(
            status=SubscriptionRefreshStatus.FAILED,
            message="fetch failed",
            error_code="SUBSCRIPTION_DOWNLOAD_FAILED",
            error_message="fetch failed",
            metadata={"url": url},
        )

    monkeypatch.setattr(DEFAULT_SUBSCRIPTION_ADAPTER, "refresh", failed_refresh)
    refresh_subscription_inventory_batch(["https://feeds.example/automatic"])

    assert not [
        event for event in _events()
        if event["details"].get("event_code") == "subscription.source_added"
    ]


def test_subscription_identity_disable_audits_actual_change_without_token(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    token = "subscription-identity-secret"
    ensure_subscription_identity(token)

    first = disable_subscription_identity(token, requested_by="operator")
    second = disable_subscription_identity(token, requested_by="operator")

    assert first["account"]["changed"] is True
    assert second["account"]["changed"] is False
    audit = [event for event in _events() if event["details"].get("event_code") == "subscription.identity_disabled"]
    assert len(audit) == 1
    assert token not in json.dumps(audit)


def test_core_bypass_audit_contract_and_noop(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)

    _save_bypass_setting({"enabled": True}, requested_by="operator", job_id="job-enable")
    _save_bypass_setting({"enabled": True, "reason": "metadata-only"}, requested_by="operator", job_id="job-repeat")
    noop = enable_core_bypass(job_id="job-public-noop", requested_by="operator")

    audit = [event for event in _events() if event["details"].get("event_code") == "core.bypass_enabled"]
    assert len(audit) == 1
    assert noop["already_enabled"] is True
    details = audit[0]["details"]
    assert details["event_category"] == "audit"
    assert details["severity"] == "info"
    assert details["actor_attribution"] == "caller_supplied"
    assert details["job_id"] == "job-enable"
    assert details["previous_value"] == {"enabled": False}
    assert details["new_value"] == {"enabled": True}
