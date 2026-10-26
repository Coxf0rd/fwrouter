from __future__ import annotations

import contextvars
import hashlib
import json
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, Lock
import pwd
import secrets
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from _test_support import (
    configure_test_state_dir as _configure_env,
    maintenance_import_test_client,
)
from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import get_db_path
from fwrouter_api.main import create_app
from fwrouter_api.services.control_plane_transfer import import_control_plane_snapshot
from fwrouter_api.services import control_plane_import_admission as admission


class _ObservedRLock:
    """Test-owned RLock that signals when a worker queues behind its owner."""

    def __init__(self) -> None:
        import threading

        self._lock = threading.RLock()
        self.waiting = Event()

    def acquire(self, *args, **kwargs):
        if not self._lock._is_owned():
            self.waiting.set()
        return self._lock.acquire(*args, **kwargs)

    def release(self) -> None:
        self._lock.release()


def _uncredentialed_client() -> TestClient:
    return TestClient(
        create_app(enable_startup_tasks=False),
        client=("127.0.0.1", 45678),
        headers={"Host": "127.0.0.1:5000"},
    )


def _request(client: TestClient, **kwargs):
    return client.post(
        "/api/v2/transfer/control-plane/import",
        json={"snapshot": {"snapshot_version": "invalid"}},
        **kwargs,
    )


