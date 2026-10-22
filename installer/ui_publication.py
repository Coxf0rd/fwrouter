#!/usr/bin/env python3
"""Publish the FWRouter UI through a validated immutable release pointer."""
from __future__ import annotations

import argparse
import ctypes
import fcntl
import hashlib
import os
from pathlib import Path, PurePosixPath
import signal
import stat
import sys
import uuid

import component_install_lock

RENAME_EXCHANGE = 2
UNSAFE_WRITE = stat.S_IWGRP | stat.S_IWOTH
MAX_UI_ENTRIES = 10_000
MAX_UI_FILE_BYTES = 512 * 1024 * 1024
MAX_UI_TOTAL_BYTES = 512 * 1024 * 1024


class PublicationError(RuntimeError):
    pass


def _identity(before: os.stat_result, after: os.stat_result) -> bool:
    return (before.st_dev, before.st_ino, before.st_mode, before.st_uid, before.st_size,
            before.st_mtime_ns, before.st_ctime_ns) == (
            after.st_dev, after.st_ino, after.st_mode, after.st_uid, after.st_size,
            after.st_mtime_ns, after.st_ctime_ns)


def _check_mode(path: str, info: os.stat_result, owner_uid: int, kind: str) -> None:
    if info.st_uid != owner_uid:
        raise PublicationError(f"{kind} has unexpected owner: {path}")
    if info.st_mode & UNSAFE_WRITE:
        raise PublicationError(f"{kind} is group/world writable: {path}")
    if stat.S_ISDIR(info.st_mode) and not info.st_mode & stat.S_IXOTH:
        raise PublicationError(f"{kind} is not traversable by the static-file server: {path}")
    if stat.S_ISREG(info.st_mode) and not info.st_mode & stat.S_IROTH:
        raise PublicationError(f"{kind} is not readable by the static-file server: {path}")


