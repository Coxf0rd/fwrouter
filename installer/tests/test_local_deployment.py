"""Public deployment command regressions using disposable installer targets."""
from __future__ import annotations

import json
from contextlib import contextmanager
import hashlib
import fcntl
import os
from pathlib import Path
import select
import shutil
import stat
import subprocess
import sys
import tempfile
import time
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
        "raise SystemExit(local_deploy.main(['--check', '--json'], paths_override=p))"
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


def test_dev_captures_dirty_untracked_content_without_runtime_or_remote_calls(
        deployment_fixture, monkeypatch) -> None:
    deploy, paths, _, source, *_ = deployment_fixture
    tracked = source / "ui/index.html"
    tracked.write_text("Development content\n")
    untracked = source / "ui/staged-development.js"
    untracked.write_text("console.log('first')\n")
    index = source / ".git/index"
    index_before = index.read_bytes()
    monkeypatch.setattr(deploy, "_gh_api", lambda *args: pytest.fail("Development queried GitHub"))
    monkeypatch.setattr(deploy, "_read_identity", lambda *args: pytest.fail("Development read Stable identity"))
    first = deploy._development_source(source)
    assert first["dirty"] is True
    assert first["provenance"] == "local_development_worktree"
    assert not paths.state_dir.exists()
    untracked.write_text("console.log('second')\n")
    second = deploy._development_source(source)
    assert first["content_sha256"] != second["content_sha256"]
    assert first["git_sha"] == second["git_sha"]
    assert index.read_bytes() == index_before
    assert tracked.read_text() == "Development content\n"


def test_dev_rejects_change_between_fenced_content_passes(deployment_fixture, monkeypatch) -> None:
    deploy, _paths, _, source, *_ = deployment_fixture
    extra = source / "ui/volatile.js"
    extra.write_text("before")
    collect = deploy._dev_content

    def mutate_after_capture(*args):
        result = collect(*args)
        extra.write_text("after")
        return result

    monkeypatch.setattr(deploy, "_dev_content", mutate_after_capture)
    with pytest.raises(deploy.DeploymentError, match="changed during capture"):
        deploy._development_source(source)


def test_dev_capture_does_not_admit_a_snapshot_after_source_mutation(deployment_fixture, monkeypatch) -> None:
    deploy, paths, _, source, *_ = deployment_fixture
    paths.state_dir.mkdir(mode=0o700, parents=True)
    extra = source / "ui/capture-race.js"
    extra.write_text("original\n")
    collect = deploy._dev_content

    def read_after_mutation(*args, **kwargs):
        extra.write_text("changed during capture\n")
        return collect(*args, **kwargs)

    monkeypatch.setattr(deploy, "_dev_content", read_after_mutation)
    with pytest.raises(deploy.DeploymentError, match="changed during snapshot"):
        deploy._dev_snapshot_capture(source, paths)
    assert not list((paths.state_dir / "dev-snapshots").glob("*.tar*"))
    assert not (paths.state_dir / "deployment-identity.json").exists()


def test_dev_snapshot_rejects_insufficient_space_before_durable_capture(deployment_fixture, monkeypatch) -> None:
    deploy, paths, _, source, *_ = deployment_fixture
    paths.state_dir.mkdir(mode=0o700, parents=True)
    monkeypatch.setattr(deploy.shutil, "disk_usage", lambda _path: SimpleNamespace(free=0))

    with pytest.raises(deploy.DeploymentIncomplete, match="Temporary storage is insufficient"):
        deploy._dev_snapshot_capture(source, paths)
    assert not (paths.state_dir / "dev-snapshots").exists()
    assert not (paths.state_dir / "deployment-identity.json").exists()


def _qualification(sha):
    return {"source_sha": sha, "run_id": 101, "run_attempt": 2, "artifact_id": 303,
            "artifact_digest": "sha256:" + "a" * 64}


@pytest.mark.parametrize("scenario", ["valid", "missing_run", "failed_run", "pending_run", "stale_artifact",
                                     "incomplete_jobs", "forged_metadata", "invalid_bundle"])
def test_automatic_stable_qualification_rejects_unqualified_evidence(
        deployment_fixture, monkeypatch, tmp_path, scenario) -> None:
    deploy, paths, _, source, *_ = deployment_fixture
    sha = deploy._source_commit(source, paths.owner_uid)
    expected = _qualification(sha)
    run = {"id": 101, "run_attempt": 2, "event": "push", "head_branch": "main", "head_sha": sha,
           "status": "completed", "conclusion": "success", "path": deploy.MAIN_WORKFLOW,
           "repository": {"full_name": "Coxf0rd/fwrouter"}}
    if scenario == "failed_run":
        run["conclusion"] = "failure"
    if scenario == "pending_run":
        run.update(status="in_progress", conclusion=None)
    artifact = {"id": 303, "expired": scenario == "stale_artifact", "digest": expected["artifact_digest"],
                "name": f"fwrouter-main-evidence-{sha}-101-2", "workflow_run":
                    {"id": 101, "head_sha": "b" * 40 if scenario == "forged_metadata" else sha, "head_branch": "main"}}
    calls = []

    def provider(endpoint):
        calls.append(endpoint)
        if "/workflows/" in endpoint:
            rows = [] if scenario == "missing_run" else [run]
            return {"total_count": len(rows), "workflow_runs": rows}
        if endpoint.endswith("/101"):
            return run
        if "/attempts/2/jobs" in endpoint:
            return {"total_count": 2 if scenario == "incomplete_jobs" else 1,
                    "jobs": [{"name": "Affected L0-L5 with hosted profiles",
                              "status": "completed", "conclusion": "success"}]}
        if "/101/artifacts" in endpoint:
            return {"total_count": 1, "artifacts": [artifact]}
        if endpoint.endswith("/artifacts/303"):
            return artifact
        if endpoint.endswith("/commits/main"):
            return {"sha": sha}
        pytest.fail(f"unexpected GitHub read {endpoint}")

    verified = []

    def verify_bundle(*args):
        verified.append(args[-1])
        if scenario == "invalid_bundle":
            raise deploy.DeploymentError("offline bundle failed")

    monkeypatch.setattr(deploy, "_gh_api", provider)
    monkeypatch.setattr(deploy, "_verify_main_bundle", verify_bundle)
    if scenario == "valid":
        assert deploy._stable_qualification(source, sha, tmp_path, paths.owner_uid) == expected
        assert verified == [expected]
        assert len([call for call in calls if "/attempts/2/jobs" in call]) == 2
    else:
        with pytest.raises(deploy.DeploymentError):
            deploy._stable_qualification(source, sha, tmp_path, paths.owner_uid)


def test_stable_eligibility_report_keeps_noop_and_health_separate(deployment_fixture, monkeypatch, capsys) -> None:
    deploy, paths, _, old_source, _marker, _env, _active, calls, owner_driver, system_driver = deployment_fixture
    source = pristine_checkout(old_source.parent / "selected")
    sha = deploy._source_commit(source, paths.owner_uid)
    evidence = _qualification(sha)
    monkeypatch.setattr(deploy, "_current_main", lambda: sha)

    @contextmanager
    def fixture_source(_paths, recovery):
        assert recovery is None
        yield deploy.StableSource(source, evidence)

    monkeypatch.setattr(deploy, "_temporary_main", fixture_source)
    args = ["--stable", "--json"]
    assert deploy.main(args, paths_override=paths, owner_driver=owner_driver, system_driver=system_driver) == 3
    first = json.loads(capsys.readouterr().out)
    assert first["status"] == "INCOMPLETE"
    assert first["stages"]["CI"]["status"] == "PASS"
    assert first["installed_content"] == "VERIFIED"
    assert first["native_proof"]["status"] == "NOT_VERIFIED"
    assert first["installed_version"] == sha
    assert first["deployment_identity"]["last_verified_install"]["path"] == deploy.MAIN_ORIGIN
    assert str(source) not in json.dumps(first["deployment_identity"])
    assert "stable_admission" not in first["deployment_identity"]
    before = calls.copy()
    assert deploy.main(args, paths_override=paths, owner_driver=owner_driver, system_driver=system_driver) == 3
    second = json.loads(capsys.readouterr().out)
    assert second["content_noop"] is True and second["installed_content"] == "VERIFIED"
    assert calls == before
    unhealthy = deploy.replace(paths, runtime_healthy=False)
    assert deploy.main(args, paths_override=unhealthy) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "FAILED"
    assert calls == before


