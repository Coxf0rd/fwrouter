from __future__ import annotations

import hashlib
import base64
import json
import os
from pathlib import Path
import secrets
from threading import Event, Thread
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from fwrouter_api.services import api_client_registry as registry
from fwrouter_api.services import presentation_client_list_admission as admission


def _paths(monkeypatch: pytest.MonkeyPatch, root: Path) -> tuple[Path, Path, Path, Path]:
    host = root / "etc" / "fwrouter"
    host.mkdir(parents=True, mode=0o700)
    verifier = host / "presentation-client-list-verifier.json"
    monkeypatch.setattr(registry, "REGISTRY_PATH", host / "api-clients.json")
    monkeypatch.setattr(registry, "SELECTOR_PATH", verifier)
    monkeypatch.setattr(registry, "FENCE_PATH", host / "api-clients.required")
    monkeypatch.setattr(registry, "PENDING_PATH", host / "api-clients.pending")
    monkeypatch.setattr(registry, "LOCK_PATH", host / "api-clients.lock")
    monkeypatch.setattr(registry, "_trusted_uid", os.getuid)
    monkeypatch.setattr(admission, "VERIFIER_PATH", verifier)
    monkeypatch.setattr(admission, "_trusted_uid", os.getuid)
    return host, verifier, registry.REGISTRY_PATH, registry.FENCE_PATH


def _legacy(path: Path, token: str) -> None:
    path.write_text(json.dumps({
        "schema_version": 1, "principal": "presentation-operator",
        "scopes": ["ui.client-list.write"],
        "token_sha256": hashlib.sha256(token.encode("ascii")).hexdigest(),
        "canonical_origin": "https://router.example.test",
    }), encoding="utf-8")
    path.chmod(0o600)


def _admin(monkeypatch, command: list[str], capsys) -> dict:
    from fwrouter_api.cli import main

    assert main(["api-clients", *command]) == 0
    captured = capsys.readouterr()
    assert not captured.err
    return json.loads(captured.out)


def _cut_over(monkeypatch, tmp_path: Path, capsys):
    host, verifier, _, _ = _paths(monkeypatch, tmp_path)
    _legacy(verifier, secrets.token_hex(32))
    assert _admin(monkeypatch, ["init", "--canonical-origin", "https://router.example.test"], capsys)["policy_revision"] == 1
    created = {}
    for kind in ("web_ui", "cli"):
        secret_path = host / f"{kind}.token"
        created[kind] = _admin(monkeypatch, ["create", "--expected-revision", str(len(created) + 1),
            "--kind", kind, "--tag", kind, "--scope", "ui.client-list.write",
            "--secret-file", str(secret_path)], capsys)
        token = secret_path.read_text(encoding="ascii").strip()
        assert len(token) == 64 and all(char in "0123456789abcdef" for char in token)
        assert secret_path.stat().st_mode & 0o777 == 0o600
        created[kind]["token"] = token
    cutover = _admin(monkeypatch, ["cutover", "--expected-revision", "3"], capsys)
    assert cutover == {"mode": "registry", "policy_revision": 3}
    return host, created


def _app():
    app = FastAPI()

    @app.exception_handler(admission.PresentationAdmissionError)
    async def admission_error(_request: Request, exc: admission.PresentationAdmissionError):
        return JSONResponse(status_code=exc.status_code, content={"error": {"code": exc.code}})

    @app.post("/presentation")
    def presentation(request: Request):
        with admission.verified_presentation_write(request):
            identity = admission.presentation_identity()
            admission.require_presentation_write_grant()
            return {"client_id": identity.client_id, "kind": identity.kind,
                    "tag": identity.display_tag, "assurance": identity.assurance}

    return app


def test_admin_cli_cutover_and_real_http_identity_use_distinct_credentials(monkeypatch, tmp_path, capsys):
    _, created = _cut_over(monkeypatch, tmp_path, capsys)
    app = _app()
    web = TestClient(app, client=("198.51.100.12", 43210), headers={"Host": "router.example.test"})
    browser = web.post("/presentation", headers={
        "Authorization": f"Bearer {created['web_ui']['token']}",
        "Origin": "https://router.example.test",
    })
    assert browser.status_code == 200
    assert browser.json() == {"client_id": created["web_ui"]["client_id"], "kind": "web_ui",
                              "tag": "web_ui", "assurance": "credential_bound"}

    cli = TestClient(app, client=("127.0.0.1", 45678), headers={"Host": admission.LOCAL_AUTHORITY})
    local = cli.post("/presentation", headers={"Authorization": f"Bearer {created['cli']['token']}"})
    assert local.status_code == 200
    assert local.json()["client_id"] == created["cli"]["client_id"]
    assert local.json()["kind"] == "cli"
    forged = web.post("/presentation", headers={
        "Authorization": f"Bearer {created['web_ui']['token']}",
        "Origin": "https://router.example.test", "X-FWRouter-Client": "cli",
        "X-Client-Id": created["cli"]["client_id"],
    })
    assert forged.json()["kind"] == "web_ui"


def test_repeat_cutover_after_registry_update_preserves_existing_fence(monkeypatch, tmp_path, capsys):
    _host, created = _cut_over(monkeypatch, tmp_path, capsys)
    fence = registry.FENCE_PATH.read_bytes()
    assert _admin(monkeypatch, ["rename", created["web_ui"]["client_id"],
        "--expected-revision", "3", "--tag", "renamed-web"], capsys) == {
            "policy_revision": 4}
    current_policy = registry.REGISTRY_PATH.read_bytes()

    def reject_publication(*_args, **_kwargs):
        raise AssertionError("repeat cutover must not rewrite active registry state")

    monkeypatch.setattr(registry, "_atomic_publish", reject_publication)
    assert _admin(monkeypatch, ["cutover", "--expected-revision", "4"], capsys) == {
        "mode": "registry", "policy_revision": 4}
    assert registry.REGISTRY_PATH.read_bytes() == current_policy
    assert registry.FENCE_PATH.read_bytes() == fence
    assert registry.SELECTOR_PATH.read_bytes() == b'{"provider":"client_registry_v1","schema_version":2}\n'


