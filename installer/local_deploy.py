#!/usr/bin/env python3
"""Deploy FWRouter through its existing installer and lifecycle owner."""
from __future__ import annotations

import argparse
import ast
import ctypes
from contextlib import contextmanager
from dataclasses import dataclass, replace
import fcntl
import hashlib
import http.client
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
from typing import Callable
import uuid
import zipfile


CONFIG_PATH = Path("/etc/fwrouter/deploy-source.json")
INSTALLER_LOCK_FD = 9
MAINTENANCE_LOCK_FD = 8
UNSAFE_WRITE = stat.S_IWGRP | stat.S_IWOTH
MAX_TREE_ENTRIES = 10000
MAX_OLD_SOURCE_COMMITS = 1000
MAX_SOURCE_ENTRIES = 100000
IDENTITY_NAME = "deployment-identity.json"
MAIN_REPO = "repos/Coxf0rd/fwrouter"
MAIN_ORIGIN = "git@github.com:Coxf0rd/fwrouter.git"
MAIN_WORKFLOW = ".github/workflows/test-gates.yml"
HOST_ROWS = (
    ("host/libexec/fwrouter/fwrouter-boot-preflight.sh",
     "usr/local/libexec/fwrouter/fwrouter-boot-preflight.sh", 0o755),
    ("host/systemd/fwrouter-api.service", "etc/systemd/system/fwrouter-api.service", 0o644),
)


class DeploymentError(RuntimeError):
    pass


class DeploymentIncomplete(DeploymentError):
    pass


def _hex(value: object, size: int) -> bool:
    return (isinstance(value, str) and len(value) == size
            and all(ch in "0123456789abcdef" for ch in value))


def _positive_id(value: object) -> bool:
    return type(value) is int and value > 0


def _valid_qualification(value: object) -> bool:
    return (isinstance(value, dict)
            and set(value) == {"source_sha", "run_id", "run_attempt", "artifact_id", "artifact_digest"}
            and _hex(value["source_sha"], 40)
            and all(_positive_id(value[key]) for key in ("run_id", "run_attempt", "artifact_id"))
            and isinstance(value["artifact_digest"], str) and value["artifact_digest"].startswith("sha256:")
            and _hex(value["artifact_digest"][7:], 64))


@dataclass(frozen=True)
class StableSource:
    root: Path
    qualification: dict

    @property
    def commit(self) -> str:
        return self.qualification["source_sha"]

    def provenance(self) -> dict:
        return {"path": MAIN_ORIGIN, "git_sha": self.commit, "mode": "STABLE",
                "provenance": "verified_actual_main_ci", "qualification_status": "VERIFIED_MAIN_CI",
                "dirty": False, "qualification": self.qualification}


def _emit_progress(message: str) -> None:
    print(message, flush=True)


def _emit_result(value: dict, *, as_json: bool, verbose: bool = False, heading: bool = True) -> None:
    if as_json:
        _emit_progress(json.dumps(value, sort_keys=True))
        return
    status = str(value.get("status", "INCOMPLETE"))
    mode = str(value.get("mode", "UNKNOWN")).upper()
    lines = [f"FWRouter Deployment — {mode}"] if heading else []
    for name, row in value.get("stages", {}).items():
        lines.append(f"[{row['status']}] {name:<12} {row['detail']}")
    for name, row in value.get("health", {}).items():
        lines.append(f"[{row['status']}] {name:<12} {row['detail']}")
    lines.append(f"Result: {value.get('result', status)}")
    if value.get("installed_version"):
        last = (value.get("deployment_identity") or {}).get("last_verified_install") or {}
        installed = last.get("base_git_sha") or last.get("base_sha") or last.get("git_sha") or value["installed_version"]
        suffix = " + local changes" if last.get("dirty") is True else ""
        installed_mode = last.get("mode")
        label = f"Installed {installed_mode}" if installed_mode in {"STABLE", "DEV"} else "Installed version"
        lines.append(f"{label}: {installed[:12]}{suffix}")
    if value.get("previous_version"):
        previous = value.get("previous_base_git_sha") or value["previous_version"]
        lines.append(f"Previous version: {previous[:12]}" +
                     (" + local changes" if value.get("previous_dirty") else ""))
    components = value.get("components")
    if components:
        if components.get("updated"):
            lines.append(f"Updated components: {', '.join(components['updated'])}")
        if components.get("unchanged"):
            lines.append(f"Unchanged components: {', '.join(components['unchanged'])}")
    backup = value.get("backup")
    if isinstance(backup, dict) and backup.get("status"):
        detail = f" · {backup['detail']}" if backup.get("detail") else ""
        lines.append(f"Deployment backup: {backup['status']}{detail}")
    storage = value.get("storage")
    if isinstance(storage, dict):
        stable = storage.get("stable_current") or "UNKNOWN"
        rollback = storage.get("stable_rollback")
        if "stable_rollback" not in storage:
            rollback = stable if storage.get("dev_current") and stable != "UNKNOWN" else storage.get("stable_previous")
        label = "Active Stable" if stable == value.get("installed_version") else "Last accepted Stable"
        lines.append(f"{label}: {stable[:12] if stable != 'UNKNOWN' else stable}")
        dev_current = storage.get("dev_current")
        if isinstance(dev_current, dict):
            dev_sha = dev_current.get("base_git_sha") or dev_current.get("base_sha") or dev_current.get("git_sha") or "UNKNOWN"
            dev_current = dev_sha[:12] + (" (dirty)" if dev_current.get("dirty") else "")
        lines.append(f"Active DEV: {dev_current[:24] if dev_current else 'None'}")
        lines.append(f"Stable rollback: {rollback[:12] if rollback else 'Unavailable'}")
        lines.append(f"Managed storage: {storage.get('managed_bytes', 0)} bytes. "
                     f"Reclaimed: {storage.get('cleanup_reclaimed_bytes', 0)} bytes")
        if storage.get("dev_snapshot_bytes"):
            lines.append(f"DEV snapshot: {storage['dev_snapshot_bytes']} bytes")
        if "temporary_staging_bytes" in storage:
            lines.append(f"Temporary staging: {storage['temporary_staging_bytes']} bytes")
        if "rollback_database_backup" in value:
            lines.append(f"Rollback database backup: {value['rollback_database_backup']}")
        if "dev_baseline_database_backup" in value:
            lines.append(f"DEV baseline database backup: {value['dev_baseline_database_backup']}")
        for retained in storage.get("retained", []):
            name = retained.get("resource") or retained.get("digest", "resource")
            name = name[:12] if _hex(name.removesuffix(".tar"), 64) else name
            lines.append(f"[SKIP] Retained {name}: {retained.get('reason', 'ownership or recovery reference')}")
        for failure in storage.get("cleanup_failures", []):
            name = failure.get("resource") or failure.get("digest", "resource")
            name = name[:12] if _hex(name, 64) else name
            lines.append(f"[FAIL] Cleanup {name}: {failure.get('reason', 'see verbose diagnostics')}")
    if value.get("pending_units"):
        lines.append(f"Pending units: {', '.join(value['pending_units'])}")
    for warning in value.get("warnings", []):
        lines.append(f"Action: {warning}")
    if "duration_seconds" in value:
        lines.append(f"Duration: {value['duration_seconds']:.1f}s")
    if verbose:
        lines.append(json.dumps({key: item for key, item in value.items() if key != "deployment_identity"},
                                sort_keys=True, indent=2))
    _emit_progress("\n".join(lines))


@dataclass(frozen=True)
class Paths:
    """Host paths; target_root is injectable only by this module's test driver."""

    config: Path = CONFIG_PATH
    target_root: Path = Path("/")
    state_dir: Path = Path("/var/lib/fwrouter-v2/state/controlled-rollout")
    owner_uid: int = 0
    test_driver: bool = False
    api_healthy: bool = False
    api_active: bool = False
    runtime_healthy: bool = False
    lifecycle_units_stopped: bool = False
    stable_source: StableSource | None = None
    development_source: dict | None = None
    retire_paths: list[dict] | None = None

    def target(self, relative: str) -> Path:
        return self.target_root / relative


def _load_module(path: Path, name: str):
    # Importing helpers from a selected checkout must not dirty it with pyc files.
    sys.dont_write_bytecode = True
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise DeploymentError(f"cannot load selected source module: {path.name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    if name == "fwrouter_deploy_component_lock":
        sys.path.insert(0, str(path.parent))
    spec.loader.exec_module(module)
    return module


def _git_environment(base: dict[str, str] | None = None) -> dict[str, str]:
    """Return a Git environment bound to this process, never inherited repository/config overrides."""
    env = dict(os.environ if base is None else base)
    for key in tuple(env):
        if key.startswith("GIT_"):
            env.pop(key, None)
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_CONFIG_COUNT": "2", "GIT_CONFIG_KEY_0": "core.fsmonitor",
                "GIT_CONFIG_VALUE_0": "false", "GIT_CONFIG_KEY_1": "core.hooksPath",
                "GIT_CONFIG_VALUE_1": "/dev/null", "GIT_NO_REPLACE_OBJECTS": "1",
                "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"})
    return env


def _run(argv: list[str], *, timeout: int = 30, pass_fds: tuple[int, ...] = (),
         env_overrides: dict[str, str] | None = None,
         clean_git_env: bool = False, host_environment: bool = False) -> subprocess.CompletedProcess:
    try:
        env = ({"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8", "SYSTEMD_COLORS": "0"}
               if host_environment else os.environ.copy())
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        if env_overrides:
            env.update(env_overrides)
        if clean_git_env or (argv and Path(argv[0]).name == "git"):
            # Sanitize after caller overrides too: repository redirection and
            # replacement-object variables must never be reintroduced here.
            env = _git_environment(env)
        return subprocess.run(argv, check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, timeout=timeout, pass_fds=pass_fds, env=env)
    except (OSError, subprocess.SubprocessError) as exc:
        raise DeploymentError(f"command did not complete: {Path(argv[0]).name}") from exc


def _bounded_process(argv: list[str], *, limit: int, timeout: int, destination: Path | None = None) -> bytes | None:
    """Capture a bounded child stream; terminate and reap on overflow or timeout."""
    try:
        process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   env={**os.environ, "GH_HOST": "github.com"})
    except OSError as exc:
        raise DeploymentError(f"command unavailable: {Path(argv[0]).name}") from exc
    assert process.stdout is not None
    selector = selectors.DefaultSelector()
    output: bytearray | None = bytearray() if destination is None else None
    target = None
    try:
        if destination is not None:
            target = destination.open("xb")
        selector.register(process.stdout, selectors.EVENT_READ)
        deadline = time.monotonic() + timeout
        total = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DeploymentError("bounded command timed out")
            ready = selector.select(remaining)
            if not ready:
                raise DeploymentError("bounded command timed out")
            data = os.read(process.stdout.fileno(), min(65536, limit - total + 1))
            if not data:
                selector.unregister(process.stdout)
                break
            total += len(data)
            if total > limit:
                raise DeploymentError("bounded command output exceeded its limit")
            if target is not None:
                target.write(data)
            else:
                assert output is not None
                output.extend(data)
        if target is not None:
            target.flush()
            os.fsync(target.fileno())
        remaining = max(0.01, deadline - time.monotonic())
        try:
            return_code = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired as exc:
            raise DeploymentError("bounded command timed out") from exc
        if return_code != 0:
            raise DeploymentError("bounded command returned failure")
        return bytes(output) if output is not None else None
    except BaseException:
        if process.poll() is None:
            process.kill()
        process.wait()
        raise
    finally:
        selector.close()
        process.stdout.close()
        if target is not None:
            target.close()


def _check_trusted_tree(path: Path, owner_uid: int) -> Path:
    if not path.is_absolute() or ".." in path.parts:
        raise DeploymentError("source path must be absolute and contain no traversal")
    cursor = Path(path.anchor)
    for part in path.parts[1:]:
        cursor = cursor / part
        try:
            info = cursor.lstat()
        except OSError as exc:
            raise DeploymentError("source path is unavailable") from exc
        sticky_root_tmp = cursor == Path("/tmp") and info.st_uid == 0 and stat.S_IMODE(info.st_mode) == 0o1777
        if (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)
                or info.st_uid not in {0, owner_uid}
                or (info.st_mode & UNSAFE_WRITE and not sticky_root_tmp)):
            raise DeploymentError("source path contains an untrusted or writable directory")
    return path


def _validate_source_tree(root: Path, owner_uid: int) -> None:
    """Require trusted, ordinary checkout inputs and local Git metadata before Git reads them."""
    git_dir = root / ".git"
    try:
        info = git_dir.lstat()
    except OSError as exc:
        raise DeploymentError("selected source must be a standard local Git checkout") from exc
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise DeploymentError("selected source must not use a linked Git worktree")
    if (git_dir / "commondir").exists() or (git_dir / "objects/info/alternates").exists():
        raise DeploymentError("selected source must use a self-contained local Git object database")
    stack = [root]
    count = 0
    while stack:
        directory = stack.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError as exc:
            raise DeploymentError("selected source tree cannot be inspected safely") from exc
        for entry in entries:
            count += 1
            if count > MAX_SOURCE_ENTRIES:
                raise DeploymentError("selected source tree exceeds the bounded entry limit")
            try:
                item = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise DeploymentError("selected source tree changed during trust validation") from exc
            if item.st_uid != owner_uid or item.st_mode & UNSAFE_WRITE:
                raise DeploymentError("selected source contains a non-owner-controlled or writable entry")
            if stat.S_ISDIR(item.st_mode):
                stack.append(Path(entry.path))
            elif stat.S_ISREG(item.st_mode) and item.st_nlink == 1:
                continue
            else:
                raise DeploymentError("selected source contains a symlink, hard link or special file")


def _source_commit(root: Path, owner_uid: int) -> str:
    root = _check_trusted_tree(root, owner_uid)
    _validate_source_tree(root, owner_uid)
    top = _run(["/usr/bin/git", "-C", str(root), "rev-parse", "--show-toplevel"])
    if top.returncode != 0 or Path(top.stdout.strip()) != root:
        raise DeploymentError("selected source must be the root of a local Git checkout")
    uid = _run(["/usr/bin/git", "-C", str(root), "rev-parse", "--show-object-format"])
    if uid.returncode != 0 or uid.stdout.strip() != "sha1":
        raise DeploymentError("selected source must use a full SHA-1 Git commit identity")
    head = _run(["/usr/bin/git", "-C", str(root), "rev-parse", "--verify", "HEAD^{commit}"])
    commit = head.stdout.strip()
    if head.returncode != 0 or len(commit) != 40 or any(ch not in "0123456789abcdef" for ch in commit):
        raise DeploymentError("selected source has no valid full commit identity")
    status = _run(["/usr/bin/git", "-C", str(root), "status", "--porcelain=v1",
                   "--untracked-files=all", "--ignored=matching"])
    if status.returncode != 0 or status.stdout:
        raise DeploymentError("selected source checkout is dirty, including ignored files")
    return commit


def _read_config(path: Path, owner_uid: int) -> tuple[Path, str]:
    try:
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != owner_uid
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
                or info.st_size <= 0 or info.st_size > 4096):
            raise DeploymentError("source config must be an owner-only regular file")
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except DeploymentError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DeploymentError("root-controlled source config is unavailable or malformed") from exc
    if (not isinstance(value, dict) or set(value) != {"schema", "source", "commit"}
            or value.get("schema") != "fwrouter-local-deploy-source/v1"
            or not isinstance(value.get("source"), str) or not Path(value["source"]).is_absolute()
            or not isinstance(value.get("commit"), str) or len(value["commit"]) != 40
            or any(ch not in "0123456789abcdef" for ch in value["commit"])):
        raise DeploymentError("root-controlled source config has an invalid shape")
    source = Path(value["source"])
    if _source_commit(source, owner_uid) != value["commit"]:
        raise DeploymentError("selected source commit changed; reselect it explicitly")
    return source, value["commit"]


def _write_config(path: Path, source: Path, commit: str, owner_uid: int) -> None:
    parent = path.parent
    if not parent.exists():
        parent.mkdir(mode=0o700, parents=True)
    current = Path(parent.anchor)
    for part in parent.parts[1:]:
        current = current / part
        info = current.lstat()
        sticky_root_tmp = current == Path("/tmp") and info.st_uid == 0 and stat.S_IMODE(info.st_mode) == 0o1777
        if (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)
                or info.st_uid not in {0, owner_uid}
                or (info.st_mode & UNSAFE_WRITE and not sticky_root_tmp)):
            raise DeploymentError("source config directory is not root-controlled")
    payload = (json.dumps({"schema": "fwrouter-local-deploy-source/v1",
                           "source": str(source), "commit": commit},
                          sort_keys=True, separators=(",", ":")) + "\n").encode()
    fd = -1
    tmp = ""
    try:
        fd, tmp = tempfile.mkstemp(prefix=".deploy-source.", dir=parent)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb", closefd=True) as stream:
            fd = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        tmp = ""
        dfd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError as exc:
        raise DeploymentError("could not durably write selected source config") from exc
    finally:
        if fd >= 0:
            os.close(fd)
        if tmp:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass


def _source_modules(source: Path, paths: Paths, *, engine_source: Path | None = None
                   ) -> tuple[object, object, object]:
    engine = engine_source or source
    installer = engine / "installer"
    lifecycle = _load_module(installer / "controlled_backend_lifecycle.py", "fwrouter_local_deploy_lifecycle")
    lifecycle.SOURCE_BACKEND = source / "backend"
    lifecycle.TARGET_BACKEND = paths.target("opt/fwrouter-api")
    lifecycle.DEFAULT_STATE_DIR = paths.state_dir
    lock = _load_module(engine / "installer/component_install_lock.py", "fwrouter_deploy_component_lock")
    ui = _load_module(source / "installer/ui_publication.py", "fwrouter_local_deploy_ui")
    ui.component_install_lock = lock
    return lifecycle, lock, ui


LIFECYCLE_OWNER_ROOT = "lifecycle-owners"


def _lifecycle_owner_dir(paths: Paths, source_sha: str) -> Path:
    return paths.state_dir / LIFECYCLE_OWNER_ROOT / source_sha


def _sync_lifecycle_owner_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _publish_lifecycle_owner_capsule(paths: Paths, source: Path, source_sha: str,
                                    qualification: dict, updater_sha256: str) -> dict:
    """Persist the exact qualified Stable lifecycle and lock as DEV recovery evidence."""
    if paths.test_driver:
        return {}
    if (not _valid_qualification(qualification) or qualification.get("source_sha") != source_sha
            or _source_commit(source, paths.owner_uid) != source_sha or not _hex(updater_sha256, 64)):
        raise DeploymentIncomplete("Qualified Stable lifecycle owner cannot be pinned to the accepted source.")
    updater = paths.target("usr/local/libexec/fwrouter/local_deploy.py")
    info = updater.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != paths.owner_uid
            or stat.S_IMODE(info.st_mode) != 0o644 or info.st_nlink != 1
            or _digest(updater) != updater_sha256):
        raise DeploymentIncomplete("Installed deployer changed before lifecycle owner pinning.")
    blobs = {}
    relatives = ["installer/controlled_backend_lifecycle.py", "installer/component_install_lock.py"]
    timer_names = ("fwrouter-jobs-retention-dry-run.timer", "fwrouter-maintenance.timer",
                   "fwrouter-subscription-refresh.timer", "fwrouter-traffic-collect.timer")
    relatives.extend(f"host/systemd/{name}" for name in timer_names)
    for relative in relatives:
        payload = _git_blob_bytes(source, source_sha, relative)
        if _read_stable_source_file(source / relative, paths.owner_uid) != payload:
            raise DeploymentError("qualified lifecycle owner helper differs from its exact Stable Git blob")
        blobs[relative] = payload
    lifecycle_blob = blobs["installer/controlled_backend_lifecycle.py"]
    if (b"--restore-timers" not in lifecycle_blob
            or b"_preflight_timer_restore" not in lifecycle_blob):
        raise DeploymentIncomplete("Accepted Stable source has no D2 timer-restoration owner contract.")
    pin = {"qualification": qualification, "updater_sha256": updater_sha256,
           "lifecycle_sha256": hashlib.sha256(lifecycle_blob).hexdigest(),
           "lock_sha256": hashlib.sha256(blobs["installer/component_install_lock.py"]).hexdigest(),
           "timer_sha256": {name: hashlib.sha256(blobs[f"host/systemd/{name}"]).hexdigest()
                            for name in timer_names}}
    owner_root = paths.state_dir / LIFECYCLE_OWNER_ROOT
    owner_root.mkdir(mode=0o700, exist_ok=True)
    root_info = owner_root.lstat()
    if (not stat.S_ISDIR(root_info.st_mode) or stat.S_ISLNK(root_info.st_mode)
            or root_info.st_uid != paths.owner_uid or stat.S_IMODE(root_info.st_mode) != 0o700):
        raise DeploymentError("lifecycle owner capsule directory is not private and owner-controlled")
    target = _lifecycle_owner_dir(paths, source_sha)
    if target.exists():
        # A retry may find a rename that preceded the parent directory sync.
        _verify_lifecycle_owner_files(paths, source_sha, pin)
        _sync_lifecycle_owner_directory(owner_root)
        _sync_lifecycle_owner_directory(paths.state_dir)
        return pin
    staging = Path(tempfile.mkdtemp(prefix=".owner-", dir=owner_root))
    try:
        os.chown(staging, paths.owner_uid, -1)
        os.chmod(staging, 0o700)
        for relative, payload in blobs.items():
            path = staging / relative
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            parent = path.parent
            while parent != staging:
                os.chown(parent, paths.owner_uid, -1)
                os.chmod(parent, 0o700)
                parent = parent.parent
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            os.fchown(fd, paths.owner_uid, -1)
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        for relative in ("host/systemd", "host", "installer", "."):
            _sync_lifecycle_owner_directory(staging / relative)
        os.rename(staging, target)
        _sync_lifecycle_owner_directory(owner_root)
        _sync_lifecycle_owner_directory(paths.state_dir)
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging)
        raise
    _verify_lifecycle_owner_files(paths, source_sha, pin)
    return pin


def _verify_lifecycle_owner_files(paths: Paths, source_sha: str, pin: dict) -> Path:
    root = paths.state_dir / LIFECYCLE_OWNER_ROOT
    directory = _lifecycle_owner_dir(paths, source_sha)
    root_info, dir_info = root.lstat(), directory.lstat()
    if (not stat.S_ISDIR(root_info.st_mode) or root_info.st_uid != paths.owner_uid
            or stat.S_IMODE(root_info.st_mode) != 0o700 or not stat.S_ISDIR(dir_info.st_mode)
            or dir_info.st_uid != paths.owner_uid or stat.S_IMODE(dir_info.st_mode) != 0o700
            or dir_info.st_dev != root_info.st_dev):
        raise DeploymentError("lifecycle owner capsule has unsafe directory ownership")
    expected = {"installer/controlled_backend_lifecycle.py": pin["lifecycle_sha256"],
                "installer/component_install_lock.py": pin["lock_sha256"]}
    expected.update({f"host/systemd/{name}": digest for name, digest in pin["timer_sha256"].items()})
    actual_files, actual_dirs = set(), set()
    for path in directory.rglob("*"):
        relative = path.relative_to(directory).as_posix()
        if path.is_symlink():
            raise DeploymentError("lifecycle owner capsule contains a symlink")
        (actual_dirs if path.is_dir() else actual_files).add(relative)
    if (actual_files != set(expected)
            or actual_dirs != {"installer", "host", "host/systemd"}):
        raise DeploymentError("lifecycle owner capsule contains unknown or missing files")
    for relative in actual_dirs:
        path = directory / relative
        info = path.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != paths.owner_uid
                or stat.S_IMODE(info.st_mode) != 0o700):
            raise DeploymentError("lifecycle owner capsule subdirectory is not private")
    for relative, digest in expected.items():
        path = directory / relative
        item = path.lstat()
        if (not stat.S_ISREG(item.st_mode) or item.st_uid != paths.owner_uid
                or stat.S_IMODE(item.st_mode) != 0o600 or item.st_nlink != 1
                or _digest(path) != digest):
            raise DeploymentError("lifecycle owner capsule content failed its exact digest pin")
    return directory


def _cleanup_lifecycle_owner_capsules(paths: Paths, result: dict) -> None:
    """Remove only verified capsules no longer referenced by either Stable pin."""
    if paths.test_driver:
        return
    storage = result.setdefault("storage", {})
    try:
        with _configuration_component_lock(paths):
            record = _read_identity(paths)
            operation = record.get("operation") or {}
            checkpoint = paths.state_dir / "pending.json"
            checkpoint_pending = False
            if os.path.lexists(checkpoint):
                try:
                    checkpoint_state = _json_object(checkpoint.read_text(encoding="utf-8")).get("status")
                    checkpoint_pending = checkpoint_state != "complete"
                except (OSError, UnicodeError, DeploymentError):
                    checkpoint_pending = True
            if (record.get("stable_admission") or record.get("rollback_admission")
                    or record.get("dev_admission") or operation.get("status") in {"PENDING", "FAILED"}
                    or checkpoint_pending):
                storage.setdefault("retained", []).append(
                    {"resource": "Lifecycle owner capsules", "reason": "active deployment or recovery reference"})
                return
            keep = {row.get("lifecycle_owner_sha", row["source_sha"]) for row in
                    (record.get("stable_current"), record.get("stable_previous")) if row}
            owners = record.get("lifecycle_owners", {})
            reclaimed, changed, failures = 0, False, []
            for sha, pin in list(owners.items()):
                if sha in keep:
                    continue
                directory = _lifecycle_owner_dir(paths, sha)
                try:
                    _verify_lifecycle_owner_files(paths, sha, pin)
                    reclaimed += _tree_bytes(directory)
                    shutil.rmtree(directory)
                except FileNotFoundError:
                    pass
                except (OSError, DeploymentError) as exc:
                    failures.append({"resource": f"Lifecycle owner {sha[:12]}",
                                     "reason": str(exc) if isinstance(exc, DeploymentError)
                                     else type(exc).__name__})
                    continue
                del owners[sha]
                changed = True
            if changed:
                owner_root = paths.state_dir / LIFECYCLE_OWNER_ROOT
                if owner_root.exists():
                    _sync_lifecycle_owner_directory(owner_root)
                _write_identity(paths, record)
                storage["cleanup_reclaimed_bytes"] = storage.get("cleanup_reclaimed_bytes", 0) + reclaimed
            storage.setdefault("cleanup_failures", []).extend(failures)
            if failures:
                storage.setdefault("retained", []).extend(
                    {"resource": row["resource"], "reason": "ownership or digest verification failed"}
                    for row in failures)
    except (OSError, DeploymentError) as exc:
        storage.setdefault("cleanup_failures", []).append(
            {"resource": "Lifecycle owner capsules",
             "reason": str(exc) if isinstance(exc, DeploymentError) else type(exc).__name__})


def _read_lifecycle_owner_capsule(paths: Paths, source_sha: str, *, pin: dict | None = None,
                                 verify_installed_updater: bool = True) -> tuple[dict, Path]:
    identity = _read_identity(paths)
    owners = identity.get("lifecycle_owners") or {}
    pin = pin or owners.get(source_sha)
    stable_match = any(
        row.get("lifecycle_owner_sha", row["source_sha"]) == source_sha
        and ("lifecycle_owner_sha" in row or row["qualification"] == (pin or {}).get("qualification"))
        for row in (identity.get("stable_current"), identity.get("stable_previous")) if row
    )
    if (not isinstance(pin, dict) or not stable_match
            or pin.get("qualification", {}).get("source_sha") != source_sha):
        raise DeploymentIncomplete("A verified lifecycle owner capsule for the current Stable rollback pin is unavailable.")
    updater = paths.target("usr/local/libexec/fwrouter/local_deploy.py")
    try:
        info = updater.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != paths.owner_uid
                or stat.S_IMODE(info.st_mode) != 0o644 or info.st_nlink != 1
                or verify_installed_updater and _digest(updater) != pin["updater_sha256"]):
            raise DeploymentError("installed updater differs from the capsule's pinned engine digest")
        directory = _verify_lifecycle_owner_files(paths, source_sha, pin)
        updater_record = identity.get("components", {}).get("updater") or {}
        if (verify_installed_updater and updater_record.get("installed_sha256") != pin["updater_sha256"]
                or updater_record.get("qualification_status") != "VERIFIED_MAIN_CI"):
            raise DeploymentError("deployment identity does not verify the installed D2 updater owner")
        return pin, directory
    except FileNotFoundError as exc:
        raise DeploymentIncomplete("Pinned Stable lifecycle owner capsule is missing; DEV recovery is unavailable.") from exc


def _materialize_pinned_owner(paths: Paths, source: Path, scratch: Path, *,
                              timer_source: Path | None = None) -> Path:
    identity = _read_identity(paths)
    owners = identity.get("lifecycle_owners") or {}
    source_sha = next((sha for row in (identity.get("stable_current"), identity.get("stable_previous"))
                       if row and (sha := row.get("lifecycle_owner_sha", row["source_sha"])) in owners), None)
    if not _hex(source_sha, 40):
        raise DeploymentIncomplete("No accepted Stable lifecycle owner is available for this operation.")
    _, capsule = _read_lifecycle_owner_capsule(paths, source_sha)
    owner_source = scratch / "lifecycle-owner-source"
    (owner_source / "installer").mkdir(mode=0o700, parents=True)
    (owner_source / "backend").mkdir(mode=0o700)
    for name in ("controlled_backend_lifecycle.py", "component_install_lock.py"):
        shutil.copyfile(capsule / "installer" / name, owner_source / "installer" / name)
        os.chmod(owner_source / "installer" / name, 0o600)
    timer_dir = owner_source / "host/systemd"
    timer_dir.mkdir(mode=0o700, parents=True)
    for name in ("fwrouter-jobs-retention-dry-run.timer", "fwrouter-maintenance.timer",
                 "fwrouter-subscription-refresh.timer", "fwrouter-traffic-collect.timer"):
        timer_path = ((timer_source / "host/systemd" / name) if timer_source is not None
                      else (capsule / "host/systemd" / name))
        if timer_source is not None:
            expected = _git_blob_bytes(timer_source, _source_commit(timer_source, paths.owner_uid),
                                       f"host/systemd/{name}")
            if _read_stable_source_file(timer_path, paths.owner_uid) != expected:
                raise DeploymentError("qualified rollback timer source differs from its exact Git object")
        shutil.copyfile(timer_path, timer_dir / name)
        os.chmod(timer_dir / name, 0o600)
    backend = source / "backend"
    entries = []
    total_bytes = 0
    inode_count = 0
    for directory, dirs, files in os.walk(backend, followlinks=False):
        current = Path(directory)
        inode_count += 1
        for name in dirs:
            if (current / name).is_symlink():
                raise DeploymentError("captured DEV backend contains a symlink directory")
        for name in files:
            child = current / name
            info = child.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise DeploymentError("captured DEV backend contains an unsupported file")
            total_bytes += info.st_size
            inode_count += 1
            entries.append((child, current.relative_to(backend), name, info.st_mode))
    _require_staging_space(scratch, total_bytes, inode_count)
    for directory, dirs, files in os.walk(backend, followlinks=False):
        current = Path(directory)
        relative = current.relative_to(backend)
        target_dir = owner_source / "backend" / relative
        for name in dirs:
            (target_dir / name).mkdir(mode=0o700)
    for child, relative, name, mode in entries:
        target = owner_source / "backend" / relative / name
        shutil.copyfile(child, target)
        os.chmod(target, stat.S_IMODE(mode))
    return owner_source


def _require_development_lifecycle_contract(source: Path, lifecycle) -> None:
    """Require the separately pinned Stable owner contract before service work."""
    owner = source / "installer/controlled_backend_lifecycle.py"
    try:
        owner_text = owner.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise DeploymentIncomplete(
            "Pinned Stable lifecycle owner has no readable D2 timer contract; repair Stable owner evidence before DEV.") from exc
    if (not callable(getattr(lifecycle, "_preflight_timer_restore", None))
            or "--restore-timers" not in owner_text):
        raise DeploymentIncomplete(
            "Pinned Stable lifecycle owner predates D2 automatic timer restoration; repair Stable owner evidence before DEV.")


def _has_d2_lifecycle_contract(source: Path) -> bool:
    try:
        content = (source / "installer/controlled_backend_lifecycle.py").read_bytes()
    except OSError:
        return False
    return b"--restore-timers" in content and b"_preflight_timer_restore" in content


def _git_blob_oid(path: Path) -> str:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise DeploymentError("installed backend contains an unsupported file type")
    digest = hashlib.sha1()
    digest.update(f"blob {info.st_size}\0".encode("ascii"))
    size = 0
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            size += len(block)
            if size > info.st_size:
                raise DeploymentError("installed backend file changed while resolving its source")
            digest.update(block)
    if size != info.st_size:
        raise DeploymentError("installed backend file changed while resolving its source")
    return digest.hexdigest()


def _find_source_commit(source: Path, installed_backend: Path, lifecycle) -> str:
    """Resolve the exact deployed backend from local Git objects without checkout or fetch."""
    expected = {}
    for entry in lifecycle._backend_manifest(installed_backend):
        expected[entry["path"]] = _git_blob_oid(installed_backend / entry["path"])
    history = _run(["/usr/bin/git", "-C", str(source), "rev-list", "--max-count",
                    str(MAX_OLD_SOURCE_COMMITS + 1), "--all", "--", "backend"], timeout=30)
    if history.returncode != 0:
        raise DeploymentError("cannot inspect local backend source history")
    commits = history.stdout.splitlines()
    if len(commits) > MAX_OLD_SOURCE_COMMITS:
        commits = commits[:MAX_OLD_SOURCE_COMMITS]
    deadline = time.monotonic() + 45
    for commit in commits:
        if time.monotonic() > deadline:
            raise DeploymentError("installed source lookup exceeded its bounded local-history scan")
        tree = _run_bytes(["/usr/bin/git", "-C", str(source), "ls-tree", "-r", "-z",
                           "--full-tree", commit, "--", "backend"], timeout=10)
        if tree.returncode != 0:
            continue
        actual = {}
        valid = True
        for item in tree.stdout.split(b"\0"):
            if not item:
                continue
            try:
                facts, raw_path = item.split(b"\t", 1)
                mode, object_type, oid = facts.decode("ascii").split(" ")
                path = raw_path.decode("utf-8")
            except (ValueError, UnicodeError):
                valid = False
                break
            if not path.startswith("backend/") or object_type != "blob" or mode not in {"100644", "100755"}:
                valid = False
                break
            relative = path.removeprefix("backend/")
            if lifecycle._excluded(PurePosixPath(relative)):
                continue
            actual[relative] = oid
            if len(actual) > MAX_TREE_ENTRIES:
                valid = False
                break
        if valid and actual == expected:
            return commit
    raise DeploymentError(
        "installed backend is not an exact source in the local Git history; no service was stopped"
    )


def _run_bytes(argv: list[str], *, timeout: int) -> subprocess.CompletedProcess:
    try:
        env = _git_environment()
        env["GIT_ALLOW_PROTOCOL"] = ""
        return subprocess.run(argv, check=False, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              timeout=timeout, env=env)
    except (OSError, subprocess.SubprocessError) as exc:
        raise DeploymentError("cannot inspect local Git source history") from exc


def _materialize_old_source(source: Path, commit: str, destination: Path) -> Path:
    """Extract only the matched backend and lifecycle helper into a disposable directory."""
    destination.mkdir(mode=0o700)
    process = subprocess.Popen(
        ["/usr/bin/git", "-C", str(source), "archive", commit, "backend",
         "installer/controlled_backend_lifecycle.py"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=_git_environment(),
    )
    assert process.stdout is not None
    count = total = 0
    try:
        with tarfile.open(fileobj=process.stdout, mode="r|") as archive:
            for member in archive:
                count += 1
                if count > MAX_TREE_ENTRIES or member.size > 512 * 1024 * 1024:
                    raise DeploymentError("matched old source exceeds export bounds")
                parts = PurePosixPath(member.name).parts
                if (not parts or parts[0] not in {"backend", "installer"}
                        or any(part in {"", ".", ".."} for part in parts)
                        or (parts[0] == "installer" and parts not in {
                            ("installer",), ("installer", "controlled_backend_lifecycle.py")})):
                    raise DeploymentError("matched old source archive contains an unexpected path")
                target = destination.joinpath(*parts)
                if member.isdir():
                    if parts == ("installer",):
                        target.mkdir(mode=0o700, parents=True, exist_ok=True)
                        continue
                    if parts[0] == "installer":
                        raise DeploymentError("matched old source archive contains an unexpected directory")
                    target.mkdir(mode=0o700, parents=True, exist_ok=True)
                    continue
                if not member.isfile() or parts[0] == "installer" and parts[-1] != "controlled_backend_lifecycle.py":
                    raise DeploymentError("matched old source archive contains a non-regular entry")
                total += member.size
                if total > 512 * 1024 * 1024:
                    raise DeploymentError("matched old source exceeds export bounds")
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                source_stream = archive.extractfile(member)
                if source_stream is None:
                    raise DeploymentError("matched old source archive is incomplete")
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                             0o600)
                with os.fdopen(fd, "wb") as output:
                    while chunk := source_stream.read(1024 * 1024):
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                os.chmod(target, member.mode & 0o700)
        process.stdout.close()
        if process.wait(timeout=10) != 0:
            raise DeploymentError("could not materialize the exact installed source")
    except BaseException:
        process.kill()
        process.wait()
        raise
    helper = destination / "installer/controlled_backend_lifecycle.py"
    if not helper.is_file() or not (destination / "backend").is_dir():
        raise DeploymentError("matched old source export is incomplete")
    return destination


