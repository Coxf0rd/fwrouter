#!/usr/bin/env python3
"""Serialize cooperating FWRouter backend and UI installer operations."""
from __future__ import annotations

import argparse
import fcntl
import os
from pathlib import Path
import stat
import sys


class ComponentInstallLockError(RuntimeError):
    pass


LOCK_NAME = ".fwrouter-component-install.lock"
LOCK_FD = 9
UNSAFE_WRITE = stat.S_IWGRP | stat.S_IWOTH


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


def verify_inherited_lock(parent: Path, fd: int) -> int:
    """Verify and retain the lock FD passed by the process-level lock runner."""
    if fd != LOCK_FD:
        raise ComponentInstallLockError(f"installer lock must be inherited as FD {LOCK_FD}")
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


def acquire(parent: Path) -> tuple[int, bool]:
    fd, _ = _open_lock(parent, create_parent=False)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
    except OSError as exc:
        os.close(fd)
        raise ComponentInstallLockError("cannot acquire component installer lock") from exc
    return fd, True


def _run(parent: Path, command: list[str]) -> int:
    if not command:
        raise ComponentInstallLockError("missing installer command")
    fd, _ = _open_lock(parent, create_parent=True)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        if fd != LOCK_FD:
            os.dup2(fd, LOCK_FD, inheritable=True)
            os.close(fd)
            fd = LOCK_FD
        else:
            os.set_inheritable(fd, True)
        os.execv(command[0], [*command, "--_component-lock-fd", str(LOCK_FD)])
    except OSError as exc:
        if fd >= 0:
            os.close(fd)
        raise ComponentInstallLockError("cannot exec installer while holding component lock") from exc
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-parent", type=Path)
    parser.add_argument("--verify-parent", type=Path)
    parser.add_argument("--fd", type=int, default=LOCK_FD)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    try:
        if args.run_parent is not None:
            command = args.command[1:] if args.command[:1] == ["--"] else args.command
            return _run(args.run_parent, command)
        if args.verify_parent is not None:
            verify_inherited_lock(args.verify_parent, args.fd)
            return 0
        raise ComponentInstallLockError("select --run-parent or --verify-parent")
    except ComponentInstallLockError as exc:
        print(f"component-install-lock: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