def test_api_first_config_cli_uses_registered_cli_credential_at_real_owner_route(monkeypatch, tmp_path, capsys):
    host, created = _cut_over(monkeypatch, tmp_path, capsys)
    from fwrouter_api.main import create_app
    from fwrouter_api.services import config_cli

    api = TestClient(create_app(enable_startup_tasks=False), client=("127.0.0.1", 45678),
                     headers={"Host": admission.LOCAL_AUTHORITY})
    before = api.get("/api/v2/ui/settings/client-list")
    assert before.status_code == 200
    resource = before.json()["data"]["client_list"]

    def isolated_http(method: str, *, token=None, etag=None, body=None):
        headers = {}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        if etag is not None:
            headers["If-Match"] = etag
        if body is not None:
            headers["Content-Type"] = "application/json"
        response = api.request(method, "/api/v2/ui/settings/client-list", content=body, headers=headers)
        return response.status_code, response.json(), response.headers.get("etag")

    monkeypatch.setattr(config_cli, "_request", isolated_http)
    credential_file = host / "cli.token"
    assert credential_file.read_text(encoding="ascii") == created["cli"]["token"]
    assert config_cli.main(["set", "ui-client-list", "--show-inactive", "true",
        "--expected-revision", resource["etag"], "--credential-file", str(credential_file)]) == 0
    cli_result = json.loads(capsys.readouterr().out)
    assert cli_result["ok"] is True and cli_result["show_inactive"] is True
    after = api.get("/api/v2/ui/settings/client-list")
    assert after.json()["data"]["client_list"]["show_inactive"] is True


def test_invalid_origin_wrong_transport_and_multiple_bearers_are_denied(monkeypatch, tmp_path, capsys):
    _, created = _cut_over(monkeypatch, tmp_path, capsys)
    client = TestClient(_app(), client=("198.51.100.12", 43210), headers={"Host": "router.example.test"})
    wrong_origin = client.post("/presentation", headers={
        "Authorization": f"Bearer {created['web_ui']['token']}", "Origin": "https://attacker.test",
    })
    assert wrong_origin.status_code == 403
    web_without_origin = TestClient(_app(), client=("127.0.0.1", 45678),
                                    headers={"Host": admission.LOCAL_AUTHORITY})
    missing_origin = web_without_origin.post("/presentation", headers={
        "Authorization": f"Bearer {created['web_ui']['token']}"})
    assert missing_origin.status_code == 403
    cli_with_origin = TestClient(_app(), client=("127.0.0.1", 45678),
                                 headers={"Host": "router.example.test"})
    cli_origin = cli_with_origin.post("/presentation", headers={
        "Authorization": f"Bearer {created['cli']['token']}",
        "Origin": "https://router.example.test"})
    assert cli_origin.status_code == 403
    wrong_peer = TestClient(_app(), client=("198.51.100.12", 45678), headers={"Host": admission.LOCAL_AUTHORITY})
    cli = wrong_peer.post("/presentation", headers={"Authorization": f"Bearer {created['cli']['token']}"})
    assert cli.status_code == 403
    duplicate = client.post("/presentation", headers=[
        ("Authorization", f"Bearer {created['web_ui']['token']}"),
        ("Authorization", f"Bearer {created['cli']['token']}"),
        ("Origin", "https://router.example.test"),
    ])
    assert duplicate.status_code == 401


def test_wrong_scope_and_invalid_credentials_do_not_echo_supplied_secret(monkeypatch, tmp_path, capsys):
    _, created = _cut_over(monkeypatch, tmp_path, capsys)
    policy = registry.load_policy()
    cli = next(client for client in policy["clients"] if client["kind"] == "cli")
    cli["credentials"][0]["scopes"] = []
    policy["policy_revision"] += 1
    registry.publish_policy(policy)
    client = TestClient(_app(), client=("127.0.0.1", 45678), headers={"Host": admission.LOCAL_AUTHORITY})
    denied = client.post("/presentation", headers={
        "Authorization": f"Bearer {created['cli']['token']}"})
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "PRESENTATION_SCOPE_FORBIDDEN"
    canary = "f" * 64
    invalid = client.post("/presentation", headers={"Authorization": f"Bearer {canary}"})
    assert invalid.status_code == 401
    assert canary not in invalid.text and hashlib.sha256(canary.encode("ascii")).hexdigest() not in invalid.text


