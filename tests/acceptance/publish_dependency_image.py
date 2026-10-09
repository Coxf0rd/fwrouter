#!/usr/bin/env python3
"""Build, verify, and publish the source-free acceptance dependency image."""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import selectors
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from prepare_dependency_image import LOCK_PATH, _canonical_digest, _file_digest, validate_lock


OWNER = "Coxf0rd/fwrouter"
BRANCH = "refs/heads/stage/test-architecture-ci-stabilization"
MARKER = "ci:publish-dependencies"
REGISTRY_IMAGE = "ghcr.io/coxf0rd/fwrouter-acceptance-dependencies"
OWNER_LABEL = "io.fwrouter.acceptance.dependencies.lock-sha256"
BASE_LABEL = "org.opencontainers.image.base.name"
MAX_JSON_BYTES = 512 * 1024
MAX_DIAGNOSTIC_BYTES = 32 * 1024
SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}")


class PublishError(RuntimeError):
    """The trusted publisher refused its environment or image inputs."""


def validate_publisher_environment(env: dict[str, str]) -> None:
    if env.get("GITHUB_ACTIONS") != "true" or env.get("RUNNER_ENVIRONMENT") != "github-hosted":
        raise PublishError("publisher requires a GitHub-hosted runner")
    if env.get("GITHUB_REPOSITORY") != OWNER or env.get("GITHUB_REF") != BRANCH:
        raise PublishError("publisher is restricted to the authorized repository branch")
    if env.get("GITHUB_EVENT_NAME") != "push":
        raise PublishError("publisher requires the authorized marker commit")
    if env.get("FWROUTER_DEPENDENCY_PUBLISH_MARKER") != MARKER:
        raise PublishError("publisher authorization marker is missing")
    if not env.get("GITHUB_TOKEN"):
        raise PublishError("publisher package credential is unavailable")
    if not env.get("RUNNER_TEMP"):
        raise PublishError("publisher runner-owned temporary directory is unavailable")


def _run_json(argv: list[str], *, timeout: int = 20) -> Any:
    try:
        result = subprocess.run(argv, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PublishError("bounded Docker metadata command failed") from exc
    if result.returncode or len(result.stdout) > MAX_JSON_BYTES:
        raise PublishError("Docker metadata command failed or exceeded its bound")
    try:
        return json.loads(result.stdout)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise PublishError("Docker metadata command returned invalid JSON") from exc


def _write_runner_receipt(path: Path, value: dict[str, Any], runner_temp: Path) -> None:
    if path.name in {"", ".", ".."}:
        raise PublishError("publisher receipt filename is invalid")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent = path.parent.resolve(strict=True)
    info = parent.stat()
    if (not parent.is_relative_to(runner_temp) or info.st_uid != os.getuid()
            or info.st_mode & 0o022):
        raise PublishError("publisher receipt directory is outside the owned runner temp root")
    target = parent / path.name
    try:
        with target.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(value, sort_keys=True, indent=2) + "\n")
        target.chmod(0o600)
    except FileExistsError as exc:
        raise PublishError("publisher receipt path must be fresh") from exc


def _valid_asset_measurements(value: Any) -> bool:
    bytes_by_asset = {
        "xray": {"source_archive_bytes", "executable_bytes"},
        "mihomo": {"source_archive_bytes", "executable_bytes"},
        "chromium": {"source_archive_bytes", "normalized_bundle_bytes", "expanded_content_bytes",
                     "chrome_executable_bytes"},
    }
    durations_by_asset = {
        "xray": {"download_seconds", "extract_seconds"},
        "mihomo": {"download_seconds", "extract_seconds"},
        "chromium": {"download_seconds", "normalize_seconds"},
    }
    if not isinstance(value, dict) or set(value) != set(bytes_by_asset):
        return False
    for name in bytes_by_asset:
        item = value.get(name)
        if (not isinstance(item, dict) or set(item) != bytes_by_asset[name] | durations_by_asset[name]
                or any(not isinstance(item.get(key), int) or isinstance(item.get(key), bool)
                       or item[key] <= 0 for key in bytes_by_asset[name])):
            return False
        for key in durations_by_asset[name]:
            duration = item.get(key)
            if not isinstance(duration, (int, float)) or isinstance(duration, bool):
                return False
            try:
                if not math.isfinite(float(duration)) or duration < 0:
                    return False
            except (OverflowError, TypeError, ValueError):
                return False
    return True


