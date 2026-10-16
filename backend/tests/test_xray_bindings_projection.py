from __future__ import annotations

import json
from typing import Any

import pytest

from fwrouter_api.core.config import get_settings
from fwrouter_api.db import connection as db_connection
from fwrouter_api.db.connection import db_session, initialize_database
from fwrouter_api.services import xray_bindings


def _seed_projection_subjects(monkeypatch, tmp_path, *, extra_email: Any = ...) -> list[str]:
    monkeypatch.setenv("FWROUTER_STATE_DIR", str(tmp_path / "state"))
    get_settings.cache_clear()
    initialize_database()
    emails = [f"sub-account-{index}@fwrouter.local" for index in range(70)]
    emails += [f"vpn-auto-{index}@fwrouter.local" for index in range(10)]
    emails += ["fixed-one@example.test", "fixed-two@example.test"]
    emails[0] = "SUB-account-0@fwrouter.local"
    emails[70] = "VPN-auto-0@fwrouter.local"
    if extra_email is not ...:
        emails.append(extra_email)
    with db_session() as connection:
        for index, email in enumerate(emails):
            subject_id = f"xray:projection-{index}"
            detail = {
                "client_uuid": f"00000000-0000-4000-8000-{index:012d}",
                "client_id": f"00000000-0000-4000-8000-{index:012d}",
                "enabled": True,
            }
            if isinstance(email, str) and email:
                detail["email"] = email
            elif email is not None:
                detail["email"] = email
            connection.execute(
                "INSERT INTO subjects (subject_id, subject_type, subject_role, implementation_kind, "
                "stable_key, desired_mode, is_active, is_deleted, metadata_json) "
                "VALUES (?, 'explicit_external_client', 'vless_client', 'xray', ?, 'direct', 1, 0, ?)",
                (subject_id, subject_id, json.dumps({"detail": detail})),
            )
    return [f"xray:projection-{index}" for index in range(len(emails))]


def _install_projection_spies(monkeypatch) -> list[str]:
    enriched: list[str] = []
    monkeypatch.setattr(xray_bindings.subject_policy_service, "get_routing_snapshot", lambda: {})
    monkeypatch.setattr(xray_bindings.subject_policy_service, "build_runtime_enforcement_state", lambda: {})
    monkeypatch.setattr(xray_bindings.subject_policy_service, "get_core_bypass_state", lambda: {})
    monkeypatch.setattr(
        xray_bindings.subject_policy_service, "_default_subject_runtime_enforcement",
        lambda **_kwargs: {},
    )

    def enrich(subject, **_kwargs):
        enriched.append(subject["subject_id"])
        return {**subject, "effective_state": {"effective_mode": "direct", "dataplane_path": "vpn"}}

    monkeypatch.setattr(
        xray_bindings.subject_policy_service, "enrich_subject_with_effective_state", enrich
    )
    monkeypatch.setattr(
        xray_bindings, "_build_binding_for_subject",
        lambda subject: {"subject_id": subject["subject_id"], "client_email": subject["detail"].get("email")},
    )
    monkeypatch.setattr(xray_bindings, "_annotate_bindings_with_handoff", lambda bindings: bindings)
    return enriched


def _install_projection_policy(monkeypatch) -> None:
    routing = {
        "desired_mode": "vpn", "applied_mode": "vpn", "selective_default": "direct",
        "server_mode": "auto", "desired_fixed_server_id": None,
        "applied_fixed_server_id": None, "active_auto_server_id": "synthetic-active",
        "apply_state": "clean",
    }
    enforcement = {
        "supported_modes": {"direct": True, "selective": True, "vpn": True},
        "dataplane_capability": "synthetic", "enforcement_level": "synthetic",
        "traffic_enforcement_guaranteed": False, "missing_runtime_requirements": [],
        "profile": "synthetic", "bypass_active": False,
    }
    bypass = {"enabled": False, "active": False}
    monkeypatch.setattr(xray_bindings.subject_policy_service, "get_routing_snapshot", lambda: routing)
    monkeypatch.setattr(xray_bindings.subject_policy_service, "build_runtime_enforcement_state", lambda: enforcement)
    monkeypatch.setattr(xray_bindings.subject_policy_service, "get_core_bypass_state", lambda: bypass)
    monkeypatch.setattr(
        xray_bindings.subject_policy_service, "_default_subject_runtime_enforcement",
        lambda **_kwargs: enforcement,
    )


