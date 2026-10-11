#!/usr/bin/env python3
"""Anonymously qualify the immutable, source-free acceptance dependency image."""
from __future__ import annotations

import json
import os
import platform
import re
import selectors
import shutil
import stat
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from prepare_dependency_image import LOCK_PATH, PreparationError, _canonical_digest, validate_lock
from publish_dependency_image import (
    REGISTRY_IMAGE,
    PublishError,
    validate_dependency_image_metadata,
    validate_dependency_manifest,
)


ROOT = Path(__file__).resolve().parents[2]
CONSUMER_REFERENCE_PATH = ROOT / "tests/acceptance/dependency-image-consumer.lock.json"
REFERENCE_SCHEMA = "fwrouter-acceptance-dependency-consumer-ref/v1"
CONSUMER_RECEIPT_SCHEMA = "fwrouter-acceptance-dependency-consumer-receipt/v1"
MAX_REFERENCE_BYTES = 16 * 1024
MAX_DOCKER_OUTPUT_BYTES = 128 * 1024
SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
REJECTED_DOCKER_ENV = {
    "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_AUTH_CONFIG", "DOCKER_CERT_PATH",
    "DOCKER_TLS", "DOCKER_TLS_VERIFY", "DOCKER_CONFIG", "CONTAINER_HOST", "BUILDKIT_HOST",
}


class ConsumerError(RuntimeError):
    """The anonymous consumer refused an unqualified image or runner."""


def validate_consumer_environment(env: dict[str, str], *, system: str | None = None,
                                  machine: str | None = None) -> None:
    if (not isinstance(env, dict)
            or any(not isinstance(name, str) or not isinstance(value, str) for name, value in env.items())):
        raise ConsumerError("consumer environment has an invalid shape")
    if (env.get("GITHUB_ACTIONS") != "true" or env.get("RUNNER_ENVIRONMENT") != "github-hosted"
            or (system or platform.system()) != "Linux"
            or (machine or platform.machine()).lower() not in {"x86_64", "amd64"}):
        raise ConsumerError("dependency image consumer requires GitHub-hosted Linux amd64")
    if any(name in env for name in REJECTED_DOCKER_ENV):
        raise ConsumerError("Docker override or credential environment is not allowed for anonymous consumption")


def _validated_runner_temp(path: Path) -> Path:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ConsumerError("runner temporary directory is unavailable") from exc
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise ConsumerError("runner temporary path must be a real directory")
    root = path.resolve(strict=True)
    if Path(os.path.abspath(path)) != root:
        raise ConsumerError("runner temporary path must not contain symlinks")
    info = root.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise ConsumerError("runner temporary directory ownership or permissions are unsafe")
    return root


def load_locked_dependencies() -> dict[str, Any]:
    try:
        lock_value = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
        return validate_lock(lock_value, root=ROOT)
    except (OSError, json.JSONDecodeError, PreparationError) as exc:
        raise ConsumerError("checked-in dependency lock is invalid") from exc


def load_consumer_reference(path: Path = CONSUMER_REFERENCE_PATH) -> dict[str, Any]:
    try:
        if not path.resolve(strict=True).is_relative_to(ROOT):
            raise ConsumerError("consumer reference file is outside its bounded source contract")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_REFERENCE_BYTES:
                raise ConsumerError("consumer reference file is outside its bounded source contract")
            with os.fdopen(fd, "rb") as stream:
                fd = -1
                raw = stream.read(MAX_REFERENCE_BYTES + 1)
        finally:
            if fd >= 0:
                os.close(fd)
        if len(raw) > MAX_REFERENCE_BYTES:
            raise ConsumerError("consumer reference file is outside its bounded source contract")
        value = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise ConsumerError("consumer reference file is missing or invalid") from exc
    if not isinstance(value, dict):
        raise ConsumerError("consumer reference must be a JSON object")
    return value


