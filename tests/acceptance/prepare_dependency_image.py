#!/usr/bin/env python3
"""Prepare a source-free build context for the pinned acceptance dependency image."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import stat
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
LOCK_PATH = ROOT / "tests/acceptance/dependency-image.lock.json"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
LOCK_SCHEMA = "fwrouter-acceptance-dependency-lock/v1"
INPUT_SCHEMA = "fwrouter-hosted-inputs/v1"


class PreparationError(ValueError):
    """A dependency lock, prebuilt input, or staging path failed validation."""


def _canonical_digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _file_digest(path: Path) -> tuple[str, int]:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or path.is_symlink():
        raise PreparationError(f"dependency input is not a regular file: {path.name}")
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            size += len(block)
            digest.update(block)
    return digest.hexdigest(), size


def _valid_duration(value: Any) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(float(value)) and value >= 0
    except (OverflowError, TypeError, ValueError):
        return False


def validate_lock(lock: Any, *, root: Path = ROOT) -> dict[str, Any]:
    required = {
        "schema", "platform", "base_image", "debian_snapshot", "apt_packages",
        "requirements", "assets", "playwright_version", "dockerfile_sha256",
    }
    if not isinstance(lock, dict) or set(lock) != required or lock.get("schema") != LOCK_SCHEMA:
        raise PreparationError("dependency lock has an unexpected schema or fields")
    if lock.get("platform") != "linux/amd64":
        raise PreparationError("dependency image platform must remain linux/amd64")
    base = str(lock.get("base_image") or "")
    if "@sha256:" not in base or not SHA256_RE.fullmatch(base.rsplit("@sha256:", 1)[-1]):
        raise PreparationError("dependency base image must be digest-pinned")
    snapshot = lock.get("debian_snapshot")
    if (not isinstance(snapshot, dict)
            or set(snapshot) != {"url", "suite", "components", "check_valid_until", "signature_verification"}
            or not re.fullmatch(r"https://snapshot\.debian\.org/archive/debian/\d{8}T\d{6}Z",
                                str(snapshot.get("url", "")))
            or snapshot.get("suite") != "bookworm"
            or snapshot.get("components") != ["main"]
            or snapshot.get("check_valid_until") is not False
            or snapshot.get("signature_verification") != "apt-secure-required"):
        raise PreparationError("dependency lock must identify one signed fixed Debian snapshot")
    packages = lock.get("apt_packages")
    if (not isinstance(packages, list) or not packages
            or any(not isinstance(name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9+.-]*", name) for name in packages)
            or len(packages) != len(set(packages)) or packages != sorted(packages)):
        raise PreparationError("dependency lock apt package list is invalid")
    expected_requirements = {
        "tests/gates/requirements-ci.txt",
        "tests/acceptance/requirements-browser.txt",
    }
    requirements = lock.get("requirements")
    if not isinstance(requirements, dict) or set(requirements) != expected_requirements:
        raise PreparationError("dependency lock requirements set is incomplete")
    for relative, expected in requirements.items():
        if not isinstance(expected, str) or not SHA256_RE.fullmatch(expected):
            raise PreparationError("dependency requirements digest is invalid")
        observed, _size = _file_digest(root / relative)
        if observed != expected:
            raise PreparationError(f"dependency requirements digest mismatch: {relative}")
    assets = lock.get("assets")
    expected_assets = {
        "xray": {"version", "archive_sha256", "binary_sha256"},
        "mihomo": {"version", "archive_sha256", "binary_sha256"},
        "chromium": {"version", "revision", "archive_sha256", "bundle_sha256", "executable_sha256"},
    }
    if not isinstance(assets, dict) or set(assets) != set(expected_assets):
        raise PreparationError("dependency lock native/browser asset set is invalid")
    for name, fields in expected_assets.items():
        item = assets[name]
        if not isinstance(item, dict) or set(item) != fields:
            raise PreparationError(f"dependency lock fields are invalid for {name}")
        for key, value in item.items():
            if key.endswith("sha256") and (not isinstance(value, str) or not SHA256_RE.fullmatch(value)):
                raise PreparationError(f"dependency lock digest is invalid for {name}.{key}")
            if key in {"version", "revision"} and (not isinstance(value, str) or not value.strip()):
                raise PreparationError(f"dependency lock version is invalid for {name}.{key}")
    if not isinstance(lock.get("playwright_version"), str) or not lock["playwright_version"].strip():
        raise PreparationError("dependency lock Playwright version is missing")
    dockerfile_digest = lock.get("dockerfile_sha256")
    if not isinstance(dockerfile_digest, str) or not SHA256_RE.fullmatch(dockerfile_digest):
        raise PreparationError("dependency Dockerfile digest is invalid")
    observed_dockerfile, _size = _file_digest(root / "tests/acceptance/Dependencies.Dockerfile")
    if observed_dockerfile != dockerfile_digest:
        raise PreparationError("dependency Dockerfile digest differs from the reviewed lock")
    return lock


def validate_provision_manifest(lock: dict[str, Any], manifest: Any) -> None:
    if not isinstance(manifest, dict) or manifest.get("schema") != INPUT_SCHEMA:
        raise PreparationError("pinned native/browser provisioning manifest is invalid")
    if manifest.get("base_image") != lock["base_image"]:
        raise PreparationError("provisioned base image differs from the dependency lock")
    if manifest.get("playwright") != lock["playwright_version"]:
        raise PreparationError("provisioned Playwright version differs from the dependency lock")
    native = manifest.get("native")
    if not isinstance(native, dict) or set(native) != {"xray", "mihomo"}:
        raise PreparationError("provisioned native input inventory is incomplete")
    for name in ("xray", "mihomo"):
        observed = native[name]
        expected = lock["assets"][name]
        if (not isinstance(observed, dict)
                or observed.get("version") != expected["version"]
                or observed.get("source_archive_sha256") != expected["archive_sha256"]
                or observed.get("binary_sha256") != expected["binary_sha256"]):
            raise PreparationError(f"provisioned {name} input differs from the dependency lock")
    chromium = manifest.get("chromium")
    expected_chromium = lock["assets"]["chromium"]
    if (not isinstance(chromium, dict)
            or chromium.get("version") != expected_chromium["version"]
            or chromium.get("revision") != expected_chromium["revision"]
            or chromium.get("source_archive_sha256") != expected_chromium["archive_sha256"]
            or chromium.get("bundle_sha256") != expected_chromium["bundle_sha256"]
            or chromium.get("executable_sha256") != expected_chromium["executable_sha256"]):
        raise PreparationError("provisioned Chromium input differs from the dependency lock")
    measurements = manifest.get("asset_measurements")
    byte_fields = {
        "xray": {"source_archive_bytes", "executable_bytes"},
        "mihomo": {"source_archive_bytes", "executable_bytes"},
        "chromium": {"source_archive_bytes", "normalized_bundle_bytes", "expanded_content_bytes",
                     "chrome_executable_bytes"},
    }
    duration_fields = {
        "xray": {"download_seconds", "extract_seconds"},
        "mihomo": {"download_seconds", "extract_seconds"},
        "chromium": {"download_seconds", "normalize_seconds"},
    }
    if not isinstance(measurements, dict) or set(measurements) != set(byte_fields):
        raise PreparationError("provisioned asset measurement inventory is invalid")
    for name in byte_fields:
        item = measurements[name]
        if (not isinstance(item, dict) or set(item) != byte_fields[name] | duration_fields[name]
                or any(not isinstance(item[key], int) or isinstance(item[key], bool) or item[key] <= 0
                       for key in byte_fields[name])
                or any(not _valid_duration(item[key]) for key in duration_fields[name])):
            raise PreparationError(f"provisioned asset measurements are invalid for {name}")


def _owned_directory(path: Path, root: Path) -> Path:
    if path.is_symlink() or not path.is_dir():
        raise PreparationError(f"staging directory is missing or symlinked: {path.name}")
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(root):
        raise PreparationError(f"staging directory is outside RUNNER_TEMP: {path.name}")
    info = resolved.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise PreparationError(f"staging directory ownership or permissions are unsafe: {path.name}")
    return resolved


def prepare(*, runner_temp: Path, input_dir: Path, context: Path, receipt_path: Path) -> dict[str, Any]:
    temp_root = runner_temp.resolve(strict=True)
    temp_info = temp_root.stat()
    if temp_info.st_uid != os.getuid() or temp_info.st_mode & 0o022:
        raise PreparationError("RUNNER_TEMP ownership or permissions are unsafe")
    inputs_root = _owned_directory(input_dir, temp_root)
    if inputs_root != (temp_root / "fwrouter-acceptance-inputs").resolve(strict=True):
        raise PreparationError("provisioned input directory is not the fixed owned location")
    if context.exists() or context.is_symlink() or not context.resolve().is_relative_to(temp_root):
        raise PreparationError("dependency build context must be a fresh runner-owned path")
    if receipt_path.exists() or receipt_path.is_symlink() or not receipt_path.resolve().is_relative_to(temp_root):
        raise PreparationError("dependency receipt path must be fresh and runner-owned")

    lock = validate_lock(json.loads(LOCK_PATH.read_text(encoding="utf-8")))
    lock_digest = _canonical_digest(lock)
    provision_path = inputs_root / "inputs.json"
    provision_digest, _ = _file_digest(provision_path)
    provision = json.loads(provision_path.read_text(encoding="utf-8"))
    validate_provision_manifest(lock, provision)

    sources = {
        "native/xray": (inputs_root / "xray", lock["assets"]["xray"]["binary_sha256"], 0o755),
        "native/mihomo": (inputs_root / "mihomo", lock["assets"]["mihomo"]["binary_sha256"], 0o755),
        "native/chromium.tar": (inputs_root / "chromium.tar", lock["assets"]["chromium"]["bundle_sha256"], 0o644),
        "requirements-ci.txt": (ROOT / "tests/gates/requirements-ci.txt", lock["requirements"]["tests/gates/requirements-ci.txt"], 0o644),
        "requirements-browser.txt": (ROOT / "tests/acceptance/requirements-browser.txt", lock["requirements"]["tests/acceptance/requirements-browser.txt"], 0o644),
        "dependency-image.lock.json": (LOCK_PATH, None, 0o644),
        "Dockerfile": (ROOT / "tests/acceptance/Dependencies.Dockerfile", lock["dockerfile_sha256"], 0o644),
    }
    context.mkdir(mode=0o700)
    members: list[dict[str, Any]] = []
    try:
        for relative, (source, expected_digest, mode) in sources.items():
            observed_digest, byte_size = _file_digest(source)
            if expected_digest is not None and observed_digest != expected_digest:
                raise PreparationError(f"dependency context input digest mismatch: {relative}")
            target = context / relative
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            target.chmod(mode)
            copied_digest, copied_size = _file_digest(target)
            if copied_digest != observed_digest or copied_size != byte_size:
                raise PreparationError(f"dependency context copy mismatch: {relative}")
            members.append({"path": relative, "sha256": copied_digest, "bytes": copied_size})
    except BaseException:
        # The context is a fresh path below RUNNER_TEMP and belongs only to this
        # publisher process; leave failed inputs for the hosted job to expire.
        raise

    receipt = {
        "schema": "fwrouter-acceptance-dependency-inputs/v1",
        "lock_sha256": lock_digest,
        "provision_manifest_sha256": provision_digest,
        "asset_measurements": provision["asset_measurements"],
        "context_members": members,
        "context_bytes": sum(item["bytes"] for item in members),
        "image_digest": None,
        "status": "prepared",
    }
    receipt_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    receipt_path.write_text(json.dumps(receipt, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    receipt_path.chmod(0o600)
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runner-temp", type=Path, required=True)
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--context", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    try:
        receipt = prepare(
            runner_temp=args.runner_temp,
            input_dir=args.input_dir,
            context=args.context,
            receipt_path=args.receipt,
        )
    except (OSError, json.JSONDecodeError, PreparationError) as exc:
        raise SystemExit(f"dependency image preparation refused: {exc}") from exc
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
