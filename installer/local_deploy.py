#!/usr/bin/env python3
"""Apply one explicitly selected, clean local FWRouter checkout on this host."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from typing import Callable


CONFIG_PATH = Path("/etc/fwrouter/deploy-source.json")
INSTALLER_LOCK_FD = 9
MAINTENANCE_LOCK_FD = 8
UNSAFE_WRITE = stat.S_IWGRP | stat.S_IWOTH
MAX_TREE_ENTRIES = 10000
MAX_OLD_SOURCE_COMMITS = 1000
MAX_SOURCE_ENTRIES = 100000
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


def _run(argv: list[str], *, timeout: int = 30, pass_fds: tuple[int, ...] = ()) -> subprocess.CompletedProcess:
    try:
        env = os.environ.copy()
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        if argv and Path(argv[0]).name == "git":
            env["GIT_CONFIG_NOSYSTEM"] = "1"
            env["GIT_CONFIG_GLOBAL"] = "/dev/null"
            env["GIT_CONFIG_COUNT"] = "2"
            env["GIT_CONFIG_KEY_0"] = "core.fsmonitor"
            env["GIT_CONFIG_VALUE_0"] = "false"
            env["GIT_CONFIG_KEY_1"] = "core.hooksPath"
            env["GIT_CONFIG_VALUE_1"] = "/dev/null"
        return subprocess.run(argv, check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, timeout=timeout, pass_fds=pass_fds, env=env)
    except (OSError, subprocess.SubprocessError) as exc:
        raise DeploymentError(f"command did not complete: {Path(argv[0]).name}") from exc


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
                              timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        raise DeploymentError("cannot inspect local Git source history") from exc


def _materialize_old_source(source: Path, commit: str, destination: Path) -> Path:
    """Extract only the matched backend and lifecycle helper into a disposable directory."""
    destination.mkdir(mode=0o700)
    process = subprocess.Popen(
        ["/usr/bin/git", "-C", str(source), "archive", commit, "backend",
         "installer/controlled_backend_lifecycle.py"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
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
            healthy = healthy and lifecycle._is_active("fwrouter-xray.service")
        except Exception:
            healthy = False
    return backend_same, ui_same, host_same, healthy


def check(paths: Paths, source: Path, commit: str) -> str:
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
            if checkpoint_digest not in {selected_digest, installed_digest}:
                raise DeploymentError("interrupted operation source differs from both installed and selected source")

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

    def resume_result(expected_operation_id: str) -> dict:
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

    if needs_transfer and pending is not None and pending["status"] == "pending":
        if pending["backend_manifest_sha256"] != installed_digest:
            raise DeploymentError("interrupted checkpoint and installed backend do not identify the same old source")
        transfer_args = ["--expected-operation-id", pending["operation_id"],
                         "--expected-current-manifest-sha256", installed_digest,
                         "--expected-new-manifest-sha256", selected_digest]
        transferred = owner_call("transfer", source, transfer_args)
        if transferred.returncode != 0:
            raise DeploymentError("owner source transfer failed before installer writes")
        pending = checkpoint_after_transfer(pending, transferred)

    if pending is None or pending["status"] == "complete":
        if installed_digest == selected_digest:
            stopped = owner_call("stop", source)
            if stopped.returncode != 0:
                raise DeploymentError("controlled lifecycle stop failed")
        else:
            old_commit = _find_source_commit(source, paths.target("opt/fwrouter-api"), lifecycle)
            with tempfile.TemporaryDirectory(prefix="fwrouter-old-source-") as temp:
                old_root = _materialize_old_source(source, old_commit, Path(temp) / "source")
                stopped = owner_call("stop", old_root)
                if stopped.returncode != 0:
                    raise DeploymentError("old-source controlled lifecycle stop failed")
                pending = checkpoint_after_stop(stopped)
                if (pending["status"] != "pending"
                        or pending["backend_manifest_sha256"] != installed_digest):
                    raise DeploymentError("old-source stop checkpoint does not match the installed backend")
                transfer_args = ["--expected-operation-id", pending["operation_id"],
                                 "--expected-current-manifest-sha256", installed_digest,
                                 "--expected-new-manifest-sha256", selected_digest]
                transferred = owner_call("transfer", source, transfer_args)
                if transferred.returncode != 0:
                    raise DeploymentError("owner source transfer failed before installer writes")
                pending = checkpoint_after_transfer(pending, transferred)
        if installed_digest == selected_digest:
            pending = checkpoint_after_stop(stopped)
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
    try:
        lock_fd, maintenance_fd, saved_maintenance_target, saved_installer_target = _component_lock(lock_module, paths)
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
        if not resume_only:
            _normalize_legacy_ui(paths.target("opt/fwrouter-ui"), paths.owner_uid)
            _rows, host_plan = _host_plan(source, paths)
            if host_plan is not None:
                _publish_host_pair(source, paths, host_plan)
            _run_installer(source, paths, ["backend", "ui"], inherited=True)
        backend_same, ui_same, host_same, _healthy = _facts(source, paths, lifecycle, ui)
        if not (backend_same and ui_same and host_same):
            raise DeploymentError("installed backend manifest, UI release or host pair differs from selected source before API startup")
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
        return resume_result(checkpoint_under_lock["operation_id"])
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
    args = parser.parse_args(argv)
    test_driver = paths_override is not None
    if test_driver and (paths_override.target_root == Path("/") or not paths_override.test_driver):
        parser.error("test context must use an explicitly disposable target")
    if os.geteuid() != 0 and not test_driver:
        print("fwrouter-deploy: must run as root", file=sys.stderr)
        return 1
    if args.check and (args.configure_source or args.allow_timer_catchup):
        parser.error("--check cannot be combined with source configuration or timer catch-up")
    if args.configure_source and args.allow_timer_catchup:
        parser.error("source configuration cannot be combined with timer catch-up")
    paths = paths_override or Paths(config=CONFIG_PATH, owner_uid=0)
    try:
        if args.configure_source:
            source = Path(args.configure_source).resolve(strict=True)
            commit = _source_commit(source, paths.owner_uid)
            _write_config(paths.config, source, commit, paths.owner_uid)
            print(json.dumps({"status": "SOURCE_SELECTED", "source_commit": commit}, sort_keys=True))
            return 0
        source, commit = _read_config(paths.config, paths.owner_uid)
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
            print(json.dumps({"status": status, "source_commit": commit}, sort_keys=True))
            return 1 if status == "UNHEALTHY_CURRENT_SOURCE" else 0
        status = check(paths, source, commit)
        if status == "NOOP":
            print(json.dumps({"status": "NOOP", "source_commit": commit}, sort_keys=True))
            return 0
        if status == "UNHEALTHY_CURRENT_SOURCE":
            raise DeploymentError("installed source matches selection but runtime health is incomplete; resolve health before deployment")
        result = apply(paths, source, commit, allow_timer_catchup=args.allow_timer_catchup,
                       owner_driver=owner_driver, system_driver=system_driver)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (DeploymentError, OSError, ValueError, RuntimeError) as exc:
        print(f"fwrouter-deploy: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