def validate_consumer_reference(value: Any, *, lock: dict[str, Any]) -> str:
    required = {"schema", "image_ref", "lock_sha256", "platform"}
    if not isinstance(value, dict) or set(value) != required or value.get("schema") != REFERENCE_SCHEMA:
        raise ConsumerError("consumer reference schema or fields are invalid")
    try:
        checked_lock = validate_lock(lock, root=ROOT)
    except PreparationError as exc:
        raise ConsumerError("checked-in dependency lock is invalid") from exc
    expected_lock_digest = _canonical_digest(checked_lock)
    image_ref = value.get("image_ref")
    expected_ref_pattern = rf"{re.escape(REGISTRY_IMAGE)}@sha256:[0-9a-f]{{64}}"
    if (not isinstance(image_ref, str) or not re.fullmatch(expected_ref_pattern, image_ref)
            or value.get("lock_sha256") != expected_lock_digest
            or value.get("platform") != checked_lock["platform"]):
        raise ConsumerError("consumer reference digest, lock, or platform does not match source")
    return image_ref


def _child_environment(env: dict[str, str], docker_config: Path) -> dict[str, str]:
    # Keep only process basics; credentials, Docker overrides, and unrelated CI
    # environment never enter the Docker client or its child containers.
    child = {name: env[name] for name in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR") if env.get(name)}
    child["DOCKER_CONFIG"] = str(docker_config)
    return child


def _run_bounded(argv: list[str], *, env: dict[str, str], timeout: int,
                 output_limit: int = MAX_DOCKER_OUTPUT_BYTES) -> tuple[bytes, bytes, float]:
    started = time.monotonic()
    try:
        process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   env=env, close_fds=True)
    except OSError as exc:
        raise ConsumerError("anonymous Docker command could not be started") from exc
    assert process.stdout is not None and process.stderr is not None
    buffers = {process.stdout: bytearray(), process.stderr: bytearray()}
    total = 0
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    selector.register(process.stderr, selectors.EVENT_READ)
    deadline = started + timeout
    failure: str | None = None
    while selector.get_map():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            failure = "anonymous Docker command exceeded its deadline"
            process.kill()
            break
        for key, _events in selector.select(min(0.25, remaining)):
            chunk = os.read(key.fileobj.fileno(), 8192)
            if not chunk:
                selector.unregister(key.fileobj)
                continue
            total += len(chunk)
            if total > output_limit:
                failure = "anonymous Docker command exceeded its output bound"
                process.kill()
                break
            buffers[key.fileobj].extend(chunk)
        if failure:
            break
    process.wait()
    selector.close()
    process.stdout.close()
    process.stderr.close()
    if failure:
        raise ConsumerError(failure)
    if process.returncode != 0:
        raise ConsumerError("anonymous Docker command failed")
    return bytes(buffers[process.stdout]), bytes(buffers[process.stderr]), round(time.monotonic() - started, 3)


def _read_json_output(raw: bytes, *, label: str) -> Any:
    if len(raw) > MAX_DOCKER_OUTPUT_BYTES:
        raise ConsumerError(f"{label} output exceeded its bound")
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ConsumerError(f"{label} output is invalid JSON") from exc


def _validate_source_free_image_config(image: dict[str, Any]) -> None:
    config = image.get("Config")
    if not isinstance(config, dict) or config.get("User") != "10001:10001" or config.get("WorkingDir") != "/opt/fwrouter-test":
        raise ConsumerError("dependency image runtime identity differs from its source-free recipe")
    if (config.get("Entrypoint") not in (None, [])
            or config.get("Cmd") not in (None, [], ["python3"])):
        raise ConsumerError("dependency image must not supply an application command")
    environment = config.get("Env")
    if (not isinstance(environment, list) or any(not isinstance(item, str) or item.startswith("FWROUTER_")
                                                  or "/workspace" in item for item in environment)):
        raise ConsumerError("dependency image contains application source/runtime environment metadata")


def _validate_pull_digest(stdout: bytes, stderr: bytes, image_ref: str) -> None:
    expected_digest = image_ref.split("@", 1)[1]
    output = (stdout + b"\n" + stderr).decode("utf-8", "replace")
    matches = re.findall(r"(?im)^Digest:\s+(sha256:[0-9a-f]{64})\s*$", output)
    if matches != [expected_digest]:
        raise ConsumerError("anonymous pull did not confirm the exact immutable image digest")