def _safe_command_output(raw: bytes) -> str:
    detail = raw.decode("utf-8", "replace")
    secret_values = [value for name, value in os.environ.items()
                     if any(part in name.lower() for part in ("token", "password", "secret", "credential"))
                     and value]
    for secret in secret_values:
        detail = detail.replace(secret, "<redacted>")
    detail = re.sub(r"(?i)(bearer\s+)\S+", r"\1<redacted>", detail)
    detail = re.sub(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", "<email>", detail, flags=re.I)
    return detail[-4000:].strip()


def _run_output(argv: list[str], *, timeout: int = 900, missing_manifest_ok: bool = False,
                env: dict[str, str] | None = None) -> str | None:
    # Drain arbitrarily verbose progress while retaining only bounded tails.
    try:
        process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    except OSError as exc:
        raise PublishError("bounded Docker build or publish operation failed") from exc
    assert process.stdout is not None and process.stderr is not None
    buffers = {process.stdout: bytearray(), process.stderr: bytearray()}
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    selector.register(process.stderr, selectors.EVENT_READ)
    deadline = time.monotonic() + timeout
    timed_out = False
    while selector.get_map():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            process.kill()
            break
        for key, _events in selector.select(min(0.25, remaining)):
            chunk = os.read(key.fileobj.fileno(), 8192)
            if not chunk:
                selector.unregister(key.fileobj)
                continue
            buffer = buffers[key.fileobj]
            buffer.extend(chunk)
            if len(buffer) > MAX_DIAGNOSTIC_BYTES:
                del buffer[:-MAX_DIAGNOSTIC_BYTES]
    process.wait()
    selector.close()
    process.stdout.close()
    process.stderr.close()
    if timed_out:
        raise PublishError("bounded Docker build or publish operation timed out")
    safe_output = "\n".join(part for part in (
        _safe_command_output(bytes(buffers[process.stdout])),
        _safe_command_output(bytes(buffers[process.stderr])),
    ) if part)
    if process.returncode:
        lowered = safe_output.lower()
        if missing_manifest_ok and any(marker in lowered for marker in (
                "manifest unknown", "no such manifest", "name unknown")):
            return None
        suffix = f": {safe_output}" if safe_output else ""
        raise PublishError(f"bounded Docker operation failed with exit {process.returncode}{suffix}")
    return safe_output


def _check_local_image(image_tag: str, *, lock: dict[str, Any], lock_digest: str) -> dict[str, Any]:
    rows = _run_json(["docker", "image", "inspect", image_tag])
    if not isinstance(rows, list) or len(rows) != 1:
        raise PublishError("built dependency image inspect returned an unexpected result")
    image = rows[0]
    labels = image.get("Config", {}).get("Labels", {})
    if (image.get("Os") != "linux" or image.get("Architecture") != "amd64"
            or labels.get(OWNER_LABEL) != lock_digest
            or labels.get(BASE_LABEL) != lock["base_image"]
            or not isinstance(image.get("Id"), str)
            or not SHA256_RE.fullmatch(image["Id"])):
        raise PublishError("built dependency image platform or lock labels do not match")
    return image


def _read_image_manifest(image_tag: str, *, lock: dict[str, Any], lock_digest: str) -> dict[str, Any]:
    argv = [
        "docker", "run", "--rm", "--network=none", "--read-only", "--user", "10001:10001",
        "--memory=256m", "--memory-swap=256m", "--cpus=0.5", "--pids-limit=32",
        "--cap-drop=ALL", "--security-opt=no-new-privileges", "--entrypoint", "python", image_tag,
        "-c", "from pathlib import Path; print(Path('/opt/fwrouter-test/dependency-manifest.json').read_text())",
    ]
    try:
        result = subprocess.run(argv, capture_output=True, timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PublishError("bounded dependency-manifest verification failed") from exc
    if result.returncode or len(result.stdout) > MAX_JSON_BYTES:
        raise PublishError("dependency image manifest could not be read within its bound")
    try:
        manifest = json.loads(result.stdout)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise PublishError("dependency image manifest is invalid JSON") from exc
    if (not isinstance(manifest, dict)
            or manifest.get("schema") != "fwrouter-acceptance-dependency-image/v1"
            or manifest.get("lock_sha256") != lock_digest
            or manifest.get("base_image") != lock["base_image"]
            or manifest.get("platform") != lock["platform"]
            or manifest.get("debian_snapshot") != lock["debian_snapshot"]
            or manifest.get("requirements") != lock["requirements"]
            or manifest.get("assets") != lock["assets"]
            or manifest.get("playwright_version") != lock["playwright_version"]
            or not isinstance(manifest.get("installed_packages"), list)
            or not manifest["installed_packages"]
            or any(not isinstance(item, str) or "=" not in item for item in manifest["installed_packages"])):
        raise PublishError("dependency image manifest differs from the reviewed lock")
    if manifest["installed_packages"] != sorted(manifest["installed_packages"]):
        raise PublishError("dependency image package manifest is not sorted")
    return manifest


def _anonymous_pull(image_ref: str, *, runner_temp: Path) -> tuple[bool, float, str | None]:
    config = runner_temp / "fwrouter-anonymous-docker-config"
    if config.exists() or config.is_symlink():
        raise PublishError("anonymous pull config path is not fresh")
    config.mkdir(mode=0o700)
    config_info = config.stat()
    if config_info.st_uid != os.getuid() or config_info.st_mode & 0o077:
        raise PublishError("anonymous Docker config directory is not privately owned")
    env = dict(os.environ)
    for name in tuple(env):
        if any(part in name.lower() for part in ("token", "password", "secret", "credential")):
            env.pop(name, None)
    env["DOCKER_CONFIG"] = str(config)
    started = time.monotonic()
    try:
        _run_output(["docker", "--config", str(config), "pull", "--platform", "linux/amd64", image_ref],
                    timeout=600, env=env)
    except PublishError as exc:
        elapsed = round(time.monotonic() - started, 3)
        return False, elapsed, _safe_command_output(str(exc).encode("utf-8", "replace"))
    finally:
        shutil.rmtree(config, ignore_errors=True)
    return True, round(time.monotonic() - started, 3), None


def publish(*, context: Path, input_receipt_path: Path, receipt_path: Path) -> dict[str, Any]:
    env = dict(os.environ)
    validate_publisher_environment(env)
    lock = validate_lock(json.loads(LOCK_PATH.read_text(encoding="utf-8")))
    lock_digest = _canonical_digest(lock)
    image_tag = f"{REGISTRY_IMAGE}:deps-{lock_digest}"
    runner_temp = Path(env.get("RUNNER_TEMP", "")).resolve(strict=True)
    temp_info = runner_temp.stat()
    if temp_info.st_uid != os.getuid() or temp_info.st_mode & 0o022:
        raise PublishError("RUNNER_TEMP ownership or permissions are unsafe")
    if context.is_symlink() or not context.is_dir():
        raise PublishError("dependency build context must be an owned directory")
    context_info = context.stat()
    context = context.resolve(strict=True)
    if (not context.is_relative_to(runner_temp) or context_info.st_uid != os.getuid()
            or context_info.st_mode & 0o022):
        raise PublishError("dependency build context is outside the owned runner temp root")
    if receipt_path.exists() or receipt_path.is_symlink():
        raise PublishError("dependency receipt path must be fresh")
    if not receipt_path.resolve().is_relative_to(runner_temp):
        raise PublishError("dependency receipt path is outside RUNNER_TEMP")
    if input_receipt_path.is_symlink() or not input_receipt_path.is_file():
        raise PublishError("prepared input receipt is missing or symlinked")
    input_receipt_path = input_receipt_path.resolve(strict=True)
    input_info = input_receipt_path.stat()
    if (not input_receipt_path.is_relative_to(runner_temp) or input_info.st_uid != os.getuid()
            or input_info.st_mode & 0o022 or input_info.st_size > MAX_JSON_BYTES):
        raise PublishError("prepared input receipt is outside the owned bounded runner path")
    input_receipt_digest, _ = _file_digest(input_receipt_path)
    input_receipt = json.loads(input_receipt_path.read_text(encoding="utf-8"))
    if (not isinstance(input_receipt, dict)
            or input_receipt.get("schema") != "fwrouter-acceptance-dependency-inputs/v1"
            or input_receipt.get("status") != "prepared"
            or input_receipt.get("lock_sha256") != lock_digest
            or input_receipt.get("image_digest") is not None
            or not SHA256_RE.fullmatch(str(input_receipt.get("provision_manifest_sha256", "")))
            or not _valid_asset_measurements(input_receipt.get("asset_measurements"))):
        raise PublishError("prepared input receipt does not match the locked dependency build")
    expected_members = {
        "Dockerfile", "dependency-image.lock.json", "requirements-ci.txt",
        "requirements-browser.txt", "native",
    }
    members = {path.name for path in context.iterdir()}
    if members != expected_members or any(path.is_symlink() for path in context.rglob("*")):
        raise PublishError("dependency build context contains missing, extra, or symlinked inputs")
    expected_files = {
        "Dockerfile", "dependency-image.lock.json", "requirements-ci.txt",
        "requirements-browser.txt", "native/xray", "native/mihomo", "native/chromium.tar",
    }
    actual_files = {path.relative_to(context).as_posix() for path in context.rglob("*") if path.is_file()}
    if actual_files != expected_files or any(not path.is_file() and not path.is_dir()
                                             for path in context.rglob("*")):
        raise PublishError("dependency build context file inventory is not exact")
    receipt_members = input_receipt.get("context_members")
    if (not isinstance(receipt_members, list) or len(receipt_members) != len(expected_files)
            or any(not isinstance(item, dict) or not isinstance(item.get("path"), str)
                   for item in receipt_members)
            or {item["path"] for item in receipt_members} != expected_files):
        raise PublishError("prepared receipt context member list is incomplete")
    for item in receipt_members:
        relative = item["path"]
        if (not isinstance(relative, str) or not isinstance(item.get("sha256"), str)
                or not SHA256_RE.fullmatch(item["sha256"])
                or not isinstance(item.get("bytes"), int) or isinstance(item.get("bytes"), bool)
                or item["bytes"] <= 0):
            raise PublishError("prepared context member metadata is invalid")
        try:
            observed_digest, observed_size = _file_digest(context / relative)
        except (OSError, ValueError) as exc:
            raise PublishError("prepared context member could not be reverified") from exc
        if item.get("sha256") != observed_digest or item.get("bytes") != observed_size:
            raise PublishError("prepared context member changed after receipt creation")
    context_size = sum(item["bytes"] for item in receipt_members)
    if input_receipt.get("context_bytes") != context_size:
        raise PublishError("prepared context byte count differs from its member receipts")
    native_members = {path.name for path in (context / "native").iterdir()}
    if native_members != {"xray", "mihomo", "chromium.tar"} or any(
            path.is_symlink() or not path.is_file() for path in (context / "native").iterdir()):
        raise PublishError("dependency native input directory contains an unreviewed file")

    existing = _run_output(["docker", "manifest", "inspect", image_tag], timeout=30,
                           missing_manifest_ok=True)
    if existing is not None:
        raise PublishError("dependency lock tag already exists in GHCR; refusing to rebuild or overwrite it")

    build_started = time.monotonic()
    _run_output([
        "docker", "build", "--pull", "--platform", lock["platform"],
        "--file", str(context / "Dockerfile"),
        "--build-arg", f"BASE_IMAGE={lock['base_image']}",
        "--build-arg", f"DEBIAN_SNAPSHOT={lock['debian_snapshot']['url']}",
        "--build-arg", f"DEPENDENCY_LOCK_SHA256={lock_digest}",
        "--tag", image_tag, str(context),
    ], timeout=1200)
    build_seconds = round(time.monotonic() - build_started, 3)
    image = _check_local_image(image_tag, lock=lock, lock_digest=lock_digest)
    dependency_manifest = _read_image_manifest(image_tag, lock=lock, lock_digest=lock_digest)

    push_started = time.monotonic()
    push_output = _run_output(["docker", "push", image_tag], timeout=300)
    push_seconds = round(time.monotonic() - push_started, 3)
    digests = re.findall(r"digest:\s+(sha256:[0-9a-f]{64})", push_output)
    if len(digests) != 1:
        try:
            pushed = _run_json(["docker", "image", "inspect", image_tag])
        except PublishError as exc:
            raise PublishError(f"registry digest inspect failed; bounded push output: {push_output}") from exc
        repo_digests = pushed[0].get("RepoDigests", []) if isinstance(pushed, list) and len(pushed) == 1 else []
        digests = [item.split("@", 1)[1] for item in repo_digests
                   if isinstance(item, str) and item.startswith(REGISTRY_IMAGE + "@")
                   and re.fullmatch(r"sha256:[0-9a-f]{64}", item.split("@", 1)[1])]
    if len(set(digests)) != 1:
        raise PublishError(f"registry push did not return one immutable manifest digest; bounded output: {push_output}")
    immutable_ref = f"{REGISTRY_IMAGE}@{digests[0]}"
    anonymous_pull_ok, anonymous_pull_seconds, anonymous_pull_error = _anonymous_pull(
        immutable_ref, runner_temp=runner_temp)
    receipt = {
        "schema": "fwrouter-acceptance-dependency-publish-receipt/v1",
        "status": "published" if anonymous_pull_ok else "published_visibility_unverified",
        "repository": OWNER,
        "ref": immutable_ref,
        "tag_hint": image_tag,
        "lock_sha256": lock_digest,
        "input_receipt_sha256": input_receipt_digest,
        "context_bytes": context_size,
        "context_members": receipt_members,
        "asset_measurements": input_receipt["asset_measurements"],
        "image_config_id": image["Id"],
        "platform": lock["platform"],
        "image_bytes": image.get("Size"),
        "build_seconds": build_seconds,
        "push_seconds": push_seconds,
        "anonymous_pull_seconds": anonymous_pull_seconds,
        "anonymous_pull_status": "passed" if anonymous_pull_ok else "blocked_or_unverified",
        "anonymous_pull_diagnostic": anonymous_pull_error,
        "base_image": lock["base_image"],
        "debian_snapshot": lock["debian_snapshot"],
        "installed_packages": dependency_manifest["installed_packages"],
        "assets": lock["assets"],
        "requirements": lock["requirements"],
    }
    if (not isinstance(receipt["image_bytes"], int) or isinstance(receipt["image_bytes"], bool)
            or receipt["image_bytes"] <= 0
            or any(not isinstance(receipt[field], (int, float)) or isinstance(receipt[field], bool)
                   or not math.isfinite(float(receipt[field])) or receipt[field] < 0
                   for field in ("build_seconds", "push_seconds"))):
        raise PublishError("published image measurements are invalid")
    _write_runner_receipt(receipt_path, receipt, runner_temp)
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", type=Path, required=True)
    parser.add_argument("--input-receipt", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    try:
        receipt = publish(context=args.context, input_receipt_path=args.input_receipt,
                          receipt_path=args.receipt)
    except Exception as exc:
        diagnostic = _safe_command_output(str(exc).encode("utf-8", "replace"))
        failure = {
            "schema": "fwrouter-acceptance-dependency-publish-receipt/v1",
            "status": "failed",
            "stage": "publisher",
            "error_type": type(exc).__name__,
            "diagnostic": diagnostic,
        }
        try:
            runner_temp = Path(os.environ.get("RUNNER_TEMP", "")).resolve(strict=True)
            _write_runner_receipt(args.receipt, failure, runner_temp)
        except Exception:
            pass
        raise SystemExit(f"dependency image publisher failed: {diagnostic or type(exc).__name__}") from exc
    print(json.dumps(receipt, sort_keys=True))
    if receipt["status"] != "published":
        raise SystemExit("dependency image was published but anonymous registry pull was not verified; consumer remains NOTREADY")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
