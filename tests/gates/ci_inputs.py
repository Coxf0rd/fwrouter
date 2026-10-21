#!/usr/bin/env python3
"""Build and verify the source-bound backend wheel used by parallel CI jobs."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
GATES = ROOT / "tests/gates"
MANIFEST = GATES / "manifest.json"
BUILD_LOCK = GATES / "requirements-build.txt"
BUILD_DIR = "build"
SUMMARY_NAME = "build-summary.json"
METADATA_NAME = "wheel-metadata.json"
MAX_WHEEL_BYTES = 128 * 1024 * 1024
MAX_METADATA_BYTES = 2 * 1024 * 1024
MAX_SUMMARY_BYTES = 64 * 1024
MAX_PLAN_BYTES = 8 * 1024 * 1024
MAX_PRODUCER_BYTES = 64 * 1024
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


class BuildInputError(ValueError):
    """The plan, build artifact, or local source does not meet its contract."""


def canonical_digest(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _valid_timing(value: Any) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(float(value)) and 0 <= value <= 300
    except (OverflowError, TypeError, ValueError):
        return False


def _read_json(path: Path, *, limit: int) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
        raise BuildInputError(f"{path.name} is missing, unsafe, or oversized")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BuildInputError(f"{path.name} is invalid JSON") from exc
    if not isinstance(value, dict):
        raise BuildInputError(f"{path.name} must be a JSON object")
    return value


def _current_commit(root: Path) -> str:
    result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=False,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10)
    value = result.stdout.decode("ascii", "ignore").strip()
    if result.returncode or not COMMIT_RE.fullmatch(value):
        raise BuildInputError("cannot identify the checked-out source commit")
    status = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"], cwd=root,
                            check=False, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10)
    if status.returncode or status.stdout:
        raise BuildInputError("build source checkout is not clean at the planned commit")
    return value


def _validate_plan(root: Path, plan_path: Path) -> tuple[dict[str, Any], dict[str, Any], str, str]:
    plan = _read_json(plan_path, limit=MAX_PLAN_BYTES)
    plan_digest = plan.get("plan_digest")
    if not isinstance(plan_digest, str) or not SHA256_RE.fullmatch(plan_digest):
        raise BuildInputError("plan digest is missing or malformed")
    unsigned = dict(plan)
    unsigned.pop("plan_digest", None)
    if canonical_digest(unsigned) != plan_digest:
        raise BuildInputError("plan digest does not match its contents")
    commit = _current_commit(root)
    if plan.get("source_commit") != commit:
        raise BuildInputError("plan source commit differs from the checked-out source")
    manifest_path = root / "tests/gates/manifest.json"
    manifest = _read_json(manifest_path, limit=16 * 1024 * 1024)
    manifest_digest = canonical_digest(manifest)
    if (plan.get("manifest_digest") != manifest_digest
            or plan.get("manifest_version") != manifest.get("version")):
        raise BuildInputError("plan manifest identity differs from current source")
    if not isinstance(plan.get("selected_files"), list):
        raise BuildInputError("plan selected test files must be a list")
    return plan, manifest, commit, plan_digest


def _run(argv: list[str], *, root: Path, timeout: int, label: str) -> float:
    started = time.monotonic()
    try:
        result = subprocess.run(argv, cwd=root, check=False, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BuildInputError(f"{label} did not complete within its bounded execution") from exc
    elapsed = round(time.monotonic() - started, 3)
    if result.returncode:
        raise BuildInputError(f"{label} failed with exit code {result.returncode}")
    return elapsed


def _prepare_root(path: Path) -> Path:
    if path.exists() or path.is_symlink():
        raise BuildInputError("build artifact root must be a fresh path")
    path.mkdir(mode=0o700, parents=True)
    if path.is_symlink() or not path.is_dir() or path.stat().st_uid != os.getuid():
        raise BuildInputError("build artifact root ownership or type is unsafe")
    return path


def prepare(root: Path, plan_path: Path, *, source_root: Path = ROOT) -> dict[str, Any]:
    source_root = source_root.resolve(strict=True)
    plan_path = plan_path.resolve(strict=True)
    plan, manifest, commit, plan_digest = _validate_plan(source_root, plan_path)
    artifact_path = root.resolve(strict=False)
    if artifact_path == source_root or artifact_path.is_relative_to(source_root):
        raise BuildInputError("build artifacts must be outside the source checkout")
    artifact_root = _prepare_root(root)
    wheel_dir = artifact_root / BUILD_DIR
    wheel_dir.mkdir(mode=0o700)
    staging = artifact_root / ".wheel-staging"
    staging.mkdir(mode=0o700)
    try:
        lock_path = source_root / "tests/gates/requirements-build.txt"
        lock_sha = file_digest(lock_path)
        install_seconds = _run(
            [sys.executable, "-m", "pip", "install", "--require-hashes", "-r", str(lock_path)],
            root=source_root, timeout=300, label="hashed wheel build dependency installation")
        compile_seconds = _run(
            [sys.executable, "-m", "compileall", "-q", "backend/fwrouter_api"],
            root=source_root, timeout=120, label="backend compileall")
        javascript = sorted((source_root / "ui/static/js").glob("*.js"))
        if not javascript:
            raise BuildInputError("static UI JavaScript files are missing")
        ui_started = time.monotonic()
        for path in javascript:
            _run(["node", "--check", str(path)], root=source_root, timeout=30,
                 label=f"JavaScript syntax check for {path.name}")
        ui_seconds = round(time.monotonic() - ui_started, 3)
        wheel_seconds = _run(
            [sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation",
             "--wheel-dir", str(staging), "./backend"],
            root=source_root, timeout=300, label="backend wheel build")
        verifier = source_root / "tests/gates/verify_backend_wheel.py"
        metadata_path = artifact_root / BUILD_DIR / METADATA_NAME
        _run([sys.executable, str(verifier), str(staging), str(metadata_path)],
             root=source_root, timeout=30, label="backend wheel source-content verification")
        wheels = sorted(staging.glob("*.whl"))
        if len(wheels) != 1:
            raise BuildInputError("wheel build must produce exactly one package")
        wheel = wheels[0]
        wheel_bytes = wheel.stat().st_size
        if wheel.is_symlink() or not wheel.is_file() or not 0 < wheel_bytes <= MAX_WHEEL_BYTES:
            raise BuildInputError("built wheel is unsafe or outside its size bound")
        target = wheel_dir / wheel.name
        wheel.replace(target)
        metadata_bytes = metadata_path.stat().st_size
        if metadata_path.is_symlink() or metadata_bytes > MAX_METADATA_BYTES:
            raise BuildInputError("wheel metadata is unsafe or oversized")
        summary = {
            "schema": "fwrouter-ci-build-inputs/v1",
            "source_commit": commit,
            "plan_digest": plan_digest,
            "manifest_version": manifest["version"],
            "manifest_digest": canonical_digest(manifest),
            "python_version": platform.python_version(),
            "build_requirements_sha256": lock_sha,
            "wheel": {"path": f"{BUILD_DIR}/{wheel.name}", "sha256": file_digest(target),
                      "bytes": wheel_bytes},
            "wheel_metadata_sha256": file_digest(metadata_path),
            "timings_seconds": {"build_dependency_install": install_seconds,
                                "compileall": compile_seconds, "ui_syntax": ui_seconds,
                                "wheel_build": wheel_seconds},
        }
        summary_path = artifact_root / BUILD_DIR / SUMMARY_NAME
        summary_bytes = (json.dumps(summary, sort_keys=True, indent=2) + "\n").encode("utf-8")
        if len(summary_bytes) > MAX_SUMMARY_BYTES:
            raise BuildInputError("build summary exceeds its size bound")
        summary_path.write_bytes(summary_bytes)
        summary_path.chmod(0o600)
        metadata_path.chmod(0o600)
        target.chmod(0o600)
        return summary
    except BaseException:
        shutil.rmtree(artifact_root, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def verify(root: Path, plan_path: Path, *, source_root: Path = ROOT) -> dict[str, Any]:
    source_root = source_root.resolve(strict=True)
    plan_path = plan_path.resolve(strict=True)
    manifest, _plan, commit, plan_digest = _unpack_plan(source_root, plan_path)
    artifact_root = root.resolve(strict=True)
    if artifact_root == source_root or artifact_root.is_relative_to(source_root):
        raise BuildInputError("build artifacts must be outside the source checkout")
    root_entries = {path.name for path in artifact_root.iterdir()}
    if root_entries not in ({BUILD_DIR}, {BUILD_DIR, "producer.json"}):
        raise BuildInputError("build artifact root has missing or unexpected entries")
    if "producer.json" in root_entries:
        producer = artifact_root / "producer.json"
        if (producer.is_symlink() or not producer.is_file()
                or producer.stat().st_size > MAX_PRODUCER_BYTES):
            raise BuildInputError("producer artifact metadata is missing, unsafe, or oversized")
    build_dir = artifact_root / BUILD_DIR
    if (root.is_symlink() or build_dir.is_symlink() or not build_dir.is_dir()
            or not build_dir.resolve().is_relative_to(artifact_root)):
        raise BuildInputError("build artifact directory is missing or unsafe")
    expected_names = {SUMMARY_NAME, METADATA_NAME}
    entries = list(build_dir.iterdir())
    wheels = [path for path in entries if path.name.endswith(".whl")]
    if len(wheels) != 1 or {path.name for path in entries} != expected_names | {wheels[0].name}:
        raise BuildInputError("build artifact has missing or unexpected files")
    if any(path.is_symlink() or not path.is_file() for path in entries):
        raise BuildInputError("build artifact contains a symlink or non-file")
    summary_path = build_dir / SUMMARY_NAME
    metadata_path = build_dir / METADATA_NAME
    summary = _read_json(summary_path, limit=MAX_SUMMARY_BYTES)
    expected_summary_fields = {
        "schema", "source_commit", "plan_digest", "manifest_version", "manifest_digest",
        "python_version", "build_requirements_sha256", "wheel", "wheel_metadata_sha256",
        "timings_seconds",
    }
    timings = summary.get("timings_seconds")
    if (set(summary) != expected_summary_fields
            or summary.get("schema") != "fwrouter-ci-build-inputs/v1"
            or summary.get("source_commit") != commit
            or summary.get("plan_digest") != plan_digest
            or summary.get("manifest_version") != manifest.get("version")
            or summary.get("manifest_digest") != canonical_digest(manifest)
            or summary.get("build_requirements_sha256") != file_digest(source_root / "tests/gates/requirements-build.txt")
            or summary.get("python_version") != platform.python_version()
            or not isinstance(timings, dict)
            or set(timings) != {"build_dependency_install", "compileall", "ui_syntax", "wheel_build"}
            or any(not _valid_timing(value) for value in timings.values())):
        raise BuildInputError("build artifact provenance differs from this source plan")
    wheel_record = summary.get("wheel")
    wheel = wheels[0]
    metadata_size = metadata_path.stat().st_size
    if (not isinstance(wheel_record, dict) or wheel_record != {
            "path": f"{BUILD_DIR}/{wheel.name}", "sha256": file_digest(wheel), "bytes": wheel.stat().st_size}
            or not 0 < wheel.stat().st_size <= MAX_WHEEL_BYTES
            or metadata_size > MAX_METADATA_BYTES
            or summary.get("wheel_metadata_sha256") != file_digest(metadata_path)):
        raise BuildInputError("wheel digest or measurements do not match the build summary")
    regenerated_metadata = build_dir / ".wheel-metadata-verified.json"
    try:
        _run([sys.executable, str(source_root / "tests/gates/verify_backend_wheel.py"),
              str(build_dir), str(regenerated_metadata)],
             root=source_root, timeout=30, label="downloaded wheel source-content verification")
        if file_digest(regenerated_metadata) != file_digest(metadata_path):
            raise BuildInputError("wheel verifier metadata differs from its prepared receipt")
    finally:
        regenerated_metadata.unlink(missing_ok=True)
    return summary


def _unpack_plan(source_root: Path, plan_path: Path) -> tuple[dict[str, Any], dict[str, Any], str, str]:
    plan, manifest, commit, plan_digest = _validate_plan(source_root, plan_path)
    return manifest, plan, commit, plan_digest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare", "verify"):
        command = sub.add_parser(name)
        command.add_argument("--root", type=Path, required=True,
                             help="build artifact root (runner-temp path; the build/ subdir is owned)")
        command.add_argument("--plan", type=Path, required=True,
                             help="immutable affected plan produced for this source checkout")
    args = parser.parse_args()
    try:
        result = prepare(args.root, args.plan) if args.command == "prepare" else verify(args.root, args.plan)
    except (BuildInputError, OSError, UnicodeError, subprocess.SubprocessError) as exc:
        print(f"CI build inputs: {str(exc)[:512]}", file=sys.stderr)
        return 2
    print(json.dumps({"status": "prepared" if args.command == "prepare" else "verified",
                      "source_commit": result["source_commit"], "plan_digest": result["plan_digest"],
                      "wheel_sha256": result["wheel"]["sha256"],
                      "wheel_bytes": result["wheel"]["bytes"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
