#!/usr/bin/env python3
"""Fail-closed build-time qualification for the immutable acceptance base."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import re
import subprocess
from pathlib import Path
from typing import Any


ROOT = Path("/opt/fwrouter-test")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
IMAGE_REF_RE = re.compile(
    r"^ghcr\.io/coxf0rd/fwrouter-acceptance-dependencies@sha256:[0-9a-f]{64}$"
)


def canonical_digest(value: dict[str, Any]) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must be a JSON object")
    return value


def validate(base_image: str, source_revision: str) -> None:
    dependency_lock = read_json(Path("/tmp/fwrouter-dependency/dependency-image.lock.json"))
    consumer_lock = read_json(Path("/tmp/fwrouter-dependency/dependency-image-consumer.lock.json"))
    manifest = read_json(ROOT / "dependency-manifest.json")
    if not IMAGE_REF_RE.fullmatch(base_image):
        raise ValueError("application base image is not the locked GHCR digest form")
    if (consumer_lock.get("schema") != "fwrouter-acceptance-dependency-consumer-ref/v1"
            or consumer_lock.get("image_ref") != base_image
            or consumer_lock.get("platform") != "linux/amd64"
            or consumer_lock.get("lock_sha256") != canonical_digest(dependency_lock)):
        raise ValueError("application base image differs from the checked-in consumer lock")
    if (manifest.get("schema") != "fwrouter-acceptance-dependency-image/v1"
            or manifest.get("lock_sha256") != canonical_digest(dependency_lock)
            or manifest.get("base_image") != dependency_lock.get("base_image")
            or manifest.get("platform") != dependency_lock.get("platform")
            or manifest.get("debian_snapshot") != dependency_lock.get("debian_snapshot")
            or manifest.get("requirements") != dependency_lock.get("requirements")
            or manifest.get("assets") != dependency_lock.get("assets")
            or manifest.get("playwright_version") != dependency_lock.get("playwright_version")):
        raise ValueError("dependency image manifest differs from the reviewed dependency lock")
    if manifest.get("installed_packages") != sorted(set(manifest.get("installed_packages", []))):
        raise ValueError("dependency image package inventory is not sorted and unique")
    package_file = ROOT / "dpkg-packages.txt"
    if package_file.read_text(encoding="utf-8").splitlines() != manifest["installed_packages"]:
        raise ValueError("dependency image package inventory differs from its package receipt")
    package_query = subprocess.check_output(
        ["dpkg-query", "-W", "-f=${binary:Package}=${Version}\\n"], text=True
    ).splitlines()
    if sorted(package_query) != manifest["installed_packages"]:
        raise ValueError("installed dpkg inventory differs from the dependency manifest")
    requirements = dependency_lock["requirements"]
    for relative, expected in requirements.items():
        if not SHA256_RE.fullmatch(expected):
            raise ValueError("dependency requirements lock contains an invalid hash")
        path = Path("/workspace") / relative
        if file_digest(path) != expected:
            raise ValueError(f"fresh source requirements differ from the dependency lock: {relative}")
    assets = dependency_lock["assets"]
    for name, path in (("xray", ROOT / "bin/xray"), ("mihomo", ROOT / "bin/mihomo"),
                       ("chromium", ROOT / "chromium/chrome-linux64/chrome")):
        observed = file_digest(path)
        expected = assets[name]["binary_sha256"] if name != "chromium" else assets[name]["executable_sha256"]
        if not SHA256_RE.fullmatch(expected) or observed != expected:
            raise ValueError(f"dependency image {name} executable hash differs from its lock")
    if importlib.metadata.version("playwright") != dependency_lock["playwright_version"]:
        raise ValueError("dependency image Playwright version differs from its lock")
    revision = read_json(Path("/workspace/.fwrouter-acceptance-revision"))
    if (revision.get("source_revision") != source_revision
            or not SHA256_RE.fullmatch(str(revision.get("source_manifest_sha256", "")))
            or not SHA256_RE.fullmatch(str(revision.get("ui_tree_sha256", "")))):
        raise ValueError("fresh application source sidecar is malformed or bound to another commit")
    architecture = subprocess.check_output(["dpkg", "--print-architecture"], text=True).strip()
    if architecture != "amd64":
        raise ValueError("dependency image is not running on amd64")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-image", required=True)
    parser.add_argument("--source-revision", required=True)
    args = parser.parse_args()
    validate(args.base_image, args.source_revision)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