def _materialize_installed_backend_source(paths: Paths, selected_source: Path, lifecycle,
                                          destination: Path) -> Path:
    """Build a temporary owner input from the durably pinned installed backend."""
    manifest = lifecycle._backend_manifest(paths.target("opt/fwrouter-api"))
    digest = lifecycle._manifest_digest(manifest)
    if not _known_installed_backend(paths, digest):
        raise DeploymentError("installed backend has no durable source identity for lifecycle recovery")
    destination.mkdir(mode=0o700)
    backend = destination / "backend"
    backend.mkdir(mode=0o700)
    for entry in manifest:
        rel = PurePosixPath(entry["path"])
        if not rel.parts or any(part in {"", ".", ".."} for part in rel.parts):
            raise DeploymentError("installed backend manifest contains an unsafe path")
        source = paths.target("opt/fwrouter-api").joinpath(*rel.parts)
        target = backend.joinpath(*rel.parts)
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = source.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != paths.owner_uid:
            raise DeploymentError("installed backend changed ownership or type during recovery preparation")
        fd_in = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
        fd_out = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd_in, "rb") as input_stream, os.fdopen(fd_out, "wb") as output_stream:
            shutil.copyfileobj(input_stream, output_stream, 1024 * 1024)
            output_stream.flush()
            os.fsync(output_stream.fileno())
        if _digest(target) != entry["sha256"]:
            raise DeploymentError("installed backend changed while preparing lifecycle recovery")
        os.chmod(target, stat.S_IMODE(info.st_mode))
    helper = destination / "installer"
    helper.mkdir(mode=0o700)
    owner = selected_source / "installer/controlled_backend_lifecycle.py"
    if not owner.is_file() or not callable(getattr(lifecycle, "_preflight_timer_restore", None)):
        raise DeploymentIncomplete("selected lifecycle owner lacks the D2 timer restoration contract")
    shutil.copyfile(owner, helper / owner.name)
    os.chmod(helper / owner.name, 0o600)
    return destination


def _materialize_recoverable_old_source(paths: Paths, selected_source: Path, lifecycle,
                                        destination: Path) -> Path:
    installed = paths.target("opt/fwrouter-api")
    digest = lifecycle._manifest_digest(lifecycle._backend_manifest(installed))
    if _known_installed_backend(paths, digest):
        return _materialize_installed_backend_source(paths, selected_source, lifecycle, destination)
    candidates = []
    if paths.development_source and isinstance(paths.development_source.get("path"), str):
        candidates.append(Path(paths.development_source["path"]))
    if paths.config.exists():
        try:
            configured, _ = _read_config(paths.config, paths.owner_uid)
            candidates.append(configured)
        except DeploymentError:
            pass
    for candidate in dict.fromkeys(candidates):
        try:
            _check_trusted_tree(candidate, paths.owner_uid)
            _validate_source_tree(candidate, paths.owner_uid)
            commit = _find_source_commit(candidate, installed, lifecycle)
            recovered = _materialize_old_source(candidate, commit, destination)
            # Backend bytes come from the exact reconstructed Git object, while
            # lifecycle authority remains the separately pinned qualified owner.
            trusted_owner = selected_source / "installer/controlled_backend_lifecycle.py"
            target_owner = recovered / "installer/controlled_backend_lifecycle.py"
            shutil.copyfile(trusted_owner, target_owner)
            os.chmod(target_owner, 0o600)
            return recovered
        except (DeploymentError, OSError):
            continue
    raise DeploymentIncomplete("The installed OLD backend is UNKNOWN and no trusted local source reconstructs it.")


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def _identity_path(paths: Paths) -> Path:
    return paths.state_dir / IDENTITY_NAME


def _read_identity(paths: Paths) -> dict:
    """Read the single deployment identity record; absence is legacy UNKNOWN."""
    try:
        directory_info = paths.state_dir.lstat()
    except FileNotFoundError:
        return {"status": "UNKNOWN", "reason": "legacy_install_without_identity_record"}
    if (not stat.S_ISDIR(directory_info.st_mode) or stat.S_ISLNK(directory_info.st_mode)
            or directory_info.st_uid != paths.owner_uid or stat.S_IMODE(directory_info.st_mode) != 0o700):
        raise DeploymentError("controlled lifecycle state directory is unsafe")
    path = _identity_path(paths)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {"status": "UNKNOWN", "reason": "legacy_install_without_identity_record"}
    if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or info.st_uid != paths.owner_uid or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1 or info.st_size <= 0 or info.st_size > 128 * 1024):
        raise DeploymentError("deployment identity record has unsafe type, owner, mode or size")
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            opened = os.fstat(fd)
            named = path.lstat()
            if ((opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)
                    or (named.st_dev, named.st_ino) != (info.st_dev, info.st_ino)):
                raise DeploymentError("deployment identity path changed during read")
            raw = bytearray()
            while block := os.read(fd, 65536):
                raw.extend(block)
                if len(raw) > 128 * 1024:
                    raise DeploymentError("deployment identity record exceeds its size bound")
        finally:
            os.close(fd)
        def unique_pairs(pairs):
            result = {}
            for key, item in pairs:
                if key in result:
                    raise ValueError("duplicate key")
                result[key] = item
            return result
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_pairs)
    except DeploymentError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise DeploymentError("deployment identity record is malformed") from exc
    required_keys = {"schema", "last_verified_install", "operation", "components"}
    if (not isinstance(value, dict) or value.get("schema") != "fwrouter-deployment-identity/v1"
            or set(value) - {"stable_admission", "stable_current", "stable_previous", "dev_current", "dev_admission", "dev_retired", "dev_baseline_backup", "rollback_admission", "lifecycle_owners",
                             "database_rollback", "database_rollback_candidate", "database_rollback_retired",
                             "retired_ui_releases"} != required_keys
            or ("stable_admission" in value and not _valid_qualification(value["stable_admission"]))):
        raise DeploymentError("deployment identity record has an invalid shape")
    def valid_digest(item: object) -> bool:
        return (isinstance(item, str) and len(item) == 64
                and all(ch in "0123456789abcdef" for ch in item))

    def validate_content(item: object) -> bool:
        return (isinstance(item, dict) and set(item) == {"backend_sha256", "ui_sha256", "host_pair_sha256"}
                and valid_digest(item["backend_sha256"]) and valid_digest(item["ui_sha256"])
                and isinstance(item["host_pair_sha256"], dict)
                and set(item["host_pair_sha256"]) == {row[0] for row in HOST_ROWS}
                and all(valid_digest(digest) for digest in item["host_pair_sha256"].values()))

    def validate_native(item: object) -> bool:
        if (not isinstance(item, dict) or not isinstance(item.get("scope"), str)
                or not isinstance(item.get("evidence_refs"), list)
                or any(not isinstance(ref, str) for ref in item["evidence_refs"])):
            return False
        return (item.get("status") == "NOT_VERIFIED"
                or (item.get("status") == "PASS" and item["scope"] == "ui"
                    and len(item["evidence_refs"]) == 1 and _hex(item["evidence_refs"][0], 64)))

    def validate_acceptance(item: object) -> bool:
        return (isinstance(item, dict) and (item.get("status") == "NOT_VERIFIED"
                or (item.get("status") == "VERIFIED" and isinstance(item.get("verified_at"), str))))

    def stable_provenance(item: dict) -> bool:
        qualification = item.get("qualification")
        return (_valid_qualification(qualification) and item.get("mode") == "STABLE"
                and item.get("path") == MAIN_ORIGIN and item.get("dirty") is False
                and item.get("provenance") == "verified_actual_main_ci"
                and item.get("qualification_status") == "VERIFIED_MAIN_CI"
                and item.get("git_sha") == qualification["source_sha"])

    def dev_provenance(item: dict) -> bool:
        return (item.get("mode") == "DEV" and item.get("provenance") == "local_development_worktree"
                and item.get("qualification_status") == "NOT_VERIFIED"
                and _hex(item.get("git_sha"), 40) and _hex(item.get("base_git_sha"), 40)
                and isinstance(item.get("dirty"), bool) and valid_digest(item.get("content_sha256"))
                and valid_digest(item.get("snapshot_sha256"))
                and isinstance(item.get("path"), str) and item["path"].startswith("/")
                and isinstance(item.get("snapshot_path"), str)
                and Path(item["snapshot_path"]).name == f"{item['content_sha256']}.tar"
                and Path(item["snapshot_path"]).parent.name == DEV_SOURCE_ROOT)

    components = value["components"]
    if not isinstance(components, dict) or set(components) != {"backend", "ui", "host_pair", "updater"}:
        raise DeploymentError("deployment identity component map is malformed")
    for name, component in components.items():
        if component is None:
            continue
        if (not isinstance(component, dict) or not isinstance(component.get("source_path"), str)
                or not isinstance(component.get("source_sha"), str)
                or len(component["source_sha"]) != 40
                or any(ch not in "0123456789abcdef" for ch in component["source_sha"])
                or not isinstance(component.get("provenance"), str)
                or component.get("provenance") not in {"local_git", "verified_actual_main_ci", "local_development_worktree"}
                or not isinstance(component.get("installed_sha256"), (str, dict))
                or not isinstance(component.get("operation_id"), str)
                or component.get("mode") not in {"UNKNOWN", "STABLE", "DEV"}
                or (component.get("mode") == "STABLE"
                    and (component.get("source_path") != MAIN_ORIGIN
                         or component.get("provenance") != "verified_actual_main_ci"
                         or component.get("qualification_status") != "VERIFIED_MAIN_CI"))
                or (component.get("mode") == "DEV"
                    and (component.get("provenance") != "local_development_worktree"
                         or component.get("qualification_status") != "NOT_VERIFIED"))
                or component.get("qualification_status") not in {"NOT_VERIFIED", "VERIFIED_MAIN_CI"}):
            raise DeploymentError(f"deployment identity {name} component is malformed")
        try:
            if str(uuid.UUID(component["operation_id"])) != component["operation_id"]:
                raise ValueError
        except (ValueError, TypeError, AttributeError) as exc:
            raise DeploymentError(f"deployment identity {name} operation ID is malformed") from exc
        installed_digest = component["installed_sha256"]
        if name == "host_pair":
            if (not isinstance(installed_digest, dict)
                    or set(installed_digest) != {row[0] for row in HOST_ROWS}
                    or any(not valid_digest(digest) for digest in installed_digest.values())):
                raise DeploymentError("deployment identity host-pair component is malformed")
        elif not valid_digest(installed_digest):
            raise DeploymentError(f"deployment identity {name} component digest is malformed")

    last = value["last_verified_install"]
    if last is not None:
        if (not isinstance(last, dict) or not isinstance(last.get("path"), str)
                or not isinstance(last.get("git_sha"), str) or len(last["git_sha"]) != 40
                or any(ch not in "0123456789abcdef" for ch in last["git_sha"])
                or not (stable_provenance(last) or dev_provenance(last)
                        or (last.get("provenance") in {"local_git", "UNKNOWN"}
                            and last.get("qualification_status") == "NOT_VERIFIED"
                            and last.get("mode") == "UNKNOWN" and isinstance(last.get("dirty"), bool)))
                or not validate_content(last.get("source_content"))
                or not validate_content(last.get("installed_content"))
                or last.get("source_content") != last.get("installed_content")
                or not validate_native(last.get("native_proof"))
                or not validate_acceptance(last.get("acceptance"))
                or not isinstance(last.get("operation_id"), str)
                or not isinstance(last.get("completed_at"), str)
                or not isinstance(last.get("observed_runtime_image_id"), dict)
                or set(last["observed_runtime_image_id"]) != {"xray", "mihomo"}
                or any(value != "UNKNOWN" and not (isinstance(value, str) and value.startswith("sha256:")
                       and len(value) == 71 and all(ch in "0123456789abcdef" for ch in value[7:]))
                       for value in last["observed_runtime_image_id"].values())):
            raise DeploymentError("deployment identity last-verified record is malformed")
        try:
            if str(uuid.UUID(last["operation_id"])) != last["operation_id"]:
                raise ValueError
        except (ValueError, TypeError, AttributeError) as exc:
            raise DeploymentError("deployment identity operation ID is malformed") from exc
    operation = value["operation"]
    if operation is not None:
        if not isinstance(operation, dict) or not isinstance(operation.get("status"), str):
            raise DeploymentError("deployment identity operation is malformed")
        if operation.get("kind") == "self_upgrade":
            if (operation.get("status") not in {"PENDING", "COMPLETE"}
                    or not isinstance(operation.get("operation_id"), str)
                    or not valid_digest(operation.get("candidate_module_sha256"))
                    or not valid_digest(operation.get("old_module_sha256"))
                    or not valid_digest(operation.get("wrapper_sha256"))
                    or not isinstance(operation.get("source_sha"), str)
                    or len(operation["source_sha"]) != 40
                    or any(ch not in "0123456789abcdef" for ch in operation["source_sha"])
                    or not validate_acceptance(operation.get("acceptance"))
                    or not isinstance(operation.get("verifier_sources"), list)
                    or len(operation["verifier_sources"]) > 256
                    or any(not isinstance(item, str) or PurePosixPath(item).is_absolute()
                           or any(part in {"", ".", ".."} for part in PurePosixPath(item).parts)
                           for item in operation["verifier_sources"])):
                raise DeploymentError("deployment identity updater transition is malformed")
            try:
                if str(uuid.UUID(operation["operation_id"])) != operation["operation_id"]:
                    raise ValueError
            except (ValueError, TypeError, AttributeError) as exc:
                raise DeploymentError("deployment identity updater operation ID is malformed") from exc
        else:
            requested = operation.get("requested_source")
            if (operation.get("status") not in {"PENDING", "FAILED", "VERIFIED_CONTENT"}
                    or not isinstance(operation.get("operation_id"), str)
                    or not isinstance(requested, dict) or not isinstance(requested.get("path"), str)
                    or not isinstance(requested.get("git_sha"), str) or len(requested["git_sha"]) != 40
                    or any(ch not in "0123456789abcdef" for ch in requested["git_sha"])
                    or not (stable_provenance(requested) or dev_provenance(requested)
                            or (requested.get("mode") == "UNKNOWN" and requested.get("provenance") == "local_git"
                                and requested.get("qualification_status") == "NOT_VERIFIED"
                                and isinstance(requested.get("dirty"), bool)))
                    or not validate_content(operation.get("source_content"))
                    or not validate_native(operation.get("native_proof"))
                    or not validate_acceptance(operation.get("acceptance"))
                    or not isinstance(operation.get("pending_units"), list)
                    or any(not isinstance(unit, str) for unit in operation["pending_units"])
                    or operation.get("pending_units_status") not in {"OBSERVED", "UNKNOWN"}
                    or (operation.get("actual_installed_content") != "UNKNOWN"
                        and operation.get("actual_installed_content") is not None
                        and not validate_content(operation.get("actual_installed_content")))):
                raise DeploymentError("deployment identity install operation is malformed")
            try:
                if str(uuid.UUID(operation["operation_id"])) != operation["operation_id"]:
                    raise ValueError
            except (ValueError, TypeError, AttributeError) as exc:
                raise DeploymentError("deployment identity install operation ID is malformed") from exc
    for pin_name in ("stable_current", "stable_previous"):
        previous = value.get(pin_name)
        if previous is None:
            continue
        common = {"source_sha", "qualification", "installed_content"}
        if (not isinstance(previous, dict) or not common <= set(previous)
                or not _hex(previous["source_sha"], 40)
                or not _valid_qualification(previous["qualification"])
                or previous["qualification"]["source_sha"] != previous["source_sha"]
                or not validate_content(previous["installed_content"])):
            raise DeploymentError("Stable rollback pin is malformed")
        fields = set(previous) - common
        owner_sha = previous.get("lifecycle_owner_sha")
        if "lifecycle_owner_sha" in fields:
            if not _hex(owner_sha, 40):
                raise DeploymentError("Stable rollback lifecycle owner is malformed")
            fields.remove("lifecycle_owner_sha")
        historical = (fields == {"provenance", "native_acceptance"} and owner_sha is not None
                      and previous["provenance"] == "UNKNOWN"
                      and previous["native_acceptance"] == "NOT_VERIFIED")
        accepted = (fields == {"accepted_at", "proof_sha256"}
                    and isinstance(previous["accepted_at"], str) and _hex(previous["proof_sha256"], 64))
        if not historical and not accepted:
            raise DeploymentError("Stable rollback pin verification is malformed")
    lifecycle_owners = value.get("lifecycle_owners", {})
    if (not isinstance(lifecycle_owners, dict) or len(lifecycle_owners) > 4
            or any(not _hex(sha, 40) or not isinstance(pin, dict)
                   or set(pin) != {"qualification", "updater_sha256", "lifecycle_sha256", "lock_sha256", "timer_sha256"}
                   or not _valid_qualification(pin.get("qualification"))
                   or pin["qualification"].get("source_sha") != sha
                   or any(not valid_digest(pin.get(key)) for key in
                          ("updater_sha256", "lifecycle_sha256", "lock_sha256"))
                   or not isinstance(pin.get("timer_sha256"), dict)
                   or set(pin["timer_sha256"]) != {"fwrouter-jobs-retention-dry-run.timer",
                        "fwrouter-maintenance.timer", "fwrouter-subscription-refresh.timer",
                        "fwrouter-traffic-collect.timer"}
                   or any(not valid_digest(digest) for digest in pin["timer_sha256"].values())
                   for sha, pin in lifecycle_owners.items())):
        raise DeploymentError("pinned lifecycle owner inventory is malformed")
    for row in (value.get("stable_current"), value.get("stable_previous")):
        if row and "lifecycle_owner_sha" in row and row["lifecycle_owner_sha"] not in lifecycle_owners:
            raise DeploymentError("pinned Stable rollback lifecycle owner is missing")
    dev_pin = value.get("dev_current")
    if dev_pin is not None and (not isinstance(dev_pin, dict) or not dev_provenance(dev_pin)):
        raise DeploymentError("Development source pin is malformed")
    dev_retired = value.get("dev_retired")
    if dev_retired is not None and (not isinstance(dev_retired, dict) or not dev_provenance(dev_retired)):
        raise DeploymentError("retired Development snapshot pin is malformed")
    dev_admission = value.get("dev_admission")
    if dev_admission is not None:
        if (not isinstance(dev_admission, dict) or not dev_provenance(dev_admission)
                or not isinstance(dev_admission.get("retire_paths"), list)
                or len(dev_admission["retire_paths"]) > MAX_TREE_ENTRIES
                or any(not isinstance(row, dict) or set(row) != {"path", "sha256", "size"}
                       or not isinstance(row.get("path"), str) or not _hex(row.get("sha256"), 64)
                       or type(row.get("size")) is not int or row["size"] < 0
                       for row in dev_admission["retire_paths"])):
            raise DeploymentError("Development recovery admission is malformed")
    if value.get("dev_baseline_backup") is not None and not (
            _valid_managed_backup(value["dev_baseline_backup"], complete=False)
            or _valid_managed_backup(value["dev_baseline_backup"], complete=True)):
        raise DeploymentError("Development baseline database backup pin is malformed")
    rollback = value.get("rollback_admission")
    if rollback is not None and (not isinstance(rollback, dict) or not _valid_qualification(rollback)):
        raise DeploymentError("Stable rollback admission is malformed")
    if rollback is not None and value.get("stable_admission") != rollback:
        raise DeploymentError("Stable rollback admission does not match its source pin")
    retired = value.get("retired_ui_releases", {})
    if (not isinstance(retired, dict) or len(retired) > 128
            or any(not _hex(digest, 64) or not isinstance(pin, list) or len(pin) != 2
                   or any(type(part) is not int or part < 0 for part in pin)
                   for digest, pin in retired.items())):
        raise DeploymentError("retired UI release ownership pins are malformed")
    if value.get("database_rollback") is not None and not _valid_managed_backup(
            value["database_rollback"], complete=True):
        raise DeploymentError("managed database rollback pin is malformed")
    candidate = value.get("database_rollback_candidate")
    if candidate is not None and not (_valid_managed_backup(candidate, complete=False)
                                      or _valid_managed_backup(candidate, complete=True)):
        raise DeploymentError("managed database rollback candidate is malformed")
    retired_backup = value.get("database_rollback_retired")
    if retired_backup is not None and not _valid_managed_backup(retired_backup, complete=True):
        raise DeploymentError("retired database rollback pin is malformed")
    return value


def _write_identity(paths: Paths, value: dict) -> None:
    """Durably replace the record while caller holds the existing component mutex."""
    directory = paths.state_dir
    info = directory.lstat()
    if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or info.st_uid != paths.owner_uid or stat.S_IMODE(info.st_mode) != 0o700):
        raise DeploymentError("controlled lifecycle state directory is unsafe")
    payload = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    if len(payload) > 128 * 1024:
        raise DeploymentIncomplete("deployment identity exceeds its durable recovery bound")
    fd = -1
    temporary = ""
    try:
        fd, temporary = tempfile.mkstemp(prefix=".deployment-identity.", dir=directory)
        os.fchown(fd, paths.owner_uid, -1)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb", closefd=True) as stream:
            fd = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, _identity_path(paths))
        temporary = ""
        dfd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError as exc:
        raise DeploymentError("could not durably write deployment identity") from exc
    finally:
        if fd >= 0:
            os.close(fd)
        if temporary:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def _source_content_identity(source: Path, lifecycle, ui, paths: Paths) -> dict:
    backend = lifecycle._manifest_digest(lifecycle._backend_manifest(source / "backend"))
    ui_digest, _ = ui._walk_tree(source / "ui", owner_uid=paths.owner_uid, require_index=True)
    host = {source_rel: _digest(source / source_rel) for source_rel, _, _ in HOST_ROWS}
    return {"backend_sha256": backend, "ui_sha256": ui_digest, "host_pair_sha256": host}


def _tree_bytes(root: Path) -> int:
    """Bounded advisory size estimate that skips symlinked venv/runtime entries."""
    if not root.exists():
        return 0
    total = 0
    entries = 0
    for directory, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        base = Path(directory)
        for name in list(dirnames):
            info = (base / name).lstat()
            if not stat.S_ISDIR(info.st_mode):
                dirnames.remove(name)
                continue
            entries += 1
        for name in filenames:
            info = (base / name).lstat()
            if not stat.S_ISREG(info.st_mode):
                continue
            total += info.st_size
            entries += 1
            if entries > MAX_SOURCE_ENTRIES:
                raise DeploymentError("storage preflight exceeds the bounded source inventory")
    return total


def _existing_mount_path(path: Path) -> Path:
    candidate = path
    while not candidate.exists():
        if candidate == candidate.parent:
            return candidate
        candidate = candidate.parent
    return candidate


def _storage_preflight(paths: Paths, source: Path, changed: list[str], result: dict) -> None:
    source_bytes = _tree_bytes(source / "backend") + _tree_bytes(source / "ui")
    old_bytes = 0
    target_root = paths.target("opt")
    if "Backend" in changed:
        old_bytes += _tree_bytes(paths.target("opt/fwrouter-api"))
    if "UI" in changed:
        old_bytes += _tree_bytes(paths.target("opt/fwrouter-ui"))
    # Transactional publication can temporarily hold the candidate and the current
    # generation together. Keep a 20% margin for directory entries and metadata.
    required = int((source_bytes + old_bytes) * 1.2) + 16 * 1024 * 1024
    temp_free = __import__("shutil").disk_usage("/tmp").free
    install_free = __import__("shutil").disk_usage(_existing_mount_path(target_root)).free
    database = paths.target("var/lib/fwrouter-v2/fwrouter.db")
    database_bytes = database.stat().st_size if database.exists() and "Backend" in changed else 0
    for suffix in ("-wal", "-shm"):
        sidecar = database.with_name(database.name + suffix)
        if sidecar.exists() and "Backend" in changed:
            database_bytes += sidecar.stat().st_size
    backup_free = __import__("shutil").disk_usage(_existing_mount_path(paths.state_dir)).free
    result["storage"] = {"temporary_required_bytes": source_bytes,
                         "temporary_available_bytes": temp_free,
                         "persistent_required_bytes": required,
                         "persistent_available_bytes": install_free,
                         "database_backup_required_bytes": database_bytes,
                         "database_backup_available_bytes": backup_free,
                         "managed_release_bytes": _tree_bytes(paths.target("opt/fwrouter-api"))
                            + _tree_bytes(paths.target("opt/.fwrouter-ui-releases")),
                         "cleanup_reclaimed_bytes": 0, "retained": []}
    if temp_free < source_bytes:
        raise DeploymentIncomplete(f"temporary storage needs {source_bytes} bytes; /tmp has {temp_free} available")
    if install_free < required:
        raise DeploymentIncomplete(f"deployment needs {required} bytes on the installation filesystem; only {install_free} are available")
    if backup_free < database_bytes:
        raise DeploymentIncomplete(f"rollback backup needs {database_bytes} bytes on the database state filesystem; only {backup_free} are available")


def _installed_content_identity(source: Path, paths: Paths, lifecycle, ui) -> dict:
    backend = lifecycle._manifest_digest(lifecycle._backend_manifest(paths.target("opt/fwrouter-api")))
    ui_target = paths.target("opt/fwrouter-ui")
    active_ui = ui._managed_release(ui_target, paths.target("opt/.fwrouter-ui-releases"), paths.owner_uid)
    ui_digest, _ = ui._walk_tree(active_ui or ui_target, owner_uid=paths.owner_uid, require_index=True)
    host = {}
    for source_rel, target_rel, _ in HOST_ROWS:
        host[source_rel] = _digest(paths.target(target_rel))
    return {"backend_sha256": backend, "ui_sha256": ui_digest, "host_pair_sha256": host}


def _identity_record_base(paths: Paths) -> dict:
    current = _read_identity(paths)
    if current.get("status") == "UNKNOWN":
        return {"schema": "fwrouter-deployment-identity/v1", "last_verified_install": None,
                "operation": None,
                "components": {"backend": None, "ui": None, "host_pair": None, "updater": None}}
    return current


def _new_operation_identity(source: Path, commit: str, contents: dict, operation_id: str,
                            lifecycle_status: str, status: str) -> dict:
    return {"status": status, "operation_id": operation_id, "lifecycle_operation_id": operation_id,
            "lifecycle_status": lifecycle_status, "requested_source": {"path": str(source),
                "git_sha": commit, "mode": "UNKNOWN", "provenance": "local_git",
                "qualification_status": "NOT_VERIFIED", "dirty": False},
            "source_content": contents, "pending_units": [], "pending_units_status": "UNKNOWN",
            "native_proof": {
                "status": "NOT_VERIFIED", "scope": "not_run_d1", "evidence_refs": []},
            "acceptance": {"status": "NOT_VERIFIED", "reason": "native_proof_not_integrated_in_d1"}}


def _record_operation(paths: Paths, lock_module, source: Path, commit: str, lifecycle, ui,
                     operation_id: str, lifecycle_status: str, status: str,
                     pending_units: list[str] | None = None, *, lock_held: bool = False,
                     details: dict | None = None) -> None:
    lock_fd = None
    if not lock_held:
        lock_fd, _ = lock_module.acquire(paths.target("opt"), maintenance_mode="shared")
    try:
        _assert_requested_source(paths, source, commit)
        record = _identity_record_base(paths)
        previous = record.get("operation")
        if isinstance(previous, dict) and previous.get("status") in {"PENDING", "FAILED"}:
            if (previous.get("kind") == "self_upgrade"
                    or previous.get("lifecycle_operation_id") != operation_id
                    or previous.get("requested_source", {}).get("git_sha") != commit):
                raise DeploymentError("deployment identity fence changed before operation publication")
        contents = _source_content_identity(source, lifecycle, ui, paths)
        operation = _new_operation_identity(source, commit, contents, operation_id,
                                            lifecycle_status, status)
        if paths.stable_source is not None:
            operation["requested_source"] = paths.stable_source.provenance()
            operation["native_proof"]["scope"] = "owner_readback_required"
            operation["acceptance"]["reason"] = "native_proof_requires_independent_runtime_pins"
        elif paths.development_source is not None:
            operation["requested_source"] = {**paths.development_source, "git_sha": commit,
                "mode": "DEV", "qualification_status": "NOT_VERIFIED"}
            operation["native_proof"]["scope"] = "owner_readback_required"
            operation["acceptance"]["reason"] = "development_source_is_not_stable_qualified"
        operation["pending_units"] = pending_units or []
        operation["pending_units_status"] = "OBSERVED" if pending_units is not None else "UNKNOWN"
        if details:
            operation.update(details)
        if paths.retire_paths is not None:
            operation["retire_paths"] = paths.retire_paths
        if status in {"FAILED", "PENDING"}:
            try:
                operation["actual_installed_content"] = _installed_content_identity(
                    source, paths, lifecycle, ui)
            except Exception:
                operation["actual_installed_content"] = "UNKNOWN"
        if status == "VERIFIED_CONTENT" and lifecycle_status == "complete":
            installed = _installed_content_identity(source, paths, lifecycle, ui)
            if installed != contents:
                operation["status"] = "FAILED"
                operation["failure"] = "installed_content_changed_before_identity_publication"
                record["operation"] = operation
                _write_identity(paths, record)
                raise DeploymentError("installed content changed before deployment identity publication")
            record["last_verified_install"] = {
                **operation["requested_source"], "source_content": contents,
                "installed_content": installed, "operation_id": operation_id,
                "lifecycle_operation_id": operation_id, "completed_at": time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "observed_runtime_image_id": _observed_runtime_image_ids(paths),
                "native_proof": operation["native_proof"], "acceptance": operation["acceptance"]}
            if paths.development_source is not None:
                old_dev = record.get("dev_current")
                new_dev = operation["requested_source"]
                if isinstance(old_dev, dict) and old_dev.get("snapshot_sha256") != new_dev.get("snapshot_sha256"):
                    record["dev_retired"] = old_dev
                record["dev_current"] = operation["requested_source"]
            operation["completed_at"] = record["last_verified_install"]["completed_at"]
            provenance = operation["requested_source"]
            component_origin = {key: provenance[key] for key in ("provenance", "mode", "qualification_status")}
            source_base = (operation["requested_source"].get("path")
                           if operation["requested_source"].get("mode") == "DEV" else None)
            for name, source_rel, digest_key in (
                    ("backend", "backend", "backend_sha256"),
                    ("ui", "ui", "ui_sha256")):
                record["components"][name] = {"source_path": MAIN_ORIGIN if paths.stable_source else
                    str(Path(source_base) / source_rel) if source_base else str(source / source_rel),
                    "source_sha": commit, **component_origin, "installed_sha256": installed[digest_key],
                    "operation_id": operation_id}
            record["components"]["host_pair"] = {"source_path": MAIN_ORIGIN if paths.stable_source else
                str(Path(source_base) / "host") if source_base else str(source / "host"),
                "source_sha": commit, **component_origin, "installed_sha256": installed["host_pair_sha256"],
                "operation_id": operation_id}
            updater_source = source / "installer/local_deploy.py"
            updater_installed = paths.target("usr/local/libexec/fwrouter/local_deploy.py")
            if updater_source.is_file() and updater_installed.is_file():
                updater_digest = _digest(updater_installed)
                if updater_digest == _digest(updater_source):
                    prior_updater = record["components"].get("updater")
                    if not isinstance(prior_updater, dict) or prior_updater.get("installed_sha256") != updater_digest:
                        record["components"]["updater"] = {"source_path": MAIN_ORIGIN if paths.stable_source else str(updater_source),
                            "source_sha": commit, **component_origin, "installed_sha256": updater_digest,
                            "operation_id": operation_id}
            if paths.stable_source and not record.get("rollback_admission"):
                record.pop("stable_admission", None)
        record["operation"] = operation
        _write_identity(paths, record)
    finally:
        if lock_fd is not None:
            lock_module.release(lock_fd)


def _observed_runtime_image_ids(paths: Paths) -> dict:
    if paths.test_driver or not Path("/usr/bin/docker").is_file():
        return {"xray": "UNKNOWN", "mihomo": "UNKNOWN"}
    observed = {}
    for component in ("xray", "mihomo"):
        try:
            result = _run(["/usr/bin/docker", "--host", "unix:///var/run/docker.sock",
                           "inspect", "--format", "{{.State.Running}} {{.Image}}", f"fwrouter-{component}"],
                          timeout=10, host_environment=True)
            fields = result.stdout.strip().split()
            value = fields[1] if result.returncode == 0 and len(fields) == 2 and fields[0] == "true" else ""
        except (DeploymentError, UnicodeError):
            value = ""
        observed[component] = (value if value.startswith("sha256:") and len(value) == 71
                               and all(ch in "0123456789abcdef" for ch in value[7:]) else "UNKNOWN")
    return observed


def _assert_requested_source(paths: Paths, source: Path, commit: str) -> None:
    if paths.stable_source is not None:
        selected = paths.stable_source
        if (source != selected.root or commit != selected.commit
                or _read_identity(paths).get("stable_admission") != selected.qualification
                or _source_commit(source, paths.owner_uid) != commit):
            raise DeploymentError("qualified Stable source or durable admission changed during deployment")
        return
    if paths.development_source is not None:
        identity = _read_identity(paths)
        if identity.get("stable_admission") is not None:
            raise DeploymentError("rerun --stable to complete the admitted Stable operation")
        admission = identity.get("dev_admission")
        if isinstance(admission, dict) and (admission.get("git_sha") != commit
                or admission.get("snapshot_sha256") != paths.development_source.get("snapshot_sha256")):
            raise DeploymentError("Development recovery admission changed before source publication")
        operation = identity.get("operation") if identity.get("status") != "UNKNOWN" else None
        if isinstance(operation, dict) and operation.get("status") in {"PENDING", "FAILED"}:
            requested = operation.get("requested_source", {})
            if (requested.get("mode") != "DEV" or requested.get("git_sha") != commit
                    or requested.get("snapshot_sha256") != paths.development_source.get("snapshot_sha256")):
                raise DeploymentError("unfinished deployment must resume from its captured Development snapshot")
        if _source_commit(source, paths.owner_uid) != commit:
            raise DeploymentError("captured Development source changed before publication")
        return
    if paths.test_driver and not paths.config.exists():
        return
    identity = _read_identity(paths)
    if identity.get("stable_admission") is not None:
        raise DeploymentError("rerun --stable to complete the interrupted Stable deployment")
    if identity.get("dev_admission") is not None:
        raise DeploymentError("rerun --dev to complete the interrupted Development deployment")
    configured_source, configured_commit = _read_config(paths.config, paths.owner_uid)
    if configured_source != source or configured_commit != commit:
        raise DeploymentError("selected source configuration changed during deployment")


def _known_installed_backend(paths: Paths, digest: str) -> bool:
    """Check a backend against durable Stable/DEV identity, independent of source checkout."""
    identity = _read_identity(paths)
    rows = [identity.get("last_verified_install"), identity.get("stable_current"),
            identity.get("stable_previous")]
    operation = identity.get("operation")
    if isinstance(operation, dict):
        rows.append(operation.get("actual_installed_content"))
    for row in rows:
        content = row.get("installed_content") if isinstance(row, dict) else None
        if ((isinstance(content, dict) and content.get("backend_sha256") == digest)
                or (isinstance(row, dict) and row.get("backend_sha256") == digest)):
            return True
    return False


def _installed_source_reconstruction_required(paths: Paths) -> bool:
    last = _read_identity(paths).get("last_verified_install") or {}
    return paths.development_source is not None or last.get("mode") == "DEV"


def _prepare_backend_retirements(paths: Paths, source: Path, lifecycle) -> list[dict]:
    """Pin source-owned backend removals before stopping lifecycle units."""
    selected = lifecycle._backend_manifest(source / "backend")
    selected_paths = {entry["path"] for entry in selected}
    installed = lifecycle._backend_manifest(paths.target("opt/fwrouter-api"))
    installed_digest = lifecycle._manifest_digest(installed)
    if not _known_installed_backend(paths, installed_digest):
        recovered = False
        candidates = []
        if paths.development_source and isinstance(paths.development_source.get("path"), str):
            candidates.append(Path(paths.development_source["path"]))
        if paths.config.exists():
            try:
                configured, _ = _read_config(paths.config, paths.owner_uid)
                candidates.append(configured)
            except DeploymentError:
                pass
        for candidate in dict.fromkeys(candidates):
            try:
                _check_trusted_tree(candidate, paths.owner_uid)
                _validate_source_tree(candidate, paths.owner_uid)
                _find_source_commit(candidate, paths.target("opt/fwrouter-api"), lifecycle)
                recovered = True
                break
            except (DeploymentError, OSError):
                continue
        if not recovered and {entry["path"] for entry in installed} - selected_paths:
            raise DeploymentIncomplete("Installed backend has unknown ownership; cannot safely retire extra source files.")
        if not recovered and not {entry["path"] for entry in installed}.issubset(selected_paths):
            raise DeploymentIncomplete("Installed backend has unknown ownership; cannot safely proceed with DEV.")
        if not recovered:
            return []
    return [{"path": item["path"], "sha256": item["sha256"], "size": item["size"]}
            for item in installed if item["path"] not in selected_paths]


