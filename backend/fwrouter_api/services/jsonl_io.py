from __future__ import annotations

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import re
import secrets
import stat
import time
from typing import Iterator


JSONL_LOCK_TIMEOUT_SECONDS = 5.0


class JsonlLeaseError(OSError):
    """A JSONL path could not be accessed under its required file lease."""


class JsonlLease:
    def __init__(self, directory_fd: int, parent: Path, basename: str, lock_name: str, lock_fd: int) -> None:
        self.directory_fd = directory_fd
        self.parent = parent
        self.basename = basename
        self.lock_name = lock_name
        self.lock_fd = lock_fd


def _directory_fd(parent: Path) -> int:
    if not parent.is_absolute():
        raise JsonlLeaseError("JSONL parent path must be absolute")
    if any(part in {".", ".."} for part in parent.parts):
        raise JsonlLeaseError("JSONL parent path contains traversal components")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    current_path = Path("/")
    try:
        components = parent.parts[1:]
        for index, part in enumerate(components):
            current_path = current_path / part
            try:
                next_fd = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=fd,
                )
            except FileNotFoundError:
                current = os.fstat(fd)
                if not stat.S_ISDIR(current.st_mode) or current.st_uid not in {0, os.geteuid()}:
                    raise JsonlLeaseError("cannot create JSONL directory beneath untrusted owner")
                sticky_tmp = (
                    current.st_uid == 0
                    and current_path.parent in {Path("/tmp"), Path("/var/tmp")}
                    and bool(current.st_mode & stat.S_ISVTX)
                )
                if current.st_mode & 0o022 and not sticky_tmp:
                    raise JsonlLeaseError("cannot create JSONL directory beneath writable ancestor")
                os.mkdir(part, 0o700, dir_fd=fd)
                next_fd = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=fd,
                )
            component_info = os.fstat(next_fd)
            if not stat.S_ISDIR(component_info.st_mode) or component_info.st_uid not in {0, os.geteuid()}:
                os.close(next_fd)
                raise JsonlLeaseError("JSONL path contains an untrusted directory")
            sticky_tmp = (
                component_info.st_uid == 0
                and current_path in {Path("/tmp"), Path("/var/tmp")}
                and bool(component_info.st_mode & stat.S_ISVTX)
            )
            if component_info.st_mode & 0o022 and not sticky_tmp:
                os.close(next_fd)
                raise JsonlLeaseError("JSONL path contains a group/world-writable directory")
            if index == len(components) - 1 and (
                component_info.st_uid != os.geteuid() or component_info.st_mode & 0o022
            ):
                os.close(next_fd)
                raise JsonlLeaseError("JSONL parent directory owner or permissions are unsafe")
            os.close(fd)
            fd = next_fd
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
            raise JsonlLeaseError("JSONL parent directory owner or type is unsafe")
        if info.st_mode & 0o022:
            raise JsonlLeaseError("JSONL parent directory is group/world writable")
        return fd
    except Exception:
        os.close(fd)
        raise


def _check_regular_private(fd: int, *, label: str, allow_private_tightening: bool = False) -> os.stat_result:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
        raise JsonlLeaseError(f"{label} owner, link count, or file type is unsafe")
    if not allow_private_tightening and info.st_mode & 0o077:
        raise JsonlLeaseError(f"{label} permissions are unsafe")
    return info


