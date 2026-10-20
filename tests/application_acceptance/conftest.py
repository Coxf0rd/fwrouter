from __future__ import annotations

import json
import hashlib
import logging
import math
import os
import signal
import resource
import shutil
import socket
import subprocess
import sys
import stat
import tempfile
import threading
import time
from pathlib import Path

import pytest

from .native_runner import NativeXrayProcess
from .profile import ProfileError, load_profile
from .http_support import http_json
from .joined_support import ProviderHttpTestBridge


_CRITICAL_SUCCESS_DIAGNOSTIC_NODEIDS = frozenset({
    "tests/application_acceptance/test_core_provider_mihomo.py::"
    "test_confirmed_provider_failure_applies_emergency_direct_and_failed_reentry_stays_direct",
})

MINIMAL_WORKER_STARTUP_DEADLINE_SECONDS = 15.0
QUALIFIED_STARTUP_DEADLINE_SECONDS = 60.0
SLOW_STARTUP_WARNING_SECONDS = 45.0


def _retain_acceptance_diagnostic(nodeid: str, *, failed: bool, has_records: bool) -> bool:
    """Retain all failures, the first success, and one exact critical recovery success."""
    return bool(failed or nodeid in _CRITICAL_SUCCESS_DIAGNOSTIC_NODEIDS or not has_records)


def _owned_dir(path: Path) -> bool:
    try:
        info = path.lstat()
        return path.is_dir() and not path.is_symlink() and info.st_uid == os.getuid() and not info.st_mode & 0o022
    except OSError:
        return False