def _retire_installed_backend_files(paths: Paths, source: Path, lifecycle,
                                   retirement: list[dict]) -> None:
    if not retirement:
        return
    selected = {entry["path"] for entry in lifecycle._backend_manifest(source / "backend")}
    plan = {item["path"]: item for item in retirement}
    current = lifecycle._backend_manifest(paths.target("opt/fwrouter-api"))
    for entry in current:
        name = entry["path"]
        if name in selected:
            continue
        pinned = plan.get(name)
        if pinned is None or entry["sha256"] != pinned["sha256"] or entry["size"] != pinned["size"]:
            raise DeploymentError("installed backend contains an unpinned extra file; refusing DEV retirement")
    for name, pinned in plan.items():
        rel = PurePosixPath(name)
        if rel.is_absolute() or any(part in {"", ".", ".."} for part in rel.parts):
            raise DeploymentError("backend retirement plan contains an unsafe path")
        target = paths.target("opt/fwrouter-api").joinpath(*rel.parts)
        try:
            info = target.lstat()
        except FileNotFoundError:
            continue
        if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or info.st_uid != paths.owner_uid or info.st_size != pinned["size"]
                or _digest(target) != pinned["sha256"]):
            raise DeploymentError("backend retirement target changed from its pinned source identity")
        target.unlink()
        parent = target.parent
        backend_root = paths.target("opt/fwrouter-api")
        while parent != backend_root:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent


def _record_development_acceptance(paths: Paths, proof: dict, lifecycle, ui,
                                  source: Path, commit: str) -> None:
    if proof.get("status") != "PASS" or proof.get("scope") != "ui" or not _hex(proof.get("proof_sha256"), 64):
        raise DeploymentError("Development runtime proof does not contain verified evidence")
    if paths.development_source is None:
        raise DeploymentError("Development acceptance requires the admitted snapshot")
    with _configuration_component_lock(paths):
        installed = _installed_content_identity(source, paths, lifecycle, ui)
        record = _identity_record_base(paths)
        operation = record.get("operation") or {}
        last = record.get("last_verified_install") or {}
        requested = operation.get("requested_source") or {}
        admission = record.get("dev_admission")
        if (operation.get("status") != "VERIFIED_CONTENT"
                or requested.get("mode") != "DEV" or requested.get("git_sha") != commit
                or last.get("mode") != "DEV" or last.get("git_sha") != commit
                or last.get("installed_content") != installed
                or any(requested.get(key) != value for key, value in paths.development_source.items())
                or (admission is not None and any(admission.get(key) != value
                    for key, value in paths.development_source.items()))):
            raise DeploymentError("Development identity changed before runtime acceptance publication")
        if last.get("acceptance", {}).get("status") == "VERIFIED" and admission is None:
            return
        acceptance = {"status": "VERIFIED", "verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        evidence = {"status": "PASS", "scope": "ui", "evidence_refs": [proof["proof_sha256"]]}
        for target in (operation, last):
            target["native_proof"] = evidence
            target["acceptance"] = acceptance
        record.pop("dev_admission", None)
        _write_identity(paths, record)


def _cleanup_development_snapshots(paths: Paths, result: dict) -> None:
    directory = _dev_source_directory(paths)
    storage = result.setdefault("storage", {})
    try:
        with _configuration_component_lock(paths):
            record = _read_identity(paths)
            retired = record.get("dev_retired")
            current = record.get("dev_current")
            admission = record.get("dev_admission")
            operation = record.get("operation") or {}
            protected = {item["snapshot_path"] for item in
                         (current, admission, operation.get("requested_source"))
                         if isinstance(item, dict) and item.get("snapshot_path")}
            for anchor in sorted(protected):
                storage.setdefault("retained", []).append(
                    {"resource": Path(anchor).name, "reason": "current or pending DEV recovery reference"})
            if retired is None or retired["snapshot_path"] in protected:
                return
            capsule = Path(retired["snapshot_path"])
            if capsule.parent != directory or capsule.name != f"{retired['content_sha256']}.tar":
                raise DeploymentError("retired DEV snapshot is outside the owned snapshot directory")
            reclaimed = 0
            try:
                info = capsule.lstat()
            except FileNotFoundError:
                pass
            else:
                root = directory.lstat()
                if (not stat.S_ISDIR(root.st_mode) or root.st_uid != paths.owner_uid
                        or stat.S_IMODE(root.st_mode) != 0o700 or not stat.S_ISREG(info.st_mode)
                        or info.st_uid != paths.owner_uid or stat.S_IMODE(info.st_mode) != 0o600
                        or info.st_nlink != 1 or _backup_file_digest(capsule, info) != retired["snapshot_sha256"]):
                    raise DeploymentError("retired DEV snapshot ownership could not be verified")
                reclaimed = info.st_blocks * 512
                capsule.unlink()
            try:
                fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            except FileNotFoundError:
                pass
            else:
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            record.pop("dev_retired")
            _write_identity(paths, record)
            storage["cleanup_reclaimed_bytes"] = storage.get("cleanup_reclaimed_bytes", 0) + reclaimed
    except (OSError, DeploymentError) as exc:
        storage.setdefault("cleanup_failures", []).append(
            {"resource": "DEV snapshots", "reason": str(exc) if isinstance(exc, DeploymentError)
             else type(exc).__name__})


def _remember_development_admission(paths: Paths, source: dict, previous: dict,
                                    acknowledged: bool) -> None:
    with _configuration_component_lock(paths):
        record = _identity_record_base(paths)
        if record.get("stable_admission") is not None:
            raise DeploymentIncomplete("Stable admission appeared before DEV recovery reservation")
        current = record.get("last_verified_install") or {}
        admitted = record.get("dev_admission")
        if admitted is not None:
            if admitted != source:
                raise DeploymentIncomplete("A different DEV snapshot is already admitted; resume it first")
            return
        if current != previous:
            raise DeploymentIncomplete("Installed identity changed during DEV preparation; repeat the command.")
        if current.get("mode") != "DEV" and not acknowledged:
            raise DeploymentIncomplete("The active runtime is Stable. Rerun with --acknowledge-stable-replacement.")
        capsule = Path(source["snapshot_path"])
        info = capsule.lstat()
        if (capsule.parent != _dev_source_directory(paths) or not stat.S_ISREG(info.st_mode)
                or info.st_uid != paths.owner_uid or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1 or _backup_file_digest(capsule, info) != source["snapshot_sha256"]):
            raise DeploymentError("Development snapshot changed before durable admission")
        record["dev_admission"] = source
        _write_identity(paths, record)


def _development_deploy_inner(paths: Paths, result: dict, *, source_path: Path,
                              acknowledge_stable_replacement: bool, owner_driver, system_driver,
                              progress) -> None:
    identity = _read_identity(paths)
    if identity.get("stable_admission") is not None:
        raise DeploymentIncomplete("A Stable operation is pending; rerun --stable to complete it first.")
    operation = identity.get("operation") if identity.get("status") != "UNKNOWN" else None
    admission = identity.get("dev_admission")
    pending_dev = (operation if isinstance(operation, dict)
                  and operation.get("status") in {"PENDING", "FAILED"}
                  and operation.get("requested_source", {}).get("mode") == "DEV" else None)
    if isinstance(operation, dict) and operation.get("status") in {"PENDING", "FAILED"} and pending_dev is None:
        raise DeploymentIncomplete("Another deployment operation is pending; resume it before selecting DEV.")

    last = identity.get("last_verified_install") or {}
    current_mode = last.get("mode", "UNKNOWN")
    replacing_stable = current_mode != "DEV" and admission is None
    if replacing_stable and not acknowledge_stable_replacement:
        raise DeploymentIncomplete("DEV will replace the currently installed runtime. Rerun with --acknowledge-stable-replacement.")
    if replacing_stable:
        result["previous_version"] = last.get("git_sha")
        progress("Acknowledged: DEV will replace the active Stable runtime; the qualified Stable rollback pin is retained")

    policy = _require_acceptance_policy(paths)
    _stage(result, "Runtime images", "SKIP" if paths.test_driver else "PASS",
           "disposable target" if paths.test_driver else "running image identities match local configuration")
    if pending_dev is not None:
        metadata = dict(pending_dev["requested_source"])
        metadata["retire_paths"] = operation.get("retire_paths", [])
    elif admission is not None:
        metadata = dict(admission)
    else:
        if identity.get("dev_retired") is not None:
            _cleanup_development_snapshots(paths, result)
            if result.get("storage", {}).get("cleanup_failures"):
                raise DeploymentIncomplete("An older DEV snapshot could not be safely retired; resolve cleanup before another DEV deployment.")
        progress("Capturing immutable DEV source snapshot")
        with _configuration_component_lock(paths):
            metadata = _dev_snapshot_capture(source_path, paths)
        if metadata.pop("_created_by_capture", False):
            result["_dev_unadmitted_snapshot"] = dict(metadata)
    if not isinstance(metadata.get("snapshot_path"), str):
        raise DeploymentError("Development snapshot reference is unavailable")
    if pending_dev is not None:
        progress("Resuming captured Development snapshot")
    else:
        progress("Captured immutable Development snapshot")

    with _materialize_development_snapshot(paths, metadata) as (source, commit, scratch):
        retire_paths = metadata.pop("retire_paths", None)
        dev_source = {**metadata, "git_sha": commit, "mode": "DEV",
                      "qualification_status": "NOT_VERIFIED"}
        if not paths.test_driver and replacing_stable and current_mode == "UNKNOWN" and not identity.get("stable_current"):
            progress("Checking the installed Stable recovery baseline")
            _adopt_legacy_dev_baseline(paths, source, identity, result)
            identity = _read_identity(paths)
        engine_source = (source if paths.test_driver
                         else _materialize_pinned_owner(paths, source, scratch))
        if retire_paths is None:
            provisional = replace(paths, development_source=dev_source)
            retire_paths = _prepare_backend_retirements(provisional, source,
                (_source_modules(source, provisional) if engine_source == source
                 else _source_modules(source, provisional, engine_source=engine_source))[0])
        if (not isinstance(retire_paths, list) or len(retire_paths) > MAX_TREE_ENTRIES
                or any(not isinstance(row, dict) or set(row) != {"path", "sha256", "size"}
                       or not isinstance(row.get("path"), str) or not _hex(row.get("sha256"), 64)
                       or type(row.get("size")) is not int or row["size"] < 0
                       for row in retire_paths)):
            raise DeploymentError("Development recovery retirement plan is malformed")
        selected = replace(paths, development_source=dev_source, retire_paths=retire_paths)
        lifecycle, lock_module, ui = (_source_modules(source, selected) if engine_source == source
                                      else _source_modules(source, selected, engine_source=engine_source))
        _require_development_lifecycle_contract(engine_source, lifecycle)
        result["source_commit"] = metadata["base_git_sha"]
        result["source"] = {key: metadata[key] for key in
                             ("path", "base_git_sha", "dirty", "content_sha256", "file_count")}
        result["source"]["snapshot_sha256"] = metadata["snapshot_sha256"]
        progress(f"Source {metadata['path']} @ {metadata['base_git_sha'][:12]}"
                 + (" (dirty)" if metadata["dirty"] else " (clean)"))
        _stage(result, "Source", "PASS", f"{metadata['path']} @ {metadata['base_git_sha'][:12]}"
               + (" (dirty)" if metadata["dirty"] else "") + "; immutable snapshot verified")
        _stage(result, "CI", "SKIP", "CI qualification is not required for DEV")

        status = (check(selected, source, commit) if engine_source == source
                  else check(selected, source, commit, engine_source=engine_source))
        facts = _facts(source, selected, lifecycle, ui)
        changed = [name for name, same in zip(("Backend", "UI", "Host files"), facts[:3]) if not same]
        result["components"] = {"updated": [], "unchanged": [name for name in
                                  ("Backend", "UI", "Host files") if name not in changed]}
        unhealthy_current = status == "UNHEALTHY_CURRENT_SOURCE"
        deploying = status not in {"NOOP", "UNHEALTHY_CURRENT_SOURCE"}
        admitted_source = {**dev_source, "retire_paths": retire_paths}
        publication_error = None
        if deploying:
            progress("Checking DEV preflight and lifecycle recovery")
            _storage_preflight(selected, source, changed, result)
            _strict_database_preflight(paths, lifecycle, source)
            if "Backend" in changed:
                _verify_dependency_compatibility(paths, source)
            if not paths.test_driver:
                checkpoint_path = paths.state_dir / "pending.json"
                checkpoint = lifecycle._read_checkpoint(checkpoint_path) if checkpoint_path.exists() else {}
                active = (checkpoint["active_units"] if checkpoint.get("status") == "pending" else
                          {unit: lifecycle._is_active(unit) for unit in lifecycle.PERSISTENT_TIMERS})
                lifecycle._preflight_timer_restore(source, active)
            _stage(result, "Storage", "PASS", "temporary, installation and schema-27 recovery space verified")
            _stage(result, "Preflight", "PASS", "shared lifecycle owner and timer restoration are ready")
            if "Backend" in changed:
                installed_digest = lifecycle._manifest_digest(
                    lifecycle._backend_manifest(paths.target("opt/fwrouter-api")))
                if not _known_installed_backend(paths, installed_digest):
                    with _managed_staging_workspace(paths) as recovery_scratch:
                        _materialize_recoverable_old_source(paths, engine_source, lifecycle,
                                                            recovery_scratch / "old-source")
            _remember_development_admission(selected, admitted_source, last,
                                            acknowledge_stable_replacement)
            if "Backend" in changed:
                _ensure_dev_baseline_backup(selected, lifecycle, _read_acceptance_policy(paths), result)
            else:
                result["backup"] = {"status": "NOT_REQUIRED", "detail": "no backend change requires a database recovery snapshot"}
            backup_status = result.get("backup", {}).get("status")
            _stage(result, "Backup", "PASS" if backup_status in {"CREATED", "REUSED", "VERIFIED"}
                   else "SKIP" if backup_status in {"SKIP", "NOT_REQUIRED"} else "INCOMPLETE",
                   result.get("backup", {}).get("detail") or "database recovery status unavailable")
            if "UI" in changed:
                _pin_retired_ui_release(selected, ui, expected_admission=admitted_source, development=True)
            result.pop("_dev_unadmitted_snapshot", None)
            progress(f"Applying DEV snapshot from base {metadata['base_git_sha'][:12]} through shared lifecycle")
            try:
                outcome = apply(selected, source, commit, owner_driver=owner_driver,
                                system_driver=system_driver,
                                **({} if engine_source == source else {"engine_source": engine_source}))
                result["install_status"] = outcome["status"]
                result["pending_units"] = outcome.get("pending_units", [])
                result["components"]["updated"] = changed
                result["components"]["unchanged"] = [name for name in ("Backend", "UI", "Host files")
                                                        if name not in changed]
            except (DeploymentError, RuntimeError, OSError, ValueError) as exc:
                publication_error = str(exc) if isinstance(exc, (DeploymentError, RuntimeError)) else type(exc).__name__
                result["warnings"].append("Inspect the lifecycle checkpoint and rerun --dev to resume the admitted snapshot.")
                result["install_error"] = publication_error
                result["pending_units"] = []
        else:
            _stage(result, "Storage", "SKIP", "content is unchanged")
            _stage(result, "Backup", "SKIP", "no database migration or compatibility change")
            _stage(result, "Preflight", "SKIP", "content NOOP; no component publication")
            if not unhealthy_current and (identity.get("dev_current") != dev_source
                    or last.get("mode") != "DEV"):
                _remember_development_admission(selected, admitted_source, last,
                                                acknowledge_stable_replacement)
                op_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"fwrouter-dev:{commit}:{metadata['content_sha256']}"))
                _record_operation(selected, lock_module, source, commit, lifecycle, ui, op_id,
                                  "complete", "VERIFIED_CONTENT", [], details={"kind": "development_deploy"})

        installed = _installed_content_identity(source, selected, lifecycle, ui)
        matches = installed == _source_content_identity(source, lifecycle, ui, selected)
        _stage(result, "Install", "FAIL" if publication_error else
               "PASS" if matches and deploying else "SKIP" if matches else "FAIL",
               publication_error or
               (", ".join(changed) + " updated" if changed else "installed component files already match"))
        result["installed_content"] = "VERIFIED" if matches else "FAILED"
        _stage(result, "Identity", "PASS" if matches else "FAIL",
               "installed component content matches the immutable DEV snapshot" if matches
               else "installed content differs from captured DEV source")
        _report_lifecycle(result, paths, lifecycle)
        if paths.test_driver and "Timers" not in result["stages"]:
            _stage(result, "Timers", "INCOMPLETE", "systemd timer state is intentionally not observed in a disposable fixture")
        progress("Verifying DEV health and native runtime acceptance")
        result["health"] = _read_health(paths, lifecycle, policy)
        proof = (_native_acceptance(paths, lifecycle, installed["backend_sha256"], policy)
                 if matches and not publication_error
                 else {"status": "NOT_VERIFIED", "reason": "installed content verification failed"})
        result["native_proof"] = proof
        _stage(result, "Native proof", {"PASS": "PASS", "FAIL": "FAIL", "NOT_VERIFIED": "INCOMPLETE"}[proof["status"]],
               proof.get("reason") or "fenced runtime acceptance verified")
        states = {row["status"] for row in (*result["stages"].values(), *result["health"].values())}
        acceptance_ready = (proof["status"] == "PASS" and matches
                            and all(row["status"] == "PASS" for row in result["health"].values())
                            and not result.get("pending_units")
                            and all(result["stages"].get(name, {}).get("status") == "PASS"
                                    for name in ("Lifecycle", "Timers", "Identity", "Native proof")))
        if acceptance_ready and not unhealthy_current:
            _record_development_acceptance(selected, proof, lifecycle, ui, source, commit)
        if "FAIL" in states:
            result["result"] = "FAILED"
            result["warnings"].append("Resolve the failed health or installed-content check before repeating deployment.")
        elif "INCOMPLETE" in states:
            result["result"] = "INCOMPLETE"
        else:
            result["result"] = ("INCOMPLETE" if unhealthy_current and any(
                row["status"] == "INCOMPLETE" for row in result["health"].values()) else
                "FAILED" if unhealthy_current else
                "SUCCESS" if deploying or last.get("mode") != "DEV" else "NOOP")
        result["status"] = result["result"]
        result["mode"] = "DEV"
        result["acceptance"] = {"status": "VERIFIED" if acceptance_ready else "NOT_VERIFIED"}
        _installed_version(result, _read_identity(paths))
        result.setdefault("backup", {"status": "NOT_REQUIRED", "detail": "database schema remains 27"})
        if result["result"] in {"SUCCESS", "NOOP"}:
            _cleanup_retired_ui(paths, ui, result)
            _cleanup_development_snapshots(paths, result)
            if not paths.test_driver:
                cleanup = _cleanup_expired_staging(paths)
                result.setdefault("storage", {})["cleanup_reclaimed_bytes"] = (
                    result["storage"].get("cleanup_reclaimed_bytes", 0) + cleanup["reclaimed_bytes"])
                result["storage"].setdefault("cleanup_failures", []).extend(cleanup["failures"])
        _refresh_storage_summary(paths, result, lifecycle=lifecycle)


def _development_deploy(paths: Paths, result: dict, *, source_path: Path,
                        acknowledge_stable_replacement: bool, owner_driver, system_driver,
                        progress) -> None:
    try:
        _development_deploy_inner(paths, result, source_path=source_path,
                                  acknowledge_stable_replacement=acknowledge_stable_replacement,
                                  owner_driver=owner_driver, system_driver=system_driver, progress=progress)
    finally:
        metadata = result.pop("_dev_unadmitted_snapshot", None)
        if isinstance(metadata, dict) and isinstance(metadata.get("snapshot_path"), str):
            try:
                with _configuration_component_lock(paths):
                    identity = _read_identity(paths)
                    operation = identity.get("operation") or {}
                    refs = [identity.get("dev_current"), identity.get("dev_retired"),
                            identity.get("dev_admission"), operation.get("requested_source")]
                    referenced = any(isinstance(item, dict)
                                     and item.get("snapshot_path") == metadata["snapshot_path"] for item in refs)
                    capsule = Path(metadata["snapshot_path"])
                    if (not referenced and capsule.parent == _dev_source_directory(paths)
                            and capsule.name == f"{metadata.get('content_sha256')}.tar"):
                        info = capsule.lstat()
                        if (not stat.S_ISREG(info.st_mode) or info.st_uid != paths.owner_uid
                                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
                                or _backup_file_digest(capsule, info) != metadata.get("snapshot_sha256")):
                            raise DeploymentError("unadmitted DEV snapshot ownership could not be verified")
                        capsule.unlink()
            except FileNotFoundError:
                pass
            except (OSError, DeploymentError) as exc:
                result.setdefault("storage", {}).setdefault("cleanup_failures", []).append(
                    {"resource": "unadmitted DEV snapshot", "reason": str(exc) if isinstance(
                        exc, DeploymentError) else type(exc).__name__})


@contextmanager
def _configuration_component_lock(paths: Paths, *, command: bool = False):
    """Acquire the CLI transaction mutex or the shared component mutex."""
    parent = paths.target("opt")
    parent_info = parent.lstat()
    if (not stat.S_ISDIR(parent_info.st_mode) or stat.S_ISLNK(parent_info.st_mode)
            or parent_info.st_uid not in {0, paths.owner_uid}
            or parent_info.st_mode & UNSAFE_WRITE):
        raise DeploymentError("component lock directory is unsafe")
    lock_path = parent / (".fwrouter-deploy-command.lock" if command else ".fwrouter-component-install.lock")
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except OSError as exc:
        raise DeploymentError("cannot safely open the existing component mutex") from exc
    info = os.fstat(fd)
    try:
        named = lock_path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != paths.owner_uid
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
                or stat.S_ISLNK(named.st_mode) or (info.st_dev, info.st_ino) != (named.st_dev, named.st_ino)):
            raise DeploymentError("existing component mutex has unsafe type, owner, mode or links")
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _gh_api(endpoint: str, *, output_limit: int = 4 * 1024 * 1024) -> dict:
    if not Path("/usr/bin/gh").is_file():
        raise DeploymentError("GitHub qualification requires /usr/bin/gh")
    try:
        raw = _bounded_process(["/usr/bin/gh", "api", "--hostname", "github.com", endpoint],
                               limit=output_limit, timeout=30)
    except DeploymentError as exc:
        raise DeploymentError("GitHub API metadata query failed or exceeded its bound") from exc
    try:
        value = json.loads(raw or b"")
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise DeploymentError("GitHub API returned malformed metadata") from exc
    if not isinstance(value, dict):
        raise DeploymentError("GitHub API returned an invalid metadata shape")
    return value


def _gh_artifact_zip(artifact_id: str, destination: Path, *, expected_size: int,
                     size_limit: int = 128 * 1024 * 1024) -> None:
    if not isinstance(expected_size, int) or expected_size <= 0 or expected_size > size_limit:
        raise DeploymentError("GitHub evidence artifact size is invalid or exceeds its bound")
    _require_staging_space(destination.parent, expected_size, 1)
    command = ["/usr/bin/gh", "api", "--hostname", "github.com", "-H",
               "Accept: application/vnd.github+json",
               f"repos/Coxf0rd/fwrouter/actions/artifacts/{artifact_id}/zip"]
    try:
        _bounded_process(command, limit=min(size_limit, expected_size), timeout=120,
                         destination=destination)
    except DeploymentError:
        raise DeploymentError("GitHub evidence artifact download failed or exceeded its bound")
    if destination.stat().st_size != expected_size:
        raise DeploymentError("downloaded evidence artifact size differs from GitHub metadata")


def _git_blob_bytes(source: Path, commit: str, relative: str, *, limit: int = 16 * 1024 * 1024) -> bytes:
    try:
        result = subprocess.run(["/usr/bin/git", "-C", str(source), "cat-file", "blob",
                                 f"{commit}:{relative}"], check=False, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, timeout=20, env=_git_environment())
    except (OSError, subprocess.SubprocessError) as exc:
        raise DeploymentError("cannot read exact candidate bytes from the pinned Git object") from exc
    if result.returncode != 0 or len(result.stdout) > limit:
        raise DeploymentError("pinned Git object is unavailable or exceeds its bound")
    return result.stdout


