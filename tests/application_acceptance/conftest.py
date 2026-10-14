from __future__ import annotations

import json
import hashlib
import math
import os
import signal
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
    # Mirror the normal startup source-of-truth read. Reconciliation fingerprints
    # the singleton row, so it must exist before the isolated native runtime is
    # started and before any candidate generation begins.
    from fwrouter_api.services.bootstrap import _read_persisted_global_routing_intent
    _read_persisted_global_routing_intent()


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _health_response_is_ready(status_code: int, payload: object) -> bool:
    """Accept readiness only after the ASGI lifespan and database are healthy."""
    if status_code != 200 or not isinstance(payload, dict) or payload.get("ok") is not True:
        return False
    data = payload.get("data")
    if not isinstance(data, dict) or data.get("service") != "fwrouter-api" or data.get("status") != "healthy":
        return False
    database = data.get("database")
    if not isinstance(database, dict) or database.get("status") != "healthy":
        return False
    schema = database.get("schema")
    return isinstance(schema, dict) and schema.get("ok") is True


def _startup_phase_diagnostics(log_path: Path) -> dict[str, object]:
    """Extract only bounded timing fields from the owned worker's phase markers."""
    prefix = b"FWROUTER_ACCEPTANCE_BOOTSTRAP_PHASE "
    try:
        with log_path.open("rb") as stream:
            size = stream.seek(0, os.SEEK_END)
            stream.seek(max(0, size - 16 * 1024))
            raw = stream.read(16 * 1024)
    except OSError:
        return {"status": "unavailable", "events": []}
    events: list[dict[str, object]] = []
    totals: dict[str, dict[str, float | int]] = {}
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-")
    for line in raw.splitlines():
        if not line.startswith(prefix):
            continue
        try:
            item = json.loads(line[len(prefix):].decode("ascii"))
        except (UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(item, dict):
            continue
        name, event = item.get("phase"), item.get("event")
        if (not isinstance(name, str) or not name or len(name) > 96
                or any(character not in allowed for character in name)
                or not isinstance(event, str) or event not in {"enter", "exit"}):
            continue
        row: dict[str, object] = {"phase": name, "event": event}
        call = item.get("call")
        if type(call) is int and 0 <= call <= 10_000:
            row["call"] = call
        if event == "exit":
            for source, target in (("seconds", "wall_seconds"), ("cpu_seconds", "cpu_seconds")):
                value = item.get(source)
                if type(value) in {int, float}:
                    try:
                        number = float(value)
                    except OverflowError:
                        continue
                    if math.isfinite(number) and 0 <= number <= 600:
                        row[target] = round(number, 4)
            aggregate = totals.setdefault(name, {"calls": 0, "wall_seconds": 0.0, "cpu_seconds": 0.0})
            if "wall_seconds" in row:
                aggregate["calls"] += 1
                aggregate["wall_seconds"] = round(float(aggregate["wall_seconds"]) + float(row["wall_seconds"]), 4)
            if "cpu_seconds" in row:
                aggregate["cpu_seconds"] = round(float(aggregate["cpu_seconds"]) + float(row["cpu_seconds"]), 4)
        events.append(row)
    return {"status": "parsed", "events": events[-128:], "totals": totals, "tail_bytes": len(raw)}


def _process_cpu_seconds(pid: int) -> float | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        fields = raw[raw.rfind(")") + 2:].split()
        ticks = int(fields[11]) + int(fields[12])  # utime + stime (fields 14 and 15)
        return round(ticks / os.sysconf("SC_CLK_TCK"), 4)
    except (OSError, ValueError, IndexError):
        return None


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    config.addinivalue_line("markers", "l7: actual API-worker SIGKILL/restart acceptance; release-only")
    config.addinivalue_line("markers", "browser: real pinned Chromium against real loopback app/API")
    config.addinivalue_line("markers", "packet: hosted isolated network-namespace packet dataplane acceptance")
    try:
        profile, profile_digest = load_profile()
    except (OSError, ProfileError) as exc:
        raise pytest.UsageError(f"hosted application acceptance profile refused: {exc}") from exc
    config._fwrouter_acceptance_profile = profile
    config._fwrouter_acceptance_profile_digest = profile_digest
    config._fwrouter_acceptance_started = time.monotonic()
    config._fwrouter_acceptance_reports = {}
    config._fwrouter_diagnostic_records = []
    config._fwrouter_startup_failures = []
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
    startup_failures: list[dict[str, object]] = []
    request.config._fwrouter_startup_failures = startup_failures
    provider_bridge: ProviderHttpTestBridge | None = None
    setup_complete = False
    try:
        native.start()
        packet_endpoint = profile.get("profile") == "hosted-kernel-packet"
        mihomo_probe_address = (
            profile["mihomo_handoff"]["address"]
            if profile.get("profile") == "hosted-kernel-dataplane" else None
        )
        provider_bridge = ProviderHttpTestBridge(
            packet_endpoint=packet_endpoint,
            mihomo_probe_address=mihomo_probe_address,
        ).start()

        kernel_profile = profile.get("profile") in {"hosted-kernel-dataplane", "hosted-kernel-packet"}
        worker_path = ("/opt/fwrouter-test/bin:/usr/sbin:/sbin:/usr/bin:/bin" if kernel_profile
                       else "/opt/fwrouter-test/bin:/usr/bin:/bin")

        def start_worker(*, startup_tasks: bool = False, startup_recovery: bool = False, on_spawn=None) -> tuple[subprocess.Popen[bytes], str]:
            if startup_recovery and (not startup_tasks or profile.get("profile") != "hosted-kernel-packet"):
                pytest.fail("startup recovery requires the qualified packet worker with native DNS")
            port = _free_loopback_port()
            logs = (suite_root / f"uvicorn-{port}.log").open("ab")
            env = {
                "PATH": worker_path, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TZ": "UTC",
                "PYTHONPATH": "/workspace/backend:/workspace/tests:/workspace", "PYTHONDONTWRITEBYTECODE": "1",
                "FWROUTER_STATE_DIR": str(state), "FWROUTER_ENVIRONMENT": "test",
                "FWROUTER_STARTUP_TASKS_ENABLED": "1" if startup_tasks else "0",
                "FWROUTER_STARTUP_RECOVERY_ENABLED": "1" if startup_recovery else "0",
                "FWROUTER_ACCEPTANCE_RPC_SOCKET": str(rpc_socket),
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
            if provider_bridge.mihomo_probe_url is not None:
                env["FWROUTER_ACCEPTANCE_MIHOMO_PROBE_URL"] = provider_bridge.mihomo_probe_url
                env["FWROUTER_ACCEPTANCE_MIHOMO_PROBE_PORT"] = str(provider_bridge.mihomo_probe_port)
            spawned_at = time.monotonic()
            proc = subprocess.Popen(
                [sys.executable, "-m", "application_acceptance.worker", "--socket", str(rpc_socket), "--port", str(port)],
                cwd="/workspace", env=env, stdin=subprocess.DEVNULL, stdout=logs, stderr=logs, close_fds=True,
                start_new_session=True,
            )
            owned_workers.append(proc)
            proc._acceptance_log_path = logs.name
            logs.close()
            if on_spawn is not None:
                on_spawn(proc)
            base = f"http://127.0.0.1:{port}/api/v2"
            readiness_limit = 45 if startup_tasks else 15
            deadline = spawned_at + readiness_limit
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    startup_failures.append({
                        "worker_pid": proc.pid,
                        "readiness_deadline_seconds": 45 if startup_tasks else 15,
                        "readiness_elapsed_seconds": round(time.monotonic() - spawned_at, 4),
                        "ready": False,
                        "failure": "worker_exited_before_readiness",
                        "worker_exit_at_deadline": proc.returncode,
                        "rss_kib_at_deadline": None,
                        "rss_hwm_kib_at_deadline": None,
                        "process_cpu_seconds_at_deadline": _process_cpu_seconds(proc.pid),
                        "phase_diagnostics_at_deadline": _startup_phase_diagnostics(Path(proc._acceptance_log_path)),
                        "termination": {"status": "already_exited", "return_code": proc.returncode},
                    })
                    pytest.fail("owned API worker exited before readiness")
                try:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    code, payload = http_json(base + "/health", timeout=min(0.5, remaining))
                    if _health_response_is_ready(code, payload) and time.monotonic() < deadline:
                        proc._acceptance_readiness_probe = {
                            "path": "/health",
                            "status_code": code,
                            "healthy": True,
                            "elapsed_seconds": round(time.monotonic() - spawned_at, 4),
                        }
                        return proc, base
                except (OSError, ValueError, TimeoutError):
                    threading.Event().wait(0.1)
            # Record the actual readiness deadline before diagnostics and termination add time.
            readiness_elapsed = time.monotonic() - spawned_at
            failure: dict[str, object] = {
                "worker_pid": proc.pid,
                "readiness_deadline_seconds": readiness_limit,
                "readiness_elapsed_seconds": round(readiness_elapsed, 4),
                "ready": False,
                "worker_exit_at_deadline": proc.poll(),
                "rss_kib_at_deadline": None,
                "rss_hwm_kib_at_deadline": None,
                "process_cpu_seconds_at_deadline": _process_cpu_seconds(proc.pid),
                "termination": {"status": "NOT_RUN"},
            }
            try:
                for field, key in (("VmRSS", "rss_kib_at_deadline"), ("VmHWM", "rss_hwm_kib_at_deadline")):
                    for status_line in Path(f"/proc/{proc.pid}/status").read_text(encoding="ascii").splitlines():
                        if status_line.startswith(field + ":"):
                            failure[key] = int(status_line.split()[1])
                            break
            except (OSError, ValueError, IndexError):
                pass
            log_path = Path(proc._acceptance_log_path)
            failure["phase_diagnostics_at_deadline"] = _startup_phase_diagnostics(log_path)
            # The worker registered a traceback-only handler; no locals/config are dumped.
            diagnostic_started = time.monotonic()
            diagnostic_status = "sent"
            try:
                if proc.poll() is None:
                    os.kill(proc.pid, signal.SIGUSR1)
                    threading.Event().wait(0.2)
                else:
                    diagnostic_status = "worker_exited_before_signal"
            except ProcessLookupError:
                diagnostic_status = "worker_exited_before_signal"
            proc._acceptance_stop_requested = True
            termination_started = time.monotonic()
            if proc.poll() is None:
                proc.terminate()
            try:
                return_code = proc.wait(timeout=3)
                termination_status = "reaped"
            except subprocess.TimeoutExpired:
                return_code = None
                termination_status = "pending_fixture_cleanup"
            failure["diagnostic_status"] = diagnostic_status
            failure["diagnostic_elapsed_seconds"] = round(termination_started - diagnostic_started, 4)
            failure["termination"] = {
                "status": termination_status,
                "elapsed_seconds": round(time.monotonic() - termination_started, 4),
                "return_code": return_code,
            }
            failure["phase_diagnostics_after_stop"] = _startup_phase_diagnostics(log_path)
            startup_failures.append(failure)
            pytest.fail("owned API worker did not become ready within the qualified startup deadline")

        worker, api = start_worker()
        stack = {
            "root": suite_root, "state": state, "native": native, "profile": profile,
            "profile_sha256": profile_digest, "api": api, "worker": worker,
            "start_worker": start_worker, "receipt_tests": [], "provider_bridge": provider_bridge,
            "startup_failures": startup_failures,
            "mihomo_probe_url": provider_bridge.mihomo_probe_url,
        }
        setup_complete = True
        yield stack
    finally:
        cleanup_errors = []
        native.release_reload()
        for owned_worker in owned_workers:
            cleanup_started = time.monotonic()
            cleanup_action = "already_exited"
            cleanup_exit_code = owned_worker.poll()
            if cleanup_exit_code is None:
                try:
                    if not getattr(owned_worker, "_acceptance_stop_requested", False):
                        owned_worker._acceptance_stop_requested = True
                        owned_worker.terminate()
                        cleanup_action = "sigterm"
                    else:
                        cleanup_action = "wait_after_prior_sigterm"
                    owned_worker.wait(timeout=16)
                except subprocess.TimeoutExpired:
                    cleanup_errors.append("application worker exceeded bounded graceful teardown")
                    owned_worker.kill()
                    cleanup_action = "sigkill_after_grace_timeout"
                    try:
                        owned_worker.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        cleanup_errors.append("uvicorn worker could not be reaped")
                cleanup_exit_code = owned_worker.poll()
            cleanup_outcome = {
                "status": "reaped" if cleanup_exit_code is not None else "not_reaped",
                "action": cleanup_action,
                "elapsed_seconds": round(time.monotonic() - cleanup_started, 4),
                "return_code": cleanup_exit_code,
            }
            log_name = getattr(owned_worker, "_acceptance_log_path", None)
            if log_name:
                try:
                    with Path(log_name).open("rb") as stream:
                        size = stream.seek(0, os.SEEK_END)
                        stream.seek(max(0, size - 4096))
                        cleanup_outcome["application_cleanup_observed"] = (
                            b"FWROUTER_ACCEPTANCE_LIFESPAN_SHUTDOWN_COMPLETE" in stream.read(4096)
                        )
                except OSError:
                    cleanup_outcome["application_cleanup_observed"] = False
            for failure in startup_failures:
                if failure.get("worker_pid") == owned_worker.pid:
                    failure["fixture_cleanup"] = cleanup_outcome
        # Snapshot bounded diagnostics before the isolated per-test tree is removed.
        worker_logs = []
        for log_path in sorted(suite_root.glob("uvicorn-*.log")):
            try:
                with log_path.open("rb") as stream:
                    size = stream.seek(0, os.SEEK_END)
                    stream.seek(max(0, size - 16 * 1024))
                    raw = stream.read(16 * 1024)
                if b"owned application worker survived lifespan cleanup" in raw:
                    cleanup_errors.append("application lifespan left an owned background worker alive")
                from .native_runner import _redact_diagnostic
                worker_logs.append({"name": log_path.name, "tail": _redact_diagnostic(raw, limit=16 * 1024)})
            except OSError:
                worker_logs.append({"name": log_path.name, "tail": "[unreadable]"})
        native_diagnostics = native.diagnostic_snapshot()
        candidate_graph_path = state / "run" / "mihomo-candidate-graph.json"
        if candidate_graph_path.is_file() and not candidate_graph_path.is_symlink():
            try:
                raw_graph = candidate_graph_path.read_bytes()
                if len(raw_graph) <= 12 * 1024:
                    graph_payload = json.loads(raw_graph.decode("utf-8"))
                    if (isinstance(graph_payload, dict)
                            and graph_payload.get("schema") == "fwrouter-acceptance-mihomo-candidate-graph/v1"
                            and graph_payload.get("event") == "native_candidate_validation_failed"):
                        native_diagnostics["candidate_graph_diagnostic"] = graph_payload
            except (OSError, UnicodeDecodeError, ValueError):
                native_diagnostics["candidate_graph_diagnostic_status"] = "invalid_or_unreadable"
        elif any("FWROUTER_ACCEPTANCE_MIHOMO_CANDIDATE_GRAPH_WRITE_FAILED" in str(row.get("tail") or "")
                 for row in worker_logs):
            native_diagnostics["candidate_graph_diagnostic_status"] = "writer_failed"
        state_summary = []
        for item in sorted(state.rglob("*")):
            if item.is_symlink() or not item.is_file():
                continue
            try:
                relative = item.relative_to(state).as_posix()
                detail = {"path": relative, "bytes": item.stat().st_size}
                if relative.startswith(("xray/", "generated/mihomo/")) and item.stat().st_size <= 4 * 1024 * 1024:
                    detail["sha256"] = hashlib.sha256(item.read_bytes()).hexdigest()
                state_summary.append(detail)
            except OSError:
                continue
            if len(state_summary) >= 256:
                break
        phases = request.config._fwrouter_acceptance_reports.get(request.node.nodeid, {})
        failed = not setup_complete or any(value == "failed" for value in phases.values()) or bool(cleanup_errors)
        if failed or not request.config._fwrouter_diagnostic_records:
            request.config._fwrouter_diagnostic_records.append({
                "nodeid": request.node.nodeid, "status": "failed" if failed else "passed",
                "worker_logs": worker_logs[:8], "native": native_diagnostics,
                "state": state_summary, "cleanup_errors": list(cleanup_errors),
                "startup_failures": list(startup_failures[-4:]),
            })
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
    diagnostic_records = getattr(config, "_fwrouter_diagnostic_records", [])
    from .native_runner import DIAGNOSTIC_TOTAL_LIMIT
    diagnostic_payloads = {
        "native-process-diagnostics.json": {"schema": "fwrouter-native-process-diagnostics/v1",
                                            "records": [{"nodeid": row["nodeid"], "status": row["status"],
                                                         "native": row["native"]} for row in diagnostic_records]},
        "worker-service-logs.json": {"schema": "fwrouter-worker-service-logs/v1",
                                     "records": [{"nodeid": row["nodeid"], "status": row["status"],
                                                  "logs": row["worker_logs"],
                                                  "startup_failures": row.get("startup_failures", [])}
                                                 for row in diagnostic_records]},
        "state-snapshot.json": {"schema": "fwrouter-acceptance-state-summary/v1",
                                 "records": [{"nodeid": row["nodeid"], "status": row["status"],
                                              "files": row["state"]} for row in diagnostic_records]},
    }
    for filename, payload in diagnostic_payloads.items():
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        aggregate_limit = 1024 * 1024
        while len(encoded) > aggregate_limit and payload["records"]:
            payload["records"].pop(0)
            encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        (receipt_path.parent / filename).write_bytes(encoded + b"\n")
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
    packet_profile = profile.get("profile") == "hosted-kernel-packet"
    kernel_dataplane = profile.get("profile") in {"hosted-kernel-dataplane", "hosted-kernel-packet"}
    receipt = {
        "schema": "fwrouter-application-acceptance-receipt/v2",
        "scope": "hosted-kernel-packet" if packet_profile else "hosted-kernel-dataplane" if kernel_dataplane else "hosted-native-process",
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
        "limitations": (["container-kernel dataplane is not the Debian host dataplane",
                         "no external client packet path or provider traffic is claimed"] if kernel_dataplane else
                        ["process-backed Xray transport is not stock Docker runtime parity",
                         "no host dataplane or provider traffic is claimed"]),
    }
    temp_path = receipt_path.with_name(receipt_path.name + ".tmp")
    temp_path.write_text(json.dumps(receipt, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    temp_path.chmod(0o600)
    os.replace(temp_path, receipt_path)
