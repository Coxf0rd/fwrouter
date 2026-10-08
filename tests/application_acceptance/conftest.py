from __future__ import annotations

import json
import os
import resource
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from .native_runner import NativeXrayProcess
from .profile import ProfileError, load_profile
from .http_support import http_json
from .joined_support import ProviderHttpTestBridge


def _owned_dir(path: Path) -> bool:
    try:
        info = path.lstat()
        return path.is_dir() and not path.is_symlink() and info.st_uid == os.getuid() and not info.st_mode & 0o022
    except OSError:
        return False


def _prepare_db(state: Path) -> None:
    os.environ["FWROUTER_STATE_DIR"] = str(state)
    os.environ["FWROUTER_ENVIRONMENT"] = "test"
    os.environ["FWROUTER_STARTUP_TASKS_ENABLED"] = "0"
    from fwrouter_api.core.config import Settings, get_settings
    Settings.model_config["env_file"] = None
    get_settings.cache_clear()
    from fwrouter_api.db.connection import db_session, initialize_database
    initialize_database()
    with db_session() as connection:
        connection.execute(
            """INSERT OR REPLACE INTO modules
               (module_name, desired_state, lifecycle_mode, runtime_state, apply_state, status_text)
               VALUES ('xray', 'enabled', 'managed', 'running', 'clean', 'Acceptance-owned Xray process')"""
        )
        # Mihomo's host dataplane is deliberately absent in this profile. The
        # Xray API test permits blocked egress explicitly; it does not assert
        # traffic availability or a verified proxy path.
        connection.execute(
            """INSERT OR REPLACE INTO modules
               (module_name, desired_state, lifecycle_mode, runtime_state, apply_state, status_text)
               VALUES ('vpn', 'disabled', 'managed', 'stopped', 'clean', 'Acceptance scope has no host dataplane')"""
        )


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    config.addinivalue_line("markers", "l7: actual API-worker SIGKILL/restart acceptance; release-only")
    config.addinivalue_line("markers", "browser: real pinned Chromium against real loopback app/API")
    try:
        profile, profile_digest = load_profile()
    except (OSError, ProfileError) as exc:
        raise pytest.UsageError(f"hosted application acceptance profile refused: {exc}") from exc
    config._fwrouter_acceptance_profile = profile
    config._fwrouter_acceptance_profile_digest = profile_digest
    config._fwrouter_acceptance_started = time.monotonic()
    config._fwrouter_acceptance_reports = {}
    config._fwrouter_acceptance_cleanup_errors = []
    config._fwrouter_acceptance_temp_bytes = 0


