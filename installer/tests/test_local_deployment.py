"""Public deployment command regressions using disposable installer targets."""
from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
from types import SimpleNamespace
import zipfile

import pytest


ROOT = Path(__file__).resolve().parents[2]
INSTALLER = ROOT / "installer" / "install.sh"
MODULE = ROOT / "installer" / "local_deploy.py"


def command(argv: list[str], *, timeout: int = 300) -> subprocess.CompletedProcess:
    result = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, timeout=timeout, check=False)
    assert result.returncode == 0, result.stderr[-4000:]
    return result


def fd_identity(fd: int) -> tuple[int, int, int] | None:
    try:
        info = os.fstat(fd)
    except OSError:
        return None
    return info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)


def cleanup_test_tmp_path(tmp_path: Path) -> None:
    """Remove this test's disposable clones and installer targets promptly."""
    if tmp_path.is_symlink():
        raise AssertionError("pytest temporary test root must not be a symlink")
    if tmp_path.exists():
        shutil.rmtree(tmp_path)


def pristine_checkout(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    command(["/usr/bin/git", "clone", "--no-hardlinks", "--quiet", str(ROOT), str(source)])
    return source


def installed_command(tmp_path: Path) -> tuple[Path, Path, Path]:
    target = tmp_path / "target"
    (target / "opt").mkdir(parents=True)
    (target / "etc/systemd/system").mkdir(parents=True)
    (target / "usr/local/libexec/fwrouter").mkdir(parents=True)
    command(["sh", str(INSTALLER), "--deploy", "--component", "deploy", "--target", str(target)])
    module = target / "usr/local/libexec/fwrouter/local_deploy.py"
    wrapper = target / "usr/local/sbin/fwrouter-deploy"
    assert module.is_file()
    assert stat.S_IMODE(wrapper.stat().st_mode) == 0o755
    return target, module, wrapper


def test_component_install_and_real_check_entrypoint_are_local_and_read_only(
        tmp_path: Path, request) -> None:
    request.addfinalizer(lambda: cleanup_test_tmp_path(tmp_path))
    target, module, wrapper = installed_command(tmp_path)
    source = pristine_checkout(tmp_path)
    sys.path.insert(0, str(module.parent))
    import local_deploy

    guarded = source / "README.md"
    guarded.chmod(0o664)
    with pytest.raises(local_deploy.DeploymentError, match="writable entry"):
        local_deploy._source_commit(source, os.geteuid())
    guarded.chmod(0o644)

    paths = local_deploy.Paths(config=target / "etc/fwrouter/deploy-source.json",
                               target_root=target, state_dir=target / "var/lib/fwrouter-v2/state/controlled-rollout",
                               owner_uid=os.geteuid(), test_driver=True)
    assert local_deploy.main(["--configure-source", str(source)], paths_override=paths) == 0
    config_before = paths.config.read_bytes()
    marker = target / "var/lib/fwrouter-v2/operator-state"
    marker.parent.mkdir(parents=True)
    marker.write_bytes(b"durable-state\x00preserved")
    before = marker.read_bytes()
    driver = (
        "import sys; from pathlib import Path; import local_deploy; "
        "root=Path(sys.argv[1]); p=local_deploy.Paths(config=root/'etc/fwrouter/deploy-source.json', "
        "target_root=root, state_dir=root/'var/lib/fwrouter-v2/state/controlled-rollout', "
        "owner_uid=__import__('os').geteuid(), test_driver=True, api_healthy=False); "
        "raise SystemExit(local_deploy.main(['--check'], paths_override=p))"
    )
    result = subprocess.run([sys.executable, "-c", driver, str(target)], cwd=module.parent,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    assert result.returncode == 0, result.stderr
    check_result = json.loads(result.stdout.splitlines()[-1])
    assert check_result["status"] == "READY_TO_APPLY"
    assert check_result["deployment_identity"] == {
        "status": "UNKNOWN", "reason": "legacy_install_without_identity_record"}
    assert paths.config.read_bytes() == config_before
    assert marker.read_bytes() == before
    assert not paths.state_dir.exists()
    assert wrapper.read_text(encoding="utf-8").startswith("#!/bin/sh\n")
    assert "/usr/bin/python3 -I -B " in wrapper.read_text(encoding="utf-8")
    paths.config.write_text("{}\n", encoding="utf-8")
    error_result = subprocess.run([sys.executable, "-c", driver, str(target)], cwd=module.parent,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    assert error_result.returncode == 1
    assert "source config" in error_result.stderr
    paths.config.write_bytes(config_before)


def test_deploy_component_bootstrap_is_atomic_and_rejects_wrapper_drift(tmp_path: Path, request) -> None:
    request.addfinalizer(lambda: cleanup_test_tmp_path(tmp_path))
    target, module, wrapper = installed_command(tmp_path)
    old_module = module.read_bytes()
    wrapper_inode = wrapper.stat().st_ino
    command(["sh", str(INSTALLER), "--deploy", "--component", "deploy", "--target", str(target)])
    assert module.read_bytes() == old_module
    assert wrapper.stat().st_ino == wrapper_inode
    wrapper.write_bytes(b"unexpected wrapper change\n")
    result = subprocess.run(["sh", str(INSTALLER), "--deploy", "--component", "deploy", "--target", str(target)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    assert result.returncode != 0
    assert "wrapper" in result.stderr
    assert module.read_bytes() == old_module


def test_deploy_only_bootstrap_uses_common_lock_and_skips_generic_post_actions(tmp_path: Path, request) -> None:
    request.addfinalizer(lambda: cleanup_test_tmp_path(tmp_path))
    target, module, _wrapper = installed_command(tmp_path)
    bootstrap = target / "opt/fwrouter-api/scripts/bootstrap-state.sh"
    bootstrap.parent.mkdir(parents=True, exist_ok=True)
    (target / "var/lib").mkdir(parents=True, exist_ok=True)
    bootstrap.write_text('#!/bin/sh\ntouch "$1.touched"\n', encoding="utf-8")
    bootstrap.chmod(0o755)
    command(["sh", str(INSTALLER), "--deploy", "--component", "deploy", "--target", str(target)])
    assert module.is_file()
    assert (target / "opt/.fwrouter-component-install.lock").is_file()
    assert not (target / "var/lib/fwrouter-v2.touched").exists()


@pytest.fixture
def self_upgrade_fixture(tmp_path: Path, request, monkeypatch):
    target, module_path, wrapper_path = installed_command(tmp_path)
    import importlib.util
    spec = importlib.util.spec_from_file_location("test_updater_self_upgrade", MODULE)
    deploy = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = deploy
    spec.loader.exec_module(deploy)
    source = pristine_checkout(tmp_path / "candidate")
    candidate_module = source / "installer/local_deploy.py"
    candidate_module.write_bytes(MODULE.read_bytes() + b"\n# exact self-upgrade candidate\n")
    command(["/usr/bin/git", "-C", str(source), "add", "installer/local_deploy.py"])
    command(["/usr/bin/git", "-C", str(source), "-c", "user.name=FWRouter Test",
             "-c", "user.email=fwrouter-test@example.invalid", "commit", "--quiet", "-m", "qualified candidate"])
    sha = subprocess.check_output(["/usr/bin/git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    paths = deploy.Paths(config=target / "etc/fwrouter/deploy-source.json", target_root=target,
                         state_dir=target / "var/lib/fwrouter-v2/state/controlled-rollout",
                         owner_uid=os.geteuid(), test_driver=True)
    paths.state_dir.mkdir(parents=True, mode=0o700)
    paths.state_dir.chmod(0o700)
    deploy._write_config(paths.config, source, sha, paths.owner_uid)
    bundle_zip = tmp_path / "bundle.zip"
    with zipfile.ZipFile(bundle_zip, "w") as bundle:
        bundle.writestr("metadata.json", "{}")
    bundle_bytes = bundle_zip.read_bytes()
    artifact_digest = "sha256:" + hashlib.sha256(bundle_bytes).hexdigest()
    api_calls = {"main": 0}

    def api(endpoint: str, **_kwargs):
        if endpoint == "repos/Coxf0rd/fwrouter/commits/main":
            api_calls["main"] += 1
            return {"sha": sha}
        if endpoint == "repos/Coxf0rd/fwrouter/actions/runs/101":
            return {"id": 101, "run_attempt": 2, "event": "push", "head_branch": "main",
                    "head_sha": sha, "status": "completed", "conclusion": "success",
                    "path": ".github/workflows/test-gates.yml",
                    "repository": {"full_name": "Coxf0rd/fwrouter"}}
        if endpoint.endswith("/runs/101/jobs?per_page=100"):
            return {"total_count": 1, "jobs": [{"name": "Affected L0-L5 with hosted profiles",
                                                   "status": "completed", "conclusion": "success"}]}
        if endpoint.endswith("/artifacts/202"):
            return {"id": 202, "expired": False,
                    "name": f"fwrouter-main-evidence-{sha}-101-2", "digest": artifact_digest,
                    "size_in_bytes": len(bundle_bytes),
                    "workflow_run": {"id": 101, "head_sha": sha, "head_branch": "main"}}
        raise AssertionError(endpoint)

    monkeypatch.setattr(deploy, "_gh_api", api)
    monkeypatch.setattr(deploy, "_gh_artifact_zip",
                        lambda _artifact, destination, **_kwargs: destination.write_bytes(bundle_bytes))
    original_run = deploy._run

    def trusted_verify(argv, **kwargs):
        if "evidence_bundle.py" in " ".join(map(str, argv)):
            return subprocess.CompletedProcess(argv, 0, json.dumps({
                "status": "evidence_verified_not_deploy_authorized", "source_sha": sha,
                "run_id": "101", "run_attempt": "2", "strict_aggregate": "passed",
                "offline_source_checkout": "verified"}), "")
        return original_run(argv, **kwargs)

    monkeypatch.setattr(deploy, "_run", trusted_verify)
    args = SimpleNamespace(main_sha=sha, run_id="101", run_attempt="2", artifact_id="202",
                           artifact_digest=artifact_digest,
                           expected_installed_module_sha256=deploy._digest(module_path),
                           confirm_main_sha=sha)
    request.addfinalizer(lambda: cleanup_test_tmp_path(tmp_path))
    return deploy, paths, source, sha, module_path, wrapper_path, args, api_calls


def test_self_upgrade_requires_actual_main_pins_and_replaces_only_module(self_upgrade_fixture) -> None:
    deploy, paths, source, sha, module_path, wrapper_path, args, api_calls = self_upgrade_fixture
    old_wrapper = wrapper_path.read_bytes()
    result = deploy._self_upgrade(paths, source, sha, args)
    assert result["status"] == "UPDATER_UPGRADED"
    assert module_path.read_bytes() == (source / "installer/local_deploy.py").read_bytes()
    assert wrapper_path.read_bytes() == old_wrapper
    identity = deploy._read_identity(paths)
    assert identity["last_verified_install"] is None
    assert identity["operation"]["kind"] == "self_upgrade"
    assert identity["operation"]["status"] == "COMPLETE"
    assert identity["operation"]["source_sha"] == sha
    assert identity["operation"]["acceptance"]["status"] == "NOT_VERIFIED"
    assert api_calls["main"] == 2


def test_app_identity_and_updater_provenance_survive_both_transition_directions(
        self_upgrade_fixture) -> None:
    deploy, paths, source, sha, _module_path, _wrapper_path, args, _api_calls = self_upgrade_fixture
    install_env = os.environ.copy()
    install_env["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(["sh", str(source / "installer/install.sh"), "--deploy", "--component", "backend",
                             "--component", "ui", "--component", "host", "--target", str(paths.target_root)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False, env=install_env)
    assert result.returncode == 0, result.stderr[-4000:]
    lifecycle, lock, ui = deploy._source_modules(source, paths)
    first_app_operation = str(uuid.uuid4())
    deploy._record_operation(paths, lock, source, sha, lifecycle, ui, first_app_operation,
                             "complete", "VERIFIED_CONTENT")
    first_app_identity = deploy._read_identity(paths)["last_verified_install"]

    deploy._self_upgrade(paths, source, sha, args)
    after_upgrade = deploy._read_identity(paths)
    assert after_upgrade["last_verified_install"] == first_app_identity
    assert after_upgrade["components"]["backend"]["source_sha"] == sha
    assert after_upgrade["components"]["updater"]["provenance"] == "verified_actual_main_ci"
    updater_identity = after_upgrade["components"]["updater"]

    second_app_operation = str(uuid.uuid4())
    deploy._record_operation(paths, lock, source, sha, lifecycle, ui, second_app_operation,
                             "complete", "VERIFIED_CONTENT")
    final_identity = deploy._read_identity(paths)
    assert final_identity["last_verified_install"]["operation_id"] == second_app_operation
    assert final_identity["components"]["backend"]["provenance"] == "local_git"
    assert final_identity["components"]["updater"] == updater_identity


def test_self_upgrade_retries_atomic_replacement_left_pending_after_interruption(
        self_upgrade_fixture, monkeypatch, capsys) -> None:
    deploy, paths, source, sha, module_path, _wrapper_path, args, _api_calls = self_upgrade_fixture
    write_identity = deploy._write_identity
    writes = {"count": 0}

    def fail_completion_once(paths_arg, value):
        writes["count"] += 1
        if writes["count"] == 2:
            raise deploy.DeploymentError("synthetic interruption after atomic replace")
        return write_identity(paths_arg, value)

    monkeypatch.setattr(deploy, "_write_identity", fail_completion_once)
    with pytest.raises(deploy.DeploymentError, match="synthetic interruption"):
        deploy._self_upgrade(paths, source, sha, args)
    assert module_path.read_bytes() == (source / "installer/local_deploy.py").read_bytes()
    assert deploy._read_identity(paths)["operation"]["status"] == "PENDING"
    assert deploy.main(["--check"], paths_override=paths) == 0
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["status"] == "UPDATER_SELF_UPGRADE_PENDING"
    before_rejected_apply = (paths.state_dir / "deployment-identity.json").read_bytes()
    with pytest.raises(deploy.DeploymentError, match="exact pinned transition"):
        deploy.apply(paths, source, sha)
    assert (paths.state_dir / "deployment-identity.json").read_bytes() == before_rejected_apply
    identity_path = paths.state_dir / "deployment-identity.json"
    pending_bytes = identity_path.read_bytes()
    pending_value = json.loads(pending_bytes)
    pending_value["operation"]["old_module_sha256"] = "f" * 64
    identity_path.write_text(json.dumps(pending_value), encoding="utf-8")
    changed_pending_bytes = identity_path.read_bytes()
    with pytest.raises(deploy.DeploymentError, match="unfinished deployment operation"):
        deploy._self_upgrade(paths, source, sha, args)
    assert identity_path.read_bytes() == changed_pending_bytes
    identity_path.write_bytes(pending_bytes)
    monkeypatch.setattr(deploy, "_gh_api", lambda *_args, **_kwargs:
                        (_ for _ in ()).throw(AssertionError("pending exact transition must resume offline")))
    monkeypatch.setattr(deploy, "_write_identity", write_identity)
    recovered = deploy._self_upgrade(paths, source, sha, args)
    assert recovered["status"] == "UPDATER_UPGRADED"
    assert deploy._read_identity(paths)["operation"]["status"] == "COMPLETE"


def test_self_upgrade_interruption_before_rename_keeps_old_module_and_resumes(
        self_upgrade_fixture, monkeypatch) -> None:
    deploy, paths, source, sha, module_path, _wrapper_path, args, _api_calls = self_upgrade_fixture
    old_module = module_path.read_bytes()
    atomic_replace = deploy._atomic_replace_module

    def fail_before_replace(*_args, **_kwargs):
        raise deploy.DeploymentError("synthetic interruption before atomic replace")

    monkeypatch.setattr(deploy, "_atomic_replace_module", fail_before_replace)
    with pytest.raises(deploy.DeploymentError, match="before atomic replace"):
        deploy._self_upgrade(paths, source, sha, args)
    assert module_path.read_bytes() == old_module
    assert deploy._read_identity(paths)["operation"]["status"] == "PENDING"
    monkeypatch.setattr(deploy, "_atomic_replace_module", atomic_replace)
    assert deploy._self_upgrade(paths, source, sha, args)["status"] == "UPDATER_UPGRADED"
    assert module_path.read_bytes() == (source / "installer/local_deploy.py").read_bytes()


def test_self_upgrade_rejects_unqualified_run_and_wrapper_drift(self_upgrade_fixture) -> None:
    deploy, paths, source, sha, module_path, wrapper_path, args, _api_calls = self_upgrade_fixture
    old_module = module_path.read_bytes()
    valid_api = deploy._gh_api
    deploy._gh_api = lambda endpoint, **_kwargs: ({"sha": sha} if endpoint.endswith("commits/main") else
        {"id": 101, "run_attempt": 2, "event": "pull_request", "head_branch": "feature",
         "head_sha": sha, "status": "completed", "conclusion": "success"}
        if endpoint.endswith("/runs/101") else {})
    with pytest.raises(deploy.DeploymentError, match="successful exact-SHA push"):
        deploy._self_upgrade(paths, source, sha, args)
    assert module_path.read_bytes() == old_module
    deploy._gh_api = valid_api
    wrapper_path.write_bytes(b"drifted wrapper\n")
    with pytest.raises(deploy.DeploymentError, match="wrapper"):
        deploy._self_upgrade(paths, source, sha, args)
    assert module_path.read_bytes() == old_module


@pytest.mark.parametrize("member", ["../escape", "/absolute", "C:/drive", "folder\\file"])
def test_evidence_zip_rejects_unsafe_archive_paths(tmp_path: Path, member: str) -> None:
    archive_path = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(member, b"unsafe")
    import importlib.util
    spec = importlib.util.spec_from_file_location("test_zip_extract", MODULE)
    deploy = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = deploy
    spec.loader.exec_module(deploy)
    with pytest.raises(deploy.DeploymentError, match="unsafe or duplicate path"):
        deploy._extract_evidence_zip(archive_path, tmp_path / "extract")


def test_identity_rejects_malformed_component_digest(tmp_path: Path) -> None:
    import importlib.util
    spec = importlib.util.spec_from_file_location("test_identity_validation", MODULE)
    deploy = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = deploy
    spec.loader.exec_module(deploy)
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    paths = deploy.Paths(config=tmp_path / "config", state_dir=state, owner_uid=os.geteuid(),
                         target_root=tmp_path, test_driver=True)
    identity = deploy._identity_record_base(paths)
    identity["components"]["backend"] = {"source_path": "/srv/fwrouter/backend", "source_sha": "a" * 40,
        "provenance": "local_git", "mode": "UNKNOWN", "qualification_status": "NOT_VERIFIED",
        "installed_sha256": "invalid", "operation_id": str(uuid.uuid4())}
    deploy._write_identity(paths, identity)
    with pytest.raises(deploy.DeploymentError, match="backend component digest"):
        deploy._read_identity(paths)


def test_apply_source_config_cas_rejects_change_at_component_lock(
        deployment_fixture, monkeypatch) -> None:
    deploy, paths, _source, old_source, _marker, _env, _active, _calls, owner_driver, system_driver = deployment_fixture
    selected_source = pristine_checkout(old_source.parent / "cas-next")
    (selected_source / "backend/new-source-contract.txt").write_text("selected source\n", encoding="utf-8")
    command(["/usr/bin/git", "-C", str(selected_source), "add", "backend/new-source-contract.txt"])
    command(["/usr/bin/git", "-C", str(selected_source), "-c", "user.name=FWRouter Test",
             "-c", "user.email=fwrouter-test@example.invalid", "commit", "--quiet", "-m", "CAS source"])
    selected_sha = subprocess.check_output(["/usr/bin/git", "-C", str(selected_source),
                                            "rev-parse", "HEAD"], text=True).strip()
    old_sha = subprocess.check_output(["/usr/bin/git", "-C", str(old_source), "rev-parse", "HEAD"], text=True).strip()
    deploy._write_config(paths.config, selected_source, selected_sha, paths.owner_uid)
    original_lock = deploy._component_lock

    def change_source_before_lock(lock_module, target_paths):
        deploy._write_config(paths.config, old_source, old_sha, paths.owner_uid)
        return original_lock(lock_module, target_paths)

    monkeypatch.setattr(deploy, "_component_lock", change_source_before_lock)
    with pytest.raises(deploy.DeploymentError, match="source configuration changed"):
        deploy.apply(paths, selected_source, selected_sha, owner_driver=owner_driver,
                     system_driver=system_driver)
    checkpoint = json.loads((paths.state_dir / "pending.json").read_text(encoding="utf-8"))
    assert checkpoint["status"] == "pending"
    assert not (paths.target("opt/fwrouter-api/new-source-contract.txt")).exists()
    identity = deploy._read_identity(paths)
    assert identity["status"] == "UNKNOWN"


def test_old_source_lookup_and_archive_ignore_hostile_git_environment(
        deployment_fixture, monkeypatch) -> None:
    deploy, paths, _source, old_source, _marker, _env, _active, _calls, _owner, _system = deployment_fixture
    selected_source = pristine_checkout(old_source.parent / "verified-old-source")
    foreign_source = pristine_checkout(old_source.parent / "foreign-replacement-source")
    selected_lifecycle, _lock, _ui = deploy._source_modules(selected_source, paths)
    # Force this fixture's selected HEAD to be installer/docs-only, even if
    # the repository HEAD used by CI happened to change backend files.
    readme_path = selected_source / "installer/README.md"
    readme_path.write_bytes(readme_path.read_bytes() + b"\nD1 hostile Git environment fixture\n")
    command(["/usr/bin/git", "-C", str(selected_source), "add", "installer/README.md"])
    command(["/usr/bin/git", "-C", str(selected_source), "-c", "user.name=FWRouter Test",
             "-c", "user.email=fwrouter-test@example.invalid", "commit", "--quiet",
             "-m", "installer-only fixture head"])
    selected_head = subprocess.check_output(
        ["/usr/bin/git", "-C", str(selected_source), "rev-parse", "HEAD"], text=True).strip()
    selected_commit = deploy._find_source_commit(
        selected_source, paths.target("opt/fwrouter-api"), selected_lifecycle)
    assert selected_commit != selected_head

    helper_path = foreign_source / "installer/controlled_backend_lifecycle.py"
    helper_path.write_bytes(helper_path.read_bytes() + b"\n# foreign replacement helper\n")
    command(["/usr/bin/git", "-C", str(foreign_source), "add", "installer/controlled_backend_lifecycle.py"])
    command(["/usr/bin/git", "-C", str(foreign_source), "-c", "user.name=FWRouter Test",
             "-c", "user.email=fwrouter-test@example.invalid", "commit", "--quiet",
             "-m", "foreign helper replacement target"])
    foreign_commit = subprocess.check_output(
        ["/usr/bin/git", "-C", str(foreign_source), "rev-parse", "HEAD"], text=True).strip()
    command(["/usr/bin/git", "-C", str(foreign_source), "replace", selected_commit, foreign_commit])

    # A replaced selected commit has identical backend entries but foreign
    # lifecycle code. Git subprocesses must still inspect/export selected_source.
    monkeypatch.setenv("GIT_DIR", str(foreign_source / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(foreign_source))
    monkeypatch.delenv("GIT_NO_REPLACE_OBJECTS", raising=False)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.hooksPath")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(foreign_source / "hostile-hooks"))

    # Prove this fixture activates the attack: with inherited Git environment,
    # archiving the selected commit resolves the foreign replacement object.
    unsafe_archive = subprocess.run(
        ["/usr/bin/git", "-C", str(selected_source), "archive", selected_commit,
         "installer/controlled_backend_lifecycle.py"],
        check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
    assert unsafe_archive.returncode == 0, unsafe_archive.stderr.decode("utf-8", errors="replace")
    assert b"foreign replacement helper" in unsafe_archive.stdout

    installed_commit = deploy._find_source_commit(
        selected_source, paths.target("opt/fwrouter-api"), selected_lifecycle)
    assert installed_commit == selected_commit
    with tempfile.TemporaryDirectory(prefix="fwrouter-git-env-test-") as temp:
        exported = deploy._materialize_old_source(selected_source, installed_commit, Path(temp) / "source")
        actual_helper = (exported / "installer/controlled_backend_lifecycle.py").read_bytes()
    selected_helper = deploy._git_blob_bytes(
        selected_source, selected_commit, "installer/controlled_backend_lifecycle.py")
    assert actual_helper == selected_helper
    assert b"foreign replacement helper" not in actual_helper


def test_self_upgrade_requires_the_named_successful_main_check(self_upgrade_fixture) -> None:
    deploy, paths, source, sha, module_path, _wrapper, args, _api_calls = self_upgrade_fixture
    old_module = module_path.read_bytes()
    original_api = deploy._gh_api

    def missing_check(endpoint: str, **kwargs):
        if endpoint.endswith("/runs/101/jobs?per_page=100"):
            return {"total_count": 1, "jobs": [{"name": "unrelated green check",
                                                   "status": "completed", "conclusion": "success"}]}
        return original_api(endpoint, **kwargs)

    deploy._gh_api = missing_check
    with pytest.raises(deploy.DeploymentError, match="incomplete, failed, skipped"):
        deploy._self_upgrade(paths, source, sha, args)
    assert module_path.read_bytes() == old_module
    assert deploy._read_identity(paths)["status"] == "UNKNOWN"


def test_self_upgrade_rejects_skip_worktree_candidate_drift(self_upgrade_fixture) -> None:
    deploy, paths, source, sha, module_path, _wrapper, args, _api_calls = self_upgrade_fixture
    old_module = module_path.read_bytes()
    candidate = source / "installer/local_deploy.py"
    candidate.write_bytes(candidate.read_bytes() + b"\n# skip-worktree hidden edit\n")
    command(["/usr/bin/git", "-C", str(source), "update-index", "--skip-worktree",
             "installer/local_deploy.py"])
    with pytest.raises(deploy.DeploymentError, match="exact Main Git blob"):
        deploy._self_upgrade(paths, source, sha, args)
    assert module_path.read_bytes() == old_module
    assert deploy._read_identity(paths)["status"] == "UNKNOWN"


def test_self_upgrade_rejects_wrong_attempt_expired_artifact_and_bad_verifier_result(
        self_upgrade_fixture, monkeypatch) -> None:
    deploy, paths, source, sha, module_path, _wrapper, args, _api_calls = self_upgrade_fixture
    old_module = module_path.read_bytes()
    original_api = deploy._gh_api

    def wrong_attempt(endpoint: str, **kwargs):
        value = original_api(endpoint, **kwargs)
        if endpoint.endswith("/runs/101"):
            value["run_attempt"] = 1
        return value

    monkeypatch.setattr(deploy, "_gh_api", wrong_attempt)
    with pytest.raises(deploy.DeploymentError, match="successful exact-SHA push"):
        deploy._self_upgrade(paths, source, sha, args)
    assert module_path.read_bytes() == old_module

    def expired_artifact(endpoint: str, **kwargs):
        value = original_api(endpoint, **kwargs)
        if endpoint.endswith("/artifacts/202"):
            value["expired"] = True
        return value

    monkeypatch.setattr(deploy, "_gh_api", expired_artifact)
    with pytest.raises(deploy.DeploymentError, match="artifact metadata"):
        deploy._self_upgrade(paths, source, sha, args)
    assert module_path.read_bytes() == old_module

    monkeypatch.setattr(deploy, "_gh_api", original_api)
    valid_download = deploy._gh_artifact_zip
    monkeypatch.setattr(deploy, "_gh_artifact_zip",
                        lambda _artifact, destination, **_kwargs: destination.write_bytes(b"corrupt zip"))
    with pytest.raises(deploy.DeploymentError, match="API digest"):
        deploy._self_upgrade(paths, source, sha, args)
    assert module_path.read_bytes() == old_module

    monkeypatch.setattr(deploy, "_gh_artifact_zip", valid_download)
    monkeypatch.setattr(deploy, "_run", lambda argv, **_kwargs: subprocess.CompletedProcess(
        argv, 0, json.dumps({"status": "wrong", "source_sha": sha, "run_id": "101",
                             "run_attempt": "2", "strict_aggregate": "passed",
                             "offline_source_checkout": "verified"}), "")
        if "evidence_bundle.py" in " ".join(map(str, argv)) else subprocess.CompletedProcess(argv, 1, "", "unexpected"))
    with pytest.raises(deploy.DeploymentError, match="result does not match"):
        deploy._self_upgrade(paths, source, sha, args)
    assert module_path.read_bytes() == old_module
    assert deploy._read_identity(paths)["status"] == "UNKNOWN"


@pytest.fixture
def deployment_fixture(tmp_path: Path, request):
    target = tmp_path / "host"
    release_root = target / "opt/.fwrouter-ui-releases"

    def cleanup_immutable_releases() -> None:
        # UI publication deliberately makes managed releases immutable. Restore
        # owner access only in this fixture's disposable installer target so
        # the non-root gate can remove its owned temporary tree.
        if not release_root.is_symlink() and release_root.exists():
            for path in sorted(release_root.rglob("*"), reverse=True):
                if path.is_dir() and not path.is_symlink():
                    path.chmod(path.stat().st_mode | stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
            release_root.chmod(release_root.stat().st_mode | stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        cleanup_test_tmp_path(tmp_path)

    request.addfinalizer(cleanup_immutable_releases)
    source = ROOT
    old_source = pristine_checkout(tmp_path)
    (target / "opt").mkdir(parents=True)
    for relative in ("etc/systemd/system", "usr/local/libexec/fwrouter"):
        (target / relative).mkdir(parents=True)
    command(["sh", str(old_source / "installer/install.sh"), "--deploy", "--component", "backend",
             "--target", str(target)])
    backend_env = target / "opt/fwrouter-api/.env"
    backend_env.write_text("FWROUTER_TEST_SETTING=preserve\n", encoding="utf-8")
    backend_env.chmod(0o600)
    marker = target / "var/lib/fwrouter-v2/state/preserve-me.db"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_bytes(b"sqlite-marker\x00with-existing-bytes")

    ui_target = target / "opt/fwrouter-ui"
    shutil.copytree(old_source / "ui", ui_target)
    for path in sorted(ui_target.rglob("*"), reverse=True):
        if path.is_dir():
            path.chmod(0o775)
        else:
            path.chmod(0o664)
    ui_target.chmod(0o775)

    for source_rel, target_rel in (
            ("host/libexec/fwrouter/fwrouter-boot-preflight.sh",
             "usr/local/libexec/fwrouter/fwrouter-boot-preflight.sh"),
            ("host/systemd/fwrouter-api.service", "etc/systemd/system/fwrouter-api.service")):
        destination = target / target_rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"previous source\n")
        destination.chmod(0o755 if source_rel.endswith(".sh") else 0o644)

    import importlib.util
    spec = importlib.util.spec_from_file_location("test_local_deploy", MODULE)
    deploy = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = deploy
    spec.loader.exec_module(deploy)
    paths = deploy.Paths(config=target / "etc/fwrouter/deploy-source.json", target_root=target,
                         state_dir=target / "var/lib/fwrouter-v2/state/controlled-rollout",
                         owner_uid=os.geteuid(), test_driver=True, api_healthy=True,
                         api_active=True, runtime_healthy=True, lifecycle_units_stopped=True)
    active = {"fwrouter-api.service": True, "fwrouter-xray-sub-gateway.service": True,
              "fwrouter-xray.service": True, "fwrouter-mihomo.service": True}
    calls = {"stop": 0}

    def owner_driver(lifecycle, action, state_dir, allow_timer_catchup, owner_source, owner_args):
        # This fixture models the lifecycle owner's private test state. The
        # production _validate_state_dir intentionally requires root; tests
        # instead create the disposable directory under the invoking UID.
        if state_dir.is_symlink():
            raise AssertionError("test lifecycle state directory must not be a symlink")
        state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        state_dir.chmod(0o700)
        selected_lifecycle = lifecycle
        if action == "stop" and owner_source != source:
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                "test_old_lifecycle", owner_source / "installer/controlled_backend_lifecycle.py")
            lifecycle = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = lifecycle
            spec.loader.exec_module(lifecycle)
            lifecycle.SOURCE_BACKEND = owner_source / "backend"
            lifecycle.TARGET_BACKEND = target / "opt/fwrouter-api"
            lifecycle.DEFAULT_STATE_DIR = state_dir
        for owner in {selected_lifecycle, lifecycle}:
            owner._is_active = lambda unit: active.get(unit, False)
            owner._api_health = lambda: True
            owner._wait_gateway_listener = lambda: None
        def systemctl(verb, unit):
            active[unit] = verb == "start"
            if calls.pop("fail_stop_after_checkpoint", False) and verb == "stop":
                raise OSError("synthetic systemctl stop failure after durable owner checkpoint")
        lifecycle._systemctl = systemctl
        if action == "stop":
            calls["stop"] += 1
            result = lifecycle._stop(state_dir)
        elif action == "transfer":
            args = SimpleNamespace(
                expected_operation_id=owner_args[owner_args.index("--expected-operation-id") + 1],
                expected_current_manifest_sha256=owner_args[
                    owner_args.index("--expected-current-manifest-sha256") + 1],
                expected_new_manifest_sha256=owner_args[
                    owner_args.index("--expected-new-manifest-sha256") + 1],
            )
            result = lifecycle._transfer(args, state_dir)
        else:
            result = lifecycle._resume(SimpleNamespace(allow_timer_catchup=allow_timer_catchup), state_dir)
        return subprocess.CompletedProcess([action, *owner_args], 0, json.dumps(result), "")

    def system_driver(argv):
        active["fwrouter-api.service"] = True
        return subprocess.CompletedProcess(argv, 0, "", "")

    return deploy, paths, source, old_source, marker, backend_env, active, calls, owner_driver, system_driver


def test_apply_uses_real_installer_owners_preserves_state_and_retries_same_operation(
        deployment_fixture, monkeypatch) -> None:
    deploy, paths, _source, old_source, marker, backend_env, active, calls, owner_driver, system_driver = deployment_fixture
    reserved_fds_before = {fd: fd_identity(fd) for fd in (8, 9)}
    selected_source = pristine_checkout(old_source.parent / "next")
    (selected_source / "backend/new-source-contract.txt").write_text("selected source\n", encoding="utf-8")
    command(["/usr/bin/git", "-C", str(selected_source), "add", "backend/new-source-contract.txt"])
    command(["/usr/bin/git", "-C", str(selected_source), "-c", "user.name=FWRouter Test",
             "-c", "user.email=fwrouter-test@example.invalid", "commit", "--quiet", "-m", "test source update"])
    commit = subprocess.check_output(["/usr/bin/git", "-C", str(selected_source), "rev-parse", "HEAD"], text=True).strip()
    original_run = deploy._run
    failed = {"once": False}

    def fail_backend_once(argv, **kwargs):
        if (not failed["once"] and Path(argv[0]).name == "install.sh"
                and "--component" in argv and "backend" in argv and "ui" in argv):
            failed["once"] = True
            return subprocess.CompletedProcess(argv, 1, "", "synthetic installer interruption")
        return original_run(argv, **kwargs)

    monkeypatch.setattr(deploy, "_run", fail_backend_once)
    with pytest.raises(deploy.DeploymentError, match="installer failed"):
        deploy.apply(paths, selected_source, commit, owner_driver=owner_driver, system_driver=system_driver)
    failed_identity = deploy._read_identity(paths)
    assert failed_identity["last_verified_install"] is None
    assert failed_identity["operation"]["status"] == "FAILED"
    assert failed_identity["operation"]["lifecycle_status"] == "pending"
    assert failed_identity["operation"]["last_completed_stage"] == "lifecycle_stop_and_source_cas"
    checkpoint = json.loads((paths.state_dir / "pending.json").read_text(encoding="utf-8"))
    operation_id = checkpoint["operation_id"]
    assert checkpoint["status"] == "pending"
    assert active["fwrouter-api.service"] is False
    assert marker.read_bytes() == b"sqlite-marker\x00with-existing-bytes"
    assert backend_env.read_text(encoding="utf-8") == "FWROUTER_TEST_SETTING=preserve\n"
    assert stat.S_IMODE(backend_env.stat().st_mode) == 0o600
    assert calls["stop"] == 1

    monkeypatch.setattr(deploy, "_run", original_run)
    outcome = deploy.apply(paths, selected_source, commit, owner_driver=owner_driver, system_driver=system_driver)
    final_checkpoint = json.loads((paths.state_dir / "pending.json").read_text(encoding="utf-8"))
    assert outcome["status"] == "APPLIED"
    assert final_checkpoint["status"] == "complete"
    identity = deploy._read_identity(paths)
    assert identity["last_verified_install"]["git_sha"] == commit
    assert identity["last_verified_install"]["source_content"] == identity["last_verified_install"]["installed_content"]
    assert identity["last_verified_install"]["native_proof"]["status"] == "NOT_VERIFIED"
    assert identity["last_verified_install"]["acceptance"]["status"] == "NOT_VERIFIED"
    assert final_checkpoint["operation_id"] == operation_id
    assert len(final_checkpoint["source_transitions"]) == 1
    assert calls["stop"] == 1
    assert marker.read_bytes() == b"sqlite-marker\x00with-existing-bytes"
    assert backend_env.read_text(encoding="utf-8") == "FWROUTER_TEST_SETTING=preserve\n"
    assert stat.S_IMODE(backend_env.stat().st_mode) == 0o600
    assert stat.S_IMODE((paths.target("usr/local/libexec/fwrouter/fwrouter-boot-preflight.sh")).stat().st_mode) == 0o755
    assert stat.S_IMODE((paths.target("etc/systemd/system/fwrouter-api.service")).stat().st_mode) == 0o644
    assert not list(paths.state_dir.glob("*.bak"))
    assert (paths.target("opt/fwrouter-ui")).is_symlink()
    assert (paths.target("opt/fwrouter-api/new-source-contract.txt")).read_text(encoding="utf-8") == "selected source\n"
    healthy_paths = deploy.Paths(config=paths.config, target_root=paths.target_root, state_dir=paths.state_dir,
                                 owner_uid=paths.owner_uid, test_driver=True, api_healthy=True,
                                 api_active=True, runtime_healthy=True, lifecycle_units_stopped=True)
    deploy._write_config(paths.config, selected_source, commit, paths.owner_uid)
    assert deploy.main(["--check"], paths_override=healthy_paths) == 0
    identity_before_check = (paths.state_dir / "deployment-identity.json").read_bytes()
    assert deploy.main(["--check"], paths_override=healthy_paths) == 0
    assert (paths.state_dir / "deployment-identity.json").read_bytes() == identity_before_check
    assert {fd: fd_identity(fd) for fd in (8, 9)} == reserved_fds_before
    unhealthy_paths = deploy.Paths(config=paths.config, target_root=paths.target_root, state_dir=paths.state_dir,
                                   owner_uid=paths.owner_uid, test_driver=True, api_healthy=True,
                                   api_active=True, runtime_healthy=False)
    checkpoint_before = (paths.state_dir / "pending.json").read_bytes()
    marker_before = marker.read_bytes()
    assert deploy.main([], paths_override=unhealthy_paths) == 1
    assert (paths.state_dir / "pending.json").read_bytes() == checkpoint_before
    assert marker.read_bytes() == marker_before


def test_apply_leaves_persistent_timers_pending_by_default(deployment_fixture, monkeypatch) -> None:
    deploy, paths, _source, old_source, _marker, _env, active, _calls, owner_driver, system_driver = deployment_fixture
    selected_source = pristine_checkout(old_source.parent / "next")
    (selected_source / "backend/new-source-contract.txt").write_text("selected source\n", encoding="utf-8")
    command(["/usr/bin/git", "-C", str(selected_source), "add", "backend/new-source-contract.txt"])
    command(["/usr/bin/git", "-C", str(selected_source), "-c", "user.name=FWRouter Test",
             "-c", "user.email=fwrouter-test@example.invalid", "commit", "--quiet", "-m", "test source update"])
    active["fwrouter-traffic-collect.timer"] = True
    commit = subprocess.check_output(["/usr/bin/git", "-C", str(selected_source), "rev-parse", "HEAD"], text=True).strip()
    outcome = deploy.apply(paths, selected_source, commit, owner_driver=owner_driver, system_driver=system_driver)
    checkpoint = json.loads((paths.state_dir / "pending.json").read_text(encoding="utf-8"))
    assert outcome["status"] == "APPLIED_WITH_PENDING_TIMERS"
    assert "fwrouter-traffic-collect.timer" in outcome["pending_units"]
    assert active["fwrouter-traffic-collect.timer"] is False
    assert checkpoint["status"] == "pending"
    identity = deploy._read_identity(paths)
    assert identity["operation"]["status"] == "PENDING"
    assert identity["last_verified_install"] is None
    assert identity["operation"]["acceptance"]["status"] == "NOT_VERIFIED"
    installs = {"count": 0}
    real_installer = deploy._run_installer

    def count_installer(*args, **kwargs):
        installs["count"] += 1
        return real_installer(*args, **kwargs)

    monkeypatch.setattr(deploy, "_run_installer", count_installer)
    healthy_paths = deploy.Paths(config=paths.config, target_root=paths.target_root, state_dir=paths.state_dir,
                                 owner_uid=paths.owner_uid, test_driver=True, api_healthy=True,
                                 api_active=True, runtime_healthy=True, lifecycle_units_stopped=True)
    repeated = deploy.apply(healthy_paths, selected_source, commit,
                            owner_driver=owner_driver, system_driver=system_driver)
    assert repeated["status"] == "APPLIED_WITH_PENDING_TIMERS"
    assert installs["count"] == 0


def test_stop_failure_after_owner_checkpoint_publishes_retryable_incomplete_identity(
        deployment_fixture, capsys) -> None:
    deploy, paths, _source, old_source, _marker, _env, _active, calls, owner_driver, system_driver = deployment_fixture
    selected_source = pristine_checkout(old_source.parent / "stop-failure")
    (selected_source / "backend/new-source-contract.txt").write_text("selected source\n", encoding="utf-8")
    command(["/usr/bin/git", "-C", str(selected_source), "add", "backend/new-source-contract.txt"])
    command(["/usr/bin/git", "-C", str(selected_source), "-c", "user.name=FWRouter Test",
             "-c", "user.email=fwrouter-test@example.invalid", "commit", "--quiet", "-m", "test stop failure"])
    commit = subprocess.check_output(["/usr/bin/git", "-C", str(selected_source), "rev-parse", "HEAD"], text=True).strip()

    calls["fail_stop_after_checkpoint"] = True
    with pytest.raises(OSError, match="after durable owner checkpoint"):
        deploy.apply(paths, selected_source, commit, owner_driver=owner_driver,
                     system_driver=system_driver)

    identity_path = paths.state_dir / "deployment-identity.json"
    identity = deploy._read_identity(paths)
    checkpoint_path = paths.state_dir / "pending.json"
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert checkpoint["status"] == "pending"
    checkpoint_before_check = checkpoint_path.read_bytes()
    assert identity["last_verified_install"] is None
    assert identity["operation"]["status"] == "FAILED"
    assert identity["operation"]["lifecycle_status"] == "pending"
    assert identity["operation"]["lifecycle_operation_id"] == checkpoint["operation_id"]
    assert identity["operation"]["last_completed_stage"] == "lifecycle_stop"
    deploy._write_config(paths.config, selected_source, commit, paths.owner_uid)
    identity_before_check = identity_path.read_bytes()
    assert deploy.main(["--check"], paths_override=paths) == 0
    check_result = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert check_result["status"] == "TRANSFER_PENDING_OPERATION"
    assert identity_path.read_bytes() == identity_before_check
    assert checkpoint_path.read_bytes() == checkpoint_before_check

    outcome = deploy.apply(paths, selected_source, commit, owner_driver=owner_driver,
                           system_driver=system_driver)
    assert outcome["status"] == "APPLIED"
    assert json.loads(checkpoint_path.read_text(encoding="utf-8"))["status"] == "complete"


def test_ui_only_stop_failure_retry_finishes_same_owner_stop_before_publication(
        deployment_fixture, monkeypatch) -> None:
    deploy, paths, _source, old_source, _marker, _env, active, calls, owner_driver, system_driver = deployment_fixture
    selected_source = pristine_checkout(old_source.parent / "ui-only-stop-retry")
    ui_file = selected_source / "ui/index.html"
    ui_file.write_bytes(ui_file.read_bytes() + b"\n<!-- selected UI -->\n")
    command(["/usr/bin/git", "-C", str(selected_source), "add", "ui/index.html"])
    command(["/usr/bin/git", "-C", str(selected_source), "-c", "user.name=FWRouter Test",
             "-c", "user.email=fwrouter-test@example.invalid", "commit", "--quiet", "-m", "UI-only update"])
    commit = subprocess.check_output(["/usr/bin/git", "-C", str(selected_source), "rev-parse", "HEAD"],
                                     text=True).strip()
    installed_digest = deploy._source_modules(selected_source, paths)[0]._manifest_digest(
        deploy._source_modules(selected_source, paths)[0]._backend_manifest(paths.target("opt/fwrouter-api")))
    selected_digest = deploy._source_modules(selected_source, paths)[0]._manifest_digest(
        deploy._source_modules(selected_source, paths)[0]._backend_manifest(selected_source / "backend"))
    assert installed_digest == selected_digest

    calls["fail_stop_after_checkpoint"] = True
    with pytest.raises(OSError, match="after durable owner checkpoint"):
        deploy.apply(paths, selected_source, commit, owner_driver=owner_driver,
                     system_driver=system_driver)

    checkpoint_path = paths.state_dir / "pending.json"
    checkpoint_before_retry = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    operation_id = checkpoint_before_retry["operation_id"]
    assert checkpoint_before_retry["status"] == "pending"
    assert checkpoint_before_retry["backend_manifest_sha256"] == installed_digest
    assert active["fwrouter-api.service"] is True
    assert any(not active.get(unit, False) for unit in (
        *deploy._source_modules(selected_source, paths)[0].RESTORE_ORDER,
        *deploy._source_modules(selected_source, paths)[0].PERSISTENT_TIMERS,
        *deploy._source_modules(selected_source, paths)[0].TRANSIENT_JOBS))
    assert calls["stop"] == 1

    installed_at_publication = {"observed": False}
    real_installer = deploy._run_installer

    def assert_quiesced_then_install(*args, **kwargs):
        assert calls["stop"] == 2
        assert all(not active.get(unit, False) for unit in (
            *deploy._source_modules(selected_source, paths)[0].RESTORE_ORDER,
            *deploy._source_modules(selected_source, paths)[0].PERSISTENT_TIMERS,
            *deploy._source_modules(selected_source, paths)[0].TRANSIENT_JOBS,
            deploy._source_modules(selected_source, paths)[0].API_UNIT))
        installed_at_publication["observed"] = True
        return real_installer(*args, **kwargs)

    monkeypatch.setattr(deploy, "_run_installer", assert_quiesced_then_install)
    outcome = deploy.apply(paths, selected_source, commit, owner_driver=owner_driver,
                           system_driver=system_driver)

    final_checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert outcome["status"] == "APPLIED"
    assert installed_at_publication["observed"]
    assert calls["stop"] == 2
    assert final_checkpoint["status"] == "complete"
    assert final_checkpoint["operation_id"] == operation_id
    assert final_checkpoint["backend_manifest_sha256"] == installed_digest


def test_api_start_failure_keeps_the_same_operation_retryable(deployment_fixture) -> None:
    deploy, paths, _source, old_source, _marker, _env, active, _calls, owner_driver, _system_driver = deployment_fixture
    selected_source = pristine_checkout(old_source.parent / "next")
    (selected_source / "backend/new-source-contract.txt").write_text("selected source\n", encoding="utf-8")
    command(["/usr/bin/git", "-C", str(selected_source), "add", "backend/new-source-contract.txt"])
    command(["/usr/bin/git", "-C", str(selected_source), "-c", "user.name=FWRouter Test",
             "-c", "user.email=fwrouter-test@example.invalid", "commit", "--quiet", "-m", "test source update"])
    commit = subprocess.check_output(["/usr/bin/git", "-C", str(selected_source), "rev-parse", "HEAD"], text=True).strip()
    starts = {"count": 0}

    def fail_first_start(argv):
        starts["count"] += 1
        if starts["count"] == 1:
            active["fwrouter-api.service"] = False
            return subprocess.CompletedProcess(argv, 1, "", "synthetic start failure")
        active["fwrouter-api.service"] = True
        return subprocess.CompletedProcess(argv, 0, "", "")

    with pytest.raises(deploy.DeploymentError, match="API unit did not start"):
        deploy.apply(paths, selected_source, commit, owner_driver=owner_driver, system_driver=fail_first_start)
    checkpoint_path = paths.state_dir / "pending.json"
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    operation_id = checkpoint["operation_id"]
    assert checkpoint["status"] == "pending"
    assert active["fwrouter-api.service"] is False

    retry_paths = deploy.Paths(config=paths.config, target_root=paths.target_root, state_dir=paths.state_dir,
                               owner_uid=paths.owner_uid, test_driver=True, api_healthy=True,
                               api_active=False, runtime_healthy=True, lifecycle_units_stopped=True)
    outcome = deploy.apply(retry_paths, selected_source, commit,
                           owner_driver=owner_driver, system_driver=fail_first_start)
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert outcome["status"] == "APPLIED"
    assert checkpoint["status"] == "complete"
    assert checkpoint["operation_id"] == operation_id


@pytest.mark.parametrize("race_point", ["after_transfer", "before_lock"])
def test_backend_change_in_stop_to_lock_gap_fails_before_publication(
        deployment_fixture, monkeypatch, race_point: str) -> None:
    deploy, paths, _source, old_source, _marker, _env, active, _calls, owner_driver, system_driver = deployment_fixture
    selected_source = pristine_checkout(old_source.parent / "next")
    (selected_source / "backend/new-source-contract.txt").write_text("selected source\n", encoding="utf-8")
    command(["/usr/bin/git", "-C", str(selected_source), "add", "backend/new-source-contract.txt"])
    command(["/usr/bin/git", "-C", str(selected_source), "-c", "user.name=FWRouter Test",
             "-c", "user.email=fwrouter-test@example.invalid", "commit", "--quiet", "-m", "test source update"])
    commit = subprocess.check_output(["/usr/bin/git", "-C", str(selected_source), "rev-parse", "HEAD"], text=True).strip()
    host_pair = paths.target("usr/local/libexec/fwrouter/fwrouter-boot-preflight.sh")
    host_before = host_pair.read_bytes()
    installer_calls = {"count": 0}
    real_install = deploy._run_installer
    real_lock = deploy._component_lock

    def count_install(*args, **kwargs):
        installer_calls["count"] += 1
        return real_install(*args, **kwargs)

    def mutate_backend(target_paths):
        lifecycle, _lock, _ui = deploy._source_modules(selected_source, target_paths)
        entries = lifecycle._backend_manifest(target_paths.target("opt/fwrouter-api"))
        changed = target_paths.target("opt/fwrouter-api") / entries[0]["path"]
        changed.write_bytes(changed.read_bytes() + b"\nintervening writer\n")

    def mutate_then_lock(lock_module, target_paths):
        mutate_backend(target_paths)
        return real_lock(lock_module, target_paths)

    real_owner_driver = owner_driver

    def mutate_after_transfer(*args):
        result = real_owner_driver(*args)
        if args[1] == "transfer":
            mutate_backend(paths)
        return result

    monkeypatch.setattr(deploy, "_run_installer", count_install)
    if race_point == "after_transfer":
        owner_driver = mutate_after_transfer
    else:
        monkeypatch.setattr(deploy, "_component_lock", mutate_then_lock)
    with pytest.raises(deploy.DeploymentError, match="installed backend changed before publication"):
        deploy.apply(paths, selected_source, commit, owner_driver=owner_driver, system_driver=system_driver)
    assert installer_calls["count"] == 0
    assert host_pair.read_bytes() == host_before
    checkpoint = json.loads((paths.state_dir / "pending.json").read_text(encoding="utf-8"))
    assert checkpoint["status"] == "pending"
    assert checkpoint["backend_manifest_sha256"] == deploy._source_modules(selected_source, paths)[0]._manifest_digest(
        deploy._source_modules(selected_source, paths)[0]._backend_manifest(selected_source / "backend"))
    assert active["fwrouter-api.service"] is False


def test_schema2_checkpoint_transfers_through_real_owner_compatibility(
        deployment_fixture) -> None:
    deploy, paths, _source, old_source, _marker, _env, _active, _calls, owner_driver, system_driver = deployment_fixture
    selected_source = pristine_checkout(old_source.parent / "legacy-next")
    (selected_source / "backend/new-source-contract.txt").write_text("selected source\n", encoding="utf-8")
    command(["/usr/bin/git", "-C", str(selected_source), "add", "backend/new-source-contract.txt"])
    command(["/usr/bin/git", "-C", str(selected_source), "-c", "user.name=FWRouter Test",
             "-c", "user.email=fwrouter-test@example.invalid", "commit", "--quiet", "-m", "test source update"])
    commit = subprocess.check_output(["/usr/bin/git", "-C", str(selected_source), "rev-parse", "HEAD"], text=True).strip()
    real_owner_driver = owner_driver

    def legacy_owner_driver(lifecycle, action, state_dir, allow_timer_catchup, owner_source, owner_args):
        result = real_owner_driver(lifecycle, action, state_dir, allow_timer_catchup, owner_source, owner_args)
        if action == "stop":
            checkpoint_path = state_dir / "pending.json"
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            checkpoint["schema"] = 2
            checkpoint.pop("maintenance_operation", None)
            checkpoint.pop("runtime_quiescence", None)
            checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")
        return result

    outcome = deploy.apply(paths, selected_source, commit,
                           owner_driver=legacy_owner_driver, system_driver=system_driver)
    checkpoint = json.loads((paths.state_dir / "pending.json").read_text(encoding="utf-8"))
    assert outcome["status"] == "APPLIED"
    assert checkpoint["schema"] == 4
    assert checkpoint["status"] == "complete"
    assert checkpoint["source_transitions"][0]["to_manifest_sha256"] == checkpoint["backend_manifest_sha256"]


@pytest.mark.parametrize(
    ("xray_result", "expected_status"),
    [
        (subprocess.CompletedProcess([], 0, "active\n", ""), "NOOP"),
        (subprocess.CompletedProcess([], 3, "inactive\n", ""), "UNHEALTHY_CURRENT_SOURCE"),
        (subprocess.CompletedProcess([], 3, "failed\n", ""), "UNHEALTHY_CURRENT_SOURCE"),
        (subprocess.CompletedProcess([], 0, "unexpected\n", ""), "UNHEALTHY_CURRENT_SOURCE"),
        (subprocess.CompletedProcess([], 1, "active\n", "error"), "UNHEALTHY_CURRENT_SOURCE"),
        (OSError("systemctl unavailable"), "UNHEALTHY_CURRENT_SOURCE"),
        (subprocess.TimeoutExpired(["/usr/bin/systemctl"], 10), "UNHEALTHY_CURRENT_SOURCE"),
    ],
    ids=("active", "inactive", "failed", "malformed", "nonzero", "command-error", "timeout"),
)
def test_production_check_observes_xray_read_only_and_fails_closed(
        deployment_fixture, monkeypatch, xray_result, expected_status) -> None:
    deploy, paths, source, _old_source, _marker, _backend_env, _active, _calls, _owner_driver, _system_driver = deployment_fixture
    from dataclasses import replace

    paths = replace(paths, test_driver=False)
    # Match the disposable host pair so this exercises the healthy NOOP decision.
    ui_target = paths.target("opt/fwrouter-ui")
    shutil.rmtree(ui_target)
    shutil.copytree(source / "ui", ui_target)
    for source_rel, target_rel, mode in deploy.HOST_ROWS:
        target = paths.target(target_rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / source_rel, target)
        target.chmod(mode)

    lifecycle, lock, ui = deploy._source_modules(source, paths)
    lifecycle._api_health = lambda: True
    lifecycle._gateway_listener_ready = lambda: True
    invocations: list[list[str]] = []

    def controlled_subprocess(argv, **kwargs):
        invocations.append(list(argv))
        assert kwargs.get("timeout") == 10
        assert kwargs.get("check") is False
        assert kwargs.get("text") is True
        assert "--" not in argv
        if argv == ["/usr/bin/systemctl", "is-active", "fwrouter-xray.service"]:
            if isinstance(xray_result, BaseException):
                raise xray_result
            return subprocess.CompletedProcess(argv, xray_result.returncode,
                                                xray_result.stdout, xray_result.stderr)
        if argv in (["/usr/bin/systemctl", "is-active", lifecycle.GATEWAY_UNIT],
                    ["/usr/bin/systemctl", "is-active", lifecycle.MIHOMO_UNIT]):
            return subprocess.CompletedProcess(argv, 0, "active\n", "")
        pytest.fail(f"unexpected systemctl invocation: {argv!r}")

    monkeypatch.setattr(lifecycle.subprocess, "run", controlled_subprocess)
    monkeypatch.setattr(deploy, "_source_modules", lambda _source, _paths: (lifecycle, lock, ui))

    # Create a valid completed checkpoint: it must not prevent a healthy NOOP.
    manifest = lifecycle._backend_manifest(source / "backend")
    paths.state_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    checkpoint = {
        "schema": lifecycle.SCHEMA,
        "operation_id": str(uuid.uuid4()),
        "status": "complete",
        "created_at": "2026-10-30T00:00:00Z",
        "backend_manifest": manifest,
        "backend_manifest_sha256": lifecycle._manifest_digest(manifest),
        "active_units": {unit: False for unit in {*lifecycle.RESTORE_ORDER,
                                                    *lifecycle.TRANSIENT_JOBS,
                                                    lifecycle.API_UNIT}},
        "pending_units": [],
        "initial_backend_manifest_sha256": lifecycle._manifest_digest(manifest),
        "source_transitions": [],
        "maintenance_operation": None,
        "runtime_quiescence": None,
    }
    checkpoint_path = paths.state_dir / "pending.json"
    checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")
    checkpoint_path.chmod(0o600)

    observed = deploy._facts(source, paths, lifecycle, ui)
    assert observed[:3] == (True, True, True), observed
    invocations.clear()
    status = deploy.check(paths, source, "unused-by-check")

    assert status == expected_status
    assert invocations == [
        ["/usr/bin/systemctl", "is-active", lifecycle.GATEWAY_UNIT],
        ["/usr/bin/systemctl", "is-active", lifecycle.MIHOMO_UNIT],
        ["/usr/bin/systemctl", "is-active", "fwrouter-xray.service"],
    ]
    assert all(argv[:2] == ["/usr/bin/systemctl", "is-active"] for argv in invocations)