def _walk_tree(root: Path, *, owner_uid: int, copy_to: Path | None = None, require_index: bool = False):
    """Hash through pinned directory/file descriptors; optionally copy those exact bytes."""
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        root_before = os.fstat(root_fd)
        _check_mode(str(root), root_before, owner_uid, "UI directory")
    except BaseException:
        os.close(root_fd)
        raise
    records: list[tuple[str, int, int, str]] = []
    entries_seen = 0
    bytes_reserved = 0
    bytes_read = 0
    if copy_to is not None:
        copy_to.mkdir(mode=0o700)

    def walk(dir_fd: int, relative: PurePosixPath) -> None:
        nonlocal entries_seen, bytes_reserved, bytes_read
        before_dir = os.fstat(dir_fd)
        names = sorted(os.listdir(dir_fd))
        out_dir = copy_to / Path(relative.as_posix()) if copy_to is not None and relative.parts else copy_to
        for name in names:
            entries_seen += 1
            if entries_seen > MAX_UI_ENTRIES:
                raise PublicationError(f"UI source exceeds the {MAX_UI_ENTRIES}-entry limit")
            rel = relative / name if relative.parts else PurePosixPath(name)
            rel_text = rel.as_posix()
            before = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
            if stat.S_ISDIR(before.st_mode):
                _check_mode(rel_text, before, owner_uid, "UI directory")
                child_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)
                try:
                    opened = os.fstat(child_fd)
                    if not _identity(before, opened):
                        raise PublicationError(f"UI directory changed while opening: {rel_text}")
                    if copy_to is not None:
                        (out_dir / name).mkdir(mode=0o700)
                    walk(child_fd, rel)
                    after = os.fstat(child_fd)
                    current = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
                    if not _identity(opened, after) or not _identity(opened, current):
                        raise PublicationError(f"UI directory changed while reading: {rel_text}")
                    mode = stat.S_IMODE(opened.st_mode)
                    records.append((rel_text + "/", 0, mode, "directory"))
                    if copy_to is not None:
                        os.chmod(out_dir / name, mode)
                finally:
                    os.close(child_fd)
            elif stat.S_ISREG(before.st_mode):
                _check_mode(rel_text, before, owner_uid, "UI file")
                if before.st_size > MAX_UI_FILE_BYTES or bytes_reserved + before.st_size > MAX_UI_TOTAL_BYTES:
                    raise PublicationError("UI source exceeds the configured file or total size limit")
                bytes_reserved += before.st_size
                file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
                digest = hashlib.sha256()
                total = 0
                dst_fd = None
                try:
                    opened = os.fstat(file_fd)
                    if not _identity(before, opened):
                        raise PublicationError(f"UI file changed while opening: {rel_text}")
                    if copy_to is not None:
                        dst = out_dir / name
                        dst_fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                    while True:
                        chunk = os.read(file_fd, 1024 * 1024)
                        if not chunk:
                            break
                        if total + len(chunk) > before.st_size or total + len(chunk) > MAX_UI_FILE_BYTES:
                            raise PublicationError(f"UI file grew beyond its verified size: {rel_text}")
                        if bytes_read + len(chunk) > MAX_UI_TOTAL_BYTES:
                            raise PublicationError("UI source grew beyond the configured total size limit")
                        total += len(chunk)
                        bytes_read += len(chunk)
                        digest.update(chunk)
                        if dst_fd is not None:
                            view = memoryview(chunk)
                            while view:
                                written = os.write(dst_fd, view)
                                view = view[written:]
                    after = os.fstat(file_fd)
                    current = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
                    if not _identity(opened, after) or not _identity(opened, current) or total != opened.st_size:
                        raise PublicationError(f"UI file changed while reading: {rel_text}")
                    mode = stat.S_IMODE(opened.st_mode)
                    records.append((rel_text, total, mode, digest.hexdigest()))
                    if dst_fd is not None:
                        os.fchmod(dst_fd, mode)
                finally:
                    if dst_fd is not None:
                        os.close(dst_fd)
                    os.close(file_fd)
            else:
                raise PublicationError(f"UI contains a symlink or special file: {rel_text}")
        after_dir = os.fstat(dir_fd)
        if not _identity(before_dir, after_dir):
            raise PublicationError(f"UI directory changed while reading: {relative.as_posix() or '.'}")

    try:
        walk(root_fd, PurePosixPath())
        root_after = os.fstat(root_fd)
        if not _identity(root_before, root_after):
            raise PublicationError(f"UI root changed while reading: {root}")
    finally:
        os.close(root_fd)
    if require_index and not any(path == "index.html" for path, _, _, _ in records):
        raise PublicationError("UI source must contain a regular top-level index.html")
    root_mode = stat.S_IMODE(root_before.st_mode)
    records.append(("./", 0, root_mode, "directory"))
    records.sort()
    if copy_to is not None:
        for base, _, _ in os.walk(copy_to, topdown=False, followlinks=False):
            rel = Path(base).relative_to(copy_to)
            source_mode = root_mode if str(rel) == "." else next(
                mode for path, _, mode, _ in records if path == rel.as_posix() + "/"
            )
            os.chmod(base, source_mode)
    encoded = "".join(f"{path}\0{size}\0{mode:o}\0{digest}\n" for path, size, mode, digest in records).encode()
    return hashlib.sha256(encoded).hexdigest(), records