def _read_acceptance_xray_checkpoint_lifecycle(state: Path) -> dict[str, object]:
    """Read the packet worker's fixed, bounded, closed-schema checkpoint trace."""
    empty: dict[str, object] = {"status": "missing", "events": []}
    path = state / "run" / "xray-checkpoint-lifecycle.jsonl"
    phases = {"prepared", "transition_applied", "xray_applied", "runtime_applied",
              "inventory_synced", "bindings_written", "projections_cleaned", "selection_verified",
              "snapshots_published", "restore_started", "restore_failed", "unknown", "absent"}
    owners = {"subscription_refresh", "acceptance_inventory_publication", "startup_profile_reconcile",
              "provider_recovery", "bootstrap", "api", "watchdog", "other"}
    events = {"checkpoint_create_failed", "checkpoint_created", "publication_exception",
              "publication_finalized", "restore_result", "reconcile_exception", "reconcile_result"}
    stages = {"unknown", "checkpoint_prepare", "native_candidate_validation", "mihomo_transition_apply",
              "xray_generation_apply", "mihomo_final_apply", "inventory_synced", "bindings_written",
              "projections_cleaned", "selection_verification", "generation_publication",
              "xray_runtime_readback", "mihomo_udp_policy_receipt", "inventory_generation_promotion",
              "generation_recovery", "generation_verification_deferred", "generation_publication_revalidation"}
    errors = {"unknown", "XRAY_GENERATION_CHECKPOINT_FAILED", "XRAY_GENERATION_TRANSITION_APPLY_FAILED",
              "XRAY_GENERATION_APPLY_FAILED", "XRAY_GENERATION_FINAL_APPLY_FAILED",
              "XRAY_GENERATION_FINAL_READBACK_FAILED", "XRAY_GENERATION_RUNTIME_READBACK_FAILED",
              "XRAY_GENERATION_STALE_BEFORE_PUBLICATION", "XRAY_GENERATION_RECOVERY_FAILED",
              "SUBSCRIPTION_INVENTORY_GENERATION_UNVERIFIED",
              "SUBSCRIPTION_INVENTORY_GENERATION_PROMOTION_UNVERIFIED", "MIHOMO_UDP_POLICY_RECEIPT_UNCONFIRMED"}
    statuses = {"unknown", "success", "failed", "pending", "partial"}
    outcomes = {"unknown", "current_projection_unproven", "current_projection_verified", "no_generation_checkpoint",
                "restore_failed", "restored_last_good", "stale_checkpoint_declined"}
    reasons = {"unknown", "selection_fence_changed", "runtime_incarnation_changed",
               "mihomo_transition_digest_unavailable", "active_mihomo_generation_changed",
               "active_mihomo_unavailable", "mihomo_preimage_unavailable", "mihomo_preimage_invalid",
               "mihomo_preimage_policy_unproven", "mihomo_preimage_native_invalid", "xray_preimage_unavailable",
               "xray_preimage_invalid", "xray_runtime_ownership_changed", "xray_runtime_identity_changed",
               "xray_runtime_readback_unavailable", "xray_preimage_api_unsafe", "xray_preimage_bindings_unavailable",
               "xray_preimage_native_invalid", "subscription_snapshot_changed", "generation_source_changed",
               "generation_projection_changed", "selection_provenance_changed", "selection_runtime_unconfirmed",
               "runtime_ownership_changed", "runtime_readback_unavailable", "inventory_generation_not_bound",
               "checkpoint_phase_not_recoverable", "recovery_attempt_mismatch", "recovery_attempt_phase_unknown",
               "inventory_checkpoint_binding_mismatch", "inventory_generation_unverified",
               "supersession_current_candidate_unowned", "committed_projection_mismatch", "bindings_artifact_mismatch",
               "mounted_config_mismatch", "checkpoint_selection_intent_missing", "handoff_listener_unready",
               "api_listener_unsafe", "config_identity_mismatch", "native_candidate_invalid",
               "supersession_candidate_rebuild_mismatch", "recovery_backup_mismatch", "recovery_backup_missing",
               "ambiguous_prior_prepare", "ambiguous_prior_reload", "inventory_mihomo_checkpoint_mismatch",
               "inventory_native_receipt_unconfirmed", "inventory_generation_promotion_unconfirmed",
               "preapply_revalidation_failed", "xray_reload_failed", "runtime_revalidation_failed",
               "loaded_identity_mismatch", "runtime_projection_readback_failed", "auto_target_unverified",
               "preterminal_revalidation_failed", "terminal_revalidation_failed", "supersession_preimage_unproven"}
    try:
        directory = path.parent.lstat()
        if (not stat.S_ISDIR(directory.st_mode) or stat.S_ISLNK(directory.st_mode)
                or directory.st_uid != os.getuid() or directory.st_mode & 0o022):
            return {"status": "rejected", "events": []}
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                               | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return empty
    except OSError:
        return {"status": "rejected", "events": []}
    try:
        opened_directory = os.fstat(directory_fd)
        if ((opened_directory.st_dev, opened_directory.st_ino) != (directory.st_dev, directory.st_ino)
                or not stat.S_ISDIR(opened_directory.st_mode)):
            return {"status": "rejected", "events": []}
        try:
            path_info = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            return empty
        if (not stat.S_ISREG(path_info.st_mode) or path_info.st_uid != os.getuid()
                or path_info.st_mode & 0o077 or path_info.st_size < 0 or path_info.st_size > 48 * 1025):
            return {"status": "rejected", "events": []}
        descriptor = os.open(path.name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory_fd)
        try:
            info = os.fstat(descriptor)
            if ((info.st_dev, info.st_ino) != (path_info.st_dev, path_info.st_ino)
                    or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077 or info.st_size > 48 * 1025):
                return {"status": "rejected", "events": []}
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                raw = stream.read(48 * 1025 + 1)
        finally:
            os.close(descriptor)
        if len(raw) > 48 * 1025 or (raw and not raw.endswith(b"\n")):
            return {"status": "invalid", "events": []}
        parsed = [json.loads(line) for line in raw.splitlines()]
        if len(parsed) > 48:
            return {"status": "invalid", "events": []}
        safe: list[dict[str, object]] = []
        for index, item in enumerate(parsed, 1):
            if (not isinstance(item, dict) or item.get("schema") != "fwrouter-acceptance-xray-checkpoint/v1"
                    or type(item.get("sequence")) is not int or item["sequence"] != index
                    or item.get("event") not in events or item.get("owner") not in owners):
                return {"status": "invalid", "events": []}
            event = item["event"]
            base = {"schema", "sequence", "event", "owner"}
            expected = {
                "checkpoint_create_failed": {"phase"},
                "checkpoint_created": {"phase", "checkpoint_exists", "checkpoint_phase", "inventory_admission_present"},
                "publication_exception": {"checkpoint_before", "checkpoint_after"},
                "publication_finalized": {"checkpoint_before", "checkpoint_after", "ok", "status", "stage", "error_code"},
                "restore_result": {"checkpoint_before", "checkpoint_after", "ok", "status", "stage", "error_code", "recovered", "reason"},
                "reconcile_exception": {"checkpoint_before", "checkpoint_after", "exception"},
                "reconcile_result": {"checkpoint_before", "checkpoint_after", "ok", "status", "stage", "error_code"},
            }[event]
            if set(item) != base | expected:
                return {"status": "invalid", "events": []}
            row = {key: item[key] for key in ("schema", "sequence", "event", "owner")}
            for key in ("phase", "checkpoint_phase"):
                if key in item:
                    if item[key] not in phases:
                        return {"status": "invalid", "events": []}
                    row[key] = item[key]
            for key in ("checkpoint_exists", "inventory_admission_present", "ok"):
                if key in item:
                    if type(item[key]) is not bool:
                        return {"status": "invalid", "events": []}
                    row[key] = item[key]
            for key, allowed in (("status", statuses), ("stage", stages), ("error_code", errors),
                                 ("recovered", outcomes), ("reason", reasons),
                                 ("exception", {"known", "other"})):
                if key in item:
                    if item[key] not in allowed:
                        return {"status": "invalid", "events": []}
                    row[key] = item[key]
            for key in ("checkpoint_before", "checkpoint_after"):
                if key in item:
                    facts = item[key]
                    if (not isinstance(facts, dict) or set(facts) != {"exists", "phase"}
                            or type(facts["exists"]) is not bool or facts["phase"] not in phases):
                        return {"status": "invalid", "events": []}
                    row[key] = {"exists": facts["exists"], "phase": facts["phase"]}
            safe.append(row)
        return {"status": "captured", "events": safe}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        return {"status": "invalid", "events": []}
    finally:
        os.close(directory_fd)


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