def _read_stable_source_file(path: Path, owner_uid: int, *, limit: int = 16 * 1024 * 1024) -> bytes:
    try:
        before = path.lstat()
        if (not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode)
                or before.st_uid != owner_uid or before.st_nlink != 1 or before.st_size > limit):
            raise DeploymentError("candidate source file has an unsafe type, owner or size")
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            opened = os.fstat(fd)
            if ((opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
                    or opened.st_size != before.st_size):
                raise DeploymentError("candidate source file changed before exact-byte verification")
            chunks = bytearray()
            while block := os.read(fd, min(1024 * 1024, limit - len(chunks) + 1)):
                chunks.extend(block)
                if len(chunks) > limit:
                    raise DeploymentError("candidate source file exceeds its byte limit")
            after = os.fstat(fd)
            if ((after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                    != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)):
                raise DeploymentError("candidate source file changed during exact-byte verification")
            return bytes(chunks)
        finally:
            os.close(fd)
    except OSError as exc:
        raise DeploymentError("cannot read candidate source file safely") from exc


def _verify_candidate_tree(source: Path, commit: str, owner_uid: int) -> list[str]:
    verifier_path = source / "tests/gates/evidence_bundle.py"
    trusted_verifier = _git_blob_bytes(source, commit, "tests/gates/evidence_bundle.py")
    if _read_stable_source_file(verifier_path, owner_uid) != trusted_verifier:
        raise DeploymentError("trusted evidence verifier differs from its exact Git blob")
    try:
        syntax = ast.parse(trusted_verifier, filename="trusted tests/gates/evidence_bundle.py")
        node = next(item for item in syntax.body if isinstance(item, (ast.Assign, ast.AnnAssign))
                    and any(isinstance(target, ast.Name) and target.id == "SOURCE_FILES"
                            for target in (item.targets if isinstance(item, ast.Assign) else [item.target])))
        value_node = node.value
        files = ast.literal_eval(value_node)
    except (OSError, SyntaxError, StopIteration, ValueError) as exc:
        raise DeploymentError("trusted evidence verifier source inventory is unavailable") from exc
    if (not isinstance(files, tuple) or not files or any(not isinstance(item, str) for item in files)
            or len(files) > 256 or len(files) != len(set(files))):
        raise DeploymentError("trusted evidence verifier source inventory is invalid")
    required = {"tests/gates/evidence_bundle.py", "tests/gates/parallel_ci.py", "tests/gates/gate.py",
                "tests/gates/aggregate_hosted.py", "tests/gates/manifest.json"}
    if not required.issubset(files):
        raise DeploymentError("trusted verifier does not bind its required source files")
    for relative in files:
        rel = PurePosixPath(relative)
        if (rel.is_absolute() or any(part in {"", ".", ".."} for part in rel.parts)
                or "\\" in relative or ":" in rel.parts[0]):
            raise DeploymentError("trusted verifier source inventory contains an unsafe path")
        candidate = source / relative
        expected = _git_blob_bytes(source, commit, relative)
        if _read_stable_source_file(candidate, owner_uid) != expected:
            raise DeploymentError("trusted verifier source differs from the exact Git commit")
    return list(files)


def _extract_evidence_zip(archive_path: Path, destination: Path) -> None:
    max_entries, max_bytes = 6000, 512 * 1024 * 1024
    total = 0
    seen: set[str] = set()
    try:
        with zipfile.ZipFile(archive_path) as archive:
            entries = archive.infolist()
            if not entries or len(entries) > max_entries:
                raise DeploymentError("evidence artifact entry count is outside its bound")
            expanded = sum(item.file_size for item in entries)
            if expanded > max_bytes:
                raise DeploymentError("evidence artifact exceeds its expanded-size bound")
            _require_staging_space(destination if destination.exists() else destination.parent, expanded, len(entries) * 2)
            for item in entries:
                name = item.filename
                clean = name[:-1] if name.endswith("/") else name
                path = PurePosixPath(clean)
                mode = item.external_attr >> 16
                if (not clean or path.is_absolute() or "\\" in clean
                        or any(part in {"", ".", ".."} for part in path.parts)
                        or ":" in path.parts[0]
                        or clean in seen or stat.S_ISLNK(mode)):
                    raise DeploymentError("evidence artifact contains an unsafe or duplicate path")
                seen.add(clean)
                if item.is_dir():
                    (destination / Path(*path.parts)).mkdir(mode=0o700, parents=True, exist_ok=True)
                    continue
                if stat.S_IFMT(mode) not in {0, stat.S_IFREG} or item.file_size > max_bytes:
                    raise DeploymentError("evidence artifact contains a non-regular or oversized entry")
                total += item.file_size
                if total > max_bytes:
                    raise DeploymentError("evidence artifact exceeds its expanded-size bound")
                target = destination.joinpath(*path.parts)
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                with archive.open(item) as source_stream, target.open("xb") as output:
                    copied = 0
                    while block := source_stream.read(1024 * 1024):
                        copied += len(block)
                        if copied > item.file_size:
                            raise DeploymentError("evidence artifact member expanded beyond its declared size")
                        output.write(block)
                    if copied != item.file_size:
                        raise DeploymentError("evidence artifact member size did not match its declaration")
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        if isinstance(exc, DeploymentError):
            raise
        raise DeploymentError("evidence artifact is malformed") from exc


def _matches_self_upgrade_transition(operation: object, *, status: str, operation_id: str,
                                    source_sha: str, run_id: str, run_attempt: str,
                                    artifact_id: str, artifact_digest: str, old_digest: str,
                                    candidate_digest: str, wrapper_digest: str) -> bool:
    return (isinstance(operation, dict) and operation.get("kind") == "self_upgrade"
            and operation.get("status") == status and operation.get("operation_id") == operation_id
            and operation.get("source_sha") == source_sha and operation.get("run_id") == run_id
            and operation.get("run_attempt") == run_attempt and operation.get("artifact_id") == artifact_id
            and operation.get("artifact_digest") == artifact_digest
            and operation.get("old_module_sha256") == old_digest
            and operation.get("candidate_module_sha256") == candidate_digest
            and operation.get("wrapper_sha256") == wrapper_digest)


def _successful_main_jobs(value: dict) -> bool:
    rows = value.get("jobs")
    if (not isinstance(rows, list) or not rows or value.get("total_count") != len(rows)
            or any(not isinstance(job, dict) or job.get("status") != "completed"
                   or job.get("conclusion") != "success" for job in rows)):
        return False
    required = [job for job in rows if job.get("name") == "Affected L0-L5 with hosted profiles"]
    return len(required) == 1


def _main_run_matches(run: dict, sha: str, run_id: int, attempt: int) -> bool:
    return (run.get("id") == run_id and run.get("run_attempt") == attempt
            and run.get("event") == "push" and run.get("head_branch") == "main"
            and run.get("head_sha") == sha and run.get("status") == "completed"
            and run.get("conclusion") == "success" and run.get("path") == MAIN_WORKFLOW
            and isinstance(run.get("repository"), dict)
            and run["repository"].get("full_name") == "Coxf0rd/fwrouter")


def _main_artifact_matches(artifact: dict, qualification: dict) -> bool:
    sha = qualification["source_sha"]
    run_id, attempt = qualification["run_id"], qualification["run_attempt"]
    workflow_run = artifact.get("workflow_run")
    return (artifact.get("id") == qualification["artifact_id"] and artifact.get("expired") is False
            and artifact.get("name") == f"fwrouter-main-evidence-{sha}-{run_id}-{attempt}"
            and artifact.get("digest") == qualification["artifact_digest"]
            and isinstance(workflow_run, dict) and workflow_run.get("id") == run_id
            and workflow_run.get("head_sha") == sha and workflow_run.get("head_branch") == "main")


def _verify_main_bundle(source: Path, scratch: Path, artifact: dict, qualification: dict) -> None:
    sha = qualification["source_sha"]
    run_id, attempt = str(qualification["run_id"]), str(qualification["run_attempt"])
    archive_path = scratch / "main-evidence.zip"
    _gh_artifact_zip(str(qualification["artifact_id"]), archive_path, expected_size=artifact.get("size_in_bytes"))
    if f"sha256:{_digest(archive_path)}" != qualification["artifact_digest"]:
        raise DeploymentError("downloaded evidence artifact differs from its GitHub API digest")
    bundle_path = scratch / "bundle"
    bundle_path.mkdir(mode=0o700)
    _extract_evidence_zip(archive_path, bundle_path)
    result = _run(["/usr/bin/python3", "-I", "-B", str(source / "tests/gates/evidence_bundle.py"),
                   "verify", "--bundle", str(bundle_path), "--expected-sha", sha, "--event", "push",
                   "--ref", "refs/heads/main", "--run-id", run_id, "--attempt", attempt, "--require-main"],
                  timeout=180, clean_git_env=True)
    if result.returncode != 0:
        raise DeploymentError("exact-Main trusted evidence verifier rejected the downloaded artifact")
    try:
        verified = json.loads(result.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError) as exc:
        raise DeploymentError("trusted evidence verifier returned no qualification result") from exc
    expected = {"status": "evidence_verified_not_deploy_authorized", "source_sha": sha,
                "run_id": run_id, "run_attempt": attempt, "strict_aggregate": "passed",
                "offline_source_checkout": "verified"}
    if not isinstance(verified, dict) or any(verified.get(key) != value for key, value in expected.items()):
        raise DeploymentError("trusted evidence verifier result does not match Main qualification")


def _api_rows(reply: dict, key: str) -> list[dict]:
    rows = reply.get(key)
    if (not isinstance(rows, list) or reply.get("total_count") != len(rows)
            or any(not isinstance(row, dict) for row in rows)):
        raise DeploymentError(f"GitHub {key} response is incomplete or paginated")
    return rows


def _current_main() -> str:
    sha = _gh_api(f"{MAIN_REPO}/commits/main").get("sha")
    if not _hex(sha, 40):
        raise DeploymentError("GitHub returned no valid origin/main SHA")
    return sha


def _stable_qualification(source: Path, sha: str, scratch: Path, owner_uid: int, *,
                          historical_head: str | None = None) -> dict:
    runs = _api_rows(_gh_api(
        f"{MAIN_REPO}/actions/workflows/test-gates.yml/runs?branch=main&event=push&head_sha={sha}&per_page=100"),
        "workflow_runs")
    if not runs:
        raise DeploymentIncomplete("Release Acceptance has no run for the exact origin/main SHA")
    if any(not _positive_id(row.get("id")) or row.get("head_sha") != sha for row in runs):
        raise DeploymentError("Release Acceptance run listing does not match the selected Main SHA")
    run_id = max(row["id"] for row in runs)
    run_endpoint = f"{MAIN_REPO}/actions/runs/{run_id}"
    run = _gh_api(run_endpoint)
    attempt = run.get("run_attempt")
    if not _positive_id(attempt) or not _main_run_matches(run, sha, run_id, attempt):
        if run.get("head_sha") == sha and run.get("status") in {"queued", "in_progress", "waiting", "pending", "requested"}:
            raise DeploymentIncomplete("Release Acceptance has not completed for the exact origin/main SHA")
        raise DeploymentError("Release Acceptance did not pass exact-SHA push-to-Main qualification")
    jobs_endpoint = f"{run_endpoint}/attempts/{attempt}/jobs?per_page=100"
    if not _successful_main_jobs(_gh_api(jobs_endpoint)):
        raise DeploymentError("Release Acceptance jobs are incomplete, failed, skipped or paginated")
    artifacts = _api_rows(_gh_api(f"{run_endpoint}/artifacts?per_page=100"), "artifacts")
    expected_name = f"fwrouter-main-evidence-{sha}-{run_id}-{attempt}"
    matches = [row for row in artifacts if row.get("name") == expected_name]
    if not matches:
        raise DeploymentIncomplete("Release Acceptance is missing the exact-run sealed Main evidence artifact")
    if len(matches) != 1:
        raise DeploymentError("Release Acceptance returned ambiguous sealed Main evidence")
    qualification = {"source_sha": sha, "run_id": run_id, "run_attempt": attempt,
                     "artifact_id": matches[0].get("id"), "artifact_digest": matches[0].get("digest")}
    if not _valid_qualification(qualification):
        raise DeploymentError("Release Acceptance artifact has no valid immutable identity")
    artifact_endpoint = f"{MAIN_REPO}/actions/artifacts/{qualification['artifact_id']}"
    artifact = _gh_api(artifact_endpoint)
    if artifact.get("expired") is True:
        raise DeploymentIncomplete("Release Acceptance evidence has expired for the selected Main SHA")
    if not _main_artifact_matches(artifact, qualification):
        raise DeploymentError("Release Acceptance artifact metadata does not match the exact Main run")
    _verify_candidate_tree(source, sha, owner_uid)
    _verify_main_bundle(source, scratch, artifact, qualification)
    if (_current_main() != (historical_head or sha) or not _main_run_matches(_gh_api(run_endpoint), sha, run_id, attempt)
            or not _successful_main_jobs(_gh_api(jobs_endpoint))
            or not _main_artifact_matches(_gh_api(artifact_endpoint), qualification)):
        raise DeploymentError("Main or Release Acceptance changed during source qualification; rerun --stable")
    return qualification


@contextmanager
def _temporary_main(paths: Paths, recovery: dict | None, additionally_fetch_sha: str | None = None):
    with _managed_staging_workspace(paths) as scratch:
        source = scratch / "source"
        sha = recovery["source_sha"] if recovery else _current_main()
        ref = sha if recovery else "refs/heads/main"

        def git(*args: str, timeout: int = 30) -> str:
            result = _run(["/usr/bin/git", "-C", str(source), *args], timeout=timeout)
            if result.returncode != 0:
                raise DeploymentError(f"cannot {args[0]} the exact Stable source from GitHub")
            return result.stdout.strip()

        source.mkdir(mode=0o700)
        git("init", "--quiet", "--template=")
        # The live Main evidence verifier replays history-dependent checks.
        # Exact, previously admitted recovery uses pinned source content.
        shallow_args = ("--depth=1",) if recovery is not None and additionally_fetch_sha else ()
        git("fetch", "--quiet", "--no-tags", "--no-recurse-submodules", *shallow_args,
            MAIN_ORIGIN, ref, timeout=300)
        if git("rev-parse", "FETCH_HEAD^{commit}") != sha:
            raise DeploymentError("origin/main advanced during fetch; rerun --stable")
        if additionally_fetch_sha and additionally_fetch_sha != sha:
            if not _hex(additionally_fetch_sha, 40):
                raise DeploymentError("the accepted current Stable source pin is malformed")
            result = _run(["/usr/bin/git", "-C", str(source), "fetch", "--quiet", "--no-tags",
                           "--no-recurse-submodules", "--depth=1", MAIN_ORIGIN,
                           f"{additionally_fetch_sha}:refs/fwrouter/old-owner"], timeout=300)
            if result.returncode or git("rev-parse", "refs/fwrouter/old-owner^{commit}") != additionally_fetch_sha:
                raise DeploymentIncomplete("the accepted current source is unavailable for rollback ownership checks")
        git("checkout", "--quiet", "--detach", sha, timeout=90)
        if _source_commit(source, paths.owner_uid) != sha:
            raise DeploymentError("temporary Stable tree differs from the exact Main commit")
        qualification = recovery or _stable_qualification(source, sha, scratch, paths.owner_uid)
        if recovery:
            _verify_candidate_tree(source, sha, paths.owner_uid)
        yield StableSource(source, qualification)


def _dev_git(source: Path, *args: str) -> bytes:
    result = _run_bytes(["/usr/bin/git", "-C", str(source), *args], timeout=40)
    if result.returncode != 0 or len(result.stdout) > 16 * 1024 * 1024:
        raise DeploymentError("cannot read the bounded Development Git inventory")
    return result.stdout


def _dev_inventory(source: Path) -> tuple[str, dict[bytes, bytes], dict[bytes, bytes], set[bytes]]:
    def entries(raw: bytes, *, index: bool) -> dict[bytes, bytes]:
        rows = {}
        for entry in raw.split(b"\0")[:-1]:
            meta, name = entry.split(b"\t", 1)
            mode, second, third = meta.split()
            if mode not in {b"100644", b"100755"} or name in rows or (index and third != b"0"):
                raise DeploymentError("Development source has a link, submodule or unresolved index entry")
            rows[name] = mode + b" " + (second if index else third)
        return rows

    sha = _dev_git(source, "rev-parse", "--verify", "HEAD^{commit}").strip().decode("ascii")
    if not _hex(sha, 40):
        raise DeploymentError("Development checkout must have a full SHA-1 HEAD")
    base = entries(_dev_git(source, "ls-tree", "-rz", sha), index=False)
    index = entries(_dev_git(source, "ls-files", "--stage", "-z"), index=True)
    untracked = set(_dev_git(source, "ls-files", "--others", "--exclude-standard", "-z").split(b"\0")[:-1])
    if len(set(index) | untracked) > MAX_TREE_ENTRIES:
        raise DeploymentError("Development source exceeds the bounded file count")
    return sha, base, index, untracked


def _dev_read_file(root: int, name: bytes, remaining: int,
                   owner_uid: int | None = None) -> tuple[bytes, str, int] | None:
    parts = name.split(b"/")
    if any(part in {b"", b".", b"..", b".git"} for part in parts):
        raise DeploymentError("Development file inventory contains an unsafe path")
    flags = os.O_RDONLY | os.O_NOFOLLOW
    parent = os.dup(root)
    try:
        try:
            for part in parts[:-1]:
                child = os.open(part, flags | os.O_DIRECTORY, dir_fd=parent)
                os.close(parent)
                parent = child
            fd = os.open(parts[-1], flags | os.O_NONBLOCK, dir_fd=parent)
        except FileNotFoundError:
            return None
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if (not stat.S_ISREG(before.st_mode) or before.st_size > remaining
                    or (owner_uid is not None and (before.st_uid != owner_uid or before.st_mode & UNSAFE_WRITE))):
                raise DeploymentError("Development file has an unsafe type or exceeds the capture bound")
            git_hash = hashlib.sha1(f"blob {before.st_size}\0".encode(), usedforsecurity=False)
            content_hash = hashlib.sha256()
            size = 0
            while block := stream.read(65536):
                size += len(block)
                if size > before.st_size:
                    raise DeploymentError("Development source changed while reading a file")
                git_hash.update(block)
                content_hash.update(block)
            after = os.fstat(stream.fileno())
            named = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
            def fence(info):
                return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_mode
            if size != before.st_size or fence(before) != fence(after) or fence(before) != fence(named):
                raise DeploymentError("Development source changed while reading a file")
            mode = b"100755" if before.st_mode & stat.S_IXUSR else b"100644"
            return mode + b" " + git_hash.hexdigest().encode(), content_hash.hexdigest(), size
    finally:
        os.close(parent)


def _dev_content(source: Path, index: dict, untracked: set,
                 owner_uid: int | None = None) -> tuple[dict, str]:
    root = os.open(source, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        files = {}
        digest = hashlib.sha256(b"fwrouter-development-source/v1\n")
        remaining = 512 * 1024 * 1024
        for name in sorted(set(index) | untracked):
            actual = _dev_read_file(root, name, remaining, owner_uid)
            if actual is None:
                if name in untracked:
                    raise DeploymentError("Development source changed while collecting untracked files")
                continue
            git_identity, sha256, size = actual
            files[name] = git_identity
            remaining -= size
            digest.update(git_identity[:6] + b" " + sha256.encode() + b" " + name + b"\0")
        return files, digest.hexdigest()
    finally:
        os.close(root)


def _development_source(path: Path) -> dict:
    top = _dev_git(path, "rev-parse", "--show-toplevel").decode().rstrip("\n")
    source = Path(top).resolve(strict=True)
    inventory = _dev_inventory(source)
    sha, base, index, untracked = inventory
    captured = _dev_content(source, index, untracked)
    if captured != _dev_content(source, index, untracked) or inventory != _dev_inventory(source):
        raise DeploymentError("Development source changed during capture; retry with a stable working tree")
    files, digest = captured
    return {"path": str(source), "git_sha": sha, "dirty": base != files or base != index,
            "provenance": "local_development_worktree", "content_sha256": digest,
            "file_count": len(files)}


DEV_SOURCE_ROOT = "dev-snapshots"
DEV_SOURCE_PREFIXES = (b"backend/", b"ui/", b"host/", b"installer/")
DEV_SOURCE_MAX_BYTES = 512 * 1024 * 1024


def _dev_source_directory(paths: Paths) -> Path:
    return paths.state_dir / DEV_SOURCE_ROOT


def _dev_snapshot_capture(source: Path, paths: Paths) -> dict:
    """Capture only installer-owned source trees and durably retain the exact archive."""
    top = _dev_git(source, "rev-parse", "--show-toplevel").decode().rstrip("\n")
    source = Path(top).resolve(strict=True)
    _check_trusted_tree(source, paths.owner_uid)
    inventory = _dev_inventory(source)
    sha, base, index, untracked = inventory
    selected_index = {name: value for name, value in index.items()
                      if name.startswith(DEV_SOURCE_PREFIXES)}
    selected_base = {name: value for name, value in base.items()
                     if name.startswith(DEV_SOURCE_PREFIXES)}
    selected_untracked = {name for name in untracked if name.startswith(DEV_SOURCE_PREFIXES)}
    entries = sorted(set(selected_index) | selected_untracked)
    with _managed_staging_workspace(paths) as scratch:
        free = shutil.disk_usage(scratch).free
        reserve = 16 * 1024 * 1024
        if free <= reserve:
            raise DeploymentIncomplete("Temporary storage is insufficient for a DEV snapshot; free space under /tmp.")
        tree = scratch / "tree"
        tree.mkdir(mode=0o700)
        root_fd = os.open(source, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        digest = hashlib.sha256(b"fwrouter-development-source/v1\n")
        files = {}
        total = 0
        try:
            for name in entries:
                actual = _dev_read_file(root_fd, name, DEV_SOURCE_MAX_BYTES - total, paths.owner_uid)
                if actual is None:
                    if name in selected_untracked:
                        raise DeploymentError("Development source changed while capturing an untracked file")
                    continue
                git_identity, content_hash, size = actual
                files[name] = git_identity
                total += size
                if total * 2 + len(files) * 4096 + reserve > free:
                    raise DeploymentIncomplete("Temporary storage is insufficient for a DEV snapshot; free space under /tmp.")
                digest.update(git_identity[:6] + b" " + content_hash.encode() + b" " + name + b"\0")
                parts = [part.decode("utf-8", "strict") for part in name.split(b"/")]
                target = tree.joinpath(*parts)
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                # Read the same fenced file descriptor again into the staged tree. The
                # final inventory/content pass below detects any mutation between reads.
                data = _dev_read_bytes(root_fd, name, DEV_SOURCE_MAX_BYTES - total + size, paths.owner_uid)
                if hashlib.sha256(data).hexdigest() != content_hash or len(data) != size:
                    raise DeploymentError("Development source changed while creating its snapshot")
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, "wb") as output:
                    output.write(data)
                os.chmod(target, 0o755 if git_identity.startswith(b"100755 ") else 0o644)
                os.chown(target, paths.owner_uid, -1)
            captured_digest = digest.hexdigest()
            if inventory != _dev_inventory(source):
                raise DeploymentError("Development source changed during snapshot creation; retry")
            # Fence a second complete read against the bytes staged above.
            check_files, check_digest = _dev_content(source, selected_index, selected_untracked, paths.owner_uid)
            if check_files != files or check_digest != captured_digest:
                raise DeploymentError("Development source changed during snapshot creation; retry")
        finally:
            os.close(root_fd)

        archive_path = scratch / "snapshot.tar"
        with tarfile.open(archive_path, "w", format=tarfile.PAX_FORMAT) as archive:
            for file in sorted(tree.rglob("*")):
                rel = file.relative_to(tree).as_posix()
                item = file.lstat()
                if stat.S_ISDIR(item.st_mode):
                    info = tarfile.TarInfo(rel + "/")
                    info.type, info.mode, info.uid, info.gid = tarfile.DIRTYPE, 0o755, paths.owner_uid, 0
                    archive.addfile(info)
                elif stat.S_ISREG(item.st_mode) and item.st_nlink == 1:
                    info = archive.gettarinfo(str(file), arcname=rel)
                    info.mtime = 0
                    info.uid, info.gid, info.uname, info.gname = paths.owner_uid, 0, "", ""
                    info.pax_headers = {}
                    with file.open("rb") as stream:
                        archive.addfile(info, stream)
                else:
                    raise DeploymentError("Development snapshot contains an unsupported file type")
        directory = _dev_source_directory(paths)
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            pass
        info = directory.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != paths.owner_uid
                or stat.S_IMODE(info.st_mode) != 0o700):
            raise DeploymentError("Development snapshot directory has unsafe ownership or mode")
        capsule = directory / f"{captured_digest}.tar"
        expected_archive_digest = _digest(archive_path)
        capsule_created = False
        if not os.path.lexists(capsule):
            required = archive_path.stat().st_size + reserve
            if shutil.disk_usage(directory).free < required:
                raise DeploymentIncomplete(f"DEV recovery snapshot needs {required} free bytes in {directory.parent}.")
            fd, temporary = tempfile.mkstemp(prefix=".capture-", dir=directory)
            try:
                with os.fdopen(fd, "wb") as stream, archive_path.open("rb") as source_archive:
                    shutil.copyfileobj(source_archive, stream, 1024 * 1024)
                    stream.flush()
                    os.fsync(stream.fileno())
                if os.path.lexists(capsule):
                    raise DeploymentError("Development snapshot path appeared during publication")
                os.replace(temporary, capsule)
                capsule_created = True
            finally:
                Path(temporary).unlink(missing_ok=True)
            dfd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        info = capsule.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != paths.owner_uid
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
                or _backup_file_digest(capsule, info) != expected_archive_digest):
            raise DeploymentError("existing Development snapshot capsule does not match captured source content")
        return {"path": str(source), "base_git_sha": sha,
                "dirty": selected_base != files or selected_base != selected_index,
                "provenance": "local_development_worktree", "content_sha256": captured_digest,
                "file_count": len(files), "snapshot_sha256": expected_archive_digest,
                "snapshot_path": str(capsule), "_created_by_capture": capsule_created}


def _dev_read_bytes(root: int, name: bytes, remaining: int, owner_uid: int) -> bytes:
    parts = name.split(b"/")
    parent = os.dup(root)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            os.close(parent)
            parent = child
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != owner_uid
                    or before.st_mode & UNSAFE_WRITE or before.st_size > remaining):
                raise DeploymentError("Development file changed type or exceeds the snapshot size bound")
            data = stream.read(remaining + 1)
            after = os.fstat(stream.fileno())
            named = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
            fence = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns,
                                   value.st_ctime_ns, value.st_mode)
            if len(data) != before.st_size or fence(before) != fence(after) or fence(before) != fence(named):
                raise DeploymentError("Development source changed while creating its snapshot")
            return data
    finally:
        os.close(parent)


@contextmanager
def _materialize_development_snapshot(paths: Paths, metadata: dict):
    capsule = Path(metadata["snapshot_path"])
    expected = metadata["snapshot_sha256"]
    if (capsule.parent != _dev_source_directory(paths) or capsule.name != f"{metadata['content_sha256']}.tar"
            or not _hex(expected, 64)):
        raise DeploymentError("Development recovery snapshot reference is malformed")
    info = capsule.lstat()
    if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_uid != paths.owner_uid
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1 or _digest(capsule) != expected):
        raise DeploymentError("Development recovery snapshot failed its ownership or digest check")
    with _managed_staging_workspace(paths) as scratch:
        source = scratch / "source"
        source.mkdir(mode=0o700)
        count = total = 0
        try:
            with tarfile.open(capsule, "r:") as archive:
                for member in archive:
                    count += 1
                    parts = PurePosixPath(member.name).parts
                    if (count > MAX_TREE_ENTRIES * 4 or not parts
                            or any(part in {"", ".", "..", ".git"} for part in parts)
                            or parts[0] not in {"backend", "ui", "host", "installer"}):
                        raise DeploymentError("Development snapshot contains an unsafe path or too many entries")
                    target = source.joinpath(*parts)
                    if member.isdir():
                        target.mkdir(mode=0o755, parents=True, exist_ok=True)
                        os.chmod(target, member.mode & 0o755)
                        continue
                    if not member.isfile() or member.size > DEV_SOURCE_MAX_BYTES - total:
                        raise DeploymentError("Development snapshot contains an unsafe entry or exceeds size bound")
                    total += member.size
                    target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
                    stream = archive.extractfile(member)
                    if stream is None:
                        raise DeploymentError("Development snapshot is incomplete")
                    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                    with os.fdopen(fd, "wb") as output:
                        shutil.copyfileobj(stream, output, 1024 * 1024)
                    os.chmod(target, member.mode & 0o755)
        except (OSError, tarfile.TarError) as exc:
            raise DeploymentError("Development recovery snapshot could not be materialized") from exc
        digest = hashlib.sha256(b"fwrouter-development-source/v1\n")
        files = sorted(path for path in source.rglob("*") if path.is_file())
        if len(files) != metadata.get("file_count"):
            raise DeploymentError("Development snapshot file count differs from its captured identity")
        for path in files:
            rel = path.relative_to(source).as_posix().encode("utf-8", "strict")
            mode = b"100755" if path.stat().st_mode & stat.S_IXUSR else b"100644"
            digest.update(mode + b" " + _digest(path).encode("ascii") + b" " + rel + b"\0")
        if digest.hexdigest() != metadata.get("content_sha256"):
            raise DeploymentError("Development snapshot content digest does not match its captured identity")
        _initialize_development_git(source, paths.owner_uid, metadata)
        commit = _source_commit(source, paths.owner_uid)
        yield source, commit, scratch


def _initialize_development_git(source: Path, owner_uid: int, metadata: dict) -> None:
    def git(args: list[str], *, env: dict[str, str] | None = None, input_text: str | None = None) -> str:
        process_env = _git_environment()
        if env:
            process_env.update(env)
        try:
            result = subprocess.run(["/usr/bin/git", "-C", str(source), *args], check=False,
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    timeout=60, env=process_env, encoding="utf-8", input=input_text)
        except (OSError, subprocess.SubprocessError) as exc:
            raise DeploymentError("could not create an immutable Git identity for the Development snapshot") from exc
        if result.returncode:
            raise DeploymentError("could not create an immutable Git identity for the Development snapshot")
        return result.stdout.strip()

    git(["init", "--quiet", "--template="])
    files = sorted(path for path in source.rglob("*") if path.is_file() and ".git" not in path.parts)
    index_rows = []
    for start in range(0, len(files), 64):
        batch = files[start:start + 64]
        names = [path.relative_to(source).as_posix() for path in batch]
        oids = git(["hash-object", "-w", "--no-filters", "--", *names]).splitlines()
        if len(oids) != len(names) or any(not _hex(oid, 40) for oid in oids):
            raise DeploymentError("Development snapshot Git object inventory is incomplete")
        for path, name, oid in zip(batch, names, oids, strict=True):
            mode = "100755" if path.stat().st_mode & stat.S_IXUSR else "100644"
            index_rows.append(f"{mode} {oid}\t{name}\0")
    git(["update-index", "-z", "--index-info"], input_text="".join(index_rows))
    tree = git(["write-tree"])
    env = {"GIT_AUTHOR_NAME": "FWRouter Development Snapshot", "GIT_AUTHOR_EMAIL": "fwrouter@localhost",
           "GIT_COMMITTER_NAME": "FWRouter Development Snapshot", "GIT_COMMITTER_EMAIL": "fwrouter@localhost",
           "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z"}
    commit = git(["commit-tree", tree, "-m", "Development snapshot"], env=env)
    git(["update-ref", "refs/heads/snapshot", commit])
    git(["symbolic-ref", "HEAD", "refs/heads/snapshot"])
    for root, dirs, files in os.walk(source, topdown=True):
        for name in dirs:
            os.chown(Path(root) / name, owner_uid, -1)
        for name in files:
            os.chown(Path(root) / name, owner_uid, -1)