def _fsync_tree(root: Path) -> None:
    directories = []
    for base, _, files in os.walk(root, followlinks=False):
        base_path = Path(base)
        directories.append(base_path)
        for name in files:
            fd = os.open(base_path / name, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    for path in reversed(directories):
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _exchange(left: Path, right: Path) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise PublicationError("renameat2(RENAME_EXCHANGE) is unavailable; UI publication refused")
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    if renameat2(-100, os.fsencode(left), -100, os.fsencode(right), RENAME_EXCHANGE) != 0:
        error = ctypes.get_errno()
        raise PublicationError(f"atomic UI exchange failed: {os.strerror(error)}")


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _validate_ancestors(path: Path, owner_uid: int) -> None:
    if not path.is_absolute() or ".." in path.parts:
        raise PublicationError(f"path must be absolute and contain no parent traversal: {path}")
    absolute = Path(os.path.abspath(path))
    for ancestor in reversed((absolute, *absolute.parents)):
        info = ancestor.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise PublicationError(f"target ancestor is not a real directory: {ancestor}")
        if info.st_uid not in {0, owner_uid}:
            raise PublicationError(f"target ancestor has unexpected owner: {ancestor}")
        if info.st_mode & UNSAFE_WRITE and not (info.st_mode & stat.S_ISVTX):
            raise PublicationError(f"target ancestor is group/world writable without sticky bit: {ancestor}")


def _managed_release(target: Path, releases: Path, owner_uid: int) -> Path | None:
    if not target.is_symlink():
        return None
    raw = os.readlink(target)
    link = PurePosixPath(raw)
    if link.is_absolute() or len(link.parts) != 2 or link.parts[0] != releases.name:
        raise PublicationError("existing UI symlink is not a direct relative managed-release pointer")
    name = link.parts[1]
    if len(name) != 72 or not name.startswith("release-") or any(c not in "0123456789abcdef" for c in name[8:]):
        raise PublicationError("existing UI symlink has an invalid release identity")
    root_info = releases.lstat()
    if not stat.S_ISDIR(root_info.st_mode) or root_info.st_uid != owner_uid or root_info.st_mode & UNSAFE_WRITE:
        raise PublicationError("managed release directory is unsafe")
    resolved = releases / name
    info = resolved.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise PublicationError("existing UI symlink target is not a real managed directory")
    _check_mode(str(resolved), info, owner_uid, "UI release")
    return resolved


def _backend_compatibility(lifecycle, backend_source: Path, backend_installed: Path) -> str:
    try:
        expected = lifecycle._manifest_digest(lifecycle._backend_manifest(backend_source))
        installed = lifecycle._manifest_digest(lifecycle._backend_manifest(backend_installed))
    except Exception as exc:
        raise PublicationError(f"cannot verify installed backend compatibility: {exc}") from exc
    if expected != installed:
        raise PublicationError("installed backend does not match the trusted source UI compatibility manifest")
    return expected


def _release_digest(release: Path, owner_uid: int) -> str:
    info = release.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise PublicationError("managed UI release is not a real directory")
    _check_mode(str(release), info, owner_uid, "UI release")
    digest, _ = _walk_tree(release, owner_uid=owner_uid, require_index=True)
    if release.name != f"release-{digest}":
        raise PublicationError("managed UI release name does not match its content digest")
    return digest


def _recover_interrupted_exchanges(target: Path, releases: Path, owner_uid: int) -> None:
    """Finish archiving whole old trees left after a process died just after exchange."""
    for orphan in sorted(target.parent.glob(".fwrouter-ui-next-*")):
        suffix = orphan.name.removeprefix(".fwrouter-ui-next-")
        if len(suffix) != 32 or any(c not in "0123456789abcdef" for c in suffix):
            raise PublicationError(f"unexpected UI exchange recovery path: {orphan.name}")
        info = orphan.lstat()
        if stat.S_ISLNK(info.st_mode):
            # A managed symlink was the previous active pointer; its release remains immutable.
            release = _managed_release(orphan, releases, owner_uid)
            if release is None:
                raise PublicationError("interrupted UI pointer is not a managed release")
            _release_digest(release, owner_uid)
            orphan.unlink()
            _fsync_dir(target.parent)
            continue
        if not stat.S_ISDIR(info.st_mode):
            raise PublicationError(f"unexpected object at interrupted UI exchange path: {orphan.name}")
        digest, _ = _walk_tree(orphan, owner_uid=owner_uid, require_index=True)
        destination = releases / f"release-{digest}"
        try:
            existing_info = destination.lstat()
        except FileNotFoundError:
            os.rename(orphan, destination)
            _fsync_dir(target.parent)
            _fsync_dir(releases)
            continue
        _check_mode(str(destination), existing_info, owner_uid, "UI release")
        existing_digest = _release_digest(destination, owner_uid)
        if existing_digest != digest:
            raise PublicationError("interrupted UI tree conflicts with an existing content-addressed release")
        # Keep the duplicate whole tree in place; the verified managed release is its recovery copy.


def _signal_as_exception(signum, _frame):
    raise InterruptedError(f"received signal {signum}")


def _publish_under_component_lock(source: Path, target: Path, backend_source: Path,
                                  backend_installed: Path, *, require_root: bool = False,
                                  restore_release_sha256: str | None = None) -> str:
    import importlib.util
    lifecycle_path = Path(__file__).with_name("controlled_backend_lifecycle.py")
    spec = importlib.util.spec_from_file_location("fwrouter_backend_manifest", lifecycle_path)
    if spec is None or spec.loader is None:
        raise PublicationError("cannot load backend compatibility manifest")
    lifecycle = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lifecycle)

    owner_uid = os.geteuid()
    if require_root and owner_uid != 0:
        raise PublicationError("live UI publication requires effective UID 0")
    _validate_ancestors(source, owner_uid)
    _validate_ancestors(backend_source, owner_uid)
    _validate_ancestors(backend_installed, owner_uid)
    _validate_ancestors(target.parent, owner_uid)
    source_info = source.lstat()
    if not stat.S_ISDIR(source_info.st_mode):
        raise PublicationError("UI source must be a real directory")
    if require_root and source_info.st_uid != 0:
        raise PublicationError("live UI source must be owned by root")
    source_uid = source_info.st_uid
    source_digest, _ = _walk_tree(source, owner_uid=source_uid, require_index=True)
    if restore_release_sha256 is not None:
        if (len(restore_release_sha256) != 64
                or any(c not in "0123456789abcdef" for c in restore_release_sha256)):
            raise PublicationError("restore release must be identified by a lowercase SHA-256 digest")
        if source_digest != restore_release_sha256:
            raise PublicationError("trusted source UI digest does not match the requested retained release")
    lock_path = target.parent / ".fwrouter-ui.lock"
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        os.fchmod(fd, 0o600)
    except FileExistsError:
        fd = os.open(lock_path, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != owner_uid or stat.S_IMODE(info.st_mode) != 0o600:
            os.close(fd)
            raise PublicationError("existing UI publication lock is unsafe")
    old_term = signal.signal(signal.SIGTERM, _signal_as_exception)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        _backend_compatibility(lifecycle, backend_source, backend_installed)
        releases = target.parent / ".fwrouter-ui-releases"
        try:
            releases.mkdir(mode=0o755)
        except FileExistsError:
            pass
        release_info = releases.lstat()
        if not stat.S_ISDIR(release_info.st_mode):
            raise PublicationError("managed UI release path is not a directory")
        _check_mode(str(releases), release_info, owner_uid, "managed UI release directory")
        _fsync_dir(target.parent)

        _recover_interrupted_exchanges(target, releases, owner_uid)

        current = _managed_release(target, releases, owner_uid)
        if current is not None:
            _release_digest(current, owner_uid)
        initial_digest: str | None = None
        if current is None and target.exists():
            target_info = target.lstat()
            if not stat.S_ISDIR(target_info.st_mode):
                raise PublicationError("initial UI target must be a real directory")
            _check_mode(str(target), target_info, owner_uid, "initial UI directory")
            initial_digest, _ = _walk_tree(target, owner_uid=owner_uid, require_index=True)
            existing_archive = releases / f"release-{initial_digest}"
            try:
                existing_archive.lstat()
            except FileNotFoundError:
                pass
            else:
                try:
                    archived_digest = _release_digest(existing_archive, owner_uid)
                except PublicationError as exc:
                    raise PublicationError("initial UI conflicts with an existing content-addressed release") from exc
                if archived_digest != initial_digest:
                    raise PublicationError("initial UI conflicts with an existing content-addressed release")
        if restore_release_sha256 is not None:
            release = releases / f"release-{restore_release_sha256}"
            if not release.exists():
                raise PublicationError("requested retained UI release does not exist")
            if _release_digest(release, owner_uid) != restore_release_sha256:
                raise PublicationError("requested retained UI release failed content verification")
            if current == release:
                return f"UI release already active: {restore_release_sha256}"
        else:
            release = releases / f"release-{source_digest}"
        if current is not None:
            active_digest, _ = _walk_tree(current, owner_uid=owner_uid)
            if active_digest == source_digest and restore_release_sha256 is None:
                return f"UI already current: {source_digest}"

        if restore_release_sha256 is None:
            release = releases / f"release-{source_digest}"
            try:
                existing = release.lstat()
            except FileNotFoundError:
                existing = None
            if existing is not None:
                _check_mode(str(release), existing, owner_uid, "UI release")
                if _release_digest(release, owner_uid) != source_digest:
                    raise PublicationError("existing immutable release digest does not match its contents")
            else:
                staging = releases / f".staging-{uuid.uuid4().hex}"
                try:
                    copied_digest, _ = _walk_tree(source, owner_uid=source_uid, copy_to=staging, require_index=True)
                    _fsync_tree(staging)
                    prepared_digest, _ = _walk_tree(staging, owner_uid=owner_uid, require_index=True)
                    after_source_digest, _ = _walk_tree(source, owner_uid=source_uid, require_index=True)
                    if copied_digest != source_digest or prepared_digest != source_digest or after_source_digest != source_digest:
                        raise PublicationError("source or prepared UI changed during release construction")
                    os.rename(staging, release)
                    _fsync_dir(releases)
                except BaseException:
                    # SIGKILL can leave a private .staging directory; it is never an active release.
                    import shutil
                    shutil.rmtree(staging, ignore_errors=True)
                    raise

        # Backend may have changed while the release was prepared; check again under the lock.
        _backend_compatibility(lifecycle, backend_source, backend_installed)
        pointer = target.parent / f".fwrouter-ui-next-{uuid.uuid4().hex}"
        os.symlink(f"{releases.name}/{release.name}", pointer)
        _fsync_dir(target.parent)
        if target.exists() or target.is_symlink():
            _exchange(target, pointer)
            _fsync_dir(target.parent)
            old = pointer
            if old.is_dir() and not old.is_symlink():
                old_digest, _ = _walk_tree(old, owner_uid=owner_uid, require_index=True)
                if initial_digest is not None and old_digest != initial_digest:
                    raise PublicationError("initial UI changed between preflight and atomic exchange")
                archived = releases / f"release-{old_digest}"
                try:
                    archived_info = archived.lstat()
                except FileNotFoundError:
                    archived_info = None
                if archived_info is None:
                    os.rename(old, archived)
                    _fsync_dir(target.parent)
                    _fsync_dir(releases)
                elif _release_digest(archived, owner_uid) != old_digest:
                    raise PublicationError("previous UI conflicts with an existing content-addressed release")
                # If an identical archive already exists, preserve the exchanged directory as an orphan.
            else:
                pointer.unlink()
                _fsync_dir(target.parent)
        else:
            os.rename(pointer, target)
            _fsync_dir(target.parent)

        active = _managed_release(target, releases, owner_uid)
        if active != release:
            raise PublicationError("published UI pointer failed postflight validation")
        active_digest, _ = _walk_tree(active, owner_uid=owner_uid, require_index=True)
        if active_digest != source_digest:
            raise PublicationError("published UI content failed postflight digest validation")
        if restore_release_sha256 is not None:
            return f"Restored UI release {restore_release_sha256}"
        return f"Published UI release {source_digest}"
    finally:
        signal.signal(signal.SIGTERM, old_term)
        os.close(fd)


def publish(source: Path, target: Path, backend_source: Path, backend_installed: Path, *,
            require_root: bool = False, restore_release_sha256: str | None = None,
            installer_lock_fd: int | None = None) -> str:
    """Serialize direct publication with installer backend/UI operations."""
    owned_fd = False
    try:
        if installer_lock_fd is None:
            lock_fd, owned_fd = component_install_lock.acquire(target.parent)
        else:
            lock_fd = component_install_lock.verify_inherited_lock(target.parent, installer_lock_fd)
    except component_install_lock.ComponentInstallLockError as exc:
        raise PublicationError(str(exc)) from exc
    try:
        return _publish_under_component_lock(
            source, target, backend_source, backend_installed,
            require_root=require_root, restore_release_sha256=restore_release_sha256,
        )
    finally:
        if owned_fd:
            os.close(lock_fd)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--backend-source", type=Path, required=True)
    parser.add_argument("--backend-installed", type=Path, required=True)
    parser.add_argument("--require-root", action="store_true")
    parser.add_argument("--restore-release", help="atomically activate a retained release only when this trusted source UI has the same digest")
    parser.add_argument("--component-lock-fd", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        print(publish(args.source, args.target, args.backend_source, args.backend_installed,
                      require_root=args.require_root, restore_release_sha256=args.restore_release,
                      installer_lock_fd=args.component_lock_fd))
        return 0
    except Exception as exc:
        print(f"ui_publication: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