def test_stable_staging_recovers_exact_source_and_cleans_up_after_interrupt(
        deployment_fixture, monkeypatch) -> None:
    deploy, paths, _, origin, *_ = deployment_fixture
    sha = deploy._source_commit(origin, paths.owner_uid)
    evidence = _qualification(sha)
    command(["/usr/bin/git", "-C", str(origin), "update-ref", "refs/heads/main", sha])
    monkeypatch.setattr(deploy, "MAIN_ORIGIN", str(origin))
    monkeypatch.setattr(deploy, "_current_main", lambda: sha)
    monkeypatch.setattr(deploy, "_stable_qualification", lambda *_: evidence)
    with pytest.raises(KeyboardInterrupt):
        with deploy._temporary_main(paths, None) as selection:
            stage = selection.root.parent
            assert stage.parent == Path("/tmp")
            assert selection.commit == sha
            assert stage.exists()
            raise KeyboardInterrupt
    assert not stage.exists()
    monkeypatch.setattr(deploy, "_current_main", lambda: pytest.fail("recovery selected a different Main"))
    monkeypatch.setattr(deploy, "_stable_qualification", lambda *_: pytest.fail("durable qualification was ignored"))
    with deploy._temporary_main(paths, evidence) as recovered:
        stage = recovered.root.parent
        assert deploy._source_commit(recovered.root, paths.owner_uid) == sha
    assert not stage.exists()


def test_stable_completes_previously_recorded_install_after_owner_success(
        deployment_fixture, monkeypatch, capsys) -> None:
    deploy, paths, _, old_source, _marker, _env, _active, calls, owner_driver, system_driver = deployment_fixture
    source = pristine_checkout(old_source.parent / "selected")
    sha = deploy._source_commit(source, paths.owner_uid)
    evidence = _qualification(sha)
    monkeypatch.setattr(deploy, "_current_main", lambda: sha)

    @contextmanager
    def fixture_source(_paths, recovery):
        assert recovery in (None, evidence)
        yield deploy.StableSource(source, evidence)

    monkeypatch.setattr(deploy, "_temporary_main", fixture_source)
    original = deploy._write_identity
    interrupted = False

    def crash_after_lifecycle(paths_arg, record):
        nonlocal interrupted
        if not interrupted and (record["operation"] or {}).get("status") == "VERIFIED_CONTENT":
            interrupted = True
            raise KeyboardInterrupt
        return original(paths_arg, record)

    monkeypatch.setattr(deploy, "_write_identity", crash_after_lifecycle)
    args = ["--stable", "--json"]
    assert deploy.main(args, paths_override=paths, owner_driver=owner_driver, system_driver=system_driver) == 130
    assert json.loads(capsys.readouterr().out)["status"] == "INCOMPLETE"
    assert deploy._read_identity(paths)["stable_admission"] == evidence
    before = calls.copy()
    assert deploy.main(args, paths_override=paths, owner_driver=owner_driver, system_driver=system_driver) == 3
    result = json.loads(capsys.readouterr().out)
    assert result["installed_content"] == "VERIFIED"
    assert result["installed_version"] == sha
    assert "stable_admission" not in result["deployment_identity"]
    assert calls == before


def test_human_and_verbose_check_avoid_dumping_identity(deployment_fixture, monkeypatch, capsys) -> None:
    deploy, paths, *_ = deployment_fixture
    monkeypatch.setattr(deploy, "_gh_api", lambda *args: pytest.fail("--check queried GitHub"))
    assert deploy.main(["--stable", "--check", "--verbose"], paths_override=paths) == 3
    rendered = capsys.readouterr().out
    assert "FWRouter Deployment — STABLE" in rendered
    assert "installed version is UNKNOWN" in rendered
    assert "deployment_identity" not in rendered
    assert not paths.state_dir.exists()


def test_stable_check_inspects_configured_health_without_remote_actions(
        deployment_fixture, monkeypatch, capsys) -> None:
    deploy, paths, _, source, *_ = deployment_fixture
    sha = deploy._source_commit(source, paths.owner_uid)
    deploy._write_config(paths.config, source, sha, paths.owner_uid)
    monkeypatch.setattr(deploy, "_gh_api", lambda *_: pytest.fail("--check queried GitHub"))
    paths = deploy.replace(paths, runtime_healthy=False)
    assert deploy.main(["--stable", "--check", "--json"], paths_override=paths) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["health"]["API"]["status"] == "PASS"
    assert result["health"]["Runtime"]["status"] == "FAIL"
    assert not paths.state_dir.exists()


def test_stable_host_environment_cannot_inherit_database_overrides(deployment_fixture, monkeypatch) -> None:
    deploy, *_ = deployment_fixture
    for name in ("FWROUTER_STATE_DIR", "STATE_DIR", "SYSTEMD_BUS_ADDRESS"):
        monkeypatch.setenv(name, "/tmp/fixture-override")
    command = [sys.executable, "-I", "-B", "-c",
               "import os; print({key: os.environ.get(key) for key in "
               "('FWROUTER_STATE_DIR', 'STATE_DIR', 'SYSTEMD_BUS_ADDRESS')})"]
    result = deploy._run(command, host_environment=True)
    assert result.returncode == 0
    assert result.stdout == "{'FWROUTER_STATE_DIR': None, 'STATE_DIR': None, 'SYSTEMD_BUS_ADDRESS': None}\n"


def test_stable_service_state_refuses_an_environment_file_override(deployment_fixture, monkeypatch) -> None:
    deploy, paths, _, _, _marker, env, *_ = deployment_fixture
    owner = SimpleNamespace(API_UNIT="fwrouter-api.service")
    properties = (f"EnvironmentFiles={env} (ignore_errors=yes)\n"
                  "LoadState=loaded\nEnvironment=\nDropInPaths=\nPassEnvironment=\nMainPID=0\n")

    def query(argv, **kwargs):
        assert kwargs["host_environment"]
        assert argv[0:2] == ["/usr/bin/systemctl", "show"]
        return subprocess.CompletedProcess(argv, 0, properties, "")

    monkeypatch.setattr(deploy, "_run", query)
    deploy._stable_service_state(paths, owner)
    baseline = properties
    properties = "EnvironmentFiles=/tmp/unknown.env (ignore_errors=no)\n" + baseline
    with pytest.raises(deploy.DeploymentError, match="outside the supported Stable state-root contract"):
        deploy._stable_service_state(paths, owner)
    properties = baseline
    env.write_text("STATE_DIR=/tmp/alternate-db\n")
    with pytest.raises(deploy.DeploymentError, match="environment file overrides"):
        deploy._stable_service_state(paths, owner)