def _self_upgrade(paths: Paths, source: Path, commit: str, args: argparse.Namespace) -> dict:
    pins = (args.main_sha, args.run_id, args.run_attempt, args.artifact_id,
            args.artifact_digest, args.expected_installed_module_sha256, args.confirm_main_sha)
    if any(not isinstance(value, str) or not value for value in pins):
        raise DeploymentError("self-upgrade requires all exact root-pinned run, artifact and source inputs")
    sha = args.main_sha
    if (len(sha) != 40 or any(ch not in "0123456789abcdef" for ch in sha)
            or args.confirm_main_sha != sha or commit != sha):
        raise DeploymentError("selected source, confirmation and exact Main SHA must match")
    if (not all(value.isascii() and value.isdigit() for value in
                (args.run_id, args.run_attempt, args.artifact_id))
            or args.expected_installed_module_sha256 != args.expected_installed_module_sha256.lower()
            or len(args.expected_installed_module_sha256) != 64
            or any(ch not in "0123456789abcdef" for ch in args.expected_installed_module_sha256)):
        raise DeploymentError("self-upgrade pins have an invalid format")
    if not args.artifact_digest.startswith("sha256:") or len(args.artifact_digest) != 71:
        raise DeploymentError("artifact digest must be the exact GitHub SHA-256 digest")
    confirm_digest = args.artifact_digest.removeprefix("sha256:")
    if any(ch not in "0123456789abcdef" for ch in confirm_digest):
        raise DeploymentError("artifact digest has an invalid format")
    qualified_selection = paths.stable_source
    source_location = MAIN_ORIGIN if qualified_selection else str(source)
    if qualified_selection:
        _assert_requested_source(paths, source, commit)
        expected = qualified_selection.qualification
        if (int(args.run_id), int(args.run_attempt), int(args.artifact_id), args.artifact_digest) != (
                expected["run_id"], expected["run_attempt"], expected["artifact_id"], expected["artifact_digest"]):
            raise DeploymentError("updater transition differs from the admitted Stable qualification")

    module_path = paths.target("usr/local/libexec/fwrouter/local_deploy.py")
    wrapper_path = paths.target("usr/local/sbin/fwrouter-deploy")
    _check_trusted_tree(module_path.parent, paths.owner_uid)
    _check_trusted_tree(wrapper_path.parent, paths.owner_uid)
    source_module = _git_blob_bytes(source, sha, "installer/local_deploy.py")
    source_wrapper = _git_blob_bytes(source, sha, "host/sbin/fwrouter-deploy")
    if source_module != _read_stable_source_file(source / "installer/local_deploy.py", paths.owner_uid):
        raise DeploymentError("candidate updater differs from the exact Main Git blob")
    if source_wrapper != _read_stable_source_file(source / "host/sbin/fwrouter-deploy", paths.owner_uid):
        raise DeploymentError("candidate wrapper differs from the exact Main Git blob")
    for path in (module_path, wrapper_path):
        info = path.lstat()
        expected_mode = 0o644 if path == module_path else 0o755
        if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_uid != paths.owner_uid
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != expected_mode):
            raise DeploymentError("installed updater or wrapper has unsafe owner, type or mode")
    if _digest(wrapper_path) != hashlib.sha256(source_wrapper).hexdigest():
        raise DeploymentError("candidate changes the installed wrapper; this bootstrap only supports module replacement")
    wrapper_digest = hashlib.sha256(source_wrapper).hexdigest()
    current_module_digest = _digest(module_path)
    candidate_module_digest = hashlib.sha256(source_module).hexdigest()
    if current_module_digest not in {args.expected_installed_module_sha256, candidate_module_digest}:
        raise DeploymentError("installed updater differs from both the pinned old and qualified new module")
    if module_path == wrapper_path or module_path.parent != wrapper_path.parent.parent / "libexec/fwrouter":
        raise DeploymentError("installed updater paths do not match the supported host layout")

    operation_id = str(uuid.uuid5(uuid.NAMESPACE_URL,
        f"fwrouter-updater:{sha}:{args.run_id}:{args.run_attempt}:{args.artifact_id}:"
        f"{args.artifact_digest}:{args.expected_installed_module_sha256}:{wrapper_digest}:{candidate_module_digest}"))
    identity_before = _read_identity(paths)
    prior_operation = identity_before.get("operation") if identity_before.get("status") != "UNKNOWN" else None
    pending_resume = _matches_self_upgrade_transition(
        prior_operation, status="PENDING", operation_id=operation_id, source_sha=sha,
        run_id=args.run_id, run_attempt=args.run_attempt, artifact_id=args.artifact_id,
        artifact_digest=args.artifact_digest, old_digest=args.expected_installed_module_sha256,
        candidate_digest=candidate_module_digest, wrapper_digest=wrapper_digest)
    pending_resume = pending_resume and prior_operation.get("source_path") == source_location
    if prior_operation and prior_operation.get("status") in {"PENDING", "FAILED"} and not pending_resume:
        raise DeploymentError("another unfinished deployment operation prevents updater self-upgrade")
    if prior_operation and prior_operation.get("kind") == "self_upgrade" and prior_operation.get("status") == "COMPLETE":
        if (prior_operation.get("operation_id") == operation_id
                and current_module_digest == candidate_module_digest):
            return {"status": "UPDATER_ALREADY_INSTALLED", "operation_id": operation_id,
                    "source_sha": sha, "module_sha256": candidate_module_digest,
                    "native_proof": "NOT_VERIFIED", "acceptance": "NOT_VERIFIED"}
        # A completed transition is not a recovery fence. A later independently
        # qualified Main may replace the updater when its explicit old-byte pin
        # matches the module currently installed; pending transitions remain
        # strictly bound to their original pins above.
    if pending_resume and current_module_digest not in {args.expected_installed_module_sha256,
                                                         candidate_module_digest}:
        raise DeploymentError("pending updater transition does not match installed module bytes")

    verifier_files = list(prior_operation.get("verifier_sources", [])) if pending_resume else []
    main = run = jobs = artifact = None
    if not pending_resume and not qualified_selection:
        main = _gh_api("repos/Coxf0rd/fwrouter/commits/main")
        if main.get("sha") != sha:
            raise DeploymentError("pinned source is no longer the current GitHub Main head")
        run = _gh_api(f"repos/Coxf0rd/fwrouter/actions/runs/{args.run_id}")
        if not _main_run_matches(run, sha, int(args.run_id), int(args.run_attempt)):
            raise DeploymentError("GitHub run is not a successful exact-SHA push to Main")
        jobs = _gh_api(f"repos/Coxf0rd/fwrouter/actions/runs/{args.run_id}/jobs?per_page=100")
        if not _successful_main_jobs(jobs):
            raise DeploymentError("GitHub run jobs are incomplete, failed, skipped or paginated")
        artifact = _gh_api(f"repos/Coxf0rd/fwrouter/actions/artifacts/{args.artifact_id}")
        qualification = {"source_sha": sha, "run_id": int(args.run_id), "run_attempt": int(args.run_attempt),
                         "artifact_id": int(args.artifact_id), "artifact_digest": args.artifact_digest}
        if not _main_artifact_matches(artifact, qualification):
            raise DeploymentError("GitHub artifact metadata does not match the pinned Main run")

    verifier_files = verifier_files or _verify_candidate_tree(source, sha, paths.owner_uid)
    for relative in ("installer/local_deploy.py", "installer/component_install_lock.py",
                     "installer/controlled_backend_lifecycle.py", "host/sbin/fwrouter-deploy"):
        if _read_stable_source_file(source / relative, paths.owner_uid) != _git_blob_bytes(source, sha, relative):
            raise DeploymentError("candidate updater, helper or wrapper differs from its exact Git blob")
    if not pending_resume and not qualified_selection:
        with tempfile.TemporaryDirectory(prefix="fwrouter-updater-qualification-", dir="/tmp") as scratch:
            _verify_main_bundle(source, Path(scratch), artifact, qualification)
    lock = _load_module(source / "installer/component_install_lock.py", "fwrouter_updater_component_lock")
    lock_fd = None
    try:
        lock_fd, _ = lock.acquire(paths.target("opt"), maintenance_mode="shared")
        _assert_requested_source(paths, source, commit)
        if not pending_resume and not qualified_selection:
            if _gh_api("repos/Coxf0rd/fwrouter/commits/main").get("sha") != sha:
                raise DeploymentError("GitHub Main changed before updater publication")
            final_run = _gh_api(f"repos/Coxf0rd/fwrouter/actions/runs/{args.run_id}")
            if any(final_run.get(key) != run.get(key) for key in (
                    "id", "run_attempt", "event", "head_branch", "head_sha", "status", "conclusion", "path")):
                raise DeploymentError("GitHub run identity changed during updater qualification")
            if (not isinstance(final_run.get("repository"), dict)
                    or final_run["repository"].get("full_name") != "Coxf0rd/fwrouter"):
                raise DeploymentError("GitHub run repository changed during updater qualification")
            final_jobs = _gh_api(f"repos/Coxf0rd/fwrouter/actions/runs/{args.run_id}/jobs?per_page=100")
            if not _successful_main_jobs(final_jobs):
                raise DeploymentError("GitHub run jobs lost successful completion during updater qualification")
            final_artifact = _gh_api(f"repos/Coxf0rd/fwrouter/actions/artifacts/{args.artifact_id}")
            if any(final_artifact.get(key) != artifact.get(key) for key in
                   ("id", "name", "digest", "size_in_bytes", "expired", "workflow_run")):
                raise DeploymentError("GitHub artifact identity changed during updater qualification")
        if (_read_stable_source_file(source / "installer/local_deploy.py", paths.owner_uid) != source_module
                or _read_stable_source_file(source / "host/sbin/fwrouter-deploy", paths.owner_uid) != source_wrapper
                or _digest(wrapper_path) != hashlib.sha256(source_wrapper).hexdigest()):
            raise DeploymentError("candidate updater or pinned wrapper changed before atomic publication")
        checkpoint = paths.state_dir / "pending.json"
        if checkpoint.exists():
            lifecycle = _load_module(source / "installer/controlled_backend_lifecycle.py",
                                     "fwrouter_updater_lifecycle")
            current = lifecycle._read_checkpoint(checkpoint)
            identity = _read_identity(paths)
            active_transition = identity.get("operation") if identity.get("status") != "UNKNOWN" else None
            same_transition = _matches_self_upgrade_transition(
                active_transition, status="PENDING", operation_id=operation_id, source_sha=sha,
                run_id=args.run_id, run_attempt=args.run_attempt, artifact_id=args.artifact_id,
                artifact_digest=args.artifact_digest, old_digest=args.expected_installed_module_sha256,
                candidate_digest=candidate_module_digest, wrapper_digest=wrapper_digest)
            same_transition = same_transition and active_transition.get("source_path") == source_location
            if current.get("status") == "pending":
                raise DeploymentError("pending lifecycle operation prevents updater self-upgrade")
        else:
            identity = _read_identity(paths)
            active_transition = identity.get("operation") if identity.get("status") != "UNKNOWN" else None
            same_transition = _matches_self_upgrade_transition(
                active_transition, status="PENDING", operation_id=operation_id, source_sha=sha,
                run_id=args.run_id, run_attempt=args.run_attempt, artifact_id=args.artifact_id,
                artifact_digest=args.artifact_digest, old_digest=args.expected_installed_module_sha256,
                candidate_digest=candidate_module_digest, wrapper_digest=wrapper_digest)
            same_transition = same_transition and active_transition.get("source_path") == source_location
        if active_transition and active_transition.get("status") in {"PENDING", "FAILED"} and not same_transition:
            raise DeploymentError("another unfinished deployment operation prevents updater self-upgrade")
        current_module_digest = _digest(module_path)
        if current_module_digest not in {args.expected_installed_module_sha256, candidate_module_digest}:
            raise DeploymentError("installed updater changed before atomic publication")
        record = _identity_record_base(paths)
        if not same_transition:
            record["operation"] = {"kind": "self_upgrade", "status": "PENDING",
                "operation_id": operation_id, "source_sha": sha, "source_path": source_location,
                "source_mode": "UNKNOWN", "provenance": "verified_actual_main_ci",
                "run_id": args.run_id, "run_attempt": args.run_attempt, "artifact_id": args.artifact_id,
                "artifact_digest": args.artifact_digest,
                "old_module_sha256": args.expected_installed_module_sha256,
                "candidate_module_sha256": candidate_module_digest, "wrapper": "unchanged",
                "wrapper_sha256": wrapper_digest, "verifier_sources": verifier_files,
                "acceptance": {"status": "NOT_VERIFIED", "scope": "updater_self_upgrade_only"}}
            _write_identity(paths, record)
        if current_module_digest == args.expected_installed_module_sha256:
            _atomic_replace_module(module_path, source_module, paths.owner_uid)
        final_info = module_path.lstat()
        if (_digest(module_path) != candidate_module_digest or not stat.S_ISREG(final_info.st_mode)
                or final_info.st_uid != paths.owner_uid or stat.S_IMODE(final_info.st_mode) != 0o644
                or final_info.st_nlink != 1):
            raise DeploymentError("atomic updater replacement failed exact post-publication verification")
        record["operation"] = {"kind": "self_upgrade", "status": "COMPLETE",
            "operation_id": operation_id, "source_sha": sha, "source_path": source_location,
            "source_mode": "UNKNOWN", "provenance": "verified_actual_main_ci",
            "run_id": args.run_id, "run_attempt": args.run_attempt, "artifact_id": args.artifact_id,
            "artifact_digest": args.artifact_digest, "candidate_module_sha256": candidate_module_digest,
            "old_module_sha256": args.expected_installed_module_sha256,
            "wrapper": "unchanged", "wrapper_sha256": wrapper_digest, "verifier_sources": verifier_files,
            "completed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "acceptance": {"status": "NOT_VERIFIED", "scope": "updater_self_upgrade_only"}}
        record["components"]["updater"] = {"source_path": MAIN_ORIGIN if qualified_selection else str(source / "installer/local_deploy.py"),
            "source_sha": sha, "provenance": "verified_actual_main_ci", "mode": "STABLE" if qualified_selection else "UNKNOWN",
            "qualification_status": "VERIFIED_MAIN_CI", "installed_sha256": candidate_module_digest,
            "operation_id": operation_id, "run_id": args.run_id, "run_attempt": args.run_attempt,
            "artifact_id": args.artifact_id, "artifact_digest": args.artifact_digest,
            "wrapper_sha256": wrapper_digest}
        _write_identity(paths, record)
    except Exception:
        if lock_fd is not None:
            try:
                current = _read_identity(paths)
                transition = current.get("operation") if current.get("status") != "UNKNOWN" else None
                if isinstance(transition, dict) and transition.get("operation_id") == operation_id:
                    transition["status"] = "PENDING"
                    _write_identity(paths, current)
            except Exception:
                pass
        raise
    finally:
        if lock_fd is not None:
            lock.release(lock_fd)
    return {"status": "UPDATER_UPGRADED", "operation_id": operation_id,
            "source_sha": sha, "module_sha256": candidate_module_digest,
            "native_proof": "NOT_VERIFIED", "acceptance": "NOT_VERIFIED"}


def _atomic_replace_module(path: Path, payload: bytes, owner_uid: int) -> None:
    fd = -1
    temporary = ""
    try:
        fd, temporary = tempfile.mkstemp(prefix=".local-deploy-updater.", dir=path.parent)
        os.fchown(fd, owner_uid, -1)
        os.fchmod(fd, 0o644)
        with os.fdopen(fd, "wb") as stream:
            fd = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = ""
        dfd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError as exc:
        raise DeploymentError("atomic updater module replacement failed") from exc
    finally:
        if fd >= 0:
            os.close(fd)
        if temporary:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


DEV_UPDATER_DIRECTORY = "dev-updater"


def _verified_main_updater(paths: Paths, identity: dict) -> str:
    recorded = identity.get("components", {}).get("updater") or {}
    module = paths.target("usr/local/libexec/fwrouter/local_deploy.py")
    wrapper = paths.target("usr/local/sbin/fwrouter-deploy")
    for path, mode in ((module, 0o644), (wrapper, 0o755)):
        _check_trusted_tree(path.parent, paths.owner_uid)
        if stat.S_IMODE(path.lstat().st_mode) != mode:
            raise DeploymentError("Qualified updater entrypoint has an unsafe mode.")
    digest = hashlib.sha256(_read_stable_source_file(module, paths.owner_uid)).hexdigest()
    if (recorded.get("qualification_status") != "VERIFIED_MAIN_CI"
            or recorded.get("installed_sha256") != digest
            or recorded.get("wrapper_sha256") != hashlib.sha256(
                _read_stable_source_file(wrapper, paths.owner_uid)).hexdigest()):
        raise DeploymentIncomplete("The installed Main-qualified updater and wrapper must match their verified identity.")
    return digest


def _read_dev_updater(paths: Paths) -> dict:
    directory = paths.state_dir / DEV_UPDATER_DIRECTORY
    _check_trusted_tree(directory, paths.owner_uid)
    if stat.S_IMODE(directory.lstat().st_mode) != 0o700:
        raise DeploymentError("DEV updater directory is not private.")
    receipt = directory / "identity.json"
    if stat.S_IMODE(receipt.lstat().st_mode) != 0o600:
        raise DeploymentError("DEV updater identity is not private.")
    identity = _json_object(_read_stable_source_file(receipt, paths.owner_uid, limit=4096).decode("utf-8"))
    if (set(identity) != {"source_sha", "module_sha256", "main_updater_sha256", "qualification"}
            or identity["qualification"] != "LOCAL_DEV"
            or not _hex(identity["source_sha"], 40)
            or not _hex(identity["module_sha256"], 64)
            or not _hex(identity["main_updater_sha256"], 64)
            or stat.S_IMODE((directory / "local_deploy.py").lstat().st_mode) != 0o644
            or hashlib.sha256(_read_stable_source_file(directory / "local_deploy.py", paths.owner_uid)).hexdigest()
               != identity["module_sha256"]):
        raise DeploymentError("DEV updater content or identity does not match its recorded local source.")
    if identity["main_updater_sha256"] != _verified_main_updater(paths, _read_identity(paths)):
        raise DeploymentIncomplete("The qualified updater changed after the DEV updater was installed.")
    return identity


def _install_dev_updater(paths: Paths, source: Path) -> dict:
    commit = _source_commit(source, paths.owner_uid)
    relative = "installer/local_deploy.py"
    payload = _git_blob_bytes(source, commit, relative)
    if _read_stable_source_file(source / relative, paths.owner_uid) != payload:
        raise DeploymentError("DEV updater differs from its exact Git blob.")
    compile(payload, relative, "exec")
    with _configuration_component_lock(paths, command=True), _configuration_component_lock(paths):
        identity = _read_identity(paths)
        last = identity.get("last_verified_install") or {}
        if (last.get("mode") == "DEV" or any(identity.get(k) for k in
                ("stable_admission", "rollback_admission", "dev_admission"))
                or (identity.get("operation") or {}).get("status") in {"PENDING", "FAILED"}):
            raise DeploymentIncomplete("Complete the existing application deployment or recovery before installing a DEV updater.")
        main_digest = _verified_main_updater(paths, identity)
        receipt = {"source_sha": commit, "module_sha256": hashlib.sha256(payload).hexdigest(),
                   "main_updater_sha256": main_digest, "qualification": "LOCAL_DEV"}
        directory = paths.state_dir / DEV_UPDATER_DIRECTORY
        if os.path.lexists(directory):
            if _read_dev_updater(paths) != receipt:
                raise DeploymentIncomplete("A different DEV updater is retained for development recovery.")
            return {**receipt, "path": str(directory / "local_deploy.py"), "result": "NOOP"}
        _check_trusted_tree(paths.state_dir, paths.owner_uid)
        staging = Path(tempfile.mkdtemp(prefix=".dev-updater-", dir=paths.state_dir))
        try:
            _atomic_replace_module(staging / "local_deploy.py", payload, paths.owner_uid)
            fd = os.open(staging / "identity.json", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(receipt, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            _sync_lifecycle_owner_directory(staging)
            staging.rename(directory)
            _sync_lifecycle_owner_directory(paths.state_dir)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
        if _read_dev_updater(paths) != receipt:
            raise DeploymentError("Installed DEV updater verification failed.")
        return {**receipt, "path": str(directory / "local_deploy.py"), "result": "SUCCESS"}


def _host_plan(source: Path, paths: Paths) -> tuple[list[dict], Path | None]:
    rows: list[dict] = []
    for source_rel, target_rel, mode in HOST_ROWS:
        source_path = source / source_rel
        target_path = paths.target(target_rel)
        source_info = source_path.lstat()
        if (not stat.S_ISREG(source_info.st_mode) or source_info.st_uid != paths.owner_uid
                or stat.S_IMODE(source_info.st_mode) != mode):
            raise DeploymentError("selected host pair has an unexpected owner, type or mode")
        old_digest = None
        try:
            info = target_path.lstat()
        except FileNotFoundError:
            info = None
        if info is not None:
            if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
                    or info.st_uid != paths.owner_uid or info.st_nlink != 1
                    or info.st_mode & UNSAFE_WRITE):
                raise DeploymentError("existing host pair target is not a safe regular file")
            old_digest = _digest(target_path)
        new_digest = _digest(source_path)
        if old_digest == new_digest and info is not None and stat.S_IMODE(info.st_mode) == mode:
            continue
        rows.append({"source": source_rel, "target": target_rel,
                     "expected_old_sha256": old_digest, "source_sha256": new_digest, "mode": mode})
    if not rows:
        return rows, None
    paths.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = paths.state_dir.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != paths.owner_uid
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise DeploymentError("controlled lifecycle state directory is unsafe")
    plan = {"schema": "fwrouter-host-publication-plan/v1", "files": rows}
    fd, filename = tempfile.mkstemp(prefix=".public-host-plan.", dir=paths.state_dir)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            fd = -1
            json.dump(plan, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        if fd >= 0:
            os.close(fd)
        os.unlink(filename)
        raise DeploymentError("could not prepare the selective host CAS plan") from exc
    return rows, Path(filename)


def _normalize_legacy_ui(target: Path, owner_uid: int) -> None:
    """Make a root-owned legacy directory web-readable before immutable publication."""
    try:
        root_info = target.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(root_info.st_mode):
        return  # Managed content-addressed releases remain immutable.
    if not stat.S_ISDIR(root_info.st_mode) or root_info.st_uid != owner_uid:
        raise DeploymentError("legacy UI target must be a real root-owned directory")
    count = 0

    def walk(directory_fd: int) -> None:
        nonlocal count
        for name in sorted(os.listdir(directory_fd)):
            count += 1
            if count > MAX_TREE_ENTRIES:
                raise DeploymentError("legacy UI directory exceeds the bounded entry limit")
            before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if before.st_uid != owner_uid:
                raise DeploymentError("legacy UI entry has an unexpected owner")
            if stat.S_ISDIR(before.st_mode):
                child = os.open(name, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                                | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory_fd)
                try:
                    opened = os.fstat(child)
                    if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                        raise DeploymentError("legacy UI directory changed during normalization")
                    walk(child)
                    os.fchmod(child, (stat.S_IMODE(opened.st_mode) & ~UNSAFE_WRITE) | stat.S_IXOTH)
                finally:
                    os.close(child)
            elif stat.S_ISREG(before.st_mode) and before.st_nlink == 1:
                fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory_fd)
                try:
                    opened = os.fstat(fd)
                    if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                        raise DeploymentError("legacy UI file changed during normalization")
                    os.fchmod(fd, (stat.S_IMODE(opened.st_mode) & ~UNSAFE_WRITE) | stat.S_IROTH)
                finally:
                    os.close(fd)
            else:
                raise DeploymentError("legacy UI tree contains a symlink, special file or hard link")

    fd = os.open(target, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (root_info.st_dev, root_info.st_ino):
            raise DeploymentError("legacy UI root changed during normalization")
        walk(fd)
        os.fchmod(fd, (stat.S_IMODE(opened.st_mode) & ~UNSAFE_WRITE) | stat.S_IXOTH)
        os.fsync(fd)
    finally:
        os.close(fd)


def _component_lock(lock_module, paths: Paths) -> tuple[int, int | None, int | None, int | None]:
    fd, _ = lock_module.acquire(paths.target("opt"), maintenance_mode="exclusive")
    maintenance_fd = lock_module._OWNED_MAINTENANCE_FDS.get(fd)
    # The installer contract reserves descriptors 8 and 9 for inherited locks.
    # Move held originals away first so child-FD cleanup can never close the
    # in-process lock descriptor when acquire happened to return 8 or 9.
    old_fd = fd
    fd = fcntl.fcntl(old_fd, fcntl.F_DUPFD, 20)
    os.close(old_fd)
    if maintenance_fd is not None:
        old_maintenance_fd = maintenance_fd
        maintenance_fd = fcntl.fcntl(old_maintenance_fd, fcntl.F_DUPFD, 20)
        os.close(old_maintenance_fd)
        lock_module._OWNED_MAINTENANCE_FDS.pop(old_fd, None)
        lock_module._OWNED_MAINTENANCE_FDS[fd] = maintenance_fd
    saved_child_fds: dict[int, int | None] = {}
    installed_child_fds: list[int] = []
    try:
        for original, target_fd in ((fd, INSTALLER_LOCK_FD), (maintenance_fd, MAINTENANCE_LOCK_FD)):
            if original is None:
                continue
            try:
                saved = fcntl.fcntl(target_fd, fcntl.F_DUPFD, 20)
                os.set_inheritable(saved, False)
            except OSError:
                saved = None
            saved_child_fds[target_fd] = saved
            os.dup2(original, target_fd, inheritable=True)
            installed_child_fds.append(target_fd)
    except BaseException:
        for target_fd in reversed(installed_child_fds):
            _restore_child_fd(target_fd, saved_child_fds.get(target_fd))
        lock_module.release(fd)
        raise
    return fd, maintenance_fd, saved_child_fds.get(MAINTENANCE_LOCK_FD), saved_child_fds.get(INSTALLER_LOCK_FD)


def _restore_child_fd(target_fd: int, saved_fd: int | None) -> None:
    try:
        os.close(target_fd)
    except OSError:
        pass
    if saved_fd is not None:
        os.dup2(saved_fd, target_fd, inheritable=True)
        os.close(saved_fd)


def _run_installer(source: Path, paths: Paths, components: list[str], *, host_plan: Path | None = None,
                   inherited: bool) -> None:
    argv = [str(source / "installer" / "install.sh"), "--deploy"]
    for component in components:
        argv.extend(("--component", component))
    if paths.target_root != Path("/"):
        argv.extend(("--target", str(paths.target_root)))
    if host_plan is not None:
        argv.extend(("--host-plan", str(host_plan)))
    if inherited:
        maintenance_mode = "exclusive" if "backend" in components or host_plan else "shared"
        argv.extend(("--_component-lock-fd", str(INSTALLER_LOCK_FD),
                     "--_maintenance-lock-mode", maintenance_mode))
        pass_fds = (INSTALLER_LOCK_FD, MAINTENANCE_LOCK_FD) if paths.target_root == Path("/") else (INSTALLER_LOCK_FD,)
    else:
        pass_fds = ()
    result = _run(argv, timeout=1800, pass_fds=pass_fds,
                  host_environment=bool((paths.stable_source or paths.development_source) and not paths.test_driver))
    if result.returncode != 0:
        raise DeploymentError(f"installer failed for component set: {','.join(components)}")


def _publish_host_pair(source: Path, paths: Paths, plan: Path) -> None:
    lock = _load_module(source / "installer" / "component_install_lock.py", "fwrouter_host_pair_lock")
    publisher = _load_module(source / "installer" / "host_publication.py", "fwrouter_local_host_publication_runtime")
    publisher.component_install_lock = lock
    result = _run([str(source / "installer" / "install.sh"), "--deploy", "--component", "host",
                   "--host-plan", str(plan), *(["--target", str(paths.target_root)] if paths.target_root != Path("/") else []),
                   "--_component-lock-fd", str(INSTALLER_LOCK_FD), "--_maintenance-lock-mode", "exclusive"],
                  timeout=1800,
                  pass_fds=(INSTALLER_LOCK_FD, MAINTENANCE_LOCK_FD)
                  if paths.target_root == Path("/") else (INSTALLER_LOCK_FD,),
                  host_environment=bool((paths.stable_source or paths.development_source) and not paths.test_driver))
    if result.returncode != 0:
        raise DeploymentError("selective host pair publication failed")
    if paths.target_root == Path("/"):
        reload_result = _run(["/usr/bin/systemctl", "daemon-reload"],
                             host_environment=bool(paths.stable_source or paths.development_source))
        if reload_result.returncode != 0:
            raise DeploymentError("systemd could not reload the published API unit")
        check = _run(["/usr/bin/systemd-analyze", "verify",
                      str(paths.target("etc/systemd/system/fwrouter-api.service"))],
                     host_environment=bool(paths.stable_source or paths.development_source))
        if check.returncode != 0:
            raise DeploymentError("published API unit failed systemd verification")


def _xray_unit_active() -> bool:
    """Observe the Xray runtime unit without routing it through lifecycle ownership."""
    try:
        result = subprocess.run(["/usr/bin/systemctl", "is-active", "fwrouter-xray.service"],
                                check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and result.stdout.strip() == "active"


def _facts(source: Path, paths: Paths, lifecycle, ui) -> tuple[bool, bool, bool, bool]:
    source_manifest = lifecycle._backend_manifest(source / "backend")
    backend_same = False
    try:
        installed_manifest = lifecycle._backend_manifest(paths.target("opt/fwrouter-api"))
        backend_same = installed_manifest == source_manifest
    except Exception:
        pass
    ui_same = False
    try:
        source_ui_digest, _ = ui._walk_tree(source / "ui", owner_uid=paths.owner_uid, require_index=True)
        target = paths.target("opt/fwrouter-ui")
        active = ui._managed_release(target, paths.target("opt/.fwrouter-ui-releases"), paths.owner_uid)
        active = active or target
        active_digest, _ = ui._walk_tree(active, owner_uid=paths.owner_uid, require_index=True)
        ui_same = active_digest == source_ui_digest
    except Exception:
        pass
    host_same = True
    for source_rel, target_rel, mode in HOST_ROWS:
        try:
            target = paths.target(target_rel)
            info = target.lstat()
            host_same = host_same and stat.S_ISREG(info.st_mode) and info.st_uid == paths.owner_uid \
                and stat.S_IMODE(info.st_mode) == mode and _digest(target) == _digest(source / source_rel)
        except OSError:
            host_same = False
    if paths.test_driver:
        healthy = bool(paths.api_healthy and paths.runtime_healthy)
    else:
        try:
            healthy = bool(lifecycle._api_health())
            healthy = healthy and lifecycle._is_active(lifecycle.GATEWAY_UNIT)
            healthy = healthy and lifecycle._gateway_listener_ready()
            healthy = healthy and lifecycle._is_active(lifecycle.MIHOMO_UNIT)
            healthy = healthy and _xray_unit_active()
        except Exception:
            healthy = False
    return backend_same, ui_same, host_same, healthy


def check(paths: Paths, source: Path, commit: str, *, engine_source: Path | None = None) -> str:
    identity = _read_identity(paths)
    operation = identity.get("operation") if identity.get("status") != "UNKNOWN" else None
    if isinstance(operation, dict) and operation.get("kind") == "self_upgrade" \
            and operation.get("status") == "PENDING":
        return "UPDATER_SELF_UPGRADE_PENDING"
    lifecycle, _lock, ui = (_source_modules(source, paths) if engine_source is None
                            else _source_modules(source, paths, engine_source=engine_source))
    backend_same, ui_same, host_same, healthy = _facts(source, paths, lifecycle, ui)
    checkpoint = None
    checkpoint_path = paths.state_dir / "pending.json"
    if checkpoint_path.exists():
        checkpoint = lifecycle._read_checkpoint(checkpoint_path)
        if (checkpoint["status"] == "complete" and isinstance(operation, dict)
                and operation.get("status") in {"PENDING", "FAILED"}
                and operation.get("lifecycle_operation_id") == checkpoint["operation_id"]
                and operation.get("requested_source", {}).get("git_sha") == commit
                and backend_same and ui_same and host_same):
            return "FINALIZE_PENDING_IDENTITY"
        if checkpoint["status"] == "pending":
            manifest = lifecycle._backend_manifest(source / "backend")
            if checkpoint.get("maintenance_operation") is not None or checkpoint.get("runtime_quiescence") is not None:
                raise DeploymentError("pending lifecycle checkpoint uses an unsupported maintenance operation")
            selected_digest = lifecycle._manifest_digest(manifest)
            if selected_digest == checkpoint["backend_manifest_sha256"]:
                return "RESUME_PENDING_UNITS"
            installed = lifecycle._manifest_digest(lifecycle._backend_manifest(paths.target("opt/fwrouter-api")))
            if installed == checkpoint["backend_manifest_sha256"]:
                if _installed_source_reconstruction_required(paths):
                    if not _known_installed_backend(paths, installed):
                        raise DeploymentError("installed backend has no durable DEV or Stable identity for recovery")
                else:
                    _find_source_commit(source, paths.target("opt/fwrouter-api"), lifecycle)
                return "TRANSFER_PENDING_OPERATION"
            raise DeploymentError("pending lifecycle checkpoint and installed backend identities disagree")
    if backend_same and ui_same and host_same and healthy:
        return "NOOP"
    if backend_same and ui_same and host_same and not healthy:
        return "UNHEALTHY_CURRENT_SOURCE"
    return "READY_TO_APPLY"


def apply(paths: Paths, source: Path, commit: str, *, allow_timer_catchup: bool = False,
          owner_driver: Callable[[object, str, Path, bool, Path, list[str]], subprocess.CompletedProcess] | None = None,
          system_driver: Callable[[list[str]], subprocess.CompletedProcess] | None = None,
          engine_source: Path | None = None) -> dict:
    _assert_requested_source(paths, source, commit)
    initial_identity = _read_identity(paths)
    initial_operation = initial_identity.get("operation") if initial_identity.get("status") != "UNKNOWN" else None
    if (isinstance(initial_operation, dict) and initial_operation.get("kind") == "self_upgrade"
            and initial_operation.get("status") == "PENDING"):
        raise DeploymentError("pending updater self-upgrade must be resumed with the exact pinned transition")
    lifecycle, lock_module, ui = (_source_modules(source, paths) if engine_source is None
                                  else _source_modules(source, paths, engine_source=engine_source))
    owner_source = engine_source or source
    state_file = paths.state_dir / "pending.json"
    selected_manifest = lifecycle._backend_manifest(source / "backend")
    selected_digest = lifecycle._manifest_digest(selected_manifest)
    installed_manifest = lifecycle._backend_manifest(paths.target("opt/fwrouter-api"))
    installed_digest = lifecycle._manifest_digest(installed_manifest)
    pending = None
    if state_file.exists():
        pending = lifecycle._read_checkpoint(state_file)
        if (pending["status"] == "complete" and isinstance(initial_operation, dict)
                and initial_operation.get("status") in {"PENDING", "FAILED"}
                and initial_operation.get("lifecycle_operation_id") == pending["operation_id"]
                and initial_operation.get("requested_source", {}).get("git_sha") == commit
                and pending["backend_manifest_sha256"] == selected_digest
                and all(_facts(source, paths, lifecycle, ui)[:3])):
            _record_operation(paths, lock_module, source, commit, lifecycle, ui, pending["operation_id"],
                              "complete", "VERIFIED_CONTENT", [])
            return {"status": "APPLIED", "pending_units": [], "source_commit": commit}
        if pending["status"] == "pending":
            if pending.get("maintenance_operation") is not None or pending.get("runtime_quiescence") is not None:
                raise DeploymentError("interrupted operation is outside the simple deployment contract")
            checkpoint_digest = pending["backend_manifest_sha256"]
            identity = _read_identity(paths)
            operation = identity.get("operation") if identity.get("status") != "UNKNOWN" else None
            if isinstance(operation, dict) and operation.get("status") in {"PENDING", "FAILED"}:
                if (operation.get("kind") == "self_upgrade"
                        or operation.get("lifecycle_operation_id") != pending.get("operation_id")
                        or operation.get("requested_source", {}).get("git_sha") != commit):
                    raise DeploymentError("unfinished deployment identity belongs to another operation or source")
            if checkpoint_digest not in {selected_digest, installed_digest}:
                raise DeploymentError("interrupted operation source differs from both installed and selected source")
    if isinstance(initial_operation, dict) and initial_operation.get("status") in {"PENDING", "FAILED"}:
        if (initial_operation.get("kind") == "self_upgrade" or pending is None
                or pending.get("status") != "pending"
                or initial_operation.get("lifecycle_operation_id") != pending.get("operation_id")
                or initial_operation.get("requested_source", {}).get("git_sha") != commit):
            raise DeploymentError("unfinished deployment identity must be resumed before selecting another operation")

    def owner_call(action: str, owner_source: Path = source, owner_args: list[str] | None = None
                   ) -> subprocess.CompletedProcess:
        owner_args = owner_args or []
        if owner_driver is not None:
            return owner_driver(lifecycle, action, paths.state_dir, allow_timer_catchup,
                                owner_source, owner_args)
        argv = ["/usr/bin/python3", str(owner_source / "installer" / "controlled_backend_lifecycle.py"),
                action, *owner_args]
        if action == "resume" and allow_timer_catchup:
            argv.append("--allow-timer-catchup")
        elif action == "resume" and (paths.stable_source or paths.development_source):
            argv.append("--restore-timers")
        return _run(argv, timeout=1800,
                    host_environment=bool(paths.stable_source or paths.development_source))

    def owner_output(result: subprocess.CompletedProcess, action: str) -> dict:
        try:
            value = json.loads(result.stdout.strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError) as exc:
            raise DeploymentError(f"controlled lifecycle {action} returned no verified result") from exc
        if not isinstance(value, dict):
            raise DeploymentError(f"controlled lifecycle {action} returned an invalid result")
        return value

    def checkpoint_after_stop(result: subprocess.CompletedProcess) -> dict:
        output = owner_output(result, "stop")
        checkpoint = lifecycle._read_checkpoint(state_file)
        if (output.get("status") != "stopped" or output.get("operation_id") != checkpoint["operation_id"]
                or checkpoint["status"] != "pending"
                or checkpoint["backend_manifest_sha256"] != installed_digest):
            raise DeploymentError("stop result does not match the resulting lifecycle checkpoint")
        return checkpoint

    def checkpoint_after_transfer(before: dict, result: subprocess.CompletedProcess) -> dict:
        output = owner_output(result, "transfer")
        checkpoint = lifecycle._read_checkpoint(state_file)
        # The owner upgrades supported legacy checkpoint schemas during the
        # source CAS. Compare against that canonical in-memory form, using the
        # owner's compatibility routine rather than relaxing field checks.
        before_current = lifecycle._checkpoint_as_current_schema(before)
        if (checkpoint["operation_id"] != before["operation_id"]
                or checkpoint["status"] != "pending"
                or checkpoint["backend_manifest_sha256"] != selected_digest
                or output.get("operation_id") != before["operation_id"]
                or output.get("backend_manifest_sha256") != selected_digest):
            raise DeploymentError("transfer result does not match the resulting lifecycle checkpoint")
        if output.get("status") == "transferred":
            old_transitions = before_current["source_transitions"]
            transitions = checkpoint["source_transitions"]
            if (len(transitions) != len(old_transitions) + 1 or transitions[:-1] != old_transitions
                    or transitions[-1].get("operation_id") != before["operation_id"]
                    or transitions[-1].get("from_manifest_sha256") != before["backend_manifest_sha256"]
                    or transitions[-1].get("to_manifest_sha256") != selected_digest
                    or output.get("source_transition_count") != len(transitions)):
                raise DeploymentError("source transfer history does not match the exact owner transition")
            for key in before_current.keys() - {"backend_manifest", "backend_manifest_sha256", "source_transitions"}:
                if checkpoint.get(key) != before_current.get(key):
                    raise DeploymentError("lifecycle checkpoint changed beyond the requested source transfer")
        elif output.get("status") == "already_transferred":
            if checkpoint != before_current:
                raise DeploymentError("idempotent source transfer changed the lifecycle checkpoint")
        else:
            raise DeploymentError("controlled lifecycle returned an unexpected transfer result")
        return checkpoint

    def capture_owner_failure(before: dict | None, expected_operation_id: str | None,
                              stage: str, failure: Exception) -> None:
        """Record only a still-current, owner-authored pending checkpoint; never repair it."""
        lock_fd = None
        try:
            checkpoint = lifecycle._read_checkpoint(state_file)
            if (checkpoint.get("status") != "pending"
                    or checkpoint.get("maintenance_operation") is not None
                    or checkpoint.get("runtime_quiescence") is not None
                    or checkpoint.get("backend_manifest_sha256") not in {installed_digest, selected_digest}):
                return
            operation_id = checkpoint.get("operation_id")
            if expected_operation_id is not None:
                if operation_id != expected_operation_id:
                    return
            elif (before is not None and before.get("status") == "pending") or (
                    before is not None and operation_id == before.get("operation_id")):
                return
            if before is not None and before.get("status") == "pending" and before.get("operation_id") != operation_id:
                return
            checkpoint_snapshot = json.dumps(checkpoint, sort_keys=True, separators=(",", ":"))
            lock_fd, _ = lock_module.acquire(paths.target("opt"), maintenance_mode="shared")
            _assert_requested_source(paths, source, commit)
            if _source_commit(source, paths.owner_uid) != commit:
                return
            current_checkpoint = lifecycle._read_checkpoint(state_file)
            if json.dumps(current_checkpoint, sort_keys=True, separators=(",", ":")) != checkpoint_snapshot:
                return
            if current_checkpoint.get("backend_manifest_sha256") not in {installed_digest, selected_digest}:
                return
            current_identity = _read_identity(paths)
            current_operation = (current_identity.get("operation")
                                 if current_identity.get("status") != "UNKNOWN" else None)
            if isinstance(current_operation, dict) and current_operation.get("status") in {"PENDING", "FAILED"}:
                if (current_operation.get("kind") == "self_upgrade"
                        or current_operation.get("lifecycle_operation_id") != operation_id
                        or current_operation.get("requested_source", {}).get("git_sha") != commit):
                    return
            _record_operation(paths, lock_module, source, commit, lifecycle, ui, operation_id,
                              "pending", "FAILED", current_checkpoint.get("pending_units"),
                              lock_held=True, details={"last_completed_stage": stage,
                                                       "failure": type(failure).__name__})
        except Exception:
            # The original lifecycle failure remains authoritative. A failed
            # best-effort identity publication must not mask or rewrite it.
            return
        finally:
            if lock_fd is not None:
                lock_module.release(lock_fd)

    def owner_transition(action: str, owner_source: Path, owner_args: list[str],
                         before: dict | None, expected_operation_id: str | None,
                         stage: str, validate: Callable[[subprocess.CompletedProcess], dict]) -> dict:
        try:
            result = owner_call(action, owner_source, owner_args)
            if result.returncode != 0:
                raise DeploymentError(f"controlled lifecycle {action} failed")
            validated = validate(result)
            if expected_operation_id is not None and validated.get("operation_id") != expected_operation_id:
                raise DeploymentError("lifecycle operation changed during owner transition")
            return validated
        except Exception as exc:
            capture_owner_failure(before, expected_operation_id, stage, exc)
            raise

    def resume_result(expected_operation_id: str, *, lock_held: bool = False) -> dict:
        before_resume = lifecycle._read_checkpoint(state_file)
        if before_resume["operation_id"] != expected_operation_id:
            raise DeploymentError("lifecycle operation changed before resume; refusing to claim another operation")
        resumed = owner_call("resume", owner_source)
        if resumed.returncode != 0:
            raise DeploymentError("controlled lifecycle resume or API readiness gate failed")
        try:
            outcome = json.loads(resumed.stdout.strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError) as exc:
            raise DeploymentError("controlled lifecycle returned no verified resume result") from exc
        if outcome.get("status") not in {"complete", "pending"}:
            raise DeploymentError("controlled lifecycle returned an unexpected operation state")
        after_resume = lifecycle._read_checkpoint(state_file)
        if (after_resume["operation_id"] != expected_operation_id
                or after_resume["status"] != outcome["status"]):
            raise DeploymentError("lifecycle operation changed during resume; refusing to claim another operation")
        backend_same, ui_same, host_same, healthy = _facts(source, paths, lifecycle, ui)
        if not (backend_same and ui_same and host_same):
            raise DeploymentError("postflight failed: installed backend, UI or host pair differs from source")
        if not healthy and not (paths.stable_source or paths.development_source):
            raise DeploymentError("postflight failed: API, Gateway, Xray or Mihomo is not ready")
        _record_operation(paths, lock_module, source, commit, lifecycle, ui, expected_operation_id,
                          after_resume["status"], "VERIFIED_CONTENT" if outcome["status"] == "complete"
                          else "PENDING", outcome.get("pending_units", []), lock_held=lock_held)
        return {"status": "APPLIED" if outcome["status"] == "complete" else "APPLIED_WITH_PENDING_TIMERS",
                "pending_units": outcome.get("pending_units", []), "source_commit": commit}

    needs_transfer = False
    if pending is not None and pending["status"] == "pending":
        needs_transfer = pending["backend_manifest_sha256"] != selected_digest
    elif installed_digest != selected_digest:
        needs_transfer = True

    resume_only = False
    if (pending is not None and pending["status"] == "pending"
            and pending["backend_manifest_sha256"] == selected_digest
            and installed_digest == selected_digest):
        backend_same, ui_same, host_same, _healthy = _facts(source, paths, lifecycle, ui)
        resume_only = backend_same and ui_same and host_same
        if resume_only:
            api_active = (paths.api_active if paths.test_driver
                          else lifecycle._is_active(lifecycle.API_UNIT))
            if api_active:
                # The owner checkpoint may deliberately retain pending timers;
                # never reinstall over an already-active API on such a retry.
                return resume_result(pending["operation_id"])

    # A UI/host-only publication can be interrupted after the owner has
    # written its pending checkpoint but before it has stopped every unit.
    # The backend digest then still matches both installed and selected source,
    # so there is no source transfer to trigger the recovery stop below. Finish
    # that same owner's stop before taking the publication lock, even when the
    # API happens to be inactive already; only the owner can confirm quiescence.
    if (pending is not None and pending["status"] == "pending"
            and pending["backend_manifest_sha256"] == selected_digest
            and installed_digest == selected_digest and not resume_only):
        pending = owner_transition("stop", owner_source, [], pending, pending["operation_id"],
                                   "lifecycle_stop_recovery", checkpoint_after_stop)

    if needs_transfer and pending is not None and pending["status"] == "pending":
        if pending["backend_manifest_sha256"] != installed_digest:
            raise DeploymentError("interrupted checkpoint and installed backend do not identify the same old source")
        # Re-enter the existing owner's idempotent stop on every old-source
        # transfer resume. A prior stop can have durably checkpointed before a
        # unit-stop error, so apply must confirm quiescence before source CAS.
        if _installed_source_reconstruction_required(paths):
            old_commit = None
        else:
            old_commit = _find_source_commit(source, paths.target("opt/fwrouter-api"), lifecycle)
        with _managed_staging_workspace(paths) as temp:
            old_root = (_materialize_recoverable_old_source(paths, owner_source, lifecycle, temp / "source")
                        if _installed_source_reconstruction_required(paths) else
                        _materialize_old_source(source, old_commit, temp / "source"))
            pending = owner_transition("stop", old_root, [], pending, pending["operation_id"],
                                       "lifecycle_stop_recovery", checkpoint_after_stop)
        transfer_args = ["--expected-operation-id", pending["operation_id"],
                         "--expected-current-manifest-sha256", installed_digest,
                         "--expected-new-manifest-sha256", selected_digest]
        pending = owner_transition("transfer", owner_source, transfer_args, pending,
                                    pending["operation_id"], "lifecycle_source_transfer",
                                    lambda result: checkpoint_after_transfer(pending, result))

    if pending is None or pending["status"] == "complete":
        if installed_digest == selected_digest:
            before_stop = lifecycle._read_checkpoint(state_file) if state_file.exists() else None
            stopped_pending = owner_transition("stop", owner_source, [], before_stop, None,
                                               "lifecycle_stop", checkpoint_after_stop)
        else:
            if _installed_source_reconstruction_required(paths):
                old_commit = None
            else:
                old_commit = _find_source_commit(source, paths.target("opt/fwrouter-api"), lifecycle)
            with _managed_staging_workspace(paths) as temp:
                old_root = (_materialize_recoverable_old_source(paths, owner_source, lifecycle, temp / "source")
                            if _installed_source_reconstruction_required(paths) else
                            _materialize_old_source(source, old_commit, temp / "source"))
                before_stop = lifecycle._read_checkpoint(state_file) if state_file.exists() else None
                pending = owner_transition("stop", old_root, [], before_stop, None,
                                           "lifecycle_stop", checkpoint_after_stop)
                if (pending["status"] != "pending"
                        or pending["backend_manifest_sha256"] != installed_digest):
                    raise DeploymentError("old-source stop checkpoint does not match the installed backend")
                transfer_args = ["--expected-operation-id", pending["operation_id"],
                                 "--expected-current-manifest-sha256", installed_digest,
                                 "--expected-new-manifest-sha256", selected_digest]
                pending = owner_transition("transfer", owner_source, transfer_args, pending,
                                            pending["operation_id"], "lifecycle_source_transfer",
                                            lambda result: checkpoint_after_transfer(pending, result))
        if installed_digest == selected_digest:
            pending = stopped_pending
        if pending["backend_manifest_sha256"] != selected_digest:
            raise DeploymentError("lifecycle checkpoint does not bind the selected backend")

    # Preserve the pre-stop installed tree and exact owner transition result as
    # the lock-gap baseline. Never refresh either value after stop/CAS: doing so
    # could bless a concurrent install that ran before this mutex was acquired.
    installed_before_lock = installed_manifest
    checkpoint_before_lock = pending
    if (checkpoint_before_lock is None or checkpoint_before_lock["status"] != "pending"
            or checkpoint_before_lock["backend_manifest_sha256"] != selected_digest):
        raise DeploymentError("lifecycle checkpoint changed after source stop/CAS")
    checkpoint_bytes_before_lock = json.dumps(
        checkpoint_before_lock, sort_keys=True, separators=(",", ":"))

    lock_fd = maintenance_fd = saved_maintenance_target = saved_installer_target = None
    host_plan = None
    operation_id = checkpoint_before_lock["operation_id"]
    completed_stage = "lifecycle_stop_and_source_cas"
    try:
        lock_fd, maintenance_fd, saved_maintenance_target, saved_installer_target = _component_lock(lock_module, paths)
        _assert_requested_source(paths, source, commit)
        if _source_commit(source, paths.owner_uid) != commit:
            raise DeploymentError("selected source changed before publication")
        checkpoint_under_lock = lifecycle._read_checkpoint(state_file)
        if json.dumps(checkpoint_under_lock, sort_keys=True, separators=(",", ":")) != checkpoint_bytes_before_lock:
            raise DeploymentError("lifecycle checkpoint changed before publication")
        installed_under_lock = lifecycle._backend_manifest(paths.target("opt/fwrouter-api"))
        if installed_under_lock != installed_before_lock:
            raise DeploymentError("installed backend changed before publication")
        stopped_units = (*lifecycle.RESTORE_ORDER, *lifecycle.PERSISTENT_TIMERS,
                         *lifecycle.TRANSIENT_JOBS, lifecycle.API_UNIT)
        units_stopped = (paths.lifecycle_units_stopped if paths.test_driver
                         else not any(lifecycle._is_active(unit) for unit in stopped_units))
        if not units_stopped:
            raise DeploymentError("lifecycle-owned units must remain stopped before source publication")
        _record_operation(paths, lock_module, source, commit, lifecycle, ui, operation_id,
                          checkpoint_under_lock["status"], "PENDING", lock_held=True)
        if not resume_only:
            _normalize_legacy_ui(paths.target("opt/fwrouter-ui"), paths.owner_uid)
            _rows, host_plan = _host_plan(source, paths)
            if host_plan is not None:
                _publish_host_pair(source, paths, host_plan)
            changed_components = ["backend", "ui"]
            if paths.stable_source or paths.development_source:
                changed_components = []
                if installed_under_lock != selected_manifest:
                    changed_components.append("backend")
                try:
                    source_ui_digest, _ = ui._walk_tree(source / "ui", owner_uid=paths.owner_uid, require_index=True)
                    active_ui = ui._managed_release(paths.target("opt/fwrouter-ui"),
                        paths.target("opt/.fwrouter-ui-releases"), paths.owner_uid) or paths.target("opt/fwrouter-ui")
                    installed_ui_digest, _ = ui._walk_tree(active_ui, owner_uid=paths.owner_uid, require_index=True)
                    if source_ui_digest != installed_ui_digest:
                        changed_components.append("ui")
                except (OSError, RuntimeError, ValueError):
                    changed_components.append("ui")
            if changed_components:
                if "backend" in changed_components and paths.retire_paths:
                    _retire_installed_backend_files(paths, source, lifecycle, paths.retire_paths)
                _run_installer(source, paths, changed_components, inherited=True)
        completed_stage = "component_publication"
        backend_same, ui_same, host_same, _healthy = _facts(source, paths, lifecycle, ui)
        if not (backend_same and ui_same and host_same):
            raise DeploymentError("installed backend manifest, UI release or host pair differs from selected source before API startup")
        _assert_requested_source(paths, source, commit)
        # The API acquires the shared maintenance lease while starting. Release
        # only the installer-held exclusive lease; keep the component mutex.
        if maintenance_fd is not None:
            lock_module._OWNED_MAINTENANCE_FDS.pop(lock_fd, None)
            os.close(maintenance_fd)
            maintenance_fd = None
            _restore_child_fd(MAINTENANCE_LOCK_FD, saved_maintenance_target)
            saved_maintenance_target = None
        systemctl = ["/usr/bin/systemctl", "start", "fwrouter-api.service"]
        if paths.target_root == Path("/"):
            result = (system_driver(systemctl) if system_driver is not None
                      else _run(systemctl, host_environment=bool(paths.stable_source or paths.development_source)))
            if result.returncode != 0:
                raise DeploymentError("API unit did not start after source installation")
        elif system_driver is not None:
            result = system_driver(systemctl)
            if result.returncode != 0:
                raise DeploymentError("API unit did not start after source installation")
        else:
            service_stub = paths.target_root / ".fwrouter-test-systemd-start"
            if not service_stub.exists():
                raise DeploymentError("test driver must provide its disposable systemd marker")
        if not paths.test_driver:
            deadline = time.monotonic() + 180
            healthy = False
            while time.monotonic() < deadline:
                healthy = lifecycle._api_health()
                if healthy:
                    break
                time.sleep(0.5)
            if not healthy:
                raise DeploymentError("API did not become healthy within the bounded startup window")
        completed_stage = "api_start"
        return resume_result(operation_id, lock_held=True)
    except Exception as exc:
        try:
            checkpoint_now = lifecycle._read_checkpoint(state_file)
            _record_operation(paths, lock_module, source, commit, lifecycle, ui, operation_id,
                              checkpoint_now["status"], "FAILED",
                              checkpoint_now.get("pending_units"), lock_held=lock_fd is not None,
                              details={"last_completed_stage": completed_stage,
                                       "failure": type(exc).__name__})
        except Exception:
            pass
        raise
    finally:
        if host_plan is not None:
            try:
                host_plan.unlink()
            except FileNotFoundError:
                pass
        if lock_fd is not None:
            _restore_child_fd(INSTALLER_LOCK_FD, saved_installer_target)
            if maintenance_fd is not None:
                _restore_child_fd(MAINTENANCE_LOCK_FD, saved_maintenance_target)
        if lock_fd is not None:
            lock_module.release(lock_fd)


def _stage(result: dict, name: str, status: str, detail: str) -> None:
    result["stages"][name] = {"status": status, "detail": detail}


def _json_object(output: str) -> dict:
    try:
        value = json.loads(output.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return {}
    return value if isinstance(value, dict) else {}


def _stable_service_state(paths: Paths, lifecycle) -> None:
    """Verify that the API unit and read-only owner CLI use the canonical state root."""
    result = _run(["/usr/bin/systemctl", "show", "--property=LoadState,Environment,EnvironmentFiles,DropInPaths,"
                   "PassEnvironment,MainPID", lifecycle.API_UNIT], timeout=10, host_environment=True)
    rows = [line.split("=", 1) for line in result.stdout.splitlines() if "=" in line]
    props = dict(rows)
    env_file = paths.target("opt/fwrouter-api/.env")
    if (result.returncode or len(rows) != len(props) or props.get("LoadState") != "loaded"
            or props.get("DropInPaths", "") != ""
            or props.get("PassEnvironment", "") != ""
            or props.get("EnvironmentFiles") != f"{env_file} (ignore_errors=yes)"):
        raise DeploymentError("API unit environment is outside the supported Stable state-root contract.")
    override = rb"\b(?:FWROUTER_STATE_DIR|STATE_DIR)[ \t]*="
    if re.search(override, props.get("Environment", "").encode()):
        raise DeploymentError("API unit overrides the canonical Stable state root.")
    try:
        if os.path.lexists(env_file) and re.search(
                rb"(?m)^[ \t]*(?:FWROUTER_STATE_DIR|STATE_DIR)[ \t]*=",
                _read_stable_source_file(env_file, paths.owner_uid, limit=128 * 1024)):
            raise DeploymentError("API environment file overrides the canonical Stable state root.")
        pid = props.get("MainPID", "")
        if not pid.isascii() or not pid.isdecimal():
            raise DeploymentError("API unit process identity is unavailable.")
        if int(pid):
            env_path = Path("/proc") / pid / "environ"
            with env_path.open("rb") as stream:
                environment = stream.read(1024 * 1024 + 1)
            if len(environment) > 1024 * 1024 or not environment:
                raise DeploymentError("API process environment readback is unavailable.")
            if any(entry.startswith((b"FWROUTER_STATE_DIR=", b"STATE_DIR=")) for entry in environment.split(b"\0")):
                raise DeploymentError("Running API uses a custom state root.")
    except OSError as exc:
        raise DeploymentError("API environment could not be inspected safely.") from exc


DEPLOYMENT_LEGACY_SCHEMA27 = "legacy_migrated_schema27_manual_ping_additive"

_READ_INSTALLED_SCHEMA27 = """
import json, sqlite3, sys
from pathlib import Path
from fwrouter_api.core.config import get_settings
from fwrouter_api.cli import _validate_database_path
from fwrouter_api.db.schema_state import inspect_database_schema
p = Path(sys.argv[1])
if get_settings().paths.db_path != p:
    raise SystemExit(1)
_validate_database_path(p)
with sqlite3.connect(p.as_uri() + "?mode=ro", uri=True, timeout=5) as db:
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    db.execute("BEGIN")
    schema = inspect_database_schema(db, allow_legacy_schema27_profiles=True)
    if (not schema["ok"] or str(schema["actual_schema_version"]) != "27"
            or schema.get("compatibility_profile") != sys.argv[2]
            or [row[0] for row in db.execute("PRAGMA integrity_check")] != ["ok"]
            or db.execute("PRAGMA foreign_key_check").fetchone() is not None):
        raise SystemExit(1)
print(json.dumps({"ok": True, "compatibility_profile": sys.argv[2]}))
"""


def _strict_database_preflight(paths: Paths, lifecycle, source: Path, *, legacy_rollback: bool = False) -> str:
    if paths.test_driver:
        return "fixture"
    _stable_service_state(paths, lifecycle)
    python = str(lifecycle._verified_backend_interpreter())
    database = str(paths.target("var/lib/fwrouter-v2/fwrouter.db"))
    expected = {"ok": True, "status": "already_current", "current_schema": 27,
                "target_schema": 27, "database_changed": False}
    canonical = ["database", "upgrade", "--expected-schema", "27"]
    source_cli = ("import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); "
                  "from fwrouter_api.core.config import get_settings; "
                  "sys.exit(1) if get_settings().paths.db_path != Path(sys.argv[2]) else None; "
                  "from fwrouter_api.cli import main; raise SystemExit(main(sys.argv[3:]))")
    base = [python, "-I", "-B"]
    installed = [*base, "-m", "fwrouter_api.cli"]
    candidate = [*base, "-c", source_cli, str(source / "backend"), database]
    legacy = False
    for label, entry in (("installed", installed), ("selected", candidate)):
        result = _run([*entry, *canonical], timeout=120, host_environment=True)
        payload = _json_object(result.stdout)
        if not result.returncode and payload == expected and not legacy:
            continue
        if label == "installed" and payload == {"ok": False, "error_code": "DATABASE_SCHEMA_PREFLIGHT_FAILED"}:
            # D1 has the exact profile inspector but predates the explicit
            # compatibility command. Keep this independent installed-side test.
            readback = _run([*base, "-c", _READ_INSTALLED_SCHEMA27, database, DEPLOYMENT_LEGACY_SCHEMA27],
                            timeout=120, host_environment=True)
            legacy = not readback.returncode and _json_object(readback.stdout) == {
                "ok": True, "compatibility_profile": DEPLOYMENT_LEGACY_SCHEMA27}
            if legacy:
                continue
        if label == "selected" and legacy:
            compatibility = _run([*entry, "database", "preflight", "--expected-schema", "27",
                                  "--compatibility-profile", DEPLOYMENT_LEGACY_SCHEMA27],
                                 timeout=120, host_environment=True)
            if not compatibility.returncode and _json_object(compatibility.stdout) == {
                    **expected, "compatibility_profile": DEPLOYMENT_LEGACY_SCHEMA27,
                    "integrity": "ok", "foreign_key_violations": 0}:
                continue
            # A pinned historical rollback can predate the CLI subcommand.
            # Keep both exact read-only checks and use its own legacy inspector.
            if legacy_rollback and paths.stable_source is not None and compatibility.returncode == 2:
                historical_reader = ("import sys; sys.path.insert(0, sys.argv.pop(1)); "
                                     + _READ_INSTALLED_SCHEMA27)
                historical = _run([*base, "-c", historical_reader, str(source / "backend"),
                                   database, DEPLOYMENT_LEGACY_SCHEMA27], timeout=120, host_environment=True)
                if not historical.returncode and _json_object(historical.stdout) == {
                        "ok": True, "compatibility_profile": DEPLOYMENT_LEGACY_SCHEMA27}:
                    continue
        reason = payload.get("error_code")
        if (not isinstance(reason, str) or not reason.startswith("DATABASE_")
                or len(reason) > 100 or not reason.replace("_", "").isalnum()):
            reason = "DATABASE_SCHEMA_PREFLIGHT_UNVERIFIED"
        raise DeploymentError(f"Database preflight ({label}) {reason}. The schema or compatibility profile could not be verified.")
    return DEPLOYMENT_LEGACY_SCHEMA27 if legacy else "canonical"


def _verify_dependency_compatibility(paths: Paths, source: Path) -> None:
    if paths.test_driver:
        return
    installed = paths.target("opt/fwrouter-api/pyproject.toml")
    candidate = source / "backend/pyproject.toml"
    try:
        dependency_contract = [tomllib.loads(_read_stable_source_file(path, paths.owner_uid).decode())["project"]
                               for path in (installed, candidate)]
    except (OSError, KeyError, ValueError) as exc:
        raise DeploymentIncomplete("The installed and selected backend dependency contracts could not be compared.") from exc
    old, new = dependency_contract
    for key in ("requires-python", "dependencies"):
        if old.get(key) != new.get(key):
            raise DeploymentIncomplete("The backend dependency contract changed. Qualify the prepared runtime before deploying.")


def _read_acceptance_policy(paths: Paths) -> dict:
    path = paths.target("etc/fwrouter/deploy-acceptance.json")
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {}
    try:
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise DeploymentError("deployment acceptance policy must be a regular file")
        _check_trusted_tree(path.parent, paths.owner_uid)
        if stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != paths.owner_uid or info.st_nlink != 1:
            raise DeploymentError("deployment acceptance policy must be owner-only, standalone and mode 0600")
        data = _strict_policy_json(_read_stable_source_file(path, paths.owner_uid, limit=4096))
    except (OSError, ValueError, UnicodeError) as exc:
        raise DeploymentError("deployment acceptance policy cannot be read safely") from exc
    return _validate_acceptance_policy(data)


def _strict_policy_json(payload: bytes) -> dict:
    def unique_object(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate JSON key")
            value[key] = item
        return value

    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=unique_object,
                           parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("nonstandard constant")))
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise DeploymentError("acceptance policy must be one strict JSON object without duplicate keys") from exc
    if not isinstance(value, dict):
        raise DeploymentError("acceptance policy must be one strict JSON object")
    return value


def _validate_acceptance_policy(data: dict) -> dict:
    if not data:
        raise DeploymentError("deployment acceptance policy is empty or malformed JSON")
    if data.get("schema") != "fwrouter-deploy-acceptance/v1" or set(data) - {"recovery"} != {
            "schema", "xray_image", "mihomo_image", "ui_port", "ui_path"}:
        raise DeploymentError("deployment acceptance policy is malformed")
    if any(not isinstance(data[key], str) or not data[key].startswith("sha256:")
           or not _hex(data[key][7:], 64) for key in ("xray_image", "mihomo_image")):
        raise DeploymentError("deployment acceptance policy requires independently approved runtime image IDs")
    if (not _positive_id(data["ui_port"]) or data["ui_port"] > 65535
            or not isinstance(data["ui_path"], str) or not data["ui_path"].startswith("/")
            or data["ui_path"].startswith("//") or len(data["ui_path"]) > 256
            or not all(32 < ord(char) < 127 for char in data["ui_path"])):
        raise DeploymentError("deployment acceptance policy has an invalid loopback UI endpoint")
    recovery = data.get("recovery")
    if "recovery" in data and (
            not isinstance(recovery, dict)
            or set(recovery) != {"backup_path", "backup_sha256", "installed_backend_sha256"}
            or not isinstance(recovery.get("backup_path"), str)
            or not Path(recovery["backup_path"]).is_absolute()
            or ".." in Path(recovery["backup_path"]).parts
            or not _hex(recovery.get("backup_sha256"), 64)
            or not _hex(recovery.get("installed_backend_sha256"), 64)):
        raise DeploymentError("deployment acceptance policy has an invalid recovery backup pin")
    return data


def _initialize_acceptance_policy(paths: Paths, source: Path) -> str:
    """Install an independently approved policy without deriving or replacing approval."""
    try:
        _check_trusted_tree(source.parent, paths.owner_uid)
        info = source.lstat()
        if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or info.st_uid != paths.owner_uid or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 4096):
            raise DeploymentError("approved policy input must be an owner-only mode-0600 regular file")
        payload = _read_stable_source_file(source, paths.owner_uid, limit=4096)
        after = source.lstat()
        stable_fields = ("st_dev", "st_ino", "st_mode", "st_uid", "st_nlink", "st_size", "st_mtime_ns", "st_ctime_ns")
        if any(getattr(after, name) != getattr(info, name) for name in stable_fields):
            raise DeploymentError("approved policy input changed during validation")
        policy = _strict_policy_json(payload)
        _validate_acceptance_policy(policy)
    except (OSError, UnicodeError) as exc:
        raise DeploymentError("approved policy input cannot be read safely") from exc

    destination = paths.target("etc/fwrouter/deploy-acceptance.json")
    parent = destination.parent
    _check_trusted_tree(parent, paths.owner_uid)
    canonical = (json.dumps(policy, sort_keys=True, separators=(",", ":")) + "\n").encode()
    if len(canonical) > 4096:
        raise DeploymentError("validated acceptance policy exceeds the supported size limit")

    def sync_policy_directory() -> None:
        descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    try:
        existing_info = destination.lstat()
    except FileNotFoundError:
        existing_info = None
    if existing_info is not None:
        existing = _read_acceptance_policy(paths)
        if existing == policy:
            sync_policy_directory()
            return "NOOP"
        raise DeploymentError("a different acceptance policy is already installed; only the runtime owner may rotate it")

    directory_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temp_name = f".deploy-acceptance.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    try:
        fd = os.open(temp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=directory_fd)
        with os.fdopen(fd, "wb") as stream:
            stream.write(canonical)
            stream.flush()
            os.fsync(stream.fileno())
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = getattr(libc, "renameat2", None)
        if renameat2 is None:
            raise DeploymentError("atomic no-replace policy publication is unavailable on this host")
        renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        renameat2.restype = ctypes.c_int
        if renameat2(directory_fd, os.fsencode(temp_name), directory_fd,
                     os.fsencode(destination.name), 1) != 0:  # RENAME_NOREPLACE
            error = ctypes.get_errno()
            if error == 17:  # EEXIST
                existing = _read_acceptance_policy(paths)
                if existing == policy:
                    os.unlink(temp_name, dir_fd=directory_fd)
                    os.fsync(directory_fd)
                    return "NOOP"
                raise DeploymentError("a different acceptance policy is already installed; only the runtime owner may rotate it")
            raise OSError(error, os.strerror(error))
        os.fsync(directory_fd)
    except BaseException:
        try:
            os.unlink(temp_name, dir_fd=directory_fd)
        except OSError:
            pass
        raise
    finally:
        os.close(directory_fd)
    installed = _read_acceptance_policy(paths)
    if installed != policy or stat.S_IMODE(destination.stat().st_mode) != 0o600:
        raise DeploymentError("installed acceptance policy failed post-publication verification")
    return "INSTALLED"


def _runtime_image_evidence(paths: Paths, policy: dict) -> tuple[str, str, dict]:
    if paths.test_driver:
        return "SKIP", "disposable target has no native runtime", policy
    images = _observed_runtime_image_ids(paths)
    configured = _configured_runtime_image_ids(paths)
    mismatch = [name for name in ("xray", "mihomo")
                if images.get(name) not in (None, "UNKNOWN")
                and any(expected not in (None, "UNKNOWN") and images[name] != expected
                        for expected in (configured.get(name), policy.get(f"{name}_image")))]
    if mismatch:
        return "FAIL", "Running runtime images differ from their configured identities: " + ", ".join(mismatch), policy
    unavailable = [name for name in ("xray", "mihomo")
                   if images.get(name) in (None, "UNKNOWN") or configured.get(name) in (None, "UNKNOWN")]
    if unavailable:
        return "INCOMPLETE", ("Cannot establish configured and running image identity for "
                              + ", ".join(unavailable) + ". Verify the installed Compose image and container."), policy
    return ("PASS", "Running Xray and Mihomo match their installed Compose references.",
            {**policy, "xray_image": configured["xray"], "mihomo_image": configured["mihomo"]})


def _native_policy_evidence(paths: Paths, policy: dict) -> tuple[str, str]:
    status, detail, _ = _runtime_image_evidence(paths, policy)
    return status, detail


def _require_acceptance_policy(paths: Paths, policy: dict | None = None) -> dict:
    status, detail, expectations = _runtime_image_evidence(
        paths, _read_acceptance_policy(paths) if policy is None else policy)
    if status == "INCOMPLETE":
        raise DeploymentIncomplete(detail)
    if status == "FAIL":
        raise DeploymentError(detail)
    return expectations


def _read_installed_runtime_config(paths: Paths, relative: str, *, limit: int,
                                   historical_blob: str | None = None) -> bytes:
    path = paths.target(relative)
    observed = []
    verify_historical_bytes = False
    for item in (*reversed(path.parents), path):
        if item == paths.target_root or not item.is_relative_to(paths.target_root):
            continue
        info = item.lstat()
        if (info.st_uid != paths.owner_uid or info.st_mode & stat.S_IWOTH
                or (item != path and not stat.S_ISDIR(info.st_mode))):
            raise DeploymentError("installed runtime configuration path is not owner-controlled")
        if info.st_mode & stat.S_IWGRP:
            if historical_blob is None or info.st_uid != 0 or info.st_gid != 0:
                raise DeploymentError("installed runtime configuration path is group-writable")
            verify_historical_bytes = True
        observed.append((item, info))
    payload = _read_stable_source_file(path, paths.owner_uid, limit=limit)
    if verify_historical_bytes:
        # Older installations shipped group-writable Compose paths. Their
        # bytes can only establish expectations if independently reconstructed
        # from the exact configured, trusted release checkout.
        source, sha = _read_config(paths.config, paths.owner_uid)
        if payload != _git_blob_bytes(source, sha, historical_blob):
            raise DeploymentError("group-writable Compose bytes differ from the recorded source")
    for item, info in observed:
        after = item.lstat()
        fields = ("st_dev", "st_ino", "st_mode", "st_uid", "st_gid", "st_ctime_ns")
        if any(getattr(info, key) != getattr(after, key) for key in fields):
            raise DeploymentError("installed runtime configuration path changed during inspection")
    return payload


def _configured_runtime_image_ids(paths: Paths) -> dict:
    """Resolve configured image references only from installed Compose and local Docker metadata."""
    if paths.test_driver or not Path("/usr/bin/docker").is_file():
        return {"xray": "UNKNOWN", "mihomo": "UNKNOWN"}
    result = {}
    for component in ("xray", "mihomo"):
        try:
            payload = _read_installed_runtime_config(
                paths, f"opt/fwrouter-{component}/docker-compose.yml", limit=256 * 1024,
                historical_blob=f"runtimes/{component}/docker-compose.yml").decode("utf-8")
            refs = re.findall(r"^\s{4}image:\s*([^\s#]+)\s*(?:#.*)?$", payload, flags=re.MULTILINE)
            if len(refs) != 1:
                result[component] = "UNKNOWN"
                continue
            ref = refs[0]
            if not ref or ref in {"latest", ""} or any(ch.isspace() for ch in ref):
                result[component] = "UNKNOWN"
                continue
            inspected = _run(["/usr/bin/docker", "--host", "unix:///var/run/docker.sock", "image", "inspect",
                              "--format", "{{.Id}}", ref], timeout=10, host_environment=True)
            image_id = inspected.stdout.strip()
            result[component] = (image_id if inspected.returncode == 0 and image_id.startswith("sha256:")
                                 and _hex(image_id[7:], 64) else "UNKNOWN")
        except (DeploymentError, OSError, UnicodeError):
            result[component] = "UNKNOWN"
    return result


def _verify_backend_recovery(paths: Paths, lifecycle, policy: dict, old_manifest: str) -> dict:
    if paths.test_driver:
        return {"status": "SKIP", "detail": "disposable installer target"}
    recovery = policy.get("recovery")
    if (not isinstance(recovery, dict)
            or set(recovery) != {"backup_path", "backup_sha256", "installed_backend_sha256"}
            or recovery.get("installed_backend_sha256") != old_manifest
            or not _hex(recovery.get("backup_sha256"), 64)
            or not isinstance(recovery.get("backup_path"), str)):
        raise DeploymentIncomplete("A qualified recovery backup pin for the current backend is required before backend publication.")
    path = Path(recovery["backup_path"])
    try:
        _check_trusted_tree(path.parent, paths.owner_uid)
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != paths.owner_uid
                or stat.S_IMODE(info.st_mode) != 0o600):
            raise DeploymentError("recovery backup is not an owner-only standalone database")
        for suffix in ("-wal", "-shm", "-journal"):
            if path.with_name(path.name + suffix).exists():
                raise DeploymentError("recovery backup has a SQLite sidecar")
        python = lifecycle._verified_backend_interpreter()
        script = (
            "import sys; from pathlib import Path; "
            "from fwrouter_api.services.database_admin import _validate_database_file, _hash_regular_file; "
            "p = Path(sys.argv[1]); schema = _validate_database_file(p, allow_legacy_schema27_profiles=True); "
            "raise SystemExit(0 if schema.get('compatibility_profile') in (None, sys.argv[3]) and _hash_regular_file(p) == sys.argv[2] else 1)"
        )
        verified = _run([str(python), "-I", "-B", "-c", script, str(path), recovery["backup_sha256"], DEPLOYMENT_LEGACY_SCHEMA27],
                        timeout=180, host_environment=True)
        after = path.lstat()
        fields = ("st_dev", "st_ino", "st_mode", "st_uid", "st_nlink", "st_size", "st_mtime_ns", "st_ctime_ns")
        if verified.returncode or any(getattr(after, key) != getattr(info, key) for key in fields):
            raise DeploymentError("recovery backup failed structural or pinned content verification")
    except OSError as exc:
        raise DeploymentError("recovery backup could not be read safely") from exc
    return {"status": "REUSED", "detail": "approved backup schema, integrity, foreign keys and SHA-256 verified"}


def _valid_managed_backup(value: object, *, complete: bool) -> bool:
    keys = {"id", "source_sha", "backend_sha256", "schema"}
    if complete:
        keys.update(("filename", "sha256", "snapshot_sha256"))
    return (isinstance(value, dict) and value.keys() == keys
            and _hex(value["id"], 32) and _hex(value["source_sha"], 40)
            and _hex(value["backend_sha256"], 64)
            and type(value["schema"]) is int and value["schema"] == 27
            and (not complete or (
                isinstance(value["filename"], str)
                and re.fullmatch(r"rollback-[0-9a-f]{32}\.bak", value["filename"]) is not None
                and _hex(value["sha256"], 64) and _hex(value["snapshot_sha256"], 64))))


def _backup_directory(paths: Paths, pin: dict) -> Path:
    return paths.state_dir / "deploy-rollback-backups" / pin["id"]


def _protected_backup_file(paths: Paths, path: Path) -> os.stat_result:
    _check_trusted_tree(path.parent, paths.owner_uid)
    if (path.parent.stat().st_uid != paths.owner_uid or stat.S_IMODE(path.parent.stat().st_mode) != 0o700):
        raise DeploymentError("managed rollback backup directory is not private")
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or info.st_uid != paths.owner_uid or stat.S_IMODE(info.st_mode) != 0o600
            or any(os.path.lexists(path.with_name(path.name + suffix)) for suffix in ("-wal", "-shm", "-journal"))):
        raise DeploymentError("managed rollback backup is not a private standalone database")
    return info


def _backup_file_digest(path: Path, before: os.stat_result) -> str:
    fields = ("st_dev", "st_ino", "st_mode", "st_uid", "st_nlink", "st_size", "st_mtime_ns", "st_ctime_ns")
    signature = tuple(getattr(before, name) for name in fields)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    digest = hashlib.sha256()
    with os.fdopen(fd, "rb") as stream:
        if tuple(getattr(os.fstat(fd), name) for name in fields) != signature:
            raise DeploymentError("managed rollback backup changed before verification")
        while block := stream.read(1024 * 1024):
            digest.update(block)
        if (tuple(getattr(os.fstat(fd), name) for name in fields) != signature
                or tuple(getattr(path.lstat(), name) for name in fields) != signature):
            raise DeploymentError("managed rollback backup changed during verification")
    return digest.hexdigest()


def _sqlite_backup_command(lifecycle, script: str, *args: str) -> dict:
    python = lifecycle._verified_backend_interpreter()
    result = _run([str(python), "-I", "-B", "-c", script, *args], timeout=180, host_environment=True)
    data = _json_object(result.stdout)
    if result.returncode or not data:
        raise DeploymentError("SQLite backup owner could not verify a recoverable schema-27 snapshot")
    return data


_READ_BACKUP_SNAPSHOT = """
import json, sqlite3, sys
from pathlib import Path
from fwrouter_api.services.database_admin import _validate_database_file
from fwrouter_api.services.database_backup import _logical_digest
path = Path(sys.argv[1])
schema = _validate_database_file(path, allow_legacy_schema27_profiles=True)
if schema.get("compatibility_profile") not in (None, sys.argv[2]):
    raise SystemExit(1)
with sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True) as connection:
    connection.execute("PRAGMA query_only=ON")
    print(json.dumps({"schema": int(schema["actual_schema_version"]), "snapshot_sha256": _logical_digest(connection)}))
"""


def _validate_managed_backup(paths: Paths, lifecycle, pin: dict) -> None:
    if not _valid_managed_backup(pin, complete=True):
        raise DeploymentError("managed rollback backup pin is malformed")
    path = _backup_directory(paths, pin) / pin["filename"]
    before = _protected_backup_file(paths, path)
    result = _sqlite_backup_command(lifecycle, _READ_BACKUP_SNAPSHOT, str(path), DEPLOYMENT_LEGACY_SCHEMA27)
    if (result != {"schema": 27, "snapshot_sha256": pin["snapshot_sha256"]}
            or _backup_file_digest(path, before) != pin["sha256"]):
        raise DeploymentError("managed rollback backup does not match its schema and digest pin")


def _recover_backup_reservation(paths: Paths, directory: Path) -> list[Path]:
    """Remove only interrupted SQLite-owner scratch inside a reserved directory."""
    _check_trusted_tree(directory, paths.owner_uid)
    info = directory.lstat()
    if info.st_uid != paths.owner_uid or stat.S_IMODE(info.st_mode) != 0o700:
        raise DeploymentError("SQLite backup reservation is not private")
    entries = list(directory.iterdir())
    published = [entry for entry in entries if re.fullmatch(r"rollback-[0-9a-f]{32}\.bak", entry.name)]
    scratch = [entry for entry in entries if re.fullmatch(r"\.backup-tmp-[0-9a-f]{32}", entry.name)]
    if len(published) > 1 or len(entries) != len(published) + len(scratch):
        raise DeploymentIncomplete("Managed backup reservation contains unrecognised recovery data.")
    known_final = published[0].lstat() if published else None
    removals = {}
    for temporary in scratch:
        before = temporary.lstat()
        if (not stat.S_ISDIR(before.st_mode) or before.st_uid != paths.owner_uid
                or stat.S_IMODE(before.st_mode) != 0o700 or os.path.ismount(temporary)):
            raise DeploymentIncomplete("Interrupted SQLite backup scratch has unknown ownership.")
        children = list(temporary.iterdir())
        for child in children:
            child_info = child.lstat()
            linked_publication = (child.name == "database.sqlite" and known_final is not None
                                  and stat.S_ISREG(known_final.st_mode)
                                  and known_final.st_uid == paths.owner_uid and known_final.st_nlink == 2
                                  and (child_info.st_dev, child_info.st_ino) == (known_final.st_dev, known_final.st_ino))
            if (child.name not in {"database.sqlite", "database.sqlite-journal", "database.sqlite-wal", "database.sqlite-shm"}
                    or not stat.S_ISREG(child_info.st_mode) or child_info.st_uid != paths.owner_uid
                    or child_info.st_dev != before.st_dev or (child_info.st_nlink != 1 and not linked_publication)):
                raise DeploymentIncomplete("Interrupted SQLite backup contains unrecognised scratch files.")
        removals[temporary] = children
    for temporary, children in removals.items():
        for child in children:
            child.unlink()
        temporary.rmdir()
    if removals:
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    return published


def _prepare_managed_backup(paths: Paths, lifecycle, reservation: dict) -> dict:
    """Use the SQLite backup owner inside a durably reserved private directory."""
    if not _valid_managed_backup(reservation, complete=False):
        raise DeploymentError("managed rollback backup reservation is malformed")
    directory = _backup_directory(paths, reservation)
    _check_trusted_tree(paths.state_dir, paths.owner_uid)
    directory.parent.mkdir(mode=0o700, exist_ok=True)
    _check_trusted_tree(directory.parent, paths.owner_uid)
    if directory.parent.stat().st_uid != paths.owner_uid or stat.S_IMODE(directory.parent.stat().st_mode) != 0o700:
        raise DeploymentError("managed rollback backup root is not private")
    entries = _recover_backup_reservation(paths, directory) if os.path.lexists(directory) else []
    if entries:
        path = entries[0]
        before = _protected_backup_file(paths, path)
        # This reservation was durable before creation. The SQLite owner
        # exposes only a complete atomic publication under the final name.
        result = _sqlite_backup_command(lifecycle, _READ_BACKUP_SNAPSHOT, str(path), DEPLOYMENT_LEGACY_SCHEMA27)
        pin = {**reservation, "filename": path.name, "sha256": _backup_file_digest(path, before),
               "snapshot_sha256": result.get("snapshot_sha256")}
        if result.get("schema") != 27 or not _valid_managed_backup(pin, complete=True):
            raise DeploymentError("interrupted SQLite backup failed strict verification")
        return pin

    result = _sqlite_backup_command(lifecycle, """
import json, sys
from pathlib import Path
from fwrouter_api.services.database_backup import create_sqlite_backup
print(json.dumps(create_sqlite_backup(Path(sys.argv[1]), Path(sys.argv[2]), prefix="rollback-")))
""", str(paths.target("var/lib/fwrouter-v2/fwrouter.db")), str(directory))
    filename = result.get("backup_path")
    if not isinstance(filename, str):
        raise DeploymentError("SQLite backup owner returned no publication path")
    path = Path(filename)
    if path.parent != directory or result.get("source_snapshot") != "sqlite_read_transaction":
        raise DeploymentError("SQLite backup owner returned unexpected publication metadata")
    pin = {**reservation, "filename": path.name, "sha256": result.get("backup_sha256"),
           "snapshot_sha256": result.get("snapshot_sha256")}
    _validate_managed_backup(paths, lifecycle, pin)
    return pin


def _ensure_dev_baseline_backup(paths: Paths, lifecycle, policy: dict, result: dict, *,
                                returning: bool = False) -> None:
    """Protect the accepted Stable database without replacing its rollback backup."""
    if paths.test_driver:
        result["backup"] = {"status": "SKIP", "detail": "disposable installer target"}
        return
    with _configuration_component_lock(paths):
        record = _read_identity(paths)
        admission = "stable_admission" if returning else "dev_admission"
        if record.get(admission) is None:
            raise DeploymentError("deployment admission changed before SQLite recovery verification")
        stable = record.get("stable_current") or _accepted_stable_pin(record.get("last_verified_install"))
        actual = lifecycle._manifest_digest(lifecycle._backend_manifest(paths.target("opt/fwrouter-api")))
        if stable is None:
            result["backup"] = _verify_backend_recovery(paths, lifecycle, policy, actual)
            return
        expected = {"source_sha": stable["source_sha"], "schema": 27,
                    "backend_sha256": stable["installed_content"]["backend_sha256"]}
        for key in ("database_rollback", "dev_baseline_backup"):
            pin = record.get(key)
            if (_valid_managed_backup(pin, complete=True)
                    and all(pin.get(field) == value for field, value in expected.items())):
                _validate_managed_backup(paths, lifecycle, pin)
                result["backup"] = {"status": "REUSED", "detail": "verified SQLite backup for the accepted Stable baseline"}
                return
        approved = policy.get("recovery")
        if isinstance(approved, dict) and approved.get("installed_backend_sha256") == expected["backend_sha256"]:
            result["backup"] = _verify_backend_recovery(paths, lifecycle, policy, expected["backend_sha256"])
            return
        reservation = record.get("dev_baseline_backup")
        if reservation is not None:
            if (not _valid_managed_backup(reservation, complete=False)
                    or any(reservation.get(key) != value for key, value in expected.items())):
                raise DeploymentIncomplete("A pending SQLite recovery backup belongs to a different Stable baseline.")
            if actual != expected["backend_sha256"]:
                raise DeploymentIncomplete("The backend changed before the reserved SQLite baseline was verified.")
        elif actual == expected["backend_sha256"]:
            reservation = {"id": uuid.uuid4().hex, **expected}
            record["dev_baseline_backup"] = reservation
            _write_identity(paths, record)
        else:
            raise DeploymentIncomplete("The accepted Stable baseline has no verified SQLite recovery backup. Supply an approved backup for that baseline before changing the backend.")
        prepared = _prepare_managed_backup(paths, lifecycle, reservation)
        _validate_managed_backup(paths, lifecycle, prepared)
        record["dev_baseline_backup"] = prepared
        _write_identity(paths, record)
        result["backup"] = {"status": "CREATED", "detail": "verified SQLite backup for the accepted Stable baseline"}


def _cleanup_dev_baseline_backup(paths: Paths, lifecycle, result: dict) -> None:
    storage = result.setdefault("storage", {})
    try:
        with _configuration_component_lock(paths):
            record = _read_identity(paths)
            backup = record.get("dev_baseline_backup")
            if backup is None:
                return
            stable = record.get("stable_current")
            operation = record.get("operation") or {}
            accepted = _accepted_stable_pin(record.get("last_verified_install"))
            if (record.get("stable_admission") or record.get("dev_admission") or record.get("rollback_admission")
                    or operation.get("status") != "VERIFIED_CONTENT" or not accepted or accepted != stable
                    or stable["source_sha"] != backup["source_sha"]
                    or stable["installed_content"]["backend_sha256"] != backup["backend_sha256"]):
                storage.setdefault("retained", []).append(
                    {"resource": "DEV baseline backup", "reason": "still pinned for Stable recovery"})
                return
            pinned = [record.get(key) for key in ("database_rollback", "database_rollback_retired",
                                                 "database_rollback_candidate")]
            if any(pin and pin["id"] == backup["id"] for pin in pinned):
                return
            if not _valid_managed_backup(backup, complete=True):
                raise DeploymentError("DEV baseline backup reservation has not been verified")
            path = _backup_directory(paths, backup) / backup["filename"]
            recovery = _read_acceptance_policy(paths).get("recovery") or {}
            if recovery.get("backup_path"):
                external = Path(recovery["backup_path"])
                if external.resolve() == path or (external.exists() and path.exists() and external.samefile(path)):
                    storage.setdefault("retained", []).append(
                        {"resource": "DEV baseline backup", "reason": "referenced by the approved recovery policy"})
                    return
            if path.exists():
                _validate_managed_backup(paths, lifecycle, backup)
            reclaimed = _remove_obsolete_managed_backup(paths, backup)
            record.pop("dev_baseline_backup")
            _write_identity(paths, record)
            storage["cleanup_reclaimed_bytes"] = storage.get("cleanup_reclaimed_bytes", 0) + reclaimed
    except (OSError, DeploymentError) as exc:
        storage.setdefault("cleanup_failures", []).append(
            {"resource": "DEV baseline backup", "reason": str(exc) if isinstance(exc, DeploymentError)
             else type(exc).__name__})


def _remove_obsolete_managed_backup(paths: Paths, pin: dict) -> int:
    """Remove a superseded pin only after its owner has verified replacement."""
    if not _valid_managed_backup(pin, complete=True):
        raise DeploymentError("invalid retired rollback backup pin")
    directory = _backup_directory(paths, pin)
    if not os.path.lexists(directory):
        return 0
    _check_trusted_tree(directory, paths.owner_uid)
    info = directory.lstat()
    if info.st_uid != paths.owner_uid or stat.S_IMODE(info.st_mode) != 0o700:
        raise DeploymentError("retired SQLite backup directory is not private")
    path = directory / pin["filename"]
    entries = list(directory.iterdir())
    if any(entry != path for entry in entries):
        raise DeploymentError("retired SQLite backup directory contains unowned resources")
    size = 0
    if entries:
        before = _protected_backup_file(paths, path)
        if _backup_file_digest(path, before) != pin["sha256"]:
            raise DeploymentError("retired SQLite backup content no longer matches its ownership pin")
        path.unlink()
        size = before.st_blocks * 512
    directory.rmdir()
    fd = os.open(directory.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return size


STAGING_ROOT = Path("/tmp")
STAGING_PREFIX = "fwrouter-main-"
STAGING_LEASE = ".fwrouter-deployment-lease"
STAGING_SIGNATURE = b"fwrouter-deployment-staging/v1\n"
STAGING_MAX_AGE = 24 * 60 * 60


def _require_staging_space(root: Path, bytes_needed: int, inodes_needed: int) -> None:
    fs = os.statvfs(root)
    available = fs.f_bavail * fs.f_frsize
    reserve = 16 * 1024 * 1024
    required = bytes_needed + inodes_needed * fs.f_frsize + reserve
    if available < required or (fs.f_files and fs.f_favail < inodes_needed + 128):
        raise DeploymentIncomplete(
            f"Temporary storage requires {required} bytes and {inodes_needed + 128} inodes of safe space.")


@contextmanager
def _managed_staging_workspace(paths: Paths):
    _require_staging_space(STAGING_ROOT, 0, 0)
    with tempfile.TemporaryDirectory(prefix=STAGING_PREFIX, dir=STAGING_ROOT) as directory:
        scratch = Path(directory)
        if scratch.lstat().st_uid != paths.owner_uid:
            raise DeploymentError("temporary Stable staging has unexpected ownership")
        lease = os.open(scratch / STAGING_LEASE, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(lease, fcntl.LOCK_EX)
            with os.fdopen(os.dup(lease), "wb") as stream:
                stream.write(STAGING_SIGNATURE)
            yield scratch
        finally:
            os.close(lease)


def _staging_space_bytes(path: Path) -> int:
    root = path.lstat()
    with Path("/proc/self/mountinfo").open(encoding="utf-8", errors="surrogateescape") as stream:
        for line in stream:
            mount = re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), line.split()[4])
            if Path(mount).is_relative_to(path):
                raise DeploymentError("temporary deployment tree crosses a mount point")
    total = root.st_blocks * 512
    count = 0
    for directory, dirs, files in os.walk(path, followlinks=False):
        for name in dirs + files:
            info = (Path(directory) / name).lstat()
            count += 1
            if (info.st_dev != root.st_dev or info.st_uid != root.st_uid
                    or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)
                            or stat.S_ISLNK(info.st_mode))
                    or count > MAX_SOURCE_ENTRIES * 2):
                raise DeploymentError("temporary deployment tree exceeds its ownership boundary")
            total += info.st_blocks * 512
    return total