def test_revoke_waits_for_admitted_request_and_prevents_later_request(monkeypatch, tmp_path, capsys):
    host, created = _cut_over(monkeypatch, tmp_path, capsys)
    entered, release, revoked = Event(), Event(), Event()
    app = FastAPI()

    @app.exception_handler(admission.PresentationAdmissionError)
    async def admission_error(_request: Request, exc: admission.PresentationAdmissionError):
        return JSONResponse(status_code=exc.status_code, content={"error": {"code": exc.code}})

    @app.post("/slow")
    def slow(request: Request):
        with admission.verified_presentation_write(request):
            entered.set()
            assert release.wait(2)
            admission.require_presentation_write_grant()
            return {"ok": True}

    client = TestClient(app, client=("127.0.0.1", 45678), headers={"Host": admission.LOCAL_AUTHORITY})
    response: list = []
    request_thread = Thread(target=lambda: response.append(client.post("/slow", headers={
        "Authorization": f"Bearer {created['cli']['token']}"})))
    request_thread.start()
    assert entered.wait(1)

    def revoke():
        from types import SimpleNamespace
        from fwrouter_api.services.api_client_registry_admin import _run

        _run(SimpleNamespace(command="revoke", client_id=created["cli"]["client_id"], expected_revision=3))
        revoked.set()

    revoke_thread = Thread(target=revoke)
    revoke_thread.start()
    time.sleep(0.05)
    assert not revoked.is_set()
    release.set()
    request_thread.join(2)
    revoke_thread.join(2)
    assert response[0].status_code == 200
    assert revoked.is_set()
    denied = client.post("/slow", headers={"Authorization": f"Bearer {created['cli']['token']}"})
    assert denied.status_code == 401


def test_revoke_uncertain_publication_stops_api_until_forward_recovery(monkeypatch, tmp_path, capsys):
    _, created = _cut_over(monkeypatch, tmp_path, capsys)
    client = TestClient(_app(), client=("127.0.0.1", 45678), headers={"Host": admission.LOCAL_AUTHORITY})
    original_fsync = registry.os.fsync
    calls = 0

    def fail_target_directory(fd: int):
        nonlocal calls
        calls += 1
        # Intent write (2), intent readback (2), target temp write (2),
        # then fail the target directory sync after replace.
        if calls == 6:
            raise OSError("injected post-replace target directory fsync failure")
        return original_fsync(fd)

    monkeypatch.setattr(registry.os, "fsync", fail_target_directory)
    from fwrouter_api.cli import main
    assert main(["api-clients", "revoke", created["cli"]["client_id"],
                 "--expected-revision", "3"]) == 2
    capsys.readouterr()
    stopped = client.post("/presentation", headers={"Authorization": f"Bearer {created['cli']['token']}"})
    assert stopped.status_code == 503
    monkeypatch.setattr(registry.os, "fsync", original_fsync)
    assert main(["api-clients", "recover", "--expected-revision", "4"]) == 0
    capsys.readouterr()
    assert client.post("/presentation", headers={
        "Authorization": f"Bearer {created['cli']['token']}"}).status_code == 401


def test_pending_corruption_or_target_mismatch_fails_closed(monkeypatch, tmp_path, capsys):
    _, created = _cut_over(monkeypatch, tmp_path, capsys)
    pending = registry.PENDING_PATH
    pending.write_text("{}", encoding="ascii")
    pending.chmod(0o600)
    client = TestClient(_app(), client=("127.0.0.1", 45678), headers={"Host": admission.LOCAL_AUTHORITY})
    stopped = client.post("/presentation", headers={"Authorization": f"Bearer {created['cli']['token']}"})
    assert stopped.status_code == 503
    with pytest.raises(registry.RegistryError, match="API_CLIENT_POLICY_RECOVERY_REQUIRED"):
        registry.recover_pending(3)
    pending.unlink()
    prepared = registry._json_bytes({"schema_version": 1, "policy_revision": 4,
        "canonical_origin": "https://router.example.test", "clients": []})
    intent = {"schema_version": 1, "target": "registry", "previous_sha256": "0" * 64,
              "prepared_sha256": hashlib.sha256(prepared).hexdigest(),
              "prepared_b64": base64.b64encode(prepared).decode("ascii"),
              "policy_revision": 4}
    registry._atomic_publish(pending, registry._json_bytes(intent))
    with pytest.raises(registry.RegistryError, match="API_CLIENT_POLICY_RECOVERY_REQUIRED"):
        registry.recover_pending(4)
    assert pending.is_file()


@pytest.mark.parametrize("scopes", [[], ["Bad Scope"], ["ui.client-list.write", "ui.client-list.write"], [1]])
def test_malformed_legacy_scopes_are_rejected_before_cutover(monkeypatch, tmp_path, scopes):
    _, verifier, _, _ = _paths(monkeypatch, tmp_path)
    malformed = {"schema_version": 1, "principal": "presentation-operator", "scopes": scopes,
                 "token_sha256": "a" * 64, "canonical_origin": "https://router.example.test"}
    verifier.write_text(json.dumps(malformed), encoding="utf-8")
    verifier.chmod(0o600)
    with pytest.raises(registry.RegistryError, match="API_CLIENT_CUTOVER_STATE_INVALID"):
        registry.selector_mode()


def test_admin_cas_rotation_redaction_and_revoke_credential(monkeypatch, tmp_path, capsys):
    host, created = _cut_over(monkeypatch, tmp_path, capsys)
    cli = created["cli"]
    rotated_path = host / "rotated.token"
    rotated = _admin(monkeypatch, ["rotate", cli["client_id"], "--expected-revision", "3",
                                    "--secret-file", str(rotated_path)], capsys)
    assert rotated["policy_revision"] == 4
    token = rotated_path.read_text(encoding="ascii").strip()
    listed = _admin(monkeypatch, ["list"], capsys)
    serialized = json.dumps(listed)
    assert cli["token"] not in serialized and token not in serialized
    assert hashlib.sha256(cli["token"].encode("ascii")).hexdigest() not in serialized
    policy_raw = registry.REGISTRY_PATH.read_text(encoding="utf-8")
    digest = hashlib.sha256(cli["token"].encode("ascii")).hexdigest()
    assert digest in policy_raw
    conflict = main_status(monkeypatch, ["rename", cli["client_id"], "--expected-revision", "3", "--tag", "new"], capsys)
    assert conflict == 2
    assert "API_CLIENT_POLICY_REVISION_CONFLICT" in capsys.readouterr().err
    _admin(monkeypatch, ["revoke-credential", cli["client_id"], rotated["credential_id"],
                         "--expected-revision", "4"], capsys)
    assert _admin(monkeypatch, ["validate"], capsys)["valid"] is True


