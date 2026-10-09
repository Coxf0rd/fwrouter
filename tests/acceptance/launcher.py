#!/usr/bin/env python3
"""Fail-closed hosted acceptance launcher; ordinary use is source-only prepare."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import tarfile
import uuid
import shlex
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
OWNER_LABEL = "io.fwrouter.acceptance.owner"
RUN_LABEL = "io.fwrouter.acceptance.run"
OWNER_VALUE = "fwrouter-test-harness-v1"
PROFILE_SCHEMA = "fwrouter-acceptance-profile/v2"
MAX_RECEIPT_BYTES = 2 * 1024 * 1024
MAX_TEST_OUTPUT_BYTES = 4 * 1024 * 1024
MAX_TEST_SECONDS = 900
ALLOWED_PREFIXES = (
    "backend/fwrouter_api/", "backend/tests/", "backend/pyproject.toml", "ui/",
    "tests/application_acceptance/", "tests/acceptance/",
    "tests/gates/requirements-ci.txt", "host/libexec/fwrouter/traffic-collect.sh",
)
FORBIDDEN_PARTS = {
    ".venv", "__pycache__", "node_modules", ".git", "secrets",
    "credentials", "private", "certificates",
}
FORBIDDEN_SUFFIXES = (".db", ".sqlite", ".sqlite3", ".wal", ".shm", ".log", ".bak", ".backup",
                      ".tar", ".gz", ".zip", ".pem", ".key", ".p12", ".pfx")
PRODUCTION_MARKERS = (
    Path("/opt/fwrouter-api"), Path("/opt/fwrouter-xray"),
    Path("/opt/fwrouter-mihomo"), Path("/var/lib/fwrouter-v2"),
    Path("/run/fwrouter-v2"), Path("/etc/systemd/system/fwrouter-api.service"),
)
BASE_IMAGE_RE = re.compile(r"^[a-zA-Z0-9./:_-]+@sha256:[0-9a-f]{64}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_PUBLIC_SECRET = re.compile(
    r"(?i)([\"']?\b(?:password|passwd|secret|token|authorization|api[_-]?key|private[_-]?key|"
    r"pre[_-]?shared[_-]?key|client[_-]?secret|credential)\b[\"']?\s*[:=]\s*)"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_PUBLIC_BEARER = re.compile(r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/=-]+")
_PUBLIC_PEM = re.compile(r"-----BEGIN [^-]+-----.*?-----END [^-]+-----", re.DOTALL)
_PUBLIC_UUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-8][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}\b")
_PUBLIC_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_DIAGNOSTIC_NODEIDS = {
    "xray-diagnostic": "tests/application_acceptance/test_xray_api.py::test_api_xray_client_create_delete_has_native_loaded_readback",
    "provider-diagnostic": "tests/application_acceptance/test_core_provider_mihomo.py::test_core_subscription_provider_discovery_exclusive_intent_and_real_mihomo_child",
    "browser-diagnostic": "tests/application_acceptance/test_browser_locale.py::test_real_chromium_xray_client_editor_uses_api_jobs_and_native_readback",
}
_PROVIDER_COHORT_NODEIDS = (
    "tests/application_acceptance/test_browser_locale.py::test_real_chromium_provider_exclusive_control_persists_and_excludes_auto_candidate",
    "tests/application_acceptance/test_core_provider_mihomo.py::test_core_subscription_provider_discovery_exclusive_intent_and_real_mihomo_child",
    "tests/application_acceptance/test_core_provider_mihomo.py::test_confirmed_provider_failure_applies_emergency_direct_and_failed_reentry_stays_direct",
    "tests/application_acceptance/test_core_provider_mihomo.py::test_provider_handoff_rejects_stale_selection_after_real_selector_wins_probe_race",
    "tests/application_acceptance/test_core_provider_mihomo.py::test_provider_reentry_rejects_old_probe_after_real_mihomo_incarnation_change",
    "tests/application_acceptance/test_core_provider_mihomo.py::test_provider_reentry_fences_concurrent_public_exclusive_intent_change",
    "tests/application_acceptance/test_core_provider_mihomo.py::test_confirmed_recovery_typed_provider_api_errors_are_unknown_not_member_down[recovery-timeout]",
    "tests/application_acceptance/test_core_provider_mihomo.py::test_confirmed_recovery_typed_provider_api_errors_are_unknown_not_member_down[recovery-rate-limited]",
    "tests/application_acceptance/test_core_provider_mihomo.py::test_confirmed_recovery_typed_provider_api_errors_are_unknown_not_member_down[recovery-malformed]",
    "tests/application_acceptance/test_core_provider_mihomo.py::test_provider_status_controls_real_core_apply_and_native_mihomo_parity[unknown-status-neutral]",
    "tests/application_acceptance/test_xray_generation.py::test_xray_generation_fence_rejects_replaced_native_incarnation",
)
_FENCE_DIAGNOSTIC_NODEIDS = (
    "tests/application_acceptance/test_core_provider_mihomo.py::test_confirmed_recovery_typed_provider_api_errors_are_unknown_not_member_down[recovery-unavailable]",
    "tests/application_acceptance/test_xray_generation.py::test_real_core_selection_cas_miss_reconciles_after_native_readback_without_provider_retry",
)
_DIAGNOSTIC_SUITES = {*_DIAGNOSTIC_NODEIDS, "provider-cohort", "fence-diagnostic"}


def _redact_public(value: bytes | str, *, limit: int = 16 * 1024) -> str:
    text = value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)
    text = _PUBLIC_SECRET.sub(r"\1[REDACTED]", text)
    text = _PUBLIC_BEARER.sub(r"\1[REDACTED]", text)
    text = _PUBLIC_PEM.sub("[REDACTED PEM]", text)
    text = _PUBLIC_UUID.sub("[UUID]", text)
    text = _PUBLIC_EMAIL.sub("[EMAIL]", text)
    encoded = text.encode("utf-8", "replace")
    if len(encoded) > limit:
        marker = b"[truncated]\n"
        encoded = marker[:limit] if limit <= len(marker) else marker + encoded[-(limit - len(marker)):]
    return encoded.decode("utf-8", "replace")


def _docker_exec_capture(argv: list[str], *, cwd: Path, env: dict[str, str], timeout: int) -> dict[str, Any]:
    try:
        proc = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, timeout=timeout, check=False)
        return {"exit_code": proc.returncode, "stdout": _redact_public(proc.stdout, limit=8192),
                "stderr": _redact_public(proc.stderr, limit=8192)}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"exit_code": 124, "stdout": "", "stderr": _redact_public(str(exc), limit=2048)}


def _docker_copy_capture(docker: str, container: str, source: str, destination: Path,
                         *, env: dict[str, str]) -> dict[str, Any]:
    try:
        proc = subprocess.run([docker, "cp", f"{container}:{source}", str(destination)], cwd=ROOT,
                              env=env, capture_output=True, timeout=20, check=False)
        result = {"copied": proc.returncode == 0 and destination.is_file(),
                  "exit_code": proc.returncode,
                  "stderr": _redact_public(proc.stderr, limit=2048), "method": "docker-cp"}
    except (OSError, subprocess.TimeoutExpired) as exc:
        result = {"copied": False, "exit_code": 124, "stderr": _redact_public(str(exc), limit=2048),
                  "method": "docker-cp"}
    if result["copied"]:
        return result
    # Preserve the docker-cp failure and fall back only for fixed harness files.
    if source not in {
        "/tmp/fwrouter-receipts/application-acceptance.xml",
        "/tmp/fwrouter-receipts/application-acceptance.json",
        "/tmp/fwrouter-receipts/native-process-diagnostics.json",
        "/tmp/fwrouter-receipts/worker-service-logs.json",
        "/tmp/fwrouter-receipts/state-snapshot.json",
    }:
        return result
    try:
        proc = subprocess.run([docker, "exec", container, "cat", source], cwd=ROOT, env=env,
                              capture_output=True, timeout=20, check=False)
        if proc.returncode == 0 and len(proc.stdout) <= MAX_RECEIPT_BYTES:
            destination.write_bytes(proc.stdout)
            result.update({"copied": True, "fallback": "bounded-docker-exec-stdout"})
        else:
            result["fallback_error"] = _redact_public(proc.stderr, limit=2048)
            result["fallback_exit_code"] = proc.returncode
    except (OSError, subprocess.TimeoutExpired) as exc:
        result["fallback_error"] = _redact_public(str(exc), limit=2048)
    return result


def _write_emergency_junit(destination: Path, message: str) -> None:
    testsuite = ET.Element("testsuite", {"name": "acceptance-harness", "tests": "1",
                                          "failures": "0", "errors": "1", "skipped": "0"})
    testcase = ET.SubElement(testsuite, "testcase", {"classname": "tests.application_acceptance.harness",
                                                       "name": "artifact_export"})
    error = ET.SubElement(testcase, "error", {"message": "acceptance JUnit artifact unavailable"})
    error.text = _redact_public(message, limit=4096)
    ET.ElementTree(testsuite).write(destination, encoding="utf-8", xml_declaration=True)


def _redact_junit_file(path: Path) -> None:
    try:
        tree = ET.parse(path)
    except (OSError, ET.ParseError):
        return
    for element in tree.iter():
        if element.text:
            element.text = _redact_public(element.text, limit=16 * 1024)
        if element.tail:
            element.tail = _redact_public(element.tail, limit=4096)
        for key, value in list(element.attrib.items()):
            if element.tag == "testcase" and key in {"classname", "name"}:
                continue
            element.set(key, _redact_public(value, limit=4096))
    tree.write(path, encoding="utf-8", xml_declaration=True)


class NotRun(RuntimeError):
    """Qualification failed before any acceptance container was started."""


def sha256_file(path: Path) -> str:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or path.is_symlink():
        raise NotRun(f"input is not a regular non-symlink file: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _under(child: Path, parent: Path) -> bool:
    try:
        child.resolve(strict=True).relative_to(parent.resolve(strict=True))
        return True
    except (OSError, ValueError):
        return False


def qualify_host(env: dict[str, str], facts: dict[str, Any], markers: tuple[Path, ...] = PRODUCTION_MARKERS) -> list[str]:
    """Check runner layout and real host facts; CI env values alone do not qualify."""
    reasons: list[str] = []
    if env.get("GITHUB_ACTIONS") != "true" or env.get("RUNNER_ENVIRONMENT") != "github-hosted":
        reasons.append("runner is not explicitly identified as GitHub-hosted")
    if facts.get("uid") in (None, 0):
        reasons.append("root or unknown uid is not an eligible hosted runner")
    hostname = str(facts.get("hostname") or "").lower()
    if hostname == "minisk" or hostname.startswith("minisk."):
        reasons.append("minisk host identity is refused")
    if facts.get("pid1_comm") == "systemd" and facts.get("machine_id_matches_production") is True:
        reasons.append("host matches the production machine identity")
    workspace = Path(env.get("GITHUB_WORKSPACE", "/nonexistent"))
    runner_temp = Path(env.get("RUNNER_TEMP", "/nonexistent"))
    if not _under(workspace, Path("/home/runner/work")):
        reasons.append("workspace is outside the hosted runner workspace tree")
    if not _under(ROOT, workspace):
        reasons.append("repository checkout is outside the declared hosted workspace")
    if not _under(runner_temp, Path("/home/runner/work/_temp")):
        reasons.append("runner temp is outside the hosted runner temporary tree")
    try:
        temp_stat = runner_temp.stat()
        workspace_stat = workspace.stat()
        if (temp_stat.st_uid != facts.get("uid") or workspace_stat.st_uid != facts.get("uid")
                or temp_stat.st_mode & 0o022 or workspace_stat.st_mode & 0o022):
            reasons.append("runner temp ownership or permissions are unsafe")
        if workspace.stat().st_dev != temp_stat.st_dev:
            reasons.append("workspace and runner temp do not share the ephemeral runner filesystem")
    except OSError:
        reasons.append("runner workspace or temp does not exist")
    for marker in markers:
        if marker.exists():
            reasons.append(f"production marker exists: {marker}")
    if facts.get("workspace_temp_same_device") is not True:
        reasons.append("runner-owned workspace/temp device relationship was not verified")
    if facts.get("hosted_image_marker") is not True:
        reasons.append("known hosted-runner image marker is missing")
    if env.get("DOCKER_HOST"):
        reasons.append("remote Docker daemon endpoint is forbidden")
    return reasons


def validate_binary(path: str, expected_sha: str, runner_temp: Path) -> dict[str, str]:
    if not SHA_RE.fullmatch(expected_sha or ""):
        raise NotRun("binary digest must be a lowercase SHA-256")
    binary = Path(path)
    if not _under(binary, runner_temp):
        raise NotRun(f"binary is outside job-owned runner temp: {binary.name}")
    observed = sha256_file(binary)
    if observed != expected_sha:
        raise NotRun(f"binary digest mismatch: {binary.name}")
    return {"source": str(binary.resolve(strict=True)), "sha256": observed}


def validate_plan_digest(raw: str) -> str:
    if not SHA_RE.fullmatch(raw or ""):
        raise NotRun("FWROUTER_ACCEPTANCE_PLAN_DIGEST must be a lowercase SHA-256 plan digest")
    return raw


def validate_manifest_inputs(env: dict[str, str], runner_temp: Path) -> dict[str, dict[str, str]]:
    base = env.get("FWROUTER_ACCEPTANCE_BASE_IMAGE", "")
    if not BASE_IMAGE_RE.fullmatch(base):
        raise NotRun("base image must be an immutable name@sha256 digest")
    binaries = {
        key: validate_binary(env.get(f"FWROUTER_ACCEPTANCE_{key.upper()}_BINARY", ""),
                             env.get(f"FWROUTER_ACCEPTANCE_{key.upper()}_SHA256", ""), runner_temp)
        for key in ("xray", "mihomo")
    }
    browser_bundle = Path(env.get("FWROUTER_ACCEPTANCE_CHROMIUM_BUNDLE", ""))
    if not _under(browser_bundle, runner_temp):
        raise NotRun("Chromium bundle is outside job-owned runner temp")
    bundle_sha = env.get("FWROUTER_ACCEPTANCE_CHROMIUM_BUNDLE_SHA256", "")
    if not SHA_RE.fullmatch(bundle_sha or "") or sha256_file(browser_bundle) != bundle_sha:
        raise NotRun("Chromium bundle SHA-256 is invalid or mismatched")
    if not SHA_RE.fullmatch(env.get("FWROUTER_ACCEPTANCE_CHROMIUM_BINARY_SHA256", "")):
        raise NotRun("provisioned Chromium executable SHA-256 is invalid")
    binaries["chromium"] = {"source": str(browser_bundle.resolve(strict=True)), "sha256": bundle_sha}
    return binaries


def _safe_source(relative: str) -> bool:
    path = Path(relative)
    if not any(relative == prefix.rstrip("/") or relative.startswith(prefix) for prefix in ALLOWED_PREFIXES):
        return False
    if any(part.lower() in FORBIDDEN_PARTS for part in path.parts):
        return False
    if any(part.lower().startswith(".env") and part.lower() != ".env.example" for part in path.parts):
        return False
    if any(part.lower().endswith(FORBIDDEN_SUFFIXES) for part in path.parts):
        return False
    return not path.is_absolute() and ".." not in path.parts


def _extract_browser_bundle(archive_path: Path, destination: Path) -> str:
    executable = "chrome-linux64/chrome"
    if destination.exists() or destination.is_symlink():
        raise NotRun("browser bundle destination must not already exist")
    destination.mkdir(mode=0o700, parents=True)
    total = 0
    count = 0
    try:
        with tarfile.open(archive_path, mode="r:*") as archive:
            for member in archive:
                count += 1
                if count > 50000:
                    raise NotRun("Chromium bundle contains too many entries")
                name = Path(member.name)
                if name.is_absolute() or ".." in name.parts or not name.parts:
                    raise NotRun("Chromium bundle contains an unsafe path")
                if not (member.isdir() or member.isfile()) or member.issym() or member.islnk():
                    raise NotRun("Chromium bundle contains a link or special file")
                total += member.size
                if total > 2 * 1024 * 1024 * 1024:
                    raise NotRun("Chromium bundle exceeds the expanded size limit")
                target = destination.joinpath(*name.parts)
                if member.isdir():
                    target.mkdir(mode=0o755, parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
                stream = archive.extractfile(member)
                if stream is None:
                    raise NotRun("Chromium bundle file could not be read")
                with stream, target.open("xb") as output:
                    shutil.copyfileobj(stream, output, length=1024 * 1024)
                target.chmod(0o755 if member.mode & 0o111 else 0o644)
    except (tarfile.TarError, OSError) as exc:
        raise NotRun("Chromium bundle is not a readable safe tar archive") from exc
    browser = destination / executable
    if not browser.is_file() or browser.is_symlink() or not os.access(browser, os.X_OK):
        raise NotRun("Chromium bundle lacks chrome-linux64/chrome")
    return sha256_file(browser)


def export_build_context(root: Path, destination: Path, *, tracked: list[str], binaries: dict[str, Path],
                         browser_bundle: Path, source_revision: str) -> dict[str, str]:
    """Copy only reviewed source paths and hash-checked native inputs into a private context."""
    if destination.exists() or destination.is_symlink():
        raise NotRun("build context destination must not already exist")
    destination.mkdir(mode=0o700, parents=True)
    ui_digest = hashlib.sha256()
    source_digest = hashlib.sha256()
    for relative in sorted(set(tracked)):
        if not _safe_source(relative):
            raise NotRun(f"tracked source is outside acceptance allowlist: {relative}")
        source = root / relative
        if source.is_symlink() or not source.is_file() or not source.resolve().is_relative_to(root.resolve()):
            raise NotRun(f"allowlisted source is missing or symlinked: {relative}")
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        source_mode = source.stat().st_mode
        target.chmod(0o755 if source_mode & 0o111 else 0o644)
        if sha256_file(source) != sha256_file(target):
            raise NotRun(f"build context copy digest mismatch: {relative}")
        source_digest.update(relative.encode("utf-8") + b"\0")
        source_digest.update(target.read_bytes())
        if relative.startswith("ui/"):
            ui_digest.update(relative.encode("utf-8") + b"\0")
            ui_digest.update(target.read_bytes())
    for name in ("xray", "mihomo"):
        source = binaries[name]
        if source.is_symlink() or not source.is_file():
            raise NotRun(f"native binary input missing: {name}")
        target = destination / "native" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        target.chmod(0o755)
    executable_sha = _extract_browser_bundle(browser_bundle, destination / "native/chromium")
    if not (destination / "tests/acceptance/Dockerfile").is_file():
        raise NotRun("allowlisted build context lacks acceptance Dockerfile")
    source_manifest_sha256 = source_digest.hexdigest()
    sidecar = {
        "source_revision": source_revision,
        "ui_tree_sha256": ui_digest.hexdigest(),
        "source_manifest_sha256": source_manifest_sha256,
    }
    (destination / ".fwrouter-acceptance-revision").write_text(
        json.dumps(sidecar, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    (destination / ".fwrouter-acceptance-revision").chmod(0o444)
    return {**sidecar, "chromium_executable_sha256": executable_sha}


def validate_compose_config(config: dict[str, Any], *, run_id: str, profile_path: Path) -> None:
    services = config.get("services")
    networks = config.get("networks")
    if not isinstance(services, dict) or set(services) != {"application"}:
        raise NotRun("Compose must define exactly the application service")
    if not isinstance(networks, dict) or set(networks) != {"isolated"}:
        raise NotRun("Compose must define exactly one isolated network")
    service = services["application"]
    forbidden_fields = {"env_file", "secrets", "configs", "volumes_from", "extra_hosts",
                        "sysctls", "runtime", "devices", "pid", "ipc", "privileged"}
    if forbidden_fields & service.keys():
        raise NotRun("Compose contains an unreviewed host/runtime integration field")
    if service.get("privileged") or service.get("network_mode") or service.get("pid") or service.get("ipc"):
        raise NotRun("privileged/host namespace modes are forbidden")
    if service.get("ports") or service.get("devices") or service.get("cap_add"):
        raise NotRun("host ports, devices, and added capabilities are forbidden")
    if service.get("cap_drop") != ["ALL"] or service.get("read_only") is not True:
        raise NotRun("container must drop all capabilities and use a read-only root")
    if service.get("security_opt") != ["no-new-privileges:true"] or service.get("init") is not True:
        raise NotRun("Compose must enable no-new-privileges and the init process")
    if service.get("user") != "10001:10001":
        raise NotRun("container must use the fixed non-root acceptance uid")
    if service.get("restart") != "no":
        raise NotRun("automatic restart is forbidden")
    if service.get("command") != ["-c", "import signal; signal.pause()"]:
        raise NotRun("application container must remain idle until bounded preflight")
    labels = service.get("labels", {})
    if labels.get(OWNER_LABEL) != OWNER_VALUE or labels.get(RUN_LABEL) != run_id:
        raise NotRun("Compose ownership labels do not match this run")
    attached = service.get("networks")
    if not isinstance(attached, (list, dict)) or set(attached if isinstance(attached, list) else attached) != {"isolated"}:
        raise NotRun("application must attach only to the isolated internal network")
    if networks["isolated"].get("internal") is not True:
        raise NotRun("Compose network must be internal")
    network_labels = networks["isolated"].get("labels", {})
    if network_labels.get(OWNER_LABEL) != OWNER_VALUE or network_labels.get(RUN_LABEL) != run_id:
        raise NotRun("isolated network ownership labels do not match this run")
    mounts = service.get("volumes", [])
    if len(mounts) != 1:
        raise NotRun("only the read-only per-run profile bind mount is allowed")
    mount = mounts[0]
    if mount.get("target") != "/run/fwrouter-acceptance/profile.json" or mount.get("read_only") is not True:
        raise NotRun("profile mount must be the sole read-only host bind")
    if Path(mount.get("source", "")).resolve() != profile_path.resolve():
        raise NotRun("profile mount source is not owned by this acceptance run")
    memory_value = str(service.get("mem_limit", "0")).lower()
    scale = 1024 ** ("kmgt".find(memory_value[-1]) + 1) if memory_value[-1:] in "kmgt" else 1
    try:
        memory = int(float(memory_value[:-1] if scale > 1 else memory_value) * scale)
    except ValueError:
        memory = 0
    if memory <= 0 or memory > 2 * 1024 * 1024 * 1024 or int(service.get("pids_limit", 0)) not in range(1, 257):
        raise NotRun("container resource limits are missing or exceed policy")
    swap_limit = str(service.get("memswap_limit", "")).lower()
    if not swap_limit:
        raise NotRun("container swap limit must be explicit")
    swap_scale = 1024 ** ("kmgt".find(swap_limit[-1]) + 1) if swap_limit[-1:] in "kmgt" else 1
    try:
        swap_bytes = int(float(swap_limit[:-1] if swap_scale > 1 else swap_limit) * swap_scale)
    except ValueError:
        swap_bytes = 0
    if not memory <= swap_bytes <= 2 * 1024 * 1024 * 1024:
        raise NotRun("container swap limit is missing or exceeds memory policy")
    try:
        cpus = float(service.get("cpus", 0))
    except (TypeError, ValueError) as exc:
        raise NotRun("container CPU limit is invalid") from exc
    if cpus <= 0 or cpus > 2:
        raise NotRun("container CPU limit is missing or exceeds policy")
    tmpfs = service.get("tmpfs")
    if isinstance(tmpfs, list) and len(tmpfs) == 1 and isinstance(tmpfs[0], str):
        tmpfs_spec = tmpfs[0]
    elif isinstance(tmpfs, dict) and set(tmpfs) == {"/tmp"}:
        tmpfs_spec = "/tmp:" + str(tmpfs["/tmp"])
    else:
        raise NotRun("Compose must configure exactly one /tmp tmpfs")
    if not tmpfs_spec.startswith("/tmp:"):
        raise NotRun("Compose tmpfs target must be /tmp")
    tmpfs_options = tmpfs_spec.partition(":")[2].lower()
    tmpfs_size_match = re.search(r"(?:^|,)size=(\d+)([kmg]?)", tmpfs_options)
    if not all(flag in tmpfs_options for flag in ("noexec", "nosuid", "nodev")) or not tmpfs_size_match:
        raise NotRun("Compose /tmp tmpfs requires noexec/nosuid/nodev and an explicit size")
    compose_tmpfs_size = int(tmpfs_size_match.group(1)) * 1024 ** {"": 0, "k": 1, "m": 2, "g": 3}[tmpfs_size_match.group(2)]
    if not 1 <= compose_tmpfs_size <= 512 * 1024 * 1024:
        raise NotRun("Compose /tmp tmpfs exceeds the 512 MiB limit")
    environment = service.get("environment", {})
    allowed_environment = {
        "FWROUTER_ENVIRONMENT", "FWROUTER_STATE_DIR", "FWROUTER_STARTUP_TASKS_ENABLED",
        "FWROUTER_ACCEPTANCE_PROFILE", "FWROUTER_XRAY_BINARY", "FWROUTER_MIHOMO_BINARY",
        "FWROUTER_BROWSER_EXECUTABLE", "FWROUTER_CHROMIUM_BINARY",
        "FWROUTER_APPLICATION_ACCEPTANCE_ROOT", "FWROUTER_ACCEPTANCE_RECEIPT_PATH",
        "PLAYWRIGHT_BROWSERS_PATH", "PYTEST_DISABLE_PLUGIN_AUTOLOAD",
        "PYTHONDONTWRITEBYTECODE", "HOME",
    }
    if not isinstance(environment, dict) or set(environment) != allowed_environment:
        raise NotRun("Compose environment contains missing or unreviewed variables")
    if environment.get("FWROUTER_ENVIRONMENT") != "test" or environment.get("FWROUTER_STARTUP_TASKS_ENABLED") != "0":
        raise NotRun("test environment and disabled startup tasks are required")
    if environment.get("FWROUTER_ACCEPTANCE_PROFILE") != "/run/fwrouter-acceptance/profile.json":
        raise NotRun("read-only acceptance profile path is required")
    if environment.get("FWROUTER_ACCEPTANCE_RECEIPT_PATH") != "/tmp/fwrouter-receipts/application-acceptance.json":
        raise NotRun("bounded acceptance receipt path is required")
    expected_environment = {
        "FWROUTER_STATE_DIR": "/tmp/fwrouter-acceptance-state",
        "FWROUTER_XRAY_BINARY": "/opt/fwrouter-test/bin/xray",
        "FWROUTER_MIHOMO_BINARY": "/opt/fwrouter-test/bin/mihomo",
        "FWROUTER_BROWSER_EXECUTABLE": "/opt/fwrouter-test/chromium/chrome-linux64/chrome",
        "FWROUTER_CHROMIUM_BINARY": "/opt/fwrouter-test/chromium/chrome-linux64/chrome",
        "FWROUTER_APPLICATION_ACCEPTANCE_ROOT": "/tmp/fwrouter-application-acceptance",
        "PLAYWRIGHT_BROWSERS_PATH": "/opt/fwrouter-test/playwright-browsers",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTHONDONTWRITEBYTECODE": "1",
        "HOME": "/tmp/fwrouter-home",
    }
    if any(environment.get(key) != value for key, value in expected_environment.items()):
        raise NotRun("Compose paths or test-process controls do not match the acceptance contract")
    if "/var/lib/fwrouter-v2" in json.dumps(config) or "/opt/fwrouter-api/.env" in json.dumps(config):
        raise NotRun("production state or dotenv paths are forbidden")


def validate_container_inspect(value: dict[str, Any], *, project: str, run_id: str, profile_path: Path,
                               image_id: str | None = None) -> None:
    if not isinstance(value, dict) or not value.get("Id"):
        raise NotRun("container inspect did not return one concrete container")
    config = value.get("Config", {})
    host = value.get("HostConfig", {})
    labels = config.get("Labels", {})
    if labels.get(OWNER_LABEL) != OWNER_VALUE or labels.get(RUN_LABEL) != run_id or labels.get("com.docker.compose.project") != project:
        raise NotRun("created container labels do not prove this run owns it")
    if value.get("State", {}).get("Status") != "created":
        raise NotRun("container must remain stopped until confinement inspection passes")
    if config.get("User") != "10001:10001" or host.get("ReadonlyRootfs") is not True:
        raise NotRun("container identity/rootfs inspection failed")
    if config.get("Entrypoint") != ["python"] or config.get("Cmd") != ["-c", "import signal; signal.pause()"]:
        raise NotRun("container is not held at the reviewed idle entrypoint")
    if image_id and value.get("Image") != image_id:
        raise NotRun("created container does not use the inspected acceptance image")
    if host.get("Privileged") or host.get("NetworkMode") in {"host", "none", "default"}:
        raise NotRun("container has a forbidden privilege or network mode")
    if host.get("CapAdd") or host.get("Devices") or host.get("PidMode") or host.get("IpcMode") == "host":
        raise NotRun("container has forbidden capabilities, devices, or host namespaces")
    if host.get("CapDrop") != ["ALL"] or "no-new-privileges:true" not in (host.get("SecurityOpt") or []):
        raise NotRun("runtime capability drop or no-new-privileges policy is missing")
    if host.get("PortBindings"):
        raise NotRun("container has published host ports")
    mounts = value.get("Mounts", [])
    binds = [mount for mount in mounts if mount.get("Type") == "bind"]
    tmpfs = [mount for mount in mounts if mount.get("Type") == "tmpfs"]
    # Docker Engine may expose tmpfs only in HostConfig.Tmpfs and omit it from
    # the runtime Mounts array. Accept either inspect representation, while
    # keeping the exact profile bind and independently validating HostConfig
    # below. Never permit any other mount type or destination.
    if (len(mounts) not in (1, 2) or len(binds) != 1 or len(tmpfs) > 1
            or any(mount.get("Type") not in {"bind", "tmpfs"} for mount in mounts)
            or binds[0].get("Destination") != "/run/fwrouter-acceptance/profile.json"
            or (tmpfs and (tmpfs[0].get("Destination") != "/tmp" or tmpfs[0].get("RW") is not True))):
        raise NotRun("runtime container has unexpected host mounts")
    if tmpfs:
        tmpfs_mode = tmpfs[0].get("Mode", "").lower()
        if not all(flag in tmpfs_mode for flag in ("noexec", "nosuid", "nodev")):
            raise NotRun("/tmp tmpfs lacks noexec/nosuid/nodev restrictions")
    mount = binds[0]
    if mount.get("Destination") != "/run/fwrouter-acceptance/profile.json" or mount.get("RW") is not False:
        raise NotRun("runtime profile mount is not read-only")
    if Path(mount.get("Source", "")).resolve() != profile_path.resolve():
        raise NotRun("runtime mount source is not this run's profile")
    if host.get("PidsLimit", 0) not in range(1, 257) or host.get("Memory", 0) not in range(1, 2 * 1024 * 1024 * 1024 + 1):
        raise NotRun("runtime resource cgroup limits are missing or excessive")
    if host.get("MemorySwap", 0) not in range(host.get("Memory", 0), 2 * 1024 * 1024 * 1024 + 1):
        raise NotRun("runtime swap limit is missing or exceeds memory policy")
    if host.get("NanoCpus", 0) not in range(1, 2_000_000_001):
        raise NotRun("runtime CPU cgroup limit is missing or excessive")
    tmpfs_config = host.get("Tmpfs", {})
    if not isinstance(tmpfs_config, dict) or set(tmpfs_config) != {"/tmp"}:
        raise NotRun("runtime HostConfig does not preserve the bounded /tmp tmpfs")
    option_text = str(tmpfs_config["/tmp"]).lower()
    if not all(flag in option_text for flag in ("noexec", "nosuid", "nodev")):
        raise NotRun("runtime HostConfig tmpfs options differ from policy")
    size_match = re.search(r"(?:^|,)size=(\d+)([kmg]?)", option_text)
    if not size_match:
        raise NotRun("runtime /tmp size limit is not inspectable")
    tmpfs_size = int(size_match.group(1)) * 1024 ** {"": 0, "k": 1, "m": 2, "g": 3}[size_match.group(2)]
    if not 1 <= tmpfs_size <= 512 * 1024 * 1024:
        raise NotRun("runtime /tmp tmpfs exceeds the 512 MiB limit")
    allowed_env = {
        "PATH", "PYTHONPATH", "PYTHONDONTWRITEBYTECODE", "FWROUTER_ENVIRONMENT", "FWROUTER_STATE_DIR",
        "FWROUTER_STARTUP_TASKS_ENABLED", "FWROUTER_ACCEPTANCE_PROFILE", "FWROUTER_XRAY_BINARY",
        "FWROUTER_MIHOMO_BINARY", "FWROUTER_BROWSER_EXECUTABLE", "FWROUTER_CHROMIUM_BINARY",
        "FWROUTER_APPLICATION_ACCEPTANCE_ROOT", "FWROUTER_ACCEPTANCE_RECEIPT_PATH",
        "PLAYWRIGHT_BROWSERS_PATH", "PYTEST_DISABLE_PLUGIN_AUTOLOAD", "HOME", "LANG", "LC_ALL",
        "PYTHON_VERSION", "PYTHON_SHA256", "PYTHON_PIP_VERSION", "PYTHON_SETUPTOOLS_VERSION",
        "PYTHON_GET_PIP_URL", "PYTHON_GET_PIP_SHA256", "GPG_KEY",
    }
    env_keys = {str(item).split("=", 1)[0] for item in config.get("Env", [])}
    if env_keys - allowed_env:
        raise NotRun("container environment contains unreviewed variables")
    attached = value.get("NetworkSettings", {}).get("Networks", {})
    if set(attached) != {f"{project}_isolated"}:
        raise NotRun("runtime container is attached to an unexpected network")
    for key in env_keys:
        if key.lower().endswith(("_secret", "_token", "_password", "_api_key")):
            raise NotRun("container environment exposes a credential-like variable")


def validate_network_inspect(value: dict[str, Any], *, project: str, run_id: str,
                             container_id: str | None = None,
                             require_container_attached: bool = False) -> None:
    if not isinstance(value, dict) or value.get("Name") != f"{project}_isolated":
        raise NotRun("network inspect does not identify this suite's isolated network")
    labels = value.get("Labels", {})
    if labels.get(OWNER_LABEL) != OWNER_VALUE or labels.get(RUN_LABEL) != run_id:
        raise NotRun("isolated network ownership labels do not match this run")
    if value.get("Internal") is not True or value.get("EnableIPv6") is True or value.get("Options", {}).get("com.docker.network.bridge.enable_ip_masquerade") == "true":
        raise NotRun("runtime network can route outside the isolated test boundary")
    attached = value.get("Containers", {})
    if not isinstance(attached, dict):
        raise NotRun("isolated network attachment inventory is invalid")
    if container_id and attached and set(attached) != {container_id}:
        raise NotRun("isolated network has an unexpected attached container")
    if container_id and require_container_attached and set(attached) != {container_id}:
        raise NotRun("started acceptance container is missing from the isolated network inventory")
    if not container_id and attached:
        raise NotRun("isolated network has an unexpected attached container")


def _docker_json(argv: list[str], *, cwd: Path, env: dict[str, str], timeout: int = 30) -> Any:
    try:
        proc = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise NotRun(f"bounded container-control command failed: {argv[0]} {argv[1] if len(argv) > 1 else ''}") from exc
    if proc.returncode or len(proc.stdout) > MAX_RECEIPT_BYTES:
        raise NotRun(f"container-control command failed or exceeded output bound: {argv[0]}")
    try:
        return json.loads(proc.stdout)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise NotRun(f"container-control command returned invalid JSON: {argv[0]}") from exc


def _docker_text(argv: list[str], *, cwd: Path, env: dict[str, str], timeout: int = 30) -> str:
    try:
        proc = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise NotRun("bounded container-control command failed") from exc
    if proc.returncode or len(proc.stdout) > 4096:
        raise NotRun("container identity command failed or exceeded its bound")
    return proc.stdout.decode("utf-8", "strict").strip()


def _run_acceptance_tests(argv: list[str], *, cwd: Path, env: dict[str, str], output_path: Path) -> tuple[int, str, float]:
    import resource

    started = time.monotonic()
    with output_path.open("wb") as stream:
        child = subprocess.Popen(argv, cwd=cwd, env=env, stdout=stream, stderr=subprocess.STDOUT,
                                 start_new_session=True, preexec_fn=lambda: resource.setrlimit(
                                     resource.RLIMIT_FSIZE, (MAX_TEST_OUTPUT_BYTES, MAX_TEST_OUTPUT_BYTES)))
        try:
            code = child.wait(timeout=MAX_TEST_SECONDS)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, 9)
            child.wait(timeout=5)
            code = 124
    content = _redact_public(output_path.read_bytes()[:MAX_TEST_OUTPUT_BYTES], limit=MAX_TEST_OUTPUT_BYTES)
    output_path.write_text(content, encoding="utf-8")
    return code, content, round(time.monotonic() - started, 3)


def expected_acceptance_nodeids(suite: str) -> set[str]:
    """Reviewable source registry, checked without importing application tests."""
    if suite not in {"functional", "recovery", *_DIAGNOSTIC_SUITES}:
        raise NotRun("unknown acceptance suite")
    import importlib.util
    path = ROOT / "tests/acceptance/source_catalog.py"
    spec = importlib.util.spec_from_file_location("fwrouter_acceptance_source_catalog", path)
    if spec is None or spec.loader is None:
        raise NotRun("acceptance source catalog is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        rows = module.read_catalog(ROOT)
    except (OSError, ValueError, SyntaxError) as exc:
        raise NotRun(f"acceptance source registry is incomplete or stale: {exc}") from exc
    if suite in _DIAGNOSTIC_SUITES:
        diagnostics = ({_DIAGNOSTIC_NODEIDS[suite]} if suite in _DIAGNOSTIC_NODEIDS
                       else set(_PROVIDER_COHORT_NODEIDS if suite == "provider-cohort"
                                else _FENCE_DIAGNOSTIC_NODEIDS))
        if not diagnostics.issubset({row["nodeid"] for row in rows if row["suite"] == "functional"}):
            raise NotRun("fixed diagnostic node set is absent from the unchanged functional source catalog")
        return diagnostics
    selected = {row["nodeid"] for row in rows if row["suite"] == suite}
    if not selected:
        raise NotRun("acceptance suite has no registered scenarios")
    return selected


def validate_suite_node_receipt(rows: Any, suite: str, junit_nodeids: list[str]) -> None:
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise NotRun("application suite receipt contains an invalid node list")
    nodeids = [row.get("nodeid") for row in rows]
    expected = expected_acceptance_nodeids(suite)
    if (len(nodeids) != len(set(nodeids)) or set(nodeids) != expected
            or set(nodeids) != set(junit_nodeids) or len(junit_nodeids) != len(set(junit_nodeids))):
        raise NotRun("application suite receipt node IDs do not match JUnit and suite selection")
    for row in rows:
        phases = row.get("phases")
        if (not isinstance(phases, dict)
                or set(phases) != {"setup", "call", "teardown"}
                or any(value not in {"passed", "failed", "skipped"} for value in phases.values())
                or row.get("status") not in {"passed", "failed", "skipped"}):
            raise NotRun("application receipt includes an incomplete test phase")
        if suite in _DIAGNOSTIC_SUITES and row["status"] == "skipped":
            raise NotRun("diagnostic receipt skipped a selected scenario")
        if suite not in _DIAGNOSTIC_SUITES and (row["status"] != "passed" or any(v != "passed" for v in phases.values())):
            raise NotRun("application receipt includes a skipped or failed required phase")


def validate_junit(path: Path, suite: str) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink() or path.stat().st_size > MAX_RECEIPT_BYTES:
        raise NotRun("acceptance JUnit receipt is missing or exceeds its bound")
    try:
        root = ET.parse(path).getroot()
        cases = root.findall(".//testcase")
    except (ET.ParseError, OSError) as exc:
        raise NotRun("acceptance JUnit receipt is invalid") from exc
    failures = sum(case.find("failure") is not None for case in cases)
    errors = sum(case.find("error") is not None for case in cases)
    skipped = sum(case.find("skipped") is not None for case in cases)
    if not cases:
        raise NotRun("acceptance run reported no test cases")
    nodeids: list[str] = []
    statuses: dict[str, str] = {}
    for case in cases:
        module = case.get("classname", "").strip()
        name = case.get("name", "").strip()
        if not module.startswith("tests.application_acceptance.") or not name:
            raise NotRun("JUnit case does not identify an acceptance source node")
        nodeid = module.replace(".", "/") + ".py::" + name
        nodeids.append(nodeid)
        node = case.find("failure") is not None or case.find("error") is not None
        was_skipped = case.find("skipped") is not None
        statuses[nodeid] = "failed" if node else "skipped" if was_skipped else "passed"
    expected = expected_acceptance_nodeids(suite)
    if len(nodeids) != len(set(nodeids)) or set(nodeids) != expected:
        raise NotRun("JUnit acceptance node IDs do not match the selected suite contract")
    return {"tests": len(cases), "failures": failures, "errors": errors,
            "skipped": skipped, "nodeids": sorted(nodeids), "node_status": statuses}


def runtime_preflight_code() -> str:
    """Minimal in-container profile check before pytest imports application code."""
    return r'''import hashlib, importlib.metadata, json, pathlib, subprocess, sys
p = json.loads(pathlib.Path("/run/fwrouter-acceptance/profile.json").read_text())
assert p["schema"] == "fwrouter-acceptance-profile/v2" and p["profile"] == "hosted-native-process"
assert p["suite_nonce"] and sys.version_info[:2] == (3, 11)
assert len(p["plan_digest"]) == 64 and all(c in "0123456789abcdef" for c in p["plan_digest"])
def sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
for name, key in (("xray", "xray"), ("mihomo", "mihomo"), ("chromium", "chromium")):
    item = p[key]; path = pathlib.Path(item["path"])
    assert path.is_file() and not path.is_symlink()
    assert sha(path) == item["sha256"]
    args = {"xray": ["version"], "mihomo": ["-v"], "chromium": ["--version"]}[name]
    check = subprocess.run([str(path), *args], capture_output=True, text=True, timeout=8,
        stdin=subprocess.DEVNULL, env={"PATH":"/usr/bin:/bin","HOME":"/tmp","TMPDIR":"/tmp","LANG":"C.UTF-8","TZ":"UTC"})
    observed = (check.stdout + "\n" + check.stderr)[:4096]
    assert check.returncode == 0 and item["version"] in observed
for name, version in (("pytest", "8.3.5"), ("playwright", "1.55.0"), ("pyee", "13.0.0"), ("greenlet", "3.2.1")):
    assert importlib.metadata.version(name) == version
assert importlib.metadata.version("fastapi") == "0.115.12"
assert importlib.metadata.version("pydantic") == "2.10.6"
revision = json.loads(pathlib.Path("/workspace/.fwrouter-acceptance-revision").read_text())
assert revision["source_revision"] == p["source_revision"]
source_files = []
for base in ("backend/fwrouter_api", "backend/tests", "ui", "tests/application_acceptance", "tests/acceptance"):
    members = list(pathlib.Path("/workspace", base).rglob("*"))
    assert not any(x.is_symlink() for x in members)
    source_files.extend(x for x in members if x.is_file() and "/__pycache__/" not in f"/{x.relative_to('/workspace').as_posix()}/")
source_files.extend((pathlib.Path("/workspace/backend/pyproject.toml"), pathlib.Path("/workspace/tests/gates/requirements-ci.txt")))
source_files.append(pathlib.Path("/workspace/host/libexec/fwrouter/traffic-collect.sh"))
source_files = sorted(source_files, key=lambda item: item.relative_to("/workspace").as_posix())
source_hash = hashlib.sha256(); ui_hash = hashlib.sha256()
for path in source_files:
    relative = path.relative_to("/workspace").as_posix()
    source_hash.update(relative.encode() + b"\0")
    ui_member = relative.startswith("ui/")
    if ui_member: ui_hash.update(relative.encode() + b"\0")
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            source_hash.update(block)
            if ui_member: ui_hash.update(block)
assert set(revision) == {"source_revision", "ui_tree_sha256", "source_manifest_sha256"}
assert source_hash.hexdigest() == revision["source_manifest_sha256"]
assert ui_hash.hexdigest() == revision["ui_tree_sha256"] == p["ui_tree_sha256"]
for raw in ("/tmp/fwrouter-home", "/tmp/fwrouter-acceptance-state",
            "/tmp/fwrouter-application-acceptance", "/tmp/fwrouter-receipts"):
    path = pathlib.Path(raw)
    assert not path.is_symlink()
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.stat()
    assert info.st_uid == __import__("os").getuid() and info.st_mode & 0o077 == 0
print(json.dumps({"status":"passed","profile_nonce":p["suite_nonce"],"playwright":importlib.metadata.version("playwright")}))
'''


def _docker_exec_small(argv: list[str], *, cwd: Path, env: dict[str, str], timeout: int = 20) -> str:
    try:
        proc = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise NotRun("bounded container preflight failed") from exc
    if proc.returncode or len(proc.stdout) > 16384 or len(proc.stderr) > 16384:
        stdout = proc.stdout[:4096].decode("utf-8", "replace").strip()
        stderr = proc.stderr[:4096].decode("utf-8", "replace").strip()
        raise NotRun(
            "container runtime preflight failed or exceeded output bounds "
            f"(exit={proc.returncode}, stdout={stdout!r}, stderr={stderr!r})"
        )
    return proc.stdout.decode("utf-8", "replace")


def _write_receipt(path: Path, receipt: dict[str, Any]) -> None:
    payload = json.dumps(receipt, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    if len(payload) > MAX_RECEIPT_BYTES:
        raise NotRun("acceptance receipt exceeded its size bound")
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise NotRun("receipt destination is not a regular file")
    fd, temp_name = tempfile.mkstemp(prefix=".acceptance-receipt-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp_name, 0o644)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    # Read back before resource cleanup; cleanup is gated on a durable receipt.
    observed = json.loads(path.read_text(encoding="utf-8"))
    if observed.get("schema_version") != 1 or observed.get("suite_nonce") != receipt.get("suite_nonce"):
        raise NotRun("acceptance receipt validation failed")


def mark_cleanup_unconfirmed(receipt: dict[str, Any], reason: str) -> None:
    receipt["cleanup"] = "cleanup_unconfirmed"
    if receipt.get("status") in {"passed", "partial"}:
        receipt["status"] = "failed"
        receipt["reason"] = f"acceptance result downgraded because cleanup was unconfirmed: {reason}"


def _docker_id_present(kind: str, identifier: str, *, cwd: Path, env: dict[str, str]) -> bool:
    if kind == "container":
        argv = ["docker", "ps", "--all", "--quiet", "--no-trunc", "--filter", f"id={identifier}"]
    elif kind == "network":
        argv = ["docker", "network", "ls", "--quiet", "--no-trunc", "--filter", f"id={identifier}"]
    else:
        raise NotRun("unsupported resource absence check")
    return bool(_docker_text(argv, cwd=cwd, env=env))


def run_hosted_acceptance(env: dict[str, str], facts: dict[str, Any], *, suite: str,
                          allow_recovery: bool = False) -> dict[str, Any]:
    if suite not in {"functional", "recovery", *_DIAGNOSTIC_SUITES} or (suite == "recovery") != allow_recovery:
        raise NotRun("release recovery requires both --suite recovery and --allow-recovery")
    reasons = qualify_host(env, facts)
    if reasons:
        raise NotRun("host qualification failed: " + "; ".join(reasons))
    runner_temp = Path(env["RUNNER_TEMP"]).resolve(strict=True)
    binaries = validate_manifest_inputs(env, runner_temp)
    run_id = uuid.uuid4().hex
    project = f"fwrouter-acceptance-{run_id}"
    allowed_docker_env = {"PATH"}
    docker_env = {key: value for key, value in env.items() if key in allowed_docker_env}
    docker = shutil.which("docker", path=docker_env.get("PATH"))
    if not docker:
        raise NotRun("Docker CLI is unavailable on the qualified hosted runner")
    root = runner_temp / project
    root.mkdir(mode=0o700)
    artifact_dir = runner_temp / f"{project}-artifacts"
    artifact_dir.mkdir(mode=0o700)
    profile_dir = runner_temp / f"{project}-profile"
    profile_dir.mkdir(mode=0o755)
    profile_path = profile_dir / "profile.json"
    report_path = artifact_dir / "hosted-acceptance-report.json"
    junit_host = artifact_dir / "application-acceptance.xml"
    output_host = artifact_dir / "pytest-output.txt"
    compose_file = ROOT / "tests/acceptance/compose.yaml"
    context = root / "context"
    receipt: dict[str, Any] = {
        "schema_version": 1, "status": "NOTRUN",
        "scope": "hosted-native-diagnostic" if suite in _DIAGNOSTIC_SUITES else "hosted-native-process",
        "plan_digest": validate_plan_digest(env.get("FWROUTER_ACCEPTANCE_PLAN_DIGEST", "")),
        "suite_nonce": run_id, "source_revision": subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, capture_output=True, check=True).stdout.strip(),
        "project": project, "suite": suite, "base_image": env["FWROUTER_ACCEPTANCE_BASE_IMAGE"],
        "binary_sha256": {key: value["sha256"] for key, value in binaries.items()},
        "artifacts": {"directory": str(artifact_dir), "report": str(report_path),
                      "junit": str(junit_host), "pytest_log": str(output_host),
                      "profile": str(artifact_dir / "profile.json"),
                      "application_receipt": str(artifact_dir / "application-acceptance-receipt.json"),
                      "native_cli_preflight": str(artifact_dir / "native-cli-preflight.json"),
                      "native_diagnostics": str(artifact_dir / "native-process-diagnostics.json"),
                      "worker_logs": str(artifact_dir / "worker-service-logs.json"),
                      "state_summary": str(artifact_dir / "state-snapshot.json"),
                      "compose_logs": str(artifact_dir / "compose-logs.txt")},
        "diagnostic_only": suite in _DIAGNOSTIC_SUITES,
        "expected_ids": sorted(expected_acceptance_nodeids(suite)),
        "container_confinement": "not_checked", "tests": None,
    }
    container_id: str | None = None
    network_id: str | None = None
    image_id: str | None = None
    docker_env.update({
        "FWROUTER_ACCEPTANCE_CONTEXT": str(context),
        "FWROUTER_ACCEPTANCE_BASE_IMAGE": env["FWROUTER_ACCEPTANCE_BASE_IMAGE"],
        "FWROUTER_ACCEPTANCE_RUN_ID": run_id,
        "FWROUTER_ACCEPTANCE_PROFILE_FILE": str(profile_path),
        "FWROUTER_ACCEPTANCE_SOURCE_REVISION": subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, capture_output=True, check=True).stdout.strip(),
        "COMPOSE_DISABLE_ENV_FILE": "1",
    })
    docker_env["HOME"] = str(root / "home")
    docker_env["DOCKER_CONFIG"] = str(root / "docker-config")
    Path(docker_env["HOME"]).mkdir(mode=0o700)
    Path(docker_env["DOCKER_CONFIG"]).mkdir(mode=0o700)
    profile: dict[str, Any]
    try:
        tracked = git_files()
        copied = export_build_context(ROOT, context, tracked=tracked,
                                      binaries={key: Path(binaries[key]["source"]) for key in ("xray", "mihomo")},
                                      browser_bundle=Path(binaries["chromium"]["source"]),
                                      source_revision=receipt["source_revision"])
        if copied["chromium_executable_sha256"] != env.get("FWROUTER_ACCEPTANCE_CHROMIUM_BINARY_SHA256"):
            raise NotRun("normalized Chromium executable SHA-256 differs from the provisioned pin")
        fixture = context / "tests/application_acceptance/fixtures/xray.initial.json"
        if not fixture.is_file():
            raise NotRun("credential-free baseline Xray fixture is missing")
        profile = {
            "schema": PROFILE_SCHEMA, "profile": "hosted-native-process",
            "source_revision": receipt["source_revision"],
            "plan_digest": receipt["plan_digest"],
            "xray": {"path": "/opt/fwrouter-test/bin/xray", "sha256": binaries["xray"]["sha256"],
                     "version": env.get("FWROUTER_ACCEPTANCE_XRAY_VERSION", "")},
            "mihomo": {"path": "/opt/fwrouter-test/bin/mihomo", "sha256": binaries["mihomo"]["sha256"],
                       "version": env.get("FWROUTER_ACCEPTANCE_MIHOMO_VERSION", "")},
            "chromium": {"path": "/opt/fwrouter-test/chromium/chrome-linux64/chrome",
                         "sha256": copied["chromium_executable_sha256"],
                         "bundle_sha256": binaries["chromium"]["sha256"],
                         "version": env.get("FWROUTER_ACCEPTANCE_CHROMIUM_VERSION", "")},
            "playwright_python": env.get("FWROUTER_ACCEPTANCE_PLAYWRIGHT_VERSION", ""),
            "baseline_xray_config_sha256": sha256_file(fixture),
            "ui_tree_sha256": copied["ui_tree_sha256"], "suite_nonce": run_id,
        }
        required_versions = [profile["xray"]["version"], profile["mihomo"]["version"],
                             profile["chromium"]["version"], profile["playwright_python"]]
        if not all(isinstance(value, str) and value.strip() for value in required_versions):
            raise NotRun("pinned Xray/Mihomo/Chromium/Playwright version inputs are incomplete")
        if profile["playwright_python"] != "1.55.0":
            raise NotRun("Playwright Python must match the hashed acceptance lock (1.55.0)")
        profile_path.write_text(json.dumps(profile, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        profile_path.chmod(0o444)
        shutil.copyfile(profile_path, artifact_dir / "profile.json")
        (artifact_dir / "profile.json").chmod(0o444)
        config = _docker_json([docker, "compose", "-f", str(compose_file), "-p", project, "config", "--format", "json"],
                              cwd=ROOT, env=docker_env)
        validate_compose_config(config, run_id=run_id, profile_path=profile_path)
        subprocess.run([docker, "compose", "-f", str(compose_file), "-p", project, "build", "application"],
                       cwd=ROOT, env=docker_env, check=True, timeout=300,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        image_raw = _docker_json([docker, "image", "inspect", f"fwrouter-acceptance:{run_id}"], cwd=ROOT, env=docker_env)
        if not isinstance(image_raw, list) or len(image_raw) != 1:
            raise NotRun("acceptance image inspect failed")
        image_labels = image_raw[0].get("Config", {}).get("Labels", {})
        image_id = image_raw[0].get("Id")
        if (image_labels.get(OWNER_LABEL) != OWNER_VALUE or image_labels.get(RUN_LABEL) != run_id
                or image_labels.get("org.opencontainers.image.revision") != receipt["source_revision"] or not image_id):
            raise NotRun("built image lacks acceptance ownership label")
        if image_raw[0].get("Architecture") != "amd64" or image_raw[0].get("Os") != "linux":
            raise NotRun("acceptance image must be Linux amd64")
        subprocess.run([docker, "compose", "-f", str(compose_file), "-p", project, "create", "application"],
                       cwd=ROOT, env=docker_env, check=True, timeout=60,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        container_id = _docker_text([docker, "compose", "-f", str(compose_file), "-p", project,
                                     "ps", "--all", "-q", "application"], cwd=ROOT, env=docker_env)
        if not re.fullmatch(r"[0-9a-f]{64}", container_id or ""):
            raise NotRun("could not resolve exact created container identity")
        network_raw = _docker_json([docker, "network", "inspect", f"{project}_isolated"], cwd=ROOT, env=docker_env)
        if not isinstance(network_raw, list) or len(network_raw) != 1:
            raise NotRun("network inspect returned an unexpected result")
        validate_network_inspect(network_raw[0], project=project, run_id=run_id, container_id=container_id)
        network_id = network_raw[0].get("Id")
        if not network_id:
            raise NotRun("network inspect omitted owned network id")
        inspected = _docker_json([docker, "inspect", container_id], cwd=ROOT, env=docker_env)
        if not isinstance(inspected, list) or len(inspected) != 1:
            raise NotRun("container inspect returned an unexpected result")
        validate_container_inspect(inspected[0], project=project, run_id=run_id, profile_path=profile_path,
                                   image_id=image_id)
        network_raw = _docker_json([docker, "network", "inspect", network_id], cwd=ROOT, env=docker_env)
        if not isinstance(network_raw, list) or len(network_raw) != 1:
            raise NotRun("network inspect returned an unexpected result")
        validate_network_inspect(network_raw[0], project=project, run_id=run_id, container_id=container_id)
        receipt["container_confinement"] = "passed"
        subprocess.run([docker, "compose", "-f", str(compose_file), "-p", project, "start", "application"],
                       cwd=ROOT, env=docker_env, check=True, timeout=30,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        network_raw = _docker_json([docker, "network", "inspect", network_id], cwd=ROOT, env=docker_env)
        if not isinstance(network_raw, list) or len(network_raw) != 1:
            raise NotRun("started network inspect returned an unexpected result")
        validate_network_inspect(network_raw[0], project=project, run_id=run_id,
                                 container_id=container_id, require_container_attached=True)
        preflight = _docker_exec_small([docker, "exec", container_id, "python", "-c", runtime_preflight_code()],
                                       cwd=ROOT, env=docker_env)
        preflight_result = json.loads(preflight)
        if preflight_result.get("status") != "passed" or preflight_result.get("profile_nonce") != run_id:
            raise NotRun("pre-import container runtime profile check did not pass")
        receipt["runtime_preflight"] = "passed"
        prep = [docker, "exec", container_id, "python", "-c",
                "from pathlib import Path; Path('/tmp/fwrouter-receipts').mkdir(mode=0o700, parents=True, exist_ok=True)"]
        subprocess.run(prep, cwd=ROOT, env=docker_env, check=True, timeout=10,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        junit_in = "/tmp/fwrouter-receipts/application-acceptance.xml"
        if suite in _DIAGNOSTIC_SUITES:
            if suite == "xray-diagnostic":
                cli = _docker_exec_capture([docker, "exec", container_id, "/opt/fwrouter-test/bin/xray",
                                            "run", "-test", "-config",
                                            "/workspace/tests/application_acceptance/fixtures/xray.initial.json"],
                                           cwd=ROOT, env=docker_env, timeout=20)
                cli = {"schema": "fwrouter-native-cli-preflight/v1", "source_revision": receipt["source_revision"],
                       "plan_digest": receipt["plan_digest"], "suite_nonce": run_id,
                       "xray_sha256": binaries["xray"]["sha256"], **cli}
                (artifact_dir / "native-cli-preflight.json").write_text(
                    json.dumps(cli, sort_keys=True, indent=2) + "\n", encoding="utf-8")
                if cli["exit_code"] != 0:
                    raise NotRun("pinned Xray baseline config CLI preflight failed")
            command = [docker, "exec", "--env", "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1",
                       "--env", "PYTHONDONTWRITEBYTECODE=1", container_id,
                       "python", "-m", "pytest", "-p", "no:cacheprovider",
                       f"--junitxml={junit_in}", "-q", *sorted(expected_acceptance_nodeids(suite))]
        else:
            marker = "l7" if suite == "recovery" else "not l7"
            command = [docker, "exec", "--env", "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1",
                       "--env", "PYTHONDONTWRITEBYTECODE=1", container_id,
                       "python", "-m", "pytest", "-p", "no:cacheprovider",
                       f"--junitxml={junit_in}", "-m", marker, "tests/application_acceptance", "-q"]
        command_env = dict(docker_env)
        command_env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
        command_env["PYTHONDONTWRITEBYTECODE"] = "1"
        code, output, elapsed = _run_acceptance_tests(command, cwd=ROOT, env=command_env, output_path=output_host)
        receipt.update({"pytest_exit_code": code, "pytest_elapsed_seconds": elapsed,
                        "pytest_output_bytes": output_host.stat().st_size,
                        "pytest_output_tail": output[-8192:]})
        copy_result = _docker_copy_capture(docker, container_id, junit_in, junit_host, env=docker_env)
        (artifact_dir / "junit-export.json").write_text(json.dumps(copy_result, sort_keys=True, indent=2) + "\n")
        if not copy_result["copied"]:
            _write_emergency_junit(junit_host, "JUnit export failed: " + copy_result.get("stderr", "docker cp failed"))
        else:
            _redact_junit_file(junit_host)
        try:
            receipt["tests"] = validate_junit(junit_host, suite)
        except NotRun as exc:
            receipt["tests"] = {"contract_error": _redact_public(str(exc), limit=2048)}
        receipt["profile_sha256"] = hashlib.sha256(profile_path.read_bytes()).hexdigest()
        suite_receipt_path = artifact_dir / "application-acceptance-receipt.json"
        app_receipt_copy = _docker_copy_capture(docker, container_id,
            "/tmp/fwrouter-receipts/application-acceptance.json", suite_receipt_path, env=docker_env)
        (artifact_dir / "application-receipt-export.json").write_text(
            json.dumps(app_receipt_copy, sort_keys=True, indent=2) + "\n")
        if not app_receipt_copy["copied"]:
            receipt["application_receipt_export_error"] = _redact_public(
                app_receipt_copy.get("stderr", "docker cp failed"), limit=2048)
            raise NotRun("application receipt export failed")
        if suite_receipt_path.is_symlink() or suite_receipt_path.stat().st_size > MAX_RECEIPT_BYTES:
            raise NotRun("application suite receipt is not a bounded regular file")
        suite_receipt = json.loads(suite_receipt_path.read_text(encoding="utf-8"))
        if (suite_receipt.get("schema") != "fwrouter-application-acceptance-receipt/v2"
                or suite_receipt.get("source_revision") != receipt["source_revision"]
                or suite_receipt.get("plan_digest") != receipt["plan_digest"]
                or suite_receipt.get("profile_sha256") != receipt["profile_sha256"]
                or suite_receipt.get("suite_nonce") != run_id
                or suite_receipt.get("scope") != "hosted-native-process"
                or not isinstance(suite_receipt.get("tests"), list)):
            raise NotRun("application suite receipt did not match this run's source/profile/nonce")
        if "nodeids" in receipt["tests"]:
            validate_suite_node_receipt(suite_receipt["tests"], suite, receipt["tests"]["nodeids"])
        receipt["application_receipt"] = suite_receipt
        receipt["artifacts"] = {"directory": str(artifact_dir),
                                "report": str(report_path), "junit": str(junit_host),
                                "pytest_log": str(output_host),
                                "profile": str(artifact_dir / "profile.json"),
                                "application_receipt": str(suite_receipt_path),
                                "native_cli_preflight": str(artifact_dir / "native-cli-preflight.json"),
                                "native_diagnostics": str(artifact_dir / "native-process-diagnostics.json"),
                                "worker_logs": str(artifact_dir / "worker-service-logs.json"),
                                "state_summary": str(artifact_dir / "state-snapshot.json"),
                                "compose_logs": str(artifact_dir / "compose-logs.txt")}
        if code == 0 and receipt["tests"]["failures"] == 0 and receipt["tests"]["errors"] == 0 and receipt["tests"]["skipped"] == 0:
            receipt["status"] = "partial" if receipt["tests"]["skipped"] else "passed"
        else:
            receipt["status"] = "failed"
        receipt["profile"] = profile
        receipt["image_id"] = image_id
        receipt["container_id"] = container_id
        receipt["network_id"] = network_id
        receipt["report_path"] = str(report_path)
        _write_receipt(report_path, receipt)
    except (NotRun, OSError, subprocess.SubprocessError, ValueError, KeyError) as exc:
        receipt["reason"] = str(exc)[:2048]
        receipt["status"] = "NOTRUN" if container_id is None else "failed"
        receipt["report_path"] = str(report_path)
        if not report_path.exists():
            _write_receipt(report_path, receipt)
    finally:
        if container_id:
            # Save bounded runtime/service output and in-container diagnostic receipts
            # before deleting the exact owned container. Export statuses remain visible.
            logs = _docker_exec_capture([docker, "compose", "-f", str(compose_file), "-p", project,
                                         "logs", "--no-color", "--timestamps", "application"],
                                        cwd=ROOT, env=docker_env, timeout=20)
            (artifact_dir / "compose-logs.txt").write_text(
                _redact_public(logs.get("stdout", "") + "\n" + logs.get("stderr", ""), limit=64 * 1024),
                encoding="utf-8")
            exports = {}
            for source, filename in (
                ("/tmp/fwrouter-receipts/native-process-diagnostics.json", "native-process-diagnostics.json"),
                ("/tmp/fwrouter-receipts/worker-service-logs.json", "worker-service-logs.json"),
                ("/tmp/fwrouter-receipts/state-snapshot.json", "state-snapshot.json"),
            ):
                result = _docker_copy_capture(docker, container_id, source, artifact_dir / filename, env=docker_env)
                exports[filename] = result
            (artifact_dir / "diagnostic-export.json").write_text(
                json.dumps(exports, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        # Every cleanup target is resolved by exact ID and ownership labels.
        # No `compose down`, global prune, or project-name-only deletion.
        if container_id:
            try:
                try:
                    inspected = _docker_json([docker, "inspect", container_id], cwd=ROOT, env=docker_env)
                except NotRun:
                    if _docker_id_present("container", container_id, cwd=ROOT, env=docker_env):
                        raise NotRun("container inspect failed while the resource still exists")
                    inspected = []
                if inspected:
                    if not isinstance(inspected, list) or len(inspected) != 1:
                        raise NotRun("container cleanup inspect returned an unexpected shape")
                    labels = inspected[0].get("Config", {}).get("Labels", {})
                    if (labels.get(OWNER_LABEL) != OWNER_VALUE or labels.get(RUN_LABEL) != run_id
                            or labels.get("com.docker.compose.project") != project):
                        raise NotRun("container cleanup ownership labels do not match this run")
                    else:
                        subprocess.run([docker, "rm", "-f", container_id], cwd=ROOT, env=docker_env,
                                       check=True, timeout=30, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        if _docker_id_present("container", container_id, cwd=ROOT, env=docker_env):
                            raise NotRun("container remains after owned-resource removal")
            except (NotRun, OSError, subprocess.SubprocessError):
                mark_cleanup_unconfirmed(receipt, "container")
        if network_id:
            try:
                raw = _docker_json([docker, "network", "inspect", network_id], cwd=ROOT, env=docker_env)
                if not isinstance(raw, list) or len(raw) != 1:
                    raise NotRun("network cleanup inspect returned an unexpected shape")
                validate_network_inspect(raw[0], project=project, run_id=run_id)
                subprocess.run([docker, "network", "rm", network_id], cwd=ROOT, env=docker_env,
                               check=True, timeout=20, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if _docker_id_present("network", network_id, cwd=ROOT, env=docker_env):
                    raise NotRun("network remains after owned-resource removal")
            except (NotRun, OSError, subprocess.SubprocessError):
                mark_cleanup_unconfirmed(receipt, "network")
        if image_id:
            try:
                raw = _docker_json([docker, "image", "inspect", image_id], cwd=ROOT, env=docker_env)
                if not isinstance(raw, list) or len(raw) != 1:
                    raise NotRun("image cleanup inspect returned an unexpected shape")
                labels = raw[0].get("Config", {}).get("Labels", {})
                if (labels.get(OWNER_LABEL) != OWNER_VALUE or labels.get(RUN_LABEL) != run_id
                        or labels.get("org.opencontainers.image.revision") != receipt["source_revision"]):
                    raise NotRun("image cleanup ownership labels do not match this run")
                subprocess.run([docker, "image", "rm", image_id], cwd=ROOT, env=docker_env,
                               check=True, timeout=30, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except (NotRun, OSError, subprocess.SubprocessError):
                mark_cleanup_unconfirmed(receipt, "image")
        receipt.setdefault("cleanup", "owned_resources_removed" if container_id or network_id or image_id else "no_container_resources_created")
        if receipt.get("status") in {"passed", "partial"} and receipt.get("cleanup") != "owned_resources_removed":
            receipt["status"] = "failed"
            receipt["reason"] = "acceptance passed but owned-resource cleanup was not confirmed"
        temporary_paths = [root, profile_dir]
        cleanup_ok = True
        for temporary_path in temporary_paths:
            try:
                if temporary_path.is_dir() and not temporary_path.is_symlink():
                    shutil.rmtree(temporary_path)
                elif temporary_path.exists() or temporary_path.is_symlink():
                    temporary_path.unlink()
            except OSError:
                cleanup_ok = False
        receipt["temporary_artifacts_removed"] = cleanup_ok
        if not cleanup_ok:
            mark_cleanup_unconfirmed(receipt, "temporary artifacts")
        if report_path.exists():
            _write_receipt(report_path, receipt)
    return receipt


def host_facts() -> dict[str, Any]:
    # Root/production refusal is based on live host facts, never caller labels.
    hostname = subprocess.run(["hostname"], text=True, capture_output=True, timeout=3, check=True).stdout.strip()
    pid1 = Path("/proc/1/comm").read_text(encoding="ascii").strip()
    temp = Path(os.environ.get("RUNNER_TEMP", "/nonexistent"))
    workspace = Path(os.environ.get("GITHUB_WORKSPACE", "/nonexistent"))
    return {
        "uid": os.getuid(), "hostname": hostname, "pid1_comm": pid1,
        "machine_id_matches_production": False,
        "workspace_temp_same_device": temp.exists() and workspace.exists() and temp.stat().st_dev == workspace.stat().st_dev,
        "hosted_image_marker": Path("/imagegeneration").is_dir() or Path("/etc/runner-image").is_file(),
    }


def git_files() -> list[str]:
    proc = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, timeout=10, check=True)
    files = [part.decode("utf-8", "strict") for part in proc.stdout.split(b"\0") if part]
    untracked = subprocess.run(["git", "ls-files", "--others", "--exclude-standard", "-z"], cwd=ROOT,
                               capture_output=True, timeout=10, check=True).stdout
    extras = [part.decode("utf-8", "strict") for part in untracked.split(b"\0") if part]
    if extras:
        raise NotRun("untracked files are forbidden in the acceptance build context")
    dirty = subprocess.run(["git", "diff", "--quiet"], cwd=ROOT, check=False).returncode
    cached = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ROOT, check=False).returncode
    if dirty or cached:
        raise NotRun("acceptance requires a clean, committed source revision")
    rejected = [path for path in files if any(path == prefix.rstrip("/") or path.startswith(prefix) for prefix in ALLOWED_PREFIXES)
                and not _safe_source(path)]
    if rejected:
        raise NotRun("allowlisted tree contains forbidden environment, credential, DB, log, or archive files")
    return [path for path in files if _safe_source(path)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="execute only after independent hosted qualification")
    parser.add_argument("--suite", choices=("functional", "recovery", "xray-diagnostic", "provider-diagnostic", "browser-diagnostic", "provider-cohort", "fence-diagnostic"), default="functional")
    parser.add_argument("--allow-recovery", action="store_true", help="explicitly select release-only L7 recovery tests")
    args = parser.parse_args()
    receipt: dict[str, Any] = {"schema_version": 1, "status": "NOTRUN", "scope": "hosted-native-process"}
    try:
        env = dict(os.environ)
        facts = host_facts()
        reasons = qualify_host(env, facts)
        if reasons:
            raise NotRun("host qualification failed: " + "; ".join(reasons))
        runner_temp = Path(env["RUNNER_TEMP"]).resolve(strict=True)
        binaries = validate_manifest_inputs(env, runner_temp)
        if args.suite == "recovery" and not args.allow_recovery:
            raise NotRun("recovery suite requires --allow-recovery")
        if args.suite != "recovery" and args.allow_recovery:
            raise NotRun("--allow-recovery is valid only with --suite recovery")
        if not args.run:
            receipt.update({"status": "prepared_only", "suite": args.suite,
                            "binary_sha256": {k: v["sha256"] for k, v in binaries.items()},
                            "reason": "container execution requires explicit --run"})
            print(json.dumps(receipt, sort_keys=True))
            return 0
        plan_digest = validate_plan_digest(env.get("FWROUTER_ACCEPTANCE_PLAN_DIGEST", ""))
        receipt["plan_digest"] = plan_digest
        receipt = run_hosted_acceptance(env, facts, suite=args.suite, allow_recovery=args.allow_recovery)
        print(json.dumps(receipt, sort_keys=True))
        return 0 if receipt.get("status") == "passed" else 1
    except (NotRun, KeyError, OSError, subprocess.SubprocessError, UnicodeError) as exc:
        receipt["reason"] = str(exc)[:2048]
        print(json.dumps(receipt, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