def _bootstrap_observation_state(status_code: int, payload: object, *, worker_pid: int) -> str:
    """Classify the owned worker's completed startup-recovery observation."""
    if status_code != 200 or not isinstance(payload, dict):
        return "pending"
    if (payload.get("schema") != "fwrouter-runtime-bootstrap-observation/v1"
            or type(payload.get("worker_pid")) is not int or payload.get("worker_pid") != worker_pid):
        return "failed"
    if payload.get("completed") is not True:
        return "pending"
    if payload.get("foundation_ok") is not True or payload.get("recovery_enabled") is not True:
        return "failed"

    def has_explicit_failure(value: object, depth: int = 0) -> bool:
        if depth > 8:
            return True
        if isinstance(value, dict):
            if (value.get("ok") is False or value.get("status") in {"failed", "blocked", "error"}
                    or value.get("error_code") is not None):
                return True
            return any(has_explicit_failure(item, depth + 1) for item in value.values())
        if isinstance(value, list):
            return any(has_explicit_failure(item, depth + 1) for item in value[:128])
        return False

    required_summaries = {
        "live_recovery": {"live_routing", "selector"},
        "apply_reconcile": {
            "intended_routing", "scoped_subject_routing", "xray_subscription_profiles", "dnsmasq_reconcile",
        },
    }
    for key, required in required_summaries.items():
        summary = payload.get(key)
        if (not isinstance(summary, dict) or not required.issubset(summary)
                or any(not isinstance(summary[field], dict) or not summary[field] for field in required)
                or has_explicit_failure(summary)):
            return "failed"
    return "ready"


def _startup_attempt_state(
    *, elapsed_seconds: float, deadline_seconds: float, worker_alive: bool,
    health_ready: bool, bootstrap_state: str,
) -> str:
    """Apply the inclusive startup deadline and the actual readiness requirements."""
    if (type(elapsed_seconds) not in {int, float} or not math.isfinite(elapsed_seconds)
            or elapsed_seconds < 0 or type(deadline_seconds) not in {int, float}
            or not math.isfinite(deadline_seconds) or deadline_seconds <= 0):
        return "invalid_timing"
    if not worker_alive:
        return "worker_exited"
    if elapsed_seconds > deadline_seconds:
        return "deadline_exceeded"
    if not health_ready:
        return "health_unready"
    if bootstrap_state == "failed":
        return "bootstrap_failed"
    if bootstrap_state == "pending":
        return "bootstrap_pending"
    if bootstrap_state not in {"not_required", "ready"}:
        return "bootstrap_invalid"
    return "ready"


def _slow_startup_warning_due(elapsed_seconds: float) -> bool:
    return (type(elapsed_seconds) in {int, float} and math.isfinite(elapsed_seconds)
            and elapsed_seconds > SLOW_STARTUP_WARNING_SECONDS)


def _owned_worker_resource_evidence(pid: int) -> dict[str, object]:
    """Return bounded scalar resource evidence for the owned API process."""
    result: dict[str, object] = {
        "scope": "owned_api_worker", "process_cpu_seconds": _process_cpu_seconds(pid),
        "rss_kib": None, "hwm_kib": None, "threads": None,
    }
    try:
        lines = Path(f"/proc/{pid}/status").read_text(encoding="ascii")[:16_384].splitlines()
    except OSError:
        return result
    fields = {"VmRSS": "rss_kib", "VmHWM": "hwm_kib", "Threads": "threads"}
    for line in lines:
        key, separator, value = line.partition(":")
        if separator and key in fields:
            tokens = value.split()
            if tokens and tokens[0].isascii() and tokens[0].isdigit():
                result[fields[key]] = int(tokens[0])
    return result


