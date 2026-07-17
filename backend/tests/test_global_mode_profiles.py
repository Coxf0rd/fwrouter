from __future__ import annotations

import json
from pathlib import Path

from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import db_session
from fwrouter_api.db.connection import initialize_database
from fwrouter_api.services.global_mode_profiles import (
    build_global_profile_source_stamp,
    compile_all_global_mode_profiles,
    compile_global_mode_profile,
    load_precompiled_global_mode_profile,
)
from fwrouter_api.services.live_probe_cache import clear_live_probe_cache


def _configure_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FWROUTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("FWROUTER_DATABASE_URL", f"sqlite:///{tmp_path}/fwrouter.db")
    get_settings.cache_clear()
    clear_live_probe_cache()


def test_compile_and_load_precompiled_global_profile(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    monkeypatch.setattr(
        "fwrouter_api.services.global_mode_profiles.read_effective_rules_artifact",
        lambda: {"rules": [], "selective_default": "direct"},
    )
    monkeypatch.setattr(
        "fwrouter_api.services.global_mode_profiles.get_core_bypass_state",
        lambda: {"enabled": False, "reason": "disabled", "status": "idle"},
    )
    monkeypatch.setattr(
        "fwrouter_api.services.global_mode_profiles.list_subjects",
        lambda **kwargs: [
            {
                "subject_id": "lan:1",
                "subject_type": "lan",
                "display_name": "LAN 1",
                "desired_mode": "global",
                "applied_mode": "global",
                "runtime_state": "active",
                "is_active": 1,
                "is_deleted": 0,
                "visibility": "ui",
            }
        ],
    )
    monkeypatch.setattr(
        "fwrouter_api.services.global_mode_profiles._load_active_user_overrides",
        lambda ids: {},
    )
    monkeypatch.setattr(
        "fwrouter_api.services.global_mode_profiles._load_active_server_overrides",
        lambda ids: {},
    )
    monkeypatch.setattr(
        "fwrouter_api.services.global_mode_profiles.enrich_subject_with_effective_state",
        lambda subject, **kwargs: {
            **subject,
            "effective_state": {
                "mode_source": "global",
                "scoped_runtime": {"status": "applied"},
            },
        },
    )
    monkeypatch.setattr(
        "fwrouter_api.services.global_mode_profiles.build_dataplane_manifest_from_state",
        lambda **kwargs: {
            "contract_version": "v1",
            "plan_id": kwargs["plan_id"],
            "reason": kwargs["reason"],
            "generated_at": "2026-01-01T00:00:00Z",
            "routing_global_state": kwargs["routing"],
            "runtime_enforcement": {"dataplane_capability": "nft_owned_table"},
            "summary": {"subjects_count": 1, "path_counts": {"selective": 1}, "extra_keys": []},
            "scoped_egress": {},
            "global_preflight": {"missing": [], "profile": {"profile": "global_v1"}},
            "extra": {},
            "subjects": [],
            "owned_table": "inet fwrouter_v2",
            "required_chains": [],
        },
    )

    compiled = compile_global_mode_profile("selective", routing={"selective_default": "direct", "server_mode": "auto"})
    loaded = load_precompiled_global_mode_profile(
        "selective",
        routing={"selective_default": "direct", "server_mode": "auto"},
    )
    meta_path = get_settings().paths.generated_dir / "dataplane" / "profiles" / "selective.meta.json"

    assert compiled["target_mode"] == "selective"
    assert compiled["affected_subject_ids"] == ["lan:1"]
    assert meta_path.exists()
    assert loaded is not None
    assert loaded["target_mode"] == "selective"


def test_precompiled_global_profile_rejects_stale_sidecar(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    profile_dir = get_settings().paths.generated_dir / "dataplane" / "profiles"
    profile_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "target_mode": "direct",
        "source_stamp": {"routing": {"selective_default": "direct"}},
        "manifest": {"routing_global_state": {"desired_mode": "direct", "applied_mode": "direct"}},
    }
    (profile_dir / "direct.json").write_text(json.dumps(payload), encoding="utf-8")
    (profile_dir / "direct.meta.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "target_mode": "direct",
                "source_stamp": {"routing": {"selective_default": "vpn"}},
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "fwrouter_api.services.global_mode_profiles.build_global_profile_source_stamp",
        lambda routing=None: {"routing": {"selective_default": "direct"}},
    )

    assert load_precompiled_global_mode_profile("direct", routing={"selective_default": "direct"}) is None


def test_precompiled_global_profile_invalidates_on_source_stamp_change(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    profile_dir = get_settings().paths.generated_dir / "dataplane" / "profiles"
    profile_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "target_mode": "direct",
        "source_stamp": {"routing": {"selective_default": "direct"}},
        "manifest": {"routing_global_state": {"desired_mode": "direct", "applied_mode": "direct"}},
    }
    (profile_dir / "direct.json").write_text(json.dumps(payload), encoding="utf-8")

    monkeypatch.setattr(
        "fwrouter_api.services.global_mode_profiles.build_global_profile_source_stamp",
        lambda routing=None: {"routing": {"selective_default": "vpn"}},
    )

    assert load_precompiled_global_mode_profile("direct", routing={"selective_default": "direct"}) is None


def test_global_profile_stamp_ignores_ui_and_status_churn(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    monkeypatch.setattr(
        "fwrouter_api.services.global_mode_profiles.read_effective_rules_artifact",
        lambda: {"rules": []},
    )
    monkeypatch.setattr(
        "fwrouter_api.services.global_mode_profiles.get_core_bypass_state",
        lambda: {"enabled": False, "reason": "disabled", "status": "idle"},
    )

    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO subjects (
                subject_id, subject_type, stable_key, display_name, alias,
                desired_mode, applied_mode, runtime_state, is_active
            )
            VALUES ('lan:1', 'lan', 'aa:bb', 'old name', 'old alias', 'global', 'global', 'active', 1)
            """
        )
        connection.execute(
            """
            INSERT INTO subject_lan (subject_id, mac_address, ip_address, hostname)
            VALUES ('lan:1', 'aa:bb', '192.168.0.10', 'phone')
            """
        )
        connection.execute(
            """
            INSERT INTO subject_server_overrides (
                subject_id, selected_server_id, apply_state, error_code, error_message
            )
            VALUES ('lan:1', NULL, 'clean', NULL, NULL)
            """
        )

    before = build_global_profile_source_stamp(routing={"selective_default": "direct", "server_mode": "auto"})

    with db_session() as connection:
        connection.execute(
            """
            UPDATE subjects
            SET display_name = 'new name',
                alias = 'new alias',
                last_seen_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE subject_id = 'lan:1'
            """
        )
        connection.execute(
            """
            UPDATE subject_server_overrides
            SET apply_state = 'failed',
                error_code = 'TEST',
                error_message = 'status only',
                updated_at = CURRENT_TIMESTAMP
            WHERE subject_id = 'lan:1'
            """
        )

    after = build_global_profile_source_stamp(routing={"selective_default": "direct", "server_mode": "auto"})

    assert after == before


def test_global_profile_stamp_changes_on_routing_relevant_subject_change(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    monkeypatch.setattr(
        "fwrouter_api.services.global_mode_profiles.read_effective_rules_artifact",
        lambda: {"rules": []},
    )
    monkeypatch.setattr(
        "fwrouter_api.services.global_mode_profiles.get_core_bypass_state",
        lambda: {"enabled": False, "reason": "disabled", "status": "idle"},
    )

    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO subjects (
                subject_id, subject_type, stable_key, desired_mode, applied_mode, runtime_state, is_active
            )
            VALUES ('lan:1', 'lan', 'aa:bb', 'global', 'global', 'active', 1)
            """
        )
        connection.execute(
            """
            INSERT INTO subject_lan (subject_id, mac_address, ip_address)
            VALUES ('lan:1', 'aa:bb', '192.168.0.10')
            """
        )

    before = build_global_profile_source_stamp(routing={"selective_default": "direct", "server_mode": "auto"})

    with db_session() as connection:
        connection.execute(
            "UPDATE subjects SET desired_mode = 'direct', updated_at = CURRENT_TIMESTAMP WHERE subject_id = 'lan:1'"
        )

    after = build_global_profile_source_stamp(routing={"selective_default": "direct", "server_mode": "auto"})

    assert after != before


def test_compile_all_global_mode_profiles_writes_all_modes(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    calls: list[str] = []

    monkeypatch.setattr(
        "fwrouter_api.services.global_mode_profiles.compile_global_mode_profile",
        lambda mode, routing=None: calls.append(mode) or {"target_mode": mode},
    )

    result = compile_all_global_mode_profiles(routing={"server_mode": "auto"})

    assert sorted(calls) == ["direct", "selective", "vpn"]
    assert sorted(result.keys()) == ["direct", "selective", "vpn"]