@contextmanager
def jsonl_lease(path: Path, *, timeout_seconds: float = JSONL_LOCK_TIMEOUT_SECONDS) -> Iterator[JsonlLease]:
    """Acquire the stable adjacent lock shared by appenders and file maintenance.

    Linux flock locks separate open-file descriptions, including those opened by
    threads in this process. Acquisition is bounded; callers must surface a busy
    or unsafe-path failure instead of proceeding without the lease.
    """
    candidate = Path(path)
    if candidate.suffix != ".jsonl" or candidate.name in {"", ".", ".."}:
        raise JsonlLeaseError("JSONL path must name a .jsonl file")
    directory_fd = _directory_fd(candidate.parent)
    lock_fd = -1
    try:
        lock_name = f".{candidate.name}.lock"
        lock_fd = os.open(
            _proc_path(directory_fd, lock_name),
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
        _check_regular_private(lock_fd, label="JSONL lease")
        lock_path_info = os.stat(lock_name, dir_fd=directory_fd, follow_symlinks=False)
        lock_fd_info = os.fstat(lock_fd)
        if (not stat.S_ISREG(lock_path_info.st_mode)
                or (lock_path_info.st_dev, lock_path_info.st_ino)
                != (lock_fd_info.st_dev, lock_fd_info.st_ino)):
            raise JsonlLeaseError("JSONL lease path changed during acquisition")

        deadline = time.monotonic() + max(0.0, timeout_seconds)
        while True:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Timed out acquiring JSONL lease for {candidate.name}")
                time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))

        # Recheck after acquiring the lock so a replaced lease path fails closed.
        locked_info = os.fstat(lock_fd)
        named_info = os.stat(lock_name, dir_fd=directory_fd, follow_symlinks=False)
        if ((locked_info.st_dev, locked_info.st_ino) != (named_info.st_dev, named_info.st_ino)
                or locked_info.st_uid != os.geteuid() or locked_info.st_mode & 0o077):
            raise JsonlLeaseError("JSONL lease was substituted or became unsafe")
        yield JsonlLease(directory_fd, candidate.parent, candidate.name, lock_name, lock_fd)
    finally:
        if lock_fd >= 0:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)
        os.close(directory_fd)


