from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from _test_support import configure_test_state_dir_without_schedulers as _configure_env
from fwrouter_api.db.connection import db_session, initialize_database
from fwrouter_api.services import subject_groups, subscription_profiles
from fwrouter_api.services.live_probe_cache import (
    clear_live_probe_cache_matching,
    get_live_probe_cache,
    peek_live_probe_cache,
)
from fwrouter_api.services.subjects import update_subject_alias
from fwrouter_api.services.subscription_profiles import ensure_subscription_identity
from fwrouter_api.services.ui_state_clients import list_ui_clients
from fwrouter_api.services.ui_state_inventory import list_ui_settings_inventory


def _create_subscription_group(monkeypatch, tmp_path: Path, *, display_name: str = "Original Group") -> tuple[str, int, str]:
    _configure_env(monkeypatch, tmp_path)
    clear_live_probe_cache_matching(lambda key: key == "ui_state.subscription_clients")
    initialize_database()
    ensure_subscription_identity("group-label-token", display_name=display_name)
    with db_session() as connection:
        client = connection.execute(
            "SELECT account_id, token FROM subscription_clients WHERE token = ?",
            ("group-label-token",),
        ).fetchone()
        assert client is not None
        account_id = int(client["account_id"])
        token = str(client["token"])
        digest = hashlib.sha1(token.encode("utf-8")).hexdigest()[:10]
        email = f"sub-{digest}-0123456789ab@fwrouter.local"
        connection.execute(
            """INSERT INTO subjects (
                   subject_id, subject_type, subject_role, implementation_kind, stable_key,
                   display_name, desired_mode, runtime_state, is_active, is_deleted, metadata_json
               ) VALUES (?, 'explicit_external_client', 'vless_client', 'xray', ?, ?, 'enabled', 'active', 1, 0, ?)""",
            (
                "xray-subject:group-label-member",
                "xray-subject:group-label-member",
                display_name,
                json.dumps({"detail": {"email": email, "client_uuid": "00000000-0000-4000-8000-000000000001"}}),
            ),
        )
    return f"xray-subscription:sub-{digest}", account_id, token


def test_subscription_group_alias_updates_canonical_label_and_settings_projection(monkeypatch, tmp_path: Path, isolated_host_observations) -> None:
    group_id, account_id, token = _create_subscription_group(monkeypatch, tmp_path)

    def protected_state() -> dict[str, list[dict[str, object]]]:
        with db_session() as connection:
            return {
                "xray_subjects": [dict(row) for row in connection.execute(
                    "SELECT subject_id, stable_key, display_name, alias, desired_mode, applied_mode, apply_state, runtime_state, is_active, is_deleted, metadata_json FROM subjects WHERE implementation_kind = 'xray' ORDER BY subject_id"
                ).fetchall()],
                "overrides": [dict(row) for row in connection.execute(
                    "SELECT * FROM subject_server_overrides ORDER BY subject_id"
                ).fetchall()],
                "routing": [dict(row) for row in connection.execute(
                    "SELECT * FROM routing_global_state ORDER BY id"
                ).fetchall()],
                "settings": [dict(row) for row in connection.execute(
                    "SELECT * FROM settings ORDER BY key"
                ).fetchall()],
            }

    def projected_group() -> dict[str, object]:
        matches = [
            item for item in list_ui_settings_inventory(
                role="vless_client", live_observations=False
            ) if item.get("subject_id") == group_id
        ]
        assert len(matches) == 1
        return matches[0]

    def active_client_group() -> dict[str, object]:
        matches = [item for item in list_ui_clients() if item.get("subject_id") == group_id]
        assert len(matches) == 1
        return matches[0]

    assert projected_group()["display_name"] == "Original Group"
    assert active_client_group()["display_name"] == "Original Group"
    get_live_probe_cache(
        "ui_state.subscription_clients", ttl_seconds=30, loader=lambda: {"sentinel": {}}
    )
    protected_before = protected_state()
    result = update_subject_alias(group_id, "  Browser Group  ", requested_by="pytest")
    assert result == {
        "subject_id": group_id,
        "account_id": account_id,
        "display_name": "Browser Group",
        "alias": "Browser Group",
    }
    assert peek_live_probe_cache("ui_state.subscription_clients") is None
    assert projected_group()["display_name"] == "Browser Group"
    assert active_client_group()["display_name"] == "Browser Group"
    with db_session() as connection:
        account = connection.execute(
            "SELECT display_name FROM subscription_accounts WHERE account_id = ?", (account_id,)
        ).fetchone()
        client = connection.execute(
            "SELECT display_name FROM subscription_clients WHERE token = ?", (token,)
        ).fetchone()
        member = connection.execute(
            "SELECT subject_id, metadata_json FROM subjects WHERE subject_id = ?",
            ("xray-subject:group-label-member",),
        ).fetchone()
    assert account["display_name"] == client["display_name"] == "Browser Group"
    assert member["subject_id"] == "xray-subject:group-label-member"
    assert json.loads(member["metadata_json"])["detail"]["client_uuid"] == "00000000-0000-4000-8000-000000000001"
    assert protected_state() == protected_before
    assert result["subject_id"] == group_id
    with db_session() as connection:
        audit = connection.execute(
            "SELECT details_json FROM operational_logs WHERE subject_id = ? AND json_extract(details_json, '$.event_code') = 'client.alias_changed' ORDER BY created_at DESC LIMIT 1",
            (group_id,),
        ).fetchone()
        assert audit is not None
        assert token not in str(audit["details_json"])

    event_count = _subscription_alias_audit_count(group_id)
    assert update_subject_alias(group_id, "Browser Group", requested_by="pytest")["display_name"] == "Browser Group"
    assert _subscription_alias_audit_count(group_id) == event_count
    assert update_subject_alias(group_id, None, requested_by="pytest")["display_name"] == "Group Label Token"
    assert projected_group()["display_name"] == "Group Label Token"
    assert active_client_group()["display_name"] == "Group Label Token"