def test_rotate_does_not_resurrect_scope_after_all_credentials_revoked(monkeypatch, tmp_path, capsys):
    host, verifier, _, _ = _paths(monkeypatch, tmp_path)
    _legacy(verifier, secrets.token_hex(32))
    _admin(monkeypatch, ["init", "--canonical-origin", "https://router.example.test"], capsys)
    secret = host / "cli.token"
    created = _admin(monkeypatch, ["create", "--expected-revision", "1", "--kind", "cli",
        "--tag", "cli", "--scope", "ui.client-list.write", "--secret-file", str(secret)], capsys)
    assert main_status(monkeypatch, ["revoke-credential", created["client_id"],
        created["credential_id"], "--expected-revision", "2"], capsys) == 0
    rotated_secret = host / "rotated.token"
    assert main_status(monkeypatch, ["rotate", created["client_id"], "--expected-revision", "3",
        "--secret-file", str(rotated_secret)], capsys) == 2
    assert "API_CLIENT_NO_ACTIVE_SCOPED_CREDENTIAL" in capsys.readouterr().err
    assert not rotated_secret.exists()
    assert registry.load_policy()["policy_revision"] == 3


def test_cutover_fence_failure_fails_closed_and_resumes_only_exact_prepared_policy(monkeypatch, tmp_path, capsys):
    host, verifier, _, fence = _paths(monkeypatch, tmp_path)
    _legacy(verifier, secrets.token_hex(32))
    _admin(monkeypatch, ["init", "--canonical-origin", "https://router.example.test"], capsys)
    for index, kind in enumerate(("web_ui", "cli"), start=1):
        _admin(monkeypatch, ["create", "--expected-revision", str(index), "--kind", kind,
            "--tag", kind, "--scope", "ui.client-list.write", "--secret-file",
            str(host / f"{kind}.token")], capsys)
    original_publish = registry._atomic_publish

    def fail_selector(path: Path, data: bytes, **kwargs):
        if path == verifier:
            raise registry.RegistryError("API_CLIENT_POLICY_PUBLICATION_INCOMPLETE")
        return original_publish(path, data, **kwargs)

    monkeypatch.setattr(registry, "_atomic_publish", fail_selector)
    from fwrouter_api.cli import main
    assert main(["api-clients", "cutover", "--expected-revision", "3"]) == 2
    assert "API_CLIENT_POLICY_PUBLICATION_INCOMPLETE" in capsys.readouterr().err
    assert fence.is_file() and verifier.read_text(encoding="utf-8").find('"schema_version": 1') >= 0
    with pytest.raises(registry.RegistryError, match="API_CLIENT_POLICY_RECOVERY_REQUIRED"):
        registry.selector_mode()
    # Mutations cannot silently invalidate the prepared state while fenced.
    assert main(["api-clients", "rename", "a" * 32, "--expected-revision", "3", "--tag", "x"]) == 2
    capsys.readouterr()
    monkeypatch.setattr(registry, "_atomic_publish", original_publish)
    assert main(["api-clients", "recover", "--expected-revision", "3"]) == 0
    capsys.readouterr()
    assert registry.selector_mode()[0] == "registry"


def test_selector_postreplace_fsync_uncertainty_stops_api_until_recovery(monkeypatch, tmp_path, capsys):
    host, verifier, _, _ = _paths(monkeypatch, tmp_path)
    _legacy(verifier, secrets.token_hex(32))
    _admin(monkeypatch, ["init", "--canonical-origin", "https://router.example.test"], capsys)
    created = []
    for revision, kind in enumerate(("web_ui", "cli"), start=1):
        created.append(_admin(monkeypatch, ["create", "--expected-revision", str(revision),
            "--kind", kind, "--tag", kind, "--scope", "ui.client-list.write",
            "--secret-file", str(host / f"{kind}.token")], capsys))
    original_fsync = registry.os.fsync
    calls = 0

    def fail_selector_directory(fd: int):
        nonlocal calls
        calls += 1
        # Fence (2), pending intent write and readback (4), selector temp and
        # post-replace parent sync (2).
        if calls == 8:
            raise OSError("injected post-replace selector directory fsync failure")
        return original_fsync(fd)

    monkeypatch.setattr(registry.os, "fsync", fail_selector_directory)
    from fwrouter_api.cli import main
    assert main(["api-clients", "cutover", "--expected-revision", "3"]) == 2
    capsys.readouterr()
    api = TestClient(_app(), client=("127.0.0.1", 45678), headers={"Host": admission.LOCAL_AUTHORITY})
    response = api.post("/presentation", headers={"Authorization": f"Bearer {secrets.token_hex(32)}"})
    assert response.status_code == 503
    assert registry.SELECTOR_PATH.read_bytes() == b'{"provider":"client_registry_v1","schema_version":2}\n'
    monkeypatch.setattr(registry.os, "fsync", original_fsync)
    assert main(["api-clients", "recover", "--expected-revision", "3"]) == 0
    capsys.readouterr()
    assert registry.selector_mode()[0] == "registry"


