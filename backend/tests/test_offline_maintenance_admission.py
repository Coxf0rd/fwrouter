from __future__ import annotations

import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import uuid

import pytest

from fwrouter_api.services import maintenance_lifecycle_admission as admission
from fwrouter_api.services import database_admin


def _installer_lifecycle_module():
    repo_root = Path(__file__).resolve().parents[2]
    path = repo_root / "installer" / "controlled_backend_lifecycle.py"
    spec = importlib.util.spec_from_file_location("controlled_backend_lifecycle_offline_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


lifecycle = _installer_lifecycle_module()


def _fixture(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, state_root: Path | None = None,
):
    state_root = state_root or (tmp_path / ".fwrouter-maintenance-test-state")
    state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    rollout = state_root / "state" / "controlled-rollout"
    rollout.mkdir(mode=0o700, parents=True)
    for directory in (state_root, state_root / "state", rollout):
        directory.chmod(0o700)
    monkeypatch.setattr(admission, "_trusted_uid", os.geteuid)
    monkeypatch.setattr(admission, "_require_units_stopped", lambda: None)
    operation_id = str(uuid.uuid4())
    checkpoint = {
        "operation_id": operation_id,
        "status": "pending",
        "maintenance_operation": {"kind": "database_rebuild", "status": "pending", "backup": None},
    }
    checkpoint_path = rollout / "pending.json"
    checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")
    checkpoint_path.chmod(0o600)
    lifecycle_path = rollout / ".lock"
    lifecycle_fd = os.open(lifecycle_path, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(lifecycle_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    maintenance_path = rollout / ".maintenance.lock"
    maintenance_fd = os.open(maintenance_path, os.O_CREAT | os.O_RDWR, 0o600)
    return state_root, operation_id, lifecycle_fd, maintenance_fd, checkpoint_path


def _pin_synthetic_test_backup(state_root: Path, operation_id: str) -> None:
    backup_dir = state_root / "backups"
    backup_dir.mkdir(mode=0o700, exist_ok=True)
    backup_dir.chmod(0o700)
    filename = f"fwrouter.db.rebuild-{uuid.UUID(operation_id).hex}.20261027T000000Z.{'a' * 32}.bak"
    path = backup_dir / filename
    path.write_bytes(b"test-only lifecycle pin; not a SQLite backup")
    path.chmod(0o600)
    admission.record_offline_rebuild_backup_pin(filename, hashlib.sha256(path.read_bytes()).hexdigest())


def test_offline_lease_binds_both_lifecycle_and_exclusive_lock_and_revokes_context(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    state_root, operation_id, lifecycle_fd, maintenance_fd, checkpoint_path = _fixture(
        monkeypatch, tmp_path,
    )
    copied = None
    try:
        with admission.offline_rebuild_lease(
            maintenance_fd, lifecycle_fd, state_root=state_root,
        ) as admitted_operation:
            assert admitted_operation == operation_id
            assert admission.offline_rebuild_is_held()
            from contextvars import copy_context

            copied = copy_context()
            _pin_synthetic_test_backup(state_root, operation_id)
            admission.record_offline_rebuild_phase("candidate_ready")
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            assert checkpoint["maintenance_operation"]["status"] == "candidate_ready"
        assert not admission.offline_rebuild_is_held()
        assert copied is not None
        assert copied.run(admission.offline_rebuild_is_held) is False
    finally:
        os.close(maintenance_fd)
        os.close(lifecycle_fd)


def test_offline_phase_ignores_unpublished_stale_temporary_checkpoints(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    state_root, operation_id, lifecycle_fd, maintenance_fd, checkpoint_path = _fixture(
        monkeypatch, tmp_path,
    )
    rollout = checkpoint_path.parent
    legacy_orphan = rollout / f".pending-{operation_id}.tmp"
    nonce_orphan = rollout / f".pending-{operation_id}-deadbeef.tmp"
    for path in (legacy_orphan, nonce_orphan):
        path.write_text("unpublished orphan", encoding="utf-8")
        path.chmod(0o600)
    try:
        with admission.offline_rebuild_lease(
            maintenance_fd, lifecycle_fd, state_root=state_root,
        ):
            _pin_synthetic_test_backup(state_root, operation_id)
            admission.record_offline_rebuild_phase("candidate_ready")
        assert json.loads(checkpoint_path.read_text(encoding="utf-8"))[
            "maintenance_operation"]["status"] == "candidate_ready"
        assert legacy_orphan.read_text(encoding="utf-8") == "unpublished orphan"
        assert nonce_orphan.read_text(encoding="utf-8") == "unpublished orphan"
    finally:
        os.close(maintenance_fd)
        os.close(lifecycle_fd)


def test_shared_runtime_holder_excludes_offline_rebuild_without_authorizing_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    state_root, _operation_id, lifecycle_fd, maintenance_fd, _checkpoint_path = _fixture(
        monkeypatch, tmp_path,
    )
    shared_fd = os.open(
        state_root / "state" / "controlled-rollout" / ".maintenance.lock",
        os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        fcntl.flock(shared_fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        with pytest.raises(admission.MaintenanceAdmissionError, match="another supported FWRouter process"):
            with admission.offline_rebuild_lease(
                maintenance_fd, lifecycle_fd, state_root=state_root,
            ):
                pytest.fail("shared process holder must prevent offline admission")
        assert not admission.offline_rebuild_is_held()
    finally:
        os.close(shared_fd)
        os.close(maintenance_fd)
        os.close(lifecycle_fd)


def test_pending_rebuild_blocks_startup_until_lifecycle_marks_verified(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    state_root, operation_id, lifecycle_fd, maintenance_fd, checkpoint_path = _fixture(
        monkeypatch, tmp_path,
    )
    try:
        assert admission.startup_allowed(state_root) is False
        with admission.offline_rebuild_lease(
            maintenance_fd, lifecycle_fd, state_root=state_root,
        ):
            _pin_synthetic_test_backup(state_root, operation_id)
            admission.record_offline_rebuild_phase("candidate_ready")
            admission.record_offline_rebuild_phase("source_quiesced")
            admission.record_offline_rebuild_phase("published")
            assert admission.startup_allowed(state_root) is False
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        checkpoint["maintenance_operation"]["status"] = "verified"
        checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")
        checkpoint_path.chmod(0o600)
        assert admission.startup_allowed(state_root) is True
    finally:
        os.close(maintenance_fd)
        os.close(lifecycle_fd)


def test_legacy_direct_uvicorn_unit_is_rejected_by_fixed_systemctl_preflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        return type("Result", (), {
            "returncode": 0,
            "stdout": "{ path=/opt/fwrouter-api/.venv/bin/python ; argv[]=/opt/fwrouter-api/.venv/bin/python -m uvicorn fwrouter_api.main:app }",
            "stderr": "",
        })()

    monkeypatch.setattr(lifecycle.subprocess, "run", fake_run)
    with pytest.raises(lifecycle.LifecycleError, match="does not hold the shared maintenance lease"):
        lifecycle._require_maintenance_compatible_api_unit()
    assert calls == [[
        "/usr/bin/systemctl", "show", "--property=ExecStart", "--value",
        lifecycle.API_UNIT,
    ]]


def test_fixed_run_api_launcher_is_accepted_by_unit_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lifecycle.subprocess, "run", lambda *_args, **_kwargs: type("Result", (), {
        "returncode": 0,
        "stdout": "{ path=/opt/fwrouter-api/.venv/bin/python ; argv[]=/opt/fwrouter-api/.venv/bin/python -m fwrouter_api_maintenance run-api }",
        "stderr": "",
    })())
    lifecycle._require_maintenance_compatible_api_unit()


def test_database_rebuild_rejects_redirected_lifecycle_state_before_host_actions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(lifecycle.os, "geteuid", lambda: 0)
    monkeypatch.setattr(lifecycle, "DEFAULT_STATE_DIR", Path("/var/lib/fwrouter-v2/state/controlled-rollout"))
    with pytest.raises(lifecycle.LifecycleError, match="state directory is fixed"):
        lifecycle.run_command([
            "rebuild-db", "--file-path", str(tmp_path / "snapshot.json"),
            "--state-dir", str(tmp_path / "redirected"),
        ])


@pytest.mark.no_database_autoinit
def test_rebuild_rejects_mismatched_operation_id_before_snapshot_resolution_or_writes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from fwrouter_api.core.config import get_settings
    from fwrouter_api.db.connection import get_db_path

    state_root = tmp_path / "state"
    monkeypatch.setenv("FWROUTER_STATE_DIR", str(state_root))
    get_settings.cache_clear()
    state_root.mkdir(mode=0o700)
    _state_root, operation_id, lifecycle_fd, maintenance_fd, checkpoint_path = _fixture(
        monkeypatch, tmp_path, state_root=state_root,
    )
    checkpoint_before = checkpoint_path.read_bytes()
    monkeypatch.setattr(
        database_admin, "resolve_control_plane_snapshot_source",
        lambda **_kwargs: pytest.fail("mismatched operation identity must be rejected before resolution"),
    )
    try:
        with admission.offline_rebuild_lease(
            maintenance_fd, lifecycle_fd, state_root=state_root,
        ):
            result = database_admin.rebuild_control_plane_database(
                snapshot={"synthetic": True}, operation_id=uuid.uuid4().hex,
            )
        assert result["ok"] is False
        assert result["stage"] == "maintenance_admission"
        assert result["error"]["code"] == "DATABASE_REBUILD_OPERATION_ID_MISMATCH"
        assert checkpoint_path.read_bytes() == checkpoint_before
        assert not get_db_path().exists()
        assert not (state_root / "backups").exists()
        assert not (state_root / f".maintenance-rebuild-{uuid.UUID(operation_id).hex}").exists()
    finally:
        os.close(maintenance_fd)
        os.close(lifecycle_fd)


@pytest.mark.no_database_autoinit
def test_qualified_candidate_rebuild_and_published_retry_use_owned_sqlite_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from fwrouter_api.core.config import get_settings
    from fwrouter_api.db.connection import get_db_path, initialize_database
    from fwrouter_api.services.control_plane_transfer import export_control_plane_snapshot

    state_root = tmp_path / "state"
    monkeypatch.setenv("FWROUTER_STATE_DIR", str(state_root))
    get_settings.cache_clear()
    state_root.mkdir(mode=0o700)
    _state_root, operation_id, lifecycle_fd, maintenance_fd, checkpoint_path = _fixture(
        monkeypatch, tmp_path, state_root=state_root,
    )
    monkeypatch.setattr(
        "fwrouter_api.adapters.xray_common._default_xray_config_path",
        lambda: str(tmp_path / "owned-xray" / "config.json"),
    )
    monkeypatch.setattr(
        database_admin, "reconcile_control_plane_runtime",
        lambda **_kwargs: {"subject_inventory": {"ok": True, "synthetic": "not_native_proof"}},
    )
    initialize_database()
    exported = export_control_plane_snapshot(include_secrets=False, write_file=False)
    assert exported["snapshot"]["snapshot_version"]
    source_snapshot = exported["snapshot"]
    db_path = get_db_path()
    connection = sqlite3.connect(db_path)
    # Exercise real publication while recording its boundary operations: no
    # logical scan/file interpretation may follow the final raw backup hash.
    events = []
    publishing = False
    real_publish = database_admin._atomic_publish_candidate
    real_logical = database_admin._logical_database_digest
    real_hash = database_admin._hash_regular_file
    real_replace = database_admin.os.replace

    def trace_publish(*args, **kwargs):
        nonlocal publishing
        publishing = True
        try:
            return real_publish(*args, **kwargs)
        finally:
            publishing = False

    def trace_logical(*args, **kwargs):
        if publishing:
            events.append('logical')
        return real_logical(*args, **kwargs)

    def trace_hash(*args, **kwargs):
        if publishing:
            events.append('hash')
        return real_hash(*args, **kwargs)

    def trace_replace(*args, **kwargs):
        if publishing:
            events.append('replace')
        return real_replace(*args, **kwargs)

    monkeypatch.setattr(database_admin, '_atomic_publish_candidate', trace_publish)
    monkeypatch.setattr(database_admin, '_logical_database_digest', trace_logical)
    monkeypatch.setattr(database_admin, '_hash_regular_file', trace_hash)
    monkeypatch.setattr(database_admin.os, 'replace', trace_replace)

    try:
        connection.execute(
            "UPDATE settings SET value_json='41' WHERE key='core.intent_revision'",
        )
        connection.commit()
    finally:
        connection.close()
    source_snapshot = export_control_plane_snapshot(include_secrets=False, write_file=False)["snapshot"]

    try:
        with admission.offline_rebuild_lease(
            maintenance_fd, lifecycle_fd, state_root=state_root,
        ):
            first = database_admin.rebuild_control_plane_database(
                snapshot=source_snapshot, operation_id=operation_id,
            )
            assert first["ok"] is True
            assert events == ['logical', 'logical', 'hash', 'replace']
            assert first["persistence_status"] == "published"
            assert first["inventory_reconcile_status"] == "complete"
            assert first["native_proof_status"] == "not_provided"
            assert first["operation_id"] == operation_id.replace("-", "")
            assert Path(first["backup"]["backup_path"]).is_file()
            assert database_admin._validate_database_file(db_path)["ok"] is True
            backup_set = set((state_root / "backups").glob("*.bak"))

            # A retry after publication uses the durable import receipt and
            # does not republish or create a second backup.
            published_digest = database_admin._logical_database_digest(db_path)
            retry = database_admin.rebuild_control_plane_database(
                snapshot=source_snapshot, operation_id=operation_id,
            )
            assert retry["ok"] is True
            assert retry["import"]["replayed"] is True
            assert database_admin._logical_database_digest(db_path) == published_digest
            connection = sqlite3.connect(db_path)
            try:
                intent_revision = json.loads(connection.execute(
                    "SELECT value_json FROM settings WHERE key='core.intent_revision'",
                ).fetchone()[0])
            finally:
                connection.close()
            assert intent_revision == 42
            assert set((state_root / "backups").glob("*.bak")) == backup_set
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        assert checkpoint["maintenance_operation"]["status"] == "published"
        assert not Path(f"{db_path}-wal").exists()
        assert not Path(f"{db_path}-shm").exists()
    finally:
        os.close(maintenance_fd)
        os.close(lifecycle_fd)


@pytest.mark.no_database_autoinit
@pytest.mark.parametrize("failed_phase", ["candidate_ready", "source_quiesced", "published"])
def test_offline_rebuild_phase_failure_forward_retries_same_operation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failed_phase: str,
) -> None:
    from fwrouter_api.core.config import get_settings
    from fwrouter_api.db.connection import get_db_path, initialize_database
    from fwrouter_api.services.control_plane_transfer import export_control_plane_snapshot

    state_root = tmp_path / "state"
    monkeypatch.setenv("FWROUTER_STATE_DIR", str(state_root))
    get_settings.cache_clear()
    state_root.mkdir(mode=0o700)
    _state_root, operation_id, lifecycle_fd, maintenance_fd, checkpoint_path = _fixture(
        monkeypatch, tmp_path, state_root=state_root,
    )
    monkeypatch.setattr(
        "fwrouter_api.adapters.xray_common._default_xray_config_path",
        lambda: str(tmp_path / "owned-xray" / "config.json"),
    )
    monkeypatch.setattr(
        database_admin, "reconcile_control_plane_runtime",
        lambda **_kwargs: {"subject_inventory": {"ok": True}},
    )
    initialize_database()
    source_snapshot = export_control_plane_snapshot(include_secrets=False, write_file=False)["snapshot"]
    db_path = get_db_path()
    original_phase = database_admin._offline_phase
    injected = False

    def fail_once(phase: str) -> None:
        nonlocal injected
        if phase == failed_phase and not injected:
            injected = True
            raise OSError("synthetic phase publication interruption")
        original_phase(phase)

    try:
        with admission.offline_rebuild_lease(
            maintenance_fd, lifecycle_fd, state_root=state_root,
        ):
            monkeypatch.setattr(database_admin, "_offline_phase", fail_once)
            with pytest.raises(OSError, match="synthetic phase publication interruption"):
                database_admin.rebuild_control_plane_database(
                    snapshot=source_snapshot, operation_id=operation_id,
                )
            assert injected
            assert admission.startup_allowed(state_root) is False
            monkeypatch.setattr(database_admin, "_offline_phase", original_phase)
            completed = database_admin.rebuild_control_plane_database(
                snapshot=source_snapshot, operation_id=operation_id,
            )
            assert completed["ok"] is True
            assert completed["import"].get("replayed") is True
            assert completed["operation_id"] == operation_id.replace("-", "")
            assert database_admin._validate_database_file(db_path)["ok"] is True
            assert not Path(f"{db_path}-wal").exists()
            assert not Path(f"{db_path}-shm").exists()
            assert len(list((state_root / "backups").glob("*.bak"))) == 1
        assert json.loads(checkpoint_path.read_text(encoding="utf-8"))[
            "maintenance_operation"]["status"] == "published"
    finally:
        monkeypatch.setattr(database_admin, "_offline_phase", original_phase)
        os.close(maintenance_fd)
        os.close(lifecycle_fd)


@pytest.mark.no_database_autoinit
def test_offline_rebuild_pins_backup_before_candidate_and_adopts_only_same_source_after_crash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from fwrouter_api.core.config import get_settings
    from fwrouter_api.db.connection import get_db_path, initialize_database
    from fwrouter_api.services.control_plane_transfer import export_control_plane_snapshot

    state_root = tmp_path / "state"
    monkeypatch.setenv("FWROUTER_STATE_DIR", str(state_root))
    get_settings.cache_clear()
    state_root.mkdir(mode=0o700)
    _state_root, operation_id, lifecycle_fd, maintenance_fd, checkpoint_path = _fixture(
        monkeypatch, tmp_path, state_root=state_root,
    )
    monkeypatch.setattr(
        "fwrouter_api.adapters.xray_common._default_xray_config_path",
        lambda: str(tmp_path / "owned-xray" / "config.json"),
    )
    monkeypatch.setattr(
        database_admin, "reconcile_control_plane_runtime",
        lambda **_kwargs: {"subject_inventory": {"ok": True}},
    )
    initialize_database()
    source_snapshot = export_control_plane_snapshot(include_secrets=False, write_file=False)["snapshot"]
    source_before = database_admin._logical_database_digest(get_db_path())
    original_record_pin = admission.record_offline_rebuild_backup_pin
    injected = False

    def fail_before_pin(filename: str, sha256: str) -> None:
        nonlocal injected
        if not injected:
            injected = True
            raise OSError("synthetic stop before backup pin")
        original_record_pin(filename, sha256)

    try:
        with admission.offline_rebuild_lease(
            maintenance_fd, lifecycle_fd, state_root=state_root,
        ):
            monkeypatch.setattr(admission, "record_offline_rebuild_backup_pin", fail_before_pin)
            with pytest.raises(OSError, match="synthetic stop before backup pin"):
                database_admin.rebuild_control_plane_database(
                    snapshot=source_snapshot, operation_id=operation_id,
                )
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            assert checkpoint["maintenance_operation"]["backup"] is None
            backups_before_retry = list((state_root / "backups").glob("*.bak"))
            assert len(backups_before_retry) == 1
            assert database_admin._logical_database_digest(get_db_path()) == source_before
            monkeypatch.setattr(admission, "record_offline_rebuild_backup_pin", original_record_pin)
            result = database_admin.rebuild_control_plane_database(
                snapshot=source_snapshot, operation_id=operation_id,
            )
            assert result["ok"] is True
            assert Path(result["backup"]["backup_path"]) == backups_before_retry[0]
            assert len(list((state_root / "backups").glob("*.bak"))) == 1
            final_checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            assert final_checkpoint["maintenance_operation"]["backup"]["filename"] == backups_before_retry[0].name
    finally:
        os.close(maintenance_fd)
        os.close(lifecycle_fd)


@pytest.mark.no_database_autoinit
def test_rebuild_revalidates_raw_backup_pin_at_publication_boundary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from fwrouter_api.core.config import get_settings
    from fwrouter_api.db.connection import get_db_path, initialize_database
    from fwrouter_api.services.control_plane_transfer import export_control_plane_snapshot

    state_root = tmp_path / "state"
    monkeypatch.setenv("FWROUTER_STATE_DIR", str(state_root))
    get_settings.cache_clear()
    state_root.mkdir(mode=0o700)
    _state_root, operation_id, lifecycle_fd, maintenance_fd, checkpoint_path = _fixture(
        monkeypatch, tmp_path, state_root=state_root,
    )
    monkeypatch.setattr(
        "fwrouter_api.adapters.xray_common._default_xray_config_path",
        lambda: str(tmp_path / "owned-xray" / "config.json"),
    )
    monkeypatch.setattr(
        database_admin, "reconcile_control_plane_runtime",
        lambda **_kwargs: {"subject_inventory": {"ok": True}},
    )
    initialize_database()
    source_snapshot = export_control_plane_snapshot(include_secrets=False, write_file=False)["snapshot"]
    db_path = get_db_path()
    source_inode = db_path.stat().st_ino
    source_digest = database_admin._logical_database_digest(db_path)
    original_phase = database_admin._offline_phase
    tampered = False

    def replace_with_equal_logical_sqlite(phase: str) -> None:
        nonlocal tampered
        original_phase(phase)
        if phase != "source_quiesced":
            return
        pin = admission.offline_rebuild_backup_pin()
        assert pin is not None
        backup_path = state_root / "backups" / pin["filename"]
        expected_raw_digest = hashlib.sha256(backup_path.read_bytes()).hexdigest()
        logical_digest = database_admin._logical_database_digest(backup_path)
        connection = sqlite3.connect(backup_path)
        try:
            connection.execute("PRAGMA page_size=8192")
            connection.execute("VACUUM")
        finally:
            connection.close()
        assert database_admin._logical_database_digest(backup_path) == logical_digest
        assert hashlib.sha256(backup_path.read_bytes()).hexdigest() != expected_raw_digest
        tampered = True

    try:
        with admission.offline_rebuild_lease(
            maintenance_fd, lifecycle_fd, state_root=state_root,
        ):
            monkeypatch.setattr(database_admin, "_offline_phase", replace_with_equal_logical_sqlite)
            with pytest.raises(ValueError, match="DATABASE_REBUILD_BACKUP_PIN_MISMATCH"):
                database_admin.rebuild_control_plane_database(
                    snapshot=source_snapshot, operation_id=operation_id,
                )
            assert tampered
            assert db_path.stat().st_ino == source_inode
            assert database_admin._logical_database_digest(db_path) == source_digest
            assert database_admin._database_receipt(db_path, uuid.UUID(operation_id).hex) is None
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            assert checkpoint["maintenance_operation"]["status"] == "source_quiesced"
            assert admission.startup_allowed(state_root) is False
    finally:
        monkeypatch.setattr(database_admin, "_offline_phase", original_phase)
        os.close(maintenance_fd)
        os.close(lifecycle_fd)


@pytest.mark.no_database_autoinit
def test_rebuild_rechecks_backup_sha_after_logical_publication_scan(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from fwrouter_api.core.config import get_settings
    from fwrouter_api.db.connection import get_db_path, initialize_database
    from fwrouter_api.services.control_plane_transfer import export_control_plane_snapshot

    state_root = tmp_path / "state"
    monkeypatch.setenv("FWROUTER_STATE_DIR", str(state_root))
    get_settings.cache_clear()
    state_root.mkdir(mode=0o700)
    _state_root, operation_id, lifecycle_fd, maintenance_fd, checkpoint_path = _fixture(
        monkeypatch, tmp_path, state_root=state_root,
    )
    monkeypatch.setattr(
        "fwrouter_api.adapters.xray_common._default_xray_config_path",
        lambda: str(tmp_path / "owned-xray" / "config.json"),
    )
    monkeypatch.setattr(
        database_admin, "reconcile_control_plane_runtime",
        lambda **_kwargs: {"subject_inventory": {"ok": True}},
    )
    initialize_database()
    source_snapshot = export_control_plane_snapshot(include_secrets=False, write_file=False)["snapshot"]
    db_path = get_db_path()
    source_inode = db_path.stat().st_ino
    source_digest = database_admin._logical_database_digest(db_path)
    original_phase = database_admin._offline_phase
    original_logical_digest = database_admin._logical_database_digest
    backup_path: Path | None = None
    expected_backup_digest: str | None = None
    tampered = False
    armed = False

    def arm_after_quiescence(phase: str) -> None:
        nonlocal armed, backup_path, expected_backup_digest
        original_phase(phase)
        if phase == "source_quiesced":
            pin = admission.offline_rebuild_backup_pin()
            assert pin is not None
            backup_path = state_root / "backups" / pin["filename"]
            expected_backup_digest = pin["sha256"]
            armed = True

    def tamper_during_logical_scan(path: Path) -> str:
        nonlocal tampered
        digest = original_logical_digest(path)
        if armed and backup_path == path and not tampered:
            assert expected_backup_digest is not None
            assert hashlib.sha256(path.read_bytes()).hexdigest() == expected_backup_digest
            before = original_logical_digest(path)
            connection = sqlite3.connect(path)
            try:
                connection.execute("PRAGMA page_size=8192")
                connection.execute("VACUUM")
            finally:
                connection.close()
            assert original_logical_digest(path) == before == digest
            assert hashlib.sha256(path.read_bytes()).hexdigest() != expected_backup_digest
            tampered = True
        return digest

    try:
        with admission.offline_rebuild_lease(
            maintenance_fd, lifecycle_fd, state_root=state_root,
        ):
            monkeypatch.setattr(database_admin, "_offline_phase", arm_after_quiescence)
            monkeypatch.setattr(database_admin, "_logical_database_digest", tamper_during_logical_scan)
            with pytest.raises(ValueError, match="DATABASE_REBUILD_BACKUP_PIN_MISMATCH"):
                database_admin.rebuild_control_plane_database(
                    snapshot=source_snapshot, operation_id=operation_id,
                )
            assert tampered
            assert db_path.stat().st_ino == source_inode
            assert original_logical_digest(db_path) == source_digest
            assert database_admin._database_receipt(db_path, uuid.UUID(operation_id).hex) is None
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            assert checkpoint["maintenance_operation"]["status"] == "source_quiesced"
            assert admission.startup_allowed(state_root) is False
    finally:
        monkeypatch.setattr(database_admin, "_offline_phase", original_phase)
        monkeypatch.setattr(database_admin, "_logical_database_digest", original_logical_digest)
        os.close(maintenance_fd)
        os.close(lifecycle_fd)


@pytest.mark.no_database_autoinit
@pytest.mark.parametrize("tamper", ["missing_pin", "missing_file", "wrong_pin", "valid_sqlite_substitution"])
def test_offline_rebuild_refuses_missing_or_changed_pinned_backup_on_forward_retry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tamper: str,
) -> None:
    from fwrouter_api.core.config import get_settings
    from fwrouter_api.db.connection import get_db_path, initialize_database
    from fwrouter_api.services.control_plane_transfer import export_control_plane_snapshot

    state_root = tmp_path / "state"
    monkeypatch.setenv("FWROUTER_STATE_DIR", str(state_root))
    get_settings.cache_clear()
    state_root.mkdir(mode=0o700)
    _state_root, operation_id, lifecycle_fd, maintenance_fd, checkpoint_path = _fixture(
        monkeypatch, tmp_path, state_root=state_root,
    )
    monkeypatch.setattr(
        "fwrouter_api.adapters.xray_common._default_xray_config_path",
        lambda: str(tmp_path / "owned-xray" / "config.json"),
    )
    monkeypatch.setattr(
        database_admin, "reconcile_control_plane_runtime",
        lambda **_kwargs: {"subject_inventory": {"ok": True}},
    )
    initialize_database()
    source_snapshot = export_control_plane_snapshot(include_secrets=False, write_file=False)["snapshot"]
    original_phase = database_admin._offline_phase
    injected = False

    def stop_at_candidate(phase: str) -> None:
        nonlocal injected
        if phase == "candidate_ready" and not injected:
            injected = True
            raise OSError("synthetic stop after pinned candidate")
        original_phase(phase)

    try:
        with admission.offline_rebuild_lease(
            maintenance_fd, lifecycle_fd, state_root=state_root,
        ):
            monkeypatch.setattr(database_admin, "_offline_phase", stop_at_candidate)
            with pytest.raises(OSError, match="synthetic stop after pinned candidate"):
                database_admin.rebuild_control_plane_database(
                    snapshot=source_snapshot, operation_id=operation_id,
                )
            monkeypatch.setattr(database_admin, "_offline_phase", original_phase)
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            pin = checkpoint["maintenance_operation"]["backup"]
            backup_path = state_root / "backups" / pin["filename"]
            if tamper == "missing_pin":
                checkpoint["maintenance_operation"]["backup"] = None
                checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")
                checkpoint_path.chmod(0o600)
            elif tamper == "missing_file":
                backup_path.unlink()
            elif tamper == "wrong_pin":
                checkpoint["maintenance_operation"]["backup"]["sha256"] = "0" * 64
                checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")
                checkpoint_path.chmod(0o600)
            elif tamper == "valid_sqlite_substitution":
                replacement = tmp_path / "valid-but-different.sqlite"
                connection = sqlite3.connect(replacement)
                try:
                    connection.execute("CREATE TABLE valid_but_different(value TEXT)")
                    connection.execute("INSERT INTO valid_but_different VALUES ('other valid SQLite file')")
                    connection.commit()
                finally:
                    connection.close()
                replacement.chmod(0o600)
                os.replace(replacement, backup_path)
            with pytest.raises(
                (ValueError, admission.MaintenanceAdmissionError), match="(?i)backup|pin",
            ):
                database_admin.rebuild_control_plane_database(
                    snapshot=source_snapshot, operation_id=operation_id,
                )
            assert database_admin._database_receipt(get_db_path(), uuid.UUID(operation_id).hex) is None
            assert get_db_path().exists()
    finally:
        monkeypatch.setattr(database_admin, "_offline_phase", original_phase)
        os.close(maintenance_fd)
        os.close(lifecycle_fd)


@pytest.mark.no_database_autoinit
@pytest.mark.parametrize("add_future_table", [False, True])
def test_offline_rebuild_rejects_unexported_durable_rows_before_backup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, add_future_table: bool,
) -> None:
    from fwrouter_api.core.config import get_settings
    from fwrouter_api.db.connection import get_db_path, initialize_database
    from fwrouter_api.services.control_plane_transfer import export_control_plane_snapshot

    state_root = tmp_path / "state"
    monkeypatch.setenv("FWROUTER_STATE_DIR", str(state_root))
    get_settings.cache_clear()
    state_root.mkdir(mode=0o700)
    _state_root, operation_id, lifecycle_fd, maintenance_fd, _checkpoint_path = _fixture(
        monkeypatch, tmp_path, state_root=state_root,
    )
    monkeypatch.setattr(
        "fwrouter_api.adapters.xray_common._default_xray_config_path",
        lambda: str(tmp_path / "owned-xray" / "config.json"),
    )
    initialize_database()
    db_path = get_db_path()
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            "INSERT INTO provider_bindings(source_ref,provider_id,resource_id,logical_server_id,"
            "protocol,updated_at) VALUES ('test-source','test-provider','test-resource',"
            "'test-logical','vless',0)",
        )
        connection.execute(
            "INSERT INTO provider_credentials(source_ref,api_key) VALUES ('test-source','synthetic-secret')",
        )
        connection.execute("INSERT INTO subscription_accounts(slug,display_name) VALUES ('test-account','synthetic')")
        if add_future_table:
            connection.execute("CREATE TABLE future_managed_state (value TEXT NOT NULL)")
            connection.execute("INSERT INTO future_managed_state(value) VALUES ('must-not-be-lost')")
        connection.commit()
    finally:
        connection.close()
    source_snapshot = export_control_plane_snapshot(include_secrets=False, write_file=False)["snapshot"]
    monkeypatch.setattr(
        database_admin, "_operation_backup",
        lambda *_args, **_kwargs: pytest.fail("must reject unsupported state before backup"),
    )
    try:
        with admission.offline_rebuild_lease(
            maintenance_fd, lifecycle_fd, state_root=state_root,
        ):
            result = database_admin.rebuild_control_plane_database(
                snapshot=source_snapshot, operation_id=operation_id,
            )
        assert result["ok"] is False
        assert result["error"]["code"] == "DATABASE_REBUILD_UNSUPPORTED_DURABLE_TABLES"
        expected_tables = [
            "provider_bindings", "provider_credentials", "sqlite_sequence", "subscription_accounts",
        ]
        if add_future_table:
            expected_tables.append("future_managed_state")
        assert result["error"]["tables"] == sorted(expected_tables)
        assert "synthetic-secret" not in json.dumps(result)
        assert not (state_root / "backups").exists()
    finally:
        os.close(maintenance_fd)
        os.close(lifecycle_fd)


@pytest.mark.no_database_autoinit
def test_offline_rebuild_rejects_custom_sqlite_schema_objects_without_dropping_them(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from fwrouter_api.core.config import get_settings
    from fwrouter_api.db.connection import get_db_path, initialize_database
    from fwrouter_api.services.control_plane_transfer import export_control_plane_snapshot

    state_root = tmp_path / "state"
    monkeypatch.setenv("FWROUTER_STATE_DIR", str(state_root))
    get_settings.cache_clear()
    state_root.mkdir(mode=0o700)
    _state_root, operation_id, lifecycle_fd, maintenance_fd, _checkpoint_path = _fixture(
        monkeypatch, tmp_path, state_root=state_root,
    )
    monkeypatch.setattr(
        "fwrouter_api.adapters.xray_common._default_xray_config_path",
        lambda: str(tmp_path / "owned-xray" / "config.json"),
    )
    initialize_database()
    db_path = get_db_path()
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            "CREATE TRIGGER test_restore_custom_trigger AFTER INSERT ON settings "
            "BEGIN SELECT 1; END",
        )
        connection.commit()
    finally:
        connection.close()
    source_snapshot = export_control_plane_snapshot(include_secrets=False, write_file=False)["snapshot"]
    before = database_admin._logical_database_digest(db_path)
    try:
        with admission.offline_rebuild_lease(
            maintenance_fd, lifecycle_fd, state_root=state_root,
        ):
            with pytest.raises(ValueError, match="DATABASE_REBUILD_SCHEMA_OBJECTS_UNSUPPORTED"):
                database_admin.rebuild_control_plane_database(
                    snapshot=source_snapshot, operation_id=operation_id,
                )
        assert database_admin._logical_database_digest(db_path) == before
        assert len(list((state_root / "backups").glob("*.bak"))) == 1
    finally:
        os.close(maintenance_fd)
        os.close(lifecycle_fd)


def test_offline_rebuild_rejects_state_environment_redirect_before_snapshot_resolution(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from fwrouter_api.core.config import get_settings

    state_root = tmp_path / "state"
    monkeypatch.setenv("FWROUTER_STATE_DIR", str(state_root))
    get_settings.cache_clear()
    _state_root, operation_id, lifecycle_fd, maintenance_fd, _checkpoint_path = _fixture(
        monkeypatch, tmp_path, state_root=state_root,
    )
    redirected = tmp_path / "redirected"
    monkeypatch.setenv("FWROUTER_STATE_DIR", str(redirected))
    get_settings.cache_clear()
    monkeypatch.setattr(
        database_admin, "resolve_control_plane_snapshot_source",
        lambda **_kwargs: pytest.fail("snapshot resolver must follow fixed-root validation"),
    )
    try:
        with admission.offline_rebuild_lease(
            maintenance_fd, lifecycle_fd, state_root=state_root,
        ):
            result = database_admin.rebuild_control_plane_database(
                snapshot={}, operation_id=operation_id,
            )
        assert result["ok"] is False
        assert result["error"]["code"] == "DATABASE_REBUILD_STATE_ROOT_MISMATCH"
        assert not redirected.exists()
    finally:
        os.close(maintenance_fd)
        os.close(lifecycle_fd)