def _add_override_projection_cases() -> None:
    with db_session() as connection:
        connection.execute(
            "UPDATE subjects SET desired_mode = 'enabled' WHERE subject_id IN (?, ?, ?)",
            ("xray:projection-0", "xray:projection-1", "xray:projection-2"),
        )
        connection.execute(
            "INSERT INTO subject_user_overrides (subject_id, override_mode, override_until, created_by) "
            "VALUES (?, 'direct', datetime('now', '+1 day'), 'test')",
            ("xray:projection-0",),
        )
        connection.execute(
            "INSERT INTO subject_user_overrides (subject_id, override_mode, override_until, created_by) "
            "VALUES (?, 'direct', datetime('now', '-1 day'), 'test')",
            ("xray:projection-1",),
        )
        connection.execute(
            "INSERT INTO servers (server_id, server_name, raw_json) VALUES (?, ?, ?)",
            ("synthetic-server", "Synthetic server", json.dumps({"name": "synthetic-runtime"})),
        )
        connection.execute(
            "INSERT INTO subject_server_overrides (subject_id, selected_server_id, selected_until) "
            "VALUES (?, ?, datetime('now', '+1 day'))",
            ("xray:projection-2", "synthetic-server"),
        )


def test_collectors_exclude_managed_prefixes_before_subject_enrichment(monkeypatch, tmp_path) -> None:
    subject_ids = _seed_projection_subjects(monkeypatch, tmp_path)
    enriched = _install_projection_spies(monkeypatch)
    prefixes = ("sub-", "vpn-auto-")

    bindings = xray_bindings.collect_xray_runtime_bindings(exclude_client_email_prefixes=prefixes)
    assert {binding["subject_id"] for binding in bindings} == set(subject_ids[-2:])
    assert set(enriched) == set(subject_ids[-2:])

    enriched.clear()
    modes = xray_bindings.collect_xray_client_mode_directives(exclude_client_email_prefixes=prefixes)
    assert {mode["subject_id"] for mode in modes} == set(subject_ids[-2:])
    assert set(enriched) == set(subject_ids[-2:])

    # Default callers preserve their original full-inventory semantics.
    enriched.clear()
    all_bindings = xray_bindings.collect_xray_runtime_bindings()
    assert len(all_bindings) == 82
    assert len(enriched) == 82
    enriched.clear()
    all_modes = xray_bindings.collect_xray_client_mode_directives()
    assert len(all_modes) == 82
    assert len(enriched) == 82


@pytest.mark.parametrize("extra_email", [None, 123])
def test_collectors_keep_rows_with_missing_or_non_string_client_email_when_filtering(
    monkeypatch, tmp_path, extra_email,
) -> None:
    subject_ids = _seed_projection_subjects(monkeypatch, tmp_path, extra_email=extra_email)
    enriched = _install_projection_spies(monkeypatch)
    prefixes = ("sub-", "vpn-auto-")

    expected = {subject_ids[-3], subject_ids[-2], subject_ids[-1]}
    bindings = xray_bindings.collect_xray_runtime_bindings(exclude_client_email_prefixes=prefixes)
    assert {binding["subject_id"] for binding in bindings} == expected
    assert set(enriched) == expected

    enriched.clear()
    modes = xray_bindings.collect_xray_client_mode_directives(exclude_client_email_prefixes=prefixes)
    assert {mode["subject_id"] for mode in modes} == expected
    assert set(enriched) == expected