def test_publication_failures_distinguish_pre_rename_and_after_rename(monkeypatch, tmp_path):
    _, _, path, _ = _paths(monkeypatch, tmp_path)
    before = {"schema_version": 1, "policy_revision": 1,
              "canonical_origin": "https://router.example.test", "clients": []}
    after = {**before, "policy_revision": 2}
    registry.publish_policy(before)
    original_replace = registry.os.replace

    def fail_before(*_args, **_kwargs):
        raise OSError("injected rename failure")

    monkeypatch.setattr(registry.os, "replace", fail_before)
    with pytest.raises(registry.RegistryError, match="API_CLIENT_POLICY_PUBLICATION_INCOMPLETE"):
        registry.publish_policy(after)
    assert registry.load_policy()["policy_revision"] == 1
    monkeypatch.setattr(registry.os, "replace", original_replace)
    from fwrouter_api.cli import main
    assert main(["api-clients", "recover", "--expected-revision", "2"]) == 0
    assert registry.load_policy()["policy_revision"] == 2
    original_fsync = registry.os.fsync
    calls = 0
    uncertain = {**after, "policy_revision": 3}

    def fail_directory_fsync(fd: int):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected directory fsync failure")
        return original_fsync(fd)

    monkeypatch.setattr(registry.os, "fsync", fail_directory_fsync)
    with pytest.raises(registry.RegistryError, match="API_CLIENT_POLICY_PUBLICATION_INCOMPLETE"):
        registry.publish_policy(uncertain)
    with pytest.raises(registry.RegistryError, match="API_CLIENT_POLICY_RECOVERY_REQUIRED"):
        registry.selector_mode()
    monkeypatch.setattr(registry.os, "fsync", original_fsync)
    from fwrouter_api.cli import main
    assert main(["api-clients", "recover", "--expected-revision", "3"]) == 0
    assert registry.load_policy()["policy_revision"] == 3


def test_zero_length_write_fails_without_hanging_or_publishing(monkeypatch, tmp_path):
    _, _, path, _ = _paths(monkeypatch, tmp_path)
    policy = {"schema_version": 1, "policy_revision": 1,
              "canonical_origin": "https://router.example.test", "clients": []}
    monkeypatch.setattr(registry.os, "write", lambda _fd, _view: 0)
    with pytest.raises(registry.RegistryError, match="API_CLIENT_POLICY_PUBLICATION_INCOMPLETE"):
        registry.publish_policy(policy)
    assert not path.exists()
    assert not list(path.parent.glob(".api-clients.json.*.tmp"))


def test_repeated_policy_lock_release_does_not_leak_directory_descriptors(monkeypatch, tmp_path):
    _paths(monkeypatch, tmp_path)
    registry._unlock(registry._lock(False))
    baseline = len(os.listdir("/proc/self/fd"))
    for _ in range(25):
        registry._unlock(registry._lock(False))
    assert len(os.listdir("/proc/self/fd")) == baseline


def test_secret_delivery_failure_never_publishes_credential(monkeypatch, tmp_path, capsys):
    host, verifier, path, _ = _paths(monkeypatch, tmp_path)
    _legacy(verifier, secrets.token_hex(32))
    _admin(monkeypatch, ["init", "--canonical-origin", "https://router.example.test"], capsys)
    secret = host / "already-exists.token"
    secret.write_text("do-not-clobber", encoding="ascii")
    from fwrouter_api.cli import main
    assert main(["api-clients", "create", "--expected-revision", "1", "--kind", "integration",
        "--tag", "automation", "--scope", "ui.client-list.write", "--secret-file", str(secret)]) == 2
    output = capsys.readouterr()
    assert "API_CLIENT_OUTPUT_EXISTS" in output.err, output.err
    policy = registry.load_policy()
    assert policy["policy_revision"] == 1 and policy["clients"] == []
    assert secret.read_text(encoding="ascii") == "do-not-clobber"


def test_prepublication_failure_cleans_delivered_but_unregistered_secret(monkeypatch, tmp_path, capsys):
    host, verifier, _, _ = _paths(monkeypatch, tmp_path)
    _legacy(verifier, secrets.token_hex(32))
    _admin(monkeypatch, ["init", "--canonical-origin", "https://router.example.test"], capsys)
    secret = host / "temporary.token"
    original_replace = registry.os.replace

    def fail_registry(path, target, **kwargs):
        if str(target).endswith("api-clients.json"):
            raise OSError("injected pre-publication error")
        return original_replace(path, target, **kwargs)

    monkeypatch.setattr(registry.os, "replace", fail_registry)
    from fwrouter_api.cli import main
    assert main(["api-clients", "create", "--expected-revision", "1", "--kind", "cli",
        "--tag", "cli", "--scope", "ui.client-list.write", "--secret-file", str(secret)]) == 2
    result = capsys.readouterr()
    assert "API_CLIENT_POLICY_PUBLICATION_INCOMPLETE" in result.err
    assert secret.exists()
    monkeypatch.setattr(registry.os, "replace", original_replace)
    from fwrouter_api.cli import main
    assert main(["api-clients", "recover", "--expected-revision", "2"]) == 0
    assert registry.load_policy()["clients"][0]["credentials"][0]["token_sha256"] == hashlib.sha256(
        secret.read_text(encoding="ascii").encode("ascii")).hexdigest()