def test_stable_preflight_requires_both_exact_schema_checks(deployment_fixture, monkeypatch) -> None:
    deploy, paths, _, source, *_ = deployment_fixture
    paths = deploy.replace(paths, test_driver=False)
    monkeypatch.setattr(deploy, "_stable_service_state", lambda *_: None)
    owner = SimpleNamespace(_verified_backend_interpreter=lambda: Path(sys.executable))
    calls = []

    def run(argv, **kwargs):
        assert kwargs["host_environment"]
        assert argv[-4:] == ["database", "upgrade", "--expected-schema", "27"]
        calls.append(argv)
        if "-m" in argv:
            return subprocess.CompletedProcess(argv, 0, json.dumps({
                "ok": True, "status": "already_current", "current_schema": 27,
                "target_schema": 27, "database_changed": False}), "")
        return subprocess.CompletedProcess(argv, 1, '{"error_code":"DATABASE_SCHEMA_PREFLIGHT_FAILED"}', "")

    monkeypatch.setattr(deploy, "_run", run)
    with pytest.raises(deploy.DeploymentError, match=r"\(selected\) DATABASE_SCHEMA_PREFLIGHT_FAILED"):
        deploy._strict_database_preflight(paths, owner, source)
    assert len(calls) == 2


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


def test_self_upgrade_requires_actual_main_pins_and_replaces_only_module(self_upgrade_fixture, capsys) -> None:
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
    config_before_check = paths.config.read_bytes()
    identity_path = paths.state_dir / "deployment-identity.json"
    identity_before_check = identity_path.read_bytes()
    healthy_paths = deploy.replace(paths, api_healthy=True, runtime_healthy=True)
    assert deploy.main(["--check", "--json"], paths_override=healthy_paths) == 3
    check = json.loads(capsys.readouterr().out)
    assert check["stages"]["Updater"]["status"] == "PASS"
    assert check["stages"]["Identity"]["status"] == "INCOMPLETE"
    assert paths.config.read_bytes() == config_before_check
    assert identity_path.read_bytes() == identity_before_check


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
    assert deploy.main(["--check", "--json"], paths_override=paths) == 0
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
    assert deploy.main(["--check", "--json"], paths_override=paths) == 0
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


def test_staging_cleanup_requires_expired_owner_marker_and_unlocked_lease(
        deployment_fixture, monkeypatch, tmp_path) -> None:
    deploy, paths, *_ = deployment_fixture
    root = tmp_path / "isolated-tmp"
    root.mkdir()
    monkeypatch.setattr(deploy, "STAGING_ROOT", root)
    age = deploy.time.time() - deploy.STAGING_MAX_AGE - 60
    stale = root / "fwrouter-main-12345678"
    stale.mkdir(mode=0o700)
    lease = stale / deploy.STAGING_LEASE
    lease.write_bytes(deploy.STAGING_SIGNATURE)
    lease.chmod(0o600)
    os.utime(lease, (age, age))
    (stale / "abandoned-artifact").write_bytes(b"disposable")
    unknown = root / "fwrouter-main-abcdefgh"
    unknown.mkdir(mode=0o700)
    (unknown / "operator-data").write_text("preserve")

    with deploy._managed_staging_workspace(paths) as active:
        active_lease = active / deploy.STAGING_LEASE
        os.utime(active_lease, (age, age))
        result = deploy._cleanup_expired_staging(paths)
        assert result["failures"] == []
        assert result["reclaimed_bytes"] > 0
        assert active.is_dir()
        assert not stale.exists()
        assert (unknown / "operator-data").read_text() == "preserve"
    assert not active.exists()
    assert deploy._cleanup_expired_staging(paths) == {"reclaimed_bytes": 0, "failures": []}


def test_verified_stable_generations_rotate_and_rollback_reconstructs_the_previous_source(
        deployment_fixture, monkeypatch, capsys) -> None:
    deploy, paths, _, old_source, _marker, _env, active, calls, owner_driver, system_driver = deployment_fixture
    origin = pristine_checkout(old_source.parent / "rollback-origin")
    monkeypatch.setattr(deploy, "MAIN_ORIGIN", str(origin))
    monkeypatch.setattr(deploy, "_current_main",
                        lambda: command(["/usr/bin/git", "-C", str(origin), "rev-parse", "refs/heads/main"]).stdout.strip())
    monkeypatch.setattr(deploy, "_stable_qualification", lambda _source, sha, *_: _qualification(sha))
    monkeypatch.setattr(deploy, "_native_acceptance", lambda *_: {
        "status": "PASS", "reason": None, "scope": "ui", "proof_sha256": "e" * 64})
    def invoke(*argv):
        code = deploy.main([*argv, "--json"], paths_override=paths, owner_driver=owner_driver,
                           system_driver=system_driver)
        return code, json.loads(capsys.readouterr().out)

    shas = []
    for version in ("stable-a", "stable-b", "stable-c"):
        (origin / "ui/index.html").write_text(version)
        command(["/usr/bin/git", "-C", str(origin), "add", "ui/index.html"])
        if version == "stable-c":
            marker = origin / "backend/fwrouter_api/__init__.py"
            with marker.open("a", encoding="utf-8") as stream:
                stream.write("\n# newer backend content\n")
            command(["/usr/bin/git", "-C", str(origin), "add", "backend/fwrouter_api/__init__.py"])
        command(["/usr/bin/git", "-C", str(origin), "-c", "user.name=FWRouter Test",
                 "-c", "user.email=fwrouter-test@example.invalid", "commit", "--quiet", "-m", version])
        sha = command(["/usr/bin/git", "-C", str(origin), "rev-parse", "HEAD"]).stdout.strip()
        command(["/usr/bin/git", "-C", str(origin), "update-ref", "refs/heads/main", sha])
        shas.append(sha)
        code, result = invoke("--stable")
        assert code == 0, result["stages"]
        assert result["status"] == "SUCCESS"
        assert result["storage"]["stable_current"] == sha
        assert result["backup"]["status"] == ("SKIP" if version == "stable-c" else "NOT_CREATED")

    accepted = deploy._read_identity(paths)
    assert accepted["stable_current"]["source_sha"] == shas[2]
    assert accepted["stable_previous"]["source_sha"] == shas[1]
    assert shas[0] not in json.dumps({key: accepted[key] for key in ("stable_current", "stable_previous")})
    before = calls.copy()
    code, result = invoke("--stable")
    assert code == 0 and result["content_noop"]
    assert calls == before

    corrupt = json.loads(json.dumps(accepted))
    corrupt["stable_previous"]["installed_content"]["ui_sha256"] = "f" * 64
    deploy._write_identity(paths, corrupt)
    code, result = invoke("--rollback")
    assert code == 3
    assert calls == before
    assert (paths.target("opt/fwrouter-ui/index.html")).read_text() == "stable-c"
    deploy._write_identity(paths, accepted)

    code, result = invoke("--rollback")
    assert code == 0, result["stages"]
    assert result["storage"]["stable_current"] == shas[1]
    assert result["storage"]["stable_previous"] == shas[2]
    assert result["backup"]["status"] == "SKIP"
    assert (paths.target("opt/fwrouter-ui/index.html")).read_text() == "stable-b"
    assert "# newer backend content" not in paths.target("opt/fwrouter-api/fwrouter_api/__init__.py").read_text()
    assert active["fwrouter-xray.service"] and active["fwrouter-mihomo.service"]


