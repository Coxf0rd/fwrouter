#!/usr/bin/env python3
"""Serialize cooperating FWRouter backend and UI installer operations."""
from __future__ import annotations

import argparse
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys
import uuid


class ComponentInstallLockError(RuntimeError):
    pass


LOCK_NAME = ".fwrouter-component-install.lock"
LOCK_FD = 9
MAINTENANCE_LEASE_FD = 8
MAINTENANCE_STATE_ROOT = Path("/var/lib/fwrouter-v2")
MAINTENANCE_LOCK_PATH = MAINTENANCE_STATE_ROOT / "state" / "controlled-rollout" / ".maintenance.lock"
MAINTENANCE_CHECKPOINT_PATH = MAINTENANCE_STATE_ROOT / "state" / "controlled-rollout" / "pending.json"
UNSAFE_WRITE = stat.S_IWGRP | stat.S_IWOTH
_OWNED_MAINTENANCE_FDS: dict[int, int] = {}
TRUSTED_HOST_UID = 0


def _trusted_uid() -> int:
    """Owner required for host lease files (test-patchable)."""
    return TRUSTED_HOST_UID


def _uses_host_maintenance_lease(parent: Path) -> bool:
    return Path(os.path.abspath(parent)) == Path("/opt")


def _read_maintenance_checkpoint() -> dict[str, object] | None:
    directory = MAINTENANCE_CHECKPOINT_PATH.parent
    cursor = Path(directory.anchor)
    for part in directory.parts[1:]:
        cursor = cursor / part
        try:
            info = cursor.lstat()
        except FileNotFoundError:
            return None
        if (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)
                or info.st_uid not in {0, os.geteuid()}
                or (info.st_mode & UNSAFE_WRITE and not (info.st_uid == 0 and info.st_mode & stat.S_ISVTX))):
            raise ComponentInstallLockError("maintenance checkpoint parent is unsafe")
    try:
        fd = os.open(MAINTENANCE_CHECKPOINT_PATH, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ComponentInstallLockError("cannot safely read maintenance checkpoint") from exc
    try:
        info = os.fstat(fd)
        named = MAINTENANCE_CHECKPOINT_PATH.lstat()
        if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(named.st_mode)
                or info.st_uid != _trusted_uid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
                or info.st_size <= 0 or info.st_size > 2 * 1024 * 1024
                or (info.st_dev, info.st_ino) != (named.st_dev, named.st_ino)):
            raise ComponentInstallLockError("maintenance checkpoint file is unsafe")
        raw = os.read(fd, 2 * 1024 * 1024 + 1)
    finally:
        os.close(fd)
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs)
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ComponentInstallLockError("maintenance checkpoint is malformed") from exc
    if not isinstance(value, dict):
        raise ComponentInstallLockError("maintenance checkpoint is malformed")
    operation = value.get("maintenance_operation")
    if operation is not None:
        if not isinstance(operation, dict) or set(operation) not in (
                {"kind", "status"}, {"kind", "status", "backup"}):
            raise ComponentInstallLockError("maintenance checkpoint operation is invalid")
        try:
            operation_id = str(uuid.UUID(str(value.get("operation_id"))))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ComponentInstallLockError("maintenance checkpoint operation identity is invalid") from exc
        if value.get("operation_id") != operation_id:
            raise ComponentInstallLockError("maintenance checkpoint operation identity is invalid")
        if (operation.get("kind") != "database_rebuild" or operation.get("status") not in {
                "pending", "candidate_ready", "source_quiesced", "published", "verified"}):
            raise ComponentInstallLockError("maintenance checkpoint operation is invalid")
        backup = operation.get("backup")
        if backup is None:
            if operation.get("status") != "pending":
                raise ComponentInstallLockError("maintenance phase has no pinned backup")
        else:
            if not isinstance(backup, dict) or set(backup) != {"filename", "sha256"}:
                raise ComponentInstallLockError("maintenance backup pin is invalid")
            filename = backup.get("filename")
            digest = backup.get("sha256")
            prefix = f"fwrouter.db.rebuild-{uuid.UUID(operation_id).hex}."
            if (not isinstance(filename, str) or filename != Path(filename).name
                    or not filename.startswith(prefix) or not filename.endswith(".bak")
                    or not isinstance(digest, str) or len(digest) != 64
                    or any(ch not in "0123456789abcdef" for ch in digest)):
                raise ComponentInstallLockError("maintenance backup pin is invalid")
    return value


