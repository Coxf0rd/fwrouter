#!/usr/bin/env python3
"""Prepare, verify, and isolated-stage a hosted-qualified FWRouter backend artifact.

This tool never restores a live host.  Its only installer invocation is the
repository installer in ``--deploy --component backend`` mode with a checked
temporary target. Receipts are operator-supplied evidence and are not
cryptographically authenticated here; an isolated stage never qualifies live
runtime rollback.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
import sys
import tarfile
import tempfile
from typing import Any


SCHEMA = "fwrouter-safe-backend-recovery/v1"
RECEIPT_SCHEMA = "fwrouter-hosted-safety-qualification/v1"
ARCHIVE_NAME = "source.tar"
MANIFEST_NAME = "manifest.json"
STAGE_TIMEOUT_SECONDS = 120
MAX_SOURCE_FILES = 50000
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_SOURCE_BYTES = 512 * 1024 * 1024
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_JSON_BYTES = 16 * 1024 * 1024
REQUIRED_GATES = (
    "source_proof", "native", "packet_fail_closed", "rescue_isolation",
    "installer_stage", "cleanup",
)
BACKEND_INSTALLER_INPUTS = {
    "installer/install.sh",
    "installer/obsolete-backend-paths.txt",
    "installer/component_install_lock.py",
}


class RecoveryError(RuntimeError):
    pass


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_relative(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (not value or "\\" in value or path.is_absolute()
            or path.as_posix() != value
            or any(part in {"", ".", ".."} for part in path.parts)):
        raise RecoveryError(f"unsafe artifact path: {value!r}")
    return path


def _tracked_source_files(repo_root: Path) -> list[tuple[str, Path, int]]:
    files: list[tuple[str, Path, int]] = []
    try:
        tracked = subprocess.run(
            ["git", "-C", str(repo_root), "ls-files", "--stage", "-z", "--", "backend",
             *sorted(BACKEND_INSTALLER_INPUTS)],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10,
        ).stdout.decode("utf-8").split("\0")
    except (OSError, UnicodeError, subprocess.SubprocessError) as exc:
        raise RecoveryError("cannot enumerate tracked backend installer inputs") from exc
    staged: list[tuple[str, str]] = []
    for record in tracked:
        if not record:
            continue
        try:
            metadata, relative = record.split("\t", 1)
            mode, _object_id, stage = metadata.split(" ", 2)
        except ValueError as exc:
            raise RecoveryError("Git returned an invalid staged source record") from exc
        if stage != "0" or mode not in {"100644", "100755"}:
            raise RecoveryError(f"source contains a symlink or non-regular Git entry: {relative}")
        staged.append((relative, mode))
    names = sorted(name for name, _mode in staged)
    if not BACKEND_INSTALLER_INPUTS.issubset(names) or not any(name.startswith("backend/") for name in names):
        raise RecoveryError("source checkout is missing backend/ or required backend installer inputs")
    for relative, git_mode in sorted(staged):
        safe = _safe_relative(relative)
        if not (relative.startswith("backend/") or relative in BACKEND_INSTALLER_INPUTS):
            raise RecoveryError("Git returned an out-of-scope source path")
        candidate = repo_root.joinpath(*safe.parts)
        current = repo_root
        for part in safe.parts:
            current = current / part
            if current.is_symlink():
                raise RecoveryError(f"source contains a symlink: {relative}")
        if not candidate.is_file():
            raise RecoveryError(f"source is missing a tracked regular file: {relative}")
        file_info = candidate.stat()
        if file_info.st_size > MAX_FILE_BYTES:
            raise RecoveryError(f"source file exceeds size limit: {relative}")
        # The standard installer intentionally excludes the checked-in env
        # template as well as live credentials; neither belongs in recovery.
        if relative == "backend/.env.example":
            continue
        if relative.startswith("backend/"):
            components = safe.parts[1:]
            if any(part in {".env", ".venv", ".pytest_cache", "__pycache__", ".git"}
                   or part.startswith(".env.") or part.endswith((".pyc", ".pyo", ".db", ".sqlite",
                                                                  ".sqlite3", ".bak"))
                   or ".bak-" in part for part in components):
                raise RecoveryError(f"tracked backend source is excluded by installer: {relative}")
        if bool(file_info.st_mode & 0o111) != (git_mode == "100755"):
            raise RecoveryError(f"source executable mode differs from Git: {relative}")
        files.append((relative, candidate, 0o755 if git_mode == "100755" else 0o644))
    if len(files) > MAX_SOURCE_FILES or sum(path.stat().st_size for _, path, _ in files) > MAX_SOURCE_BYTES:
        raise RecoveryError("backend installer inputs exceed file-count or total-size limits")
    files.sort(key=lambda item: item[0])
    return files


def _git_source_identity(repo_root: Path) -> str:
    try:
        revision = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "--verify", "HEAD"],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "-C", str(repo_root), "status", "--porcelain", "--untracked-files=all",
             "--", "backend", *sorted(BACKEND_INSTALLER_INPUTS)],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise RecoveryError("cannot establish source Git identity") from exc
    if len(revision) != 40 or any(ch not in "0123456789abcdef" for ch in revision.lower()):
        raise RecoveryError("source Git revision is not a full commit SHA")
    if dirty:
        raise RecoveryError("backend or installer inputs have uncommitted or untracked changes")
    return revision.lower()


def _entries(files: list[tuple[str, Path, int]]) -> list[dict[str, Any]]:
    return [
        {"path": relative, "sha256": _sha256(path.read_bytes()), "mode": mode, "size": path.stat().st_size}
        for relative, path, mode in files
    ]


def _entries_digest(entries: list[dict[str, Any]]) -> str:
    return _sha256(_canonical_json(entries))


def _read_json_file(path: Path, label: str) -> dict[str, Any]:
    try:
        if path.is_symlink() or not path.is_file():
            raise RecoveryError(f"{label} must be a regular, non-symlink file")
        if path.stat().st_size > MAX_JSON_BYTES:
            raise RecoveryError(f"{label} exceeds the size limit")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RecoveryError(f"cannot read {label}") from exc
    if not isinstance(value, dict):
        raise RecoveryError(f"{label} must contain a JSON object")
    return value


def _validate_receipt(receipt_path: Path, source_sha: str, source_digest: str) -> dict[str, Any]:
    receipt = _read_json_file(receipt_path, "hosted qualification receipt")
    gates = receipt.get("gates")
    run_id = receipt.get("hosted_run_id")
    run_url = receipt.get("hosted_run_url")
    if (receipt.get("schema") != RECEIPT_SCHEMA or receipt.get("status") != "passed"
            or receipt.get("source_sha") != source_sha
            or receipt.get("source_manifest_sha256") != source_digest
            or not isinstance(receipt.get("receipt_id"), str)
            or not receipt["receipt_id"].strip()
            or not isinstance(run_id, str) or not run_id.isdigit() or int(run_id) <= 0
            or not isinstance(run_url, str) or not run_url.startswith("https://")
            or not isinstance(gates, dict)
            or any(gates.get(name) != "passed" for name in REQUIRED_GATES)):
        raise RecoveryError("hosted qualification receipt is missing, unqualified, or for another source")
    return receipt


def _write_all(path: Path, payload: bytes, mode: int) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, mode)
    try:
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(fd)


def prepare(repo_root: Path, artifact_dir: Path, receipt_path: Path) -> dict[str, Any]:
    repo_root = repo_root.resolve(strict=True)
    source_sha = _git_source_identity(repo_root)
    files = _tracked_source_files(repo_root)
    entries = _entries(files)
    source_digest = _entries_digest(entries)
    _validate_receipt(receipt_path, source_sha, source_digest)

    if artifact_dir.exists() or artifact_dir.is_symlink():
        raise RecoveryError("artifact destination must not already exist")
    artifact_dir.parent.mkdir(parents=True, exist_ok=True)
    artifact_dir.mkdir(mode=0o700)
    try:
        archive_path = artifact_dir / ARCHIVE_NAME
        with tarfile.open(archive_path, "w", format=tarfile.PAX_FORMAT) as archive:
            for relative, path, mode in files:
                data = path.read_bytes()
                info = tarfile.TarInfo(relative)
                info.size = len(data)
                info.mode = mode
                info.mtime = 0
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                archive.addfile(info, io.BytesIO(data))
        with archive_path.open("rb") as stream:
            os.fsync(stream.fileno())
        if archive_path.stat().st_size > MAX_ARCHIVE_BYTES:
            raise RecoveryError("recovery archive exceeds size limit")
        manifest = {
            "schema": SCHEMA,
            "source_sha": source_sha,
            "source_manifest_sha256": source_digest,
            "archive_sha256": _sha256(archive_path.read_bytes()),
            "files": entries,
        }
        _write_all(artifact_dir / MANIFEST_NAME, _canonical_json(manifest) + b"\n", 0o444)
        verify(artifact_dir, receipt_path)
        archive_path.chmod(0o444)
        artifact_dir.chmod(0o555)
    except BaseException:
        # Keep partial output available for inspection; never silently remove an artifact.
        try:
            artifact_dir.chmod(0o700)
        except OSError:
            pass
        raise
    return {"status": "prepared", "source_sha": source_sha,
            "source_manifest_sha256": source_digest, "artifact": str(artifact_dir)}


def _safe_archive_entries(archive: tarfile.TarFile) -> list[tarfile.TarInfo]:
    members: list[tarfile.TarInfo] = []
    seen: set[str] = set()
    total_bytes = 0
    for member in archive:
        if len(members) >= MAX_SOURCE_FILES or member.size < 0 or member.size > MAX_FILE_BYTES:
            raise RecoveryError("artifact archive exceeds member size/count limit")
        total_bytes += member.size
        if total_bytes > MAX_SOURCE_BYTES:
            raise RecoveryError("artifact archive exceeds total source-size limit")
        relative = _safe_relative(member.name)
        normalized = relative.as_posix()
        if normalized in seen or not member.isfile() or member.issym() or member.islnk():
            raise RecoveryError("artifact archive has duplicate, linked, or non-file members")
        if not (normalized.startswith("installer/") or normalized.startswith("backend/")):
            raise RecoveryError("artifact archive contains an out-of-scope path")
        seen.add(normalized)
        members.append(member)
    return members


def verify(artifact_dir: Path, receipt_path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if artifact_dir.is_symlink() or not artifact_dir.is_dir():
        raise RecoveryError("artifact must be a real directory")
    children = {entry.name for entry in artifact_dir.iterdir()}
    if children != {ARCHIVE_NAME, MANIFEST_NAME}:
        raise RecoveryError("artifact contains missing or unexpected files")
    manifest = _read_json_file(artifact_dir / MANIFEST_NAME, "artifact manifest")
    if manifest.get("schema") != SCHEMA or not isinstance(manifest.get("files"), list):
        raise RecoveryError("artifact manifest schema is invalid")
    entries = manifest["files"]
    if not entries or len(entries) > MAX_SOURCE_FILES or any(not isinstance(item, dict) for item in entries):
        raise RecoveryError("artifact file manifest is invalid")
    names: list[str] = []
    for entry in entries:
        name = entry.get("path")
        _safe_relative(name if isinstance(name, str) else "")
        if not (name in BACKEND_INSTALLER_INPUTS
                or name.startswith("backend/")):
            raise RecoveryError("artifact manifest contains an out-of-scope path")
        if (not isinstance(entry.get("sha256"), str) or len(entry["sha256"]) != 64
                or not isinstance(entry.get("mode"), int) or entry["mode"] < 0
                or entry["mode"] not in {0o644, 0o755}
                or not isinstance(entry.get("size"), int) or entry["size"] < 0
                or entry["size"] > MAX_FILE_BYTES):
            raise RecoveryError("artifact manifest entry is invalid")
        names.append(name)
    if (names != sorted(set(names))
            or not BACKEND_INSTALLER_INPUTS.issubset(names)):
        raise RecoveryError("artifact manifest paths are duplicate, unsorted, or incomplete")
    if sum(entry["size"] for entry in entries) > MAX_SOURCE_BYTES:
        raise RecoveryError("artifact manifest exceeds total source-size limit")
    source_sha = manifest.get("source_sha")
    digest = manifest.get("source_manifest_sha256")
    if not isinstance(source_sha, str) or len(source_sha) != 40 or not isinstance(digest, str):
        raise RecoveryError("artifact source identity is invalid")
    if _entries_digest(entries) != digest:
        raise RecoveryError("artifact manifest digest mismatch")
    _validate_receipt(receipt_path, source_sha, digest)

    archive_path = artifact_dir / ARCHIVE_NAME
    if archive_path.is_symlink() or not archive_path.is_file():
        raise RecoveryError("artifact archive must be a regular, non-symlink file")
    if archive_path.stat().st_size > MAX_ARCHIVE_BYTES:
        raise RecoveryError("artifact archive exceeds size limit")
    archive_bytes = archive_path.read_bytes()
    if _sha256(archive_bytes) != manifest.get("archive_sha256"):
        raise RecoveryError("artifact archive hash mismatch")
    try:
        with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:") as archive:
            members = _safe_archive_entries(archive)
            if [member.name for member in members] != names:
                raise RecoveryError("artifact archive and manifest paths differ")
            for member, entry in zip(members, entries):
                stream = archive.extractfile(member)
                if (stream is None or member.size != entry["size"]
                        or _sha256(stream.read()) != entry["sha256"] or member.mode != entry["mode"]):
                    raise RecoveryError("artifact file content or mode mismatch")
    except (OSError, tarfile.TarError) as exc:
        raise RecoveryError("artifact archive is unreadable") from exc
    return manifest, entries


def _check_isolated_target(target: Path) -> Path:
    temp_root = Path(tempfile.gettempdir()).resolve(strict=True)
    # TMPDIR is caller-controlled. It must not turn staging into a write to
    # /opt, /etc, /var/lib or another live filesystem tree.
    if not temp_root.is_relative_to(Path("/tmp")):
        raise RecoveryError("stage temporary root must stay inside canonical /tmp")
    if not target.is_absolute():
        raise RecoveryError("stage target must be an absolute path")
    if target.is_symlink() or target.parent.is_symlink() or not target.exists() or not target.is_dir():
        raise RecoveryError("stage target must be an existing non-symlink directory")
    resolved = target.resolve(strict=True)
    if resolved.parent.parent != temp_root or not resolved.is_relative_to(temp_root):
        raise RecoveryError("stage target must be a direct child of a private temporary directory")
    private_root = resolved.parent
    for path in (private_root, resolved):
        info = path.stat()
        if path.is_symlink() or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise RecoveryError("temporary isolation root and target must be owned and mode 0700")
    for current, dirs, names in os.walk(resolved, followlinks=False):
        if len(dirs) + len(names) > MAX_SOURCE_FILES:
            raise RecoveryError("stage target exceeds file-count limit")
        for name in [*dirs, *names]:
            child = Path(current) / name
            child_info = child.lstat()
            if child.is_symlink():
                raise RecoveryError("stage target contains a symlink")
            if child_info.st_dev != resolved.stat().st_dev or (stat.S_ISREG(child_info.st_mode) and child_info.st_nlink > 1):
                raise RecoveryError("stage target contains a mount boundary or hard-linked file")
            if not (stat.S_ISDIR(child_info.st_mode) or stat.S_ISREG(child_info.st_mode)):
                raise RecoveryError("stage target contains a special file")
    return resolved


def stage(artifact_dir: Path, receipt_path: Path, target: Path) -> dict[str, Any]:
    manifest, entries = verify(artifact_dir, receipt_path)
    target = _check_isolated_target(target)
    with tempfile.TemporaryDirectory(prefix="fwrouter-recovery-source-", dir=target.parent) as source_temp:
        source_root = Path(source_temp)
        archive_path = artifact_dir / ARCHIVE_NAME
        archive_bytes = archive_path.read_bytes()
        if _sha256(archive_bytes) != manifest["archive_sha256"]:
            raise RecoveryError("artifact changed after verification")
        with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:") as archive:
            members = _safe_archive_entries(archive)
            for member in members:
                relative = _safe_relative(member.name)
                destination = source_root.joinpath(*relative.parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                stream = archive.extractfile(member)
                if stream is None:
                    raise RecoveryError("artifact archive member cannot be read")
                destination.write_bytes(stream.read())
                destination.chmod(member.mode)
        installer = source_root / "installer/install.sh"
        try:
            result = subprocess.run(
                ["sh", str(installer), "--deploy", "--component", "backend", "--target", str(target)],
                cwd=source_root,
                env={"PATH": os.defpath, "LC_ALL": "C"},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=STAGE_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RecoveryError("isolated stage timed out; partial target retained and may be restaged") from exc
        except OSError as exc:
            raise RecoveryError("isolated installer could not be started") from exc
        if result.returncode != 0:
            raise RecoveryError(
                f"isolated installer failed with status {result.returncode}; target retained for inspection/restage"
            )
    installed_root = target / "opt/fwrouter-api"
    expected = {entry["path"][len("backend/"):]: entry for entry in entries
                if entry["path"].startswith("backend/")}
    for relative, entry in expected.items():
        installed = installed_root / relative
        if installed.is_symlink() or not installed.is_file() or _sha256(installed.read_bytes()) != entry["sha256"]:
            raise RecoveryError(f"isolated stage source hash mismatch: opt/fwrouter-api/{relative}")
    preserved: list[str] = []
    unsafe_extras: list[str] = []
    scanned_files = 0
    scanned_bytes = 0
    for current, dirs, names in os.walk(installed_root, followlinks=False):
        scanned_files += len(dirs) + len(names)
        if scanned_files > MAX_SOURCE_FILES:
            raise RecoveryError("isolated backend target exceeds file-count limit")
        for name in [*dirs, *names]:
            path = Path(current) / name
            relative = path.relative_to(installed_root).as_posix()
            if relative in expected or relative.startswith(tuple(f"{key}/" for key in expected)):
                continue
            info = path.lstat()
            if path.is_symlink() or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                unsafe_extras.append(relative)
                continue
            if path.is_dir() and any(value.startswith(relative + "/") for value in expected):
                continue
            preserved.append(relative)
            if path.is_file():
                scanned_bytes += info.st_size
                if scanned_bytes > MAX_SOURCE_BYTES:
                    raise RecoveryError("isolated backend target exceeds retained-file size limit")
            if (path.is_file() and (info.st_mode & 0o111 or path.suffix.lower() in
                    {".py", ".pyc", ".pyw", ".so", ".pyd", ".pth", ".sh", ".bash", ".pl", ".rb"})):
                unsafe_extras.append(relative)
    if unsafe_extras:
        raise RecoveryError("isolated stage retained unqualified executable/code files: "
                            + ", ".join(sorted(unsafe_extras)[:20]))
    return {"status": "isolated_stage_complete", "runtime_qualification": "not_established",
            "scope": "isolated_backend_file_copy", "source_sha": manifest["source_sha"], "target": str(target),
            "installer_returncode": 0, "installed_backend_files_verified": len(expected),
            "retained_extra_files": sorted(set(preserved))}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare_parser = commands.add_parser("prepare", help="build a qualified immutable backend artifact")
    prepare_parser.add_argument("--repo-root", type=Path, required=True)
    prepare_parser.add_argument("--artifact-dir", type=Path, required=True)
    prepare_parser.add_argument("--receipt", type=Path, required=True)
    verify_parser = commands.add_parser("verify", help="verify artifact hashes and hosted qualification")
    verify_parser.add_argument("--artifact-dir", type=Path, required=True)
    verify_parser.add_argument("--receipt", type=Path, required=True)
    stage_parser = commands.add_parser("stage", help="stage backend into a private temporary target")
    stage_parser.add_argument("--artifact-dir", type=Path, required=True)
    stage_parser.add_argument("--receipt", type=Path, required=True)
    stage_parser.add_argument("--target", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "prepare":
            result = prepare(args.repo_root, args.artifact_dir, args.receipt)
        elif args.command == "verify":
            manifest, _ = verify(args.artifact_dir, args.receipt)
            result = {"status": "verified", "source_sha": manifest["source_sha"],
                      "source_manifest_sha256": manifest["source_manifest_sha256"]}
        else:
            result = stage(args.artifact_dir, args.receipt, args.target)
    except RecoveryError as exc:
        print(f"safe_recovery: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
