from __future__ import annotations

import atexit
import importlib.util
import json
import os as os
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
from unittest.mock import patch
import uuid

import pytest


TOOL = Path(__file__).parents[1] / "controlled_backend_lifecycle.py"
SPEC = importlib.util.spec_from_file_location("fwrouter_controlled_backend_lifecycle", TOOL)
assert SPEC and SPEC.loader
lifecycle = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(lifecycle)


class FakeSystemd:
    def __init__(self, active: set[str]) -> None:
        self.active = set(active)
        self.calls: list[tuple[str, ...]] = []
        self.fail_start: set[str] = set()
        self.start_job_on_stop: str | None = None

    def run(self, argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append(tuple(argv))
        action, unit = argv[1], argv[-1]
        if action == "is-active":
            active = unit in self.active
            return subprocess.CompletedProcess(argv, 0 if active else 3,
                                               "active\n" if active else "inactive\n", "")
        if action == "stop":
            self.active.discard(unit)
            if unit.endswith(".timer") and self.start_job_on_stop:
                self.active.add(self.start_job_on_stop)
                self.start_job_on_stop = None
            return subprocess.CompletedProcess(argv, 0, "", "")
        if action == "start":
            if unit in self.fail_start:
                return subprocess.CompletedProcess(argv, 1, "", "injected start failure")
            self.active.add(unit)
            return subprocess.CompletedProcess(argv, 0, "", "")
        raise AssertionError(f"unexpected systemctl action: {action}")


class FakeMihomoRuntime(FakeSystemd):
    def __init__(self, active: set[str]) -> None:
        super().__init__(active)
        self.container_running = True
        self.container_id = "a" * 64
        self.image_id = "sha256:" + "b" * 64
        self.config_hash = "c" * 64
        self.enabled = "enabled"
        self.drop_ins = ""
        self.cache_path: Path | None = None
        self.cache_after_stop: bytes | None = None
        self.cache_root: Path | None = None

    def run(self, argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append(tuple(argv))
        if argv[0] == "/usr/bin/docker":
            if argv[1:3] == ["inspect", "--format"]:
                result = (f"{self.container_id}|{self.image_id}|{lifecycle.MIHOMO_CONFIG_IMAGE}|"
                          f"{str(self.container_running).lower()}|unless-stopped|host|mihomo|"
                          f"{self.config_hash}|true\n")
                return subprocess.CompletedProcess(argv, 0, result, "")
            if argv[1:3] == ["stop", "--time"]:
                assert argv == ["/usr/bin/docker", "stop", "--time", "30", self.container_id]
                self.container_running = False
                if self.cache_after_stop is not None:
                    assert self.cache_path is not None
                    self.cache_path.write_bytes(self.cache_after_stop)
                return subprocess.CompletedProcess(argv, 0, self.container_id[:12] + "\n", "")
            if argv[1] == "start":
                assert argv == ["/usr/bin/docker", "start", self.container_id]
                self.container_running = True
                return subprocess.CompletedProcess(argv, 0, self.container_id[:12] + "\n", "")
            raise AssertionError(f"unexpected docker command: {argv}")
        if argv[0] != "/usr/bin/systemctl":
            raise AssertionError(f"unexpected executable: {argv[0]}")
        if argv[1] == "show":
            prop = argv[2].removeprefix("--property=")
            values = {
                "LoadState": "loaded", "FragmentPath": "/etc/systemd/system/fwrouter-mihomo.service",
                "DropInPaths": self.drop_ins, "Type": "oneshot", "RemainAfterExit": "yes",
                "ActiveState": "active" if lifecycle.MIHOMO_UNIT in self.active else "inactive",
                "SubState": "exited" if lifecycle.MIHOMO_UNIT in self.active else "dead",
            }
            return subprocess.CompletedProcess(argv, 0, values[prop] + "\n", "")
        if argv[1] == "is-enabled":
            value = self.enabled
            return subprocess.CompletedProcess(argv, 0 if value != "disabled" else 1,
                                               value + "\n", "")
        return super().run(argv, **kwargs)


def _paths(tmp_path: Path, source_content: str = "SOURCE_SENTINEL = 'expected'\n") -> tuple[Path, Path]:
    source = tmp_path / "source-backend"
    target = tmp_path / "target-backend"
    source.mkdir(exist_ok=True)
    (source / "module.py").write_text(source_content, encoding="utf-8")
    return source, target


def _install_fixture(source: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for item in source.iterdir():
        (target / item.name).write_bytes(item.read_bytes())


def _run(tmp_path: Path, command: str, systemd: FakeSystemd, *, api_healthy: bool = True,
         allow_timer_catchup: bool = False, install_target: bool = True,
         source_content: str = "SOURCE_SENTINEL = 'expected'\n") -> dict[str, object]:
    args = [command, "--state-dir", str(tmp_path / "rollout-state")]
    if allow_timer_catchup:
        args.append("--allow-timer-catchup")
    source, target = _paths(tmp_path, source_content)
    if install_target and not target.exists():
        _install_fixture(source, target)
    def private_test_state(path: Path) -> Path:
        if path.is_symlink():
            raise lifecycle.LifecycleError("state directory path contains a symlink")
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.chmod(0o700)
        return path

    with patch.object(lifecycle.subprocess, "run", side_effect=systemd.run), \
            patch.object(lifecycle, "_api_health", return_value=api_healthy), \
            patch.object(lifecycle, "_gateway_listener_ready", return_value=True), \
            patch.object(lifecycle, "SOURCE_BACKEND", source), \
            patch.object(lifecycle, "TARGET_BACKEND", target), \
            patch.object(lifecycle, "_validate_state_dir", side_effect=private_test_state), \
            patch.object(lifecycle.os, "geteuid", return_value=0):
        return lifecycle.run_command(args)


def _run_transfer(tmp_path: Path, systemd: FakeSystemd, operation_id: str,
                  old_digest: str, new_digest: str, *, source_content: str) -> dict[str, object]:
    source, target = _paths(tmp_path, source_content)
    args = ["transfer", "--state-dir", str(tmp_path / "rollout-state"),
            "--expected-operation-id", operation_id,
            "--expected-current-manifest-sha256", old_digest,
            "--expected-new-manifest-sha256", new_digest]

    def private_test_state(path: Path) -> Path:
        if path.is_symlink():
            raise lifecycle.LifecycleError("state directory path contains a symlink")
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.chmod(0o700)
        return path

    with patch.object(lifecycle.subprocess, "run", side_effect=systemd.run), \
            patch.object(lifecycle, "SOURCE_BACKEND", source), \
            patch.object(lifecycle, "TARGET_BACKEND", target), \
            patch.object(lifecycle, "_validate_state_dir", side_effect=private_test_state), \
            patch.object(lifecycle.os, "geteuid", return_value=0):
        return lifecycle.run_command(args)


def _run_mihomo_lifecycle(tmp_path: Path, command: str, runtime: FakeMihomoRuntime,
                           operation_id: str, current_digest: str, compose_hash: str,
                           compose_digest: str, *, source_content: str,
                           jit_owner_quiescence: bool = False,
                           api_healthy: bool = True) -> dict[str, object]:
    source, target = _paths(tmp_path, source_content)
    args = [command, "--state-dir", str(tmp_path / "rollout-state"),
            "--expected-operation-id", operation_id,
            "--expected-current-manifest-sha256", current_digest,
            "--expected-container-id", runtime.container_id,
            "--expected-image-id", runtime.image_id,
            "--expected-compose-config-hash", compose_hash,
            "--expected-compose-file-sha256", compose_digest]
    if jit_owner_quiescence:
        args.append("--jit-owner-quiescence")

    def private_test_state(path: Path) -> Path:
        if path.is_symlink():
            raise lifecycle.LifecycleError("state directory path contains a symlink")
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.chmod(0o700)
        return path

    compose_path = tmp_path / "docker-compose.yml"
    compose_path.write_text("services:\n  mihomo:\n    image: pinned\n", encoding="utf-8")
    compose_path.chmod(0o600)
    if runtime.cache_root is None:
        runtime.cache_root = Path(tempfile.mkdtemp(prefix="fwrouter-jit-cache-test-", dir="/root"))
        runtime.cache_root.chmod(0o700)
        atexit.register(shutil.rmtree, runtime.cache_root, ignore_errors=True)
    cache_path = runtime.cache_root / "var/lib/fwrouter-v2/mihomo/cache.db"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    if not cache_path.exists():
        cache_path.write_bytes(b"synthetic cache before\n")
        cache_path.chmod(0o600)
    runtime.cache_path = cache_path
    unit_path = TOOL.parents[1] / "host/systemd/fwrouter-mihomo.service"
    import hashlib
    unit_digest = hashlib.sha256(unit_path.read_bytes()).hexdigest()
    assert unit_digest == lifecycle.MIHOMO_UNIT_SHA256
    def fixed_digest(path: Path, **_kwargs: object) -> str:
        if Path(path) == unit_path:
            return unit_digest
        if Path(path) == compose_path:
            return hashlib.sha256(compose_path.read_bytes()).hexdigest()
        raise AssertionError(f"unexpected fixed runtime file: {path}")

    with patch.object(lifecycle.subprocess, "run", side_effect=runtime.run), \
            patch.object(lifecycle, "_api_health", return_value=api_healthy), \
            patch.object(lifecycle, "SOURCE_BACKEND", source), \
            patch.object(lifecycle, "TARGET_BACKEND", target), \
            patch.object(lifecycle, "MIHOMO_COMPOSE_FILE", compose_path), \
            patch.object(lifecycle, "MIHOMO_CACHE_DB", cache_path), \
            patch.object(lifecycle, "MIHOMO_UNIT_FILE", unit_path), \
            patch.object(lifecycle, "_trusted_fixed_file_digest", side_effect=fixed_digest), \
            patch.object(lifecycle, "_validate_state_dir", side_effect=private_test_state), \
            patch.object(lifecycle.os, "geteuid", return_value=0):
        return lifecycle.run_command(args)


def _run_runtime_transfer(tmp_path: Path, runtime: FakeMihomoRuntime, operation_id: str,
                          old_digest: str, new_digest: str, *, source_content: str) -> dict[str, object]:
    source, target = _paths(tmp_path, source_content)
    args = ["transfer", "--state-dir", str(tmp_path / "rollout-state"),
            "--expected-operation-id", operation_id,
            "--expected-current-manifest-sha256", old_digest,
            "--expected-new-manifest-sha256", new_digest]

    def private_test_state(path: Path) -> Path:
        if path.is_symlink():
            raise lifecycle.LifecycleError("state directory path contains a symlink")
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.chmod(0o700)
        return path

    compose_path = tmp_path / "docker-compose.yml"
    compose_path.write_text("services:\n  mihomo:\n    image: pinned\n", encoding="utf-8")
    compose_path.chmod(0o600)
    if runtime.cache_path is None:
        if runtime.cache_root is None:
            runtime.cache_root = Path(tempfile.mkdtemp(prefix="fwrouter-jit-cache-test-", dir="/root"))
            runtime.cache_root.chmod(0o700)
            atexit.register(shutil.rmtree, runtime.cache_root, ignore_errors=True)
        runtime.cache_path = runtime.cache_root / "var/lib/fwrouter-v2/mihomo/cache.db"
        runtime.cache_path.parent.mkdir(parents=True, exist_ok=True)
        runtime.cache_path.write_bytes(b"synthetic cache before\n")
        runtime.cache_path.chmod(0o600)
    unit_path = TOOL.parents[1] / "host/systemd/fwrouter-mihomo.service"
    import hashlib
    unit_digest = hashlib.sha256(unit_path.read_bytes()).hexdigest()
    def fixed_digest(path: Path, **_kwargs: object) -> str:
        return unit_digest if Path(path) == unit_path else hashlib.sha256(compose_path.read_bytes()).hexdigest()
    with patch.object(lifecycle.subprocess, "run", side_effect=runtime.run), \
            patch.object(lifecycle, "SOURCE_BACKEND", source), \
            patch.object(lifecycle, "TARGET_BACKEND", target), \
            patch.object(lifecycle, "MIHOMO_COMPOSE_FILE", compose_path), \
            patch.object(lifecycle, "MIHOMO_CACHE_DB", runtime.cache_path), \
            patch.object(lifecycle, "MIHOMO_UNIT_FILE", unit_path), \
            patch.object(lifecycle, "_trusted_fixed_file_digest", side_effect=fixed_digest), \
            patch.object(lifecycle, "_validate_state_dir", side_effect=private_test_state), \
            patch.object(lifecycle.os, "geteuid", return_value=0):
        return lifecycle.run_command(args)


def _digest_for_source_content(content: str) -> str:
    import hashlib

    manifest = [{"path": "module.py", "size": len(content.encode()),
                 "sha256": hashlib.sha256(content.encode()).hexdigest()}]
    return lifecycle._manifest_digest(manifest)


def test_stop_then_api_ready_resume_restores_only_previously_active_gateway(tmp_path: Path) -> None:
    gateway = "fwrouter-xray-sub-gateway.service"
    systemd = FakeSystemd({"fwrouter-api.service", gateway})

    prepared = _run(tmp_path, "stop", systemd)
    assert prepared["status"] == "stopped"
    assert gateway not in systemd.active
    assert "fwrouter-api.service" not in systemd.active

    systemd.active.add("fwrouter-api.service")
    source, target = _paths(tmp_path)
    _install_fixture(source, target)
    resumed = _run(tmp_path, "resume", systemd)
    assert resumed["status"] == "complete"
    assert gateway in systemd.active
    assert [call[2] for call in systemd.calls if call[1] == "start"] == [gateway]


def test_transfer_preserves_rollout_ownership_and_timer_gate(tmp_path: Path) -> None:
    gateway = "fwrouter-xray-sub-gateway.service"
    timer = "fwrouter-subscription-refresh.timer"
    worker = "fwrouter-docker-subject-events.service"
    systemd = FakeSystemd({"fwrouter-api.service", gateway, worker, timer})
    old_content = "SOURCE_SENTINEL = 'old'\n"
    new_content = "SOURCE_SENTINEL = 'new'\n"
    _paths(tmp_path, old_content)
    _install_fixture(*_paths(tmp_path, old_content))

    stopped = _run(tmp_path, "stop", systemd, source_content=old_content)
    old_digest = _digest_for_source_content(old_content)
    new_digest = _digest_for_source_content(new_content)
    checkpoint_path = tmp_path / "rollout-state" / "pending.json"
    before = json.loads(checkpoint_path.read_text())
    assert before["schema"] == lifecycle.SCHEMA

    transferred = _run_transfer(tmp_path, systemd, str(stopped["operation_id"]),
                                old_digest, new_digest, source_content=new_content)
    after = json.loads(checkpoint_path.read_text())
    assert transferred["status"] == "transferred"
    assert after["operation_id"] == before["operation_id"]
    assert after["active_units"] == before["active_units"]
    assert after["pending_units"] == before["pending_units"]
    assert after["created_at"] == before["created_at"]
    assert after["backend_manifest_sha256"] == new_digest
    assert after["initial_backend_manifest_sha256"] == old_digest
    assert after["source_transitions"] == [{
        "operation_id": before["operation_id"],
        "from_manifest_sha256": old_digest,
        "to_manifest_sha256": new_digest,
        "created_at": after["source_transitions"][0]["created_at"],
    }]
    assert timer not in after["pending_units"]
    assert not systemd.active.intersection(lifecycle.ALLOWED_UNITS)

    source, target = _paths(tmp_path, new_content)
    _install_fixture(source, target)
    systemd.active.add("fwrouter-api.service")
    resumed = _run(tmp_path, "resume", systemd, source_content=new_content)
    assert resumed["status"] == "pending"
    assert timer in resumed["pending_units"]
    assert timer not in systemd.active
    assert gateway in systemd.active and worker in systemd.active


def test_transfer_is_idempotent_after_checkpoint_publish_before_install(tmp_path: Path) -> None:
    systemd = FakeSystemd({"fwrouter-api.service", "fwrouter-xray-sub-gateway.service"})
    old_content = "SOURCE_SENTINEL = 'old'\n"
    new_content = "SOURCE_SENTINEL = 'new'\n"
    _paths(tmp_path, old_content)
    _install_fixture(*_paths(tmp_path, old_content))
    stopped = _run(tmp_path, "stop", systemd, source_content=old_content)
    old_digest = _digest_for_source_content(old_content)
    new_digest = _digest_for_source_content(new_content)

    first = _run_transfer(tmp_path, systemd, str(stopped["operation_id"]),
                          old_digest, new_digest, source_content=new_content)
    checkpoint_path = tmp_path / "rollout-state" / "pending.json"
    checkpoint_bytes = checkpoint_path.read_bytes()
    replay = _run_transfer(tmp_path, systemd, str(stopped["operation_id"]),
                           old_digest, new_digest, source_content=new_content)

    assert first["status"] == "transferred"
    assert replay["status"] == "already_transferred"
    assert checkpoint_path.read_bytes() == checkpoint_bytes
    assert lifecycle._manifest_digest(lifecycle._backend_manifest(_paths(tmp_path, old_content)[1])) == old_digest


def test_transfer_rejects_stale_cas_active_units_and_installed_drift(tmp_path: Path) -> None:
    systemd = FakeSystemd({"fwrouter-api.service", "fwrouter-xray-sub-gateway.service"})
    old_content = "SOURCE_SENTINEL = 'old'\n"
    new_content = "SOURCE_SENTINEL = 'new'\n"
    _paths(tmp_path, old_content)
    _install_fixture(*_paths(tmp_path, old_content))
    stopped = _run(tmp_path, "stop", systemd, source_content=old_content)
    old_digest = _digest_for_source_content(old_content)
    new_digest = _digest_for_source_content(new_content)
    checkpoint_path = tmp_path / "rollout-state" / "pending.json"
    before = checkpoint_path.read_bytes()

    with pytest.raises(lifecycle.LifecycleError, match="operation id"):
        _run_transfer(tmp_path, systemd, str(uuid.uuid4()), old_digest, new_digest,
                      source_content=new_content)
    assert checkpoint_path.read_bytes() == before

    systemd.active.add("fwrouter-docker-subject-events.service")
    with pytest.raises(lifecycle.LifecycleError, match="must be stopped"):
        _run_transfer(tmp_path, systemd, str(stopped["operation_id"]), old_digest,
                      new_digest, source_content=new_content)
    systemd.active.clear()
    assert checkpoint_path.read_bytes() == before

    target = tmp_path / "target-backend"
    (target / "module.py").write_text("unapproved installed drift\n", encoding="utf-8")
    with pytest.raises(lifecycle.LifecycleError, match="missing, changed, or stale"):
        _run_transfer(tmp_path, systemd, str(stopped["operation_id"]), old_digest,
                      new_digest, source_content=new_content)
    assert checkpoint_path.read_bytes() == before


def _prepare_old_schema2_checkpoint(tmp_path: Path, runtime: FakeMihomoRuntime,
                                    source_content: str) -> tuple[str, str, str]:
    source, target = _paths(tmp_path, source_content)
    _install_fixture(source, target)
    stopped = _run(tmp_path, "stop", runtime, source_content=source_content)
    checkpoint_path = tmp_path / "rollout-state" / "pending.json"
    checkpoint = json.loads(checkpoint_path.read_text())
    checkpoint["schema"] = 2  # exact OLD helper schema: no maintenance/runtime extension
    checkpoint.pop("maintenance_operation")
    checkpoint.pop("runtime_quiescence")
    checkpoint_path.write_text(json.dumps(checkpoint, sort_keys=True) + "\n")
    return str(stopped["operation_id"]), lifecycle._manifest_digest(lifecycle._backend_manifest(source)), \
        str(checkpoint_path)


def _mihomo_test_inputs(tmp_path: Path) -> tuple[str, str]:
    compose = tmp_path / "docker-compose.yml"
    compose.write_text("services:\n  mihomo:\n    image: pinned\n", encoding="utf-8")
    compose.chmod(0o600)
    import hashlib
    return "c" * 64, hashlib.sha256(compose.read_bytes()).hexdigest()


def test_old_lifecycle_checkpoint_adopts_exact_operation_and_quiesces_container(tmp_path: Path) -> None:
    mihomo_unit = lifecycle.MIHOMO_UNIT
    runtime = FakeMihomoRuntime({"fwrouter-api.service", mihomo_unit})
    operation_id, old_digest, checkpoint_path = _prepare_old_schema2_checkpoint(
        tmp_path, runtime, "SOURCE_SENTINEL = 'old'\n")
    compose_hash, compose_digest = _mihomo_test_inputs(tmp_path)

    result = _run_mihomo_lifecycle(tmp_path, "quiesce-mihomo", runtime, operation_id,
                                   old_digest, compose_hash, compose_digest,
                                   source_content="SOURCE_SENTINEL = 'old'\n")
    checkpoint = json.loads(Path(checkpoint_path).read_text())
    q = checkpoint["runtime_quiescence"]
    assert result["status"] == "quiesced"
    assert checkpoint["schema"] == lifecycle.SCHEMA
    assert checkpoint["operation_id"] == operation_id
    assert q["operation_id"] == operation_id
    assert q["source_manifest_sha256"] == old_digest
    assert q["phase"] == "quiesced"
    assert not runtime.container_running
    assert ("/usr/bin/docker", "stop", "--time", "30", runtime.container_id) in runtime.calls
    assert not any(call[:2] == ("/usr/bin/systemctl", "mask") for call in runtime.calls)
    assert not any(call[:2] == ("/usr/bin/systemctl", "unmask") for call in runtime.calls)
    assert not any(call[0] == "/usr/bin/docker" and call[1] == "compose"
                   for call in runtime.calls)
    assert ("/usr/bin/systemctl", "start", lifecycle.MIHOMO_UNIT) not in runtime.calls


def test_quiescence_records_only_fixed_cache_transition_from_exact_container_stop(
    tmp_path: Path,
) -> None:
    old = "SOURCE_SENTINEL = 'old'\n"
    runtime = FakeMihomoRuntime({"fwrouter-api.service", lifecycle.MIHOMO_UNIT})
    operation_id, old_digest, checkpoint_path = _prepare_old_schema2_checkpoint(
        tmp_path, runtime, old)
    compose_hash, compose_digest = _mihomo_test_inputs(tmp_path)
    runtime.cache_after_stop = b"synthetic cache after exact Docker stop\n"

    _run_mihomo_lifecycle(tmp_path, "quiesce-mihomo", runtime, operation_id, old_digest,
                          compose_hash, compose_digest, source_content=old)

    checkpoint = json.loads(Path(checkpoint_path).read_text())
    q = checkpoint["runtime_quiescence"]
    assert q["cache_before"] == {"sha256": lifecycle._sha256(b"synthetic cache before\n"),
                                 "size": len(b"synthetic cache before\n")}
    assert q["cache_after"] == {
        "sha256": lifecycle._sha256(runtime.cache_after_stop),
        "size": len(runtime.cache_after_stop),
    }
    assert q["cache_before"] != q["cache_after"]

    # Idempotent readback accepts the captured cache only while that exact
    # fixed file remains byte-for-byte at its recorded post-stop identity.
    _run_mihomo_lifecycle(tmp_path, "quiesce-mihomo", runtime, operation_id, old_digest,
                          compose_hash, compose_digest, source_content=old)
    assert json.loads(Path(checkpoint_path).read_text()) == checkpoint
    runtime.cache_path.write_bytes(b"unowned later cache mutation")
    with pytest.raises(lifecycle.LifecycleError, match="quiescence changed"):
        _run_mihomo_lifecycle(tmp_path, "quiesce-mihomo", runtime, operation_id, old_digest,
                              compose_hash, compose_digest, source_content=old)


def test_quiescence_stops_on_unattributed_cache_change_before_docker_stop(tmp_path: Path) -> None:
    old = "SOURCE_SENTINEL = 'old'\n"
    runtime = FakeMihomoRuntime({"fwrouter-api.service", lifecycle.MIHOMO_UNIT})
    operation_id, old_digest, checkpoint_path = _prepare_old_schema2_checkpoint(
        tmp_path, runtime, old)
    compose_hash, compose_digest = _mihomo_test_inputs(tmp_path)
    original = lifecycle._mihomo_cache_state
    observations = 0

    def changed_cache() -> dict[str, object]:
        nonlocal observations
        observations += 1
        if observations == 1:
            return original()
        assert runtime.cache_path is not None
        runtime.cache_path.write_bytes(b"unowned writer mutation")
        return original()

    with patch.object(lifecycle, "_mihomo_cache_state", side_effect=changed_cache):
        with pytest.raises(lifecycle.LifecycleError, match="changed after the owned stop intent"):
            _run_mihomo_lifecycle(tmp_path, "quiesce-mihomo", runtime, operation_id,
                                  old_digest, compose_hash, compose_digest,
                                  source_content=old)
    checkpoint = json.loads(Path(checkpoint_path).read_text())
    assert checkpoint["runtime_quiescence"]["phase"] == "stop_pending"
    assert runtime.container_running is True
    assert not any(call[0] == "/usr/bin/docker" and call[1] == "stop"
                   for call in runtime.calls)


def test_quiescence_retry_stops_when_cache_changes_in_crash_gap(tmp_path: Path) -> None:
    old = "SOURCE_SENTINEL = 'old'\n"
    runtime = FakeMihomoRuntime({"fwrouter-api.service", lifecycle.MIHOMO_UNIT})
    operation_id, old_digest, checkpoint_path = _prepare_old_schema2_checkpoint(
        tmp_path, runtime, old)
    compose_hash, compose_digest = _mihomo_test_inputs(tmp_path)
    runtime.cache_after_stop = b"X" * len(b"synthetic cache before\n")
    original_atomic_write = lifecycle._atomic_write

    def interrupt_after_stop(path: Path, value: dict[str, object]) -> None:
        if value.get("runtime_quiescence", {}).get("phase") == "quiesced":
            raise OSError("injected crash after Docker stop before transition receipt")
        original_atomic_write(path, value)

    with patch.object(lifecycle, "_atomic_write", side_effect=interrupt_after_stop):
        with pytest.raises(OSError, match="before transition receipt"):
            _run_mihomo_lifecycle(tmp_path, "quiesce-mihomo", runtime, operation_id,
                                  old_digest, compose_hash, compose_digest,
                                  source_content=old)
    pending = json.loads(Path(checkpoint_path).read_text())
    assert pending["runtime_quiescence"]["phase"] == "stop_pending"
    assert runtime.container_running is False
    with pytest.raises(lifecycle.LifecycleError, match="changed after the owned stop intent"):
        _run_mihomo_lifecycle(tmp_path, "quiesce-mihomo", runtime, operation_id,
                              old_digest, compose_hash, compose_digest,
                              source_content=old)
    assert json.loads(Path(checkpoint_path).read_text()) == pending


def test_quiescence_full_transfer_release_and_resume_reuses_one_operation(tmp_path: Path) -> None:
    old = "SOURCE_SENTINEL = 'old'\n"
    new = "SOURCE_SENTINEL = 'new'\n"
    runtime = FakeMihomoRuntime({"fwrouter-api.service", lifecycle.MIHOMO_UNIT,
                                 "fwrouter-xray-sub-gateway.service"})
    operation_id, old_digest, checkpoint_path = _prepare_old_schema2_checkpoint(
        tmp_path, runtime, old)
    compose_hash, compose_digest = _mihomo_test_inputs(tmp_path)
    _run_mihomo_lifecycle(tmp_path, "quiesce-mihomo", runtime, operation_id, old_digest,
                          compose_hash, compose_digest, source_content=old)
    new_digest = _digest_for_source_content(new)
    transferred = _run_runtime_transfer(tmp_path, runtime, operation_id, old_digest,
                                        new_digest, source_content=new)
    assert transferred["status"] == "transferred"
    _install_fixture(*_paths(tmp_path, new))
    assert json.loads(Path(checkpoint_path).read_text())["runtime_quiescence"]["phase"] == "quiesced"

    released = _run_mihomo_lifecycle(tmp_path, "release-mihomo", runtime, operation_id,
                                     new_digest, compose_hash, compose_digest,
                                     source_content=new)
    assert released["status"] == "released"
    assert runtime.container_running
    assert json.loads(Path(checkpoint_path).read_text())["runtime_quiescence"]["phase"] == "released"

    runtime.active.add("fwrouter-api.service")
    resumed = _run(tmp_path, "resume", runtime, source_content=new)
    assert resumed["operation_id"] == operation_id
    assert resumed["status"] == "complete"  # no persistent timers were active in this fixture
    assert runtime.container_id == "a" * 64
    assert not any(call[0] == "/usr/bin/docker" and call[1] == "compose"
                   for call in runtime.calls)


def test_transfer_rejects_same_size_cache_change_before_source_cas(tmp_path: Path) -> None:
    old, new = "SOURCE_SENTINEL = 'old'\n", "SOURCE_SENTINEL = 'new'\n"
    runtime = FakeMihomoRuntime({"fwrouter-api.service", lifecycle.MIHOMO_UNIT})
    operation_id, old_digest, checkpoint_path = _prepare_old_schema2_checkpoint(tmp_path, runtime, old)
    compose_hash, compose_digest = _mihomo_test_inputs(tmp_path)
    _run_mihomo_lifecycle(tmp_path, "quiesce-mihomo", runtime, operation_id, old_digest,
                          compose_hash, compose_digest, source_content=old)
    before = Path(checkpoint_path).read_bytes()
    assert runtime.cache_path is not None
    cache_bytes = runtime.cache_path.read_bytes()
    runtime.cache_path.write_bytes(b"X" * len(cache_bytes))
    with pytest.raises(lifecycle.LifecycleError, match="fixed Mihomo cache"):
        _run_runtime_transfer(tmp_path, runtime, operation_id, old_digest,
                              _digest_for_source_content(new), source_content=new)
    assert Path(checkpoint_path).read_bytes() == before
    assert not json.loads(Path(checkpoint_path).read_text())["source_transitions"]


def test_jit_release_releases_exact_container_before_api_start(tmp_path: Path) -> None:
    old, new = "SOURCE_SENTINEL = 'old'\n", "SOURCE_SENTINEL = 'new'\n"
    runtime = FakeMihomoRuntime({"fwrouter-api.service", lifecycle.MIHOMO_UNIT})
    operation_id, old_digest, checkpoint_path = _prepare_old_schema2_checkpoint(tmp_path, runtime, old)
    compose_hash, compose_digest = _mihomo_test_inputs(tmp_path)
    _run_mihomo_lifecycle(tmp_path, "quiesce-mihomo", runtime, operation_id, old_digest,
                          compose_hash, compose_digest, source_content=old)
    new_digest = _digest_for_source_content(new)
    _run_runtime_transfer(tmp_path, runtime, operation_id, old_digest, new_digest, source_content=new)
    _install_fixture(*_paths(tmp_path, new))
    released = _run_mihomo_lifecycle(tmp_path, "release-mihomo", runtime, operation_id,
                                     new_digest, compose_hash, compose_digest,
                                     source_content=new, jit_owner_quiescence=True,
                                     api_healthy=False)
    assert released["status"] == "released"
    assert runtime.container_running
    assert "fwrouter-api.service" not in runtime.active
    assert json.loads(Path(checkpoint_path).read_text())["runtime_quiescence"]["phase"] == "released"


def test_jit_release_refuses_active_api_before_container_start(tmp_path: Path) -> None:
    old, new = "SOURCE_SENTINEL = 'old'\n", "SOURCE_SENTINEL = 'new'\n"
    runtime = FakeMihomoRuntime({"fwrouter-api.service", lifecycle.MIHOMO_UNIT})
    operation_id, old_digest, checkpoint_path = _prepare_old_schema2_checkpoint(tmp_path, runtime, old)
    compose_hash, compose_digest = _mihomo_test_inputs(tmp_path)
    _run_mihomo_lifecycle(tmp_path, "quiesce-mihomo", runtime, operation_id, old_digest,
                          compose_hash, compose_digest, source_content=old)
    new_digest = _digest_for_source_content(new)
    _run_runtime_transfer(tmp_path, runtime, operation_id, old_digest, new_digest, source_content=new)
    _install_fixture(*_paths(tmp_path, new))
    runtime.active.add("fwrouter-api.service")
    before = Path(checkpoint_path).read_bytes()
    with pytest.raises(lifecycle.LifecycleError, match="must be stopped"):
        _run_mihomo_lifecycle(tmp_path, "release-mihomo", runtime, operation_id,
                              new_digest, compose_hash, compose_digest, source_content=new,
                              jit_owner_quiescence=True)
    assert Path(checkpoint_path).read_bytes() == before
    assert not runtime.container_running


def test_jit_release_rejects_same_size_cache_change_before_release_pending(tmp_path: Path) -> None:
    old, new = "SOURCE_SENTINEL = 'old'\n", "SOURCE_SENTINEL = 'new'\n"
    runtime = FakeMihomoRuntime({"fwrouter-api.service", lifecycle.MIHOMO_UNIT})
    operation_id, old_digest, checkpoint_path = _prepare_old_schema2_checkpoint(tmp_path, runtime, old)
    compose_hash, compose_digest = _mihomo_test_inputs(tmp_path)
    _run_mihomo_lifecycle(tmp_path, "quiesce-mihomo", runtime, operation_id, old_digest,
                          compose_hash, compose_digest, source_content=old)
    new_digest = _digest_for_source_content(new)
    _run_runtime_transfer(tmp_path, runtime, operation_id, old_digest, new_digest, source_content=new)
    _install_fixture(*_paths(tmp_path, new))
    assert runtime.cache_path is not None
    cache_bytes = runtime.cache_path.read_bytes()
    runtime.cache_path.write_bytes(b"Y" * len(cache_bytes))
    before = Path(checkpoint_path).read_bytes()
    with pytest.raises(lifecycle.LifecycleError, match="fixed Mihomo cache"):
        _run_mihomo_lifecycle(tmp_path, "release-mihomo", runtime, operation_id,
                              new_digest, compose_hash, compose_digest, source_content=new,
                              jit_owner_quiescence=True, api_healthy=False)
    assert Path(checkpoint_path).read_bytes() == before
    assert json.loads(Path(checkpoint_path).read_text())["runtime_quiescence"]["phase"] == "quiesced"
    assert not runtime.container_running


def test_quiescence_rejects_identity_and_unsupported_unit_drift(tmp_path: Path) -> None:
    old = "SOURCE_SENTINEL = 'old'\n"
    runtime = FakeMihomoRuntime({"fwrouter-api.service", lifecycle.MIHOMO_UNIT})
    operation_id, old_digest, checkpoint_path = _prepare_old_schema2_checkpoint(tmp_path, runtime, old)
    compose_hash, compose_digest = _mihomo_test_inputs(tmp_path)
    before = Path(checkpoint_path).read_bytes()
    with pytest.raises(lifecycle.LifecycleError, match="exact pending OLD lifecycle checkpoint"):
        _run_mihomo_lifecycle(tmp_path, "quiesce-mihomo", runtime, str(uuid.uuid4()), old_digest,
                              compose_hash, compose_digest, source_content=old)
    assert Path(checkpoint_path).read_bytes() == before

    runtime.drop_ins = "/etc/systemd/system/fwrouter-mihomo.service.d/override.conf"
    with pytest.raises(lifecycle.LifecycleError, match="unit contract"):
        _run_mihomo_lifecycle(tmp_path, "quiesce-mihomo", runtime, operation_id, old_digest,
                              compose_hash, compose_digest, source_content=old)
    assert Path(checkpoint_path).read_bytes() == before


def test_quiescence_retry_after_stop_and_release_retry_after_container_start(tmp_path: Path) -> None:
    old, new = "SOURCE_SENTINEL = 'old'\n", "SOURCE_SENTINEL = 'new'\n"
    runtime = FakeMihomoRuntime({"fwrouter-api.service", lifecycle.MIHOMO_UNIT})
    operation_id, old_digest, checkpoint_path = _prepare_old_schema2_checkpoint(tmp_path, runtime, old)
    compose_hash, compose_digest = _mihomo_test_inputs(tmp_path)
    q = _run_mihomo_lifecycle(tmp_path, "quiesce-mihomo", runtime, operation_id, old_digest,
                              compose_hash, compose_digest, source_content=old)
    assert q["status"] == "quiesced"
    q_replay = _run_mihomo_lifecycle(tmp_path, "quiesce-mihomo", runtime, operation_id, old_digest,
                                     compose_hash, compose_digest, source_content=old)
    assert q_replay["status"] == "quiesced"

    new_digest = _digest_for_source_content(new)
    _run_runtime_transfer(tmp_path, runtime, operation_id, old_digest, new_digest, source_content=new)
    _install_fixture(*_paths(tmp_path, new))
    original_atomic_write = lifecycle._atomic_write
    def interrupt_after_start(path: Path, value: dict[str, object]) -> None:
        if value.get("runtime_quiescence", {}).get("phase") == "released":
            raise OSError("injected crash after container start")
        original_atomic_write(path, value)
    with patch.object(lifecycle, "_atomic_write", side_effect=interrupt_after_start):
        with pytest.raises(OSError, match="injected crash"):
            _run_mihomo_lifecycle(tmp_path, "release-mihomo", runtime, operation_id, new_digest,
                                  compose_hash, compose_digest, source_content=new,
                                  jit_owner_quiescence=True, api_healthy=False)
    assert runtime.container_running
    retried = _run_mihomo_lifecycle(tmp_path, "release-mihomo", runtime, operation_id, new_digest,
                                    compose_hash, compose_digest, source_content=new,
                                    jit_owner_quiescence=True, api_healthy=False)
    assert retried["status"] == "released"
    assert json.loads(Path(checkpoint_path).read_text())["runtime_quiescence"]["phase"] == "released"


def test_jit_release_retry_after_release_pending_before_container_start(tmp_path: Path) -> None:
    old, new = "SOURCE_SENTINEL = 'old'\n", "SOURCE_SENTINEL = 'new'\n"
    runtime = FakeMihomoRuntime({"fwrouter-api.service", lifecycle.MIHOMO_UNIT})
    operation_id, old_digest, checkpoint_path = _prepare_old_schema2_checkpoint(tmp_path, runtime, old)
    compose_hash, compose_digest = _mihomo_test_inputs(tmp_path)
    _run_mihomo_lifecycle(tmp_path, "quiesce-mihomo", runtime, operation_id, old_digest,
                          compose_hash, compose_digest, source_content=old)
    new_digest = _digest_for_source_content(new)
    _run_runtime_transfer(tmp_path, runtime, operation_id, old_digest, new_digest, source_content=new)
    _install_fixture(*_paths(tmp_path, new))
    original = lifecycle._docker_action

    def interrupt_before_start(action: str, container_id: str) -> None:
        if action == "start":
            raise OSError("injected crash before pinned container start")
        original(action, container_id)

    with patch.object(lifecycle, "_docker_action", side_effect=interrupt_before_start):
        with pytest.raises(OSError, match="before pinned container start"):
            _run_mihomo_lifecycle(tmp_path, "release-mihomo", runtime, operation_id,
                                  new_digest, compose_hash, compose_digest,
                                  source_content=new, jit_owner_quiescence=True,
                                  api_healthy=False)
    assert not runtime.container_running
    assert json.loads(Path(checkpoint_path).read_text())["runtime_quiescence"]["phase"] == "release_pending"
    retried = _run_mihomo_lifecycle(tmp_path, "release-mihomo", runtime, operation_id,
                                    new_digest, compose_hash, compose_digest,
                                    source_content=new, jit_owner_quiescence=True,
                                    api_healthy=False)
    assert retried["status"] == "released"
    assert runtime.container_running
    assert json.loads(Path(checkpoint_path).read_text())["runtime_quiescence"]["phase"] == "released"


def test_release_pending_retry_rejects_cache_change_before_container_start(tmp_path: Path) -> None:
    old, new = "SOURCE_SENTINEL = 'old'\n", "SOURCE_SENTINEL = 'new'\n"
    runtime = FakeMihomoRuntime({"fwrouter-api.service", lifecycle.MIHOMO_UNIT})
    operation_id, old_digest, checkpoint_path = _prepare_old_schema2_checkpoint(tmp_path, runtime, old)
    compose_hash, compose_digest = _mihomo_test_inputs(tmp_path)
    _run_mihomo_lifecycle(tmp_path, "quiesce-mihomo", runtime, operation_id, old_digest,
                          compose_hash, compose_digest, source_content=old)
    new_digest = _digest_for_source_content(new)
    _run_runtime_transfer(tmp_path, runtime, operation_id, old_digest, new_digest, source_content=new)
    _install_fixture(*_paths(tmp_path, new))
    original = lifecycle._docker_action

    def fail_before_start(action: str, container_id: str) -> None:
        if action == "start":
            raise OSError("injected start failure")
        original(action, container_id)

    with patch.object(lifecycle, "_docker_action", side_effect=fail_before_start):
        with pytest.raises(OSError, match="start failure"):
            _run_mihomo_lifecycle(tmp_path, "release-mihomo", runtime, operation_id,
                                  new_digest, compose_hash, compose_digest, source_content=new,
                                  jit_owner_quiescence=True, api_healthy=False)
    assert not runtime.container_running
    assert json.loads(Path(checkpoint_path).read_text())["runtime_quiescence"]["phase"] == "release_pending"
    assert runtime.cache_path is not None
    cache_bytes = runtime.cache_path.read_bytes()
    runtime.cache_path.write_bytes(b"Z" * len(cache_bytes))
    starts_before = sum(call[0] == "/usr/bin/docker" and call[1] == "start"
                        for call in runtime.calls)
    before = Path(checkpoint_path).read_bytes()
    with pytest.raises(lifecycle.LifecycleError, match="pending release retry"):
        _run_mihomo_lifecycle(tmp_path, "release-mihomo", runtime, operation_id,
                              new_digest, compose_hash, compose_digest, source_content=new,
                              jit_owner_quiescence=True, api_healthy=False)
    assert Path(checkpoint_path).read_bytes() == before
    assert not runtime.container_running
    assert sum(call[0] == "/usr/bin/docker" and call[1] == "start"
               for call in runtime.calls) == starts_before


def test_quiescence_retry_after_stop_pending_checkpoint_reuses_same_container_operation(
    tmp_path: Path,
) -> None:
    old = "SOURCE_SENTINEL = 'old'\n"
    runtime = FakeMihomoRuntime({"fwrouter-api.service", lifecycle.MIHOMO_UNIT})
    operation_id, old_digest, checkpoint_path = _prepare_old_schema2_checkpoint(
        tmp_path, runtime, old)
    compose_hash, compose_digest = _mihomo_test_inputs(tmp_path)
    original = lifecycle._docker_action

    def interrupted(action: str, container_id: str) -> None:
        assert action == "stop" and container_id == runtime.container_id
        raise OSError("simulated interruption after stop_pending publication")

    with patch.object(lifecycle, "_docker_action", side_effect=interrupted):
        with pytest.raises(OSError, match="simulated interruption"):
            _run_mihomo_lifecycle(tmp_path, "quiesce-mihomo", runtime, operation_id,
                                  old_digest, compose_hash, compose_digest, source_content=old)
    pending = json.loads(Path(checkpoint_path).read_text())
    assert pending["operation_id"] == operation_id
    assert pending["runtime_quiescence"]["phase"] == "stop_pending"
    assert runtime.container_running

    with patch.object(lifecycle, "_docker_action", side_effect=original):
        result = _run_mihomo_lifecycle(tmp_path, "quiesce-mihomo", runtime, operation_id,
                                       old_digest, compose_hash, compose_digest, source_content=old)
    assert result["operation_id"] == operation_id
    assert result["status"] == "quiesced"
    checkpoint = json.loads(Path(checkpoint_path).read_text())
    assert checkpoint["runtime_quiescence"]["phase"] == "quiesced"
    assert runtime.container_running is False
    assert sum(call[0] == "/usr/bin/docker" and call[1] == "stop" for call in runtime.calls) == 1


def test_source_transfer_rejects_stop_pending_runtime_quiescence(tmp_path: Path) -> None:
    old, new = "SOURCE_SENTINEL = 'old'\n", "SOURCE_SENTINEL = 'new'\n"
    runtime = FakeMihomoRuntime({"fwrouter-api.service", lifecycle.MIHOMO_UNIT})
    operation_id, old_digest, checkpoint_path = _prepare_old_schema2_checkpoint(
        tmp_path, runtime, old)
    compose_hash, compose_digest = _mihomo_test_inputs(tmp_path)
    original = lifecycle._docker_action
    with patch.object(lifecycle, "_docker_action", side_effect=OSError("interrupted")):
        with pytest.raises(OSError, match="interrupted"):
            _run_mihomo_lifecycle(tmp_path, "quiesce-mihomo", runtime, operation_id,
                                  old_digest, compose_hash, compose_digest, source_content=old)
    checkpoint_before = Path(checkpoint_path).read_bytes()
    with pytest.raises(lifecycle.LifecycleError, match="quiescent identity"):
        _run_runtime_transfer(tmp_path, runtime, operation_id, old_digest,
                              _digest_for_source_content(new), source_content=new)
    assert Path(checkpoint_path).read_bytes() == checkpoint_before
    assert runtime.container_running is True
    assert original is lifecycle._docker_action

def test_transfer_supports_explicit_chained_source_revisions(tmp_path: Path) -> None:
    systemd = FakeSystemd({"fwrouter-api.service"})
    versions = ["SOURCE_SENTINEL = 'old'\n", "SOURCE_SENTINEL = 'next'\n",
                "SOURCE_SENTINEL = 'final'\n"]
    _paths(tmp_path, versions[0])
    _install_fixture(*_paths(tmp_path, versions[0]))
    stopped = _run(tmp_path, "stop", systemd, source_content=versions[0])
    operation_id = str(stopped["operation_id"])
    digests = [_digest_for_source_content(value) for value in versions]

    _run_transfer(tmp_path, systemd, operation_id, digests[0], digests[1],
                  source_content=versions[1])
    source, target = _paths(tmp_path, versions[1])
    _install_fixture(source, target)
    _run_transfer(tmp_path, systemd, operation_id, digests[1], digests[2],
                  source_content=versions[2])

    checkpoint = json.loads((tmp_path / "rollout-state" / "pending.json").read_text())
    assert checkpoint["operation_id"] == operation_id
    assert checkpoint["initial_backend_manifest_sha256"] == digests[0]
    assert checkpoint["backend_manifest_sha256"] == digests[2]
    assert [(item["from_manifest_sha256"], item["to_manifest_sha256"])
            for item in checkpoint["source_transitions"]] == [
                (digests[0], digests[1]), (digests[1], digests[2])]


def test_transfer_upgrades_schema_one_checkpoint_and_preserves_operation(tmp_path: Path) -> None:
    systemd = FakeSystemd({"fwrouter-api.service", "fwrouter-xray-sub-gateway.service",
                          "fwrouter-maintenance.timer"})
    old_content = "SOURCE_SENTINEL = 'old'\n"
    new_content = "SOURCE_SENTINEL = 'new'\n"
    _paths(tmp_path, old_content)
    _install_fixture(*_paths(tmp_path, old_content))
    stopped = _run(tmp_path, "stop", systemd, source_content=old_content)
    checkpoint_path = tmp_path / "rollout-state" / "pending.json"
    legacy = json.loads(checkpoint_path.read_text())
    legacy["schema"] = lifecycle.OLDEST_SCHEMA
    legacy.pop("initial_backend_manifest_sha256")
    legacy.pop("source_transitions")
    legacy.pop("maintenance_operation")
    legacy.pop("runtime_quiescence")
    checkpoint_path.write_text(json.dumps(legacy), encoding="utf-8")

    assert lifecycle._read_checkpoint(checkpoint_path)["schema"] == lifecycle.OLDEST_SCHEMA
    old_digest = _digest_for_source_content(old_content)
    new_digest = _digest_for_source_content(new_content)
    result = _run_transfer(tmp_path, systemd, str(stopped["operation_id"]),
                           old_digest, new_digest, source_content=new_content)
    upgraded = json.loads(checkpoint_path.read_text())

    assert result["status"] == "transferred"
    assert upgraded["schema"] == lifecycle.SCHEMA
    assert upgraded["operation_id"] == legacy["operation_id"]
    assert upgraded["active_units"] == legacy["active_units"]
    assert upgraded["source_transitions"][0]["from_manifest_sha256"] == old_digest
    assert "fwrouter-maintenance.timer" not in systemd.active


def test_transfer_atomic_write_failure_keeps_original_checkpoint(tmp_path: Path) -> None:
    systemd = FakeSystemd({"fwrouter-api.service"})
    old_content = "SOURCE_SENTINEL = 'old'\n"
    new_content = "SOURCE_SENTINEL = 'new'\n"
    _paths(tmp_path, old_content)
    _install_fixture(*_paths(tmp_path, old_content))
    stopped = _run(tmp_path, "stop", systemd, source_content=old_content)
    checkpoint_path = tmp_path / "rollout-state" / "pending.json"
    before = checkpoint_path.read_bytes()

    with patch.object(lifecycle.os, "replace", side_effect=OSError("injected publication interruption")):
        with pytest.raises(lifecycle.LifecycleError, match="durably write"):
            _run_transfer(tmp_path, systemd, str(stopped["operation_id"]),
                          _digest_for_source_content(old_content),
                          _digest_for_source_content(new_content), source_content=new_content)

    assert checkpoint_path.read_bytes() == before
    assert lifecycle._read_checkpoint(checkpoint_path)["backend_manifest_sha256"] == (
        _digest_for_source_content(old_content))


def test_transfer_rejects_malformed_or_overlong_source_history(tmp_path: Path) -> None:
    systemd = FakeSystemd({"fwrouter-api.service"})
    _run(tmp_path, "stop", systemd)
    checkpoint_path = tmp_path / "rollout-state" / "pending.json"
    checkpoint = json.loads(checkpoint_path.read_text())
    checkpoint["source_transitions"] = [{"unexpected": "field"}]
    checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")
    with pytest.raises(lifecycle.LifecycleError, match="transition entry"):
        lifecycle._read_checkpoint(checkpoint_path)


def test_resume_fails_closed_when_api_is_not_healthy(tmp_path: Path) -> None:
    gateway = "fwrouter-xray-sub-gateway.service"
    systemd = FakeSystemd({"fwrouter-api.service", gateway})
    _run(tmp_path, "stop", systemd)
    systemd.active.add("fwrouter-api.service")

    source, target = _paths(tmp_path)
    _install_fixture(source, target)
    with pytest.raises(lifecycle.LifecycleError, match="API health"):
        _run(tmp_path, "resume", systemd, api_healthy=False)
    assert not [call for call in systemd.calls if call[1] == "start"]


def test_optional_or_previously_stopped_gateway_is_not_started(tmp_path: Path) -> None:
    systemd = FakeSystemd({"fwrouter-api.service"})
    _run(tmp_path, "stop", systemd)
    systemd.active.add("fwrouter-api.service")

    source, target = _paths(tmp_path)
    _install_fixture(source, target)
    result = _run(tmp_path, "resume", systemd)
    assert result["status"] == "complete"
    assert "fwrouter-xray-sub-gateway.service" not in systemd.active
    assert not [call for call in systemd.calls if call[1] == "start"]


def test_interrupted_resume_keeps_checkpoint_and_retry_is_idempotent(tmp_path: Path) -> None:
    gateway = "fwrouter-xray-sub-gateway.service"
    worker = "fwrouter-docker-subject-events.service"
    systemd = FakeSystemd({"fwrouter-api.service", gateway, worker})
    _run(tmp_path, "stop", systemd)
    systemd.active.add("fwrouter-api.service")
    systemd.fail_start.add(worker)
    source, target = _paths(tmp_path)
    _install_fixture(source, target)

    with pytest.raises(lifecycle.LifecycleError, match="start"):
        _run(tmp_path, "resume", systemd)
    checkpoint = json.loads((tmp_path / "rollout-state" / "pending.json").read_text())
    assert checkpoint["status"] == "pending"

    systemd.fail_start.clear()
    result = _run(tmp_path, "resume", systemd)
    assert result["status"] == "complete"
    assert gateway in systemd.active and worker in systemd.active


def test_persistent_timer_restore_requires_explicit_catchup_acknowledgement(tmp_path: Path) -> None:
    timer = "fwrouter-subscription-refresh.timer"
    gateway = "fwrouter-xray-sub-gateway.service"
    systemd = FakeSystemd({"fwrouter-api.service", gateway, timer})
    _run(tmp_path, "stop", systemd)
    systemd.active.add("fwrouter-api.service")

    source, target = _paths(tmp_path)
    _install_fixture(source, target)
    result = _run(tmp_path, "resume", systemd)
    assert result["status"] == "pending"
    assert timer in result["pending_units"]
    assert timer not in systemd.active
    assert gateway in systemd.active

    result = _run(tmp_path, "resume", systemd, allow_timer_catchup=True)
    assert result["status"] == "complete"
    assert timer in systemd.active


def test_checkpoint_path_and_host_guard_are_not_overwritten(tmp_path: Path) -> None:
    guard = tmp_path / "host-guard"
    guard.write_text("operator readiness guard\n", encoding="utf-8")
    systemd = FakeSystemd({"fwrouter-api.service"})
    _run(tmp_path, "stop", systemd)
    assert guard.read_text(encoding="utf-8") == "operator readiness guard\n"
    checkpoint = tmp_path / "rollout-state" / "pending.json"
    assert checkpoint.is_file()
    assert checkpoint.stat().st_mode & 0o777 == 0o600


def test_source_identity_mismatch_does_not_run_systemctl(tmp_path: Path) -> None:
    gateway = "fwrouter-xray-sub-gateway.service"
    systemd = FakeSystemd({"fwrouter-api.service", gateway})
    _run(tmp_path, "stop", systemd)
    checkpoint_path = tmp_path / "rollout-state" / "pending.json"
    checkpoint = json.loads(checkpoint_path.read_text())
    checkpoint["backend_manifest_sha256"] = "0" * 64
    checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")
    calls_before = len(systemd.calls)

    with pytest.raises(lifecycle.LifecycleError, match="source identity"):
        _run(tmp_path, "resume", systemd)
    assert len(systemd.calls) == calls_before


def test_state_directory_symlink_is_rejected(tmp_path: Path) -> None:
    real = tmp_path / "real-state"
    real.mkdir(mode=0o700)
    link = tmp_path / "linked-state"
    link.symlink_to(real, target_is_directory=True)
    systemd = FakeSystemd({"fwrouter-api.service"})
    with patch.object(lifecycle.subprocess, "run", side_effect=systemd.run), \
            patch.object(lifecycle.os, "geteuid", return_value=0):
        with pytest.raises(lifecycle.LifecycleError, match="symlink"):
            lifecycle.run_command(["stop", "--state-dir", str(link)])
    assert not systemd.calls


def test_state_directory_rejects_non_root_owner(tmp_path: Path) -> None:
    candidate = tmp_path / "user-owned-state"
    candidate.mkdir(mode=0o700)
    candidate.chmod(0o700)
    original_lstat = Path.lstat

    def report_non_root_owner(path: Path) -> object:
        info = original_lstat(path)
        if path == candidate:
            return SimpleNamespace(st_mode=info.st_mode, st_uid=12345)
        return info

    with patch.object(Path, "lstat", report_non_root_owner):
        with pytest.raises(lifecycle.LifecycleError, match="root-owned"):
            lifecycle._validate_state_dir(candidate)


@pytest.mark.parametrize("payload,expected", [
    ({"ok": True, "data": {"status": "healthy", "database": {"status": "healthy"}}}, True),
    ({"ok": True, "data": {"status": "healthy", "database": {"status": "failed"}}}, False),
    ({"ok": False, "data": {"status": "healthy", "database": {"status": "healthy"}}}, False),
])
def test_api_health_requires_healthy_envelope_and_database(payload: dict[str, object],
                                                            expected: bool) -> None:
    class Response:
        status = 200

        def read(self, _limit: int) -> bytes:
            return json.dumps(payload).encode()

    class Connection:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.requested: tuple[object, ...] | None = None
            self.closed = False

        def request(self, *args: object, **_kwargs: object) -> None:
            self.requested = args

        def getresponse(self) -> Response:
            return Response()

        def close(self) -> None:
            self.closed = True

    connections: list[Connection] = []

    def make_connection(*args: object, **kwargs: object) -> Connection:
        connection = Connection(*args, **kwargs)
        connections.append(connection)
        return connection

    with patch.object(lifecycle.http.client, "HTTPConnection", side_effect=make_connection):
        assert lifecycle._api_health() is expected
    assert connections[0].requested == ("GET", "/api/v2/health")
    assert connections[0].closed


def test_incomplete_backend_export_never_restores_dependents(tmp_path: Path) -> None:
    gateway = "fwrouter-xray-sub-gateway.service"
    systemd = FakeSystemd({"fwrouter-api.service", gateway})
    _run(tmp_path, "stop", systemd)
    systemd.active.add("fwrouter-api.service")
    source, target = _paths(tmp_path)
    (target / "module.py").write_text("stale source\n", encoding="utf-8")
    calls_before = len(systemd.calls)

    with pytest.raises(lifecycle.LifecycleError, match="export"):
        _run(tmp_path, "resume", systemd)
    assert len(systemd.calls) == calls_before
    assert gateway not in systemd.active


@pytest.mark.parametrize("extra_kind", ["file", "symlink"])
def test_stale_backend_export_entry_blocks_resume_before_dependents_start(
    tmp_path: Path, extra_kind: str,
) -> None:
    gateway = "fwrouter-xray-sub-gateway.service"
    systemd = FakeSystemd({"fwrouter-api.service", gateway})
    _run(tmp_path, "stop", systemd)
    systemd.active.add("fwrouter-api.service")
    source, target = _paths(tmp_path)
    _install_fixture(source, target)
    stale = target / "stale_importable.py"
    if extra_kind == "file":
        stale.write_text("STALE_IMPORTABLE = True\n", encoding="utf-8")
    else:
        stale.symlink_to(target / "module.py")
    calls_before = len(systemd.calls)

    with pytest.raises(lifecycle.LifecycleError):
        _run(tmp_path, "resume", systemd)
    assert len(systemd.calls) == calls_before
    assert gateway not in systemd.active


def test_installer_exclusions_do_not_count_as_stale_backend_export(tmp_path: Path) -> None:
    gateway = "fwrouter-xray-sub-gateway.service"
    systemd = FakeSystemd({"fwrouter-api.service", gateway})
    _run(tmp_path, "stop", systemd)
    systemd.active.add("fwrouter-api.service")
    source, target = _paths(tmp_path)
    _install_fixture(source, target)
    (target / ".env").write_text("PRIVATE_FIXTURE=1\n", encoding="utf-8")
    (target / "runtime.db").write_bytes(b"fixture")
    cache = target / "__pycache__"
    cache.mkdir()
    (cache / "runtime.pyc").write_bytes(b"fixture")
    venv = target / ".venv"
    venv.mkdir()
    (venv / "pyvenv.cfg").write_text("fixture\n", encoding="utf-8")

    assert _run(tmp_path, "resume", systemd)["status"] == "complete"
    assert gateway in systemd.active


def test_gateway_listener_failure_leaves_checkpoint_pending_for_retry(tmp_path: Path) -> None:
    gateway = "fwrouter-xray-sub-gateway.service"
    systemd = FakeSystemd({"fwrouter-api.service", gateway})
    _run(tmp_path, "stop", systemd)
    systemd.active.add("fwrouter-api.service")
    source, target = _paths(tmp_path)
    _install_fixture(source, target)

    with patch.object(lifecycle, "_wait_gateway_listener",
                      side_effect=lifecycle.LifecycleError("Gateway listener did not become ready")):
        with pytest.raises(lifecycle.LifecycleError, match="Gateway listener"):
            _run(tmp_path, "resume", systemd)
    checkpoint = json.loads((tmp_path / "rollout-state" / "pending.json").read_text())
    assert checkpoint["status"] == "pending"
    assert gateway in systemd.active

    assert _run(tmp_path, "resume", systemd)["status"] == "complete"


def test_gateway_listener_wait_uses_only_mocked_bounded_attempts() -> None:
    with patch.object(lifecycle, "_gateway_listener_ready", side_effect=[False, True]), \
            patch.object(lifecycle.time, "monotonic", side_effect=[0.0, 1.0, 2.0]), \
            patch.object(lifecycle.time, "sleep"):
        lifecycle._wait_gateway_listener()


def test_lifecycle_instructions_never_select_the_host_component() -> None:
    readme = (Path(__file__).parents[1] / "README.md").read_text(encoding="utf-8")
    section = readme.split("# Controlled API Rollout Lifecycle", 1)[1]
    assert "--deploy --component backend" in section
    assert "--component host" not in section
    assert "trusted-clean-fwrouter-source" in section


def test_stop_rejects_installed_extra_before_checkpoint_or_systemd_stop(tmp_path: Path) -> None:
    gateway = "fwrouter-xray-sub-gateway.service"
    systemd = FakeSystemd({"fwrouter-api.service", gateway})
    source, target = _paths(tmp_path)
    _install_fixture(source, target)
    cache_file = target / ".ruff_cache" / "content" / "stale.py"
    cache_file.parent.mkdir(parents=True)
    cache_file.write_text("STALE_CACHE_ENTRY = True\n", encoding="utf-8")

    with pytest.raises(lifecycle.LifecycleError, match="extra files"):
        _run(tmp_path, "stop", systemd)
    assert not systemd.calls
    assert "fwrouter-api.service" in systemd.active
    assert gateway in systemd.active
    assert not (tmp_path / "rollout-state" / "pending.json").exists()


def test_stop_allows_existing_backend_files_with_old_content(tmp_path: Path) -> None:
    systemd = FakeSystemd({"fwrouter-api.service", "fwrouter-xray-sub-gateway.service"})
    source, target = _paths(tmp_path)
    target.mkdir()
    (target / "module.py").write_text("PREVIOUS_VERSION = True\n", encoding="utf-8")

    result = _run(tmp_path, "stop", systemd, install_target=False)
    assert result["status"] == "stopped"
    assert "fwrouter-api.service" not in systemd.active


def test_stop_never_interrupts_a_job_racing_with_timer_shutdown(tmp_path: Path) -> None:
    job = "fwrouter-subscription-refresh.service"
    systemd = FakeSystemd({"fwrouter-api.service", "fwrouter-subscription-refresh.timer"})
    systemd.start_job_on_stop = job

    with pytest.raises(lifecycle.LifecycleError, match="one-shot job became active"):
        _run(tmp_path, "stop", systemd)
    assert job in systemd.active
    assert "fwrouter-api.service" in systemd.active
    checkpoint = json.loads((tmp_path / "rollout-state" / "pending.json").read_text())
    assert checkpoint["status"] == "pending"

    systemd.active.remove(job)
    result = _run(tmp_path, "stop", systemd)
    assert result["status"] == "stopped"
    assert "fwrouter-api.service" not in systemd.active