def test_retired_backup_is_preserved_while_configured_recovery_references_it(
        deployment_fixture, monkeypatch) -> None:
    deploy, paths, *_ = deployment_fixture
    paths.state_dir.mkdir(mode=0o700, parents=True)
    record = deploy._identity_record_base(paths)
    pin = {"id": "a" * 32, "source_sha": "b" * 40, "backend_sha256": "c" * 64, "schema": 27,
           "filename": "rollback-" + "d" * 32 + ".bak", "sha256": "e" * 64, "snapshot_sha256": "f" * 64}
    record["database_rollback_retired"] = pin
    record["database_rollback"] = {**pin, "id": "1" * 32}
    deploy._write_identity(paths, record)
    retired_path = deploy._backup_directory(paths, pin) / pin["filename"]
    monkeypatch.setattr(deploy, "_read_acceptance_policy", lambda _: {
        "recovery": {"backup_path": str(retired_path)}})
    monkeypatch.setattr(deploy, "_remove_obsolete_managed_backup", lambda *_: pytest.fail("removed recovery backup"))
    result = {}
    deploy._cleanup_retired_backup(paths, SimpleNamespace(), result)
    assert "configured recovery backup pin" in result["storage"]["retained"][0]["reason"]
    assert deploy._read_identity(paths)["database_rollback_retired"] == pin


def test_rollback_source_without_cleanup_helper_preserves_pinned_ui_releases(deployment_fixture) -> None:
    deploy, paths, *_ = deployment_fixture
    paths.state_dir.mkdir(mode=0o700, parents=True)
    record = deploy._identity_record_base(paths)
    record["retired_ui_releases"] = {"a" * 64: [1, 2]}
    deploy._write_identity(paths, record)
    result = {}
    deploy._cleanup_retired_ui(paths, SimpleNamespace(), result)
    assert "predates the UI cleanup owner" in result["storage"]["retained"][0]["reason"]
    assert deploy._read_identity(paths)["retired_ui_releases"] == {"a" * 64: [1, 2]}


def test_stable_acceptance_pin_requires_independent_verified_proof(deployment_fixture) -> None:
    deploy, *_ = deployment_fixture
    sha = "a" * 40
    content = {"backend_sha256": "b" * 64, "ui_sha256": "c" * 64,
               "host_pair_sha256": {row[0]: "d" * 64 for row in deploy.HOST_ROWS}}
    last = {"mode": "STABLE", "git_sha": sha,
            "qualification": {"source_sha": sha, "run_id": 1, "run_attempt": 1,
                "artifact_id": 2, "artifact_digest": "sha256:" + "e" * 64},
            "acceptance": {"status": "VERIFIED", "verified_at": "2026-11-19T00:00:00Z"},
            "native_proof": {"status": "PASS", "evidence_refs": ["f" * 64]},
            "source_content": content, "installed_content": content}
    assert deploy._accepted_stable_pin(last)["source_sha"] == sha
    last["native_proof"] = {"status": "NOT_VERIFIED", "evidence_refs": []}
    assert deploy._accepted_stable_pin(last) is None


def test_storage_preflight_rejects_insufficient_install_space_before_state_write(
        deployment_fixture, monkeypatch) -> None:
    deploy, paths, _, source, *_ = deployment_fixture
    identity_path = paths.state_dir / "deployment-identity.json"
    before = identity_path.read_bytes() if identity_path.exists() else None
    original_usage = deploy.shutil.disk_usage
    def usage(path):
        return (original_usage(path) if str(path) == "/tmp"
                else deploy.shutil._ntuple_diskusage(1 << 30, (1 << 30) - 1024, 1024))
    monkeypatch.setattr(deploy.shutil, "disk_usage", usage)
    with pytest.raises(deploy.DeploymentIncomplete, match="only 1024 are available"):
        deploy._storage_preflight(paths, source, ["Backend"], {})
    assert (identity_path.read_bytes() if identity_path.exists() else None) == before


def test_rollback_without_previous_pin_fails_without_remote_resolution(
        deployment_fixture, monkeypatch, capsys) -> None:
    deploy, paths, *_ = deployment_fixture
    monkeypatch.setattr(deploy, "_current_main", lambda: pytest.fail("rollback must use its accepted pin"))
    identity_path = paths.state_dir / "deployment-identity.json"
    before = identity_path.read_bytes() if identity_path.exists() else None
    assert deploy.main(["--rollback", "--json"], paths_override=paths) == 3
    result = json.loads(capsys.readouterr().out)
    assert result["result"] == "INCOMPLETE"
    assert not result.get("source_commit")
    assert (identity_path.read_bytes() if identity_path.exists() else None) == before


@pytest.mark.parametrize(
    ("mode", "expected_source"),
    [
        ("--stable", "Resolving qualified Stable source"),
        ("--dev", "Capturing immutable DEV source snapshot"),
    ],
)
def test_human_progress_is_flushed_to_stdout_before_deployment_finishes(
        tmp_path, mode: str, expected_source: str) -> None:
    (tmp_path / "opt").mkdir()
    driver = r"""
import importlib.util
import os
from pathlib import Path
import sys

spec = importlib.util.spec_from_file_location("progress_fixture", Path(sys.argv[1]))
deploy = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = deploy
spec.loader.exec_module(deploy)
root = Path(sys.argv[2])
mode = sys.argv[3]
paths = deploy.Paths(target_root=root, config=root / "config", state_dir=root / "state",
                     owner_uid=os.geteuid(), test_driver=True)

def wait_after_progress(report, progress, source):
    progress(source)
    if mode == "--dev":
        progress("Source /srv/fwrouter @ abcdef0 (dirty)")
    if sys.stdin.readline() != "continue\n":
        raise RuntimeError("fixture did not receive continuation")
    deploy._stage(report, "Source", "PASS", "fixture source verified")
    report["result"] = "NOOP"

def stable_fixture(_paths, report, *, progress, **_kwargs):
    wait_after_progress(report, progress, "Resolving qualified Stable source")

def development_fixture(_paths, report, *, progress, **_kwargs):
    wait_after_progress(report, progress, "Capturing immutable DEV source snapshot")

deploy._stable_deploy = stable_fixture
deploy._development_deploy = development_fixture
raise SystemExit(deploy.main([mode], paths_override=paths))
"""
    env = os.environ.copy()
    env.pop("PYTHONUNBUFFERED", None)
    proc = subprocess.Popen(
        [sys.executable, "-B", "-c", driver, str(MODULE), str(tmp_path), mode],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
    )
    assert proc.stdin is not None and proc.stdout is not None
    output = bytearray()
    try:
        deadline = time.monotonic() + 8
        expected = expected_source.encode()
        milestones = [expected]
        if mode == "--dev":
            milestones.append(b"Source /srv/fwrouter @ abcdef0 (dirty)")
        while not all(milestone in output for milestone in milestones) and time.monotonic() < deadline:
            ready, _, _ = select.select([proc.stdout], [], [], max(0, deadline - time.monotonic()))
            if not ready:
                break
            chunk = os.read(proc.stdout.fileno(), 4096)
            if not chunk:
                break
            output.extend(chunk)
        assert all(milestone in output for milestone in milestones)
        assert b"Result:" not in output
        assert proc.poll() is None
        with (tmp_path / "opt/.fwrouter-deploy-command.lock").open("rb") as transaction:
            with pytest.raises(BlockingIOError):
                fcntl.flock(transaction, fcntl.LOCK_EX | fcntl.LOCK_NB)
        remaining, diagnostics = proc.communicate(input=b"continue\n", timeout=8)
        output.extend(remaining)
        text = output.decode()
        assert proc.returncode == 0, diagnostics.decode()
        assert text.count("FWRouter Deployment") == 1
        assert text.index(expected_source) < text.index("Result: NOOP")
        assert not diagnostics
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate(timeout=5)


def test_deployment_json_excludes_progress_and_failure_is_diagnostic_on_stderr(
        deployment_fixture, monkeypatch, capsys) -> None:
    deploy, paths, *_ = deployment_fixture

    def fail_fixture(_paths, _report, *, progress, **_kwargs):
        progress("Checking storage preflight")
        raise deploy.DeploymentError("fixture storage unavailable")

    monkeypatch.setattr(deploy, "_stable_deploy", fail_fixture)
    assert deploy.main(["--stable", "--json"], paths_override=paths) == 1
    output = capsys.readouterr()
    report = json.loads(output.out)
    assert report["result"] == "FAILED"
    assert "fixture storage unavailable" in output.err
    assert "Checking storage preflight" not in output.out