def test_pending_creation_failure_cleans_secret_before_any_target_intent(monkeypatch, tmp_path, capsys):
    host, verifier, _, _ = _paths(monkeypatch, tmp_path)
    _legacy(verifier, secrets.token_hex(32))
    _admin(monkeypatch, ["init", "--canonical-origin", "https://router.example.test"], capsys)
    secret = host / "not-registered.token"
    original = registry._atomic_publish

    def fail_intent(path: Path, data: bytes, **kwargs):
        if path == registry.PENDING_PATH:
            raise registry.RegistryError("API_CLIENT_POLICY_PUBLICATION_INCOMPLETE")
        return original(path, data, **kwargs)

    monkeypatch.setattr(registry, "_atomic_publish", fail_intent)
    from fwrouter_api.cli import main
    assert main(["api-clients", "create", "--expected-revision", "1", "--kind", "cli",
        "--tag", "cli", "--scope", "ui.client-list.write", "--secret-file", str(secret)]) == 2
    capsys.readouterr()
    assert not secret.exists()
    assert not registry.PENDING_PATH.exists()


def main_status(monkeypatch, command, capsys) -> int:
    from fwrouter_api.cli import main

    return main(["api-clients", *command])


def test_durable_cutover_fence_stops_legacy_and_registry_corruption_has_no_fallback(monkeypatch, tmp_path, capsys):
    host, created = _cut_over(monkeypatch, tmp_path, capsys)
    verifier = registry.SELECTOR_PATH
    fence_bytes = registry.FENCE_PATH.read_bytes()
    registry.FENCE_PATH.unlink()
    with pytest.raises(registry.RegistryError, match="API_CLIENT_CUTOVER_STATE_INVALID"):
        registry.selector_mode()
    registry.FENCE_PATH.write_bytes(fence_bytes)
    registry.FENCE_PATH.chmod(0o600)
    legacy_token = secrets.token_hex(32)
    _legacy(verifier, legacy_token)
    client = TestClient(_app(), client=("127.0.0.1", 45678), headers={"Host": admission.LOCAL_AUTHORITY})
    stopped = client.post("/presentation", headers={"Authorization": f"Bearer {legacy_token}"})
    assert stopped.status_code == 503
    assert stopped.json()["error"]["code"] == "PRESENTATION_AUTH_CONFIG_UNAVAILABLE"
    selector = b'{"provider":"client_registry_v1","schema_version":2}\n'
    registry._atomic_publish(verifier, selector)
    registry.REGISTRY_PATH.unlink()
    stopped_again = client.post("/presentation", headers={
        "Authorization": f"Bearer {created['cli']['token']}"})
    assert stopped_again.status_code == 503
    assert registry.FENCE_PATH.is_file()


def test_secure_loader_rejects_hardlink_symlink_duplicate_digest_and_bidi_tag(monkeypatch, tmp_path):
    _, _, path, _ = _paths(monkeypatch, tmp_path)
    token_digest = hashlib.sha256(secrets.token_hex(32).encode("ascii")).hexdigest()
    doc = {"schema_version": 1, "policy_revision": 1, "canonical_origin": "https://router.example.test",
           "clients": [{"client_id": "a" * 32, "kind": "integration", "display_tag": "safe",
             "status": "active", "credentials": [{"credential_id": "b" * 32,
             "token_sha256": token_digest, "status": "active", "scopes": ["ui.client-list.write"]}]}]}
    path.write_text(json.dumps(doc), encoding="utf-8")
    path.chmod(0o600)
    assert registry.load_policy()["policy_revision"] == 1
    hardlink = path.with_name("hardlink")
    os.link(path, hardlink)
    with pytest.raises(registry.RegistryError):
        registry.load_policy()
    hardlink.unlink()
    target = path.with_name("target")
    target.write_bytes(path.read_bytes())
    target.chmod(0o600)
    path.unlink()
    path.symlink_to(target)
    with pytest.raises(registry.RegistryError):
        registry.load_policy()
    path.unlink()
    doc["clients"][0]["display_tag"] = "bad\u202eTag"
    path.write_text(json.dumps(doc), encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(registry.RegistryError):
        registry.load_policy()


@pytest.mark.parametrize("origin", [
    "https://bad_host.example", "https://bad..example", "https://-bad.example",
    "https://bad-.example", "https://example.test/%2f", "https://example.test:443",
    "https://user@example.test", "https://[2001:0db8::1]", "https://[::1%25eth0]",
    "https://127.1", "https://2130706433", "https://0177.0.0.1", "https://0x7f.0.0.1",
    "https://router.123", "https://router.0x7f",
])
def test_origin_requires_exact_canonical_host_syntax(origin):
    assert registry.valid_origin(origin) is False


@pytest.mark.parametrize("origin", [
    "https://router.example.test", "https://router.example.test:8443",
    "https://192.0.2.1", "https://[2001:db8::1]",
])
def test_origin_accepts_canonical_dns_ip_and_nondefault_ports(origin):
    assert registry.valid_origin(origin) is True


def test_absent_selector_bootstrap_set_origin_cas_and_client_preservation(monkeypatch, tmp_path, capsys):
    host, selector, _, fence = _paths(monkeypatch, tmp_path)
    assert not selector.exists() and not fence.exists()
    _admin(monkeypatch, ["init", "--canonical-origin", "https://router.example.test"], capsys)
    listed = _admin(monkeypatch, ["list"], capsys)
    assert listed["policy_revision"] == 1
    assert listed["mode"] == "unselected" and listed["admission_ready"] is False
    validated = _admin(monkeypatch, ["validate"], capsys)
    assert validated["valid"] is True
    assert validated["mode"] == "unselected" and validated["admission_ready"] is False
    token_path = host / "integration.token"
    created = _admin(monkeypatch, ["create", "--expected-revision", "1", "--kind", "integration",
        "--tag", "home", "--scope", "events.read", "--secret-file", str(token_path)], capsys)
    api = TestClient(_app(), client=("127.0.0.1", 45678), headers={"Host": admission.LOCAL_AUTHORITY})
    denied_before_selector = api.post("/presentation", headers={
        "Authorization": f"Bearer {token_path.read_text(encoding='ascii').strip()}"})
    assert denied_before_selector.status_code == 503
    before = registry.load_policy()
    changed = _admin(monkeypatch, ["set-origin", "--expected-revision", "2",
        "--canonical-origin", "https://new-router.example.test:8443"], capsys)
    after = registry.load_policy()
    assert changed == {"changed": True, "policy_revision": 3}
    assert after["canonical_origin"] == "https://new-router.example.test:8443"
    assert after["clients"] == before["clients"]
    assert created["policy_revision"] == 2
    assert _admin(monkeypatch, ["set-origin", "--expected-revision", "3",
        "--canonical-origin", after["canonical_origin"]], capsys) == {
            "changed": False, "policy_revision": 3}
    assert registry.load_policy()["policy_revision"] == 3
    assert main_status(monkeypatch, ["set-origin", "--expected-revision", "2",
        "--canonical-origin", "https://stale.example.test"], capsys) == 2
    assert "API_CLIENT_POLICY_REVISION_CONFLICT" in capsys.readouterr().err
    assert not selector.exists() and not fence.exists()
    with pytest.raises(registry.RegistryError, match="API_CLIENT_CUTOVER_STATE_INVALID"):
        registry.selector_mode()


def test_concurrent_origin_changes_commit_one_revision(monkeypatch, tmp_path):
    _paths(monkeypatch, tmp_path)
    registry.publish_policy({"schema_version": 1, "policy_revision": 1,
        "canonical_origin": "https://router.example.test", "clients": []})

    def change(origin):
        from types import SimpleNamespace
        from fwrouter_api.services.api_client_registry_admin import _run
        try:
            return ("ok", _run(SimpleNamespace(command="set-origin", expected_revision=1,
                                                canonical_origin=origin)))
        except registry.RegistryError as exc:
            return ("error", exc.code)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(change, ("https://one.example.test", "https://two.example.test")))
    assert sum(result[0] == "ok" for result in results) == 1
    assert sum(result == ("error", "API_CLIENT_POLICY_REVISION_CONFLICT") for result in results) == 1
    assert registry.load_policy()["policy_revision"] == 2


