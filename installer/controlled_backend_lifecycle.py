#!/usr/bin/env python3
"""Safely stop and resume the allowlisted API dependents during a source rollout."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import http.client
import importlib
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


SCHEMA = 4
PREVIOUS_SCHEMA = 3
LEGACY_SCHEMA = 2
OLDEST_SCHEMA = 1
MAX_SOURCE_TRANSITIONS = 16
DEFAULT_STATE_DIR = Path("/var/lib/fwrouter-v2/state/controlled-rollout")
SOURCE_BACKEND = Path(__file__).resolve().parents[1] / "backend"
TARGET_BACKEND = Path("/opt/fwrouter-api")
API_UNIT = "fwrouter-api.service"
API_EXECUTABLE = "/opt/fwrouter-api/.venv/bin/python"
API_MAINTENANCE_ARGV = "fwrouter_api_maintenance run-api"
GATEWAY_UNIT = "fwrouter-xray-sub-gateway.service"
MIHOMO_UNIT = "fwrouter-mihomo.service"
GATEWAY_ADDRESS = ("172.18.0.1", 5055)
HEALTH_TIMEOUT_SECONDS = 8
HEALTH_MAX_BYTES = 64 * 1024
GATEWAY_TIMEOUT_SECONDS = 10
MAX_MANIFEST_FILES = 10000
MAX_MANIFEST_BYTES = 512 * 1024 * 1024
MAX_CHECKPOINT_BYTES = 2 * 1024 * 1024
MIHOMO_CONTAINER = "fwrouter-mihomo"
MIHOMO_COMPOSE_FILE = Path("/opt/fwrouter-mihomo/docker-compose.yml")
MIHOMO_CACHE_DB = Path("/var/lib/fwrouter-v2/mihomo/cache.db")
MIHOMO_CONFIG_IMAGE = "metacubex/mihomo:v1.19.31"
MIHOMO_UNIT_FILE = Path("/etc/systemd/system/fwrouter-mihomo.service")
MIHOMO_UNIT_SHA256 = "9467c8b563cdfba9be8b30f6974e8f2ec6c157ebbe4c8f2abab17b7881fed8a2"
DOCKER_INSPECT_FORMAT = (
    '{{.Id}}|{{.Image}}|{{.Config.Image}}|{{.State.Running}}|'
    '{{.HostConfig.RestartPolicy.Name}}|{{.HostConfig.NetworkMode}}|'
    '{{index .Config.Labels "com.docker.compose.service"}}|'
    '{{index .Config.Labels "com.docker.compose.config-hash"}}|'
    '{{range .Mounts}}{{if and (eq .Source "/var/lib/fwrouter-v2/mihomo") '
    '(eq .Destination "/root/.config/mihomo")}}{{.RW}}{{end}}{{end}}'
)

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
ALLOWED_UNITS = frozenset((API_UNIT, MIHOMO_UNIT, *LONG_RUNNING_DEPENDENTS, *PERSISTENT_TIMERS, *TRANSIENT_JOBS))
RESTORE_ORDER = (*LONG_RUNNING_DEPENDENTS, *PERSISTENT_TIMERS)
STOP_ORDER = (*reversed(PERSISTENT_TIMERS), *reversed(LONG_RUNNING_DEPENDENTS))
CHECKPOINT_KEYS = {
    "schema", "operation_id", "status", "created_at", "backend_manifest",
    "backend_manifest_sha256", "active_units", "pending_units",
    "initial_backend_manifest_sha256", "source_transitions", "maintenance_operation",
    "runtime_quiescence",
}
SCHEMA3_CHECKPOINT_KEYS = CHECKPOINT_KEYS - {"runtime_quiescence"}
PREVIOUS_CHECKPOINT_KEYS = SCHEMA3_CHECKPOINT_KEYS - {"maintenance_operation"}
LEGACY_CHECKPOINT_KEYS = PREVIOUS_CHECKPOINT_KEYS - {
    "initial_backend_manifest_sha256", "source_transitions",
}
OLDEST_CHECKPOINT_KEYS = LEGACY_CHECKPOINT_KEYS - {"maintenance_operation"}


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
    if ".." in path.parts:
        raise LifecycleError("state directory must not contain traversal components")
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
            or (schema == OLDEST_SCHEMA and set(value) != OLDEST_CHECKPOINT_KEYS)
            or (schema == LEGACY_SCHEMA and set(value) != PREVIOUS_CHECKPOINT_KEYS)
            or (schema == PREVIOUS_SCHEMA and set(value) != SCHEMA3_CHECKPOINT_KEYS)
            or (schema == SCHEMA and set(value) != CHECKPOINT_KEYS)
            or schema not in {OLDEST_SCHEMA, LEGACY_SCHEMA, PREVIOUS_SCHEMA, SCHEMA}):
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
    if schema in {LEGACY_SCHEMA, PREVIOUS_SCHEMA, SCHEMA}:
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
    if schema in {PREVIOUS_SCHEMA, SCHEMA}:
        maintenance = value["maintenance_operation"]
        if maintenance is not None:
            _validate_maintenance_operation(maintenance, value["operation_id"])
            if maintenance.get("status") != "pending" and maintenance.get("backup") is None:
                raise LifecycleError("offline rebuild phase has no pinned backup")
            if maintenance.get("status") == "verified" and maintenance.get("backup") is None:
                raise LifecycleError("verified rebuild has no pinned backup")
            if maintenance.get("status") == "verified" and value["status"] not in {"pending", "complete"}:
                raise LifecycleError("verified rebuild checkpoint has an invalid rollout status")
            if maintenance.get("status") not in {
                    "pending", "candidate_ready", "source_quiesced", "published", "verified"}:
                raise LifecycleError("rollout maintenance operation is invalid")
    if schema == SCHEMA:
        _validate_runtime_quiescence(value["runtime_quiescence"], value["operation_id"])
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


def _validate_maintenance_operation(value: object, operation_id: str) -> None:
    if not isinstance(value, dict) or set(value) not in (
            {"kind", "status"}, {"kind", "status", "backup"}):
        raise LifecycleError("rollout maintenance operation is invalid")
    if value.get("kind") != "database_rebuild" or value.get("status") not in {
            "pending", "candidate_ready", "source_quiesced", "published", "verified"}:
        raise LifecycleError("rollout maintenance operation is invalid")
    backup = value.get("backup")
    if backup is None:
        if value.get("status") != "pending":
            raise LifecycleError("offline rebuild phase has no pinned backup")
        return
    if not isinstance(backup, dict) or set(backup) != {"filename", "sha256"}:
        raise LifecycleError("rollout maintenance backup pin is invalid")
    filename = backup.get("filename")
    digest = backup.get("sha256")
    prefix = f"fwrouter.db.rebuild-{uuid.UUID(operation_id).hex}."
    if (not isinstance(filename, str) or filename != Path(filename).name
            or not filename.startswith(prefix) or not filename.endswith(".bak")
            or not isinstance(digest, str) or len(digest) != 64
            or any(ch not in "0123456789abcdef" for ch in digest)):
        raise LifecycleError("rollout maintenance backup pin is invalid")


def _validate_runtime_quiescence(value: object, operation_id: str) -> None:
    if value is None:
        return
    keys = {"kind", "phase", "operation_id", "source_manifest_sha256", "unit_active_state",
            "unit_sub_state", "unit_enabled_state", "container_id", "image_id",
            "config_image", "compose_config_hash", "compose_file_sha256",
            "cache_before", "cache_after"}
    if not isinstance(value, dict) or set(value) != keys:
        raise LifecycleError("Mihomo runtime quiescence checkpoint is invalid")
    if (value.get("kind") != "mihomo_existing_container/v1"
            or value.get("operation_id") != operation_id
            or value.get("phase") not in {"stop_pending", "quiesced", "release_pending", "released"}
            or value.get("unit_active_state") not in {"active", "inactive"}
            or value.get("unit_sub_state") not in {"exited", "dead"}
            or value.get("unit_enabled_state") not in {"enabled", "disabled"}
            or value.get("config_image") != MIHOMO_CONFIG_IMAGE
            or (value.get("unit_active_state"), value.get("unit_sub_state")) not in {
                ("active", "exited"), ("inactive", "dead")}):
        raise LifecycleError("Mihomo runtime quiescence checkpoint is invalid")
    for name in ("source_manifest_sha256", "compose_config_hash", "compose_file_sha256"):
        digest = value.get(name)
        if (not isinstance(digest, str) or len(digest) != 64
                or any(char not in "0123456789abcdef" for char in digest)):
            raise LifecycleError("Mihomo runtime quiescence checkpoint is invalid")
    image_id = value.get("image_id")
    if (not isinstance(image_id, str) or not image_id.startswith("sha256:")
            or not re_full_hex(image_id.removeprefix("sha256:"), 64)):
        raise LifecycleError("Mihomo runtime quiescence checkpoint is invalid")
    container_id = value.get("container_id")
    if (not isinstance(container_id, str) or len(container_id) != 64
            or any(char not in "0123456789abcdef" for char in container_id.lower())):
        raise LifecycleError("Mihomo runtime quiescence checkpoint is invalid")
    for name in ("cache_before", "cache_after"):
        cache=value.get(name)
        if cache is None and name=="cache_after" and value.get("phase")=="stop_pending":
            continue
        if (not isinstance(cache,dict) or set(cache)!={"sha256","size"}
                or not isinstance(cache.get("sha256"),str) or not re_full_hex(cache["sha256"],64)
                or type(cache.get("size")) is not int or cache["size"]<0):
            raise LifecycleError("Mihomo cache transition checkpoint is invalid")
    if value.get("phase") in {"quiesced","release_pending","released"} and value.get("cache_after") is None:
        raise LifecycleError("Mihomo quiescence lacks its post-stop cache identity")


def _check_expected_lifecycle_identity(checkpoint: dict[str, Any], operation_id: str,
                                       manifest_sha256: str) -> None:
    try:
        expected_operation = str(uuid.UUID(operation_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise LifecycleError("expected rollout operation id is invalid") from exc
    if (checkpoint.get("status") != "pending" or checkpoint.get("operation_id") != expected_operation
            or checkpoint.get("backend_manifest_sha256") != manifest_sha256
            or checkpoint.get("initial_backend_manifest_sha256") != manifest_sha256
            or checkpoint.get("source_transitions") != []
            or checkpoint.get("maintenance_operation") is not None):
        raise LifecycleError("Mihomo quiescence requires the exact pending OLD lifecycle checkpoint")


def _systemctl_state(action: str, unit: str) -> str:
    if unit not in {MIHOMO_UNIT}:
        raise LifecycleError("refusing to inspect non-allowlisted runtime unit")
    env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "HOME": "/root", "LANG": "C.UTF-8"}
    try:
        result = subprocess.run(["/usr/bin/systemctl", action, unit], check=False,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, timeout=10, env=env)
    except (OSError, subprocess.SubprocessError) as exc:
        raise LifecycleError(f"cannot inspect systemd state for {unit}") from exc
    expected_codes = {0, 1} if action == "is-enabled" else {0}
    if result.returncode not in expected_codes or len(result.stdout) > 256:
        raise LifecycleError(f"cannot inspect systemd state for {unit}")
    return result.stdout.strip()


def _mihomo_unit_state() -> tuple[str, str, str]:
    properties = {}
    env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "HOME": "/root", "LANG": "C.UTF-8"}
    for prop in ("LoadState", "FragmentPath", "DropInPaths", "Type", "RemainAfterExit",
                 "ActiveState", "SubState"):
        try:
            result = subprocess.run(["/usr/bin/systemctl", "show", f"--property={prop}",
                                     "--value", MIHOMO_UNIT], check=False,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, timeout=10, env=env)
        except (OSError, subprocess.SubprocessError) as exc:
            raise LifecycleError("cannot inspect Mihomo unit contract") from exc
        if result.returncode != 0 or len(result.stdout) > 512:
            raise LifecycleError("cannot inspect Mihomo unit contract")
        properties[prop] = result.stdout.strip()
    if (properties["LoadState"] != "loaded"
            or properties["FragmentPath"] != "/etc/systemd/system/fwrouter-mihomo.service"
            or properties["DropInPaths"] not in {"", "-"}
            or properties["Type"] != "oneshot"
            or properties["RemainAfterExit"] != "yes"
            or _trusted_fixed_file_digest(MIHOMO_UNIT_FILE) != MIHOMO_UNIT_SHA256):
        raise LifecycleError("Mihomo systemd unit contract is not the qualified fixed unit")
    if properties["ActiveState"] == "active" and properties["SubState"] == "exited":
        active_state, sub_state = "active", "exited"
    elif properties["ActiveState"] == "inactive" and properties["SubState"] == "dead":
        active_state, sub_state = "inactive", "dead"
    else:
        raise LifecycleError("Mihomo systemd unit is in an unsupported state")
    enabled = _systemctl_state("is-enabled", MIHOMO_UNIT)
    if enabled not in {"enabled", "disabled"}:
        raise LifecycleError("Mihomo systemd enablement is unsupported")
    return active_state, sub_state, enabled


def _mihomo_container() -> dict[str, str | bool]:
    env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "HOME": "/root",
           "LANG": "C.UTF-8", "DOCKER_HOST": "unix:///var/run/docker.sock"}
    try:
        result = subprocess.run(["/usr/bin/docker", "inspect", "--format",
                                 DOCKER_INSPECT_FORMAT, MIHOMO_CONTAINER],
                                check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, timeout=15, env=env)
    except (OSError, subprocess.SubprocessError) as exc:
        raise LifecycleError("cannot inspect the fixed Mihomo container") from exc
    if result.returncode != 0 or len(result.stdout) > 2048 or len(result.stderr) > 4096:
        raise LifecycleError("cannot inspect the fixed Mihomo container")
    fields = result.stdout.strip().split("|")
    if (len(fields) != 9 or len(fields[0]) != 64
            or any(char not in "0123456789abcdef" for char in fields[0].lower())
            or len(fields[1]) != 71 or not fields[1].startswith("sha256:")
            or any(char not in "0123456789abcdef" for char in fields[1][7:])
            or fields[2] != MIHOMO_CONFIG_IMAGE or fields[3] not in {"true", "false"}
            or fields[4] != "unless-stopped" or fields[5] != "host"
            or fields[6] != "mihomo" or len(fields[7]) != 64
            or any(char not in "0123456789abcdef" for char in fields[7])
            or fields[8] not in {"true", "false"}):
        raise LifecycleError("fixed Mihomo container identity is invalid")
    if fields[8] != "true":
        raise LifecycleError("Mihomo container lacks its required writable protected mount")
    return {"container_id": fields[0].lower(), "image_id": fields[1],
            "config_image": fields[2], "running": fields[3] == "true",
            "compose_config_hash": fields[7]}


def _trusted_fixed_file_digest(path: Path, *, max_bytes: int = 1024 * 1024) -> str:
    path = Path(path)
    for parent in reversed(path.parents):
        try:
            info = parent.lstat()
        except OSError as exc:
            raise LifecycleError("fixed runtime path ancestor is unavailable") from exc
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0
                or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)):
            raise LifecycleError("fixed runtime path ancestor is untrusted")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise LifecycleError("fixed runtime file is unavailable") from exc
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != 0
                or before.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
                or before.st_size > max_bytes):
            raise LifecycleError("fixed runtime file is untrusted")
        digest = hashlib.sha256()
        total = 0
        while chunk := os.read(fd, 64 * 1024):
            total += len(chunk)
            if total > max_bytes:
                raise LifecycleError("fixed runtime file exceeds its size limit")
            digest.update(chunk)
        after = os.fstat(fd)
        named = path.lstat()
        identity = lambda item: (item.st_dev, item.st_ino, item.st_size,
                                  item.st_mtime_ns, item.st_ctime_ns)
        if (identity(before) != identity(after) or identity(after) != identity(named)
                or not stat.S_ISREG(named.st_mode) or named.st_uid != 0
                or named.st_mode & (stat.S_IWGRP | stat.S_IWOTH) or total != after.st_size):
            raise LifecycleError("fixed runtime file changed while reading")
        return digest.hexdigest()
    finally:
        os.close(fd)


def _compose_file_digest() -> str:
    return _trusted_fixed_file_digest(MIHOMO_COMPOSE_FILE)


def _mihomo_cache_state() -> dict[str, Any]:
    """Hash only the fixed writable cache file, with bounded trusted-path checks."""
    path = MIHOMO_CACHE_DB
    for parent in reversed(path.parents):
        info = parent.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)):
            raise LifecycleError("fixed Mihomo cache ancestor is untrusted")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try: fd=os.open(path,flags)
    except OSError as exc:
        raise LifecycleError("fixed Mihomo cache is unavailable") from exc
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
                or before.st_nlink != 1 or before.st_size > 64 * 1024 * 1024
                or before.st_mode & (stat.S_IWGRP | stat.S_IWOTH)):
            raise LifecycleError("fixed Mihomo cache is untrusted")
        digest = hashlib.sha256()
        total = 0
        while chunk := os.read(fd, 65536):
            total += len(chunk)
            if total > 64 * 1024 * 1024:
                raise LifecycleError("fixed Mihomo cache exceeds its bound")
            digest.update(chunk)
        after = os.fstat(fd)
        named = path.lstat()
        identity = lambda item: (item.st_dev, item.st_ino, item.st_size,
                                 item.st_mtime_ns, item.st_ctime_ns)
        if (identity(before) != identity(after) or identity(after) != identity(named)
                or total != after.st_size or not stat.S_ISREG(named.st_mode)):
            raise LifecycleError("fixed Mihomo cache changed while hashing")
        return {"sha256": digest.hexdigest(), "size": total}
    finally:
        os.close(fd)


def _docker_action(action: str, container_id: str) -> None:
    if action not in {"stop", "start"} or len(container_id) != 64:
        raise LifecycleError("refusing non-allowlisted Mihomo container action")
    env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "HOME": "/root",
           "LANG": "C.UTF-8", "DOCKER_HOST": "unix:///var/run/docker.sock"}
    argv = (["/usr/bin/docker", "stop", "--time", "30", container_id]
            if action == "stop" else ["/usr/bin/docker", "start", container_id])
    try:
        result = subprocess.run(argv, check=False, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, timeout=45, env=env)
    except (OSError, subprocess.SubprocessError) as exc:
        raise LifecycleError(f"cannot {action} the pinned Mihomo container") from exc
    if result.returncode != 0 or len(result.stdout) > 256 or len(result.stderr) > 4096:
        raise LifecycleError(f"cannot {action} the pinned Mihomo container")


def _verify_operation_backup_pin(state_dir: Path, operation_id: str, backup: object) -> None:
    _validate_maintenance_operation(
        {"kind": "database_rebuild", "status": "published", "backup": backup}, operation_id,
    )
    assert isinstance(backup, dict)
    backup_dir = state_dir.parents[1] / "backups"
    try:
        parent = backup_dir.lstat()
        path = backup_dir / str(backup["filename"])
        info = path.lstat()
    except OSError as exc:
        raise LifecycleError("pinned operation backup is unavailable") from exc
    if (not stat.S_ISDIR(parent.st_mode) or stat.S_ISLNK(parent.st_mode)
            or parent.st_uid != os.geteuid() or stat.S_IMODE(parent.st_mode) != 0o700
            or not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1):
        raise LifecycleError("pinned operation backup path is unsafe")
    digest = _sha256_file(path)
    if digest != backup["sha256"]:
        raise LifecycleError("pinned operation backup digest changed")


def _sha256_file(path: Path) -> str:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
                or stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1):
            raise LifecycleError("pinned operation backup file is unsafe")
        digest = hashlib.sha256()
        while chunk := os.read(fd, 1024 * 1024):
            digest.update(chunk)
        after = os.fstat(fd)
        named = path.lstat()
        if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                or after.st_uid != os.geteuid() or stat.S_IMODE(after.st_mode) != 0o600
                or after.st_nlink != 1
                or (after.st_dev, after.st_ino) != (named.st_dev, named.st_ino)):
            raise LifecycleError("pinned operation backup changed while hashing")
        return digest.hexdigest()
    finally:
        os.close(fd)


def _validate_expected_runtime_args(args: argparse.Namespace) -> None:
    try:
        normalized = str(uuid.UUID(args.expected_operation_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise LifecycleError("expected rollout operation id is invalid") from exc
    args.expected_operation_id = normalized
    for value in (args.expected_current_manifest_sha256, args.expected_container_id,
                  args.expected_image_id, args.expected_compose_config_hash,
                  args.expected_compose_file_sha256):
        if not isinstance(value, str):
            raise LifecycleError("Mihomo runtime operation identity is incomplete")
    if (not re_full_hex(args.expected_current_manifest_sha256, 64)
            or not re_full_hex(args.expected_container_id, 64)
            or not re_full_hex(args.expected_image_id.removeprefix("sha256:"), 64)
            or not args.expected_image_id.startswith("sha256:")
            or not re_full_hex(args.expected_compose_config_hash, 64)
            or not re_full_hex(args.expected_compose_file_sha256, 64)):
        raise LifecycleError("Mihomo runtime operation identity is invalid")


def re_full_hex(value: str, length: int) -> bool:
    return len(value) == length and all(char in "0123456789abcdef" for char in value)


def _runtime_checkpoint(checkpoint: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    quiescence = checkpoint.get("runtime_quiescence")
    if not isinstance(quiescence, dict):
        raise LifecycleError("Mihomo quiescence has not been recorded for this operation")
    if (quiescence.get("operation_id") != args.expected_operation_id
            or quiescence.get("container_id") != args.expected_container_id
            or quiescence.get("image_id") != args.expected_image_id
            or quiescence.get("compose_config_hash") != args.expected_compose_config_hash
            or quiescence.get("compose_file_sha256") != args.expected_compose_file_sha256):
        raise LifecycleError("Mihomo runtime identity changed from the approved operation")
    if args.expected_current_manifest_sha256 != checkpoint.get("backend_manifest_sha256"):
        raise LifecycleError("Mihomo runtime operation source checkpoint changed")
    initial = quiescence["source_manifest_sha256"]
    transitions = checkpoint.get("source_transitions", [])
    if checkpoint["backend_manifest_sha256"] == initial:
        if transitions:
            raise LifecycleError("Mihomo source checkpoint has an unexpected transition")
    elif (len(transitions) != 1
            or transitions[0].get("operation_id") != args.expected_operation_id
            or transitions[0].get("from_manifest_sha256") != initial
            or transitions[0].get("to_manifest_sha256") != checkpoint["backend_manifest_sha256"]):
        raise LifecycleError("Mihomo source checkpoint is not the owned forward CAS")
    return quiescence


def _quiesce_mihomo(args: argparse.Namespace, state_dir: Path) -> dict[str, Any]:
    _validate_expected_runtime_args(args)
    path = state_dir / "pending.json"
    checkpoint = _checkpoint_as_current_schema(_read_checkpoint(path))
    _check_expected_lifecycle_identity(
        checkpoint, args.expected_operation_id, args.expected_current_manifest_sha256)
    installed = _backend_manifest(TARGET_BACKEND)
    if _manifest_digest(installed) != args.expected_current_manifest_sha256:
        raise LifecycleError("installed OLD backend changed before Mihomo quiescence")
    # The first call reaches this point with no runtime record; retries may
    # reach it with stop_pending persisted immediately before docker stop.
    # Check rollout-owned systemd units here, then validate the recorded
    # container identity and continue the same bounded stop below. Transfer
    # keeps the stricter `_require_units_stopped(checkpoint)` check and cannot
    # proceed until the runtime phase is quiesced.
    _require_units_stopped()
    quiescence = checkpoint.get("runtime_quiescence")
    if quiescence is None:
        unit_active, unit_sub, unit_enabled = _mihomo_unit_state()
        container = _mihomo_container()
        compose_digest = _compose_file_digest()
        if (container["container_id"] != args.expected_container_id
                or container["image_id"] != args.expected_image_id
                or container["compose_config_hash"] != args.expected_compose_config_hash
                or compose_digest != args.expected_compose_file_sha256
                or container["running"] != (unit_active == "active")):
            raise LifecycleError("Mihomo service/container identity is not the approved baseline")
        quiescence = {
            "kind": "mihomo_existing_container/v1", "phase": "stop_pending",
            "operation_id": checkpoint["operation_id"],
            "source_manifest_sha256": args.expected_current_manifest_sha256,
            "unit_active_state": unit_active, "unit_sub_state": unit_sub,
            "unit_enabled_state": unit_enabled, "container_id": container["container_id"],
            "image_id": container["image_id"], "config_image": container["config_image"],
            "compose_config_hash": container["compose_config_hash"],
            "compose_file_sha256": compose_digest,
            "cache_before": _mihomo_cache_state(), "cache_after": None,
        }
        checkpoint["runtime_quiescence"] = quiescence
        _atomic_write(path, checkpoint)
    else:
        _validate_runtime_quiescence(quiescence, checkpoint["operation_id"])
        if (quiescence["operation_id"] != args.expected_operation_id
                or quiescence["source_manifest_sha256"] != args.expected_current_manifest_sha256
                or quiescence["container_id"] != args.expected_container_id
                or quiescence["image_id"] != args.expected_image_id
                or quiescence["compose_config_hash"] != args.expected_compose_config_hash
                or quiescence["compose_file_sha256"] != args.expected_compose_file_sha256):
            raise LifecycleError("Mihomo quiescence is owned by another runtime identity")
        if quiescence["phase"] == "quiesced":
            container = _mihomo_container()
            unit_state = _mihomo_unit_state()
            if (container["container_id"] != quiescence["container_id"]
                    or container["image_id"] != quiescence["image_id"]
                    or container["compose_config_hash"] != quiescence["compose_config_hash"]
                    or container["running"]
                    or unit_state != (quiescence["unit_active_state"],
                                      quiescence["unit_sub_state"],
                                      quiescence["unit_enabled_state"])
                    or _mihomo_cache_state() != quiescence["cache_after"]):
                raise LifecycleError("Mihomo quiescence changed before idempotent readback")
            return {"status": "quiesced", "operation_id": checkpoint["operation_id"],
                    "container_id": quiescence["container_id"],
                    "image_id": quiescence["image_id"], "phase": quiescence["phase"]}
        if quiescence["phase"] in {"release_pending", "released"}:
            raise LifecycleError("Mihomo quiescence is already beyond the stop phase")

    container = _mihomo_container()
    unit_state = _mihomo_unit_state()
    if (container["container_id"] != quiescence["container_id"]
            or container["image_id"] != quiescence["image_id"]
            or container["compose_config_hash"] != quiescence["compose_config_hash"]
            or _compose_file_digest() != quiescence["compose_file_sha256"]
            or unit_state != (quiescence["unit_active_state"],
                              quiescence["unit_sub_state"],
                              quiescence["unit_enabled_state"])):
        raise LifecycleError("Mihomo container or Compose identity changed before stop")
    cache_now = _mihomo_cache_state()
    if cache_now != quiescence["cache_before"]:
        raise LifecycleError("fixed Mihomo cache changed after the owned stop intent")
    # Do not mask the unit: the installed /etc unit has higher search priority
    # than /run/systemd/system, so a runtime mask would not reliably fence it.
    # Stop only the already-identified container by immutable Docker ID. Docker
    # stop suppresses its unless-stopped restart policy until explicit start.
    if container["running"]:
        _docker_action("stop", quiescence["container_id"])
    stopped = _mihomo_container()
    cache_after = _mihomo_cache_state()
    if (stopped["container_id"] != quiescence["container_id"]
            or stopped["image_id"] != quiescence["image_id"]
            or stopped["compose_config_hash"] != quiescence["compose_config_hash"]
            or stopped["running"]
            or _compose_file_digest() != quiescence["compose_file_sha256"]
            or _mihomo_unit_state() != (quiescence["unit_active_state"],
                                        quiescence["unit_sub_state"],
                                        quiescence["unit_enabled_state"])):
        raise LifecycleError("Mihomo container did not reach its pinned stopped state")
    quiescence["cache_after"]=cache_after
    quiescence["phase"] = "quiesced"
    _atomic_write(path, checkpoint)
    return {"status": "quiesced", "operation_id": checkpoint["operation_id"],
            "container_id": quiescence["container_id"], "image_id": quiescence["image_id"],
            "phase": quiescence["phase"]}


def _release_mihomo(args: argparse.Namespace, state_dir: Path) -> dict[str, Any]:
    _validate_expected_runtime_args(args)
    path = state_dir / "pending.json"
    checkpoint = _checkpoint_as_current_schema(_read_checkpoint(path))
    if checkpoint["status"] != "pending":
        raise LifecycleError("Mihomo release requires a pending owned operation")
    quiescence = _runtime_checkpoint(checkpoint, args)
    if quiescence["phase"] == "released":
        container = _mihomo_container()
        unit_state = _mihomo_unit_state()
        expected_running = quiescence["unit_active_state"] == "active"
        if (container["container_id"] != quiescence["container_id"]
                or container["image_id"] != quiescence["image_id"]
                or container["compose_config_hash"] != quiescence["compose_config_hash"]
                or container["config_image"] != quiescence["config_image"]
                or container["running"] != expected_running
                or unit_state != (quiescence["unit_active_state"],
                                  quiescence["unit_sub_state"],
                                  quiescence["unit_enabled_state"])
                or _compose_file_digest() != quiescence["compose_file_sha256"]):
            raise LifecycleError("released Mihomo runtime no longer matches its checkpoint")
        return {"status": "released", "operation_id": checkpoint["operation_id"],
                "container_id": quiescence["container_id"], "image_id": quiescence["image_id"]}
    if quiescence["phase"] not in {"quiesced", "release_pending"}:
        raise LifecycleError("Mihomo runtime is not ready for controlled release")
    if getattr(args, "jit_owner_quiescence", False):
        # The explicit JIT mode releases the exact pre-existing container only
        # after NEW source CAS/install while API and every rollout-owned writer
        # remain stopped. The owner proof refresh and ordinary API health gates
        # run after release; legacy resume keeps its original health requirement.
        if quiescence["phase"] not in {"quiesced", "release_pending"}:
            raise LifecycleError("JIT release requires the owned quiesced phase")
        _require_units_stopped()
        if quiescence["phase"] == "quiesced":
            _require_units_stopped(checkpoint)
            if _mihomo_cache_state() != quiescence["cache_after"]:
                raise LifecycleError("fixed Mihomo cache changed before controlled release")
        else:
            pending_container = _mihomo_container()
            if (pending_container["container_id"] != quiescence["container_id"]
                    or pending_container["image_id"] != quiescence["image_id"]
                    or pending_container["compose_config_hash"] != quiescence["compose_config_hash"]):
                raise LifecycleError("Mihomo runtime changed during pending release")
            if (not pending_container["running"]
                    and _mihomo_cache_state() != quiescence["cache_after"]):
                raise LifecycleError("fixed Mihomo cache changed before pending release retry")
        installed = _backend_manifest(TARGET_BACKEND)
        if _manifest_digest(installed) != checkpoint["backend_manifest_sha256"]:
            raise LifecycleError("JIT Mihomo release requires the exact installed NEW backend")
    elif not _api_health():
        raise LifecycleError("API health is required before Mihomo runtime release")
    compose_digest = _compose_file_digest()
    container = _mihomo_container()
    unit_state = _mihomo_unit_state()
    if (compose_digest != quiescence["compose_file_sha256"]
            or container["container_id"] != quiescence["container_id"]
            or container["image_id"] != quiescence["image_id"]
            or container["compose_config_hash"] != quiescence["compose_config_hash"]
            or (quiescence["phase"] == "quiesced" and container["running"])
            or unit_state != (quiescence["unit_active_state"],
                              quiescence["unit_sub_state"],
                              quiescence["unit_enabled_state"])):
        raise LifecycleError("Mihomo runtime changed before controlled release")
    if quiescence["phase"] == "quiesced":
        quiescence["phase"] = "release_pending"
        _atomic_write(path, checkpoint)
    if quiescence["unit_active_state"] == "active":
        if not container["running"]:
            _docker_action("start", quiescence["container_id"])
        started = _mihomo_container()
    else:
        started = container
    if (started["container_id"] != quiescence["container_id"]
            or started["image_id"] != quiescence["image_id"]
            or started["config_image"] != quiescence["config_image"]
            or started["compose_config_hash"] != quiescence["compose_config_hash"]
            or started["running"] != (quiescence["unit_active_state"] == "active")
            or _compose_file_digest() != quiescence["compose_file_sha256"]
            or _mihomo_unit_state() != (quiescence["unit_active_state"],
                                        quiescence["unit_sub_state"],
                                        quiescence["unit_enabled_state"])):
        raise LifecycleError("Mihomo release changed or failed to restore the pinned container")
    quiescence["phase"] = "released"
    _atomic_write(path, checkpoint)
    return {"status": "released", "operation_id": checkpoint["operation_id"],
            "container_id": quiescence["container_id"], "image_id": quiescence["image_id"]}


def _checkpoint_as_current_schema(checkpoint: dict[str, Any]) -> dict[str, Any]:
    """Upgrade an in-memory legacy checkpoint without changing its ownership."""
    if checkpoint["schema"] == SCHEMA:
        return checkpoint
    upgraded = dict(checkpoint)
    digest = checkpoint["backend_manifest_sha256"]
    upgraded.setdefault("initial_backend_manifest_sha256", digest)
    upgraded.setdefault("source_transitions", [])
    upgraded.setdefault("maintenance_operation", None)
    upgraded.setdefault("runtime_quiescence", None)
    upgraded["schema"] = SCHEMA
    return upgraded


def _require_units_stopped(checkpoint: dict[str, Any] | None = None) -> None:
    units = (*RESTORE_ORDER, *TRANSIENT_JOBS, API_UNIT)
    active = [unit for unit in units if _is_active(unit)]
    if active:
        raise LifecycleError("all rollout-owned API, dependent, timer, and one-shot units must be stopped")
    if checkpoint is not None:
        quiescence = checkpoint.get("runtime_quiescence")
        if quiescence is not None and quiescence.get("phase") in {
                "stop_pending", "quiesced"}:
            if (quiescence.get("phase") == "quiesced"
                    and _mihomo_cache_state() != quiescence.get("cache_after")):
                raise LifecycleError("fixed Mihomo cache changed after owned quiescence")
            container = _mihomo_container()
            if (container["container_id"] != quiescence["container_id"]
                    or container["image_id"] != quiescence["image_id"]
                    or container["compose_config_hash"] != quiescence["compose_config_hash"]
                    or container["running"]
                    or _compose_file_digest() != quiescence["compose_file_sha256"]
                    or _mihomo_unit_state() != (quiescence["unit_active_state"],
                                                quiescence["unit_sub_state"],
                                                quiescence["unit_enabled_state"])):
                raise LifecycleError("Mihomo runtime is not held at its owned quiescent identity")
        elif quiescence is not None and quiescence.get("phase") == "release_pending":
            raise LifecycleError("Mihomo runtime release is incomplete")


def _transfer(args: argparse.Namespace, state_dir: Path) -> dict[str, Any]:
    checkpoint_path = state_dir / "pending.json"
    checkpoint = _read_checkpoint(checkpoint_path)
    if checkpoint["status"] != "pending":
        raise LifecycleError("only a pending rollout checkpoint can transfer source ownership")
    if checkpoint.get("maintenance_operation") is not None:
        raise LifecycleError("source transfer is unavailable during database maintenance")
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
    runtime_quiescence = checkpoint.get("runtime_quiescence")
    if (runtime_quiescence is not None
            and runtime_quiescence.get("phase") != "quiesced"):
        raise LifecycleError("Mihomo must be held at the exact quiescent identity before source transfer")

    # Atomic publication can complete immediately before an interruption. Permit
    # only an exact replay of the final CAS, with the installed tree still at
    # either side of that transition and all rollout-owned units stopped.
    if current_checkpoint_digest == expected_new_digest:
        if (not transitions
                or transitions[-1]["from_manifest_sha256"] != expected_old_digest
                or transitions[-1]["to_manifest_sha256"] != expected_new_digest
                or transitions[-1]["operation_id"] != expected_operation_id):
            raise LifecycleError("checkpoint is already bound to a different source transition")
        _require_units_stopped(checkpoint)
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
    _require_units_stopped(checkpoint)
    # The old installed tree is the only admissible pre-install state. In
    # particular, a hand-edited/stale/partially overlaid backend cannot transfer.
    _verify_export(checkpoint["backend_manifest"])
    # Recheck after the bounded tree hash: the lifecycle flock serializes this
    # helper, while this second observation catches a unit started by an
    # unrelated systemd/operator action during hashing.
    _require_units_stopped(checkpoint)
    if (runtime_quiescence is not None
            and runtime_quiescence.get("phase") != "quiesced"):
        raise LifecycleError("Mihomo quiescence changed before source transfer")

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
        result = subprocess.run(["/usr/bin/systemctl", action, unit], check=False,
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
        result = subprocess.run(["/usr/bin/systemctl", "is-active", unit], check=False,
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


def _require_maintenance_compatible_api_unit() -> None:
    """Refuse a rebuild while a legacy/direct-uvicorn unit can bypass leases."""
    try:
        result = subprocess.run(
            ["/usr/bin/systemctl", "show", "--property=ExecStart", "--value", API_UNIT],
            check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise LifecycleError("cannot verify API maintenance launcher before stop") from exc
    command = result.stdout.strip()
    if (result.returncode != 0 or API_EXECUTABLE not in command
            or API_MAINTENANCE_ARGV not in command or "uvicorn" in command):
        raise LifecycleError("installed API unit does not hold the shared maintenance lease")


def _wait_gateway_listener() -> None:
    deadline = time.monotonic() + GATEWAY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if _gateway_listener_ready():
            return
        time.sleep(0.2)
    raise LifecycleError("Gateway listener did not become ready")


def _stop(
    state_dir: Path,
    *,
    maintenance_operation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    checkpoint_path = state_dir / "pending.json"
    backend_manifest = _backend_manifest(SOURCE_BACKEND)
    _preflight_export(backend_manifest)
    checkpoint: dict[str, Any] | None = None
    if checkpoint_path.exists():
        old = _read_checkpoint(checkpoint_path)
        if old["status"] == "pending":
            checkpoint = _checkpoint_as_current_schema(old)
            if _manifest_digest(backend_manifest) != checkpoint["backend_manifest_sha256"]:
                raise LifecycleError("source identity changed since rollout stop")
            if checkpoint.get("maintenance_operation") != maintenance_operation:
                raise LifecycleError("another controlled lifecycle operation is pending")
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
            "maintenance_operation": maintenance_operation,
            "runtime_quiescence": None,
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


def _run_database_rebuild(
    args: argparse.Namespace, state_dir: Path, lifecycle_lock_fd: int,
) -> dict[str, Any]:
    """Run snapshot rebuild only while the lifecycle owner holds exclusive admission."""
    checkpoint_path = state_dir / "pending.json"
    maintenance_kind = "database_rebuild"
    source_manifest = _backend_manifest(SOURCE_BACKEND)
    # Rebuild requires the exact trusted source to already be installed; it is
    # not an installer/bootstrap shortcut and must not run against stale code.
    _verify_export(source_manifest)
    # This check precedes the first stop/checkpoint/backup: older unit files
    # that launch uvicorn directly cannot participate in the shared barrier.
    _require_maintenance_compatible_api_unit()

    if checkpoint_path.exists():
        checkpoint = _read_checkpoint(checkpoint_path)
        maintenance = checkpoint.get("maintenance_operation")
        if (checkpoint["status"] != "pending" or not isinstance(maintenance, dict)
                or set(maintenance) not in ({"kind", "status"}, {"kind", "status", "backup"})
                or maintenance.get("kind") != maintenance_kind
                or maintenance.get("status") not in {
                    "pending", "candidate_ready", "source_quiesced", "published",
                }):
            raise LifecycleError("another lifecycle operation is pending")
        if checkpoint["backend_manifest_sha256"] != _manifest_digest(source_manifest):
            raise LifecycleError("installed backend identity changed during pending rebuild")
        if maintenance.get("backup") is not None:
            _verify_operation_backup_pin(state_dir, checkpoint["operation_id"], maintenance["backup"])
        operation_id = checkpoint["operation_id"]
    else:
        stopped = _stop(state_dir, maintenance_operation={
            "kind": maintenance_kind, "status": "pending", "backup": None,
        })
        operation_id = str(stopped["operation_id"])

    # Repeated stop checks are intentional: another systemd/operator action
    # can start a writer after the initial stop has completed.
    _require_units_stopped()
    if _backend_manifest(TARGET_BACKEND) != source_manifest:
        raise LifecycleError("installed backend identity changed before rebuild")

    sys.path.insert(0, str(SOURCE_BACKEND))
    try:
        admission = importlib.import_module(
            "fwrouter_api.services.maintenance_lifecycle_admission"
        )
        get_held_fd = admission.active_offline_rebuild_fd
        lease_context = admission._lifecycle_owner_offline_rebuild_lease
    except (ImportError, AttributeError) as exc:
        raise LifecycleError("installed lifecycle admission helper is unavailable") from exc

    state_root = state_dir.parents[1]
    try:
        with lease_context(lifecycle_lock_fd, state_root=state_root) as held_operation_id:
            if held_operation_id != operation_id:
                raise LifecycleError("offline operation identity changed before rebuild")
            lock_fd = get_held_fd()
            python = _verified_backend_interpreter()
            try:
                result = subprocess.run(
                    [str(python), "-m", "fwrouter_api_maintenance", "rebuild-db",
                     "--file-path", args.file_path,
                     "--operation-id", uuid.UUID(operation_id).hex,
                     "--maintenance-lock-fd", str(lock_fd),
                     "--lifecycle-lock-fd", str(lifecycle_lock_fd),
                     "--lifecycle-state-root", str(state_root)],
                    check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, timeout=1800, pass_fds=(lock_fd, lifecycle_lock_fd),
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise LifecycleError("offline database rebuild process did not complete") from exc
            if result.returncode != 0:
                raise LifecycleError("offline database rebuild failed; API startup remains blocked")
            try:
                lines = [line for line in result.stdout.splitlines() if line.strip()]
                receipt = json.loads(lines[-1]) if lines else None
            except (json.JSONDecodeError, IndexError) as exc:
                raise LifecycleError("offline rebuild returned no verifiable receipt") from exc
            expected_receipt_id = uuid.UUID(operation_id).hex
            if (not isinstance(receipt, dict) or receipt.get("ok") is not True
                    or receipt.get("operation_id") != expected_receipt_id):
                raise LifecycleError("offline rebuild receipt identity or status is invalid")

            checkpoint = _read_checkpoint(checkpoint_path)
            current_maintenance = checkpoint.get("maintenance_operation")
            if (checkpoint["status"] != "pending" or checkpoint["operation_id"] != operation_id
                    or not isinstance(current_maintenance, dict)
                    or current_maintenance.get("kind") != maintenance_kind
                    or current_maintenance.get("status") != "published"):
                raise LifecycleError("offline rebuild checkpoint changed before verification")
            _verify_operation_backup_pin(state_dir, operation_id, current_maintenance.get("backup"))
            checkpoint["maintenance_operation"] = {**current_maintenance, "status": "verified"}
            _atomic_write(checkpoint_path, checkpoint)
            return {"status": "verified", "operation_id": operation_id,
                    "pending_units": [unit for unit in PERSISTENT_TIMERS
                                      if checkpoint["active_units"].get(unit)]}
    except LifecycleError:
        raise
    except Exception as exc:
        raise LifecycleError("cannot establish offline rebuild admission") from exc


def _verified_backend_interpreter() -> Path:
    """Verify the venv launcher and resolved executable before invoking it."""
    path = TARGET_BACKEND / ".venv" / "bin" / "python"
    try:
        for parent in (TARGET_BACKEND, TARGET_BACKEND / ".venv", TARGET_BACKEND / ".venv" / "bin"):
            parent_info = parent.lstat()
            if (not stat.S_ISDIR(parent_info.st_mode) or stat.S_ISLNK(parent_info.st_mode)
                    or parent_info.st_uid != 0
                    or parent_info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)):
                raise LifecycleError("installed backend interpreter parent is writable or untrusted")
        link_info = path.lstat()
        if (not stat.S_ISREG(link_info.st_mode) and not stat.S_ISLNK(link_info.st_mode)):
            raise LifecycleError("installed backend interpreter has an unsafe type")
        if link_info.st_uid != 0:
            raise LifecycleError("installed backend interpreter is not root-owned")
        resolved = path.resolve(strict=True)
        cursor = Path(resolved.anchor)
        for part in resolved.parts[1:]:
            cursor = cursor / part
            info = cursor.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) and cursor != resolved:
                raise LifecycleError("resolved backend interpreter path is unsafe")
            if info.st_uid != 0 or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                raise LifecycleError("resolved backend interpreter path is writable or untrusted")
        final = resolved.lstat()
    except LifecycleError:
        raise
    except OSError as exc:
        raise LifecycleError("installed backend interpreter is unavailable") from exc
    if not stat.S_ISREG(final.st_mode) or not final.st_mode & 0o111:
        raise LifecycleError("resolved backend interpreter is not an executable regular file")
    return path


def _resume(args: argparse.Namespace, state_dir: Path) -> dict[str, Any]:
    checkpoint_path = state_dir / "pending.json"
    checkpoint = _read_checkpoint(checkpoint_path)
    if checkpoint["status"] == "complete":
        return {"status": "complete", "operation_id": checkpoint["operation_id"],
                "pending_units": []}
    maintenance = checkpoint.get("maintenance_operation")
    if maintenance is not None and maintenance.get("status") != "verified":
        raise LifecycleError("offline rebuild is incomplete; API startup remains blocked")
    runtime_quiescence = checkpoint.get("runtime_quiescence")
    if (runtime_quiescence is not None
            and runtime_quiescence.get("phase") != "released"):
        raise LifecycleError("Mihomo runtime must be released with its operation identity before resume")
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
    parser.add_argument("command", choices=("stop", "resume", "transfer", "rebuild-db",
                                             "quiesce-mihomo", "release-mihomo"))
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument("--expected-operation-id")
    parser.add_argument("--expected-current-manifest-sha256")
    parser.add_argument("--expected-new-manifest-sha256")
    parser.add_argument("--file-path", help="snapshot path for the lifecycle-owned database rebuild")
    parser.add_argument("--expected-container-id")
    parser.add_argument("--expected-image-id")
    parser.add_argument("--expected-compose-config-hash")
    parser.add_argument("--expected-compose-file-sha256")
    parser.add_argument("--jit-owner-quiescence", action="store_true",
                        help="release the exact pinned container while NEW API writers remain stopped")
    parser.add_argument("--allow-timer-catchup", action="store_true",
                        help="explicitly allow reactivation of previously active Persistent=true timers")
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        raise LifecycleError("controlled backend lifecycle must run as root")
    if args.allow_timer_catchup and args.command != "resume":
        raise LifecycleError("--allow-timer-catchup applies only to resume")
    if args.jit_owner_quiescence and args.command != "release-mihomo":
        raise LifecycleError("--jit-owner-quiescence applies only to release-mihomo")
    if args.command == "transfer" and not all((args.expected_operation_id,
                                                args.expected_current_manifest_sha256,
                                                args.expected_new_manifest_sha256)):
        raise LifecycleError("transfer requires expected operation id and old/new manifest digests")
    if args.command == "rebuild-db" and not args.file_path:
        raise LifecycleError("rebuild-db requires --file-path")
    if args.command in {"quiesce-mihomo", "release-mihomo"}:
        if not all((args.expected_operation_id, args.expected_current_manifest_sha256,
                    args.expected_container_id, args.expected_image_id,
                    args.expected_compose_config_hash, args.expected_compose_file_sha256)):
            raise LifecycleError(f"{args.command} requires exact operation and runtime identities")
    elif any((args.expected_container_id, args.expected_image_id,
              args.expected_compose_config_hash, args.expected_compose_file_sha256)):
        raise LifecycleError("Mihomo runtime identities apply only to Mihomo lifecycle commands")
    if (args.command == "rebuild-db"
            and Path(os.path.abspath(args.state_dir)) != Path(os.path.abspath(DEFAULT_STATE_DIR))):
        raise LifecycleError("database rebuild lifecycle state directory is fixed")
    if args.command != "rebuild-db" and args.file_path:
        raise LifecycleError("--file-path applies only to rebuild-db")
    if args.command not in {"transfer", "quiesce-mihomo", "release-mihomo"} and any((args.expected_operation_id,
                                             args.expected_current_manifest_sha256,
                                             args.expected_new_manifest_sha256)):
        raise LifecycleError("expected source transition arguments apply only to transfer")
    state_dir = _validate_state_dir(args.state_dir)
    lock_fd = _locked(state_dir)
    try:
        if args.command == "stop":
            return _stop(state_dir)
        if args.command == "quiesce-mihomo":
            return _quiesce_mihomo(args, state_dir)
        if args.command == "release-mihomo":
            return _release_mihomo(args, state_dir)
        if args.command == "transfer":
            return _transfer(args, state_dir)
        if args.command == "rebuild-db":
            return _run_database_rebuild(args, state_dir, lock_fd)
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