@pytest.fixture(scope="function")
def acceptance_stack(request):
    profile = request.config._fwrouter_acceptance_profile
    profile_digest = request.config._fwrouter_acceptance_profile_digest
    parent_root = Path(os.environ.get("FWROUTER_APPLICATION_ACCEPTANCE_ROOT", ""))
    if not parent_root.is_absolute() or parent_root != Path("/tmp/fwrouter-application-acceptance"):
        pytest.fail("acceptance root must be the orchestrator's fixed /tmp directory")
    parent_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not _owned_dir(parent_root):
        pytest.fail("acceptance root is not an owned, non-writable-by-others directory")
    suite_root = Path(tempfile.mkdtemp(prefix=f"suite-{profile['suite_nonce'][:8]}-", dir=parent_root))
    suite_root.chmod(0o700)
    request.config._fwrouter_acceptance_root = suite_root
    state = suite_root / "state"
    state.mkdir(mode=0o700)
    config_path = state / "xray" / "config.json"
    config_path.parent.mkdir(mode=0o700)
    fixture = Path(__file__).with_name("fixtures") / "xray.initial.json"
    shutil.copyfile(fixture, config_path)
    compose_path = config_path.parent / "acceptance-compose.yml"
    compose_path.write_text("# existence marker for RealXrayAdapter health projection\n", encoding="utf-8")
    (state / "run").mkdir(mode=0o700)
    (state / "generated").mkdir(mode=0o700)
    mihomo_config_path = state / "generated" / "mihomo" / "config.yaml"
    mihomo_config_path.parent.mkdir(mode=0o700)
    mihomo_fixture = Path(__file__).with_name("fixtures") / "mihomo.initial.yaml"
    shutil.copyfile(mihomo_fixture, mihomo_config_path)
    _prepare_db(state)

    rpc_socket = state / "run" / "xray.sock"
    if len(str(rpc_socket).encode()) >= 100:
        pytest.fail("acceptance Unix socket path exceeds the Linux sockaddr limit")
    native = NativeXrayProcess(
        Path(profile["xray"]["path"]), Path(profile["mihomo"]["path"]), suite_root,
        config_path, mihomo_config_path, rpc_socket,
    )
    worker: subprocess.Popen[bytes] | None = None
    owned_workers: list[subprocess.Popen[bytes]] = []
    provider_bridge: ProviderHttpTestBridge | None = None
    try:
        native.start()
        provider_bridge = ProviderHttpTestBridge().start()

        def start_worker() -> tuple[subprocess.Popen[bytes], str]:
            port = _free_loopback_port()
            logs = (suite_root / f"uvicorn-{port}.log").open("ab")
            env = {
                "PATH": "/opt/fwrouter-test/bin:/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TZ": "UTC",
                "PYTHONPATH": "/workspace/backend:/workspace/tests:/workspace", "PYTHONDONTWRITEBYTECODE": "1",
                "FWROUTER_STATE_DIR": str(state), "FWROUTER_ENVIRONMENT": "test",
                "FWROUTER_STARTUP_TASKS_ENABLED": "0", "FWROUTER_ACCEPTANCE_RPC_SOCKET": str(rpc_socket),
                "FWROUTER_ACCEPTANCE_PROFILE": "/run/fwrouter-acceptance/profile.json",
                "FWROUTER_ACCEPTANCE_PROVIDER_BASE_URL": provider_bridge.base_url,
                "FWROUTER_APPLICATION_ACCEPTANCE_ROOT": str(parent_root),
                "FWROUTER_ACCEPTANCE_RECEIPT_PATH": "/tmp/fwrouter-receipts/application-acceptance.json",
                "FWROUTER_XRAY_BINARY": profile["xray"]["path"],
                "FWROUTER_MIHOMO_BINARY": profile["mihomo"]["path"],
                "FWROUTER_BROWSER_EXECUTABLE": profile["chromium"]["path"],
                "FWROUTER_CHROMIUM_BINARY": profile["chromium"]["path"],
                "HOME": str(suite_root), "TMPDIR": str(suite_root),
            }
            proc = subprocess.Popen(
                [sys.executable, "-m", "application_acceptance.worker", "--socket", str(rpc_socket), "--port", str(port)],
                cwd="/workspace", env=env, stdin=subprocess.DEVNULL, stdout=logs, stderr=logs, close_fds=True,
                start_new_session=True,
            )
            owned_workers.append(proc)
            logs.close()
            base = f"http://127.0.0.1:{port}/api/v2"
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    pytest.fail(f"owned API worker exited before readiness: {suite_root / f'uvicorn-{port}.log'}")
                try:
                    code, _ = http_json(base + "/xray", timeout=0.5)
                    if code == 200:
                        return proc, base
                except (OSError, ValueError, TimeoutError):
                    threading.Event().wait(0.1)
            proc.terminate()
            proc.wait(timeout=3)
            pytest.fail("owned API worker did not become ready within 15 seconds")

        worker, api = start_worker()
        stack = {
            "root": suite_root, "state": state, "native": native, "profile": profile,
            "profile_sha256": profile_digest, "api": api, "worker": worker,
            "start_worker": start_worker, "receipt_tests": [], "provider_bridge": provider_bridge,
        }
        yield stack
    finally:
        cleanup_errors = []
        native.release_reload()
        for owned_worker in owned_workers:
            if owned_worker.poll() is None:
                try:
                    owned_worker.terminate()
                    owned_worker.wait(timeout=4)
                except subprocess.TimeoutExpired:
                    owned_worker.kill()
                    try:
                        owned_worker.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        cleanup_errors.append("uvicorn worker could not be reaped")
        native.stop()
        if provider_bridge is not None:
            try:
                provider_bridge.close()
            except Exception:
                cleanup_errors.append("provider HTTP bridge did not stop")
        if native.process is not None and native.process.poll() is None:
            cleanup_errors.append("Xray process could not be reaped")
        if native.mihomo_process is not None and native.mihomo_process.poll() is None:
            cleanup_errors.append("Mihomo process could not be reaped")
        if native._thread is not None and native._thread.is_alive():
            cleanup_errors.append("native RPC thread did not stop")
        if native.live_rpc_threads:
            cleanup_errors.append("native RPC connection threads did not stop")
        if rpc_socket.exists() or rpc_socket.is_symlink():
            cleanup_errors.append("native RPC socket remains after teardown")
        request.config._fwrouter_acceptance_cleanup_errors.extend(cleanup_errors)
        request.config._fwrouter_acceptance_temp_bytes += _tree_bytes(suite_root)
        if cleanup_errors:
            pytest.fail("acceptance teardown failed: " + "; ".join(cleanup_errors))
        if suite_root.exists():
            shutil.rmtree(suite_root)