def test_init_and_origin_change_refuse_selector_fence_pending_and_corrupt_states(monkeypatch, tmp_path, capsys):
    host, selector, _, fence = _paths(monkeypatch, tmp_path)
    _admin(monkeypatch, ["init", "--canonical-origin", "https://router.example.test"], capsys)
    registry.PENDING_PATH.write_text("{}", encoding="ascii")
    registry.PENDING_PATH.chmod(0o600)
    assert main_status(monkeypatch, ["set-origin", "--expected-revision", "1",
        "--canonical-origin", "https://pending.example.test"], capsys) == 2
    assert "API_CLIENT_POLICY_RECOVERY_REQUIRED" in capsys.readouterr().err
    registry.PENDING_PATH.unlink()
    _legacy(selector, secrets.token_hex(32))
    assert main_status(monkeypatch, ["set-origin", "--expected-revision", "1",
        "--canonical-origin", "https://new.example.test"], capsys) == 2
    assert "API_CLIENT_ORIGIN_CHANGE_FORBIDDEN" in capsys.readouterr().err
    assert main_status(monkeypatch, ["init", "--canonical-origin", "https://new.example.test"], capsys) == 2
    capsys.readouterr()
    selector.unlink()
    fence.write_text(json.dumps({"schema_version": 1, "registry_required": True,
        "prepared_sha256": "a" * 64}), encoding="ascii")
    fence.chmod(0o600)
    assert main_status(monkeypatch, ["init", "--canonical-origin", "https://router.example.test"], capsys) == 2
    assert "API_CLIENT_CUTOVER_REQUIRED" in capsys.readouterr().err
    assert registry.load_policy()["policy_revision"] == 1
    registry.REGISTRY_PATH.unlink()
    with pytest.raises(registry.RegistryError, match="API_CLIENT_CUTOVER_STATE_INVALID"):
        registry.selector_mode()
    assert main_status(monkeypatch, ["init", "--canonical-origin", "https://router.example.test"], capsys) == 2
    assert "API_CLIENT_CUTOVER_REQUIRED" in capsys.readouterr().err
    assert not registry.REGISTRY_PATH.exists()


def test_init_refuses_active_registry_selector_without_registry(monkeypatch, tmp_path, capsys):
    _, selector, _, _ = _paths(monkeypatch, tmp_path)
    registry._atomic_publish(selector, b'{"provider":"client_registry_v1","schema_version":2}\n')
    assert main_status(monkeypatch, ["init", "--canonical-origin", "https://router.example.test"], capsys) == 2
    assert "API_CLIENT_CUTOVER_REQUIRED" in capsys.readouterr().err
    assert not registry.REGISTRY_PATH.exists()


