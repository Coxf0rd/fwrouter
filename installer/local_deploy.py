#!/usr/bin/env python3
"""Apply one explicitly selected, clean local FWRouter checkout on this host."""
from __future__ import annotations

import argparse
import ast
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import selectors
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
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
HOST_ROWS = (
    ("host/libexec/fwrouter/fwrouter-boot-preflight.sh",
     "usr/local/libexec/fwrouter/fwrouter-boot-preflight.sh", 0o755),
    ("host/systemd/fwrouter-api.service", "etc/systemd/system/fwrouter-api.service", 0o644),
)


class DeploymentError(RuntimeError):
    pass


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
                "GIT_CONFIG_VALUE_1": "/dev/null", "GIT_NO_REPLACE_OBJECTS": "1"})
    return env


def _run(argv: list[str], *, timeout: int = 30, pass_fds: tuple[int, ...] = (),
         env_overrides: dict[str, str] | None = None,
         clean_git_env: bool = False) -> subprocess.CompletedProcess:
    try:
        env = os.environ.copy()
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


def _source_modules(source: Path, paths: Paths) -> tuple[object, object, object]:
    installer = source / "installer"
    lifecycle = _load_module(installer / "controlled_backend_lifecycle.py", "fwrouter_local_deploy_lifecycle")
    lifecycle.SOURCE_BACKEND = source / "backend"
    lifecycle.TARGET_BACKEND = paths.target("opt/fwrouter-api")
    lifecycle.DEFAULT_STATE_DIR = paths.state_dir
    lock = _load_module(installer / "component_install_lock.py", "fwrouter_deploy_component_lock")
    ui = _load_module(installer / "ui_publication.py", "fwrouter_local_deploy_ui")
    ui.component_install_lock = lock
    return lifecycle, lock, ui


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
        return subprocess.run(argv, check=False, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              timeout=timeout, env=_git_environment())
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
    if (not isinstance(value, dict) or value.get("schema") != "fwrouter-deployment-identity/v1"
            or set(value) != {"schema", "last_verified_install", "operation", "components"}):
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
        return (isinstance(item, dict) and item.get("status") == "NOT_VERIFIED"
                and isinstance(item.get("scope"), str) and isinstance(item.get("evidence_refs"), list)
                and all(isinstance(ref, str) for ref in item["evidence_refs"]))

    def validate_acceptance(item: object) -> bool:
        return isinstance(item, dict) and item.get("status") == "NOT_VERIFIED"

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
                or component.get("provenance") not in {"local_git", "verified_actual_main_ci"}
                or not isinstance(component.get("installed_sha256"), (str, dict))
                or not isinstance(component.get("operation_id"), str)
                or component.get("mode") != "UNKNOWN"
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
                or last.get("provenance") not in {"local_git", "UNKNOWN"}
                or last.get("qualification_status") != "NOT_VERIFIED"
                or last.get("mode") != "UNKNOWN" or not isinstance(last.get("dirty"), bool)
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
                    or requested.get("mode") != "UNKNOWN" or requested.get("provenance") != "local_git"
                    or requested.get("qualification_status") != "NOT_VERIFIED"
                    or not isinstance(requested.get("dirty"), bool)
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
    return value