def _ensure_maintenance_directory() -> None:
    """Create only the fixed root-owned lease directory chain when absent."""
    current = Path("/")
    trusted_uid = _trusted_uid()
    for part in MAINTENANCE_LOCK_PATH.parent.parts[1:]:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            current.mkdir(mode=0o700)
            if current == MAINTENANCE_LOCK_PATH.parent:
                os.chmod(current, 0o700, follow_symlinks=False)
            info = current.lstat()
        if (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)
                or info.st_uid not in {0, trusted_uid}
                or (current == MAINTENANCE_LOCK_PATH.parent
                    and stat.S_IMODE(info.st_mode) != 0o700)
                or (info.st_mode & UNSAFE_WRITE
                    and not (info.st_uid == 0 and info.st_mode & stat.S_ISVTX))):
            raise ComponentInstallLockError("maintenance lease parent is unsafe")


def _open_maintenance_lease(*, create: bool = False) -> int:
    directory = MAINTENANCE_LOCK_PATH.parent
    if create:
        _ensure_maintenance_directory()
    checkpoint = _read_maintenance_checkpoint()
    if checkpoint is not None:
        operation = checkpoint.get("maintenance_operation")
        if operation is not None:
            if operation.get("status") != "verified":
                raise ComponentInstallLockError("offline database rebuild requires forward completion")
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(MAINTENANCE_LOCK_PATH, flags | (os.O_CREAT if create else 0), 0o600)
    except OSError as exc:
        raise ComponentInstallLockError("maintenance lease is unavailable") from exc
    info = os.fstat(fd)
    try:
        named = MAINTENANCE_LOCK_PATH.lstat()
        parent = directory.lstat()
    except OSError as exc:
        os.close(fd)
        raise ComponentInstallLockError("maintenance lease path changed") from exc
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != _trusted_uid() or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1 or stat.S_ISLNK(named.st_mode)
            or (info.st_dev, info.st_ino) != (named.st_dev, named.st_ino)
            or not stat.S_ISDIR(parent.st_mode) or stat.S_ISLNK(parent.st_mode)
            or parent.st_uid != _trusted_uid() or stat.S_IMODE(parent.st_mode) != 0o700):
        os.close(fd)
        raise ComponentInstallLockError("maintenance lease has unsafe owner or path")
    return fd


def _acquire_maintenance_lease(parent: Path, mode: str) -> int | None:
    if not _uses_host_maintenance_lease(parent):
        return None
    if mode not in {"shared", "exclusive"}:
        raise ComponentInstallLockError("maintenance lease mode is invalid")
    try:
        fd = _open_maintenance_lease(create=True)
        lock_mode = fcntl.LOCK_EX if mode == "exclusive" else fcntl.LOCK_SH
        fcntl.flock(fd, lock_mode | fcntl.LOCK_NB)
        if mode == "exclusive":
            _require_backend_units_quiescent()
        return fd
    except (OSError, ComponentInstallLockError) as exc:
        if "fd" in locals():
            os.close(fd)
        if isinstance(exc, ComponentInstallLockError):
            raise
        raise ComponentInstallLockError("maintenance operation excludes installer") from exc


def _verify_maintenance_lease(parent: Path, mode: str | None) -> None:
    if not _uses_host_maintenance_lease(parent):
        return
    try:
        expected = _open_maintenance_lease()
        try:
            descriptor = os.fstat(MAINTENANCE_LEASE_FD)
            named = os.fstat(expected)
            if ((descriptor.st_dev, descriptor.st_ino) != (named.st_dev, named.st_ino)
                    or descriptor.st_uid != _trusted_uid() or stat.S_IMODE(descriptor.st_mode) != 0o600
                    or descriptor.st_nlink != 1):
                raise ComponentInstallLockError("inherited maintenance lease descriptor is invalid")
            if mode not in {"shared", "exclusive"}:
                raise ComponentInstallLockError("installer maintenance lease mode is invalid")
            _verify_maintenance_lock_owner(MAINTENANCE_LEASE_FD, mode)
            if mode == "exclusive":
                _require_backend_units_quiescent()
            checkpoint = _read_maintenance_checkpoint()
            operation = checkpoint.get("maintenance_operation") if checkpoint else None
            if (isinstance(operation, dict) and operation.get("kind") == "database_rebuild"
                    and operation.get("status") != "verified"):
                raise ComponentInstallLockError("offline database rebuild requires forward completion")
        finally:
            os.close(expected)
    except OSError as exc:
        raise ComponentInstallLockError("backend/UI installer lacks the shared maintenance lease") from exc


