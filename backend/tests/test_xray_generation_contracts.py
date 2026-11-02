from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest

from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import db_session, initialize_database
from fwrouter_api.services import (
    xray_admission_config_proof,
    xray_generation_checkpoint,
    xray_generation_fingerprint,
    xray_subscription_service as service,
)


def _configure_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FWROUTER_STATE_DIR", str(tmp_path / "state"))
    get_settings.cache_clear()
    initialize_database()


def _admission_checkpoint(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema": 1,
        "operation_mode": "proof_only_legacy_admission",
        "operation_id": "a" * 32,
        "phase": "proof_started",
        "candidate_digest": "b" * 64,
        "candidate_revision": 1,
        "source_fingerprint": "c" * 64,
        "selection_revision": 2,
        "selection_decision_id": "decision-2",
    }
    payload.update(overrides)
    return payload


def _write_checkpoint(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)


def test_checkpoint_reader_preserves_schema_allowlist_and_phase_contract(tmp_path: Path) -> None:
    path = tmp_path / "private" / "generation-checkpoint.json"
    payload = _admission_checkpoint(phase="proof_complete", require_native_proof=True)
    _write_checkpoint(path, payload)

    assert service._legacy_admission_checkpoint_payload(path) == payload
    assert xray_generation_checkpoint._legacy_admission_checkpoint_payload(path) == payload
    assert service._legacy_admission_checkpoint_payload(path.with_name("missing.json")) is None

    for invalid in (
        {**payload, "unapproved": "extension"},
        {**payload, "schema": True},
        {**payload, "phase": "unknown"},
        {**payload, "require_native_proof": 1},
        {**payload, "operation_id": "not-an-operation-id"},
        {**payload, "candidate_revision": True},
    ):
        _write_checkpoint(path, invalid)
        assert service._legacy_admission_checkpoint_payload(path) is None


def test_checkpoint_reader_rejects_symlink_and_unsafe_file_modes(tmp_path: Path) -> None:
    private = tmp_path / "private"
    path = private / "generation-checkpoint.json"
    _write_checkpoint(path, _admission_checkpoint())

    unsafe_parent = tmp_path / "unsafe"
    unsafe_parent.mkdir(mode=0o700)
    unsafe_path = unsafe_parent / "generation-checkpoint.json"
    _write_checkpoint(unsafe_path, _admission_checkpoint())
    unsafe_parent.chmod(0o755)
    assert service._legacy_admission_checkpoint_payload(unsafe_path) is None

    link = private / "checkpoint-link.json"
    link.symlink_to(path)
    assert service._legacy_admission_checkpoint_payload(link) is None


def test_checkpoint_mode_selection_and_fence_dto_compatibility() -> None:
    assert service._checkpoint_operation_mode({}) is None
    assert service._checkpoint_operation_mode({"operation_mode": "proof_only_legacy_admission"}) == "proof_only_legacy_admission"
    with pytest.raises(ValueError, match="XRAY_GENERATION_CHECKPOINT_MODE_UNSUPPORTED"):
        service._checkpoint_operation_mode({"operation_mode": "unrecognized"})
    with pytest.raises(ValueError, match="XRAY_GENERATION_CHECKPOINT_INVALID"):
        service._checkpoint_operation_mode(None)

    values = {
        "proof_only_source_fingerprint": "source",
        "proof_only_core_runtime_fingerprint": "core",
        "proof_only_selection_revision": 1,
        "proof_only_selection_decision_id": None,
        "proof_only_selection_snapshot": {},
        "proof_only_routing_snapshot": {},
        "proof_only_xray_incarnation": "xray",
        "proof_only_mihomo_incarnation": "mihomo",
        "proof_only_xray_config_sha256": "x" * 64,
        "proof_only_mihomo_config_sha256": "m" * 64,
        "proof_only_mihomo_fingerprint_hash": "f" * 64,
        "proof_only_core_manifest_hash": "c" * 64,
        "proof_only_core_apply_id": "apply",
        "proof_only_core_enforcement_level": "nftables",
        "proof_only_core_live_mode": "vpn",
    }
    fence = service._AdmissionFenceView(**values)
    assert fence.__dict__ == values
    assert replace(fence, proof_only_selection_revision=2).proof_only_selection_revision == 2
    assert fence.proof_only_selection_revision == 1


def test_checkpoint_selection_projection_preserves_current_and_checkpoint_bound_intent() -> None:
    current_vpn = {"routing": {"server_mode": "fixed", "desired_fixed_server_id": "server-a"}}
    assert service._checkpoint_selection_for_recovery({}, current_vpn) is current_vpn

    current_direct = {"routing": {"server_mode": "auto", "active_auto_server_id": None}}
    checkpoint_selection = {"routing": {"server_mode": "auto", "active_auto_server_id": "server-b"}}
    assert service._checkpoint_selection_for_recovery(
        {"auto_selection_after": checkpoint_selection}, current_direct,
    ) is checkpoint_selection
    assert service._checkpoint_selection_for_recovery({}, current_direct) is None