def test_dev_cli_defaults_to_srv_and_passes_explicit_acknowledgement(
        deployment_fixture, monkeypatch, capsys) -> None:
    deploy, paths, *_ = deployment_fixture
    resolved = []

    def development_fixture(_paths, report, *, source_path, acknowledge_stable_replacement, **_kwargs):
        resolved.append((source_path, acknowledge_stable_replacement))
        report["result"] = "NOOP"

    monkeypatch.setattr(deploy, "_development_deploy", development_fixture, raising=False)
    assert deploy.main(["--dev", "--acknowledge-stable-replacement", "--json"], paths_override=paths) == 0
    assert json.loads(capsys.readouterr().out)["mode"] == "DEV"
    assert resolved == [(Path("/srv/fwrouter"), True)]


def test_shared_runtime_switches_stable_dirty_dev_and_back_without_requalifying_dev(
        deployment_fixture, monkeypatch, capsys) -> None:
    deploy, paths, _, old_source, database_marker, env, active, calls, owner_driver, system_driver = deployment_fixture
    real_report = deploy._report_lifecycle

    def report_fixture(target, target_paths, lifecycle):
        real_report(target, target_paths, lifecycle)
        if not target.get("pending_units"):
            deploy._stage(target, "Timers", "PASS", "disposable timer readback")

    monkeypatch.setattr(deploy, "_report_lifecycle", report_fixture)
    run = deploy._run

    def guard_runtime_commands(argv, **kwargs):
        assert Path(argv[0]).name not in {"docker", "systemctl"}, argv
        return run(argv, **kwargs)

    monkeypatch.setattr(deploy, "_run", guard_runtime_commands)
    origin = pristine_checkout(old_source.parent / "shared-runtime-origin")
    developer = pristine_checkout(old_source.parent / "shared-runtime-dev")
    legacy_sha = command(["/usr/bin/git", "-C", str(developer), "rev-parse", "HEAD"]).stdout.strip()
    with (origin / "backend/fwrouter_api/__init__.py").open("a", encoding="utf-8") as stream:
        stream.write("\n# accepted Stable source absent from the developer Git history\n")
    command(["/usr/bin/git", "-C", str(origin), "add", "backend/fwrouter_api/__init__.py"])
    command(["/usr/bin/git", "-C", str(origin), "-c", "user.name=FWRouter Test",
             "-c", "user.email=fwrouter-test@example.invalid", "commit", "--quiet", "-m", "fixture next Stable"])
    sha = command(["/usr/bin/git", "-C", str(origin), "rev-parse", "HEAD"]).stdout.strip()
    command(["/usr/bin/git", "-C", str(origin), "update-ref", "refs/heads/main", sha])
    monkeypatch.setattr(deploy, "MAIN_ORIGIN", str(origin))
    qualification_calls = []
    monkeypatch.setattr(deploy, "_current_main", lambda: sha)
    def qualify(_source, source_sha, *_args):
        qualification_calls.append(source_sha)
        return _qualification(source_sha)
    monkeypatch.setattr(deploy, "_stable_qualification", qualify)
    monkeypatch.setattr(deploy, "_gh_api", lambda *_args: pytest.fail("fixture unexpectedly tried GitHub"))
    monkeypatch.setattr(deploy, "_native_acceptance", lambda *_: {
        "status": "PASS", "reason": None, "scope": "ui", "proof_sha256": "e" * 64})
    deploy._write_config(paths.config, old_source, legacy_sha, paths.owner_uid)

    def invoke(*argv):
        code = deploy.main([*argv, "--json"], paths_override=paths, owner_driver=owner_driver,
                           system_driver=system_driver)
        return code, json.loads(capsys.readouterr().out)

    code, stable = invoke("--stable")
    assert code == 0, stable["stages"]
    baseline = deploy._read_identity(paths)
    accepted = baseline["stable_current"]
    stable_ui = paths.target("opt/fwrouter-ui").resolve()
    previous = baseline.get("stable_previous")
    identity_file = paths.state_dir / "deployment-identity.json"
    database_bytes = database_marker.read_bytes()
    env_bytes = env.read_bytes()
    index_bytes = (developer / ".git/index").read_bytes()
    tracked = developer / "ui/index.html"
    extra = developer / "backend/dirty-dev-only.py"
    tracked.write_text("dirty DEV one\n")
    extra.write_text("FIXTURE = 1\n")
    args = ("--dev", "--source", str(developer))
    existing = identity_file.read_bytes()
    before = dict(calls)
    code, denied = invoke(*args)
    assert code != 0
    assert denied["result"] in {"INCOMPLETE", "FAILED"}
    assert identity_file.read_bytes() == existing and calls == before

    code, first = invoke(*args, "--acknowledge-stable-replacement")
    assert code == 0, first["stages"]
    assert first["stages"]["CI"]["status"] == "SKIP"
    assert len(qualification_calls) == 1
    assert (paths.target("opt/fwrouter-api/dirty-dev-only.py")).read_text() == "FIXTURE = 1\n"
    assert (paths.target("opt/fwrouter-ui/index.html")).read_text() == "dirty DEV one\n"
    identity = deploy._read_identity(paths)
    assert identity["last_verified_install"]["mode"] == "DEV"
    assert identity["last_verified_install"]["dirty"] is True
    assert identity["stable_current"] == accepted and identity.get("stable_previous") == previous
    first_ui = paths.target("opt/fwrouter-ui").resolve()
    assert first_ui != stable_ui
    first_capsule = Path(identity["dev_current"]["snapshot_path"])
    assert first_capsule.is_file()
    unrelated_capsule = first_capsule.with_name("f" * 64 + ".tar")
    unrelated_capsule.write_bytes(b"independently managed data")
    unrelated_capsule.chmod(0o600)

    tracked.write_text("dirty DEV two\n")
    extra.unlink()
    code, second = invoke(*args)
    assert code == 0, second["stages"]
    assert not paths.target("opt/fwrouter-api/dirty-dev-only.py").exists()
    assert (paths.target("opt/fwrouter-ui/index.html")).read_text() == "dirty DEV two\n"
    assert not first_capsule.exists()
    assert not first_ui.exists() and stable_ui.is_dir()
    second_ui = paths.target("opt/fwrouter-ui").resolve()
    assert second["storage"]["cleanup_reclaimed_bytes"] > 0
    second_capsule = Path(deploy._read_identity(paths)["dev_current"]["snapshot_path"])
    assert second_capsule.is_file()
    assert unrelated_capsule.read_bytes() == b"independently managed data"
    assert len(qualification_calls) == 1
    before = dict(calls)
    existing = identity_file.read_bytes()
    code, noop = invoke(*args)
    assert code == 0 and noop["result"] == "NOOP"
    assert identity_file.read_bytes() == existing and calls == before
    code, checked = invoke("--dev", "--check")
    assert code == 0
    assert checked["storage"]["dev_current"]
    assert checked["storage"]["stable_rollback"] == sha
    assert identity_file.read_bytes() == existing

    component_installs = []
    run_installer = deploy._run_installer
    with monkeypatch.context() as ui_only:
        def record_components(source, target, components, **kwargs):
            component_installs.append(components)
            return run_installer(source, target, components, **kwargs)

        ui_only.setattr(deploy, "_run_installer", record_components)
        ui_only.setattr(deploy, "_ensure_dev_baseline_backup",
                        lambda *_args, **_kwargs: pytest.fail("UI-only deploy tried to prepare a SQLite backup"))
        tracked.write_text("dirty DEV UI only\n")
        code, ui = invoke(*args)
        assert code == 0, ui["stages"]
        assert ui["components"]["updated"] == ["UI"]
        assert component_installs == [["ui"]]
    assert not second_capsule.exists()
    assert not second_ui.exists() and stable_ui.is_dir()
    active_ui = paths.target("opt/fwrouter-ui").resolve()
    active_capsule = Path(deploy._read_identity(paths)["dev_current"]["snapshot_path"])
    assert active_capsule.is_file()

    code, restored = invoke("--stable")
    assert code == 0, restored["stages"]
    assert deploy._read_identity(paths)["stable_current"]["source_sha"] == sha
    assert deploy._read_identity(paths)["last_verified_install"]["mode"] == "STABLE"
    assert not active_capsule.exists()
    assert not active_ui.exists() and stable_ui.is_dir()
    assert unrelated_capsule.read_bytes() == b"independently managed data"
    assert (paths.target("opt/fwrouter-ui/index.html")).read_bytes() == (origin / "ui/index.html").read_bytes()
    assert len(qualification_calls) == 2
    assert (developer / ".git/index").read_bytes() == index_bytes
    assert tracked.read_text() == "dirty DEV UI only\n"
    assert database_marker.read_bytes() == database_bytes and env.read_bytes() == env_bytes
    assert active["fwrouter-xray.service"] and active["fwrouter-mihomo.service"]