@pytest.mark.no_database_autoinit
def test_import_route_denies_before_resolve_directory_db_or_writer_lock(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    settings = get_settings()
    db_path = get_db_path()
    monkeypatch.setattr(
        "fwrouter_api.routes.transfer.resolve_control_plane_snapshot_source",
        lambda **_kwargs: pytest.fail("resolver must not run before maintenance admission"),
    )

    with _uncredentialed_client() as client:
        response = _request(client)

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "CONTROL_PLANE_IMPORT_FORBIDDEN"
    assert not db_path.exists()
    assert not (settings.paths.state_dir / "transfer").exists()
    assert not (settings.paths.run_dir / "xray-writer.lock").exists()


@pytest.mark.parametrize(
    "headers",
    [
        {"Authorization": "Bearer " + "0" * 64},
        {"Authorization": "Bearer " + "0" * 64, "Origin": "https://router.example"},
        {"Authorization": "Bearer " + "0" * 64, "Forwarded": "for=127.0.0.1"},
        {"Authorization": "Bearer " + "0" * 64, "X-Forwarded-For": "127.0.0.1"},
        {"Authorization": "Bearer " + "0" * 64, "Host": "localhost:5000"},
    ],
)
def test_import_route_rejects_wrong_token_browser_or_proxy_authority(
    monkeypatch, tmp_path: Path, headers: dict[str, str],
) -> None:
    _configure_env(monkeypatch, tmp_path)
    with maintenance_import_test_client(monkeypatch, tmp_path) as client:
        response = _request(client, headers=headers)
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "CONTROL_PLANE_IMPORT_FORBIDDEN"


def test_import_route_rejects_non_loopback_peer_even_with_valid_bearer(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    client = maintenance_import_test_client(monkeypatch, tmp_path)
    client.close()
    token = client.headers["authorization"]
    with TestClient(
        create_app(enable_startup_tasks=False),
        client=("127.0.0.2", 45678),
        headers={"Host": "127.0.0.1:5000", "Authorization": token},
    ) as other_peer:
        response = _request(other_peer)
    assert response.status_code == 403


def test_import_route_rejects_duplicate_authorization_headers(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    with maintenance_import_test_client(monkeypatch, tmp_path) as client:
        token = client.headers["authorization"]
        request = client.build_request(
            "POST",
            "/api/v2/transfer/control-plane/import",
            json={"snapshot": {"snapshot_version": "invalid"}},
            headers=[("Authorization", token), ("Authorization", token)],
        )
        response = client.send(request)
    assert response.status_code == 403


@pytest.mark.parametrize(
    "document",
    [
        b'{"schema_version":1,"schema_version":1,"principal":"maintenance-operator","scope":"control-plane.import","token_sha256":"' + b"0" * 64 + b'"}',
        json.dumps({
            "schema_version": 1,
            "principal": "maintenance-operator",
            "scope": "control-plane.import",
            "token_sha256": "0" * 64,
            "unexpected": True,
        }).encode(),
        json.dumps({
            "schema_version": 2,
            "principal": "maintenance-operator",
            "scope": "control-plane.import",
            "token_sha256": "0" * 64,
        }).encode(),
        json.dumps({
            "schema_version": 1,
            "principal": "presentation-admin",
            "scope": "control-plane.import",
            "token_sha256": "0" * 64,
        }).encode(),
        json.dumps({
            "schema_version": 1,
            "principal": "maintenance-operator",
            "scope": "other",
            "token_sha256": "0" * 64,
        }).encode(),
    ],
)
def test_invalid_verifier_documents_fail_closed(monkeypatch, tmp_path: Path, document: bytes) -> None:
    _configure_env(monkeypatch, tmp_path)
    client = maintenance_import_test_client(monkeypatch, tmp_path)
    verifier = admission.VERIFIER_PATH
    verifier.write_bytes(document)
    verifier.chmod(0o600)
    with client:
        response = _request(client)
    assert response.status_code == 403


@pytest.mark.parametrize("mode", [0o644, 0o666])
def test_verifier_wrong_mode_fails_closed(monkeypatch, tmp_path: Path, mode: int) -> None:
    _configure_env(monkeypatch, tmp_path)
    client = maintenance_import_test_client(monkeypatch, tmp_path)
    admission.VERIFIER_PATH.chmod(mode)
    with client:
        response = _request(client)
    assert response.status_code == 403


def test_verifier_parent_permissions_and_owner_must_be_trusted(
    monkeypatch, tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    client = maintenance_import_test_client(monkeypatch, tmp_path)
    verifier_parent = admission.VERIFIER_PATH.parent
    verifier_parent.chmod(0o770)
    with client:
        assert _request(client).status_code == 403
    verifier_parent.chmod(0o700)
    monkeypatch.setattr(admission, "_trusted_uid", lambda: os.getuid() + 1)
    with client:
        assert _request(client).status_code == 403


@pytest.mark.parametrize(
    "trusted_uid, owner_uid, mode, final_parent, expected",
    [
        # Root API identity: final parent must be root-owned and not writable.
        (0, 0, 0o755, False, True),
        (0, 0, 0o755, True, True),
        (0, 0, 0o700, False, True),
        (0, 0, 0o700, True, True),
        (0, 0, 0o1777, False, True),
        (0, 0, 0o1777, True, False),
        (0, 1001, 0o700, False, False),
        (0, 1001, 0o700, True, False),
        (0, 0, 0o777, False, False),
        (0, 0, 0o777, True, False),
        (0, 1002, 0o700, False, False),
        (0, 1002, 0o700, True, False),
        # Non-root API identity: root-owned protected ancestors are allowed,
        # but the immediate parent must be privately owned by that identity.
        (1001, 0, 0o755, False, True),
        (1001, 0, 0o755, True, False),
        (1001, 0, 0o700, False, True),
        (1001, 0, 0o700, True, False),
        (1001, 0, 0o1777, False, True),
        (1001, 0, 0o1777, True, False),
        (1001, 1001, 0o700, False, True),
        (1001, 1001, 0o700, True, True),
        (1001, 1001, 0o770, False, False),
        (1001, 1001, 0o770, True, False),
        (1001, 0, 0o777, False, False),
        (1001, 0, 0o777, True, False),
        (1001, 1002, 0o700, False, False),
        (1001, 1002, 0o700, True, False),
    ],
)
def test_parent_trust_matrix(
    trusted_uid: int,
    owner_uid: int,
    mode: int,
    final_parent: bool,
    expected: bool,
) -> None:
    info = SimpleNamespace(st_mode=stat.S_IFDIR | mode, st_uid=owner_uid)
    assert admission._is_protected_parent(
        info, trusted_uid, final_parent=final_parent,
    ) is expected


def test_nonroot_verifier_owner_works_beneath_real_root_sticky_tmp_metadata(
    monkeypatch, tmp_path: Path,
) -> None:
    if os.geteuid() == 0:
        trusted_uid = next(account.pw_uid for account in pwd.getpwall() if account.pw_uid != 0)
    else:
        trusted_uid = os.geteuid()
    token = secrets.token_hex(32)
    root = tmp_path / "fwrouter-maintenance-auth"
    parent = root / "etc" / "fwrouter"
    parent.mkdir(mode=0o700, parents=True)
    verifier = parent / "maintenance-import-verifier.json"
    verifier.write_text(json.dumps({
        "schema_version": 1,
        "principal": "maintenance-operator",
        "scope": "control-plane.import",
        "token_sha256": hashlib.sha256(token.encode("ascii")).hexdigest(),
    }), encoding="utf-8")
    verifier.chmod(0o600)
    if os.geteuid() == 0:
        for path in (root, root / "etc", parent, verifier):
            os.chown(path, trusted_uid, -1)
    monkeypatch.setattr(admission, "VERIFIER_PATH", verifier)
    monkeypatch.setattr(admission, "_trusted_uid", lambda: trusted_uid)
    monkeypatch.setattr(admission, "_effective_uid", lambda: trusted_uid)
    assert admission._verifier_digest() == hashlib.sha256(token.encode("ascii")).hexdigest()
    if os.geteuid() == 0:
        # Real inode metadata must still reject a verifier owned by a
        # different identity, even beneath an acceptable /tmp ancestor.
        os.chown(verifier, 0, -1)
        with pytest.raises(PermissionError):
            admission._verifier_digest()


def test_verifier_symlink_fails_closed(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    client = maintenance_import_test_client(monkeypatch, tmp_path)
    verifier = admission.VERIFIER_PATH
    replacement = tmp_path / "replacement.json"
    replacement.write_bytes(verifier.read_bytes())
    replacement.chmod(0o600)
    verifier.unlink()
    verifier.symlink_to(replacement)
    with client:
        response = _request(client)
    assert response.status_code == 403


def test_verifier_revocation_applies_to_next_request_and_token_is_not_returned(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    with maintenance_import_test_client(monkeypatch, tmp_path) as client:
        token = client.headers["authorization"].split(" ", 1)[1]
        verifier = admission.VERIFIER_PATH
        policy = json.loads(verifier.read_text(encoding="utf-8"))
        first = _request(client)
        policy["token_sha256"] = "f" * 64
        verifier.write_text(json.dumps(policy), encoding="utf-8")
        verifier.chmod(0o600)
        second = _request(client)
    assert first.status_code == 200
    assert second.status_code == 403
    assert token not in first.text
    assert token not in second.text


@pytest.mark.no_database_autoinit
def test_import_service_denies_direct_call_before_database_or_writer_lock(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    settings = get_settings()
    monkeypatch.setattr(
        "fwrouter_api.services.control_plane_transfer_import.validate_control_plane_snapshot",
        lambda _snapshot: {"ok": True},
    )
    result = import_control_plane_snapshot({"snapshot_version": "invalid"})
    assert result["ok"] is False
    assert result["error_code"] == "CONTROL_PLANE_IMPORT_MAINTENANCE_ADMISSION_REQUIRED"
    assert not get_db_path().exists()
    assert not settings.paths.run_dir.exists()


def test_authorized_admission_context_is_cleared_after_response(
    monkeypatch, tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "fwrouter_api.services.control_plane_transfer_import.validate_control_plane_snapshot",
        lambda _snapshot: {"ok": False},
    )
    captured_contexts: list[contextvars.Context] = []
    original_import = import_control_plane_snapshot

    def capture_admitted_context(*args, **kwargs):
        assert admission.control_plane_import_is_admitted()
        captured_contexts.append(contextvars.copy_context())
        return original_import(*args, **kwargs)

    monkeypatch.setattr(
        "fwrouter_api.routes.transfer.import_control_plane_snapshot",
        capture_admitted_context,
    )
    with maintenance_import_test_client(monkeypatch, tmp_path) as client:
        response = _request(client)
    assert response.status_code == 200
    assert len(captured_contexts) == 1
    assert not captured_contexts[0].run(admission.control_plane_import_is_admitted)
    direct = captured_contexts[0].run(
        import_control_plane_snapshot, {"snapshot_version": "invalid"},
    )
    assert direct["error_code"] == "CONTROL_PLANE_IMPORT_MAINTENANCE_ADMISSION_REQUIRED"


@pytest.mark.no_database_autoinit
def test_copied_admission_queued_on_writer_guard_is_revoked_before_import_side_effects(
    monkeypatch, tmp_path: Path,
) -> None:
    from fwrouter_api.core.config import get_settings
    from fwrouter_api.db.connection import get_db_path
    from fwrouter_api.services import control_plane_transfer_import as transfer_import
    from fwrouter_api.adapters import xray_common

    _configure_env(monkeypatch, tmp_path)
    monkeypatch.setattr(
        transfer_import, "validate_control_plane_snapshot", lambda _snapshot: {"ok": True},
    )
    monkeypatch.setattr(
        transfer_import, "initialize_database",
        lambda: pytest.fail("revoked queued import must not initialize SQLite"),
    )
    lock = _ObservedRLock()
    lock.acquire()
    lock.waiting.clear()
    monkeypatch.setattr(xray_common, "_XRAY_WRITER_THREAD_LOCK", lock)

    background: ThreadPoolExecutor | None = None
    queued: list = []
    captured_contexts: list[contextvars.Context] = []

    def resolve_and_queue(**_kwargs):
        context = contextvars.copy_context()
        captured_contexts.append(context)
        assert admission.control_plane_import_is_admitted()
        assert background is not None
        queued.append(background.submit(
            context.run,
            import_control_plane_snapshot,
            {"snapshot_version": "valid-for-patched-validator"},
        ))
        assert lock.waiting.wait(timeout=5)
        return {"ok": True, "snapshot": {"snapshot_version": "route-stub"}, "source": {}}

    monkeypatch.setattr(
        "fwrouter_api.routes.transfer.resolve_control_plane_snapshot_source",
        resolve_and_queue,
    )
    monkeypatch.setattr(
        "fwrouter_api.routes.transfer.import_control_plane_snapshot",
        lambda *_args, **_kwargs: {
            "ok": True,
            "imported": True,
            "selection_reconcile_required": False,
        },
    )
    settings = get_settings()
    db_path = get_db_path()
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            background = pool
            with maintenance_import_test_client(monkeypatch, tmp_path) as client:
                response = _request(client)
            assert response.status_code == 200
            assert len(queued) == 1
            assert len(captured_contexts) == 1
            # The dependency has closed by the time TestClient returns; the
            # copied context shares the revoked grant even while still queued.
            lock.release()
            result = queued[0].result(timeout=5)
            assert not captured_contexts[0].run(admission.control_plane_import_is_admitted)
        assert result["error_code"] == "CONTROL_PLANE_IMPORT_MAINTENANCE_ADMISSION_REQUIRED"
        assert not db_path.exists()
        assert not (settings.paths.state_dir / "transfer").exists()
    finally:
        if lock._lock._is_owned():
            lock.release()


@pytest.mark.no_database_autoinit
def test_parallel_unauthorized_request_does_not_inherit_admitted_context(
    monkeypatch, tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    entered = Event()
    release = Event()
    call_lock = Lock()
    resolver_calls = 0

    def blocked_resolver(**_kwargs):
        nonlocal resolver_calls
        with call_lock:
            resolver_calls += 1
        entered.set()
        assert release.wait(timeout=5)
        return {"ok": True, "snapshot": {"snapshot_version": "invalid"}, "source": {}}

    monkeypatch.setattr(
        "fwrouter_api.routes.transfer.resolve_control_plane_snapshot_source",
        blocked_resolver,
    )
    with maintenance_import_test_client(monkeypatch, tmp_path) as admitted_client, _uncredentialed_client() as denied_client:
        with ThreadPoolExecutor(max_workers=2) as pool:
            admitted = pool.submit(_request, admitted_client)
            try:
                assert entered.wait(timeout=5)
                denied = pool.submit(_request, denied_client)
                assert denied.result(timeout=5).status_code == 403
            finally:
                release.set()
            assert admitted.result(timeout=5).status_code == 200

    assert resolver_calls == 1


def test_environment_cannot_redirect_verifier_path(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    client = maintenance_import_test_client(monkeypatch, tmp_path)
    original = admission.VERIFIER_PATH
    alternate = tmp_path / "attacker.json"
    alternate.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("FWROUTER_MAINTENANCE_IMPORT_VERIFIER", str(alternate))
    assert admission.VERIFIER_PATH == original
    with client:
        assert _request(client).status_code == 200