def test_config_candidate_readers_preserve_byte_identity_and_fail_closed_bounds(tmp_path: Path) -> None:
    active_xray = tmp_path / "active-xray.json"
    candidate_xray = tmp_path / "candidate-xray.json"
    xray = {
        "inbounds": [],
        "outbounds": [],
        "routing": {"rules": []},
    }
    active_xray.write_text(json.dumps(xray), encoding="utf-8")
    candidate_xray.write_text(json.dumps(xray), encoding="utf-8")
    assert service._legacy_xray_candidate_matches_active(active_xray, candidate_xray) is True
    assert xray_admission_config_proof._legacy_xray_candidate_matches_active(active_xray, candidate_xray) is True

    xray_symlink = tmp_path / "xray-link.json"
    xray_symlink.symlink_to(active_xray)
    assert service._legacy_xray_candidate_matches_active(xray_symlink, candidate_xray) is False

    active_mihomo = tmp_path / "active-mihomo.yaml"
    candidate_mihomo = tmp_path / "candidate-mihomo.yaml"
    active_mihomo.write_text("mixed-port: 7890\n", encoding="utf-8")
    candidate_mihomo.write_bytes(active_mihomo.read_bytes())
    assert service._legacy_mihomo_candidate_matches(active_mihomo, candidate_mihomo) is True
    assert xray_admission_config_proof._legacy_mihomo_candidate_matches(active_mihomo, candidate_mihomo) is True
    candidate_mihomo.write_text("mixed-port: [\n", encoding="utf-8")
    assert service._legacy_mihomo_candidate_matches(active_mihomo, candidate_mihomo) is False