def _slow_startup_warning(proc: subprocess.Popen[bytes], elapsed_seconds: float, qualification: str) -> dict[str, object]:
    record = {
        "code": "SLOW_STARTUP", "threshold_seconds": SLOW_STARTUP_WARNING_SECONDS,
        "elapsed_seconds": elapsed_seconds, "qualification": qualification,
        "worker_pid": proc.pid, "resources": _owned_worker_resource_evidence(proc.pid),
    }
    proc._acceptance_startup_warning = record
    logging.getLogger("fwrouter.acceptance.startup").warning(
        "SLOW_STARTUP %s", json.dumps(record, sort_keys=True),
    )
    return record


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
    config._fwrouter_startup_observations = []
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
            readiness_limit = (
                QUALIFIED_STARTUP_DEADLINE_SECONDS if startup_tasks
                else MINIMAL_WORKER_STARTUP_DEADLINE_SECONDS
            )
            qualification = (
                "qualified_startup_recovery" if startup_recovery
                else "qualified_startup_tasks" if startup_tasks
                else "minimal_worker"
            )
            deadline = spawned_at + readiness_limit
            warning_emitted = False
            bootstrap_state = "not_required" if not startup_recovery else "pending"
            last_health_ready = False
            last_bootstrap_observation: dict[str, object] | None = None
            failure_reason = "readiness_deadline_exceeded"
            while True:
                loop_now = time.monotonic()
                elapsed = loop_now - spawned_at
                if proc.poll() is not None:
                    if _slow_startup_warning_due(elapsed) and not warning_emitted:
                        _slow_startup_warning(proc, elapsed, qualification)
                        warning_emitted = True
                    startup_failures.append({
                        "worker_pid": proc.pid,
                        "readiness_deadline_seconds": readiness_limit,
                        "readiness_elapsed_seconds": elapsed,
                        "startup_qualification": qualification,
                        "ready": False,
                        "failure": "worker_exited_before_readiness",
                        "worker_exit_at_deadline": proc.returncode,
                        "resources_at_failure": _owned_worker_resource_evidence(proc.pid),
                        "slow_startup_warning": getattr(proc, "_acceptance_startup_warning", None),
                        "phase_diagnostics_at_deadline": _startup_phase_diagnostics(Path(proc._acceptance_log_path)),
                        "termination": {"status": "already_exited", "return_code": proc.returncode},
                    })
                    pytest.fail("owned API worker exited before readiness")
                remaining = deadline - loop_now
                if remaining <= 0:
                    break
                try:
                    code, payload = http_json(base + "/health", timeout=min(0.5, remaining))
                    last_health_ready = _health_response_is_ready(code, payload)
                    if last_health_ready and startup_recovery:
                        remaining = deadline - time.monotonic()
                        if remaining > 0:
                            bootstrap_code, bootstrap_payload = http_json(
                                base + "/__acceptance/bootstrap/report", timeout=min(0.5, remaining),
                            )
                            last_bootstrap_observation = (
                                bootstrap_payload if isinstance(bootstrap_payload, dict) else None
                            )
                            bootstrap_state = _bootstrap_observation_state(
                                bootstrap_code, bootstrap_payload, worker_pid=proc.pid,
                            )
                            if bootstrap_state == "failed":
                                failure_reason = "startup_recovery_failed"
                    completed_at = time.monotonic()
                    completed_elapsed = completed_at - spawned_at
                    startup_state = _startup_attempt_state(
                        elapsed_seconds=completed_elapsed,
                        deadline_seconds=readiness_limit,
                        worker_alive=proc.poll() is None,
                        health_ready=last_health_ready,
                        bootstrap_state=bootstrap_state,
                    )
                    if startup_state == "ready":
                        elapsed_seconds = completed_at - spawned_at
                        if _slow_startup_warning_due(elapsed_seconds) and not warning_emitted:
                            _slow_startup_warning(proc, elapsed_seconds, qualification)
                            warning_emitted = True
                        proc._acceptance_readiness_probe = {
                            "path": "/health",
                            "status_code": code,
                            "healthy": True,
                            "elapsed_seconds": elapsed_seconds,
                            "qualification": qualification,
                            "bootstrap_verified": startup_recovery,
                        }
                        if warning_emitted:
                            proc._acceptance_readiness_probe["slow_startup_warning"] = getattr(
                                proc, "_acceptance_startup_warning", None,
                            )
                        if startup_tasks:
                            request.config._fwrouter_startup_observations.append({
                                "worker_pid": proc.pid,
                                "qualification": qualification,
                                "elapsed_seconds": elapsed_seconds,
                                "deadline_seconds": readiness_limit,
                                "slow_startup_warning": getattr(proc, "_acceptance_startup_warning", None),
                            })
                        return proc, base
                    if startup_state == "worker_exited":
                        failure_reason = "worker_exited_before_readiness"
                        break
                    if startup_state == "bootstrap_failed":
                        failure_reason = "startup_recovery_failed"
                        break
                    if startup_state == "invalid_timing":
                        failure_reason = "invalid_readiness_timing"
                        break
                except (OSError, ValueError, TimeoutError):
                    pass
                now = time.monotonic()
                if now >= deadline:
                    break
                threading.Event().wait(min(0.1, max(0.0, deadline - now)))
            # Record the actual readiness deadline before diagnostics and termination add time.
            readiness_elapsed = time.monotonic() - spawned_at
            if _slow_startup_warning_due(readiness_elapsed) and not warning_emitted:
                _slow_startup_warning(proc, readiness_elapsed, qualification)
                warning_emitted = True
            resource_evidence = _owned_worker_resource_evidence(proc.pid)
            failure: dict[str, object] = {
                "worker_pid": proc.pid,
                "readiness_deadline_seconds": readiness_limit,
                "readiness_elapsed_seconds": readiness_elapsed,
                "startup_qualification": qualification,
                "readiness_deadline_reached": readiness_elapsed >= readiness_limit,
                "failure": failure_reason if failure_reason != "readiness_deadline_exceeded" else (
                    "startup_recovery_not_completed"
                    if startup_recovery and last_health_ready and bootstrap_state != "ready"
                    else "readiness_deadline_exceeded"
                ),
                "ready": False,
                "worker_exit_at_deadline": proc.poll(),
                "rss_kib_at_deadline": resource_evidence.get("rss_kib"),
                "rss_hwm_kib_at_deadline": resource_evidence.get("hwm_kib"),
                "process_cpu_seconds_at_deadline": _process_cpu_seconds(proc.pid),
                "resources_at_failure": resource_evidence,
                "bootstrap_observation": last_bootstrap_observation,
                "slow_startup_warning": getattr(proc, "_acceptance_startup_warning", None),
                "termination": {"status": "NOT_RUN"},
            }
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
        native_diagnostics["xray_checkpoint_lifecycle"] = _read_acceptance_xray_checkpoint_lifecycle(state)
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
        if _retain_acceptance_diagnostic(
            request.node.nodeid,
            failed=failed,
            has_records=bool(request.config._fwrouter_diagnostic_records),
        ):
            request.config._fwrouter_diagnostic_records.append({
                "nodeid": request.node.nodeid, "status": "failed" if failed else "passed",
                "worker_logs": worker_logs[:8], "native": native_diagnostics,
                "state": state_summary, "cleanup_errors": list(cleanup_errors),
                "startup_failures": list(startup_failures[-4:]),
                "startup_observations": list(request.config._fwrouter_startup_observations[-4:]),
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
    diagnostic_payloads = {
        "native-process-diagnostics.json": {"schema": "fwrouter-native-process-diagnostics/v1",
                                            "records": [{"nodeid": row["nodeid"], "status": row["status"],
                                                         "native": row["native"]} for row in diagnostic_records]},
        "worker-service-logs.json": {"schema": "fwrouter-worker-service-logs/v1",
                                     "records": [{"nodeid": row["nodeid"], "status": row["status"],
                                                  "logs": row["worker_logs"],
                                                  "startup_failures": row.get("startup_failures", []),
                                                  "startup_observations": row.get("startup_observations", [])}
                                                 for row in diagnostic_records]},
        "startup-observations.json": {
            "schema": "fwrouter-qualified-startup-observations/v1",
            "observations": getattr(config, "_fwrouter_startup_observations", [])[-128:],
        },
        "state-snapshot.json": {"schema": "fwrouter-acceptance-state-summary/v1",
                                 "records": [{"nodeid": row["nodeid"], "status": row["status"],
                                              "files": row["state"]} for row in diagnostic_records]},
    }
    for filename, payload in diagnostic_payloads.items():
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        aggregate_limit = 1024 * 1024
        bounded_rows_key = "observations" if filename == "startup-observations.json" else "records"
        while len(encoded) > aggregate_limit and payload[bounded_rows_key]:
            payload[bounded_rows_key].pop(0)
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