def _write_identity(paths: Paths, value: dict) -> None:
    """Durably replace the record while caller holds the existing component mutex."""
    directory = paths.state_dir
    info = directory.lstat()
    if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or info.st_uid != paths.owner_uid or stat.S_IMODE(info.st_mode) != 0o700):
        raise DeploymentError("controlled lifecycle state directory is unsafe")
    payload = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
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
        operation["pending_units"] = pending_units or []
        operation["pending_units_status"] = "OBSERVED" if pending_units is not None else "UNKNOWN"
        if details:
            operation.update(details)
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
                "qualification_status": "NOT_VERIFIED",
                "native_proof": operation["native_proof"], "acceptance": operation["acceptance"]}
            operation["completed_at"] = record["last_verified_install"]["completed_at"]
            for name, source_rel, digest_key in (
                    ("backend", "backend", "backend_sha256"),
                    ("ui", "ui", "ui_sha256")):
                record["components"][name] = {"source_path": str(source / source_rel),
                    "source_sha": commit, "provenance": "local_git", "mode": "UNKNOWN",
                    "qualification_status": "NOT_VERIFIED", "installed_sha256": installed[digest_key],
                    "operation_id": operation_id}
            record["components"]["host_pair"] = {"source_path": str(source / "host"),
                "source_sha": commit, "provenance": "local_git", "mode": "UNKNOWN",
                "qualification_status": "NOT_VERIFIED", "installed_sha256": installed["host_pair_sha256"],
                "operation_id": operation_id}
            updater_source = source / "installer/local_deploy.py"
            updater_installed = paths.target("usr/local/libexec/fwrouter/local_deploy.py")
            if updater_source.is_file() and updater_installed.is_file():
                updater_digest = _digest(updater_installed)
                if updater_digest == _digest(updater_source):
                    prior_updater = record["components"].get("updater")
                    if not isinstance(prior_updater, dict) or prior_updater.get("installed_sha256") != updater_digest:
                        record["components"]["updater"] = {"source_path": str(updater_source),
                            "source_sha": commit, "provenance": "local_git", "mode": "UNKNOWN",
                            "qualification_status": "NOT_VERIFIED", "installed_sha256": updater_digest,
                            "operation_id": operation_id}
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
            raw = _bounded_process(["/usr/bin/docker", "inspect", "--format", "{{.Image}}",
                                    f"fwrouter-{component}"], limit=256, timeout=10)
            value = (raw or b"").decode("ascii").strip()
        except (DeploymentError, UnicodeError):
            value = ""
        observed[component] = (value if value.startswith("sha256:") and len(value) == 71
                               and all(ch in "0123456789abcdef" for ch in value[7:]) else "UNKNOWN")
    return observed


def _assert_requested_source(paths: Paths, source: Path, commit: str) -> None:
    if paths.test_driver and not paths.config.exists():
        return
    configured_source, configured_commit = _read_config(paths.config, paths.owner_uid)
    if configured_source != source or configured_commit != commit:
        raise DeploymentError("selected source configuration changed during deployment")


