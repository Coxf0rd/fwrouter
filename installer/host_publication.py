#!/usr/bin/env python3
"""CAS-based publication for the two approved selective host files."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import stat
import sys


SCHEMA = "fwrouter-host-publication-plan/v1"
MAX_PLAN_BYTES = 16 * 1024
MAX_SOURCE_BYTES = 1024 * 1024
FILES = (
    {
        "source": "host/libexec/fwrouter/fwrouter-boot-preflight.sh",
        "target": "usr/local/libexec/fwrouter/fwrouter-boot-preflight.sh",
        "mode": 0o755,
    },
    {
        "source": "host/systemd/fwrouter-api.service",
        "target": "etc/systemd/system/fwrouter-api.service",
        "mode": 0o644,
    },
)
SHA256 = re.compile(r"^[0-9a-f]{64}$")
O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)


class PublicationError(RuntimeError):
    pass


def _pairs_no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PublicationError("plan_duplicate_json_key")
        result[key] = value
    return result


def _read_plan(path: Path) -> dict:
    path = Path(path)
    if not path.is_absolute() or path.name in {"", ".", ".."}:
        raise PublicationError("plan_path_invalid")
    parent_fd = _open_absolute_directory(path.parent)
    try:
        fd = os.open(path.name, os.O_RDONLY | O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), dir_fd=parent_fd)
    except OSError as exc:
        os.close(parent_fd)
        raise PublicationError("plan_unavailable_or_symlink") from exc
    try:
        info = os.fstat(fd)
        parent_info = os.fstat(parent_fd)
        if (not stat.S_ISDIR(parent_info.st_mode) or parent_info.st_uid != os.geteuid()
                or stat.S_IMODE(parent_info.st_mode) & (stat.S_IWGRP | stat.S_IWOTH)):
            raise PublicationError("plan_parent_unsafe")
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) & (stat.S_IWGRP | stat.S_IWOTH)
                or info.st_size <= 0 or info.st_size > MAX_PLAN_BYTES):
            raise PublicationError("plan_type_or_size_invalid")
        chunks = []
        remaining = MAX_PLAN_BYTES + 1
        while remaining:
            block = os.read(fd, min(4096, remaining))
            if not block:
                break
            chunks.append(block)
            remaining -= len(block)
        raw = b"".join(chunks)
        after = os.fstat(fd)
        if ((info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
            raise PublicationError("plan_changed_during_read")
    finally:
        os.close(fd)
        os.close(parent_fd)
    if len(raw) > MAX_PLAN_BYTES:
        raise PublicationError("plan_type_or_size_invalid")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs_no_duplicates)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PublicationError("plan_json_invalid") from exc
    if not isinstance(value, dict):
        raise PublicationError("plan_shape_invalid")
    return value


def validate_plan(value: dict) -> list[dict]:
    if set(value) != {"schema", "files"} or value.get("schema") != SCHEMA:
        raise PublicationError("plan_schema_invalid")
    rows = value.get("files")
    if not isinstance(rows, list) or not 1 <= len(rows) <= len(FILES):
        raise PublicationError("plan_file_set_invalid")
    validated = []
    previous_index = -1
    for row in rows:
        if not isinstance(row, dict) or set(row) != {
                "source", "target", "expected_old_sha256", "source_sha256", "mode"}:
            raise PublicationError("plan_entry_shape_invalid")
        index = next((i for i, expected in enumerate(FILES)
                      if row["source"] == expected["source"] and row["target"] == expected["target"]), None)
        if index is None:
            raise PublicationError("plan_path_not_allowlisted")
        if index <= previous_index:
            raise PublicationError("plan_file_order_or_duplicate_invalid")
        previous_index = index
        expected = FILES[index]
        if row["mode"] != expected["mode"]:
            raise PublicationError("plan_mode_invalid")
        old = row["expected_old_sha256"]
        if old is not None and (not isinstance(old, str) or not SHA256.fullmatch(old)):
            raise PublicationError("plan_old_digest_invalid")
        new = row["source_sha256"]
        if not isinstance(new, str) or not SHA256.fullmatch(new):
            raise PublicationError("plan_source_digest_invalid")
        validated.append({**expected, "expected_old_sha256": old, "source_sha256": new})
    return validated


def _open_absolute_directory(root: Path) -> int:
    root = Path(root)
    if not root.is_absolute():
        raise PublicationError("root_path_must_be_absolute")
    try:
        fd = os.open("/", os.O_RDONLY | O_DIRECTORY | getattr(os, "O_CLOEXEC", 0))
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid not in {0, os.geteuid()} or stat.S_IMODE(info.st_mode) & 0o022:
            raise PublicationError("absolute_ancestor_permissions_unsafe")
        traversed = Path("/")
        for part in root.parts[1:]:
            next_fd = os.open(part, os.O_RDONLY | O_DIRECTORY | O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), dir_fd=fd)
            traversed = traversed / part
            info = os.fstat(next_fd)
            sticky_root_tmp = (traversed == Path("/tmp") and info.st_uid == 0
                               and stat.S_IMODE(info.st_mode) == 0o1777)
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in {0, os.geteuid()}
                    or (stat.S_IMODE(info.st_mode) & 0o022 and not sticky_root_tmp)):
                os.close(next_fd)
                raise PublicationError("absolute_ancestor_permissions_unsafe")
            os.close(fd)
            fd = next_fd
    except OSError as exc:
        if "fd" in locals():
            os.close(fd)
        raise PublicationError("root_ancestor_unsafe_or_missing") from exc
    except PublicationError:
        if "fd" in locals():
            os.close(fd)
        raise
    return fd


def _open_directory_tree(root: Path, relative: str) -> int:
    fd = _open_absolute_directory(root)
    parts = PurePosixPath(relative).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        os.close(fd)
        raise PublicationError("path_invalid")
    try:
        for part in parts:
            next_fd = os.open(part, os.O_RDONLY | O_DIRECTORY | O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), dir_fd=fd)
            info = os.fstat(next_fd)
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) & (stat.S_IWGRP | stat.S_IWOTH)):
                os.close(next_fd)
                raise PublicationError("ancestor_directory_unsafe_or_writable")
            os.close(fd)
            fd = next_fd
        return fd
    except OSError as exc:
        os.close(fd)
        raise PublicationError("ancestor_directory_unsafe_or_missing") from exc
    except PublicationError:
        os.close(fd)
        raise


def _read_regular_at(directory_fd: int, name: str) -> tuple[bytes, os.stat_result] | None:
    try:
        before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise PublicationError("destination_stat_failed") from exc
    if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) & (stat.S_IWGRP | stat.S_IWOTH)):
        raise PublicationError("destination_not_single_link_regular_file")
    try:
        fd = os.open(name, os.O_RDONLY | O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), dir_fd=directory_fd)
    except OSError as exc:
        raise PublicationError("destination_open_failed") from exc
    try:
        opened = os.fstat(fd)
        if (not stat.S_ISREG(opened.st_mode) or opened.st_uid != os.geteuid() or opened.st_nlink != 1
                or stat.S_IMODE(opened.st_mode) & (stat.S_IWGRP | stat.S_IWOTH)
                or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
                or opened.st_size > MAX_SOURCE_BYTES):
            raise PublicationError("destination_changed_or_size_invalid")
        chunks = []
        remaining = MAX_SOURCE_BYTES + 1
        while remaining:
            block = os.read(fd, min(65536, remaining))
            if not block:
                break
            chunks.append(block)
            remaining -= len(block)
        data = b"".join(chunks)
        if len(data) > MAX_SOURCE_BYTES:
            raise PublicationError("destination_size_invalid")
        after = os.fstat(fd)
        if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise PublicationError("destination_changed_during_read")
        return data, opened
    finally:
        os.close(fd)


def _read_source(source_root: Path, row: dict) -> bytes:
    source = PurePosixPath(row["source"])
    directory_fd = _open_directory_tree(source_root, str(source.parent))
    try:
        result = _read_regular_at(directory_fd, source.name)
    finally:
        os.close(directory_fd)
    if result is None:
        raise PublicationError("source_missing")
    data, info = result
    if stat.S_IMODE(info.st_mode) != row["mode"]:
        raise PublicationError("source_mode_mismatch")
    if hashlib.sha256(data).hexdigest() != row["source_sha256"]:
        raise PublicationError("source_digest_mismatch")
    return data


def _fsync_existing(directory_fd: int, name: str, expected_digest: str, mode: int) -> None:
    current = _read_regular_at(directory_fd, name)
    if current is None or hashlib.sha256(current[0]).hexdigest() != expected_digest:
        raise PublicationError("destination_changed_before_retry_sync")
    if stat.S_IMODE(current[1].st_mode) != mode:
        raise PublicationError("destination_mode_mismatch")
    fd = os.open(name, os.O_RDONLY | O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), dir_fd=directory_fd)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    os.fsync(directory_fd)


def _publish_one(target_root: Path, row: dict, data: bytes) -> str:
    target = PurePosixPath(row["target"])
    directory_fd = _open_directory_tree(target_root, str(target.parent))
    temp_name = f".fwrouter-publish-{secrets.token_hex(12)}.tmp"
    try:
        current = _read_regular_at(directory_fd, target.name)
        current_digest = hashlib.sha256(current[0]).hexdigest() if current else None
        if (current_digest == row["source_sha256"] and current is not None
                and stat.S_IMODE(current[1].st_mode) == row["mode"]):
            _fsync_existing(directory_fd, target.name, row["source_sha256"], row["mode"])
            return "already_desired"
        if current_digest != row["expected_old_sha256"]:
            raise PublicationError("destination_compare_and_swap_failed")

        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        fd = os.open(temp_name, flags, 0o600, dir_fd=directory_fd)
        try:
            offset = 0
            while offset < len(data):
                written = os.write(fd, data[offset:])
                if written <= 0:
                    raise PublicationError("temporary_write_failed")
                offset += written
            os.fchmod(fd, row["mode"])
            os.fsync(fd)
        finally:
            os.close(fd)

        # Recheck the compare value immediately before atomic per-file replace.
        current = _read_regular_at(directory_fd, target.name)
        current_digest = hashlib.sha256(current[0]).hexdigest() if current else None
        if current_digest != row["expected_old_sha256"]:
            raise PublicationError("destination_changed_before_replace")
        os.replace(temp_name, target.name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        installed = _read_regular_at(directory_fd, target.name)
        if (installed is None or hashlib.sha256(installed[0]).hexdigest() != row["source_sha256"]
                or stat.S_IMODE(installed[1].st_mode) != row["mode"]):
            raise PublicationError("destination_readback_failed")
        os.fsync(directory_fd)
        return "published"
    except PublicationError:
        raise
    except OSError as exc:
        raise PublicationError("publication_io_failed") from exc
    finally:
        try:
            os.unlink(temp_name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        except OSError:
            pass
        os.close(directory_fd)


def _preflight_destinations(target_root: Path, rows: list[dict]) -> None:
    """Validate every selected destination before the first file is changed."""
    for row in rows:
        target = PurePosixPath(row["target"])
        directory_fd = _open_directory_tree(target_root, str(target.parent))
        try:
            current = _read_regular_at(directory_fd, target.name)
            current_digest = hashlib.sha256(current[0]).hexdigest() if current else None
            already_desired = (current is not None and current_digest == row["source_sha256"]
                               and stat.S_IMODE(current[1].st_mode) == row["mode"])
            if not already_desired and current_digest != row["expected_old_sha256"]:
                raise PublicationError("destination_compare_and_swap_failed")
        finally:
            os.close(directory_fd)


def publish(source_root: Path, target_root: Path, plan_path: Path) -> list[dict[str, str]]:
    rows = validate_plan(_read_plan(plan_path))
    data_rows = [(row, _read_source(source_root, row)) for row in rows]
    _preflight_destinations(target_root, rows)
    results = []
    for row, data in data_rows:
        results.append({"target": row["target"], "result": _publish_one(target_root, row, data)})
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--target-root", required=True, type=Path)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--component-lock-fd", required=True, type=int)
    args = parser.parse_args(argv)
    try:
        lock_path = Path(__file__).absolute().with_name("component_install_lock.py")
        if lock_path.is_symlink() or not lock_path.is_file():
            raise PublicationError("component_lock_helper_unavailable")
        spec = importlib.util.spec_from_file_location("_host_publication_component_lock", lock_path)
        if spec is None or spec.loader is None:
            raise PublicationError("component_lock_helper_unavailable")
        lock = importlib.util.module_from_spec(spec)
        previous_bytecode = sys.dont_write_bytecode
        sys.dont_write_bytecode = True
        try:
            spec.loader.exec_module(lock)
        finally:
            sys.dont_write_bytecode = previous_bytecode
        try:
            lock.verify_inherited_lock(args.target_root / "opt", args.component_lock_fd, "exclusive")
        except lock.ComponentInstallLockError as exc:
            raise PublicationError("exclusive_component_lock_not_owned") from exc
        results = publish(args.source_root, args.target_root, args.plan)
    except PublicationError as exc:
        print(f"host-publication: STOP: {exc}", file=sys.stderr)
        return 2
    except Exception:
        print("host-publication: STOP: internal_error", file=sys.stderr)
        return 2
    for row in results:
        print(f"host-publication: {row['result']} {row['target']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
