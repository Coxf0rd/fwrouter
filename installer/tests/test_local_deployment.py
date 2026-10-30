"""Public deployment command regressions using disposable installer targets."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
from types import SimpleNamespace

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
    assert json.loads(result.stdout.splitlines()[-1])["status"] == "READY_TO_APPLY"
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
