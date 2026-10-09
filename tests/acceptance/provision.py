#!/usr/bin/env python3
"""Provision exact native/browser inputs for the hosted acceptance launcher.

This downloads only versioned public upstream release assets, checks their
published/recorded SHA-256 values, and writes launcher inputs to GITHUB_ENV.
It never executes the downloaded programs.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import os
import shutil
import stat
import tarfile
import time
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath


BASE_IMAGE = "python:3.11-bookworm@sha256:5887f265d8d44d8b4734d3658b21d668edf5aaf92e4945bc0c984afbfab105d3"
PLAYWRIGHT_VERSION = "1.55.0"
CHROMIUM_VERSION = "140.0.7339.16"
CHROMIUM_REVISION = "1187"
BINARY_SHA256 = {
    "xray": "3f650abf1fc4a4fbf5abe7fc9990a2658020907cd984214e9c075b4b00989fea",
    "mihomo": "12d97b7b7fa22cb4456e62c6e35db4be952dbb1c53eacaa9ef437c95d5068a9d",
}
CHROMIUM_BUNDLE_SHA256 = "647bf352134d85b31b35213471c67a07400f36b32e60b136e2651745f7320232"
CHROMIUM_BINARY_SHA256 = "2fa605e3639b8cfbe8037d0b8e0324dbf7f9e6ad7beb345374ecd26764e2d92b"
INPUTS = {
    "xray": {
        "version": "26.2.6",
        "url": "https://github.com/XTLS/Xray-core/releases/download/v26.2.6/Xray-linux-64.zip",
        "sha256": "29ce535b56e207a406ffa1c2d4842dcc410be003eff8ec508bb732abc9f8e385",
        "archive": "zip",
        "member": "xray",
    },
    "mihomo": {
        "version": "1.19.31",
        "url": "https://github.com/MetaCubeX/mihomo/releases/download/v1.19.31/mihomo-linux-amd64-v1-v1.19.31.gz",
        "sha256": "d4304c546c3cddcb6fafd4b4fddb0ba1a95ffa36606fda56d75db2e59ad24114",
        "archive": "gzip",
    },
    "chromium": {
        "version": CHROMIUM_VERSION,
        "revision": CHROMIUM_REVISION,
        "url": "https://playwright.download.prss.microsoft.com/dbazure/download/playwright/builds/chromium/1187/chromium-linux.zip",
        "sha256": "395e829e88f9ff3fc108cc9a3db49907afd9f5860066e9678694b01a406ee979",
        "archive": "zip",
    },
}
MAX_DOWNLOAD = {"xray": 32 * 1024 * 1024, "mihomo": 32 * 1024 * 1024,
                "chromium": 256 * 1024 * 1024}
MAX_CHROMIUM_EXPANDED = 2 * 1024 * 1024 * 1024
MAX_CHROMIUM_ENTRIES = 50000


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def download(name: str, destination: Path) -> None:
    spec = INPUTS[name]
    request = urllib.request.Request(spec["url"], headers={"User-Agent": "fwrouter-hosted-acceptance/1"})
    temporary = destination.with_suffix(destination.suffix + ".partial")
    for attempt in range(2):
        total = 0
        hasher = hashlib.sha256()
        try:
            with urllib.request.urlopen(request, timeout=90) as response, temporary.open("xb") as output:
                final_url = response.geturl()
                if not final_url.startswith("https://"):
                    raise ValueError(f"{name} download did not remain on HTTPS")
                while True:
                    block = response.read(1024 * 1024)
                    if not block:
                        break
                    total += len(block)
                    if total > MAX_DOWNLOAD[name]:
                        raise ValueError(f"{name} download exceeds its byte limit")
                    output.write(block)
                    hasher.update(block)
            if total == 0 or hasher.hexdigest() != spec["sha256"]:
                raise ValueError(f"{name} source archive SHA-256 mismatch")
            temporary.replace(destination)
            return
        except (OSError, TimeoutError):
            temporary.unlink(missing_ok=True)
            if attempt == 1:
                raise
            time.sleep(1)
        except ValueError:
            temporary.unlink(missing_ok=True)
            raise
    if not destination.is_file():
        raise ValueError(f"{name} source archive SHA-256 mismatch")


def extract_native(name: str, archive_path: Path, destination: Path) -> None:
    spec = INPUTS[name]
    if spec["archive"] == "gzip":
        expanded = 0
        with gzip.open(archive_path, "rb") as source, destination.open("xb") as output:
            while True:
                block = source.read(1024 * 1024)
                if not block:
                    break
                expanded += len(block)
                if expanded > 128 * 1024 * 1024:
                    raise ValueError(f"{name} decompressed binary exceeds its byte limit")
                output.write(block)
    else:
        with zipfile.ZipFile(archive_path) as archive:
            candidates = [item for item in archive.infolist() if PurePosixPath(item.filename).name == spec["member"]]
            if len(candidates) != 1:
                raise ValueError(f"{name} archive must contain exactly one {spec['member']} binary")
            item = candidates[0]
            mode = (item.external_attr >> 16) & 0xFFFF
            if item.is_dir() or stat.S_ISLNK(mode) or item.file_size > 64 * 1024 * 1024:
                raise ValueError(f"{name} archive contains an unsafe binary entry")
            with archive.open(item) as source, destination.open("xb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
    destination.chmod(0o755)


def make_chromium_bundle(archive_path: Path, destination: Path) -> tuple[str, int]:
    """Normalize the Playwright zip's chrome-linux root to launcher's chrome-linux64."""
    digest_source = hashlib.sha256()
    total = 0
    count = 0
    with zipfile.ZipFile(archive_path) as archive, destination.open("xb") as output:
        with tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as bundle:
            for item in sorted(archive.infolist(), key=lambda value: value.filename):
                name = item.filename.rstrip("/")
                if not name:
                    continue
                path = PurePosixPath(name)
                if path.is_absolute() or ".." in path.parts or path.parts[0] != "chrome-linux":
                    raise ValueError("Chromium archive contains an unsafe or unexpected path")
                count += 1
                total += item.file_size
                if count > MAX_CHROMIUM_ENTRIES or total > MAX_CHROMIUM_EXPANDED:
                    raise ValueError("Chromium archive exceeds expanded size or entry bounds")
                mode = (item.external_attr >> 16) & 0xFFFF
                file_type = stat.S_IFMT(mode)
                is_dir = item.is_dir()
                if file_type not in (0, stat.S_IFDIR if is_dir else stat.S_IFREG):
                    raise ValueError("Chromium archive contains a symlink or special file")
                relative = PurePosixPath("chrome-linux64", *path.parts[1:])
                info = tarfile.TarInfo(str(relative) + ("/" if is_dir else ""))
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                info.mtime = 0
                info.mode = 0o755 if is_dir or mode & 0o111 else 0o644
                if is_dir:
                    info.type = tarfile.DIRTYPE
                    bundle.addfile(info)
                else:
                    info.size = item.file_size
                    with archive.open(item) as source:
                        bundle.addfile(info, source)
    destination.chmod(0o644)
    with zipfile.ZipFile(archive_path) as archive:
        chrome = [x for x in archive.infolist() if x.filename == "chrome-linux/chrome"]
        if len(chrome) != 1 or chrome[0].file_size == 0:
            raise ValueError("Chromium archive lacks the expected browser executable")
    return digest(destination), total


def chromium_executable_digest(bundle_path: Path) -> tuple[str, int]:
    hasher = hashlib.sha256()
    with tarfile.open(bundle_path, mode="r:*") as bundle:
        member = bundle.getmember("chrome-linux64/chrome")
        if not member.isfile() or member.size > MAX_CHROMIUM_EXPANDED:
            raise ValueError("normalized Chromium bundle has an unsafe executable entry")
        source = bundle.extractfile(member)
        if source is None:
            raise ValueError("normalized Chromium executable cannot be read")
        with source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                hasher.update(block)
    return hasher.hexdigest(), member.size


def valid_asset_measurements(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != {"xray", "mihomo", "chromium"}:
        return False
    byte_fields = {
        "xray": {"source_archive_bytes", "executable_bytes"},
        "mihomo": {"source_archive_bytes", "executable_bytes"},
        "chromium": {"source_archive_bytes", "normalized_bundle_bytes", "expanded_content_bytes",
                     "chrome_executable_bytes"},
    }
    time_fields = {
        "xray": {"download_seconds", "extract_seconds"},
        "mihomo": {"download_seconds", "extract_seconds"},
        "chromium": {"download_seconds", "normalize_seconds"},
    }
    def valid_duration(duration: object) -> bool:
        if not isinstance(duration, (int, float)) or isinstance(duration, bool):
            return False
        try:
            return math.isfinite(float(duration)) and duration >= 0
        except (OverflowError, TypeError, ValueError):
            return False

    for name in byte_fields:
        item = value.get(name)
        if (not isinstance(item, dict) or set(item) != byte_fields[name] | time_fields[name]
                or any(not isinstance(item[field], int) or isinstance(item[field], bool) or item[field] <= 0
                       for field in byte_fields[name])
                or any(not valid_duration(item[field]) for field in time_fields[name])):
            return False
    return True


def write_github_env(path: Path, values: dict[str, str]) -> None:
    if path.is_symlink() or not path.is_file():
        raise ValueError("GITHUB_ENV must be an existing regular file")
    with path.open("a", encoding="utf-8") as stream:
        for key, value in values.items():
            if "\n" in value or "\r" in value:
                raise ValueError(f"newline is forbidden in GitHub env value {key}")
            stream.write(f"{key}={value}\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runner-temp", type=Path, required=True)
    parser.add_argument("--github-env", type=Path, required=True)
    args = parser.parse_args()
    runner_temp = args.runner_temp.resolve(strict=True)
    if args.github_env.is_symlink() or not args.github_env.resolve(strict=True).is_relative_to(runner_temp):
        raise SystemExit("GITHUB_ENV must be a regular file owned by RUNNER_TEMP")
    if runner_temp.stat().st_uid != os.getuid() or runner_temp.stat().st_mode & 0o022:
        raise SystemExit("RUNNER_TEMP ownership or permissions are unsafe")
    root = runner_temp / "fwrouter-acceptance-inputs"
    if root.exists() or root.is_symlink():
        raise SystemExit("provisioning directory already exists; use a fresh hosted job")
    root.mkdir(mode=0o700)
    outputs: dict[str, str] = {"FWROUTER_ACCEPTANCE_BASE_IMAGE": BASE_IMAGE,
                               "FWROUTER_ACCEPTANCE_PLAYWRIGHT_VERSION": PLAYWRIGHT_VERSION,
                               "FWROUTER_ACCEPTANCE_CHROMIUM_VERSION": CHROMIUM_VERSION}
    source_hashes: dict[str, str] = {}
    asset_measurements: dict[str, dict[str, int | float]] = {}
    try:
        for name in INPUTS:
            archive = root / f"{name}.source"
            download_started = time.monotonic()
            download(name, archive)
            download_seconds = round(time.monotonic() - download_started, 3)
            source_hashes[name] = digest(archive)
            if name in ("xray", "mihomo"):
                binary = root / name
                extract_started = time.monotonic()
                extract_native(name, archive, binary)
                extract_seconds = round(time.monotonic() - extract_started, 3)
                observed_binary_sha256 = digest(binary)
                if observed_binary_sha256 != BINARY_SHA256[name]:
                    raise ValueError(f"{name} extracted binary SHA-256 mismatch")
                asset_measurements[name] = {
                    "source_archive_bytes": archive.stat().st_size,
                    "executable_bytes": binary.stat().st_size,
                    "download_seconds": download_seconds,
                    "extract_seconds": extract_seconds,
                }
                outputs[f"FWROUTER_ACCEPTANCE_{name.upper()}_BINARY"] = str(binary)
                outputs[f"FWROUTER_ACCEPTANCE_{name.upper()}_SHA256"] = observed_binary_sha256
                outputs[f"FWROUTER_ACCEPTANCE_{name.upper()}_VERSION"] = INPUTS[name]["version"]
                archive.unlink()
            else:
                bundle = root / "chromium.tar"
                outputs["FWROUTER_ACCEPTANCE_CHROMIUM_BUNDLE"] = str(bundle)
                normalize_started = time.monotonic()
                observed_bundle_sha256, expanded_content_bytes = make_chromium_bundle(archive, bundle)
                if observed_bundle_sha256 != CHROMIUM_BUNDLE_SHA256:
                    raise ValueError("normalized Chromium bundle SHA-256 mismatch")
                outputs["FWROUTER_ACCEPTANCE_CHROMIUM_BUNDLE_SHA256"] = observed_bundle_sha256
                observed_chromium_sha256, chrome_executable_bytes = chromium_executable_digest(bundle)
                normalize_seconds = round(time.monotonic() - normalize_started, 3)
                if observed_chromium_sha256 != CHROMIUM_BINARY_SHA256:
                    raise ValueError("Chromium executable SHA-256 mismatch")
                asset_measurements[name] = {
                    "source_archive_bytes": archive.stat().st_size,
                    "normalized_bundle_bytes": bundle.stat().st_size,
                    "expanded_content_bytes": expanded_content_bytes,
                    "chrome_executable_bytes": chrome_executable_bytes,
                    "download_seconds": download_seconds,
                    "normalize_seconds": normalize_seconds,
                }
                outputs["FWROUTER_ACCEPTANCE_CHROMIUM_BINARY_SHA256"] = observed_chromium_sha256
                archive.unlink()
        if not valid_asset_measurements(asset_measurements):
            raise ValueError("provisioned asset measurements failed their numeric schema")
        manifest = {"schema": "fwrouter-hosted-inputs/v1", "base_image": BASE_IMAGE,
                    "playwright": PLAYWRIGHT_VERSION,
                    "asset_measurements": asset_measurements,
                    "chromium": {"version": CHROMIUM_VERSION, "revision": CHROMIUM_REVISION,
                                 "source_archive_sha256": INPUTS["chromium"]["sha256"],
                                 "bundle_sha256": outputs["FWROUTER_ACCEPTANCE_CHROMIUM_BUNDLE_SHA256"],
                                 "executable_sha256": outputs["FWROUTER_ACCEPTANCE_CHROMIUM_BINARY_SHA256"]},
                    "native": {name: {"version": INPUTS[name]["version"],
                                      "source_archive_sha256": INPUTS[name]["sha256"],
                                      "binary_sha256": outputs[f"FWROUTER_ACCEPTANCE_{name.upper()}_SHA256"]}
                               for name in ("xray", "mihomo")}}
        manifest_path = root / "inputs.json"
        manifest_path.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        manifest_path.chmod(0o600)
        write_github_env(args.github_env, outputs)
        print(json.dumps({"status": "prepared", "source_sha256": source_hashes,
                          "asset_measurements": asset_measurements}, sort_keys=True))
        return 0
    except BaseException:
        shutil.rmtree(root, ignore_errors=True)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