@pytest.mark.parametrize("accept_dev_before_rollback", [False, True])
def test_interrupted_dev_resumes_and_rolls_back_after_original_source_is_deleted(
        deployment_fixture, monkeypatch, capsys, accept_dev_before_rollback) -> None:
    deploy, paths, _, old_source, _db, _env, _active, calls, owner_driver, system_driver = deployment_fixture
    real_report = deploy._report_lifecycle

    def report_fixture(target, target_paths, lifecycle):
        real_report(target, target_paths, lifecycle)
        if not target.get("pending_units"):
            deploy._stage(target, "Timers", "PASS", "disposable timer readback")

    monkeypatch.setattr(deploy, "_report_lifecycle", report_fixture)
    origin = pristine_checkout(old_source.parent / "dev-resume-origin")
    developer = pristine_checkout(old_source.parent / "dev-resume-source")
    sha = command(["/usr/bin/git", "-C", str(origin), "rev-parse", "HEAD"]).stdout.strip()
    command(["/usr/bin/git", "-C", str(origin), "update-ref", "refs/heads/main", sha])
    monkeypatch.setattr(deploy, "MAIN_ORIGIN", str(origin))
    monkeypatch.setattr(deploy, "_current_main", lambda: sha)
    monkeypatch.setattr(deploy, "_stable_qualification", lambda _source, source_sha, *_: _qualification(source_sha))
    monkeypatch.setattr(deploy, "_gh_api", lambda *_: pytest.fail("fixture unexpectedly tried GitHub"))
    monkeypatch.setattr(deploy, "_native_acceptance", lambda *_: {
        "status": "PASS", "reason": None, "scope": "ui", "proof_sha256": "e" * 64})
    deploy._write_config(paths.config, old_source, sha, paths.owner_uid)

    def invoke(*argv, driver=owner_driver):
        code = deploy.main([*argv, "--json"], paths_override=paths, owner_driver=driver,
                           system_driver=system_driver)
        return code, json.loads(capsys.readouterr().out)

    code, stable = invoke("--stable")
    assert code == 0, stable["stages"]
    (developer / "backend/dev-resume-local.py").write_text("SENTINEL = True\n")
    (developer / "ui/index.html").write_text("captured before interruption\n")

    def interrupted(lifecycle, action, *args):
        if action == "resume":
            raise KeyboardInterrupt
        return owner_driver(lifecycle, action, *args)

    args = ("--dev", "--source", str(developer))
    code, partial = invoke(*args, "--acknowledge-stable-replacement", driver=interrupted)
    assert code == 130, partial["stages"]
    assert partial["result"] == "INCOMPLETE"
    assert (paths.target("opt/fwrouter-api/dev-resume-local.py")).exists()
    before = dict(calls)
    shutil.rmtree(developer)

    proof = deploy._native_acceptance
    monkeypatch.setattr(deploy, "_native_acceptance", lambda *_: {
        "status": "NOT_VERIFIED", "reason": "fixture native proof unavailable"})
    code, incomplete = invoke(*args)
    assert code == 3 and incomplete["result"] == "INCOMPLETE", incomplete["stages"]
    assert incomplete["native_proof"]["status"] == "NOT_VERIFIED"
    assert deploy._read_identity(paths)["dev_admission"]["mode"] == "DEV"
    assert calls == before
    assert incomplete["stages"]["CI"]["status"] == "SKIP"
    assert (paths.target("opt/fwrouter-ui/index.html")).read_text() == "captured before interruption\n"
    monkeypatch.setattr(deploy, "_native_acceptance", proof)

    if accept_dev_before_rollback:
        code, recovered = invoke(*args)
        if code != 0:
            pytest.fail(f"Captured DEV retry: {recovered['stages']} / {recovered.get('health')}", pytrace=False)
        assert calls == before
        assert deploy._read_identity(paths).get("dev_admission") is None

    code, rolled_back = invoke("--rollback")
    assert code == 0, rolled_back["stages"]
    assert not paths.target("opt/fwrouter-api/dev-resume-local.py").exists()
    identity = deploy._read_identity(paths)
    assert identity["last_verified_install"]["mode"] == "STABLE"
    assert identity["stable_current"]["source_sha"] == sha


def test_dev_baseline_recovery_creates_a_separate_verified_pin_and_reuses_it(
        deployment_fixture, monkeypatch) -> None:
    deploy, paths, *_ = deployment_fixture
    paths = deploy.replace(paths, test_driver=False)
    stable_sha, manifest = "a" * 40, "b" * 64
    previous = {"id": "d" * 32, "source_sha": "c" * 40, "backend_sha256": "f" * 64, "schema": 27,
                "filename": "rollback-" + "d" * 32 + ".bak", "sha256": "8" * 64, "snapshot_sha256": "9" * 64}
    identity = {"stable_current": {"source_sha": stable_sha, "installed_content": {"backend_sha256": manifest}},
                "database_rollback": previous.copy(), "dev_admission": {"test": True}}
    current_manifest = [manifest]
    lifecycle = SimpleNamespace(_manifest_digest=lambda _items: current_manifest[0],
                                _backend_manifest=lambda _path: [])
    writes = []
    verified = []
    created = []

    @contextmanager
    def lock(_paths):
        yield

    def prepare(_paths, _lifecycle, reservation):
        assert identity["dev_baseline_backup"] == reservation
        created.append(reservation)
        return {**reservation, "filename": "rollback-" + "d" * 32 + ".bak",
                "sha256": "1" * 64, "snapshot_sha256": "2" * 64}

    monkeypatch.setattr(deploy, "_configuration_component_lock", lock)
    monkeypatch.setattr(deploy, "_read_identity", lambda _paths: identity)
    monkeypatch.setattr(deploy, "_write_identity", lambda _paths, record: writes.append(dict(record)))
    monkeypatch.setattr(deploy, "_prepare_managed_backup", prepare)
    monkeypatch.setattr(deploy, "_validate_managed_backup", lambda _paths, _lifecycle, pin: verified.append(pin))

    result = {}
    deploy._ensure_dev_baseline_backup(paths, lifecycle, {}, result)
    assert result["backup"]["status"] == "CREATED"
    assert len(created) == 1 and len(writes) == 2
    assert identity["database_rollback"] == previous
    pin = identity["dev_baseline_backup"]
    assert pin["source_sha"] == stable_sha and pin["backend_sha256"] == manifest
    assert verified == [pin]

    current_manifest[0] = "0" * 64
    identity["stable_admission"] = {"test": True}
    deploy._ensure_dev_baseline_backup(paths, lifecycle, {}, result, returning=True)
    assert result["backup"]["status"] == "REUSED"
    assert len(created) == 1 and len(writes) == 2 and verified == [pin, pin]
    assert identity["database_rollback"] == previous

    identity.pop("dev_baseline_backup")
    with pytest.raises(deploy.DeploymentIncomplete, match="no verified SQLite recovery backup"):
        deploy._ensure_dev_baseline_backup(paths, lifecycle, {}, result, returning=True)
    assert len(created) == 1 and len(writes) == 2