def test_origin_pending_recovery_cannot_publish_behind_new_fence_or_selector(monkeypatch, tmp_path):
    _, selector, _, fence = _paths(monkeypatch, tmp_path)
    policy = {"schema_version": 1, "policy_revision": 1,
        "canonical_origin": "https://router.example.test", "clients": []}
    registry.publish_policy(policy)
    changed = {**policy, "policy_revision": 2, "canonical_origin": "https://new.example.test"}
    def fail_pending_cleanup(_path):
        raise registry.RegistryError("API_CLIENT_POLICY_PUBLICATION_INCOMPLETE")

    monkeypatch.setattr(registry, "_clear_pending", fail_pending_cleanup)
    with pytest.raises(registry.RegistryError, match="API_CLIENT_POLICY_PUBLICATION_INCOMPLETE"):
        registry.publish_target("registry_origin", registry._json_bytes(changed), 2)
    current = registry.load_policy()
    assert current["policy_revision"] == 2
    assert current["canonical_origin"] == "https://new.example.test"
    fence.write_text(json.dumps({"schema_version": 1, "registry_required": True,
        "prepared_sha256": hashlib.sha256(registry.REGISTRY_PATH.read_bytes()).hexdigest()}), encoding="ascii")
    fence.chmod(0o600)
    registry._atomic_publish(selector, b'{"provider":"client_registry_v1","schema_version":2}\n')
    with pytest.raises(registry.RegistryError, match="API_CLIENT_ORIGIN_CHANGE_FORBIDDEN"):
        registry.recover_pending(2)
    assert registry._pending_state() is not None


def test_origin_pending_purpose_cannot_initialize_registry_or_recover_after_registry_loss(
        monkeypatch, tmp_path):
    _paths(monkeypatch, tmp_path)
    prepared = {"schema_version": 1, "policy_revision": 1,
        "canonical_origin": "https://router.example.test", "clients": []}
    prepared_bytes = registry._json_bytes(prepared)
    impossible_initializer = {"schema_version": 1, "target": "registry_origin",
        "previous_sha256": None, "prepared_sha256": hashlib.sha256(prepared_bytes).hexdigest(),
        "prepared_b64": base64.b64encode(prepared_bytes).decode("ascii"), "policy_revision": 1}
    registry._atomic_publish(registry.PENDING_PATH, registry._json_bytes(impossible_initializer))
    with pytest.raises(registry.RegistryError, match="API_CLIENT_POLICY_RECOVERY_REQUIRED"):
        registry.recover_pending(1)
    registry.PENDING_PATH.unlink()

    old = {**prepared, "policy_revision": 1}
    new = {**old, "policy_revision": 2, "canonical_origin": "https://new.example.test"}
    registry.publish_policy(old)
    intent = {"schema_version": 1, "target": "registry_origin",
        "previous_sha256": hashlib.sha256(registry._json_bytes(old)).hexdigest(),
        "prepared_sha256": hashlib.sha256(registry._json_bytes(new)).hexdigest(),
        "prepared_b64": base64.b64encode(registry._json_bytes(new)).decode("ascii"),
        "policy_revision": 2}
    registry._atomic_publish(registry.PENDING_PATH, registry._json_bytes(intent))
    registry.REGISTRY_PATH.unlink()
    with pytest.raises(registry.RegistryError, match="API_CLIENT_POLICY_RECOVERY_REQUIRED"):
        registry.recover_pending(2)


def test_cutover_rejects_pending_before_creating_fence(monkeypatch, tmp_path, capsys):
    host, _selector, _, fence = _paths(monkeypatch, tmp_path)
    _admin(monkeypatch, ["init", "--canonical-origin", "https://router.example.test"], capsys)
    for revision, kind in enumerate(("web_ui", "cli"), start=1):
        _admin(monkeypatch, ["create", "--expected-revision", str(revision), "--kind", kind,
            "--tag", kind, "--scope", "ui.client-list.write", "--secret-file",
            str(host / f"{kind}.token")], capsys)
    pending = {"schema_version": 1, "target": "registry", "previous_sha256": None,
        "prepared_sha256": "a" * 64, "prepared_b64": "e30=", "policy_revision": 3}
    registry._atomic_publish(registry.PENDING_PATH, registry._json_bytes(pending))
    assert main_status(monkeypatch, ["cutover", "--expected-revision", "3"], capsys) == 2
    assert "API_CLIENT_POLICY_RECOVERY_REQUIRED" in capsys.readouterr().err
    assert not fence.exists()


def test_registry_rejects_duplicate_ids_digests_and_json_members(monkeypatch, tmp_path):
    _, _, path, _ = _paths(monkeypatch, tmp_path)
    digest = hashlib.sha256(secrets.token_hex(32).encode("ascii")).hexdigest()
    credential = {"credential_id": "b" * 32, "token_sha256": digest,
                  "status": "active", "scopes": ["ui.client-list.write"]}
    first = {"client_id": "a" * 32, "kind": "integration", "display_tag": "one",
             "status": "active", "credentials": [credential]}
    second = {"client_id": "c" * 32, "kind": "integration", "display_tag": "two",
              "status": "active", "credentials": [{**credential, "credential_id": "d" * 32}]}
    policy = {"schema_version": 1, "policy_revision": 1,
              "canonical_origin": "https://router.example.test", "clients": [first, second]}
    with pytest.raises(registry.RegistryError, match="API_CLIENT_REGISTRY_INVALID"):
        registry.validate_policy(policy)
    policy["clients"] = [first, {**second, "client_id": first["client_id"]}]
    with pytest.raises(registry.RegistryError, match="API_CLIENT_REGISTRY_INVALID"):
        registry.validate_policy(policy)
    path.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(registry.RegistryError, match="API_CLIENT_REGISTRY_INVALID"):
        registry.load_policy()
def test_policy_directory_permissions_are_checked_without_environment_overrides(monkeypatch, tmp_path):
    _, _, path, _ = _paths(monkeypatch, tmp_path)
    path.write_text("{}", encoding="utf-8")
    path.chmod(0o600)
    path.parent.chmod(0o770)
    with pytest.raises(registry.RegistryError):
        registry.load_policy()