def _tree_bytes(root: Path) -> int:
    total = 0
    if not root.exists():
        return 0
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink():
            total += path.stat().st_size
    return total


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    records = item.config._fwrouter_acceptance_reports.setdefault(item.nodeid, {})
    records[report.when] = "passed" if report.passed else "skipped" if report.skipped else "failed"


def pytest_sessionfinish(session, exitstatus):
    config = session.config
    profile = getattr(config, "_fwrouter_acceptance_profile", None)
    if profile is None:
        return
    receipt_path = Path(os.environ.get("FWROUTER_ACCEPTANCE_RECEIPT_PATH", ""))
    if receipt_path != Path("/tmp/fwrouter-receipts/application-acceptance.json"):
        return
    receipt_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tests = []
    for item in session.items:
        phases = config._fwrouter_acceptance_reports.get(item.nodeid, {})
        statuses = list(phases.values())
        status = "passed" if statuses and all(value == "passed" for value in statuses) and phases.get("call") == "passed" else "failed"
        tests.append({"nodeid": item.nodeid, "status": status, "phases": phases})
    usage_self = resource.getrusage(resource.RUSAGE_SELF)
    usage_children = resource.getrusage(resource.RUSAGE_CHILDREN)
    cleanup_errors = getattr(config, "_fwrouter_acceptance_cleanup_errors", [])
    all_passed = bool(tests) and all(item["status"] == "passed" for item in tests) and not cleanup_errors and int(exitstatus) == 0
    receipt = {
        "schema": "fwrouter-application-acceptance-receipt/v2",
        "scope": "hosted-native-process",
        "source_revision": profile["source_revision"],
        "plan_digest": profile["plan_digest"],
        "profile_sha256": config._fwrouter_acceptance_profile_digest,
        "suite_nonce": profile["suite_nonce"],
        "status": "passed" if all_passed else "failed",
        "exit_status": int(exitstatus),
        "tests": tests,
        "cleanup_errors": cleanup_errors,
        "resources": {
            "wall_seconds": round(time.monotonic() - config._fwrouter_acceptance_started, 3),
            "cpu_user_seconds": round(usage_self.ru_utime + usage_children.ru_utime, 3),
            "cpu_system_seconds": round(usage_self.ru_stime + usage_children.ru_stime, 3),
            "max_rss_kib": max(int(usage_self.ru_maxrss), int(usage_children.ru_maxrss)),
            "max_rss_scope": "maximum_process_high_water_mark_not_aggregate_memory",
            "owned_temp_bytes_before_cleanup": getattr(config, "_fwrouter_acceptance_temp_bytes", None),
        },
        "playwright_python": profile["playwright_python"],
        "chromium_sha256": profile["chromium"]["sha256"],
        "chromium_bundle_sha256": profile["chromium"]["bundle_sha256"],
        "xray_version": profile["xray"]["version"],
        "xray_sha256": profile["xray"]["sha256"],
        "mihomo_version": profile["mihomo"]["version"],
        "mihomo_sha256": profile["mihomo"]["sha256"],
        "limitations": ["process-backed Xray transport is not stock Docker runtime parity", "no host dataplane or provider traffic is claimed"],
    }
    temp_path = receipt_path.with_name(receipt_path.name + ".tmp")
    temp_path.write_text(json.dumps(receipt, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    temp_path.chmod(0o600)
    os.replace(temp_path, receipt_path)
