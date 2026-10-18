from __future__ import annotations

import importlib.util
import json
import os as os
from pathlib import Path
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

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


def _paths(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "source-backend"
    target = tmp_path / "target-backend"
    source.mkdir(exist_ok=True)
    (source / "module.py").write_text("SOURCE_SENTINEL = 'expected'\n", encoding="utf-8")
    return source, target


def _install_fixture(source: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for item in source.iterdir():
        (target / item.name).write_bytes(item.read_bytes())


def _run(tmp_path: Path, command: str, systemd: FakeSystemd, *, api_healthy: bool = True,
         allow_timer_catchup: bool = False, install_target: bool = True) -> dict[str, object]:
    args = [command, "--state-dir", str(tmp_path / "rollout-state")]
    if allow_timer_catchup:
        args.append("--allow-timer-catchup")
    source, target = _paths(tmp_path)
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