def _validate_parent(parent: Path, *, create: bool) -> Path:
    if not parent.is_absolute():
        raise ComponentInstallLockError("component lock parent must be absolute")
    if ".." in parent.parts:
        raise ComponentInstallLockError("component lock parent must not contain traversal components")
    cursor = Path(parent.anchor)
    for part in parent.parts[1:]:
        cursor = cursor / part
        try:
            info = cursor.lstat()
        except FileNotFoundError as exc:
            if not create:
                raise ComponentInstallLockError("component lock parent does not exist") from exc
            try:
                cursor.mkdir(mode=0o755)
            except OSError as create_exc:
                raise ComponentInstallLockError("cannot create component lock parent") from create_exc
            info = cursor.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ComponentInstallLockError("component lock parent contains a symlink or non-directory")
        if cursor == parent:
            if info.st_uid != os.geteuid() or info.st_mode & UNSAFE_WRITE:
                raise ComponentInstallLockError("component lock parent has unsafe owner or permissions")
        else:
            if info.st_uid not in {0, os.geteuid()}:
                raise ComponentInstallLockError("component lock path crosses an unexpected owner")
            if info.st_mode & UNSAFE_WRITE and not info.st_mode & stat.S_ISVTX:
                raise ComponentInstallLockError("component lock path crosses an unsafe writable ancestor")
    return parent / LOCK_NAME


def _open_lock(parent: Path, *, create_parent: bool) -> tuple[int, Path]:
    lock_path = _validate_parent(parent, create=create_parent)
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise ComponentInstallLockError("cannot safely open component installer lock") from exc
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
        os.close(fd)
        raise ComponentInstallLockError("component installer lock has unsafe type, owner, mode or links")
    try:
        path_info = lock_path.lstat()
    except OSError as exc:
        os.close(fd)
        raise ComponentInstallLockError("component installer lock path changed") from exc
    if (stat.S_ISLNK(path_info.st_mode) or (path_info.st_dev, path_info.st_ino) !=
            (info.st_dev, info.st_ino)):
        os.close(fd)
        raise ComponentInstallLockError("component installer lock path changed")
    return fd, lock_path


def verify_inherited_lock(parent: Path, fd: int, maintenance_mode: str | None = None) -> int:
    """Verify and retain the lock FD passed by the process-level lock runner."""
    if fd != LOCK_FD:
        raise ComponentInstallLockError(f"installer lock must be inherited as FD {LOCK_FD}")
    _verify_maintenance_lease(parent, maintenance_mode)
    expected = _validate_parent(parent, create=False)  # type: ignore[arg-type]
    try:
        info = os.fstat(fd)
        path_info = expected.lstat()
    except OSError as exc:
        raise ComponentInstallLockError("inherited installer lock is unavailable") from exc
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
            or stat.S_ISLNK(path_info.st_mode)
            or (info.st_dev, info.st_ino) != (path_info.st_dev, path_info.st_ino)):
        raise ComponentInstallLockError("inherited installer lock does not match the trusted target lock")
    try:
        # For the same inherited open-file description this is idempotent. If
        # another process owns a distinct lock description, it fails closed.
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        raise ComponentInstallLockError("another installer owns the component lock") from exc
    return fd


def acquire(parent: Path, *, maintenance_mode: str = "shared") -> tuple[int, bool]:
    fd, _ = _open_lock(parent, create_parent=False)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        maintenance_fd = _acquire_maintenance_lease(parent, maintenance_mode)
        if maintenance_fd is not None:
            _OWNED_MAINTENANCE_FDS[fd] = maintenance_fd
    except OSError as exc:
        os.close(fd)
        raise ComponentInstallLockError("cannot acquire component installer lock") from exc
    except ComponentInstallLockError:
        os.close(fd)
        raise
    return fd, True


def release(fd: int) -> None:
    maintenance_fd = _OWNED_MAINTENANCE_FDS.pop(fd, None)
    if maintenance_fd is not None:
        os.close(maintenance_fd)
    os.close(fd)