def _read_image_manifest(docker: str, config: Path, image_ref: str, *, env: dict[str, str],
                         lock: dict[str, Any], lock_digest: str) -> tuple[dict[str, Any], float]:
    command = [docker, "--config", str(config), "run", "--pull=never", "--rm", "--network=none",
               "--read-only", "--user", "10001:10001", "--memory=256m", "--memory-swap=256m",
               "--cpus=0.5", "--pids-limit=32", "--cap-drop=ALL", "--security-opt=no-new-privileges",
               "--entrypoint", "python", image_ref, "-c",
               "from pathlib import Path; print(Path('/opt/fwrouter-test/dependency-manifest.json').read_text())"]
    stdout, _stderr, elapsed = _run_bounded(command, env=env, timeout=20)
    manifest = _read_json_output(stdout, label="dependency manifest")
    try:
        validated = validate_dependency_manifest(manifest, lock=lock, lock_digest=lock_digest)
    except PublishError as exc:
        raise ConsumerError("dependency image manifest differs from the checked-in lock") from exc
    return validated, elapsed


def consume(reference: Any, *, runner_temp: Path, docker: str,
            env: dict[str, str] | None = None) -> dict[str, Any]:
    """Pull and qualify the referenced image anonymously before application use."""
    source_env = dict(os.environ if env is None else env)
    validate_consumer_environment(source_env)
    temp_root = _validated_runner_temp(runner_temp)
    if not isinstance(docker, str) or not Path(docker).is_absolute() or "\x00" in docker:
        raise ConsumerError("Docker executable path must be an absolute executable name")
    lock = load_locked_dependencies()
    image_ref = validate_consumer_reference(reference, lock=lock)
    lock_digest = _canonical_digest(lock)

    config = Path(tempfile.mkdtemp(prefix="fwrouter-anonymous-dependency-", dir=temp_root))
    config.chmod(0o700)
    config_info = config.lstat()
    if (not stat.S_ISDIR(config_info.st_mode) or stat.S_ISLNK(config_info.st_mode)
            or config_info.st_uid != os.getuid() or config_info.st_mode & 0o077
            or not config.resolve(strict=True).is_relative_to(temp_root)):
        shutil.rmtree(config, ignore_errors=True)
        raise ConsumerError("fresh anonymous Docker config directory is unsafe")
    child_env = _child_environment(source_env, config)
    if any(part in name.lower() for name in child_env
           for part in ("token", "password", "secret", "credential", "auth")):
        shutil.rmtree(config, ignore_errors=True)
        raise ConsumerError("credential-like environment reached the anonymous Docker client")

    try:
        pull_command = [docker, "--config", str(config), "pull", "--platform", "linux/amd64", image_ref]
        pull_stdout, pull_stderr, pull_seconds = _run_bounded(pull_command, env=child_env, timeout=600)
        _validate_pull_digest(pull_stdout, pull_stderr, image_ref)

        inspect_command = [docker, "--config", str(config), "image", "inspect", image_ref]
        inspect_stdout, _inspect_stderr, inspect_seconds = _run_bounded(
            inspect_command, env=child_env, timeout=20)
        rows = _read_json_output(inspect_stdout, label="dependency image inspect")
        if not isinstance(rows, list) or len(rows) != 1:
            raise ConsumerError("dependency image inspect returned an unexpected result")
        try:
            image = validate_dependency_image_metadata(rows[0], lock=lock, lock_digest=lock_digest,
                                                       image_ref=image_ref)
        except PublishError as exc:
            raise ConsumerError("dependency image labels or immutable digest differ from the lock") from exc
        _validate_source_free_image_config(image)
        manifest, manifest_seconds = _read_image_manifest(
            docker, config, image_ref, env=child_env, lock=lock, lock_digest=lock_digest)
        return {
            "schema": CONSUMER_RECEIPT_SCHEMA,
            "status": "ready",
            "image_ref": image_ref,
            "lock_sha256": lock_digest,
            "platform": lock["platform"],
            "image_config_id": image["Id"],
            "installed_package_count": len(manifest["installed_packages"]),
            "pull_seconds": pull_seconds,
            "inspect_seconds": inspect_seconds,
            "manifest_readback_seconds": manifest_seconds,
            "anonymous": True,
        }
    finally:
        try:
            config_info = config.lstat()
            if not stat.S_ISDIR(config_info.st_mode) or stat.S_ISLNK(config_info.st_mode):
                raise ConsumerError("anonymous Docker config path changed before cleanup")
            shutil.rmtree(config)
        except OSError as exc:
            raise ConsumerError("anonymous Docker config cleanup could not be confirmed") from exc