@pytest.mark.parametrize("stable_command", ["--stable", "--rollback"])
def test_byte_identical_dev_returns_to_stable_without_publication(
        deployment_fixture, monkeypatch, capsys, stable_command) -> None:
    deploy, paths, _, old_source, _db, _env, _active, calls, owner_driver, system_driver = deployment_fixture
    origin = pristine_checkout(old_source.parent / "identical-origin")
    developer = pristine_checkout(old_source.parent / "identical-dev")
    sha = command(["/usr/bin/git", "-C", str(origin), "rev-parse", "HEAD"]).stdout.strip()
    command(["/usr/bin/git", "-C", str(origin), "update-ref", "refs/heads/main", sha])
    monkeypatch.setattr(deploy, "MAIN_ORIGIN", str(origin))
    monkeypatch.setattr(deploy, "_current_main", lambda: sha)
    monkeypatch.setattr(deploy, "_stable_qualification", lambda _source, source_sha, *_: _qualification(source_sha))
    monkeypatch.setattr(deploy, "_gh_api", lambda *_: pytest.fail("fixture unexpectedly tried GitHub"))
    monkeypatch.setattr(deploy, "_native_acceptance", lambda *_: {
        "status": "PASS", "reason": None, "scope": "ui", "proof_sha256": "e" * 64})
    report_lifecycle = deploy._report_lifecycle

    def report_fixture(target, target_paths, lifecycle):
        report_lifecycle(target, target_paths, lifecycle)
        if not target.get("pending_units"):
            deploy._stage(target, "Timers", "PASS", "disposable timer readback")

    monkeypatch.setattr(deploy, "_report_lifecycle", report_fixture)
    deploy._write_config(paths.config, old_source, sha, paths.owner_uid)

    def invoke(*argv):
        code = deploy.main([*argv, "--json"], paths_override=paths, owner_driver=owner_driver,
                           system_driver=system_driver)
        return code, json.loads(capsys.readouterr().out)

    code, stable = invoke("--stable")
    assert code == 0, stable["stages"]
    before = dict(calls)
    (developer / "installer/README.md").write_text("dirty source with byte-identical runtime components\n")
    monkeypatch.setattr(deploy, "_run_installer", lambda *_a, **_k: pytest.fail("identical content republished"))

    code, dev = invoke("--dev", "--source", str(developer), "--acknowledge-stable-replacement")
    assert code == 0, dev["stages"]
    assert "_dev_unadmitted_snapshot" not in dev
    assert dev["stages"]["Install"]["status"] == "SKIP"
    pin = deploy._read_identity(paths)["dev_current"]
    assert pin["dirty"] is True and Path(pin["snapshot_path"]).is_file()

    code, stable = invoke(stable_command)
    assert code == 0, stable["stages"]
    assert stable["stages"]["Install"]["status"] == "SKIP"
    record = deploy._read_identity(paths)
    assert record["last_verified_install"]["mode"] == "STABLE"
    assert record["last_verified_install"]["git_sha"] == sha
    assert record["last_verified_install"]["acceptance"]["status"] == "VERIFIED"
    assert record.get("dev_current") is None
    assert record.get("dev_admission") is None
    assert not Path(pin["snapshot_path"]).exists()
    assert calls == before


def test_dev_rejects_legacy_lifecycle_without_modifying_source(deployment_fixture, tmp_path) -> None:
    deploy, paths, _, source, *_ = deployment_fixture
    owner = source / "installer/controlled_backend_lifecycle.py"
    old_owner = owner.read_text(encoding="utf-8").replace("_preflight_timer_restore", "_legacy_timer_preflight")
    old_owner = old_owner.replace("--restore-timers", "--legacy-restore-timers")
    owner.write_text(old_owner, encoding="utf-8")
    before = owner.read_bytes()

    with pytest.raises(deploy.DeploymentIncomplete, match="Pinned Stable lifecycle owner"):
        deploy._require_development_lifecycle_contract(source, SimpleNamespace())

    assert owner.read_bytes() == before
    assert not paths.state_dir.exists()


def test_qualified_stable_lifecycle_capsule_is_exact_and_bound_to_installed_updater(
        deployment_fixture, monkeypatch, tmp_path) -> None:
    deploy, paths, _, _, *_ = deployment_fixture
    source = pristine_checkout(tmp_path / "qualified-owner-source")
    source_sha = command(["/usr/bin/git", "-C", str(source), "rev-parse", "HEAD"]).stdout.strip()
    qualification = _qualification(source_sha)
    paths = deploy.replace(paths, test_driver=False)
    paths.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    paths.state_dir.chmod(0o700)
    updater = paths.target("usr/local/libexec/fwrouter/local_deploy.py")
    updater.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    shutil.copyfile(MODULE, updater)
    updater.chmod(0o644)
    updater_sha = deploy._digest(updater)

    synced = []
    sync_directory = deploy._sync_lifecycle_owner_directory

    def track_sync(directory):
        synced.append(directory)
        sync_directory(directory)

    monkeypatch.setattr(deploy, "_sync_lifecycle_owner_directory", track_sync)
    pin = deploy._publish_lifecycle_owner_capsule(
        paths, source, source_sha, qualification, updater_sha)
    stage = synced[3]
    owner_root = paths.state_dir / deploy.LIFECYCLE_OWNER_ROOT
    assert synced == [stage / "host/systemd", stage / "host", stage / "installer", stage,
                      owner_root, paths.state_dir]
    assert stage.parent == owner_root and not stage.exists()
    synced.clear()
    assert deploy._publish_lifecycle_owner_capsule(paths, source, source_sha, qualification, updater_sha) == pin
    assert synced == [owner_root, paths.state_dir]
    record = {"stable_current": {"source_sha": source_sha, "qualification": qualification},
              "lifecycle_owners": {source_sha: pin},
              "components": {"updater": {"installed_sha256": updater_sha,
                                           "qualification_status": "VERIFIED_MAIN_CI"}}}
    monkeypatch.setattr(deploy, "_read_identity", lambda _paths: record)
    loaded, directory = deploy._read_lifecycle_owner_capsule(paths, source_sha)
    assert loaded == pin
    assert deploy._digest(directory / "installer/controlled_backend_lifecycle.py") == pin["lifecycle_sha256"]
    assert set((directory / "host/systemd").iterdir()) == {
        directory / "host/systemd" / name for name in pin["timer_sha256"]}

    (directory / "host/systemd/fwrouter-maintenance.timer").write_text("tampered\n")
    with pytest.raises(deploy.DeploymentError, match="exact digest pin"):
        deploy._read_lifecycle_owner_capsule(paths, source_sha)


