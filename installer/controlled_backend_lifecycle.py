#!/usr/bin/env python3
"""Safely stop and resume the allowlisted API dependents during a source rollout."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path, PurePosixPath
import socket
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any
import uuid


SCHEMA = 2
LEGACY_SCHEMA = 1
MAX_SOURCE_TRANSITIONS = 16
DEFAULT_STATE_DIR = Path("/var/lib/fwrouter-v2/state/controlled-rollout")
SOURCE_BACKEND = Path(__file__).resolve().parents[1] / "backend"
TARGET_BACKEND = Path("/opt/fwrouter-api")
API_UNIT = "fwrouter-api.service"
GATEWAY_UNIT = "fwrouter-xray-sub-gateway.service"
GATEWAY_ADDRESS = ("172.18.0.1", 5055)
HEALTH_TIMEOUT_SECONDS = 8
HEALTH_MAX_BYTES = 64 * 1024
GATEWAY_TIMEOUT_SECONDS = 10
MAX_MANIFEST_FILES = 10000
MAX_MANIFEST_BYTES = 512 * 1024 * 1024
MAX_CHECKPOINT_BYTES = 2 * 1024 * 1024

LONG_RUNNING_DEPENDENTS = (
    GATEWAY_UNIT,
    "fwrouter-docker-subject-events.service",
)
PERSISTENT_TIMERS = (
    "fwrouter-jobs-retention-dry-run.timer",
    "fwrouter-maintenance.timer",
    "fwrouter-subscription-refresh.timer",
    "fwrouter-traffic-collect.timer",
)
# These one-shot jobs are deliberately not interrupted or replayed by this helper.
TRANSIENT_JOBS = (
    "fwrouter-jobs-retention-dry-run.service",
    "fwrouter-maintenance.service",
    "fwrouter-subscription-refresh.service",
    "fwrouter-traffic-collect.service",
)
ALLOWED_UNITS = frozenset((API_UNIT, *LONG_RUNNING_DEPENDENTS, *PERSISTENT_TIMERS, *TRANSIENT_JOBS))
RESTORE_ORDER = (*LONG_RUNNING_DEPENDENTS, *PERSISTENT_TIMERS)
STOP_ORDER = (*reversed(PERSISTENT_TIMERS), *reversed(LONG_RUNNING_DEPENDENTS))
CHECKPOINT_KEYS = {
    "schema", "operation_id", "status", "created_at", "backend_manifest",
    "backend_manifest_sha256", "active_units", "pending_units",
    "initial_backend_manifest_sha256", "source_transitions",
}
LEGACY_CHECKPOINT_KEYS = CHECKPOINT_KEYS - {
    "initial_backend_manifest_sha256", "source_transitions",
}


class LifecycleError(RuntimeError):
    pass


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _excluded(path: PurePosixPath) -> bool:
    return any(
        part in {".env", ".venv", ".pytest_cache", "__pycache__", ".git"}
        or part.startswith(".env.")
        or part.endswith((".pyc", ".pyo", ".db", ".sqlite", ".sqlite3", ".bak", ".tar.zst"))
        or ".bak-" in part
        for part in path.parts
    )


def _file_entry(path: Path, relative: str) -> dict[str, Any]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb", closefd=True) as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_MANIFEST_BYTES:
                raise LifecycleError(f"backend file is not regular or exceeds the bound: {relative}")
            digest = hashlib.sha256()
            size = 0
            while chunk := stream.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_MANIFEST_BYTES:
                    raise LifecycleError(f"backend file exceeds the bound: {relative}")
                digest.update(chunk)
            after = os.fstat(stream.fileno())
            if (size != before.st_size or before.st_size != after.st_size
                    or before.st_mtime_ns != after.st_mtime_ns):
                raise LifecycleError(f"backend file changed while hashing: {relative}")
    except LifecycleError:
        raise
    except OSError as exc:
        raise LifecycleError(f"cannot hash backend file: {relative}") from exc
    return {"path": relative, "size": size, "sha256": digest.hexdigest()}


def _backend_manifest(root: Path) -> list[dict[str, Any]]:
    if root.is_symlink() or not root.is_dir():
        raise LifecycleError("backend source/export must be a regular directory")
    entries: list[dict[str, Any]] = []
    total_bytes = 0
    for directory, dirs, files in os.walk(root, followlinks=False):
        base = Path(directory)
        relative_dir = PurePosixPath(base.relative_to(root).as_posix())
        kept_dirs: list[str] = []
        for name in dirs:
            relative = relative_dir / name
            if _excluded(relative):
                continue
            if (base / name).is_symlink():
                raise LifecycleError(f"backend source contains a symlink directory: {relative}")
            kept_dirs.append(name)
        dirs[:] = sorted(kept_dirs)
        for name in sorted(files):
            relative = relative_dir / name
            if _excluded(relative):
                continue
            entry = _file_entry(base / name, relative.as_posix())
            total_bytes += entry["size"]
            if len(entries) >= MAX_MANIFEST_FILES or total_bytes > MAX_MANIFEST_BYTES:
                raise LifecycleError("backend manifest exceeds the configured bound")
            entries.append(entry)
    entries.sort(key=lambda entry: entry["path"])
    if not entries:
        raise LifecycleError("backend manifest is empty")
    return entries


def _backend_paths(root: Path) -> set[str]:
    if root.is_symlink() or not root.is_dir():
        raise LifecycleError("installed backend export must be a regular directory")
    paths: set[str] = set()
    total_bytes = 0
    for directory, dirs, files in os.walk(root, followlinks=False):
        base = Path(directory)
        relative_dir = PurePosixPath(base.relative_to(root).as_posix())
        kept_dirs: list[str] = []
        for name in dirs:
            relative = relative_dir / name
            if _excluded(relative):
                continue
            if (base / name).is_symlink():
                raise LifecycleError("installed backend export contains an extra symlink")
            kept_dirs.append(name)
        dirs[:] = sorted(kept_dirs)
        for name in files:
            relative = relative_dir / name
            if _excluded(relative):
                continue
            try:
                info = (base / name).lstat()
            except OSError as exc:
                raise LifecycleError("cannot inspect installed backend export") from exc
            if not stat.S_ISREG(info.st_mode):
                raise LifecycleError("installed backend export contains an extra symlink or special file")
            paths.add(relative.as_posix())
            total_bytes += info.st_size
            if len(paths) > MAX_MANIFEST_FILES or total_bytes > MAX_MANIFEST_BYTES:
                raise LifecycleError("installed backend path inventory exceeds the configured bound")
    return paths


def _preflight_export(source_manifest: list[dict[str, Any]]) -> None:
    expected = {entry["path"] for entry in source_manifest}
    extras = _backend_paths(TARGET_BACKEND) - expected
    if extras:
        raise LifecycleError("installed backend export contains extra files; stop was not started")


def _manifest_digest(entries: list[dict[str, Any]]) -> str:
    return _sha256(_canonical_json(entries))


def _validate_manifest(entries: Any) -> list[dict[str, Any]]:
    if not isinstance(entries, list) or not entries or len(entries) > MAX_MANIFEST_FILES:
        raise LifecycleError("checkpoint backend manifest is invalid")
    checked: list[dict[str, Any]] = []
    previous = ""
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"path", "size", "sha256"}:
            raise LifecycleError("checkpoint backend manifest entry is invalid")
        name, size, digest = entry["path"], entry["size"], entry["sha256"]
        if not isinstance(name, str):
            raise LifecycleError("checkpoint backend path is invalid")
        safe = PurePosixPath(name)
        if (not name or safe.is_absolute() or safe.as_posix() != name
                or any(part in {"", ".", ".."} for part in safe.parts)
                or _excluded(safe) or name <= previous):
            raise LifecycleError("checkpoint backend path is unsafe or unsorted")
        if (type(size) is not int or size < 0 or size > MAX_MANIFEST_BYTES
                or not isinstance(digest, str) or len(digest) != 64
                or any(ch not in "0123456789abcdef" for ch in digest)):
            raise LifecycleError("checkpoint backend file facts are invalid")
        previous = name
        checked.append({"path": name, "size": size, "sha256": digest})
    if sum(entry["size"] for entry in checked) > MAX_MANIFEST_BYTES:
        raise LifecycleError("checkpoint backend manifest exceeds the configured bound")
    return checked


def _verify_export(expected: list[dict[str, Any]]) -> None:
    actual = _backend_manifest(TARGET_BACKEND)
    if actual != expected:
        raise LifecycleError("installed backend export has missing, changed, or stale files")


def _validate_state_dir(path: Path) -> Path:
    if not path.is_absolute():
        raise LifecycleError("state directory must be absolute")
    cursor = Path(path.anchor)
    for part in path.parts[1:]:
        cursor = cursor / part
        try:
            info = cursor.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            raise LifecycleError("state directory path contains a symlink")
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = path.lstat()
    except OSError as exc:
        raise LifecycleError("cannot create private rollout state directory") from exc
    if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o700):
        raise LifecycleError("state directory must be root-owned with mode 0700")
    return path


def _locked(state_dir: Path):
    path = state_dir / ".lock"
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != state_dir.stat().st_uid:
            os.close(fd)
            raise LifecycleError("rollout lock must be a root-owned regular file")
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except BlockingIOError as exc:
        raise LifecycleError("another rollout lifecycle operation holds the lock") from exc
    except OSError as exc:
        raise LifecycleError("cannot acquire rollout lifecycle lock") from exc


def _atomic_write(path: Path, value: dict[str, Any]) -> None:
    data = _canonical_json(value) + b"\n"
    if len(data) > MAX_CHECKPOINT_BYTES:
        raise LifecycleError("rollout checkpoint exceeds the configured bound")
    fd = -1
    tmp_name = ""
    try:
        fd, tmp_name = tempfile.mkstemp(prefix=".pending.", dir=path.parent)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb", closefd=True) as stream:
            fd = -1
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_name, path)
        tmp_name = ""
        dir_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError as exc:
        raise LifecycleError("cannot durably write rollout checkpoint") from exc
    finally:
        if fd >= 0:
            os.close(fd)
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass


def _read_checkpoint(path: Path) -> dict[str, Any]:
    try:
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != path.parent.stat().st_uid
                or stat.S_IMODE(info.st_mode) != 0o600):
            raise LifecycleError("rollout checkpoint must be root-owned mode 0600 regular file")
        if info.st_size > MAX_CHECKPOINT_BYTES:
            raise LifecycleError("rollout checkpoint exceeds the configured bound")
        value = json.loads(path.read_text(encoding="utf-8"))
    except LifecycleError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise LifecycleError("cannot read rollout checkpoint") from exc
    if not isinstance(value, dict):
        raise LifecycleError("rollout checkpoint schema or keys are invalid")
    schema = value.get("schema")
    if (type(schema) is not int
            or (schema == LEGACY_SCHEMA and set(value) != LEGACY_CHECKPOINT_KEYS)
            or (schema == SCHEMA and set(value) != CHECKPOINT_KEYS)
            or schema not in {LEGACY_SCHEMA, SCHEMA}):
        raise LifecycleError("rollout checkpoint schema or keys are invalid")
    if value.get("status") not in {"pending", "complete"}:
        raise LifecycleError("rollout checkpoint status is invalid")
    try:
        uuid.UUID(value["operation_id"])
    except (ValueError, TypeError, AttributeError) as exc:
        raise LifecycleError("rollout checkpoint operation id is invalid") from exc
    manifest = _validate_manifest(value["backend_manifest"])
    if value["backend_manifest_sha256"] != _manifest_digest(manifest):
        raise LifecycleError("checkpoint source identity does not match its backend manifest")
    if schema == SCHEMA:
        initial = value["initial_backend_manifest_sha256"]
        transitions = value["source_transitions"]
        if (not isinstance(initial, str) or len(initial) != 64
                or any(ch not in "0123456789abcdef" for ch in initial)
                or not isinstance(transitions, list)
                or len(transitions) > MAX_SOURCE_TRANSITIONS):
            raise LifecycleError("rollout source transition history is invalid")
        previous = initial
        for item in transitions:
            if not isinstance(item, dict) or set(item) != {
                    "operation_id", "from_manifest_sha256", "to_manifest_sha256", "created_at"}:
                raise LifecycleError("rollout source transition entry is invalid")
            try:
                transition_operation = str(uuid.UUID(item["operation_id"]))
            except (ValueError, TypeError, AttributeError) as exc:
                raise LifecycleError("rollout source transition operation id is invalid") from exc
            from_digest = item["from_manifest_sha256"]
            to_digest = item["to_manifest_sha256"]
            timestamp = item["created_at"]
            if (transition_operation != value["operation_id"]
                    or from_digest != previous
                    or not isinstance(to_digest, str) or len(to_digest) != 64
                    or any(ch not in "0123456789abcdef" for ch in to_digest)
                    or to_digest == from_digest
                    or not isinstance(timestamp, str) or not timestamp):
                raise LifecycleError("rollout source transition history is inconsistent")
            previous = to_digest
        if previous != value["backend_manifest_sha256"]:
            raise LifecycleError("rollout source transition history does not bind current source")
    units = value["active_units"]
    pending = value["pending_units"]
    expected_units = {*RESTORE_ORDER, *TRANSIENT_JOBS, API_UNIT}
    if (not isinstance(units, dict) or set(units) != expected_units
            or any(type(active) is not bool for active in units.values())
            or not isinstance(pending, list)
            or any(unit not in ALLOWED_UNITS for unit in pending)
            or len(pending) != len(set(pending))):
        raise LifecycleError("rollout checkpoint unit state is invalid")
    return value


def _checkpoint_as_current_schema(checkpoint: dict[str, Any]) -> dict[str, Any]:
    """Upgrade an in-memory legacy checkpoint without changing its ownership."""
    if checkpoint["schema"] == SCHEMA:
        return checkpoint
    digest = checkpoint["backend_manifest_sha256"]
    return {
        **checkpoint,
        "schema": SCHEMA,
        "initial_backend_manifest_sha256": digest,
        "source_transitions": [],
    }


def _require_units_stopped() -> None:
    units = (*RESTORE_ORDER, *TRANSIENT_JOBS, API_UNIT)
    active = [unit for unit in units if _is_active(unit)]
    if active:
        raise LifecycleError("all rollout-owned API, dependent, timer, and one-shot units must be stopped")


def _transfer(args: argparse.Namespace, state_dir: Path) -> dict[str, Any]:
    checkpoint_path = state_dir / "pending.json"
    checkpoint = _read_checkpoint(checkpoint_path)
    if checkpoint["status"] != "pending":
        raise LifecycleError("only a pending rollout checkpoint can transfer source ownership")
    try:
        expected_operation_id = str(uuid.UUID(args.expected_operation_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise LifecycleError("expected rollout operation id is invalid") from exc
    expected_old_digest = args.expected_current_manifest_sha256
    expected_new_digest = args.expected_new_manifest_sha256
    for value in (expected_old_digest, expected_new_digest):
        if (not isinstance(value, str) or len(value) != 64
                or any(ch not in "0123456789abcdef" for ch in value)):
            raise LifecycleError("expected source manifest digest is invalid")
    if checkpoint["operation_id"] != expected_operation_id:
        raise LifecycleError("rollout operation id changed")

    current_source = _backend_manifest(SOURCE_BACKEND)
    current_source_digest = _manifest_digest(current_source)
    if current_source_digest != expected_new_digest:
        raise LifecycleError("trusted source export does not match expected new manifest")

    checkpoint = _checkpoint_as_current_schema(checkpoint)
    current_checkpoint_digest = checkpoint["backend_manifest_sha256"]
    transitions = checkpoint["source_transitions"]

    # Atomic publication can complete immediately before an interruption. Permit
    # only an exact replay of the final CAS, with the installed tree still at
    # either side of that transition and all rollout-owned units stopped.
    if current_checkpoint_digest == expected_new_digest:
        if (not transitions
                or transitions[-1]["from_manifest_sha256"] != expected_old_digest
                or transitions[-1]["to_manifest_sha256"] != expected_new_digest
                or transitions[-1]["operation_id"] != expected_operation_id):
            raise LifecycleError("checkpoint is already bound to a different source transition")
        _require_units_stopped()
        installed = _backend_manifest(TARGET_BACKEND)
        installed_digest = _manifest_digest(installed)
        if installed_digest not in {expected_old_digest, expected_new_digest}:
            raise LifecycleError("installed backend does not match either side of the recorded source transition")
        return {"status": "already_transferred", "operation_id": expected_operation_id,
                "backend_manifest_sha256": expected_new_digest}

    if current_checkpoint_digest != expected_old_digest:
        raise LifecycleError("checkpoint source identity changed")
    if len(transitions) >= MAX_SOURCE_TRANSITIONS:
        raise LifecycleError("rollout source transition history reached its bound")

    _require_units_stopped()
    # The old installed tree is the only admissible pre-install state. In
    # particular, a hand-edited/stale/partially overlaid backend cannot transfer.
    _verify_export(checkpoint["backend_manifest"])
    # Recheck after the bounded tree hash: the lifecycle flock serializes this
    # helper, while this second observation catches a unit started by an
    # unrelated systemd/operator action during hashing.
    _require_units_stopped()

    checkpoint["backend_manifest"] = current_source
    checkpoint["backend_manifest_sha256"] = current_source_digest
    checkpoint["source_transitions"].append({
        "operation_id": expected_operation_id,
        "from_manifest_sha256": expected_old_digest,
        "to_manifest_sha256": current_source_digest,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })
    _atomic_write(checkpoint_path, checkpoint)
    return {"status": "transferred", "operation_id": expected_operation_id,
            "backend_manifest_sha256": current_source_digest,
            "source_transition_count": len(checkpoint["source_transitions"])}


def _systemctl(action: str, unit: str) -> None:
    if unit not in ALLOWED_UNITS:
        raise LifecycleError("refusing non-allowlisted systemd unit")
    try:
        result = subprocess.run(["systemctl", action, unit], check=False,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise LifecycleError(f"systemctl {action} failed for {unit}") from exc
    if result.returncode != 0:
        raise LifecycleError(f"systemctl {action} failed for {unit}")


def _is_active(unit: str) -> bool:
    if unit not in ALLOWED_UNITS:
        raise LifecycleError("refusing non-allowlisted systemd unit")
    try:
        result = subprocess.run(["systemctl", "is-active", unit], check=False,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        raise LifecycleError(f"cannot inspect systemd state for {unit}") from exc
    state = result.stdout.strip()
    if result.returncode == 0 and state == "active":
        return True
    if result.returncode in {3, 4} and state in {"inactive", "failed", "unknown", ""}:
        return False
    raise LifecycleError(f"cannot inspect systemd state for {unit}")


def _api_health() -> bool:
    connection = http.client.HTTPConnection("127.0.0.1", 5000, timeout=HEALTH_TIMEOUT_SECONDS)
    try:
        connection.request("GET", "/api/v2/health", headers={"Accept": "application/json"})
        response = connection.getresponse()
        if response.status != 200:
            return False
        raw = response.read(HEALTH_MAX_BYTES + 1)
    except (OSError, http.client.HTTPException, TimeoutError):
        return False
    finally:
        connection.close()
    if len(raw) > HEALTH_MAX_BYTES:
        return False
    try:
        value = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError):
        return False
    return (isinstance(value, dict) and value.get("ok") is True
            and isinstance(value.get("data"), dict)
            and value["data"].get("status") == "healthy"
            and isinstance(value["data"].get("database"), dict)
            and value["data"]["database"].get("status") == "healthy")


def _gateway_listener_ready() -> bool:
    try:
        with socket.create_connection(GATEWAY_ADDRESS, timeout=1.5):
            return True
    except OSError:
        return False


def _wait_gateway_listener() -> None:
    deadline = time.monotonic() + GATEWAY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if _gateway_listener_ready():
            return
        time.sleep(0.2)
    raise LifecycleError("Gateway listener did not become ready")


def _stop(state_dir: Path) -> dict[str, Any]:
    checkpoint_path = state_dir / "pending.json"
    backend_manifest = _backend_manifest(SOURCE_BACKEND)
    _preflight_export(backend_manifest)
    checkpoint: dict[str, Any] | None = None
    if checkpoint_path.exists():
        old = _read_checkpoint(checkpoint_path)
        if old["status"] == "pending":
            checkpoint = old
            if _manifest_digest(backend_manifest) != checkpoint["backend_manifest_sha256"]:
                raise LifecycleError("source identity changed since rollout stop")
    if checkpoint is None:
        active = {unit: _is_active(unit) for unit in (*RESTORE_ORDER, *TRANSIENT_JOBS, API_UNIT)}
        if not active.get(API_UNIT):
            raise LifecycleError("API must be active before controlled stop")
        running_jobs = [unit for unit in TRANSIENT_JOBS if active.get(unit)]
        if running_jobs:
            raise LifecycleError("finish active one-shot jobs before rollout stop")
        checkpoint = {
            "schema": SCHEMA,
            "operation_id": str(uuid.uuid4()),
            "status": "pending",
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "backend_manifest": backend_manifest,
            "backend_manifest_sha256": _manifest_digest(backend_manifest),
            "initial_backend_manifest_sha256": _manifest_digest(backend_manifest),
            "source_transitions": [],
            "active_units": active,
            "pending_units": [],
        }
        _atomic_write(checkpoint_path, checkpoint)
    active = checkpoint["active_units"]
    for unit in STOP_ORDER:
        if active.get(unit) and _is_active(unit):
            _systemctl("stop", unit)
    running_jobs = [unit for unit in TRANSIENT_JOBS if _is_active(unit)]
    if running_jobs:
        raise LifecycleError("a one-shot job became active during rollout stop; API remains available")
    if _is_active(API_UNIT):
        _systemctl("stop", API_UNIT)
    return {"status": "stopped", "operation_id": checkpoint["operation_id"],
            "active_units": [unit for unit, enabled in active.items() if enabled]}


def _resume(args: argparse.Namespace, state_dir: Path) -> dict[str, Any]:
    checkpoint_path = state_dir / "pending.json"
    checkpoint = _read_checkpoint(checkpoint_path)
    if checkpoint["status"] == "complete":
        return {"status": "complete", "operation_id": checkpoint["operation_id"],
                "pending_units": []}
    if not _api_health():
        raise LifecycleError("API health gate failed; dependent units were not restored")
    expected = checkpoint["backend_manifest"]
    current = _backend_manifest(SOURCE_BACKEND)
    if _manifest_digest(current) != checkpoint["backend_manifest_sha256"]:
        raise LifecycleError("source identity changed since rollout stop")
    _verify_export(expected)

    active_units = checkpoint["active_units"]
    pending = list(checkpoint["pending_units"])
    for unit in RESTORE_ORDER:
        if not active_units.get(unit):
            continue
        if unit in PERSISTENT_TIMERS and not args.allow_timer_catchup:
            if not _is_active(unit) and unit not in pending:
                pending.append(unit)
            continue
        if not _is_active(unit):
            _systemctl("start", unit)
        if not _is_active(unit):
            raise LifecycleError(f"unit did not become active after start: {unit}")
        if unit == GATEWAY_UNIT:
            _wait_gateway_listener()
        if unit in pending:
            pending.remove(unit)
    checkpoint["pending_units"] = pending
    checkpoint["status"] = "pending" if pending else "complete"
    _atomic_write(checkpoint_path, checkpoint)
    return {"status": checkpoint["status"], "operation_id": checkpoint["operation_id"],
            "pending_units": pending}


def run_command(argv: list[str] | None = None) -> dict[str, Any]:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("stop", "resume", "transfer"))
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument("--expected-operation-id")
    parser.add_argument("--expected-current-manifest-sha256")
    parser.add_argument("--expected-new-manifest-sha256")
    parser.add_argument("--allow-timer-catchup", action="store_true",
                        help="explicitly allow reactivation of previously active Persistent=true timers")
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        raise LifecycleError("controlled backend lifecycle must run as root")
    if args.allow_timer_catchup and args.command != "resume":
        raise LifecycleError("--allow-timer-catchup applies only to resume")
    if args.command == "transfer" and not all((args.expected_operation_id,
                                                args.expected_current_manifest_sha256,
                                                args.expected_new_manifest_sha256)):
        raise LifecycleError("transfer requires expected operation id and old/new manifest digests")
    if args.command != "transfer" and any((args.expected_operation_id,
                                             args.expected_current_manifest_sha256,
                                             args.expected_new_manifest_sha256)):
        raise LifecycleError("expected source transition arguments apply only to transfer")
    state_dir = _validate_state_dir(args.state_dir)
    lock_fd = _locked(state_dir)
    try:
        if args.command == "stop":
            return _stop(state_dir)
        if args.command == "transfer":
            return _transfer(args, state_dir)
        return _resume(args, state_dir)
    finally:
        os.close(lock_fd)


def main() -> int:
    try:
        result = run_command()
    except LifecycleError as exc:
        print(f"controlled-backend-lifecycle: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