def test_source_fingerprint_session_boundaries_and_legacy_callback_seam(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _configure_db(monkeypatch, tmp_path)
    with db_session() as connection:
        expected_generation = service._generation_source_fingerprint(connection)
        expected_legacy = service._legacy_admission_source_fingerprint(connection)
        assert expected_generation == "65c1793f3450c8a58a289a2c3e19cbdab4b3ea1015eba7b29403c806f9cf354d"
        assert expected_legacy == "1c7aee0c0c29abd443bba0c1a5820d08ae5b87eb9a60ca9be1982b8ae154a4e7"
        assert xray_generation_fingerprint.generation_source_fingerprint(connection) == expected_generation
        assert xray_generation_fingerprint.legacy_admission_source_fingerprint(
            connection, generation_fingerprint=lambda _connection: expected_generation,
        ) == expected_legacy

    assert service._generation_source_fingerprint() == expected_generation
    assert service._legacy_admission_source_fingerprint() == expected_legacy

    with db_session() as connection:
        monkeypatch.setattr(service, "_generation_source_fingerprint", lambda _connection=None: "patched-source")
        legacy = service._legacy_admission_source_fingerprint(connection)
        assert legacy != expected_legacy


def test_source_hashing_stays_outside_owned_session_after_projection_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    session_active = False

    class EmptyCursor:
        def fetchall(self) -> list[object]:
            return []

        def __iter__(self):
            return iter(())

    class ReadConnection:
        def execute(self, _query: str) -> EmptyCursor:
            events.append("read")
            return EmptyCursor()

    @contextmanager
    def owned_session():
        nonlocal session_active
        events.append("enter")
        session_active = True
        try:
            yield ReadConnection()
        except Exception:
            events.append("rollback")
            raise
        else:
            events.append("commit")
        finally:
            session_active = False
            events.append("exit")

    hash_count = 0

    def fail_during_serialization(_projection: list[object]) -> str:
        nonlocal hash_count
        hash_count += 1
        events.append("hash-in-session" if session_active else "hash-after-session")
        raise ValueError("serialization failure")

    monkeypatch.setattr(service, "db_session", owned_session)
    monkeypatch.setattr(xray_generation_fingerprint, "source_projection_sha256", fail_during_serialization)
    with pytest.raises(ValueError, match="serialization failure"):
        service._generation_source_fingerprint()
    assert events[-3:] == ["commit", "exit", "hash-after-session"]
    assert "rollback" not in events

    events.clear()
    hash_count = 0

    def fail_on_legacy_serialization(projection: list[object]) -> str:
        nonlocal hash_count
        hash_count += 1
        events.append("hash-in-session" if session_active else "hash-after-session")
        if hash_count == 1:
            return "generation-fingerprint"
        raise ValueError("serialization failure")

    monkeypatch.setattr(xray_generation_fingerprint, "source_projection_sha256", fail_on_legacy_serialization)
    with pytest.raises(ValueError, match="serialization failure"):
        service._legacy_admission_source_fingerprint()
    assert events.index("hash-in-session") < events.index("commit") < events.index("exit")
    assert events[-3:] == ["commit", "exit", "hash-after-session"]
    assert events.count("rollback") == 0


def test_core_projection_and_identity_reference_keep_baseline_golden_vectors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _configure_db(monkeypatch, tmp_path)
    with db_session() as connection:
        assert xray_generation_fingerprint.legacy_admission_core_runtime_snapshot(connection) == (
            service._legacy_admission_core_runtime_snapshot(connection)
        )
        assert service._legacy_admission_core_runtime_snapshot(connection) == [
            {"apply_state": "clean", "desired_state": "enabled", "lifecycle_mode": "managed",
             "module_name": "core", "runtime_state": "not_configured"},
            {"apply_state": "clean", "desired_state": "enabled", "lifecycle_mode": "managed",
             "module_name": "selector", "runtime_state": "not_configured"},
            {"apply_state": "clean", "desired_state": "enabled", "lifecycle_mode": "managed",
             "module_name": "vpn", "runtime_state": "not_configured"},
            {"apply_state": "clean", "desired_state": "enabled", "lifecycle_mode": "managed",
             "module_name": "watchdog", "runtime_state": "not_configured"},
            {"core_intent_revision": 0},
        ]
        assert service._legacy_admission_core_runtime_fingerprint(connection) == (
            "70cdcf04d46eaa37fe327cbd2718c8ff2b46973c55351dc3959287cfdf91a8e2"
        )
        assert service._identity_projection_reference_sha256(connection) == (
            "f7260846cc475e0306e3f2449c3599f0e767b480107e21c86535b99bbd22932c"
        )


def test_identity_projection_reference_hash_tracks_subject_and_snapshot_rows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _configure_db(monkeypatch, tmp_path)
    with db_session() as connection:
        baseline = service._identity_projection_reference_sha256(connection)
        connection.execute(
            "INSERT INTO subjects (subject_id, subject_type, implementation_kind, stable_key, metadata_json, desired_mode) "
            "VALUES ('xray-ref', 'client', 'xray', 'stable-ref', '{}', 'vpn')"
        )
        changed_subject = service._identity_projection_reference_sha256(connection)
        account = connection.execute(
            "INSERT INTO subscription_accounts (slug, display_name) VALUES ('profile-ref', 'Profile ref')"
        ).lastrowid
        connection.execute(
            "INSERT INTO subscription_clients (account_id, token) VALUES (?, 'profile-ref')", (account,),
        )
        connection.execute(
            "INSERT INTO subscription_profile_snapshots "
            "(token, nodes_json, runtime_verified_at, updated_at) VALUES ('profile-ref', '[]', 't1', 't1')"
        )
        changed_snapshot = service._identity_projection_reference_sha256(connection)
        assert changed_subject != baseline
        assert changed_snapshot != changed_subject


@pytest.mark.parametrize("changed_name", [
    "xray_admission_config_proof.py", "xray_generation_checkpoint.py",
    "xray_generation_fingerprint.py", "xray_profile_planning.py",
    "xray_generation_observation.py", "xray_generation_views.py",
    "xray_generation_staging.py", "module_attribute_aliases.py", "artifacts.py",
])
def test_extracted_executable_bytes_remain_in_mihomo_source_fence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, changed_name: str,
) -> None:
    import hashlib
    from fwrouter_api.services import mihomo_reconcile_fingerprint as fingerprint

    service_dir = tmp_path / "services"
    service_dir.mkdir()
    names = (
        "xray_admission_config_proof.py", "xray_generation_checkpoint.py",
        "xray_generation_fingerprint.py", "xray_profile_planning.py",
        "xray_generation_observation.py", "xray_generation_views.py",
        "xray_generation_staging.py", "module_attribute_aliases.py", "artifacts.py",
    )
    for name in names:
        (service_dir / name).write_bytes(b"initial-source-bytes")
    monkeypatch.setattr(fingerprint, "__file__", str(service_dir / "mihomo_reconcile_fingerprint.py"))
    before = fingerprint._source_fingerprint()
    for name in names:
        assert before[name] == hashlib.sha256(b"initial-source-bytes").hexdigest()
    (service_dir / changed_name).write_bytes(b"changed-source-bytes")
    after = fingerprint._source_fingerprint()
    assert after[changed_name] == hashlib.sha256(b"changed-source-bytes").hexdigest()
    assert {name for name in before if before[name] != after[name]} == {changed_name}