def _target_info(lease: JsonlLease) -> os.stat_result | None:
    try:
        _validate_parent(lease)
        info = os.stat(lease.basename, dir_fd=lease.directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
        raise JsonlLeaseError("JSONL target owner, link count, or file type is unsafe")
    return info


def open_jsonl_read(lease: JsonlLease, *, errors: str = "strict"):
    info = _target_info(lease)
    if info is None:
        raise FileNotFoundError(lease.basename)
    fd = os.open(
        _proc_path(lease.directory_fd, lease.basename),
        os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
    )
    opened = os.fstat(fd)
    if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
        os.close(fd)
        raise JsonlLeaseError("JSONL target changed while opening")
    return os.fdopen(fd, "r", encoding="utf-8", errors=errors)


def append_jsonl(path: Path, text: str) -> None:
    with jsonl_lease(path) as lease:
        info = _target_info(lease)
        fd = os.open(
            _proc_path(lease.directory_fd, lease.basename),
            os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
        try:
            opened = _check_regular_private(fd, label="JSONL target", allow_private_tightening=True)
            if info is not None and (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                raise JsonlLeaseError("JSONL target changed while opening for append")
            # Existing files created before the private mode contract are tightened
            # while holding the same lease, rather than retaining public access.
            named = _target_info(lease)
            if named is None or (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino):
                raise JsonlLeaseError("JSONL target changed before permission tightening")
            if opened.st_mode & 0o077:
                os.chmod(_proc_path(lease.directory_fd, lease.basename), 0o600, follow_symlinks=False)
            payload = text.encode("utf-8")
            offset = 0
            while offset < len(payload):
                written = os.write(fd, payload[offset:])
                if written <= 0:
                    raise OSError("JSONL append made no forward progress")
                offset += written
        finally:
            os.close(fd)


def replace_jsonl(lease: JsonlLease, lines: Iterator[str], *, expected: os.stat_result) -> None:
    """Publish a private replacement while the caller owns the per-file lease."""
    current = _target_info(lease)
    if current is None or (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns,
                           current.st_mode) != (
        expected.st_dev, expected.st_ino, expected.st_size, expected.st_mtime_ns, expected.st_mode
    ):
        raise JsonlLeaseError("JSONL target changed before replacement")

    temporary = f".{lease.basename}.rewrite-{os.getpid()}-{secrets.token_hex(8)}.tmp"
    fd = os.open(
        _proc_path(lease.directory_fd, temporary),
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
    )
    published = False
    try:
        _check_regular_private(fd, label="JSONL temporary")
        for line in lines:
            _write_all(fd, line.encode("utf-8"))
        os.fsync(fd)
        current = _target_info(lease)
        if current is None or (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns,
                               current.st_mode) != (
            expected.st_dev, expected.st_ino, expected.st_size, expected.st_mtime_ns, expected.st_mode
        ):
            raise JsonlLeaseError("JSONL target changed during replacement")
        _validate_parent(lease)
        os.replace(temporary, lease.basename, src_dir_fd=lease.directory_fd,
                   dst_dir_fd=lease.directory_fd)
        os.fsync(lease.directory_fd)
        published = True
    finally:
        os.close(fd)
        if not published:
            try:
                os.unlink(temporary, dir_fd=lease.directory_fd)
            except FileNotFoundError:
                pass


def remove_jsonl_temp(lease: JsonlLease, temporary_name: str) -> None:
    legacy_name = f"{lease.basename[:-6]}.tmp"
    if (not temporary_name.endswith(".tmp")
            or (temporary_name != legacy_name
                and not temporary_name.startswith(f".{lease.basename}."))):
        raise JsonlLeaseError("unrecognized JSONL temporary name")
    _validate_parent(lease)
    _validate_entry_name(lease, temporary_name)
    info = os.stat(temporary_name, dir_fd=lease.directory_fd, follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
        raise JsonlLeaseError("JSONL temporary owner, link count, or file type is unsafe")
    os.unlink(temporary_name, dir_fd=lease.directory_fd)


def _validate_entry_name(lease: JsonlLease, name: str) -> None:
    if (not name or name in {".", ".."} or Path(name).name != name
            or "/" in name or "\\" in name):
        raise JsonlLeaseError("JSONL entry name is unsafe")
    if name in {lease.basename, lease.lock_name}:
        return
    legacy_name = f"{lease.basename[:-6]}.tmp"
    generated = re.fullmatch(
        re.escape(f".{lease.basename}.rewrite-") + r"[0-9]+-[0-9a-f]{16}\.tmp", name
    )
    old_scrub = re.fullmatch(
        re.escape(f".{lease.basename}.") + r"[A-Za-z0-9_]{8}\.scrub\.tmp", name
    )
    if name != legacy_name and generated is None and old_scrub is None:
        raise JsonlLeaseError("unrecognized JSONL entry name")


def _proc_path(directory_fd: int, name: str) -> Path:
    if (not name or name in {".", ".."} or Path(name).name != name
            or "/" in name or "\\" in name):
        raise JsonlLeaseError("JSONL entry name is unsafe")
    proc_path = Path(f"/proc/self/fd/{directory_fd}/{name}")
    try:
        proc_path.parent.stat()
    except OSError as exc:
        raise JsonlLeaseError("procfs descriptor aliases are required for safe JSONL access") from exc
    return proc_path


def _validate_parent(lease: JsonlLease) -> None:
    try:
        resolved = lease.parent.resolve(strict=True)
        named = lease.parent.lstat()
    except OSError as exc:
        raise JsonlLeaseError("JSONL parent path is unavailable") from exc
    opened = os.fstat(lease.directory_fd)
    if (resolved != lease.parent or not stat.S_ISDIR(named.st_mode)
            or named.st_uid != os.geteuid() or named.st_mode & 0o022
            or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)):
        raise JsonlLeaseError("JSONL parent path was substituted")
    lock_opened = os.fstat(lease.lock_fd)
    lock_named = os.stat(lease.lock_name, dir_fd=lease.directory_fd, follow_symlinks=False)
    if (not stat.S_ISREG(lock_named.st_mode) or lock_named.st_nlink != 1 or lock_opened.st_uid != os.geteuid()
            or lock_opened.st_mode & 0o077
            or (lock_opened.st_dev, lock_opened.st_ino) != (lock_named.st_dev, lock_named.st_ino)):
        raise JsonlLeaseError("JSONL lease inode was substituted")
    _validate_entry_name(lease, lease.basename)


def _write_all(fd: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(fd, payload[offset:])
        if written <= 0:
            raise OSError("JSONL write made no forward progress")
        offset += written
