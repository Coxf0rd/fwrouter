from __future__ import annotations

import json
from typing import Any

import pytest

from fwrouter_api.core.config import get_settings
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


def _install_projection_spies(monkeypatch, subject_ids: list[str], *, extra_email: Any = ...) -> list[str]:
    enriched: list[str] = []
    subjects = {}
    for index, subject_id in enumerate(subject_ids):
        email = (
            ("SUB-account-0@fwrouter.local" if index == 0 else f"sub-account-{index}@fwrouter.local") if index < 70
            else ("VPN-auto-0@fwrouter.local" if index == 70 else f"vpn-auto-{index - 70}@fwrouter.local") if index < 80
            else ("fixed-one@example.test" if index == 80 else "fixed-two@example.test")
            if index < 82 else extra_email
        )
        detail = {"client_uuid": f"00000000-0000-4000-8000-{index:012d}",
                  "client_id": f"00000000-0000-4000-8000-{index:012d}"}
        if email:
            detail["email"] = email
        subjects[subject_id] = {"subject_id": subject_id, "desired_mode": "direct", "detail": detail}

    monkeypatch.setattr(xray_bindings, "get_subject", lambda subject_id: subjects.get(subject_id))
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

    monkeypatch.setattr(xray_bindings.subject_policy_service, "enrich_subject_with_effective_state", enrich)
    monkeypatch.setattr(
        xray_bindings, "_build_binding_for_subject",
        lambda subject: {"subject_id": subject["subject_id"], "client_email": subject["detail"].get("email")},
    )
    monkeypatch.setattr(xray_bindings, "_annotate_bindings_with_handoff", lambda bindings: bindings)
    return enriched


def test_collectors_exclude_managed_prefixes_before_subject_enrichment(monkeypatch, tmp_path) -> None:
    subject_ids = _seed_projection_subjects(monkeypatch, tmp_path)
    enriched = _install_projection_spies(monkeypatch, subject_ids)
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
    enriched = _install_projection_spies(monkeypatch, subject_ids, extra_email=extra_email)
    prefixes = ("sub-", "vpn-auto-")

    expected = {subject_ids[-3], subject_ids[-2], subject_ids[-1]}
    bindings = xray_bindings.collect_xray_runtime_bindings(exclude_client_email_prefixes=prefixes)
    assert {binding["subject_id"] for binding in bindings} == expected
    assert set(enriched) == expected

    enriched.clear()
    modes = xray_bindings.collect_xray_client_mode_directives(exclude_client_email_prefixes=prefixes)
    assert {mode["subject_id"] for mode in modes} == expected
    assert set(enriched) == expected