def test_collectors_batch_subject_and_override_reads_without_cross_call_cache(
    monkeypatch, tmp_path,
) -> None:
    subject_ids = _seed_projection_subjects(monkeypatch, tmp_path)
    _add_override_projection_cases()
    legacy_id = "xray:legacy-projection"
    legacy_detail = {
        "client_uuid": "legacy-uuid", "client_id": "legacy-id",
        "email": "legacy-client@example.test", "enabled": True,
    }
    with db_session() as connection:
        connection.execute(
            "INSERT INTO subjects (subject_id, subject_type, subject_role, implementation_kind, "
            "stable_key, desired_mode, is_active, is_deleted, metadata_json) "
            "VALUES (?, 'xray', 'vless_client', 'xray', ?, 'direct', 1, 0, ?)",
            (legacy_id, legacy_id, json.dumps({"detail": legacy_detail})),
        )
    _install_projection_policy(monkeypatch)
    enriched_overrides: dict[str, tuple[Any, Any]] = {}
    loaded_subjects: dict[str, tuple[Any, Any, Any]] = {}

    def enrich(subject, *, user_override=None, server_override=None, **_kwargs):
        subject_id = str(subject["subject_id"])
        enriched_overrides[subject_id] = (user_override, server_override)
        loaded_subjects[subject_id] = (
            subject.get("alias"),
            subject.get("updated_at"),
            subject.get("detail"),
        )
        effective_mode = (
            str(user_override["override_mode"])
            if isinstance(user_override, dict)
            else str(subject.get("desired_mode") or "enabled")
        )
        return {
            **subject,
            "effective_state": {
                "effective_mode": effective_mode,
                "dataplane_path": (
                    "vpn" if effective_mode in {"enabled", "vpn", "forced_vpn"} else "direct"
                ),
                "selected_server_id": (
                    server_override.get("selected_server_id")
                    if isinstance(server_override, dict) else None
                ),
                "selected_server_source": (
                    "subject_override" if isinstance(server_override, dict) else "vpn_auto"
                ),
            },
        }

    monkeypatch.setattr(
        xray_bindings.subject_policy_service, "enrich_subject_with_effective_state", enrich
    )
    monkeypatch.setattr(
        xray_bindings, "_build_binding_for_subject",
        lambda subject: {
            "subject_id": subject["subject_id"],
            "client_email": subject["detail"].get("email"),
            "client_uuid": subject["detail"].get("client_uuid"),
            "effective_server_id": subject["effective_state"].get("selected_server_id"),
        },
    )
    monkeypatch.setattr(xray_bindings, "_annotate_bindings_with_handoff", lambda bindings: bindings)

    real_connect = db_connection.connect
    opened_connections = 0

    def counted_connect():
        nonlocal opened_connections
        opened_connections += 1
        return real_connect()

    monkeypatch.setattr(db_connection, "connect", counted_connect)
    sub_cohort_exclusions = ("vpn-auto-", "fixed-", "legacy-")
    bindings = xray_bindings.collect_xray_runtime_bindings(
        exclude_client_email_prefixes=sub_cohort_exclusions,
    )
    assert {item["subject_id"] for item in bindings} == set(subject_ids[:70])
    assert opened_connections == 3
    assert enriched_overrides[subject_ids[0]][0]["override_mode"] == "direct"
    assert enriched_overrides[subject_ids[1]][0] is None  # expired override is inactive
    assert enriched_overrides[subject_ids[2]][1]["selected_server_id"] == "synthetic-server"
    assert bindings[0]["client_email"] == "SUB-account-0@fwrouter.local"

    opened_connections = 0
    enriched_overrides.clear()
    sub_modes = xray_bindings.collect_xray_client_mode_directives(
        exclude_client_email_prefixes=sub_cohort_exclusions,
    )
    expected_sub_modes = set(subject_ids[:70]) - {subject_ids[1], subject_ids[2]}
    assert {item["subject_id"] for item in sub_modes} == expected_sub_modes
    assert opened_connections == 3
    assert enriched_overrides[subject_ids[0]][0]["override_mode"] == "direct"
    assert enriched_overrides[subject_ids[1]][0] is None
    assert enriched_overrides[subject_ids[2]][1]["selected_server_id"] == "synthetic-server"

    opened_connections = 0
    enriched_overrides.clear()
    modes = xray_bindings.collect_xray_client_mode_directives(
        exclude_client_email_prefixes=("sub-", "fixed-"),
    )
    assert {item["subject_id"] for item in modes} == set(subject_ids[70:80])
    assert opened_connections == 3, opened_connections

    # The next request sees updated persisted metadata and a physically deleted row.
    changed_detail = {
        "client_uuid": "changed-uuid", "client_id": "changed-id",
        "email": "refreshed@example.test", "enabled": True,
    }
    with db_session() as connection:
        connection.execute(
            "UPDATE subjects SET alias = ?, metadata_json = ?, updated_at = ? WHERE subject_id = ?",
            (
                "refreshed alias", json.dumps({"detail": changed_detail}),
                "2099-01-01T00:00:00Z", subject_ids[0],
            ),
        )
        connection.execute("DELETE FROM subjects WHERE subject_id = ?", (subject_ids[3],))

    opened_connections = 0
    refreshed = xray_bindings.collect_xray_runtime_bindings()
    assert opened_connections == 3
    by_id = {item["subject_id"]: item for item in refreshed}
    assert by_id[subject_ids[0]]["client_email"] == "refreshed@example.test"
    assert by_id[subject_ids[0]]["client_uuid"] == "changed-uuid"
    assert subject_ids[3] not in by_id
    assert loaded_subjects[subject_ids[0]][:2] == ("refreshed alias", "2099-01-01T00:00:00Z")
    assert by_id[legacy_id]["client_email"] == "legacy-client@example.test"
    assert by_id[legacy_id]["client_uuid"] == "legacy-uuid"