def _cleanup_expired_staging(paths: Paths) -> dict:
    result = {"reclaimed_bytes": 0, "failures": []}
    cutoff = time.time() - STAGING_MAX_AGE
    for path in sorted(STAGING_ROOT.glob(f"{STAGING_PREFIX}*")):
        if re.fullmatch(r"fwrouter-main-[0-9a-z_]{8}", path.name) is None:
            continue
        lease_fd = None
        try:
            before = path.lstat()
            if (not stat.S_ISDIR(before.st_mode) or stat.S_IMODE(before.st_mode) != 0o700
                    or before.st_uid != paths.owner_uid):
                continue
            lease = path / STAGING_LEASE
            lease_info = lease.lstat()
            if (not stat.S_ISREG(lease_info.st_mode) or stat.S_IMODE(lease_info.st_mode) != 0o600
                    or lease_info.st_uid != paths.owner_uid or lease_info.st_nlink != 1
                    or lease_info.st_mtime >= cutoff or lease_info.st_size != len(STAGING_SIGNATURE)):
                continue
            lease_fd = os.open(lease, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                fcntl.flock(lease_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                continue
            opened = os.fstat(lease_fd)
            if ((opened.st_dev, opened.st_ino) != (lease_info.st_dev, lease_info.st_ino)
                    or os.read(lease_fd, len(STAGING_SIGNATURE) + 1) != STAGING_SIGNATURE):
                continue
            size = _staging_space_bytes(path)
            after = path.lstat()
            if (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino) or not shutil.rmtree.avoids_symlink_attacks:
                raise DeploymentError("temporary staging cannot be deleted with its ownership fence")
            # Keep the stale lease available to the next invocation until
            # all payload paths have been deleted. An interruption can leave
            # an empty directory after unlinking the lease, but no data.
            for child in path.iterdir():
                if child == lease:
                    continue
                if stat.S_ISDIR(child.lstat().st_mode):
                    shutil.rmtree(child)
                else:
                    child.unlink()
            lease.unlink()
            path.rmdir()
            result["reclaimed_bytes"] += size
        except FileNotFoundError:
            # Legacy/unowned directories have no lease. A concurrent removal
            # can also make this entry disappear before it is inspected.
            continue
        except (OSError, DeploymentError) as exc:
            result["failures"].append({"digest": path.name, "reason": str(exc) if isinstance(exc, DeploymentError)
                                       else type(exc).__name__})
        finally:
            if lease_fd is not None:
                os.close(lease_fd)
    return result


NATIVE_CHECKS = (
    "backend_manifest_sha256", "sqlite_schema_integrity_foreign_keys", "generation_owner_state",
    "checkpoint_state", "xray_identity_bindings_config_runtime", "mihomo_config_rules_selectors_runtime",
    "core_manifest_nft_ip_routing_runtime", "final_native_runtime_fence_recheck",
    "generation_native_receipt_exact_match",
)


def _native_acceptance(paths: Paths, lifecycle, manifest: str, policy: dict) -> dict:
    if paths.test_driver:
        return {"status": "NOT_VERIFIED", "reason": "native proof is unavailable for a disposable target"}
    try:
        image_status, image_detail, policy = _runtime_image_evidence(paths, policy)
        if image_status != "PASS":
            return {"status": "FAIL" if image_status == "FAIL" else "NOT_VERIFIED", "reason": image_detail}
        _stable_service_state(paths, lifecycle)
        python = lifecycle._verified_backend_interpreter()
        result = _run([str(python), "-I", "-B", "-m", "fwrouter_api.cli", "native-proof", "--scope", "ui",
                       "--expected-backend-manifest-sha256", manifest,
                       "--expected-xray-image-digest", policy["xray_image"],
                       "--expected-mihomo-image-digest", policy["mihomo_image"]], timeout=180, host_environment=True)
    except DeploymentError as exc:
        return {"status": "NOT_VERIFIED", "reason": str(exc)}
    except (OSError, RuntimeError) as exc:
        return {"status": "NOT_VERIFIED", "reason": f"native proof runner unavailable ({type(exc).__name__})"}
    proof = _json_object(result.stdout)
    source = {"kind": "backend_manifest_sha256", "sha256": manifest, "commit_sha": None}
    if (result.returncode == 0 and proof.get("status") == "PASS" and proof.get("scope") == "ui"
            and type(proof.get("proof_schema_version")) is int and proof["proof_schema_version"] == 1
            and proof.get("source_identity") == source and proof.get("checks_executed") == list(NATIVE_CHECKS)
            and _hex(proof.get("native_proof_sha256"), 64)
            and all(isinstance(proof.get(name), dict)
                    and proof[name].get("runtime_image_digest") == policy[f"{name}_image"]
                    for name in ("xray", "mihomo"))):
        return {"status": "PASS", "reason": None, "proof_sha256": proof["native_proof_sha256"], "scope": "ui"}
    reason = proof.get("reason")
    if not isinstance(reason, str) or len(reason) > 100 or not reason.replace("_", "").isalnum():
        reason = "native_proof_result_unavailable_or_invalid"
    return {"status": "FAIL" if proof.get("status") == "FAIL" else "NOT_VERIFIED", "reason": reason}


def _ui_probe_policy(paths: Paths, policy: dict) -> dict:
    if "ui_port" in policy or "ui_path" in policy:
        return policy
    # Recognize the documented single-server FWRouter Nginx layout. The
    # loopback response still has to match the exact installed index bytes.
    try:
        data = _read_installed_runtime_config(
            paths, "etc/nginx/conf.d/fwrouter-ui.conf", limit=64 * 1024).decode("utf-8")
    except (OSError, UnicodeError, DeploymentError):
        return policy
    data = re.sub(r"(?m)#.*$", "", data)
    listeners = re.findall(r"(?m)^[ 	]*listen[ 	]+([^;]+);", data)
    roots = re.findall(r"(?m)^[ 	]*root[ 	]+([^;]+);", data)
    if (len(re.findall(r"(?m)^[ 	]*server[ 	]*\{", data)) != 1
            or roots != ["/opt/fwrouter-ui"] or not listeners):
        return policy
    allowed = re.compile(r"(?:127\.0\.0\.1:|\[::\]:)?5500(?:[ 	]+default_server)?")
    if not all(allowed.fullmatch(value.strip()) for value in listeners):
        return policy
    return {**policy, "ui_port": 5500, "ui_path": "/"}


def _http_ui(paths: Paths, policy: dict) -> bool:
    if (not isinstance(policy, dict) or not _positive_id(policy.get("ui_port"))
            or not isinstance(policy.get("ui_path"), str)):
        raise DeploymentIncomplete("UI endpoint is not configured by a trusted deployment policy")
    expected = paths.target("opt/fwrouter-ui/index.html")
    connection = http.client.HTTPConnection("127.0.0.1", policy["ui_port"], timeout=5)
    try:
        connection.request("GET", policy["ui_path"], headers={"Accept-Encoding": "identity"})
        response = connection.getresponse()
        if response.status != 200:
            return False
        raw = response.read(1024 * 1024 + 1)
        return len(raw) <= 1024 * 1024 and hashlib.sha256(raw).hexdigest() == _digest(expected)
    finally:
        connection.close()


def _dns_service_ready() -> bool:
    result = _run(["/usr/bin/dig", "+time=2", "+tries=1", "+short", "@127.0.0.1", "localhost", "A"], timeout=5)
    return result.returncode == 0 and "127.0.0.1" in result.stdout.splitlines()


def _read_health(paths: Paths, lifecycle, policy: dict) -> dict:
    def probe(action, detail: str) -> dict:
        try:
            return {"status": "PASS" if action() else "FAIL", "detail": detail}
        except (OSError, ValueError, RuntimeError, http.client.HTTPException):
            return {"status": "INCOMPLETE", "detail": f"{detail} unavailable"}

    if paths.test_driver:
        return {"API": probe(lambda: paths.api_healthy, "disposable fixture"),
                "Runtime": probe(lambda: paths.runtime_healthy, "disposable fixture")}
    policy = _ui_probe_policy(paths, policy)
    ui = (probe(lambda: _http_ui(paths, policy), "loopback HTTP matches installed index.html")
          if isinstance(policy, dict) and _positive_id(policy.get("ui_port")) and isinstance(policy.get("ui_path"), str)
          else {"status": "INCOMPLETE", "detail": "loopback UI availability probe is not configured"})
    return {"API": probe(lifecycle._api_health, "local API and database readiness"),
            "UI": ui,
            "Xray": probe(_xray_unit_active, "systemd unit state"),
            "Mihomo": probe(lambda: lifecycle._is_active(lifecycle.MIHOMO_UNIT), "systemd unit state"),
            "DNS": probe(_dns_service_ready, "local DNS localhost A response"),
            "Gateway": probe(lambda: lifecycle._is_active(lifecycle.GATEWAY_UNIT)
                             and lifecycle._gateway_listener_ready(), "systemd unit and local listener")}


def _report_lifecycle(result: dict, paths: Paths, lifecycle) -> None:
    state_file = paths.state_dir / "pending.json"
    checkpoint = lifecycle._read_checkpoint(state_file) if state_file.exists() else None
    result["pending_units"] = checkpoint.get("pending_units", []) if checkpoint else []
    complete = not checkpoint or checkpoint["status"] == "complete"
    _stage(result, "Lifecycle", "PASS" if complete else "INCOMPLETE",
           "no pending operations" if complete else "owner operation still pending")
    if paths.test_driver:
        return
    timers = {}
    for unit in lifecycle.PERSISTENT_TIMERS:
        try:
            command = _run(["/usr/bin/systemctl", "show", "--property=ActiveState,UnitFileState", unit],
                           timeout=10, host_environment=True)
            properties = dict(line.split("=", 1) for line in command.stdout.splitlines() if "=" in line)
            active, enabled = properties.get("ActiveState"), properties.get("UnitFileState")
            if command.returncode or active not in {"active", "inactive", "failed"} or not enabled:
                raise DeploymentError("systemd timer state is unavailable")
        except (DeploymentError, OSError):
            active, enabled = "unknown", "unknown"
        timers[unit] = {"active": active, "enabled": enabled}
    result["timers"] = timers
    unknown = any(state["active"] == "unknown" for state in timers.values())
    failed = any(state["active"] == "failed" for state in timers.values())
    _stage(result, "Timers", "FAIL" if failed else "INCOMPLETE" if unknown or result["pending_units"] else "PASS",
           ", ".join(f"{name.removeprefix('fwrouter-').removesuffix('.timer')} {state['active']}/{state['enabled']}"
                     for name, state in timers.items()))


def _accepted_stable_pin(last: object) -> dict | None:
    if (not isinstance(last, dict) or last.get("mode") != "STABLE"
            or last.get("acceptance", {}).get("status") != "VERIFIED"
            or last.get("native_proof", {}).get("status") != "PASS"
            or not _valid_qualification(last.get("qualification"))
            or last["qualification"]["source_sha"] != last.get("git_sha")
            or last.get("source_content") != last.get("installed_content")):
        return None
    proof = last["native_proof"].get("evidence_refs", [])
    if len(proof) != 1 or not _hex(proof[0], 64):
        return None
    return {"source_sha": last["git_sha"], "qualification": last["qualification"],
            "installed_content": last["installed_content"],
            "accepted_at": last["acceptance"]["verified_at"], "proof_sha256": proof[0]}


def _discover_stable_baseline(paths: Paths, selection: StableSource, identity: dict,
                              lifecycle, ui) -> dict:
    """Verify historical rollback resources without asserting prior runtime acceptance."""
    last = identity.get("last_verified_install") or {}
    sha = last.get("git_sha")
    if (last.get("mode") != "UNKNOWN" or not _hex(sha, 40)
            or last.get("installed_content") != last.get("source_content")):
        raise DeploymentIncomplete("No accepted Stable baseline or exact legacy install identity is available.")
    installed = _installed_content_identity(selection.root, paths, lifecycle, ui)
    if installed != last.get("installed_content"):
        raise DeploymentIncomplete("Installed components differ from the recorded legacy recovery candidate.")
    ancestry = _run(["/usr/bin/git", "-C", str(selection.root), "merge-base", "--is-ancestor",
                     sha, selection.commit], timeout=30)
    if ancestry.returncode:
        raise DeploymentIncomplete("The legacy recovery candidate is outside the qualified Main history.")
    with _managed_staging_workspace(paths) as scratch:
        source = scratch / "baseline"
        clone = _run(["/usr/bin/git", "clone", "--quiet", "--no-hardlinks", "--no-checkout",
                      str(selection.root), str(source)], timeout=180)
        if clone.returncode:
            raise DeploymentIncomplete("Could not reconstruct the exact legacy recovery source locally.")
        checkout = _run(["/usr/bin/git", "-C", str(source), "checkout", "--quiet", "--detach", sha], timeout=90)
        if checkout.returncode:
            raise DeploymentIncomplete("Legacy recovery source cannot be checked out by its recorded identity.")
        qualification = _stable_qualification(source, sha, scratch, paths.owner_uid,
                                               historical_head=selection.commit)
        if _source_content_identity(source, lifecycle, ui, paths) != installed:
            raise DeploymentIncomplete("Qualified historical source does not reproduce the installed components.")
        try:
            _strict_database_preflight(replace(paths, stable_source=selection), lifecycle, source, legacy_rollback=True)
        except DeploymentError as exc:
            raise DeploymentIncomplete("Historical SQLite rollback compatibility could not be verified.") from exc
    return {"source_sha": sha, "qualification": qualification, "installed_content": installed,
            "provenance": "UNKNOWN", "native_acceptance": "NOT_VERIFIED",
            "lifecycle_owner_sha": selection.commit}


def _pin_legacy_lifecycle_owner(paths: Paths, record: dict, selection: StableSource) -> None:
    updater = record.get("components", {}).get("updater") or {}
    updater_sha = _digest(paths.target("usr/local/libexec/fwrouter/local_deploy.py"))
    if (updater.get("qualification_status") != "VERIFIED_MAIN_CI"
            or updater.get("installed_sha256") != updater_sha):
        raise DeploymentIncomplete("A qualified installed D2 updater is required to pin legacy rollback resources.")
    owner = _publish_lifecycle_owner_capsule(paths, selection.root, selection.commit,
                                            selection.qualification, updater_sha)
    record.setdefault("lifecycle_owners", {})[selection.commit] = owner


def _adopt_legacy_dev_baseline(paths: Paths, source: Path, identity: dict, result: dict) -> None:
    updater = identity.get("components", {}).get("updater") or {}
    # Reuse the completed module-only bootstrap's exact Main evidence when still current.
    qualification = {key: updater.get(key) for key in
                     ("source_sha", "run_id", "run_attempt", "artifact_id", "artifact_digest")}
    for key in ("run_id", "run_attempt", "artifact_id"):
        if isinstance(qualification[key], str) and qualification[key].isascii() and qualification[key].isdecimal():
            qualification[key] = int(qualification[key])
    reuse = (_valid_qualification(qualification)
             and updater.get("qualification_status") == "VERIFIED_MAIN_CI"
             and updater.get("installed_sha256") == _digest(paths.target("usr/local/libexec/fwrouter/local_deploy.py"))
             and qualification["source_sha"] == _current_main())
    with _temporary_main(paths, qualification if reuse else None) as selection:
        lifecycle, _, ui = _source_modules(source, paths, engine_source=selection.root)
        _require_development_lifecycle_contract(selection.root, lifecycle)
        _strict_database_preflight(paths, lifecycle, source)
        _verify_dependency_compatibility(paths, source)
        facts = _facts(source, paths, lifecycle, ui)
        changes = [name for name, same in zip(("Backend", "UI", "Host files"), facts[:3]) if not same]
        _storage_preflight(paths, source, changes, result)
        baseline = _discover_stable_baseline(paths, selection, identity, lifecycle, ui)
        with _configuration_component_lock(paths):
            record = _read_identity(paths)
            if (record != identity
                    or _installed_content_identity(source, paths, lifecycle, ui) != baseline["installed_content"]):
                raise DeploymentIncomplete("Installed recovery identity changed before legacy adoption.")
            lifecycle._validate_state_dir(paths.state_dir)
            _pin_legacy_lifecycle_owner(paths, record, selection)
            record["stable_current"] = baseline
            _write_identity(paths, record)
    _stage(result, "Stable recovery", "PASS", "historical source and D2 lifecycle verified; historical acceptance NOT_VERIFIED")


def _remember_stable_admission(paths: Paths, lifecycle, *, recovering: bool, rollback: bool = False,
                               baseline: dict | None = None, ui=None) -> None:
    selection = paths.stable_source
    with _configuration_component_lock(paths):
        record = _identity_record_base(paths)
        dev_admission = record.get("dev_admission")
        operation = record.get("operation") or {}
        last = record.get("last_verified_install") or {}
        safe_dev_rollback = (rollback and isinstance(dev_admission, dict)
                             and operation.get("status") == "VERIFIED_CONTENT"
                             and last.get("mode") == "DEV"
                             and last.get("git_sha") == dev_admission.get("git_sha")
                             and selection.commit == (record.get("stable_current") or {}).get("source_sha"))
        if dev_admission is not None and not safe_dev_rollback:
            raise DeploymentIncomplete("A Development recovery admission is active; finish it before Stable admission.")
        if safe_dev_rollback:
            record.pop("dev_admission", None)
        current = record.get("stable_admission")
        if recovering:
            if current != selection.qualification:
                raise DeploymentError("Stable recovery admission changed before deployment")
            return
        if current is not None:
            raise DeploymentError("another Stable operation was admitted; rerun --stable to resume it")
        accepted = record.get("stable_current") or _accepted_stable_pin(record.get("last_verified_install"))
        if baseline is not None:
            if (accepted is not None or baseline["source_sha"] != last.get("git_sha")
                    or _installed_content_identity(selection.root, paths, lifecycle, ui)
                    != baseline["installed_content"]):
                raise DeploymentIncomplete("The legacy recovery source changed before admission.")
            if not paths.test_driver:
                _pin_legacy_lifecycle_owner(paths, record, selection)
            accepted = baseline
        if rollback:
            last = record.get("last_verified_install") or {}
            target = (record.get("stable_current") if last.get("mode") == "DEV"
                      else record.get("stable_previous"))
            if accepted is None or target is None:
                raise DeploymentIncomplete("No accepted Stable version is available for rollback.")
            if target["qualification"]["source_sha"] != selection.commit:
                raise DeploymentError("the selected rollback source differs from its durable accepted pin")
            record["rollback_admission"] = target["qualification"]
        elif not rollback and _current_main() != selection.commit:
            raise DeploymentError("origin/main advanced before deployment admission; rerun --stable")
        if not paths.test_driver:
            lifecycle._validate_state_dir(paths.state_dir)
        else:
            paths.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        record["stable_admission"] = selection.qualification
        if accepted is not None and not record.get("stable_current"):
            record["stable_current"] = accepted
        _write_identity(paths, record)


def _pin_retired_ui_release(paths: Paths, ui, *, expected_admission: dict,
                            development: bool = False) -> None:
    target = paths.target("opt/fwrouter-ui")
    releases = paths.target("opt/.fwrouter-ui-releases")
    with _configuration_component_lock(paths):
        active = ui._managed_release(target, releases, paths.owner_uid)
        if active is None:
            return
        digest, _ = ui._walk_tree(active, owner_uid=paths.owner_uid, require_index=True)
        info = active.lstat()
        key = str(digest)
        record = _identity_record_base(paths)
        admission = "dev_admission" if development else "stable_admission"
        if record.get(admission) != expected_admission:
            raise DeploymentError("Deployment admission changed before UI retirement was pinned")
        known_stable = any((record.get(name) or {}).get("installed_content", {}).get("ui_sha256") == key
                           for name in ("stable_current", "stable_previous"))
        last = record.get("last_verified_install") or {}
        known_dev = (last.get("mode") == "DEV" and last.get("installed_content", {}).get("ui_sha256") == key)
        if not (known_stable or known_dev):
            return
        retired = record.setdefault("retired_ui_releases", {})
        prior = retired.get(key)
        pin = [info.st_dev, info.st_ino]
        if prior is not None and prior != pin:
            raise DeploymentError("UI retirement pin conflicts with the existing release inode")
        if prior != pin:
            retired[key] = pin
            _write_identity(paths, record)


def _cleanup_retired_ui(paths: Paths, ui, result: dict) -> None:
    with _configuration_component_lock(paths):
        record = _identity_record_base(paths)
        operation = record.get("operation") or {}
        checkpoint = paths.state_dir / "pending.json"
        checkpoint_pending = False
        if checkpoint.exists():
            try:
                checkpoint_pending = json.loads(checkpoint.read_text(encoding="utf-8")).get("status") == "pending"
            except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
                checkpoint_pending = True
        if (record.get("stable_admission") or record.get("rollback_admission") or record.get("dev_admission")
                or operation.get("status") in {"PENDING", "FAILED"}
                or checkpoint_pending):
            result.setdefault("storage", {}).setdefault("retained", []).append(
                {"digest": "UI releases", "reason": "active deployment or lifecycle recovery reference"})
            return
        retired = record.get("retired_ui_releases", {})
        if not retired:
            return
        prune = getattr(ui, "prune_retired_releases", None)
        if not callable(prune):
            result.setdefault("storage", {}).setdefault("retained", []).append(
                {"resource": "UI releases", "reason": "selected accepted source predates the UI cleanup owner"})
            return
        protected = set()
        for name in ("stable_current", "stable_previous"):
            pin = record.get(name)
            if pin:
                protected.add(pin["installed_content"]["ui_sha256"])
        try:
            outcome = prune(paths.target("opt/fwrouter-ui"),
                retired={key: tuple(value) for key, value in retired.items()},
                protected=protected, owner_uid=paths.owner_uid)
        except (OSError, RuntimeError, ValueError) as exc:
            result.setdefault("storage", {}).setdefault("cleanup_failures", []).append(
                {"resource": "UI releases", "reason": str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__})
            return
        for key in outcome["removed"]:
            retired.pop(key, None)
        if outcome["removed"]:
            _write_identity(paths, record)
    storage = result.setdefault("storage", {})
    storage["cleanup_reclaimed_bytes"] = storage.get("cleanup_reclaimed_bytes", 0) + outcome["reclaimed_bytes"]
    storage.setdefault("retained", []).extend(
        {"digest": key, "reason": reason} for key, reason in outcome["retained"].items())
    storage.setdefault("cleanup_failures", []).extend(
        {"digest": key, "reason": reason} for key, reason in outcome["failed"].items())


def _refresh_storage_summary(paths: Paths, result: dict, lifecycle=None) -> None:
    identity = _read_identity(paths)
    storage = result.setdefault("storage", {})
    storage["stable_current"] = (identity.get("stable_current") or {}).get("source_sha")
    storage["stable_previous"] = (identity.get("stable_previous") or {}).get("source_sha")
    storage["dev_current"] = identity.get("dev_current")
    last = identity.get("last_verified_install") or {}
    storage["stable_rollback"] = (storage["stable_current"] if last.get("mode") == "DEV"
                                  else storage["stable_previous"])
    storage.setdefault("cleanup_reclaimed_bytes", 0)
    storage.setdefault("retained", [])
    try:
        managed = _tree_bytes(paths.target("opt/fwrouter-api")) + _tree_bytes(
            paths.target("opt/.fwrouter-ui-releases"))
    except (OSError, DeploymentError):
        managed = 0
        storage["managed_bytes_status"] = "NOT_VERIFIED"
        result.setdefault("warnings", []).append("Release storage usage could not be measured safely.")
    owner_bytes = 0
    for sha, pin in (identity.get("lifecycle_owners") or {}).items():
        try:
            owner_dir = _verify_lifecycle_owner_files(paths, sha, pin)
            owner_bytes += _tree_bytes(owner_dir)
        except (OSError, DeploymentError):
            storage["managed_bytes_status"] = "NOT_VERIFIED"
            result.setdefault("warnings", []).append("Pinned lifecycle owner storage could not be verified.")
    storage["lifecycle_owner_bytes"] = owner_bytes
    managed += owner_bytes
    backup = identity.get("database_rollback")
    if backup and _valid_managed_backup(backup, complete=True):
        try:
            managed += (_backup_directory(paths, backup) / backup["filename"]).stat().st_blocks * 512
        except OSError:
            result.setdefault("warnings", []).append("Pinned rollback backup size could not be measured safely.")
    retired_backup = identity.get("database_rollback_retired")
    if retired_backup and _valid_managed_backup(retired_backup, complete=True):
        try:
            managed += (_backup_directory(paths, retired_backup) / retired_backup["filename"]).stat().st_blocks * 512
        except OSError:
            result.setdefault("warnings", []).append("Retained rollback backup size could not be measured safely.")
    baseline_backup = identity.get("dev_baseline_backup")
    if baseline_backup is not None:
        result["dev_baseline_database_backup"] = "INCOMPLETE"
    if _valid_managed_backup(baseline_backup, complete=True):
        result["dev_baseline_database_backup"] = "NOT_VERIFIED"
        if lifecycle is not None and not paths.test_driver:
            try:
                _validate_managed_backup(paths, lifecycle, baseline_backup)
                result["dev_baseline_database_backup"] = "VERIFIED"
            except (OSError, DeploymentError):
                result["dev_baseline_database_backup"] = "INCOMPLETE"
                result.setdefault("warnings", []).append("Pinned DEV baseline database backup failed verification.")
        try:
            size = (_backup_directory(paths, baseline_backup) / baseline_backup["filename"]).stat().st_blocks * 512
            storage["dev_baseline_backup_bytes"] = size
            managed += size
        except OSError:
            storage["managed_bytes_status"] = "NOT_VERIFIED"
            result.setdefault("warnings", []).append("DEV baseline backup size could not be measured safely.")
    capsules = {item["snapshot_path"] for key in ("dev_current", "dev_admission", "dev_retired")
                if (item := identity.get(key)) is not None}
    capsule_bytes = 0
    for anchor in capsules:
        capsule = Path(anchor)
        try:
            info = capsule.lstat()
            if (capsule.parent != _dev_source_directory(paths) or not stat.S_ISREG(info.st_mode)
                    or info.st_uid != paths.owner_uid or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_nlink != 1):
                raise DeploymentError("snapshot ownership is unknown")
            capsule_bytes += info.st_blocks * 512
        except (DeploymentError, OSError):
            storage["managed_bytes_status"] = "NOT_VERIFIED"
            result.setdefault("warnings", []).append("Pinned DEV snapshot size could not be measured safely.")
    storage["dev_snapshot_bytes"] = capsule_bytes
    storage["managed_bytes"] = managed + capsule_bytes
    if backup:
        if lifecycle is None:
            result["rollback_database_backup"] = "NOT_VERIFIED"
        else:
            try:
                _validate_managed_backup(paths, lifecycle, backup)
                result["rollback_database_backup"] = "VERIFIED"
            except (DeploymentError, OSError):
                result["rollback_database_backup"] = "INCOMPLETE"
                result.setdefault("warnings", []).append("Pinned rollback database backup failed verification.")
    else:
        result["rollback_database_backup"] = "NOT_REQUIRED"


def _cleanup_retired_backup(paths: Paths, lifecycle, result: dict) -> None:
    with _configuration_component_lock(paths):
        record = _identity_record_base(paths)
        retired = record.get("database_rollback_retired")
        operation = record.get("operation") or {}
        if (not retired or record.get("stable_admission") or record.get("rollback_admission")
                or operation.get("status") in {"PENDING", "FAILED"}):
            return
        if retired == record.get("database_rollback"):
            result.setdefault("storage", {}).setdefault("retained", []).append(
                {"resource": "managed SQLite backup", "reason": "still referenced by rollback identity"})
            return
        try:
            checkpoint = paths.state_dir / "pending.json"
            if checkpoint.exists() and lifecycle._read_checkpoint(checkpoint)["status"] != "complete":
                result.setdefault("storage", {}).setdefault("retained", []).append(
                    {"resource": "managed SQLite backup", "reason": "lifecycle recovery is pending"})
                return
            policy_pin = _read_acceptance_policy(paths).get("recovery")
            external = policy_pin.get("backup_path") if isinstance(policy_pin, dict) else None
            retired_path = _backup_directory(paths, retired) / retired["filename"]
            if isinstance(external, str):
                external_path = Path(external)
                if (external_path.resolve() == retired_path
                        or (external_path.exists() and retired_path.exists()
                            and external_path.samefile(retired_path))):
                    result.setdefault("storage", {}).setdefault("retained", []).append(
                        {"resource": "managed SQLite backup", "reason": "referenced by the configured recovery backup pin"})
                    return
            replacement = record.get("database_rollback")
            _validate_managed_backup(paths, lifecycle, replacement)
            reclaimed = _remove_obsolete_managed_backup(paths, retired)
            record.pop("database_rollback_retired", None)
            _write_identity(paths, record)
            storage = result.setdefault("storage", {})
            storage["cleanup_reclaimed_bytes"] = storage.get("cleanup_reclaimed_bytes", 0) + reclaimed
        except (OSError, RuntimeError, ValueError) as exc:
            result.setdefault("storage", {}).setdefault("cleanup_failures", []).append(
                {"resource": "managed SQLite backup", "reason": str(exc) if isinstance(exc, DeploymentError)
                 else type(exc).__name__})


def _managed_rollback_backup(paths: Paths, lifecycle, old_digest: str,
                            expected_admission: dict, result: dict) -> dict:
    with _configuration_component_lock(paths):
        identity = _identity_record_base(paths)
        if identity.get("stable_admission") != expected_admission:
            raise DeploymentError("Stable admission changed before database rollback reservation")
        current = identity.get("stable_current")
        if (not current or not _hex(current.get("source_sha"), 40)
                or current.get("installed_content", {}).get("backend_sha256") != old_digest):
            raise DeploymentIncomplete("A managed rollback backup requires an exact accepted current Stable identity for the installed backend.")
        existing = identity.get("database_rollback")
        candidate = identity.get("database_rollback_candidate")
        expected = {"source_sha": current["source_sha"], "backend_sha256": old_digest, "schema": 27}
        if existing and all(existing.get(key) == value for key, value in expected.items()):
            _validate_managed_backup(paths, lifecycle, existing)
            result["backup"] = {"status": "REUSED", "detail": "exact accepted Stable schema-27 rollback snapshot verified"}
            return existing
        if identity.get("database_rollback_retired"):
            raise DeploymentIncomplete("Resolve the retained superseded SQLite backup before creating another rollback snapshot.")
        if candidate:
            if any(candidate.get(key) != value for key, value in expected.items()):
                raise DeploymentIncomplete("An unfinished database backup reservation belongs to a different Stable generation.")
            reservation = {key: candidate[key] for key in ("id", "source_sha", "backend_sha256", "schema")}
        else:
            reservation = {"id": uuid.uuid4().hex, **expected}
            identity["database_rollback_candidate"] = reservation
            _write_identity(paths, identity)
        prepared = (_prepare_managed_backup(paths, lifecycle, reservation)
                    if not _valid_managed_backup(candidate, complete=True) else candidate)
        _validate_managed_backup(paths, lifecycle, prepared)
        if candidate != prepared:
            identity["database_rollback_candidate"] = prepared
            _write_identity(paths, identity)
        result["backup"] = {"status": "CREATED", "detail": "verified schema-27 SQLite rollback snapshot for the accepted Stable generation"}
        return prepared


def _installed_version(result: dict, identity: dict) -> None:
    last = identity.get("last_verified_install") or {}
    result["installed_version"] = last.get("git_sha")
    result["deployment_identity"] = identity


def _record_stable_acceptance(paths: Paths, result: dict, commit: str, proof: dict, lifecycle=None,
                              owner_source: Path | None = None) -> None:
    last = result["deployment_identity"].get("last_verified_install")
    if not last or last.get("mode") != "STABLE" or last.get("git_sha") != commit:
        raise DeploymentError("content identity changed before acceptance publication")
    with _configuration_component_lock(paths):
        record = _read_identity(paths)
        current = record.get("operation") or {}
        rollback = bool(record.get("rollback_admission"))
        if (record.get("last_verified_install") != last or (record.get("stable_admission") and not rollback)
                or current.get("operation_id") != last["operation_id"]):
            raise DeploymentError("deployment operation changed before acceptance publication")
        acceptance = {"status": "VERIFIED", "verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        native = {"status": "PASS", "scope": proof["scope"], "evidence_refs": [proof["proof_sha256"]]}
        for row in (current, record["last_verified_install"]):
            row.update(native_proof=native, acceptance=acceptance)
        old_accepted = record.get("stable_current")
        new_accepted = _accepted_stable_pin(record["last_verified_install"])
        if new_accepted is None:
            raise DeploymentError("accepted deployment identity could not be safely pinned")
        if old_accepted is not None and old_accepted["source_sha"] != new_accepted["source_sha"]:
            record["stable_previous"] = old_accepted
        record["stable_current"] = new_accepted
        if not paths.test_driver:
            if owner_source is None:
                raise DeploymentIncomplete("Qualified Stable source is unavailable to pin the DEV lifecycle owner.")
            updater_path = paths.target("usr/local/libexec/fwrouter/local_deploy.py")
            updater_digest = _digest(updater_path)
            owners = record.setdefault("lifecycle_owners", {})
            if _has_d2_lifecycle_contract(owner_source):
                updater_record = record.get("components", {}).get("updater") or {}
                if (updater_record.get("qualification_status") != "VERIFIED_MAIN_CI"
                        or updater_record.get("installed_sha256") != updater_digest):
                    raise DeploymentIncomplete(
                        "Installed deployment module lacks its verified updater identity; cannot pin a Stable lifecycle owner.")
                owner_pin = _publish_lifecycle_owner_capsule(
                    paths, owner_source, commit, new_accepted["qualification"], updater_digest)
                owners[commit] = owner_pin
            elif rollback and owners:
                previous_pin = next((row for row in (record.get("stable_previous"), old_accepted)
                                     if row and row.get("source_sha") == commit and row.get("lifecycle_owner_sha")), None)
                if previous_pin:
                    new_accepted["lifecycle_owner_sha"] = previous_pin["lifecycle_owner_sha"]
                result.setdefault("warnings", []).append(
                    "Rollback source predates the D2 lifecycle owner; the retained qualified D2 Stable capsule remains the DEV recovery owner.")
            else:
                raise DeploymentIncomplete("Accepted Stable source has no D2 lifecycle owner and no retained qualified owner capsule exists.")
            if len(owners) > 4:
                raise DeploymentError("lifecycle owner retention exceeds the two accepted Stable releases")
            record["lifecycle_owners"] = owners
        candidate_backup = record.get("database_rollback_candidate")
        dev_backup = record.get("dev_baseline_backup")
        promote_dev_backup = (
            candidate_backup is None and old_accepted is not None
            and old_accepted["source_sha"] != new_accepted["source_sha"]
            and _valid_managed_backup(dev_backup, complete=True)
            and dev_backup["source_sha"] == old_accepted["source_sha"]
            and dev_backup["backend_sha256"] == old_accepted["installed_content"]["backend_sha256"]
        )
        if promote_dev_backup:
            candidate_backup = dev_backup
        if candidate_backup and _valid_managed_backup(candidate_backup, complete=True):
            if (old_accepted is None or candidate_backup["source_sha"] != old_accepted["source_sha"]
                    or candidate_backup["backend_sha256"] != old_accepted["installed_content"]["backend_sha256"]):
                raise DeploymentError("database rollback backup does not match the accepted Stable version being retained")
            if lifecycle is None:
                configured, _configured_sha = _read_config(paths.config, paths.owner_uid)
                lifecycle, _lock, _ui = _source_modules(configured, paths)
            _validate_managed_backup(paths, lifecycle, candidate_backup)
            prior_backup = record.get("database_rollback")
            if prior_backup and prior_backup != candidate_backup:
                if record.get("database_rollback_retired"):
                    raise DeploymentIncomplete("The existing retired SQLite backup still needs safe cleanup.")
                record["database_rollback_retired"] = prior_backup
            record["database_rollback"] = candidate_backup
            record.pop("database_rollback_candidate", None)
            if promote_dev_backup:
                record.pop("dev_baseline_backup")
        if isinstance(record.get("dev_current"), dict):
            record["dev_retired"] = record["dev_current"]
        record.pop("stable_admission", None)
        record.pop("rollback_admission", None)
        record.pop("dev_current", None)
        _write_identity(paths, record)
    _cleanup_lifecycle_owner_capsules(paths, result)
    record = _read_identity(paths)
    _installed_version(result, record)


def _stable_updater_args(paths: Paths, selection: StableSource, operation: dict) -> argparse.Namespace | None:
    if paths.test_driver:
        return None
    wrapper = "host/sbin/fwrouter-deploy"
    installed_wrapper = paths.target("usr/local/sbin/fwrouter-deploy")
    if _read_stable_source_file(installed_wrapper, paths.owner_uid) != _git_blob_bytes(selection.root, selection.commit, wrapper):
        raise DeploymentError("Installed updater wrapper differs from Main. Use the approved updater bootstrap for wrapper changes.")
    module = paths.target("usr/local/libexec/fwrouter/local_deploy.py")
    old_digest = hashlib.sha256(_read_stable_source_file(module, paths.owner_uid)).hexdigest()
    candidate = _git_blob_bytes(selection.root, selection.commit, "installer/local_deploy.py")
    pending = operation.get("kind") == "self_upgrade" and operation.get("status") == "PENDING"
    if not pending and hashlib.sha256(candidate).hexdigest() == old_digest:
        return None
    evidence = selection.qualification
    return argparse.Namespace(main_sha=selection.commit, confirm_main_sha=selection.commit,
                              run_id=str(evidence["run_id"]), run_attempt=str(evidence["run_attempt"]),
                              artifact_id=str(evidence["artifact_id"]), artifact_digest=evidence["artifact_digest"],
                              expected_installed_module_sha256=operation["old_module_sha256"] if pending else old_digest)


def _stable_deploy(paths: Paths, result: dict, *, owner_driver, system_driver, progress,
                   rollback: bool = False) -> None:
    identity = _read_identity(paths)
    dev_admission = identity.get("dev_admission")
    dev_operation = identity.get("operation") or {}
    dev_last = identity.get("last_verified_install") or {}
    safe_dev_rollback = (rollback and isinstance(dev_admission, dict)
                         and dev_operation.get("status") == "VERIFIED_CONTENT"
                         and dev_last.get("mode") == "DEV"
                         and dev_last.get("git_sha") == dev_admission.get("git_sha")
                         and dev_operation.get("lifecycle_status") == "complete")
    if dev_admission is not None and not safe_dev_rollback:
        raise DeploymentIncomplete("A Development operation is pending; rerun --dev to resume its captured snapshot first.")
    if identity.get("dev_retired") is not None:
        _cleanup_development_snapshots(paths, result)
        if result.get("storage", {}).get("cleanup_failures"):
            raise DeploymentIncomplete("A prior DEV snapshot could not be safely retired; resolve cleanup before Stable deployment.")
        identity = _read_identity(paths)
    _installed_version(result, identity)
    result["previous_version"] = result["installed_version"]
    rollback_recovery = identity.get("rollback_admission")
    if rollback_recovery:
        rollback = True
    recovery = identity.get("stable_admission")
    last = identity.get("last_verified_install") or {}
    rollback_pin = None
    if rollback and not rollback_recovery and recovery is not None:
        raise DeploymentIncomplete("A different Stable operation is already admitted; resume it before rollback.")
    if rollback and recovery is None:
        rollback_pin = (identity.get("stable_current") if last.get("mode") == "DEV"
                        else identity.get("stable_previous"))
        target = rollback_pin
        if not target:
            raise DeploymentIncomplete("No accepted Stable version is available as a rollback target.")
        recovery = target["qualification"]
    elif rollback and rollback_recovery:
        rollback_pin = next((identity.get(name) for name in ("stable_current", "stable_previous")
                             if (identity.get(name) or {}).get("qualification") == rollback_recovery), None)
    operation = identity.get("operation") or {}
    if operation.get("kind") == "self_upgrade" and operation.get("status") == "PENDING" and not recovery:
        raise DeploymentIncomplete("Resume the existing D1 source transition before starting a Stable deployment.")
    progress("Resolving qualified Stable source")
    additional_sha = ((identity.get("stable_current") or {}).get("source_sha") if rollback else None)
    source_context = (_temporary_main(paths, recovery) if additional_sha is None
                      else _temporary_main(paths, recovery, additionally_fetch_sha=additional_sha))
    with source_context as source:
        selected = replace(paths, stable_source=source)
        commit = source.commit
        result["source_commit"] = commit
        result["qualification"] = source.qualification
        _stage(result, "Source", "PASS", f"main @ {commit[:12]}")
        progress(f"Source main @ {commit[:12]}")
        _stage(result, "CI", "PASS", "Release Acceptance verified" + (" before the interrupted operation" if recovery else ""))
        if (operation.get("kind") != "self_upgrade" and operation.get("status") in {"PENDING", "FAILED"}
                and operation.get("requested_source", {}).get("git_sha") != commit):
            raise DeploymentIncomplete("Complete the existing lifecycle operation against its originally selected source.")
        engine_source = source.root
        if rollback and not paths.test_driver and not _has_d2_lifecycle_contract(source.root):
            engine_source = _materialize_pinned_owner(selected, source.root, source.root.parent)
        lifecycle, lock_module, ui = (_source_modules(source.root, selected) if engine_source == source.root
                                      else _source_modules(source.root, selected, engine_source=engine_source))
        if rollback:
            pinned_previous = rollback_pin or identity.get("stable_previous")
            if not pinned_previous or pinned_previous.get("qualification") != source.qualification:
                raise DeploymentIncomplete("Stable rollback source no longer matches the last accepted previous pin.")
            reconstructed = _source_content_identity(source.root, lifecycle, ui, selected)
            if reconstructed != pinned_previous["installed_content"]:
                raise DeploymentIncomplete("The accepted rollback SHA no longer reconstructs the installed component content.")
        status = (check(selected, source.root, commit) if engine_source == source.root
                  else check(selected, source.root, commit, engine_source=engine_source))
        updater = None if rollback else _stable_updater_args(selected, source, operation)
        facts = _facts(source.root, selected, lifecycle, ui)
        policy = _read_acceptance_policy(paths)
        result["components"] = {"unchanged": [], "updated": []}
        if status == "UPDATER_SELF_UPGRADE_PENDING" and updater is None:
            raise DeploymentIncomplete("Resume the existing D1 updater transition with its pinned inputs.")
        changed = [name for name, same in zip(("Backend", "UI", "Host files"), facts[:3]) if not same]
        if "Backend" in changed:
            saved_retirement = operation.get("retire_paths") if operation.get("status") in {"PENDING", "FAILED"} else None
            retirement = saved_retirement if saved_retirement is not None else _prepare_backend_retirements(paths, source.root, lifecycle)
            selected = replace(selected, retire_paths=retirement)
            paths = selected
        timer_update = not paths.test_driver and lifecycle._traffic_timer_update_required(engine_source)
        if timer_update:
            changed.append("Traffic timer")
            if status == "NOOP":
                status = "READY_TO_APPLY"
        if status not in {"NOOP", "UNHEALTHY_CURRENT_SOURCE"} or updater is not None:
            progress("Checking storage and deployment preflight")
            _storage_preflight(selected, source.root, changed, result)
            _stage(result, "Storage", "PASS", "temporary and installation space are sufficient")
        else:
            _stage(result, "Storage", "SKIP", "no publication is required")
        old_digest = None
        if status not in {"NOOP", "UNHEALTHY_CURRENT_SOURCE"} or updater is not None:
            progress("Checking preflight and lifecycle recovery")
            schema_profile = _strict_database_preflight(selected, lifecycle, source.root, legacy_rollback=rollback)
            _stage(result, "Database", "SKIP" if paths.test_driver else "PASS",
                   "disposable target" if paths.test_driver else f"schema 27 verified ({schema_profile})")
            if not facts[0]:
                _verify_dependency_compatibility(paths, source.root)
                if rollback:
                    installed_paths = {
                        item["path"] for item in lifecycle._backend_manifest(paths.target("opt/fwrouter-api"))
                    }
                    rollback_paths = {item["path"] for item in lifecycle._backend_manifest(source.root / "backend")}
                    obsolete = {
                        line for line in (source.root / "installer/obsolete-backend-paths.txt").read_text().splitlines()
                        if line and not line.startswith("#")
                    }
                    if last.get("mode") != "DEV" and installed_paths - rollback_paths - obsolete:
                        raise DeploymentIncomplete(
                            "The rollback installer cannot retire every newer backend file through its supported allowlist.")
                checkpoint = (lifecycle._read_checkpoint(paths.state_dir / "pending.json")
                              if (paths.state_dir / "pending.json").exists() else None)
                new_digest = _source_content_identity(source.root, lifecycle, ui, selected)["backend_sha256"]
                if (checkpoint and checkpoint["status"] == "pending"
                        and checkpoint["backend_manifest_sha256"] == new_digest
                        and checkpoint.get("source_transitions")):
                    old_digest = checkpoint["source_transitions"][-1]["from_manifest_sha256"]
                else:
                    if _installed_source_reconstruction_required(paths):
                        if not _known_installed_backend(paths, lifecycle._manifest_digest(
                                lifecycle._backend_manifest(paths.target("opt/fwrouter-api")))):
                            raise DeploymentIncomplete("Installed DEV backend has no durable recovery identity.")
                    else:
                        _find_source_commit(source.root, paths.target("opt/fwrouter-api"), lifecycle)
                    old_digest = lifecycle._manifest_digest(lifecycle._backend_manifest(paths.target("opt/fwrouter-api")))
            if not paths.test_driver and status != "FINALIZE_PENDING_IDENTITY":
                checkpoint_path = paths.state_dir / "pending.json"
                checkpoint = lifecycle._read_checkpoint(checkpoint_path) if checkpoint_path.exists() else {}
                active = (checkpoint["active_units"] if checkpoint.get("status") == "pending" else
                          {unit: lifecycle._is_active(unit) for unit in lifecycle.PERSISTENT_TIMERS})
                lifecycle._preflight_timer_restore(engine_source, active)
            policy = _require_acceptance_policy(paths, policy)
            _stage(result, "Runtime images", "SKIP" if paths.test_driver else "PASS",
                   "disposable target" if paths.test_driver else "running image identities match local configuration")
            baseline = None
            if (not paths.test_driver and not recovery and not rollback and not identity.get("stable_current")
                    and not _accepted_stable_pin(last) and not all(facts[:3])):
                progress("Checking the installed Stable recovery baseline")
                baseline = _discover_stable_baseline(selected, source, identity, lifecycle, ui)
            _stage(result, "Preflight", "PASS", "schema admission and recoverable lifecycle source")
            _remember_stable_admission(selected, lifecycle, recovering=bool(identity.get("stable_admission")),
                                       rollback=rollback, baseline=baseline, ui=ui)
            if old_digest is not None:
                external_recovery = policy.get("recovery")
                if last.get("mode") == "DEV":
                    _ensure_dev_baseline_backup(selected, lifecycle, policy, result, returning=True)
                elif (paths.test_driver or (isinstance(external_recovery, dict)
                        and external_recovery.get("installed_backend_sha256") == old_digest)):
                    result["backup"] = _verify_backend_recovery(paths, lifecycle, policy, old_digest)
                else:
                    _managed_rollback_backup(paths, lifecycle, old_digest, source.qualification, result)
            if "UI" in changed:
                _pin_retired_ui_release(selected, ui, expected_admission=source.qualification)
        if updater is not None:
            _self_upgrade(selected, source.root, commit, updater)
            _stage(result, "Updater", "PASS", "qualified updater module transition")
            changed.append("Updater")
            status = (check(selected, source.root, commit) if engine_source == source.root
                      else check(selected, source.root, commit, engine_source=engine_source))
            if timer_update and status == "NOOP":
                status = "READY_TO_APPLY"
        deploying = status not in {"NOOP", "UNHEALTHY_CURRENT_SOURCE"}
        publication_error = None
        if deploying:
            progress(f"Applying {commit[:12]} through the deployment lifecycle")
            try:
                outcome = apply(selected, source.root, commit, owner_driver=owner_driver,
                                system_driver=system_driver,
                                **({} if engine_source == source.root else {"engine_source": engine_source}))
                result["install_status"] = outcome["status"]
            except (DeploymentError, RuntimeError, OSError, ValueError) as exc:
                publication_error = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
                result["warnings"].append("Inspect the owner checkpoint and rerun --stable to resume the admitted operation.")
        elif status == "NOOP" and (last.get("mode") == "DEV"
                                  or (rollback and last.get("git_sha") != commit)):
            _remember_stable_admission(selected, lifecycle, recovering=bool(identity.get("stable_admission")),
                                       rollback=rollback)
            operation_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"fwrouter-stable:{commit}"))
            _record_operation(selected, lock_module, source.root, commit, lifecycle, ui, operation_id,
                              "complete", "VERIFIED_CONTENT", [], details={"kind": "stable_deploy"})
        elif identity.get("stable_admission") or updater is not None:
            with _configuration_component_lock(paths):
                record = _identity_record_base(paths)
                if record.get("stable_admission") != source.qualification:
                    raise DeploymentError("Stable admission changed before NOOP finalization")
                record.pop("stable_admission")
                record.pop("rollback_admission", None)
                _write_identity(paths, record)
        else:
            _stage(result, "Preflight", "SKIP", "no component publication needed")
        published = _facts(source.root, selected, lifecycle, ui)
        timer_updated = not timer_update or (publication_error is None
                         and not lifecycle._traffic_timer_update_required(engine_source))
        matches = all(published[:3]) and timer_updated
        result["components"]["updated"] = changed if (deploying or updater is not None) and matches else []
        result["components"]["unchanged"] = [name for name in ("Backend", "UI", "Host files") if name not in changed]
        if publication_error:
            _stage(result, "Install", "FAIL", publication_error)
        else:
            _stage(result, "Install", "PASS" if deploying or updater is not None else "SKIP",
                   ", ".join(changed) + " updated" if changed else "component files already match")
        _stage(result, "Identity", "PASS" if matches else "FAIL", "installed content comparison against qualified source")
        result["installed_content"] = "VERIFIED" if matches else "FAILED"
        result.setdefault("backup", {"status": "NOT_CREATED", "detail": "no backend content change"})
        _installed_version(result, _read_identity(paths))
        _report_lifecycle(result, paths, lifecycle)
        result["health"] = _read_health(paths, lifecycle, policy)
        manifest = _source_content_identity(source.root, lifecycle, ui, selected)["backend_sha256"]
        progress("Verifying Stable health and native acceptance")
        proof = (_native_acceptance(paths, lifecycle, manifest, policy) if matches and not publication_error
                 else {"status": "NOT_VERIFIED", "reason": "component publication or lifecycle is incomplete"})
        result["native_proof"] = proof
        _stage(result, "Native proof", {"PASS": "PASS", "FAIL": "FAIL", "NOT_VERIFIED": "INCOMPLETE"}[proof["status"]],
               proof["reason"] or "fenced native proof verified")
        states = {item["status"] for item in (*result["stages"].values(), *result["health"].values())}
        if "FAIL" in states or status == "UNHEALTHY_CURRENT_SOURCE":
            result["result"] = "FAILED"
            result["warnings"].append("Resolve the failed health or verification checks before repeating deployment.")
        elif "INCOMPLETE" in states:
            result["result"] = "INCOMPLETE"
            if proof["reason"]:
                result["warnings"].append(f"Complete the owner-backed acceptance checks ({proof['reason']}).")
        else:
            result["result"] = "SUCCESS" if deploying or updater is not None else "NOOP"
            last_now = (_read_identity(paths).get("last_verified_install") or {})
            needs_promotion = (not deploying and not rollback and matches
                and ((last_now.get("mode") == "STABLE" and last_now.get("git_sha") == commit
                      and last_now.get("acceptance", {}).get("status") != "VERIFIED")
                     or last_now.get("mode") == "DEV")
                and (_read_identity(paths).get("operation") or {}).get("status") == "VERIFIED_CONTENT")
            if deploying or rollback or needs_promotion:
                _record_stable_acceptance(paths, result, commit, proof, lifecycle=lifecycle,
                                          owner_source=source.root)
            if result["result"] in {"SUCCESS", "NOOP"}:
                _cleanup_retired_ui(paths, ui, result)
                if not paths.test_driver:
                    _cleanup_retired_backup(paths, lifecycle, result)
                    _cleanup_dev_baseline_backup(paths, lifecycle, result)
                if identity.get("dev_current") is not None:
                    _cleanup_development_snapshots(paths, result)
        result["status"] = result["result"]
        result["content_noop"] = not deploying
        result["acceptance"] = {"status": "VERIFIED" if result["result"] in {"SUCCESS", "NOOP"} else "NOT_VERIFIED"}
        if not paths.test_driver and result["result"] in {"SUCCESS", "NOOP"}:
            cleanup = _cleanup_expired_staging(paths)
            storage = result.setdefault("storage", {})
            storage["cleanup_reclaimed_bytes"] = storage.get("cleanup_reclaimed_bytes", 0) + cleanup["reclaimed_bytes"]
            storage.setdefault("cleanup_failures", []).extend(cleanup["failures"])
        try:
            result.setdefault("storage", {})["temporary_staging_bytes"] = _staging_space_bytes(source.root.parent)
        except (OSError, DeploymentError) as exc:
            result.setdefault("storage", {})["temporary_staging_status"] = "INCOMPLETE"
            result.setdefault("warnings", []).append(f"Temporary staging usage could not be measured: {type(exc).__name__}.")
        _refresh_storage_summary(paths, result, lifecycle=lifecycle)


def _stable_check(paths: Paths, result: dict, identity: dict) -> tuple[str, int]:
    _installed_version(result, identity)
    last = identity.get("last_verified_install") or {}
    operation = identity.get("operation") or {}
    if operation.get("kind") == "self_upgrade" and operation.get("status") == "COMPLETE":
        try:
            verified = all(hashlib.sha256(_read_stable_source_file(paths.target(path), paths.owner_uid))
                           .hexdigest() == operation[key] for path, key in (
                               ("usr/local/libexec/fwrouter/local_deploy.py", "candidate_module_sha256"),
                               ("usr/local/sbin/fwrouter-deploy", "wrapper_sha256")))
        except (OSError, DeploymentError):
            _stage(result, "Updater", "INCOMPLETE", "installed updater module or wrapper could not be read safely")
        else:
            _stage(result, "Updater", "PASS" if verified else "FAIL",
                   "installed updater and wrapper match the completed transition" if verified
                   else "installed updater or wrapper differs from the completed transition")
    recovery = identity.get("stable_admission")
    dev_recovery = identity.get("dev_admission")
    configured = None
    lifecycle = None
    if paths.config.exists():
        configured, _ = _read_config(paths.config, paths.owner_uid)
        lifecycle, _lock, ui = _source_modules(configured, paths)
    _refresh_storage_summary(paths, result, lifecycle=lifecycle)
    policy = _read_acceptance_policy(paths)
    policy_status, policy_detail, policy = _runtime_image_evidence(paths, policy)
    _stage(result, "Runtime images", policy_status, policy_detail)
    if result.get("mode") == "DEV" and not paths.test_driver and last.get("mode") != "DEV":
        try:
            if not identity.get("stable_current"):
                raise DeploymentIncomplete("A real deployment must first verify and record the installed Stable recovery baseline.")
            stable = identity["stable_current"]
            _read_lifecycle_owner_capsule(paths, stable.get("lifecycle_owner_sha", stable["source_sha"]))
        except (DeploymentError, OSError) as exc:
            _stage(result, "Stable recovery", "INCOMPLETE", str(exc))
        else:
            _stage(result, "Stable recovery", "PASS", "rollback source and lifecycle owner pins recorded")
    if configured is not None:
        _report_lifecycle(result, paths, lifecycle)
        result["health"] = _read_health(paths, lifecycle, policy)
        backup_status = result["rollback_database_backup"]
        _stage(result, "Rollback database", "PASS" if backup_status == "VERIFIED" else
               "INCOMPLETE" if backup_status in {"INCOMPLETE", "NOT_VERIFIED"} else "SKIP",
               "verified pinned schema-27 backup" if backup_status == "VERIFIED" else
               "pinned backup verification failed" if backup_status == "INCOMPLETE" else
               "no database migration or compatibility change requires a backup")
        if last:
            installed = _installed_content_identity(configured, paths, lifecycle, ui)
            matches = installed == last.get("installed_content")
            _stage(result, "Identity", "PASS" if matches else "FAIL",
                   "installed content matches the last verified install" if matches
                   else "installed content differs from the last verified install")
            proof = (_native_acceptance(paths, lifecycle, installed["backend_sha256"], policy) if matches
                     else {"status": "NOT_VERIFIED", "reason": "content identity changed"})
            result["native_proof"] = proof
            _stage(result, "Native proof", {"PASS": "PASS", "FAIL": "FAIL", "NOT_VERIFIED": "INCOMPLETE"}[proof["status"]],
                   proof["reason"] or "fenced native proof verified")
    else:
        _stage(result, "Lifecycle", "INCOMPLETE", "local lifecycle inspection source is unavailable")
        _stage(result, "Health", "INCOMPLETE", "local health inspection source is unavailable")
    if "Identity" not in result["stages"]:
        _stage(result, "Identity", "INCOMPLETE",
               "installed version is UNKNOWN" if not last else "last verified install recorded, current content unverified")
    if recovery:
        command = "--rollback" if identity.get("rollback_admission") else "--stable"
        _stage(result, "Recovery", "INCOMPLETE", f"rerun {command} to resume {recovery['source_sha'][:12]}")
    elif dev_recovery:
        _stage(result, "Recovery", "INCOMPLETE",
               f"rerun --dev to finish the captured snapshot from {dev_recovery['base_git_sha'][:12]}")
    if result.get("dev_baseline_database_backup") == "INCOMPLETE":
        _stage(result, "DEV rollback database", "INCOMPLETE", "accepted Stable database protection needs verification")
    states = {row["status"] for row in (*result["stages"].values(), *result.get("health", {}).values())}
    if "FAIL" in states:
        return "FAILED", 1
    if "INCOMPLETE" in states:
        return "STABLE_RECOVERY_PENDING" if recovery else "DEV_RECOVERY_PENDING" if dev_recovery else "INCOMPLETE", 3
    return "CHECKED", 0


DEFAULT_DEV_SOURCE = Path("/srv/fwrouter")


def main(argv: list[str] | None = None, *, paths_override: Paths | None = None,
         owner_driver: Callable[[object, str, Path, bool, Path, list[str]], subprocess.CompletedProcess] | None = None,
         system_driver: Callable[[list[str]], subprocess.CompletedProcess] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--stable", action="store_true", help="deploy latest qualified origin/main")
    mode.add_argument("--rollback", action="store_true", help="restore the previously accepted Stable version")
    mode.add_argument("--dev", action="store_true", help="deploy local development source")
    mode.add_argument("--install-dev-updater", action="store_true",
                      help="install the exact trusted checkout updater for DEV and its rollback only")
    parser.add_argument("--acknowledge-local-deployer", action="store_true",
                        help="acknowledge installing a local updater alongside the qualified Main updater")
    parser.add_argument("--source", metavar="PATH", help="Development source checkout (default /srv/fwrouter)")
    parser.add_argument("--acknowledge-stable-replacement", action="store_true",
                        help="acknowledge a Development deployment that would replace Stable")
    parser.add_argument("--json", action="store_true", help="emit structured JSON output")
    parser.add_argument("--verbose", action="store_true", help="include detailed diagnostics")
    parser.add_argument("--check", action="store_true", help="inspect source and current state without mutation")
    parser.add_argument("--configure-source", metavar="PATH", help="select a root-controlled clean local Git checkout")
    parser.add_argument("--initialize-acceptance-policy", metavar="PATH",
                        help="install an independently approved owner-only acceptance policy")
    parser.add_argument("--allow-timer-catchup", action="store_true",
                        help="explicitly allow the lifecycle owner to reactivate previously active timers")
    parser.add_argument("--self-upgrade", action="store_true",
                        help="qualify and atomically replace only this updater module from current Main")
    parser.add_argument("--main-sha")
    parser.add_argument("--run-id")
    parser.add_argument("--run-attempt")
    parser.add_argument("--artifact-id")
    parser.add_argument("--artifact-digest")
    parser.add_argument("--expected-installed-module-sha256")
    parser.add_argument("--confirm-main-sha")
    args = parser.parse_args(argv)
    if args.install_dev_updater != args.acknowledge_local_deployer:
        parser.error("--install-dev-updater requires --acknowledge-local-deployer")
    if args.install_dev_updater and any((args.check, args.source, args.configure_source, args.self_upgrade,
                                        args.initialize_acceptance_policy, args.allow_timer_catchup)):
        parser.error("--install-dev-updater is a standalone operation")
    if args.initialize_acceptance_policy is not None and not args.initialize_acceptance_policy:
        parser.error("--initialize-acceptance-policy requires a non-empty approved file path")
    if args.source and not args.dev:
        parser.error("--source requires --dev")
    if args.acknowledge_stable_replacement and not args.dev:
        parser.error("--acknowledge-stable-replacement requires --dev")
    if (args.stable or args.rollback or args.dev) and (args.configure_source or args.self_upgrade or args.allow_timer_catchup):
        parser.error("deployment modes cannot be combined with legacy source, updater or timer options")
    test_driver = paths_override is not None
    if test_driver and (paths_override.target_root == Path("/") or not paths_override.test_driver):
        parser.error("test context must use an explicitly disposable target")
    if args.self_upgrade and (args.check or args.configure_source or args.allow_timer_catchup):
        parser.error("--self-upgrade cannot be combined with --check, source configuration or timer catch-up")
    if not args.self_upgrade and any((args.main_sha, args.run_id, args.run_attempt, args.artifact_id,
                                      args.artifact_digest, args.expected_installed_module_sha256,
                                      args.confirm_main_sha)):
        parser.error("self-upgrade pins require --self-upgrade")
    if args.check and (args.configure_source or args.allow_timer_catchup):
        parser.error("--check cannot be combined with source configuration or timer catch-up")
    if args.configure_source and args.allow_timer_catchup:
        parser.error("source configuration cannot be combined with timer catch-up")
    if args.initialize_acceptance_policy is not None and any((args.stable, args.rollback, args.dev, args.source,
                                                   args.acknowledge_stable_replacement, args.check,
                                                   args.configure_source, args.allow_timer_catchup,
                                                   args.self_upgrade, args.main_sha, args.run_id,
                                                   args.run_attempt, args.artifact_id, args.artifact_digest,
                                                   args.expected_installed_module_sha256, args.confirm_main_sha)):
        parser.error("--initialize-acceptance-policy is a standalone operation")
    paths = paths_override or Paths(config=CONFIG_PATH, owner_uid=0)
    started = time.monotonic()
    mode = ("ACCEPTANCE POLICY" if args.initialize_acceptance_policy is not None else
            "DEV" if args.dev else "STABLE" if args.stable or args.rollback else "LOCAL")
    summary = {"mode": mode, "stages": {}, "warnings": []}
    phase = "Source"
    if not args.json and not args.check:
        _emit_progress(f"FWRouter Deployment — {mode}")

    def progress(message: str) -> None:
        nonlocal phase
        phase = {"Resolving": "CI", "Checking": "Preflight", "Applying": "Install",
                 "Restoring": "Lifecycle", "Verifying": "Acceptance"}.get(
            message.split()[0], phase)
        if not args.json:
            _emit_progress(message)

    def emit(status: str, exit_code: int, **fields) -> int:
        summary.update(status=status, duration_seconds=time.monotonic() - started, **fields)
        if (args.dev or args.stable or args.rollback) and "installed_version" not in summary:
            try:
                _installed_version(summary, _read_identity(paths))
            except (DeploymentError, OSError):
                summary["warnings"].append("The installed identity could not be read safely.")
        important = "FAIL" if exit_code == 1 else "INCOMPLETE" if exit_code in {3, 130} else None
        if important:
            for name, row in (*summary["stages"].items(), *summary.get("health", {}).items()):
                if row["status"] == important:
                    print(f"fwrouter-deploy: {name} · {row['detail']}", file=sys.stderr, flush=True)
        for failure in (summary.get("storage") or {}).get("cleanup_failures", []):
            name = failure.get("resource") or failure.get("digest", "resource")
            print(f"fwrouter-deploy: cleanup {name} · {failure.get('reason', 'see verbose diagnostics')}",
                  file=sys.stderr, flush=True)
        _emit_result(summary, as_json=args.json, verbose=args.verbose, heading=args.check)
        return exit_code

    def interrupted(_signum, _frame) -> None:
        raise KeyboardInterrupt

    old_term = signal.signal(signal.SIGTERM, interrupted)
    try:
        if os.geteuid() != 0 and not test_driver:
            raise DeploymentError("Run this operation as root.")
        module = Path(__file__).resolve()
        retained = paths.state_dir / DEV_UPDATER_DIRECTORY / "local_deploy.py"
        if module == retained:
            if not (args.dev or args.rollback or (args.check and not args.stable)) or any((
                    args.stable, args.self_upgrade, args.configure_source, args.initialize_acceptance_policy,
                    args.allow_timer_catchup, args.install_dev_updater)):
                raise DeploymentError("The local updater is restricted to DEV, its qualified rollback, and read-only checks.")
            local = _read_dev_updater(paths)
            summary["dev_updater"] = local
            _stage(summary, "DEV updater", "PASS", f"local DEV module @ {local['source_sha'][:12]}")
        elif (not test_driver and module != paths.target("usr/local/libexec/fwrouter/local_deploy.py")
              and not args.check and (args.dev or args.stable or args.rollback)):
            raise DeploymentError("Install this DEV updater from its trusted checkout before applying a runtime change.")
        if args.install_dev_updater:
            result = _install_dev_updater(paths, module.parent.parent)
            summary["dev_updater"] = result
            _stage(summary, "DEV updater", "PASS", f"installed local DEV module @ {result['source_sha'][:12]}")
            return emit(result["result"], 0)
        if args.initialize_acceptance_policy is not None:
            status = _initialize_acceptance_policy(paths, Path(args.initialize_acceptance_policy))
            _stage(summary, "Native policy", "PASS" if status == "INSTALLED" else "SKIP",
                   "independently approved policy installed and verified" if status == "INSTALLED"
                   else "identical approved policy already installed; no changes made")
            return emit("SUCCESS" if status == "INSTALLED" else "NOOP", 0, result=status,
                        policy_status=status)
        if (args.dev or args.stable or args.rollback) and not args.check:
            with _configuration_component_lock(paths, command=True):
                if module == retained:
                    _read_dev_updater(paths)
                    if args.rollback:
                        active = _read_identity(paths)
                        if ((active.get("last_verified_install") or {}).get("mode") != "DEV"
                                and not active.get("rollback_admission")):
                            raise DeploymentIncomplete("The local updater only rolls back a Development deployment.")
                if args.dev:
                    _development_deploy(
                        paths, summary, source_path=Path(args.source) if args.source else DEFAULT_DEV_SOURCE,
                        acknowledge_stable_replacement=args.acknowledge_stable_replacement,
                        owner_driver=owner_driver, system_driver=system_driver, progress=progress,
                    )
                else:
                    _stable_deploy(paths, summary, owner_driver=owner_driver, system_driver=system_driver,
                                   progress=progress, rollback=args.rollback)
            return emit(summary["result"], {"SUCCESS": 0, "NOOP": 0, "FAILED": 1, "INCOMPLETE": 3}[summary["result"]])
        if args.check:
            identity = _read_identity(paths)
            last = identity.get("last_verified_install") or {}
            recovery = identity.get("stable_admission")
            operation = identity.get("operation") or {}
            upgraded = operation.get("kind") == "self_upgrade" and operation.get("status") == "COMPLETE"
            if (args.dev or args.stable or args.rollback or recovery or identity.get("dev_admission")
                    or last.get("mode") in {"STABLE", "DEV"} or upgraded):
                if args.dev and (args.source or not paths.test_driver):
                    dev_path = Path(args.source) if args.source else DEFAULT_DEV_SOURCE
                    _check_trusted_tree(dev_path, paths.owner_uid)
                    candidate = _development_source(dev_path)
                    summary["source"] = candidate
                    summary["source_commit"] = candidate["git_sha"]
                    _stage(summary, "Source", "PASS", f"local DEV @ {candidate['git_sha'][:12]}; working contents read")
                status, code = _stable_check(paths, summary, identity)
                if not (args.dev or args.stable or args.rollback):
                    summary["mode"] = ("DEV" if identity.get("dev_admission") else
                                       "STABLE" if recovery else last.get("mode", "UNKNOWN"))
                return emit(status, code, result="INCOMPLETE" if code == 3 else status)
        if args.configure_source:
            source = Path(args.configure_source).resolve(strict=True)
            commit = _source_commit(source, paths.owner_uid)
            with _configuration_component_lock(paths):
                commit = _source_commit(source, paths.owner_uid)
                _write_config(paths.config, source, commit, paths.owner_uid)
            _stage(summary, "Source", "PASS", f"local @ {commit[:12]}")
            return emit("SOURCE_SELECTED", 0, source_commit=commit)
        source, commit = _read_config(paths.config, paths.owner_uid)
        if args.self_upgrade:
            result = _self_upgrade(paths, source, commit, args)
            status = result.pop("status")
            _stage(summary, "Updater", "PASS", status.replace("_", " ").lower())
            return emit(status, 0, **result)
        if args.check:
            identity = _read_identity(paths)
            operation = identity.get("operation") if identity.get("status") != "UNKNOWN" else None
            if isinstance(operation, dict) and operation.get("kind") == "self_upgrade" \
                    and operation.get("status") == "PENDING":
                _stage(summary, "Updater", "INCOMPLETE", "rerun --self-upgrade with the original pinned transition")
                return emit("UPDATER_SELF_UPGRADE_PENDING", 0, source_commit=commit, deployment_identity=identity)
        installed_module = Path(__file__).resolve()
        installed_wrapper = paths.target("usr/local/sbin/fwrouter-deploy")
        if not paths.test_driver:
            if _digest(installed_module) != _digest(source / "installer/local_deploy.py"):
                raise DeploymentError("installed deployment command differs from selected source")
            wrapper_result = _run(["/usr/bin/cmp", "--silent", str(installed_wrapper),
                                   str(source / "host/sbin/fwrouter-deploy")])
            if wrapper_result.returncode != 0:
                raise DeploymentError("installed deployment entrypoint differs from selected source")
        if args.check:
            status = check(paths, source, commit)
            _installed_version(summary, _read_identity(paths))
            _stage(summary, "Source", "PASS", f"local @ {commit[:12]}")
            lifecycle, _lock, _ui = _source_modules(source, paths)
            _report_lifecycle(summary, paths, lifecycle)
            _stage(summary, "Source state", "FAIL" if status == "UNHEALTHY_CURRENT_SOURCE" else
                   "INCOMPLETE" if "PENDING" in status else "PASS", status.replace("_", " ").lower())
            return emit(status, 1 if status == "UNHEALTHY_CURRENT_SOURCE" else 0, source_commit=commit)
        status = check(paths, source, commit)
        if status == "NOOP":
            _installed_version(summary, _read_identity(paths))
            _stage(summary, "Install", "SKIP", "component files already match")
            _stage(summary, "Native proof", "INCOMPLETE", "legacy local mode has no native acceptance policy")
            return emit("NOOP", 3, source_commit=commit, result="INCOMPLETE")
        if status == "UNHEALTHY_CURRENT_SOURCE":
            raise DeploymentError("installed source matches selection but runtime health is incomplete; resolve health before deployment")
        if status == "UPDATER_SELF_UPGRADE_PENDING":
            raise DeploymentError("pending updater self-upgrade must be resumed with its exact pinned transition")
        result = apply(paths, source, commit, allow_timer_catchup=args.allow_timer_catchup,
                       owner_driver=owner_driver, system_driver=system_driver)
        _installed_version(summary, _read_identity(paths))
        _stage(summary, "Install", "PASS", "legacy local deployment completed file publication")
        _stage(summary, "Native proof", "INCOMPLETE", "legacy local mode has no native acceptance policy")
        return emit(result.pop("status"), 3, **result, result="INCOMPLETE")
    except DeploymentIncomplete as exc:
        _stage(summary, phase, "INCOMPLETE", str(exc))
        return emit("INCOMPLETE", 3, result="INCOMPLETE")
    except KeyboardInterrupt:
        _stage(summary, phase, "INCOMPLETE", "interrupted; rerun the same deployment mode to resume any admitted work")
        return emit("INCOMPLETE", 130, result="INCOMPLETE")
    except (DeploymentError, OSError, ValueError, RuntimeError) as exc:
        message = str(exc) if isinstance(exc, (DeploymentError, RuntimeError)) else type(exc).__name__
        _stage(summary, phase, "FAIL", message)
        return emit("FAILED", 1, result="FAILED")
    finally:
        signal.signal(signal.SIGTERM, old_term)


if __name__ == "__main__":
    raise SystemExit(main())