def _run(parent: Path, command: list[str]) -> int:
    if not command:
        raise ComponentInstallLockError("missing installer command")
    fd, _ = _open_lock(parent, create_parent=True)
    maintenance_fd: int | None = None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        mode = _maintenance_mode(command)
        maintenance_fd = _acquire_maintenance_lease(parent, mode)
        if maintenance_fd is not None:
            if maintenance_fd != MAINTENANCE_LEASE_FD:
                os.dup2(maintenance_fd, MAINTENANCE_LEASE_FD, inheritable=True)
                os.close(maintenance_fd)
                maintenance_fd = MAINTENANCE_LEASE_FD
            else:
                os.set_inheritable(maintenance_fd, True)
        if fd != LOCK_FD:
            os.dup2(fd, LOCK_FD, inheritable=True)
            os.close(fd)
            fd = LOCK_FD
        else:
            os.set_inheritable(fd, True)
        os.execv(command[0], [*command, "--_component-lock-fd", str(LOCK_FD),
                              "--_maintenance-lock-mode", mode])
    except OSError as exc:
        if fd >= 0:
            os.close(fd)
        if maintenance_fd is not None and maintenance_fd != fd:
            os.close(maintenance_fd)
        raise ComponentInstallLockError("cannot exec installer while holding component lock") from exc
    except ComponentInstallLockError:
        if fd >= 0:
            os.close(fd)
        if maintenance_fd is not None and maintenance_fd != fd:
            os.close(maintenance_fd)
        raise
    return 1


def _maintenance_mode(command: list[str]) -> str:
    """Backend and selective host publication are exclusive; UI is shared."""
    if not any(Path(value).name == "install.sh" for value in command):
        raise ComponentInstallLockError("host maintenance lease supports only install.sh")
    components = {
        command[index + 1]
        for index, value in enumerate(command[:-1])
        if value == "--component"
    }
    if "--host-plan" in command:
        if "--all" in command or components != {"host"}:
            raise ComponentInstallLockError("selective host publication requires host-only install")
        return "exclusive"
    return "exclusive" if "--all" in command or {"all", "backend"} & components else "shared"


def _verify_maintenance_lock_owner(fd: int, mode: str) -> None:
    """Require the FD itself to reference a flock of sufficient strength."""
    if mode not in {"shared", "exclusive"}:
        raise ComponentInstallLockError("maintenance lease mode is invalid")
    info = os.fstat(fd)
    device = f"{os.major(info.st_dev):02x}:{os.minor(info.st_dev):02x}"
    try:
        rows = Path(f"/proc/self/fdinfo/{fd}").read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ComponentInstallLockError("cannot verify inherited maintenance lease descriptor") from exc
    fdinfo_inode = next((row.split()[1] for row in rows
                         if len(row.split()) == 2 and row.split()[0] == "ino:"), None)
    if fdinfo_inode != str(info.st_ino):
        raise ComponentInstallLockError("inherited maintenance lease descriptor identity changed")
    for row in rows:
        fields = row.split()
        if (len(fields) == 9 and fields[0] == "lock:" and fields[2] == "FLOCK"
                and fields[3] == "ADVISORY" and fields[4] in {"READ", "WRITE"}
                and fields[6] == f"{device}:{info.st_ino}"
                and fields[7:] == ["0", "EOF"]):
            if fields[4] == "WRITE" or mode == "shared":
                return
    raise ComponentInstallLockError("inherited maintenance lease descriptor lacks the requested lock mode")


def _require_backend_units_quiescent() -> None:
    """Reuse the lifecycle owner's allowlisted unit inventory under the EX lease."""
    path = Path(__file__).resolve().parents[1] / "backend" / "fwrouter_api" / "services" / "maintenance_lifecycle_admission.py"
    spec = importlib.util.spec_from_file_location("_fwrouter_installer_lifecycle_admission", path)
    if spec is None or spec.loader is None:
        raise ComponentInstallLockError("cannot load FWRouter lifecycle unit inventory")
    admission = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(admission)
        admission._require_units_stopped()
    except Exception as exc:
        raise ComponentInstallLockError("FWRouter backend writer units are not quiescent") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-parent", type=Path)
    parser.add_argument("--verify-parent", type=Path)
    parser.add_argument("--fd", type=int, default=LOCK_FD)
    parser.add_argument("--mode", choices=("shared", "exclusive"))
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    try:
        if args.run_parent is not None:
            command = args.command[1:] if args.command[:1] == ["--"] else args.command
            return _run(args.run_parent, command)
        if args.verify_parent is not None:
            verify_inherited_lock(args.verify_parent, args.fd, args.mode)
            return 0
        raise ComponentInstallLockError("select --run-parent or --verify-parent")
    except ComponentInstallLockError as exc:
        print(f"component-install-lock: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