@contextmanager
def _configuration_component_lock(paths: Paths):
    """Use the existing common component mutex without importing selected-source code."""
    parent = paths.target("opt")
    parent_info = parent.lstat()
    if (not stat.S_ISDIR(parent_info.st_mode) or stat.S_ISLNK(parent_info.st_mode)
            or parent_info.st_uid not in {0, paths.owner_uid}
            or parent_info.st_mode & UNSAFE_WRITE):
        raise DeploymentError("component lock directory is unsafe")
    lock_path = parent / ".fwrouter-component-install.lock"
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
        raise DeploymentError("explicit updater self-upgrade requires /usr/bin/gh")
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
    pending_resume = pending_resume and prior_operation.get("source_path") == str(source)
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
    if not pending_resume:
        main = _gh_api("repos/Coxf0rd/fwrouter/commits/main")
        if main.get("sha") != sha:
            raise DeploymentError("pinned source is no longer the current GitHub Main head")
        run = _gh_api(f"repos/Coxf0rd/fwrouter/actions/runs/{args.run_id}")
        if (run.get("id") != int(args.run_id) or run.get("run_attempt") != int(args.run_attempt)
                or run.get("event") != "push" or run.get("head_branch") != "main"
                or run.get("head_sha") != sha or run.get("status") != "completed"
                or run.get("conclusion") != "success" or run.get("path") != ".github/workflows/test-gates.yml"
                or not isinstance(run.get("repository"), dict)
                or run["repository"].get("full_name") != "Coxf0rd/fwrouter"):
            raise DeploymentError("GitHub run is not a successful exact-SHA push to Main")
        jobs = _gh_api(f"repos/Coxf0rd/fwrouter/actions/runs/{args.run_id}/jobs?per_page=100")
        if not _successful_main_jobs(jobs):
            raise DeploymentError("GitHub run jobs are incomplete, failed, skipped or paginated")
        artifact = _gh_api(f"repos/Coxf0rd/fwrouter/actions/artifacts/{args.artifact_id}")
        expected_name = f"fwrouter-main-evidence-{sha}-{args.run_id}-{args.run_attempt}"
        workflow_run = artifact.get("workflow_run")
        if (artifact.get("id") != int(args.artifact_id) or artifact.get("expired") is not False
                or artifact.get("name") != expected_name or artifact.get("digest") != args.artifact_digest
                or not isinstance(workflow_run, dict) or workflow_run.get("id") != int(args.run_id)
                or workflow_run.get("head_sha") != sha or workflow_run.get("head_branch") != "main"):
            raise DeploymentError("GitHub artifact metadata does not match the pinned Main run")

    verifier_files = verifier_files or _verify_candidate_tree(source, sha, paths.owner_uid)
    for relative in ("installer/local_deploy.py", "installer/component_install_lock.py",
                     "installer/controlled_backend_lifecycle.py", "host/sbin/fwrouter-deploy"):
        if _read_stable_source_file(source / relative, paths.owner_uid) != _git_blob_bytes(source, sha, relative):
            raise DeploymentError("candidate updater, helper or wrapper differs from its exact Git blob")
    if not pending_resume:
        with tempfile.TemporaryDirectory(prefix="fwrouter-updater-qualification-") as scratch:
            scratch_path = Path(scratch)
            archive_path = scratch_path / "main-evidence.zip"
            _gh_artifact_zip(args.artifact_id, archive_path, expected_size=artifact.get("size_in_bytes"))
            if _digest(archive_path) != confirm_digest:
                raise DeploymentError("downloaded evidence artifact differs from its GitHub API digest")
            bundle_path = scratch_path / "bundle"
            bundle_path.mkdir(mode=0o700)
            _extract_evidence_zip(archive_path, bundle_path)
            verifier = source / "tests/gates/evidence_bundle.py"
            result = _run(["/usr/bin/python3", "-I", "-B", str(verifier), "verify", "--bundle",
                           str(bundle_path), "--expected-sha", sha, "--event", "push",
                           "--ref", "refs/heads/main", "--run-id", args.run_id,
                           "--attempt", args.run_attempt, "--require-main"], timeout=180,
                          env_overrides={"GIT_NO_REPLACE_OBJECTS": "1"}, clean_git_env=True)
            if result.returncode != 0:
                raise DeploymentError("exact-Main trusted evidence verifier rejected the downloaded artifact")
        try:
            verified_evidence = json.loads(result.stdout.strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError) as exc:
            raise DeploymentError("trusted evidence verifier returned no qualification result") from exc
        expected_evidence = {"status": "evidence_verified_not_deploy_authorized", "source_sha": sha,
                             "run_id": args.run_id, "run_attempt": args.run_attempt,
                             "strict_aggregate": "passed", "offline_source_checkout": "verified"}
        if any(verified_evidence.get(key) != value for key, value in expected_evidence.items()):
            raise DeploymentError("trusted evidence verifier result does not match the pinned Main qualification")
    lock = _load_module(source / "installer/component_install_lock.py", "fwrouter_updater_component_lock")
    lock_fd = None
    try:
        lock_fd, _ = lock.acquire(paths.target("opt"), maintenance_mode="shared")
        _assert_requested_source(paths, source, commit)
        if not pending_resume:
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
            same_transition = same_transition and active_transition.get("source_path") == str(source)
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
            same_transition = same_transition and active_transition.get("source_path") == str(source)
        if active_transition and active_transition.get("status") in {"PENDING", "FAILED"} and not same_transition:
            raise DeploymentError("another unfinished deployment operation prevents updater self-upgrade")
        current_module_digest = _digest(module_path)
        if current_module_digest not in {args.expected_installed_module_sha256, candidate_module_digest}:
            raise DeploymentError("installed updater changed before atomic publication")
        record = _identity_record_base(paths)
        if not same_transition:
            record["operation"] = {"kind": "self_upgrade", "status": "PENDING",
                "operation_id": operation_id, "source_sha": sha, "source_path": str(source),
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
            "operation_id": operation_id, "source_sha": sha, "source_path": str(source),
            "source_mode": "UNKNOWN", "provenance": "verified_actual_main_ci",
            "run_id": args.run_id, "run_attempt": args.run_attempt, "artifact_id": args.artifact_id,
            "artifact_digest": args.artifact_digest, "candidate_module_sha256": candidate_module_digest,
            "old_module_sha256": args.expected_installed_module_sha256,
            "wrapper": "unchanged", "wrapper_sha256": wrapper_digest, "verifier_sources": verifier_files,
            "completed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "acceptance": {"status": "NOT_VERIFIED", "scope": "updater_self_upgrade_only"}}
        record["components"]["updater"] = {"source_path": str(source / "installer/local_deploy.py"),
            "source_sha": sha, "provenance": "verified_actual_main_ci", "mode": "UNKNOWN",
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
        argv.extend(("--_component-lock-fd", str(INSTALLER_LOCK_FD),
                     "--_maintenance-lock-mode", "exclusive"))
        pass_fds = (INSTALLER_LOCK_FD, MAINTENANCE_LOCK_FD) if paths.target_root == Path("/") else (INSTALLER_LOCK_FD,)
    else:
        pass_fds = ()
    result = _run(argv, timeout=1800, pass_fds=pass_fds)
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
                  if paths.target_root == Path("/") else (INSTALLER_LOCK_FD,))
    if result.returncode != 0:
        raise DeploymentError("selective host pair publication failed")
    if paths.target_root == Path("/"):
        reload_result = _run(["/usr/bin/systemctl", "daemon-reload"])
        if reload_result.returncode != 0:
            raise DeploymentError("systemd could not reload the published API unit")
        check = _run(["/usr/bin/systemd-analyze", "verify",
                      str(paths.target("etc/systemd/system/fwrouter-api.service"))])
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


def check(paths: Paths, source: Path, commit: str) -> str:
    identity = _read_identity(paths)
    operation = identity.get("operation") if identity.get("status") != "UNKNOWN" else None
    if isinstance(operation, dict) and operation.get("kind") == "self_upgrade" \
            and operation.get("status") == "PENDING":
        return "UPDATER_SELF_UPGRADE_PENDING"
    lifecycle, _lock, ui = _source_modules(source, paths)
    backend_same, ui_same, host_same, healthy = _facts(source, paths, lifecycle, ui)
    checkpoint = None
    checkpoint_path = paths.state_dir / "pending.json"
    if checkpoint_path.exists():
        checkpoint = lifecycle._read_checkpoint(checkpoint_path)
        if checkpoint["status"] == "pending":
            manifest = lifecycle._backend_manifest(source / "backend")
            if checkpoint.get("maintenance_operation") is not None or checkpoint.get("runtime_quiescence") is not None:
                raise DeploymentError("pending lifecycle checkpoint uses an unsupported maintenance operation")
            selected_digest = lifecycle._manifest_digest(manifest)
            if selected_digest == checkpoint["backend_manifest_sha256"]:
                return "RESUME_PENDING_UNITS"
            installed = lifecycle._manifest_digest(lifecycle._backend_manifest(paths.target("opt/fwrouter-api")))
            if installed == checkpoint["backend_manifest_sha256"]:
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
          system_driver: Callable[[list[str]], subprocess.CompletedProcess] | None = None) -> dict:
    initial_identity = _read_identity(paths)
    initial_operation = initial_identity.get("operation") if initial_identity.get("status") != "UNKNOWN" else None
    if (isinstance(initial_operation, dict) and initial_operation.get("kind") == "self_upgrade"
            and initial_operation.get("status") == "PENDING"):
        raise DeploymentError("pending updater self-upgrade must be resumed with the exact pinned transition")
    lifecycle, lock_module, ui = _source_modules(source, paths)
    state_file = paths.state_dir / "pending.json"
    selected_manifest = lifecycle._backend_manifest(source / "backend")
    selected_digest = lifecycle._manifest_digest(selected_manifest)
    installed_manifest = lifecycle._backend_manifest(paths.target("opt/fwrouter-api"))
    installed_digest = lifecycle._manifest_digest(installed_manifest)
    pending = None
    if state_file.exists():
        pending = lifecycle._read_checkpoint(state_file)
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
        return _run(argv, timeout=1800)

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
        resumed = owner_call("resume", source)
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
        if not (backend_same and ui_same and host_same and healthy):
            raise DeploymentError("postflight failed: backend, UI, host pair, API, Gateway, Xray or Mihomo is not ready")
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
        pending = owner_transition("stop", source, [], pending, pending["operation_id"],
                                   "lifecycle_stop_recovery", checkpoint_after_stop)

    if needs_transfer and pending is not None and pending["status"] == "pending":
        if pending["backend_manifest_sha256"] != installed_digest:
            raise DeploymentError("interrupted checkpoint and installed backend do not identify the same old source")
        # Re-enter the existing owner's idempotent stop on every old-source
        # transfer resume. A prior stop can have durably checkpointed before a
        # unit-stop error, so apply must confirm quiescence before source CAS.
        old_commit = _find_source_commit(source, paths.target("opt/fwrouter-api"), lifecycle)
        with tempfile.TemporaryDirectory(prefix="fwrouter-old-source-") as temp:
            old_root = _materialize_old_source(source, old_commit, Path(temp) / "source")
            pending = owner_transition("stop", old_root, [], pending, pending["operation_id"],
                                       "lifecycle_stop_recovery", checkpoint_after_stop)
        transfer_args = ["--expected-operation-id", pending["operation_id"],
                         "--expected-current-manifest-sha256", installed_digest,
                         "--expected-new-manifest-sha256", selected_digest]
        pending = owner_transition("transfer", source, transfer_args, pending,
                                    pending["operation_id"], "lifecycle_source_transfer",
                                    lambda result: checkpoint_after_transfer(pending, result))

    if pending is None or pending["status"] == "complete":
        if installed_digest == selected_digest:
            before_stop = lifecycle._read_checkpoint(state_file) if state_file.exists() else None
            stopped_pending = owner_transition("stop", source, [], before_stop, None,
                                               "lifecycle_stop", checkpoint_after_stop)
        else:
            old_commit = _find_source_commit(source, paths.target("opt/fwrouter-api"), lifecycle)
            with tempfile.TemporaryDirectory(prefix="fwrouter-old-source-") as temp:
                old_root = _materialize_old_source(source, old_commit, Path(temp) / "source")
                before_stop = lifecycle._read_checkpoint(state_file) if state_file.exists() else None
                pending = owner_transition("stop", old_root, [], before_stop, None,
                                           "lifecycle_stop", checkpoint_after_stop)
                if (pending["status"] != "pending"
                        or pending["backend_manifest_sha256"] != installed_digest):
                    raise DeploymentError("old-source stop checkpoint does not match the installed backend")
                transfer_args = ["--expected-operation-id", pending["operation_id"],
                                 "--expected-current-manifest-sha256", installed_digest,
                                 "--expected-new-manifest-sha256", selected_digest]
                pending = owner_transition("transfer", source, transfer_args, pending,
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
            _run_installer(source, paths, ["backend", "ui"], inherited=True)
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
            result = system_driver(systemctl) if system_driver is not None else _run(systemctl)
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


def main(argv: list[str] | None = None, *, paths_override: Paths | None = None,
         owner_driver: Callable[[object, str, Path, bool, Path, list[str]], subprocess.CompletedProcess] | None = None,
         system_driver: Callable[[list[str]], subprocess.CompletedProcess] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="inspect source and current state without mutation")
    parser.add_argument("--configure-source", metavar="PATH", help="select a root-controlled clean local Git checkout")
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
    test_driver = paths_override is not None
    if test_driver and (paths_override.target_root == Path("/") or not paths_override.test_driver):
        parser.error("test context must use an explicitly disposable target")
    if os.geteuid() != 0 and not test_driver:
        print("fwrouter-deploy: must run as root", file=sys.stderr)
        return 1
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
    paths = paths_override or Paths(config=CONFIG_PATH, owner_uid=0)
    try:
        if args.configure_source:
            source = Path(args.configure_source).resolve(strict=True)
            commit = _source_commit(source, paths.owner_uid)
            with _configuration_component_lock(paths):
                commit = _source_commit(source, paths.owner_uid)
                _write_config(paths.config, source, commit, paths.owner_uid)
            print(json.dumps({"status": "SOURCE_SELECTED", "source_commit": commit}, sort_keys=True))
            return 0
        source, commit = _read_config(paths.config, paths.owner_uid)
        if args.self_upgrade:
            result = _self_upgrade(paths, source, commit, args)
            print(json.dumps(result, sort_keys=True))
            return 0
        if args.check:
            identity = _read_identity(paths)
            operation = identity.get("operation") if identity.get("status") != "UNKNOWN" else None
            if isinstance(operation, dict) and operation.get("kind") == "self_upgrade" \
                    and operation.get("status") == "PENDING":
                print(json.dumps({"status": "UPDATER_SELF_UPGRADE_PENDING", "source_commit": commit,
                                  "deployment_identity": identity}, sort_keys=True))
                return 0
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
            print(json.dumps({"status": status, "source_commit": commit,
                              "deployment_identity": _read_identity(paths)}, sort_keys=True))
            return 1 if status == "UNHEALTHY_CURRENT_SOURCE" else 0
        status = check(paths, source, commit)
        if status == "NOOP":
            print(json.dumps({"status": "NOOP", "source_commit": commit,
                              "deployment_identity": _read_identity(paths)}, sort_keys=True))
            return 0
        if status == "UNHEALTHY_CURRENT_SOURCE":
            raise DeploymentError("installed source matches selection but runtime health is incomplete; resolve health before deployment")
        if status == "UPDATER_SELF_UPGRADE_PENDING":
            raise DeploymentError("pending updater self-upgrade must be resumed with its exact pinned transition")
        result = apply(paths, source, commit, allow_timer_catchup=args.allow_timer_catchup,
                       owner_driver=owner_driver, system_driver=system_driver)
        result["deployment_identity"] = _read_identity(paths)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (DeploymentError, OSError, ValueError, RuntimeError) as exc:
        print(f"fwrouter-deploy: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