def test_collectors_chunk_override_queries_without_truncating_large_cohorts(
    monkeypatch, tmp_path,
) -> None:
    monkeypatch.setenv("FWROUTER_STATE_DIR", str(tmp_path / "large-state"))
    get_settings.cache_clear()
    initialize_database()
    subject_ids = [f"xray:bulk-{index:04d}" for index in range(1005)]
    with db_session() as connection:
        connection.executemany(
            "INSERT INTO subjects (subject_id, subject_type, subject_role, implementation_kind, "
            "stable_key, desired_mode, is_active, is_deleted, metadata_json) "
            "VALUES (?, 'explicit_external_client', 'vless_client', 'xray', ?, 'direct', 1, 0, ?)",
            [
                (
                    subject_id, subject_id,
                    json.dumps({"detail": {"client_uuid": subject_id, "client_id": subject_id}}),
                )
                for subject_id in subject_ids
            ],
        )
    _install_projection_policy(monkeypatch)
    monkeypatch.setattr(
        xray_bindings.subject_policy_service, "enrich_subject_with_effective_state",
        lambda subject, **_kwargs: {
            **subject,
            "effective_state": {"effective_mode": "direct", "dataplane_path": "direct"},
        },
    )
    monkeypatch.setattr(
        xray_bindings, "_build_binding_for_subject",
        lambda subject: {"subject_id": subject["subject_id"]},
    )
    monkeypatch.setattr(xray_bindings, "_annotate_bindings_with_handoff", lambda bindings: bindings)

    observed_chunk_sizes: list[int] = []
    user_loader = xray_bindings.subject_policy_service._load_active_user_overrides
    server_loader = xray_bindings.subject_policy_service._load_active_server_overrides

    def checked_user_loader(ids):
        observed_chunk_sizes.append(len(ids))
        assert len(ids) <= 500
        return user_loader(ids)

    def checked_server_loader(ids):
        observed_chunk_sizes.append(len(ids))
        assert len(ids) <= 500
        return server_loader(ids)

    monkeypatch.setattr(
        xray_bindings.subject_policy_service, "_load_active_user_overrides", checked_user_loader
    )
    monkeypatch.setattr(
        xray_bindings.subject_policy_service, "_load_active_server_overrides", checked_server_loader
    )
    real_connect = db_connection.connect
    opened_connections = 0

    def counted_connect():
        nonlocal opened_connections
        opened_connections += 1
        return real_connect()

    monkeypatch.setattr(db_connection, "connect", counted_connect)
    bindings = xray_bindings.collect_xray_runtime_bindings()
    assert [item["subject_id"] for item in bindings] == sorted(subject_ids)
    assert len(bindings) == 1005
    assert max(observed_chunk_sizes) == 500
    assert len(observed_chunk_sizes) == 6
    assert opened_connections == 7  # one selector plus two bulk queries per chunk