def test_dev_admission_rechecks_installed_mode_and_snapshot_under_component_lock(
        deployment_fixture, monkeypatch) -> None:
    deploy, paths, *_ = deployment_fixture
    stable = {"mode": "STABLE", "git_sha": "a" * 40}
    record = {"last_verified_install": stable}
    monkeypatch.setattr(deploy, "_identity_record_base", lambda _paths: record)
    monkeypatch.setattr(deploy, "_write_identity", lambda *_args: pytest.fail("stale DEV admission was persisted"))

    with pytest.raises(deploy.DeploymentIncomplete, match="Installed identity changed"):
        deploy._remember_development_admission(paths, {}, {"mode": "DEV", "git_sha": "b" * 40}, False)
    with pytest.raises(deploy.DeploymentIncomplete, match="acknowledge-stable-replacement"):
        deploy._remember_development_admission(paths, {}, stable, False)
    missing = {"snapshot_path": str(paths.state_dir / "missing.tar"), "snapshot_sha256": "a" * 64}
    with pytest.raises(FileNotFoundError):
        deploy._remember_development_admission(paths, missing, stable, True)
    assert "dev_admission" not in record


@pytest.mark.parametrize("candidate_profile", [
    "legacy_migrated_schema27_manual_ping_additive",
    "legacy_migrated_schema27_from_7",
])
def test_legacy_database_admission_requires_both_owner_readbacks(
        deployment_fixture, monkeypatch, candidate_profile) -> None:
    deploy, paths, source, *_ = deployment_fixture
    paths = deploy.replace(paths, test_driver=False)
    owner = SimpleNamespace(_verified_backend_interpreter=lambda: Path(sys.executable))
    profile = deploy.DEPLOYMENT_LEGACY_SCHEMA27
    calls = []
    monkeypatch.setattr(deploy, "_stable_service_state", lambda *_: None)

    def run(argv, **kwargs):
        assert kwargs["host_environment"]
        calls.append(argv)
        if deploy._READ_INSTALLED_SCHEMA27 in argv:
            return subprocess.CompletedProcess(argv, 0, json.dumps({
                "ok": True, "compatibility_profile": profile}), "")
        if "preflight" in argv:
            assert argv[-1] == profile
            return subprocess.CompletedProcess(argv, 0, json.dumps({
                "ok": True, "status": "already_current", "current_schema": 27,
                "target_schema": 27, "database_changed": False, "compatibility_profile": candidate_profile,
                "integrity": "ok", "foreign_key_violations": 0}), "")
        return subprocess.CompletedProcess(argv, 1, json.dumps({
            "ok": False, "error_code": "DATABASE_SCHEMA_PREFLIGHT_FAILED"}), "")

    monkeypatch.setattr(deploy, "_run", run)
    if candidate_profile == profile:
        assert deploy._strict_database_preflight(paths, owner, source) == profile
    else:
        with pytest.raises(deploy.DeploymentError, match=r"Database preflight \(selected\)"):
            deploy._strict_database_preflight(paths, owner, source)
    assert len(calls) == 4


def test_native_policy_uses_independent_approval_and_checked_runtime_images(
        deployment_fixture, monkeypatch) -> None:
    deploy, paths, *_ = deployment_fixture
    paths = deploy.replace(paths, test_driver=False)
    xray, mihomo = "sha256:" + "a" * 64, "sha256:" + "b" * 64
    policy = {"xray_image": xray, "mihomo_image": mihomo}
    observed = {"xray": xray, "mihomo": mihomo}
    monkeypatch.setattr(deploy, "_observed_runtime_image_ids", lambda _: observed)
    with pytest.raises(deploy.DeploymentIncomplete, match="policy is missing"):
        deploy._require_acceptance_policy(paths, {})
    observed["mihomo"] = "UNKNOWN"
    with pytest.raises(deploy.DeploymentIncomplete, match="Cannot read both"):
        deploy._require_acceptance_policy(paths, policy)
    observed["mihomo"] = xray
    with pytest.raises(deploy.DeploymentError, match="approved policy: mihomo"):
        deploy._require_acceptance_policy(paths, policy)
    observed["mihomo"] = mihomo
    assert deploy._require_acceptance_policy(paths, policy) == policy


def test_legacy_recovery_requires_recorded_identity_exact_main_evidence_and_fresh_proof(
        deployment_fixture, monkeypatch) -> None:
    deploy, paths, source, *_ = deployment_fixture
    sha = "a" * 40
    head = "b" * 40
    selection = deploy.StableSource(source, _qualification(head))
    content = {"backend_sha256": "c" * 64, "ui_sha256": "d" * 64, "host_pair_sha256": "e" * 64}
    last = {"mode": "UNKNOWN", "git_sha": sha, "installed_content": content, "source_content": content}
    paths = deploy.replace(paths, test_driver=False)
    calls = []
    proof = {"status": "NOT_VERIFIED", "reason": "receipt absent"}

    def qualify(_source, requested, _scratch, _owner, *, historical_head):
        assert requested == sha and historical_head == head
        calls.append(requested)
        return _qualification(requested)

    monkeypatch.setattr(deploy, "_installed_content_identity", lambda *_: content)
    monkeypatch.setattr(deploy, "_source_content_identity", lambda *_: content)
    monkeypatch.setattr(deploy, "_run", lambda argv, **_: subprocess.CompletedProcess(argv, 0, "", ""))
    monkeypatch.setattr(deploy, "_stable_qualification", qualify)
    monkeypatch.setattr(deploy, "_native_acceptance", lambda *args: proof)
    with pytest.raises(deploy.DeploymentIncomplete, match="exact legacy install identity"):
        deploy._discover_stable_baseline(paths, selection, {}, None, None, {})
    with pytest.raises(deploy.DeploymentIncomplete, match="fresh native proof"):
        deploy._discover_stable_baseline(paths, selection, {"last_verified_install": last}, None, None, {})
    proof.update(status="PASS", proof_sha256="f" * 64)
    pin = deploy._discover_stable_baseline(paths, selection, {"last_verified_install": last}, None, None, {})
    assert pin["source_sha"] == sha and pin["installed_content"] == content
    assert pin["qualification"] == _qualification(sha) and calls == [sha, sha]
    assert last["mode"] == "UNKNOWN"


def test_legacy_rollback_can_use_its_verified_historical_schema_inspector(
        deployment_fixture, monkeypatch) -> None:
    deploy, paths, source, *_ = deployment_fixture
    paths = deploy.replace(paths, test_driver=False,
                           stable_source=deploy.StableSource(source, _qualification("a" * 40)))
    owner = SimpleNamespace(_verified_backend_interpreter=lambda: Path(sys.executable))
    profile = deploy.DEPLOYMENT_LEGACY_SCHEMA27
    monkeypatch.setattr(deploy, "_stable_service_state", lambda *_: None)
    fallback = []

    def run(argv, **_kwargs):
        code = argv[4] if "-c" in argv else ""
        if code.endswith(deploy._READ_INSTALLED_SCHEMA27):
            if code != deploy._READ_INSTALLED_SCHEMA27:
                assert argv[5] == str(source / "backend")
                fallback.append(True)
            return subprocess.CompletedProcess(argv, 0, json.dumps({
                "ok": True, "compatibility_profile": profile}), "")
        if "preflight" in argv:
            return subprocess.CompletedProcess(argv, 2, "", "")
        return subprocess.CompletedProcess(argv, 1, json.dumps({
            "ok": False, "error_code": "DATABASE_SCHEMA_PREFLIGHT_FAILED"}), "")

    monkeypatch.setattr(deploy, "_run", run)
    with pytest.raises(deploy.DeploymentError, match=r"Database preflight \(selected\)"):
        deploy._strict_database_preflight(paths, owner, source)
    assert not fallback
    assert deploy._strict_database_preflight(paths, owner, source, legacy_rollback=True) == profile
    assert fallback == [True]