def _subscription_alias_audit_count(group_id: str) -> int:
    with db_session() as connection:
        row = connection.execute(
            "SELECT count(*) AS count FROM operational_logs WHERE subject_id = ? AND json_extract(details_json, '$.event_code') = 'client.alias_changed'",
            (group_id,),
        ).fetchone()
    return int(row["count"])


def test_subscription_group_alias_rejects_unknown_ambiguous_and_multiclient_groups(monkeypatch, tmp_path: Path) -> None:
    group_id, account_id, token = _create_subscription_group(monkeypatch, tmp_path)
    assert update_subject_alias("xray-subscription:sub-0000000000", "Nope") is None

    with db_session() as connection:
        connection.execute(
            "INSERT INTO subscription_clients (account_id, token, app_type, enabled, display_name) VALUES (?, ?, 'auto', 1, 'Other')",
            (account_id, "second-client-token"),
        )
    assert update_subject_alias(group_id, "Rejected") is None
    with db_session() as connection:
        assert connection.execute(
            "SELECT display_name FROM subscription_accounts WHERE account_id = ?", (account_id,)
        ).fetchone()["display_name"] == "Original Group"

    # Force a digest collision at the canonical resolver boundary to exercise
    # its ambiguous-match rejection without weakening the production resolver.
    with db_session() as connection:
        connection.execute("DELETE FROM subscription_clients WHERE token = ?", ("second-client-token",))
        connection.execute(
            "INSERT INTO subscription_accounts (slug, display_name) VALUES ('other-group', 'Other Group')"
        )
        other_account = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
        connection.execute(
            "INSERT INTO subscription_clients (account_id, token, app_type, display_name) VALUES (?, 'other-token', 'auto', 'Other Group')",
            (other_account,),
        )

    class _Digest:
        def hexdigest(self) -> str:
            return "aaaaaaaaaa00000000000000000000000000000000"

    monkeypatch.setattr(subject_groups, "hashlib", SimpleNamespace(sha1=lambda *_args, **_kwargs: _Digest()))
    collision_group = "xray-subscription:sub-aaaaaaaaaa"
    assert update_subject_alias(collision_group, "Rejected") is None
    with db_session() as connection:
        assert connection.execute(
            "SELECT display_name FROM subscription_accounts WHERE account_id = ?", (account_id,)
        ).fetchone()["display_name"] == "Original Group"


def test_subscription_group_alias_audit_failure_rolls_back_and_preserves_cache(monkeypatch, tmp_path: Path) -> None:
    group_id, account_id, token = _create_subscription_group(monkeypatch, tmp_path)
    cached = get_live_probe_cache(
        "ui_state.subscription_clients", ttl_seconds=30, loader=lambda: {"preserved": {}}
    )

    def fail_audit(**_kwargs):
        raise RuntimeError("audit write failed")

    monkeypatch.setattr(subscription_profiles, "write_audit_event", fail_audit)
    with pytest.raises(RuntimeError, match="audit write failed"):
        update_subject_alias(group_id, "Should Roll Back", requested_by="pytest")
    assert peek_live_probe_cache("ui_state.subscription_clients") is cached
    with db_session() as connection:
        account = connection.execute(
            "SELECT display_name FROM subscription_accounts WHERE account_id = ?", (account_id,)
        ).fetchone()
        client = connection.execute(
            "SELECT display_name FROM subscription_clients WHERE token = ?", (token,)
        ).fetchone()
    assert account["display_name"] == client["display_name"] == "Original Group"


def test_ordinary_subject_alias_still_updates_subject_alias_only(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    with db_session() as connection:
        connection.execute(
            "INSERT INTO subjects (subject_id, subject_type, stable_key, display_name, desired_mode) VALUES ('lan:test-alias', 'lan', 'lan:test-alias', 'LAN Device', 'global')"
        )
    result = update_subject_alias("lan:test-alias", "Office Laptop", requested_by="pytest")
    assert result is not None and result["alias"] == "Office Laptop"
    with db_session() as connection:
        row = connection.execute(
            "SELECT alias, display_name FROM subjects WHERE subject_id = 'lan:test-alias'"
        ).fetchone()
        assert row["alias"] == "Office Laptop"
        assert row["display_name"] == "LAN Device"

