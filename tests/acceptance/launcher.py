#!/usr/bin/env python3
"""Fail-closed hosted acceptance launcher; ordinary use is source-only prepare."""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import math
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
import socket
import struct
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
KERNEL_SCRIPT_SOURCES = (
    "host/libexec/fwrouter/dataplane-common.sh",
    "host/libexec/fwrouter/dataplane-check.sh",
    "host/libexec/fwrouter/dataplane-apply.sh",
    "host/libexec/fwrouter/dataplane-rollback.sh",
)
KERNEL_SCRIPT_TARGETS = {
    "host/libexec/fwrouter/dataplane-common.sh": "/usr/local/libexec/fwrouter/dataplane-common.sh",
    "host/libexec/fwrouter/dataplane-check.sh": "/usr/local/libexec/fwrouter/dataplane-check.sh",
    "host/libexec/fwrouter/dataplane-apply.sh": "/usr/local/libexec/fwrouter/dataplane-apply.sh",
    "host/libexec/fwrouter/dataplane-rollback.sh": "/usr/local/libexec/fwrouter/dataplane-rollback.sh",
}
KERNEL_DEBIAN_SNAPSHOT = "https://snapshot.debian.org/archive/debian/20261009T000000Z"
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
_RECOVERY_DIAGNOSTIC_NODEIDS = (
    "tests/application_acceptance/test_core_provider_mihomo.py::test_confirmed_provider_failure_applies_emergency_direct_and_failed_reentry_stays_direct",
    "tests/application_acceptance/test_core_provider_mihomo.py::test_provider_handoff_rejects_stale_selection_after_real_selector_wins_probe_race",
)
_TARGET_DIAGNOSTIC_NODEIDS = (
    "tests/application_acceptance/test_core_provider_mihomo.py::test_confirmed_recovery_typed_provider_api_errors_are_unknown_not_member_down[recovery-unavailable]",
    "tests/application_acceptance/test_xray_generation.py::test_real_core_selection_cas_miss_reconciles_after_native_readback_without_provider_retry",
    "tests/application_acceptance/test_browser_locale.py::test_real_chromium_xray_client_editor_uses_api_jobs_and_native_readback",
)
_KERNEL_RECOVERY_SUITE = "kernel-recovery-diagnostic"
_PACKET_DIAGNOSTIC_SUITE = "packet-diagnostic"
_PACKET_DIAGNOSTIC_NODEIDS = (
    "tests/application_acceptance/test_packet_dataplane.py::test_core_vpn_emergency_direct_reentry_forwards_owned_tcp_udp_dns_without_leaks",
)
_DIAGNOSTIC_SUITES = {*_DIAGNOSTIC_NODEIDS, "provider-cohort", "fence-diagnostic", "target-diagnostic",
                      "recovery-diagnostic", _KERNEL_RECOVERY_SUITE, _PACKET_DIAGNOSTIC_SUITE}
_KERNEL_SUITES = {"kernel-preflight"}
PACKET_NETWORKS = {
    "lan": {"subnet": "10.240.0.0/29", "gateway": "10.240.0.6", "router": "10.240.0.1", "client": "10.240.0.2"},
    "wan": {"subnet": "198.18.240.0/29", "gateway": "198.18.240.6", "router": "198.18.240.1", "endpoint": "198.18.240.2"},
}
PACKET_VIP = "203.0.113.53/32"
PACKET_TCPDUMP_VERSION = "tcpdump version 4.99.3"
PACKET_CAPTURE_LIMIT = 512
PACKET_CONTAINER_EVIDENCE_DIR = "/tmp/fwrouter-packet-evidence"


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
    if (relative not in KERNEL_SCRIPT_SOURCES
            and not any(relative == prefix.rstrip("/") or relative.startswith(prefix) for prefix in ALLOWED_PREFIXES)):
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


def validate_compose_config(config: dict[str, Any], *, run_id: str, profile_path: Path,
                            kernel_preflight: bool = False) -> None:
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
    if service.get("ports") or service.get("devices"):
        raise NotRun("host ports and devices are forbidden")
    expected_cap_add = ["NET_ADMIN"] if kernel_preflight else None
    if service.get("cap_add") != expected_cap_add:
        raise NotRun("container capabilities differ from the selected acceptance profile")
    if service.get("cap_drop") != ["ALL"] or service.get("read_only") is not True:
        raise NotRun("container must drop all capabilities and use a read-only root")
    if service.get("security_opt") != ["no-new-privileges:true"] or service.get("init") is not True:
        raise NotRun("Compose must enable no-new-privileges and the init process")
    expected_user = "0:0" if kernel_preflight else "10001:10001"
    if service.get("user") != expected_user:
        raise NotRun("container uid differs from the selected acceptance profile")
    if service.get("restart") != "no":
        raise NotRun("automatic restart is forbidden")
    if service.get("command") != ["-c", "import signal; signal.pause()"]:
        raise NotRun("application container must remain idle until bounded preflight")
    if kernel_preflight:
        build_args = service.get("build", {}).get("args", {})
        if (build_args.get("FWROUTER_KERNEL_PREFLIGHT") != "1"
                or build_args.get("DEBIAN_SNAPSHOT") != KERNEL_DEBIAN_SNAPSHOT):
            raise NotRun("kernel profile must enable its fixed-snapshot tool and script layer")
    elif service.get("build", {}).get("args", {}).get("FWROUTER_KERNEL_PREFLIGHT") not in (None, "0"):
        raise NotRun("ordinary profile cannot enable the kernel-preflight image layer")
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


def stopped_container_confinement_summary(value: Any) -> dict[str, Any]:
    """Return only allowlisted stopped-container confinement fields, never Env or Mounts."""
    config_raw = value.get("Config") if isinstance(value, dict) else None
    host_raw = value.get("HostConfig") if isinstance(value, dict) else None
    state_raw = value.get("State") if isinstance(value, dict) else None
    config = config_raw if isinstance(config_raw, dict) else {}
    host = host_raw if isinstance(host_raw, dict) else {}
    state = state_raw if isinstance(state_raw, dict) else {}

    def safe_mode(item: Any, limit: int = 96) -> str | None:
        if item is None:
            return None
        if not isinstance(item, str) or len(item) > limit or not re.fullmatch(r"[A-Za-z0-9_.:-]*", item):
            return "<invalid>"
        return item

    def safe_names(item: Any) -> tuple[list[str] | None, int | None]:
        if item is None:
            return None, None
        if not isinstance(item, list):
            return None, None
        count = len(item)
        values = [safe_mode(value, 48) or "" for value in item[:16]]
        if count > 16:
            values.append("<truncated>")
        return values, count

    cap_add, cap_add_count = safe_names(host.get("CapAdd"))
    cap_drop, cap_drop_count = safe_names(host.get("CapDrop"))
    devices = host.get("Devices")
    devices_count = len(devices) if isinstance(devices, list) else None
    return {
        "schema": "fwrouter-stopped-container-confinement/v1",
        "state": safe_mode(state.get("Status"), 32),
        "user": safe_mode(config.get("User"), 48),
        "readonly_rootfs": host.get("ReadonlyRootfs") if isinstance(host.get("ReadonlyRootfs"), bool) else None,
        "privileged": host.get("Privileged") if isinstance(host.get("Privileged"), bool) else None,
        "cap_add": cap_add, "cap_add_count": cap_add_count,
        "cap_drop": cap_drop, "cap_drop_count": cap_drop_count,
        "pid_mode": safe_mode(host.get("PidMode")),
        "ipc_mode": safe_mode(host.get("IpcMode")),
        "network_mode": safe_mode(host.get("NetworkMode")),
        "devices_count": devices_count,
        "devices_present": bool(devices) if isinstance(devices, (list, tuple, dict)) else None,
        "port_bindings_present": bool(host.get("PortBindings")),
    }


def validate_container_inspect(value: dict[str, Any], *, project: str, run_id: str, profile_path: Path,
                               image_id: str | None = None, kernel_preflight: bool = False) -> None:
    if not isinstance(value, dict) or not value.get("Id"):
        raise NotRun("container inspect did not return one concrete container")
    config = value.get("Config")
    host = value.get("HostConfig")
    state = value.get("State")
    if not isinstance(config, dict) or not isinstance(host, dict) or not isinstance(state, dict):
        raise NotRun("container inspect omitted required config, host, or state sections")
    labels = config.get("Labels", {})
    if labels.get(OWNER_LABEL) != OWNER_VALUE or labels.get(RUN_LABEL) != run_id or labels.get("com.docker.compose.project") != project:
        raise NotRun("created container labels do not prove this run owns it")
    if state.get("Status") != "created":
        raise NotRun("container must remain stopped until confinement inspection passes")
    expected_user = "0:0" if kernel_preflight else "10001:10001"
    if config.get("User") != expected_user:
        raise NotRun("container user differs from the selected acceptance profile")
    if host.get("ReadonlyRootfs") is not True:
        raise NotRun("container root filesystem is not read-only")
    if config.get("Entrypoint") != ["python"] or config.get("Cmd") != ["-c", "import signal; signal.pause()"]:
        raise NotRun("container is not held at the reviewed idle entrypoint")
    if image_id and value.get("Image") != image_id:
        raise NotRun("created container does not use the inspected acceptance image")
    if host.get("Privileged") is not False:
        raise NotRun("container is privileged")
    if host.get("NetworkMode") != f"{project}_isolated":
        raise NotRun("container network namespace is not its owned isolated network")
    cap_add = host.get("CapAdd") or []
    if kernel_preflight:
        # Docker inspect versions may spell this one capability with or without
        # its canonical CAP_ prefix. No other normalization is permitted.
        if cap_add not in (["NET_ADMIN"], ["CAP_NET_ADMIN"]):
            raise NotRun("kernel container CapAdd is not exactly one NET_ADMIN capability")
    elif cap_add != []:
        raise NotRun("ordinary container has added capabilities")
    if host.get("Devices") not in (None, []):
        raise NotRun("container has device mappings")
    if host.get("PidMode") not in (None, ""):
        raise NotRun("container uses a host or named PID namespace")
    if host.get("IpcMode") not in (None, "", "private"):
        raise NotRun("container uses a host or named IPC namespace")
    if host.get("CapDrop") != ["ALL"]:
        raise NotRun("runtime CapDrop is not exactly ALL")
    if "no-new-privileges:true" not in (host.get("SecurityOpt") or []):
        raise NotRun("runtime no-new-privileges policy is missing")
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


def packet_host_preflight() -> dict[str, Any]:
    """Read-only host checks before Docker creates the fixed packet subnets."""
    ip = shutil.which("ip", path="/usr/sbin:/sbin:/usr/bin:/bin")
    if not ip:
        raise NotRun("host iproute2 is required for fixed packet-subnet collision checks")
    try:
        route_result = subprocess.run([ip, "-j", "-4", "route", "show", "table", "all"],
                                       capture_output=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise NotRun("host route table could not be inspected before packet setup") from exc
    if route_result.returncode or len(route_result.stdout) > 256 * 1024:
        raise NotRun("host route table is unavailable or exceeds the packet preflight bound")
    try:
        routes = json.loads(route_result.stdout)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise NotRun("host route table is not valid bounded JSON") from exc
    if not isinstance(routes, list):
        raise NotRun("host route table has an unexpected shape")
    subnets = {name: ipaddress.ip_network(spec["subnet"]) for name, spec in PACKET_NETWORKS.items()}
    observed_routes = []
    for route in routes:
        if not isinstance(route, dict):
            raise NotRun("host route table contains an invalid entry")
        destination = route.get("dst")
        if destination in (None, "default"):
            continue
        try:
            network = ipaddress.ip_network(destination, strict=False)
        except (TypeError, ValueError) as exc:
            raise NotRun("host route table contains an unparseable destination") from exc
        observed_routes.append(str(network))
        for name, subnet in subnets.items():
            if network.version == subnet.version and network.overlaps(subnet):
                raise NotRun(f"fixed packet {name} subnet overlaps a hosted-runner route")
    route_bytes = json.dumps(sorted(routes, key=lambda row: json.dumps(
        row, sort_keys=True, separators=(",", ":"))), sort_keys=True,
        separators=(",", ":")).encode("utf-8")
    nft = shutil.which("nft", path="/usr/sbin:/sbin:/usr/bin:/bin")
    if not nft:
        raise NotRun("host nftables CLI is required for read-only packet firewall snapshots")
    try:
        sudo = shutil.which("sudo", path="/usr/bin:/bin")
        if not sudo:
            raise NotRun("host sudo is required for the bounded read-only nftables snapshot")
        nft_result = subprocess.run([sudo, "-n", nft, "-j", "list", "ruleset"], capture_output=True,
                                     timeout=8, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise NotRun("host nftables ruleset could not be inspected before packet setup") from exc
    if nft_result.returncode or len(nft_result.stdout) > 1024 * 1024:
        raise NotRun("host nftables ruleset is unavailable or exceeds the packet snapshot bound")
    try:
        nft_value = json.loads(nft_result.stdout)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise NotRun("host nftables ruleset is not valid bounded JSON") from exc
    nft_rows = nft_value.get("nftables") if isinstance(nft_value, dict) else None
    if not isinstance(nft_rows, list):
        raise NotRun("host nftables ruleset has an unexpected shape")

    def stable_nft(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: stable_nft(item) for key, item in sorted(value.items())
                    if key not in {"handle", "packets", "bytes", "counter"}}
        if isinstance(value, list):
            return [stable_nft(item) for item in value]
        return value

    semantic_nft = [stable_nft(row) for row in nft_rows
                    if isinstance(row, dict) and "metainfo" not in row]
    nft_bytes = json.dumps(semantic_nft, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "schema": "fwrouter-packet-host-preflight/v1",
        "host_netns_inode_sha256": hashlib.sha256(
            str(os.stat("/proc/self/ns/net").st_ino).encode("ascii")).hexdigest(),
        "route_count": len(routes),
        "route_snapshot_sha256": hashlib.sha256(route_bytes).hexdigest(),
        "firewall_snapshot_sha256": hashlib.sha256(nft_bytes).hexdigest(),
        "firewall_object_count": len(semantic_nft),
        "checked_subnets": {name: spec["subnet"] for name, spec in PACKET_NETWORKS.items()},
        "overlap_check": "passed",
    }


def validate_packet_service_fields(service_name: str, service: Any,
                                   expected_keys: set[str]) -> None:
    optional_null_fields = {"entrypoint"}
    if not isinstance(service, dict):
        raise NotRun(f"packet service fields differ from the closed three-role Compose contract role={service_name}")
    if "entrypoint" in service and service["entrypoint"] is not None:
        raise NotRun("packet service must inherit the fixed image entrypoint")
    observed_keys = set(service)
    allowed_keys = expected_keys | optional_null_fields
    if observed_keys == expected_keys or observed_keys == expected_keys | {"entrypoint"}:
        return
    # Compose's normalized JSON can include version-dependent defaults.
    # Report only bounded field names, never values, environment data,
    # paths, image references, or mount sources.
    missing_fields = sorted(expected_keys - observed_keys)
    unexpected_fields = sorted(
        key if isinstance(key, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", key)
        else "<other-field>"
        for key in observed_keys - allowed_keys
    )
    raise NotRun(
        "packet service fields differ from the closed three-role Compose contract "
        f"role={service_name} missing_fields={missing_fields[:24]} "
        f"unexpected_fields={unexpected_fields[:24]} "
        f"missing_count={len(missing_fields)} unexpected_count={len(observed_keys - allowed_keys)}"
    )


def validate_packet_compose_config(config: dict[str, Any], *, run_id: str,
                                   profile_paths: dict[str, Path]) -> None:
    services = config.get("services")
    networks = config.get("networks")
    if not isinstance(services, dict) or set(services) != {"application", "lanclient", "endpoint"}:
        raise NotRun("packet Compose must define exactly router, LAN client, and endpoint")
    if not isinstance(networks, dict) or set(networks) != {"lan", "wan"}:
        raise NotRun("packet Compose must define exactly the two fixed internal networks")
    expected_attachments = {"application": {"lan", "wan"}, "lanclient": {"lan"}, "endpoint": {"wan"}}
    expected_addresses = {
        "application": {"lan": "10.240.0.1", "wan": "198.18.240.1"},
        "lanclient": {"lan": "10.240.0.2"},
        "endpoint": {"wan": "198.18.240.2"},
    }
    expected_service_keys = {
        "application": {"build", "cap_add", "cap_drop", "command", "cpus", "environment", "image", "init",
                        "labels", "mem_limit", "memswap_limit", "networks", "pids_limit", "read_only",
                        "restart", "security_opt", "sysctls", "tmpfs", "user", "volumes"},
        "lanclient": {"cap_add", "cap_drop", "command", "cpus", "environment", "image", "init", "labels",
                      "mem_limit", "memswap_limit", "networks", "pids_limit", "read_only", "restart",
                      "security_opt", "tmpfs", "user", "volumes"},
        "endpoint": {"cap_add", "cap_drop", "command", "cpus", "environment", "image", "init", "labels",
                     "mem_limit", "memswap_limit", "networks", "pids_limit", "read_only", "restart",
                     "security_opt", "tmpfs", "user", "volumes"},
    }
    for service_name, attached_names in expected_attachments.items():
        service = services[service_name]
        expected_keys = expected_service_keys[service_name]
        validate_packet_service_fields(service_name, service, expected_keys)
        forbidden = {"env_file", "secrets", "configs", "volumes_from", "extra_hosts", "devices",
                     "runtime", "privileged", "network_mode", "pid", "ipc", "ports"}
        if forbidden & service.keys() or service.get("privileged"):
            raise NotRun("packet service contains an unreviewed host/runtime integration field")
        if service.get("cap_drop") != ["ALL"] or service.get("read_only") is not True:
            raise NotRun("packet services must drop all capabilities and use a read-only root")
        caps = service.get("cap_add")
        allowed_caps = ["NET_ADMIN", "NET_RAW"] if service_name == "application" else ["NET_ADMIN"]
        if caps != allowed_caps:
            raise NotRun("packet service capabilities differ from their fixed role")
        if service.get("user") != "0:0":
            raise NotRun("packet role must use the fixed isolated root identity")
        if service.get("security_opt") != ["no-new-privileges:true"] or service.get("init") is not True:
            raise NotRun("packet services require no-new-privileges and init")
        if service.get("restart") != "no" or service.get("command") != ["-c", "import signal; signal.pause()"]:
            raise NotRun("packet services must remain at the fixed idle command until verified")
        expected_image = f"fwrouter-acceptance:{run_id}"
        if service.get("image") != expected_image:
            raise NotRun("packet roles must use this run's single source-built image")
        resource_limits = {"application": (2 * 1024 ** 3, 256, 2.0),
                           "lanclient": (256 * 1024 ** 2, 64, 1.0),
                           "endpoint": (384 * 1024 ** 2, 96, 1.0)}[service_name]
        def bytes_value(raw: Any) -> int:
            text = str(raw).strip().lower()
            match = re.fullmatch(r"([0-9]+)([kmg]?)", text)
            if not match:
                return 0
            return int(match.group(1)) * 1024 ** {"": 0, "k": 1, "m": 2, "g": 3}[match.group(2)]
        try:
            cpus = float(service.get("cpus"))
        except (TypeError, ValueError) as exc:
            raise NotRun("packet service CPU limit is invalid") from exc
        memory_limit = bytes_value(service.get("mem_limit"))
        if (memory_limit != resource_limits[0] or bytes_value(service.get("memswap_limit")) != memory_limit
                or service.get("pids_limit") != resource_limits[1] or cpus != resource_limits[2]):
            raise NotRun("packet role resource limits differ from their fixed bounds")
        tmpfs = service.get("tmpfs")
        if isinstance(tmpfs, list) and len(tmpfs) == 1 and isinstance(tmpfs[0], str):
            tmpfs_spec = tmpfs[0]
        elif isinstance(tmpfs, dict) and set(tmpfs) == {"/tmp"}:
            tmpfs_spec = "/tmp:" + str(tmpfs["/tmp"])
        else:
            raise NotRun("packet role must have exactly one bounded temporary filesystem")
        target, separator, option_text = tmpfs_spec.partition(":")
        options = set(option_text.lower().split(","))
        expected_tmpfs_size = {"application": 512 * 1024 ** 2, "lanclient": 32 * 1024 ** 2,
                               "endpoint": 64 * 1024 ** 2}[service_name]
        size = next((bytes_value(item[5:]) for item in options if item.startswith("size=")), 0)
        if (target != "/tmp" or not separator or not {"rw", "noexec", "nosuid", "nodev", "mode=1777"} <= options
                or size != expected_tmpfs_size):
            raise NotRun("packet role temporary filesystem differs from its fixed bound")
        labels = service.get("labels", {})
        if labels.get(OWNER_LABEL) != OWNER_VALUE or labels.get(RUN_LABEL) != run_id:
            raise NotRun("packet service ownership labels do not match this run")
        network_map = service.get("networks")
        if not isinstance(network_map, dict) or set(network_map) != attached_names:
            raise NotRun("packet service network membership differs from its fixed role")
        for network_name, address in expected_addresses[service_name].items():
            if network_map[network_name].get("ipv4_address") != address:
                raise NotRun("packet service address differs from its fixed role assignment")
        mounts = service.get("volumes", [])
        if len(mounts) != 1:
            raise NotRun("packet role must have exactly one read-only profile bind")
        role_key = {"application": "router", "lanclient": "client", "endpoint": "endpoint"}[service_name]
        mount = mounts[0]
        if (mount.get("target") != "/run/fwrouter-acceptance/profile.json"
                or mount.get("read_only") is not True
                or Path(mount.get("source", "")).resolve() != profile_paths[role_key].resolve()):
            raise NotRun("packet role profile bind differs from its owned fixed profile")
        environment = service.get("environment")
        if service_name == "application":
            expected_environment = {
                "FWROUTER_ENVIRONMENT": "test", "FWROUTER_STATE_DIR": "/tmp/fwrouter-acceptance-state",
                "FWROUTER_STARTUP_TASKS_ENABLED": "0", "FWROUTER_ACCEPTANCE_PROFILE": "/run/fwrouter-acceptance/profile.json",
                "FWROUTER_XRAY_BINARY": "/opt/fwrouter-test/bin/xray",
                "FWROUTER_MIHOMO_BINARY": "/opt/fwrouter-test/bin/mihomo",
                "FWROUTER_BROWSER_EXECUTABLE": "/opt/fwrouter-test/chromium/chrome-linux64/chrome",
                "FWROUTER_CHROMIUM_BINARY": "/opt/fwrouter-test/chromium/chrome-linux64/chrome",
                "FWROUTER_APPLICATION_ACCEPTANCE_ROOT": "/tmp/fwrouter-application-acceptance",
                "FWROUTER_ACCEPTANCE_RECEIPT_PATH": "/tmp/fwrouter-receipts/application-acceptance.json",
                "PLAYWRIGHT_BROWSERS_PATH": "/opt/fwrouter-test/playwright-browsers",
                "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTHONDONTWRITEBYTECODE": "1", "HOME": "/tmp/fwrouter-home",
            }
        else:
            role = "client" if service_name == "lanclient" else "endpoint"
            expected_environment = {
                "FWROUTER_ACCEPTANCE_PROFILE": "/run/fwrouter-acceptance/profile.json",
                "FWROUTER_XRAY_BINARY": "/opt/fwrouter-test/bin/xray",
                "PYTHONDONTWRITEBYTECODE": "1", "HOME": "/tmp",
            }
        if not isinstance(environment, dict) or environment != expected_environment:
            raise NotRun("packet service environment differs from its fixed no-secret role profile")
    app = services["application"]
    if app.get("sysctls") != {"net.ipv4.ip_forward": "1"}:
        raise NotRun("router must enable only its namespaced IPv4 forwarding sysctl")
    build_args = app.get("build", {}).get("args", {})
    if (build_args.get("FWROUTER_KERNEL_PREFLIGHT") != "1"
            or build_args.get("FWROUTER_KERNEL_PACKET") != "1"
            or build_args.get("DEBIAN_SNAPSHOT") != KERNEL_DEBIAN_SNAPSHOT):
        raise NotRun("packet image must use the fixed kernel-tools snapshot build")
    for name, spec in PACKET_NETWORKS.items():
        network = networks[name]
        if network.get("internal") is not True or network.get("enable_ipv6") is not False:
            raise NotRun("packet networks must be internal IPv4-only Docker networks")
        labels = network.get("labels", {})
        if labels.get(OWNER_LABEL) != OWNER_VALUE or labels.get(RUN_LABEL) != run_id:
            raise NotRun("packet network labels do not match this run")
        ipam = network.get("ipam", {}).get("config", [])
        if len(ipam) != 1 or ipam[0].get("subnet") != spec["subnet"] or ipam[0].get("gateway") != spec.get("gateway"):
            raise NotRun("packet network IPAM differs from the fixed topology")


def validate_packet_container_inspect(value: dict[str, Any], *, project: str, run_id: str,
                                      image_id: str, role: str, profile_path: Path,
                                      require_networks: bool = False) -> None:
    if role not in {"router", "client", "endpoint"} or not isinstance(value, dict):
        raise NotRun("packet container inspect role is invalid")
    config, host, state = value.get("Config"), value.get("HostConfig"), value.get("State")
    if not isinstance(config, dict) or not isinstance(host, dict) or not isinstance(state, dict):
        raise NotRun("packet container inspect omitted required sections")
    labels = config.get("Labels", {})
    if (labels.get(OWNER_LABEL) != OWNER_VALUE or labels.get(RUN_LABEL) != run_id
            or labels.get("com.docker.compose.project") != project):
        raise NotRun("packet container ownership labels do not match this run")
    if state.get("Status") not in ({"created", "running"} if require_networks else {"created"}):
        raise NotRun("packet container state is outside its reviewed lifecycle")
    if value.get("Image") != image_id or config.get("User") != "0:0" or host.get("ReadonlyRootfs") is not True:
        raise NotRun("packet container image, user, or root filesystem differs from policy")
    if host.get("Privileged") is not False or host.get("CapDrop") != ["ALL"]:
        raise NotRun("packet container is privileged or does not drop all capabilities")
    expected_caps = {"NET_ADMIN", "NET_RAW"} if role == "router" else {"NET_ADMIN"}
    observed_caps = host.get("CapAdd") or []
    normalized_caps = {value.removeprefix("CAP_") for value in observed_caps}
    if len(observed_caps) != len(expected_caps) or normalized_caps != expected_caps:
        raise NotRun("packet container effective capability request differs from its role")
    if host.get("Devices") not in (None, []) or host.get("PortBindings"):
        raise NotRun("packet container has device mappings or published ports")
    if host.get("PidMode") not in (None, "") or host.get("IpcMode") not in (None, "", "private"):
        raise NotRun("packet container shares a host or named PID/IPC namespace")
    if "no-new-privileges:true" not in (host.get("SecurityOpt") or []):
        raise NotRun("packet container lacks no-new-privileges")
    if len(host.get("SecurityOpt") or []) != 1:
        raise NotRun("packet container has unreviewed runtime security options")
    if config.get("Entrypoint") != ["python"] or config.get("Cmd") != ["-c", "import signal; signal.pause()"]:
        raise NotRun("packet container command differs from the reviewed idle command")
    expected_resources = {"router": (2 * 1024 ** 3, 2_000_000_000, 256),
                          "client": (256 * 1024 ** 2, 1_000_000_000, 64),
                          "endpoint": (384 * 1024 ** 2, 1_000_000_000, 96)}[role]
    if (host.get("Memory") != expected_resources[0] or host.get("MemorySwap") != expected_resources[0]
            or host.get("NanoCpus") != expected_resources[1] or host.get("PidsLimit") != expected_resources[2]):
        raise NotRun("packet container runtime limits differ from the fixed role bounds")
    expected_tmpfs_size = {"router": "536870912", "client": "33554432", "endpoint": "67108864"}[role]
    tmpfs = host.get("Tmpfs")
    if not isinstance(tmpfs, dict) or set(tmpfs) != {"/tmp"}:
        raise NotRun("packet container runtime tmpfs is not limited to /tmp")
    tmpfs_options = set(str(tmpfs["/tmp"]).lower().split(","))
    if (not {"rw", "noexec", "nosuid", "nodev", "mode=1777"} <= tmpfs_options
            or f"size={expected_tmpfs_size}" not in tmpfs_options):
        raise NotRun("packet container runtime tmpfs options differ from its fixed role")
    allowed_network_modes = ({f"{project}_lan", f"{project}_wan"} if role == "router" else
                             {f"{project}_lan"} if role == "client" else {f"{project}_wan"})
    if host.get("NetworkMode") not in allowed_network_modes:
        raise NotRun("packet container primary network mode is not one of its exact owned networks")
    mounts = value.get("Mounts", [])
    binds = [mount for mount in mounts if mount.get("Type") == "bind"]
    if (len(mounts) != 2 or len(binds) != 1
            or binds[0].get("Destination") != "/run/fwrouter-acceptance/profile.json"
            or binds[0].get("RW") is not False
            or Path(binds[0].get("Source", "")).resolve() != profile_path.resolve()):
        raise NotRun("packet runtime mounts differ from one profile bind and one bounded tmpfs")
    tmpfs = [mount for mount in mounts if mount.get("Type") == "tmpfs"]
    if len(tmpfs) != 1 or tmpfs[0].get("Destination") != "/tmp" or tmpfs[0].get("RW") is not True:
        raise NotRun("packet runtime /tmp is not the only writable mount")
    settings = value.get("NetworkSettings", {}).get("Networks", {})
    expected = {
        "router": {f"{project}_lan": "10.240.0.1", f"{project}_wan": "198.18.240.1"},
        "client": {f"{project}_lan": "10.240.0.2"},
        "endpoint": {f"{project}_wan": "198.18.240.2"},
    }[role]
    if set(settings) != set(expected):
        raise NotRun("packet runtime network attachments differ from the role topology")
    if require_networks:
        for name, address in expected.items():
            if settings[name].get("IPAddress") != address:
                raise NotRun("packet runtime IPv4 address differs from the fixed topology")
    expected_env = {
        "PATH": "/usr/local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": "/workspace/backend",
        "FWROUTER_XRAY_BINARY": "/opt/fwrouter-test/bin/xray",
        "FWROUTER_MIHOMO_BINARY": "/opt/fwrouter-test/bin/mihomo",
        "FWROUTER_BROWSER_EXECUTABLE": "/opt/fwrouter-test/chromium/chrome-linux64/chrome",
        "FWROUTER_CHROMIUM_BINARY": "/opt/fwrouter-test/chromium/chrome-linux64/chrome",
        "PLAYWRIGHT_BROWSERS_PATH": "/opt/fwrouter-test/playwright-browsers",
        "FWROUTER_ACCEPTANCE_PROFILE": "/run/fwrouter-acceptance/profile.json",
    }
    expected_env.update({"HOME": "/tmp/fwrouter-home" if role == "router" else "/tmp"})
    if role == "router":
        expected_env.update({
            "FWROUTER_ENVIRONMENT": "test", "FWROUTER_STATE_DIR": "/tmp/fwrouter-acceptance-state",
            "FWROUTER_STARTUP_TASKS_ENABLED": "0",
            "FWROUTER_APPLICATION_ACCEPTANCE_ROOT": "/tmp/fwrouter-application-acceptance",
            "FWROUTER_ACCEPTANCE_RECEIPT_PATH": "/tmp/fwrouter-receipts/application-acceptance.json",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        })
    raw_env = config.get("Env")
    if not isinstance(raw_env, list):
        raise NotRun("packet container environment inventory is invalid")
    observed_env: dict[str, str] = {}
    for entry in raw_env:
        if not isinstance(entry, str) or "=" not in entry:
            raise NotRun("packet container environment entry is invalid")
        key, item = entry.split("=", 1)
        if key in observed_env or key.lower().endswith(("_secret", "_token", "_password", "_api_key")):
            raise NotRun("packet container environment has duplicate or credential-like entries")
        observed_env[key] = item
    if observed_env != expected_env:
        raise NotRun("packet runtime environment differs from its fixed role contract")


def validate_packet_network_inspect(value: dict[str, Any], *, project: str, run_id: str,
                                    network_name: str, attached_ids: set[str] | None = None) -> None:
    if network_name not in PACKET_NETWORKS or not isinstance(value, dict):
        raise NotRun("packet network inspection target is invalid")
    expected_name = f"{project}_{network_name}"
    if value.get("Name") != expected_name or value.get("Internal") is not True or value.get("EnableIPv6") is not False:
        raise NotRun("packet network identity or egress boundary differs from its fixed role")
    labels = value.get("Labels", {})
    if not isinstance(labels, dict):
        raise NotRun("packet network ownership labels are invalid")
    if labels.get(OWNER_LABEL) != OWNER_VALUE or labels.get(RUN_LABEL) != run_id:
        raise NotRun("packet network ownership labels do not match this run")
    options = value.get("Options", {})
    if not isinstance(options, dict):
        raise NotRun("packet network options inventory is invalid")
    if options.get("com.docker.network.bridge.enable_ip_masquerade") == "true":
        raise NotRun("packet network unexpectedly enables masquerading")
    spec = PACKET_NETWORKS[network_name]
    ipam = value.get("IPAM", {}).get("Config", [])
    if (len(ipam) != 1 or ipam[0].get("Subnet") != spec["subnet"]
            or ipam[0].get("Gateway") != spec["gateway"]):
        raise NotRun("packet network inspect IPAM differs from the fixed subnet and gateway")
    attached = value.get("Containers", {})
    if not isinstance(attached, dict):
        raise NotRun("packet network attachment inventory is invalid")
    if attached_ids is not None and set(attached) != attached_ids:
        raise NotRun("packet network attachment inventory differs from owned roles")


def validate_packet_profile_file(path: Path, *, owner_uid: int) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise NotRun("owned packet role profile is unavailable") from exc
    if (path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_uid != owner_uid
            or info.st_mode & 0o222 or info.st_size <= 0 or info.st_size > 64 * 1024):
        raise NotRun("owned packet role profile is not the expected owner read-only regular file")


def _packet_container_json(docker: str, container_id: str, args: list[str], *,
                           cwd: Path, env: dict[str, str], timeout: int = 10) -> Any:
    return _docker_json([docker, "exec", container_id, *args], cwd=cwd, env=env, timeout=timeout)


def _packet_container_command(docker: str, container_id: str, args: list[str], *,
                              cwd: Path, env: dict[str, str], timeout: int = 10) -> None:
    try:
        proc = subprocess.run([docker, "exec", container_id, *args], cwd=cwd, env=env,
                              capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise NotRun("fixed packet namespace command did not complete within its bound") from exc
    if proc.returncode or len(proc.stdout) > 4096 or len(proc.stderr) > 4096:
        raise NotRun("fixed packet namespace command failed or exceeded its output bound")


def packet_namespace_setup(docker: str, container_ids: dict[str, str], *,
                           cwd: Path, env: dict[str, str]) -> dict[str, Any]:
    """Assign only the reviewed per-container routes and endpoint VIP."""
    expected_addresses = {
        "router": {"10.240.0.1": "lan", "198.18.240.1": "wan"},
        "client": {"10.240.0.2": "lan"},
        "endpoint": {"198.18.240.2": "wan"},
    }
    interfaces: dict[str, dict[str, str]] = {}
    for role, wanted in expected_addresses.items():
        raw = _packet_container_json(docker, container_ids[role], ["ip", "-j", "-4", "address", "show"],
                                     cwd=cwd, env=env)
        if not isinstance(raw, list):
            raise NotRun("packet role address inventory has an invalid shape")
        observed: dict[str, str] = {}
        for interface in raw:
            if not isinstance(interface, dict) or not isinstance(interface.get("addr_info"), list):
                raise NotRun("packet role address inventory contains an invalid interface")
            for item in interface["addr_info"]:
                if isinstance(item, dict) and isinstance(item.get("local"), str):
                    address = item["local"]
                    if address in wanted:
                        name = interface.get("ifname")
                        if (not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", name)
                                or address in observed):
                            raise NotRun("packet role interface identity is ambiguous")
                        observed[address] = name
        if set(observed) != set(wanted):
            raise NotRun("packet role addresses do not match their fixed network assignments")
        interfaces[role] = {network: observed[address] for address, network in wanted.items()}

    expected_initial_subnets = {spec["subnet"] for spec in PACKET_NETWORKS.values()}
    for role, container_id in container_ids.items():
        raw = _packet_container_json(docker, container_id, ["ip", "-j", "-4", "route", "show", "table", "main"],
                                     cwd=cwd, env=env)
        if not isinstance(raw, list):
            raise NotRun("packet role route inventory has an invalid shape")
        defaults = []
        for route in raw:
            if not isinstance(route, dict):
                raise NotRun("packet role route inventory contains an invalid entry")
            destination = route.get("dst", "default")
            if destination == "default":
                defaults.append(route)
                gateway = route.get("gateway")
                if gateway not in {spec["gateway"] for spec in PACKET_NETWORKS.values()}:
                    raise NotRun("packet role inherited a default route outside its fixed Docker gateway")
            elif destination not in expected_initial_subnets:
                raise NotRun("packet role inherited an unexpected route before namespace setup")
        if len(defaults) > 1:
            raise NotRun("packet role has multiple inherited default routes")
        if defaults:
            _packet_container_command(docker, container_id, ["ip", "-4", "route", "del", "default"],
                                       cwd=cwd, env=env)

    router_if = interfaces["router"]["wan"]
    ifname = lambda role, network: interfaces[role][network]
    _packet_container_command(docker, container_ids["client"],
                              ["ip", "-4", "route", "replace", "default", "via", "10.240.0.1",
                               "dev", ifname("client", "lan")], cwd=cwd, env=env)
    _packet_container_command(docker, container_ids["endpoint"],
                              ["ip", "-4", "route", "replace", "10.240.0.0/29", "via", "198.18.240.1",
                               "dev", ifname("endpoint", "wan")], cwd=cwd, env=env)
    endpoint_addresses = _packet_container_json(docker, container_ids["endpoint"],
        ["ip", "-j", "-4", "address", "show", "dev", "lo"], cwd=cwd, env=env)
    if any(item.get("local") == "203.0.113.53" for row in endpoint_addresses if isinstance(row, dict)
           for item in row.get("addr_info", []) if isinstance(item, dict)):
        raise NotRun("packet endpoint service VIP was already present before owned setup")
    _packet_container_command(docker, container_ids["endpoint"],
                              ["ip", "-4", "address", "add", PACKET_VIP, "dev", "lo"],
                              cwd=cwd, env=env)
    _packet_container_command(docker, container_ids["router"],
                              ["ip", "-4", "route", "replace", PACKET_VIP, "via", "198.18.240.2",
                               "dev", router_if], cwd=cwd, env=env)

    expected_final = {
        "router": {("10.240.0.0/29", None), ("198.18.240.0/29", None), ("203.0.113.53/32", "198.18.240.2")},
        "client": {("10.240.0.0/29", None), ("default", "10.240.0.1")},
        "endpoint": {("198.18.240.0/29", None), ("10.240.0.0/29", "198.18.240.1")},
    }
    summaries: dict[str, Any] = {}
    for role, container_id in container_ids.items():
        raw_routes = _packet_container_json(docker, container_id,
            ["ip", "-j", "-4", "route", "show", "table", "main"], cwd=cwd, env=env)
        pairs = {(route.get("dst", "default"), route.get("gateway")) for route in raw_routes
                 if isinstance(route, dict)}
        if pairs != expected_final[role]:
            raise NotRun(f"{role} packet namespace routes differ from the fixed packet topology")
        summaries[role] = {"routes": sorted([{"destination": dst, "gateway": gateway}
                                                for dst, gateway in pairs],
                                             key=lambda item: (item["destination"], item["gateway"] or "")),
                           "interfaces": dict(sorted(interfaces[role].items()))}
    summaries["endpoint"]["service_vip"] = "203.0.113.53/32 on lo"
    return {"schema": "fwrouter-packet-topology/v1", "roles": summaries,
            "ipv4_forwarding": "enabled in router namespace"}


def packet_capture_start_code() -> str:
    """Start two fixed, header-only captures on the owned router WAN interface."""
    return r'''import ctypes, json, os, pathlib, re, selectors, subprocess, sys, time
root = pathlib.Path("/tmp/fwrouter-packet-evidence")
if root.is_symlink() or not root.is_dir() or root.stat().st_uid != 0 or root.stat().st_mode & 0o777 != 0o700:
    raise SystemExit("packet evidence directory ownership/mode is invalid")
iface = sys.argv[1] if len(sys.argv) == 2 else ""
if not re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", iface): raise SystemExit("WAN interface is invalid")
if pathlib.Path("/usr/bin/tcpdump").is_symlink() or not pathlib.Path("/usr/bin/tcpdump").is_file():
    raise SystemExit("pinned tcpdump path is unavailable")
if any((root / name).exists() or (root / name).is_symlink() for name in
       ("tcp.pcap", "udp.pcap", "tcp.stderr", "udp.stderr", "capture-process.json")):
    raise SystemExit("packet evidence paths already exist")
env = {"PATH":"/usr/sbin:/usr/bin:/sbin:/bin", "LANG":"C.UTF-8", "LC_ALL":"C.UTF-8", "TZ":"UTC"}
specs = (
    ("tcp", 54, 512, "(tcp and dst host 203.0.113.53 and dst port 9080) or (tcp and dst host 198.18.240.2 and dst port 5301)"),
    ("udp", 42, 512, "udp and dst host 203.0.113.53 and (dst port 9081 or dst port 5353)"),
)
processes = []
watch_fd = None
selector = selectors.DefaultSelector()
try:
    for name, snaplen, limit, expression in specs:
        output = root / (name + ".pcap")
        stderr_path = root / (name + ".stderr")
        error_stream = stderr_path.open("xb")
        os.chmod(stderr_path, 0o600)
        process = subprocess.Popen(
            ["/usr/bin/tcpdump", "-Z", "root", "-Q", "out", "-i", iface, "-p", "-n", "-U", "-s", str(snaplen),
             "-c", str(limit), "-w", str(output), expression],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=error_stream, close_fds=True,
            start_new_session=True, env=env, cwd=str(root))
        error_stream.close()
        processes.append((name, process, output, stderr_path))
    libc = ctypes.CDLL(None, use_errno=True)
    watch_fd = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
    if watch_fd < 0: raise OSError(ctypes.get_errno(), "inotify_init1 failed")
    for _, _, _, stderr_path in processes:
        if libc.inotify_add_watch(watch_fd, os.fsencode(stderr_path), 0x00000002) < 0:
            raise OSError(ctypes.get_errno(), "inotify_add_watch failed")
    selector.register(watch_fd, selectors.EVENT_READ, "stderr")
    pidfds = {}
    for name, process, _, _ in processes:
        pidfd = os.pidfd_open(process.pid, 0)
        pidfds[pidfd] = name
        selector.register(pidfd, selectors.EVENT_READ, name)
    deadline = time.monotonic() + 5.0
    ready = set()
    while len(ready) < 2:
        # Check the predicate before blocking: tcpdump can write its readiness
        # line before inotify watches are registered.
        for name, process, output, stderr_path in processes:
            current = stderr_path.read_bytes()[:2048]
            if output.is_file() and output.stat().st_size >= 24 and b"listening on" in current:
                ready.add(name)
            if process.poll() is not None:
                raise RuntimeError("a bounded tcpdump capture exited during startup")
        if len(ready) == 2:
            break
        events = selector.select(max(0.0, deadline - time.monotonic()))
        if not events: raise RuntimeError("bounded tcpdump readiness event timed out")
        for key, _ in events:
            if key.data != "stderr":
                raise RuntimeError("a bounded tcpdump capture exited during startup")
            os.read(watch_fd, 65536)
            for name, process, output, stderr_path in processes:
                current = stderr_path.read_bytes()[:2048]
                if output.is_file() and output.stat().st_size >= 24 and b"listening on" in current:
                    ready.add(name)
                if process.poll() is not None:
                    raise RuntimeError("a bounded tcpdump capture exited during startup")
    for pidfd in pidfds:
        selector.unregister(pidfd)
        os.close(pidfd)
    selector.unregister(watch_fd)
    os.close(watch_fd)
    watch_fd = None
    rows = {}
    for name, process, _, _ in processes:
        raw = pathlib.Path(f"/proc/{process.pid}/stat").read_text()
        tail = raw[raw.rfind(")") + 2:].split()
        rows[name] = {"pid":process.pid, "start_ticks":int(tail[19]), "snaplen":54 if name == "tcp" else 42,
                      "packet_limit":512}
    metadata = root / "capture-process.json"
    temp = root / ".capture-process.json.tmp"
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as stream:
        json.dump({"schema":"fwrouter-packet-capture-process/v1", "captures":rows}, stream,
                  sort_keys=True, separators=(",", ":"))
        stream.flush(); os.fsync(stream.fileno())
    os.replace(temp, metadata)
    print(json.dumps({"status":"started", "tcp_listener_ready":True, "udp_listener_ready":True,
                      "tcp_snaplen":54, "udp_snaplen":42, "packet_limit_per_capture":512},
                     sort_keys=True, separators=(",", ":")))
except Exception as exc:
    for _, process, _, _ in processes:
        if process.poll() is None:
            process.send_signal(2)
            try: process.wait(timeout=3)
            except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=2)
    print(json.dumps({"status":"failed", "exception_type":type(exc).__name__}, separators=(",", ":")))
    raise SystemExit(2)
finally:
    selector.close()
    if watch_fd is not None: os.close(watch_fd)
'''


def packet_capture_stop_code() -> str:
    """Stop only the two pidfd-verified tcpdump children created by packet_capture_start_code."""
    return r'''import json, os, pathlib, re, selectors, signal
root = pathlib.Path("/tmp/fwrouter-packet-evidence")
metadata = root / "capture-process.json"
if root.is_symlink() or not metadata.is_file() or metadata.is_symlink() or metadata.stat().st_size > 1024:
    raise SystemExit("owned packet capture process metadata is unavailable")
value = json.loads(metadata.read_text(encoding="ascii"))
if set(value) != {"schema", "captures"} or value.get("schema") != "fwrouter-packet-capture-process/v1":
    raise SystemExit("owned packet capture process metadata is invalid")
if set(value.get("captures", {})) != {"tcp", "udp"}:
    raise SystemExit("owned packet capture process set is invalid")
results = {}
for name, filename in (("tcp", "tcp.pcap"), ("udp", "udp.pcap")):
    row = value["captures"][name]
    pid = row.get("pid")
    if (type(pid) is not int or pid <= 1 or row.get("snaplen") != (54 if name == "tcp" else 42)
            or row.get("packet_limit") != 512 or type(row.get("start_ticks")) is not int):
        raise SystemExit("owned packet capture identity fields are invalid")
    try:
        pidfd = os.pidfd_open(pid, 0)
    except ProcessLookupError:
        results[name] = "already_exited"
        continue
    try:
        if os.readlink(f"/proc/{pid}/exe") != "/usr/bin/tcpdump":
            raise SystemExit("packet capture pid no longer identifies pinned tcpdump")
        stat_fields = pathlib.Path(f"/proc/{pid}/stat").read_text().split(")", 1)[1].split()
        if int(stat_fields[19]) != row["start_ticks"]:
            raise SystemExit("packet capture pid identity changed")
        argv = pathlib.Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        wanted_output = os.fsencode(str(root / filename))
        if argv[0] != b"/usr/bin/tcpdump" or b"-w" not in argv or argv[argv.index(b"-w") + 1] != wanted_output:
            raise SystemExit("packet capture process command differs from its owned output")
        signal.pidfd_send_signal(pidfd, signal.SIGINT)
        poller = selectors.DefaultSelector()
        poller.register(pidfd, selectors.EVENT_READ)
        if not poller.select(5.0):
            raise SystemExit("bounded tcpdump did not stop after its owned signal")
        poller.close()
        results[name] = "stopped"
    finally:
        os.close(pidfd)
files = {}
for name, snaplen in (("tcp",54),("udp",42)):
    path = root / (name + ".pcap")
    info = path.lstat()
    if (path.is_symlink() or not path.is_file() or info.st_uid != 0 or info.st_size < 24
            or info.st_size > 24 + 512 * (16 + snaplen)):
        raise SystemExit("owned packet capture output is not a bounded root-owned regular file")
    files[name] = {"uid":info.st_uid,"bytes":info.st_size,"mode":info.st_mode & 0o777}
print(json.dumps({"status":"stopped", "captures":results,"files":files}, sort_keys=True, separators=(",", ":")))
'''


def validate_packet_capture(path: Path, *, protocol: int, snaplen: int,
                            allowed_destinations: dict[tuple[str, int], set[int]]) -> dict[str, Any]:
    """Validate bounded Ethernet/IPv4 headers and reject any captured payload."""
    try:
        info = path.lstat()
        maximum = 24 + PACKET_CAPTURE_LIMIT * (16 + snaplen)
        if (path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_size < 24 or info.st_size > maximum):
            raise NotRun("packet capture file is outside its exported-owner/size/type bounds")
        raw = path.read_bytes()
    except OSError as exc:
        raise NotRun("packet capture file could not be safely read") from exc
    endian_by_magic = {b"\xd4\xc3\xb2\xa1": "<", b"\xa1\xb2\xc3\xd4": ">",
                       b"\x4d\x3c\xb2\xa1": "<", b"\xa1\xb2\x3c\x4d": ">"}
    endian = endian_by_magic.get(raw[:4])
    if endian is None or len(raw) < 24:
        raise NotRun("packet capture pcap header is invalid")
    major, minor, _zone, _sigfigs, observed_snaplen, linktype = struct.unpack_from(endian + "HHiiII", raw, 4)
    if (major != 2 or minor != 4 or observed_snaplen != snaplen or linktype != 1):
        raise NotRun("packet capture format differs from its fixed Ethernet profile")
    offset = 24
    counts: dict[str, int] = {}
    records = 0
    while offset < len(raw):
        if len(raw) - offset < 16:
            raise NotRun("packet capture contains a truncated record header")
        _seconds, _fraction, caplen, wirelen = struct.unpack_from(endian + "IIII", raw, offset)
        offset += 16
        if (caplen < 14 + 20 or caplen > snaplen or wirelen < caplen
                or offset + caplen > len(raw) or records >= PACKET_CAPTURE_LIMIT):
            raise NotRun("packet capture record exceeds its reviewed bounds")
        frame = raw[offset:offset + caplen]
        offset += caplen
        if frame[12:14] != b"\x08\x00":
            raise NotRun("packet capture contains a non-IPv4 Ethernet frame")
        ihl = (frame[14] & 0x0f) * 4
        if frame[14] >> 4 != 4 or ihl < 20 or caplen < 14 + ihl:
            raise NotRun("packet capture contains an invalid IPv4 header")
        total_length = struct.unpack_from("!H", frame, 16)[0]
        if total_length < ihl or frame[23] != protocol:
            raise NotRun("packet capture protocol differs from its fixed filter")
        source = socket.inet_ntoa(frame[26:30])
        destination = socket.inet_ntoa(frame[30:34])
        fragment = struct.unpack_from("!H", frame, 20)[0]
        if fragment & 0x1fff:
            raise NotRun("packet capture includes a noninitial IPv4 fragment")
        transport = 14 + ihl
        if protocol == 6:
            if caplen < transport + 20 or total_length < ihl + 20:
                raise NotRun("packet capture TCP header is incomplete")
            destination_port = struct.unpack_from("!H", frame, transport + 2)[0]
            tcp_hlen = ((frame[transport + 12] >> 4) & 0xf) * 4
            if tcp_hlen < 20 or total_length < ihl + tcp_hlen or caplen > transport + tcp_hlen:
                raise NotRun("packet capture includes TCP payload or a malformed TCP header")
        elif protocol == 17:
            if caplen < transport + 8 or total_length < ihl + 8 or caplen > transport + 8:
                raise NotRun("packet capture includes UDP payload or a malformed UDP header")
            destination_port = struct.unpack_from("!H", frame, transport + 2)[0]
        else:
            raise NotRun("packet capture validator protocol is not supported")
        ports = allowed_destinations.get((destination, protocol), set())
        if source not in {"10.240.0.2", "198.18.240.1"} or destination_port not in ports:
            raise NotRun("packet capture contains a source, destination, or port outside the owned fixture")
        key = f"{source}>{destination}:{destination_port}"
        counts[key] = counts.get(key, 0) + 1
        records += 1
    return {"records": records, "header_only": True, "snaplen": snaplen,
            "protocol": "tcp" if protocol == 6 else "udp", "flows": dict(sorted(counts.items())),
            "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}


def export_validated_packet_capture(copy_capture: Any, source: str, quarantine_path: Path,
                                    published_path: Path, *, owned_root: Path, artifact_root: Path,
                                    protocol: int, snaplen: int,
                                    allowed_destinations: dict[tuple[str, int], set[int]],
                                    receipt: dict[str, Any], name: str) -> dict[str, Any]:
    """Copy into private quarantine and publish only a header-validated capture."""

    def discard(path: Path, expected_parent: Path) -> None:
        try:
            if path.parent.resolve(strict=True) != expected_parent.resolve(strict=True):
                raise NotRun("packet capture cleanup path is outside its owned directory")
            info = path.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            path.unlink()
        else:
            raise NotRun("packet capture cleanup encountered a non-file path")

    try:
        owned_resolved = owned_root.resolve(strict=True)
        artifact_resolved = artifact_root.resolve(strict=True)
        if (quarantine_path.parent.resolve(strict=True) != owned_resolved
                or published_path.parent.resolve(strict=True) != artifact_resolved
                or owned_resolved == artifact_resolved
                or owned_resolved.is_relative_to(artifact_resolved)
                or artifact_resolved.is_relative_to(owned_resolved)
                or quarantine_path.exists() or quarantine_path.is_symlink()
                or published_path.exists() or published_path.is_symlink()):
            raise NotRun("packet capture paths are not fresh owned quarantine/export locations")
    except (NotRun, OSError, RuntimeError) as exc:
        cleanup_failed = False
        for path, parent in ((quarantine_path, owned_root), (published_path, artifact_root)):
            try:
                discard(path, parent)
            except (NotRun, OSError, RuntimeError):
                cleanup_failed = True
        if cleanup_failed:
            mark_cleanup_unconfirmed(receipt, "packet capture quarantine cleanup")
        mark_cleanup_unconfirmed(receipt, "packet capture export precondition")
        return {"copied": False, "published": False,
                "reason": "capture export precondition failed",
                "validation_error": str(exc)[:256]}

    copy_result: dict[str, Any] = {}
    try:
        copied = copy_capture(source, quarantine_path)
        if not isinstance(copied, dict):
            raise NotRun("packet capture copy returned an invalid result")
        copy_result = copied
        if copied.get("copied") is not True:
            discard(quarantine_path, owned_root)
            discard(published_path, artifact_root)
            mark_cleanup_unconfirmed(receipt, "packet capture copy failed")
            return {"copied": False, "published": False,
                    "exit_code": copied.get("exit_code"), "method": copied.get("method", "docker-cp"),
                    "reason": "capture copy failed"}
        proof = validate_packet_capture(quarantine_path, protocol=protocol, snaplen=snaplen,
                                        allowed_destinations=allowed_destinations)
        os.replace(quarantine_path, published_path)
        receipt.setdefault("packet_captures", {})[name] = proof
        return {"copied": True, "published": True, "exit_code": copied.get("exit_code", 0),
                "method": copied.get("method", "docker-cp"), "proof": proof}
    except (NotRun, OSError, ValueError) as exc:
        try:
            discard(quarantine_path, owned_root)
            discard(published_path, artifact_root)
        except (NotRun, OSError, RuntimeError):
            mark_cleanup_unconfirmed(receipt, "packet capture quarantine cleanup")
        mark_cleanup_unconfirmed(receipt, "packet capture evidence")
        return {"copied": False, "published": False,
                "exit_code": copy_result.get("exit_code"),
                "method": copy_result.get("method", "docker-cp"),
                "validation_error": str(exc)[:256]}


def packet_role_readiness_code() -> str:
    """Poll only the fixed role-control endpoints from the router namespace."""
    return r'''import json, time, urllib.error, urllib.request
targets = {"client":("http://10.240.0.2:8081/ready", lambda x: x.get("role")=="client" and x.get("ready") is True),
           "endpoint":("http://198.18.240.2:8082/ready", lambda x: x.get("role")=="endpoint" and x.get("ready") is True and isinstance(x.get("sockets"),dict) and all(x["sockets"].get(k) is True for k in ("xray_tcp_5301","http_tcp_9080","udp_echo_9081","dns_udp_5353")))}
deadline=time.monotonic()+10.0; ready={}
while time.monotonic()<deadline and len(ready)<2:
    for role,(url,valid) in targets.items():
        if role in ready: continue
        try:
            with urllib.request.urlopen(url, timeout=0.4) as response:
                body=response.read(4097)
                if response.status==200 and len(body)<=4096:
                    value=json.loads(body)
                    if isinstance(value,dict) and valid(value): ready[role]=True
        except (OSError, ValueError, urllib.error.URLError): pass
if set(ready)!={"client","endpoint"}: raise SystemExit("fixed packet role endpoints did not become ready")
print(json.dumps({"status":"passed","roles":sorted(ready)},sort_keys=True,separators=(",",":")))
'''


def packet_router_guard_setup_code(lan_interface: str, wan_interface: str) -> str:
    """Install a separate closed packet guard after fixed router routes are verified."""
    for value in (lan_interface, wan_interface):
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", value):
            raise NotRun("router guard interface is outside the fixed role contract")
    return f'''import json, subprocess
lan={lan_interface!r}; wan={wan_interface!r}; table="fwrouter_packet_guard"
counter_names={{"router_stub_drop","router_egress_drop","router_forward_drop","router_input_drop",
               "router_ipv6_output_drop","router_ipv6_input_drop","router_ipv6_forward_drop"}}
rules=[
 f'add table inet {{table}}',
 *[f'add counter inet {{table}} {{name}}' for name in sorted(counter_names)],
 'add chain inet fwrouter_packet_guard output {{ type filter hook output priority -10; policy drop; }}',
 'add chain inet fwrouter_packet_guard input {{ type filter hook input priority -10; policy drop; }}',
 'add chain inet fwrouter_packet_guard forward {{ type filter hook forward priority -10; policy drop; }}',
 'add rule inet fwrouter_packet_guard output ip daddr 127.0.0.11 counter name router_stub_drop drop comment "fwrouter packet guard dns stub deny v1"',
 'add rule inet fwrouter_packet_guard output meta nfproto ipv6 oifname != "lo" counter name router_ipv6_output_drop drop comment "fwrouter packet guard ipv6 output deny v1"',
 'add rule inet fwrouter_packet_guard output oifname "lo" accept comment "fwrouter packet guard loopback allow v1"',
 'add rule inet fwrouter_packet_guard output ct state {{ established, related }} accept comment "fwrouter packet guard established output allow v1"',
 'add rule inet fwrouter_packet_guard output ip daddr 10.240.0.2 tcp dport 8081 accept comment "fwrouter packet guard client control allow v1"',
 'add rule inet fwrouter_packet_guard output ip daddr 198.18.240.2 tcp dport 8082 accept comment "fwrouter packet guard endpoint control allow v1"',
 'add rule inet fwrouter_packet_guard output ip daddr 198.18.240.2 tcp dport 5301 accept comment "fwrouter packet guard vless allow v1"',
 'add rule inet fwrouter_packet_guard output ip daddr 203.0.113.53 tcp dport 9080 accept comment "fwrouter packet guard vip tcp allow v1"',
 'add rule inet fwrouter_packet_guard output ip daddr 203.0.113.53 udp dport {{ 9081, 5353 }} accept comment "fwrouter packet guard vip udp allow v1"',
 'add rule inet fwrouter_packet_guard output counter name router_egress_drop drop comment "fwrouter packet guard egress deny v1"',
 'add rule inet fwrouter_packet_guard input iifname "lo" accept comment "fwrouter packet guard loopback input allow v1"',
 'add rule inet fwrouter_packet_guard input meta nfproto ipv6 iifname != "lo" counter name router_ipv6_input_drop drop comment "fwrouter packet guard ipv6 input deny v1"',
 'add rule inet fwrouter_packet_guard input ct state {{ established, related }} accept comment "fwrouter packet guard established input allow v1"',
 f'add rule inet fwrouter_packet_guard input iifname "{{lan}}" ip saddr 10.240.0.2 ip daddr 203.0.113.53 tcp dport 9080 accept comment "fwrouter packet guard lan vip tcp input allow v1"',
 f'add rule inet fwrouter_packet_guard input iifname "{{lan}}" ip saddr 10.240.0.2 ip daddr 203.0.113.53 udp dport {{{{ 9081, 5353 }}}} accept comment "fwrouter packet guard lan vip udp input allow v1"',
 f'add rule inet fwrouter_packet_guard input iifname "{{lan}}" ip saddr 10.240.0.2 ip daddr 10.240.0.1 tcp dport {{{{ 5202, 5204 }}}} accept comment "fwrouter packet guard lan tproxy tcp input allow v1"',
 f'add rule inet fwrouter_packet_guard input iifname "{{lan}}" ip saddr 10.240.0.2 ip daddr 10.240.0.1 udp dport {{{{ 5203, 5205 }}}} accept comment "fwrouter packet guard lan tproxy udp input allow v1"',
 'add rule inet fwrouter_packet_guard input counter name router_input_drop drop comment "fwrouter packet guard input deny v1"',
 'add rule inet fwrouter_packet_guard forward meta nfproto ipv6 counter name router_ipv6_forward_drop drop comment "fwrouter packet guard ipv6 forward deny v1"',
 'add rule inet fwrouter_packet_guard forward ct state {{ established, related }} accept comment "fwrouter packet guard established forward allow v1"',
 f'add rule inet fwrouter_packet_guard forward iifname "{{lan}}" oifname "{{wan}}" ip saddr 10.240.0.2 ip daddr 203.0.113.53 tcp dport 9080 accept comment "fwrouter packet guard lan vip tcp forward allow v1"',
 f'add rule inet fwrouter_packet_guard forward iifname "{{lan}}" oifname "{{wan}}" ip saddr 10.240.0.2 ip daddr 203.0.113.53 udp dport {{{{ 9081, 5353 }}}} accept comment "fwrouter packet guard lan vip udp forward allow v1"',
 'add rule inet fwrouter_packet_guard forward counter name router_forward_drop drop comment "fwrouter packet guard forward deny v1"',
]
script="\\n".join(rules)+"\\n"
present=subprocess.run(["/usr/sbin/nft","-j","list","table","inet",table],capture_output=True,timeout=5,check=False)
if present.returncode == 0: raise SystemExit("router packet guard table already exists")
if present.stdout.strip(): raise SystemExit("router packet guard preflight returned unexpected output")
checked=subprocess.run(["/usr/sbin/nft","-c","-f","-"],input=script.encode(),capture_output=True,timeout=8,check=False)
if checked.returncode: raise SystemExit("router packet guard syntax check failed")
applied=subprocess.run(["/usr/sbin/nft","-f","-"],input=script.encode(),capture_output=True,timeout=8,check=False)
if applied.returncode: raise SystemExit("router packet guard apply failed")
readback=subprocess.run(["/usr/sbin/nft","-j","list","table","inet",table],capture_output=True,timeout=8,check=False)
if readback.returncode or len(readback.stdout)>65536: raise SystemExit("router packet guard readback failed")
value=json.loads(readback.stdout); rows=value.get("nftables",[])
chains={{row["chain"]["name"]:row["chain"] for row in rows if isinstance(row,dict) and isinstance(row.get("chain"),dict)}}
expected_hooks={{"input":"input","output":"output","forward":"forward"}}
if set(chains)!=set(expected_hooks): raise SystemExit("router packet guard chain inventory differs")
for name,hook in expected_hooks.items():
    chain=chains[name]
    if chain.get("hook")!=hook or chain.get("policy")!="drop" or chain.get("prio")!=-10:
        raise SystemExit("router packet guard chain policy differs")
observed_counters={{row["counter"].get("name") for row in rows if isinstance(row,dict) and isinstance(row.get("counter"),dict)}}
if observed_counters!=counter_names: raise SystemExit("router packet guard counter inventory differs")
comments={{row["rule"].get("comment") for row in rows if isinstance(row,dict) and isinstance(row.get("rule"),dict)}}
expected_comments={{line.split('comment "',1)[1].split('"',1)[0] for line in rules if ' comment "' in line}}
if comments!=expected_comments: raise SystemExit("router packet guard rule inventory differs")
print(json.dumps({{"status":"passed","table":"inet fwrouter_packet_guard","chains":sorted(chains),
                  "counters":sorted(counter_names),"rules":len(comments),"priority":-10,
                  "dns_stub_drop_precedes_loopback_allow":True}},sort_keys=True,separators=(",",":")))
'''


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


def compose_service_lookup_argv(docker: str, compose_args: list[str], project: str,
                                service_name: str) -> list[str]:
    """Build the closed Compose service lookup, including stopped containers."""
    if service_name not in {"application", "lanclient", "endpoint"}:
        raise NotRun("Compose service lookup is outside the exact acceptance role set")
    return [docker, "compose", *compose_args, "-p", project, "ps", "--all", "-q", service_name]


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
    if suite in _KERNEL_SUITES:
        return set()
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
        if suite in _DIAGNOSTIC_NODEIDS:
            diagnostics = {_DIAGNOSTIC_NODEIDS[suite]}
        elif suite == "provider-cohort":
            diagnostics = set(_PROVIDER_COHORT_NODEIDS)
        elif suite == "fence-diagnostic":
            diagnostics = set(_FENCE_DIAGNOSTIC_NODEIDS)
        elif suite == "target-diagnostic":
            diagnostics = set(_TARGET_DIAGNOSTIC_NODEIDS)
        elif suite in {"recovery-diagnostic", _KERNEL_RECOVERY_SUITE}:
            diagnostics = set(_RECOVERY_DIAGNOSTIC_NODEIDS)
        elif suite == _PACKET_DIAGNOSTIC_SUITE:
            diagnostics = set(_PACKET_DIAGNOSTIC_NODEIDS)
        else:
            raise NotRun("diagnostic suite has no fixed node set")
        expected_catalog_suite = "packet" if suite == _PACKET_DIAGNOSTIC_SUITE else "functional"
        if not diagnostics.issubset({row["nodeid"] for row in rows if row["suite"] == expected_catalog_suite}):
            raise NotRun("fixed diagnostic node set is absent from its exact source catalog suite")
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


def validate_application_receipt_scope(receipt: Any, profile: Any) -> None:
    if not isinstance(receipt, dict) or not isinstance(profile, dict):
        raise NotRun("application receipt/profile scope is invalid")
    expected = profile.get("profile")
    if expected not in {"hosted-native-process", "hosted-kernel-dataplane", "hosted-kernel-packet"}:
        raise NotRun("selected profile is not an application-test profile")
    if receipt.get("scope") != expected:
        raise NotRun("application receipt scope does not match the selected profile")


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
    return r'''import hashlib, importlib.metadata, json, os, pathlib, subprocess, sys
p = json.loads(pathlib.Path("/run/fwrouter-acceptance/profile.json").read_text())
assert p["schema"] == "fwrouter-acceptance-profile/v2"
assert p["profile"] in {"hosted-native-process", "hosted-kernel-preflight", "hosted-kernel-dataplane", "hosted-kernel-packet"}
kernel_mode = p["profile"] in {"hosted-kernel-preflight", "hosted-kernel-dataplane", "hosted-kernel-packet"}
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
source_files.extend(pathlib.Path("/workspace", name) for name in (
    "host/libexec/fwrouter/dataplane-common.sh", "host/libexec/fwrouter/dataplane-check.sh",
    "host/libexec/fwrouter/dataplane-apply.sh", "host/libexec/fwrouter/dataplane-rollback.sh"))
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
if kernel_mode:
    assert os.geteuid() == 0 and os.getegid() == 0
    kernel_key = "kernel_preflight" if p["profile"] == "hosted-kernel-preflight" else "kernel_dataplane"
    k = p[kernel_key]
    assert set(k) == {"host_netns_inode_sha256", "dataplane_scripts"}
    own_ns_hash = hashlib.sha256(str(os.stat("/proc/self/ns/net").st_ino).encode()).hexdigest()
    assert own_ns_hash != k["host_netns_inode_sha256"]
    assert os.stat("/proc/self/ns/net").st_ino == os.stat("/proc/1/ns/net").st_ino
    status = pathlib.Path("/proc/self/status").read_text()
    cap_line = next(line for line in status.splitlines() if line.startswith("CapEff:"))
    expected_cap_eff = (1 << 12) | (1 << 13) if p["profile"] == "hosted-kernel-packet" else (1 << 12)
    assert int(cap_line.split()[1], 16) == expected_cap_eff
    expected_scripts = {
      "host/libexec/fwrouter/dataplane-common.sh": "/usr/local/libexec/fwrouter/dataplane-common.sh",
      "host/libexec/fwrouter/dataplane-check.sh": "/usr/local/libexec/fwrouter/dataplane-check.sh",
      "host/libexec/fwrouter/dataplane-apply.sh": "/usr/local/libexec/fwrouter/dataplane-apply.sh",
      "host/libexec/fwrouter/dataplane-rollback.sh": "/usr/local/libexec/fwrouter/dataplane-rollback.sh",
    }
    assert set(k["dataplane_scripts"]) == set(expected_scripts)
    for source, target in expected_scripts.items():
        source_path = pathlib.Path("/workspace", source)
        target_path = pathlib.Path(target)
        expected_sha = k["dataplane_scripts"][source]
        assert source_path.is_file() and not source_path.is_symlink() and sha(source_path) == expected_sha
        assert target_path.is_file() and not target_path.is_symlink() and sha(target_path) == expected_sha
        assert target_path.stat().st_mode & 0o777 == 0o755
    if p["profile"] == "hosted-kernel-packet":
        network = p["network_testbed"]
        assert set(network) == {"role", "host_netns_inode_sha256"}
        assert network["role"] == "router"
        assert network["host_netns_inode_sha256"] == k["host_netns_inode_sha256"]
        capture = p["packet_capture"]
        assert set(capture) == {"tcpdump_version"}
        version = subprocess.run(["/usr/bin/tcpdump", "--version"], capture_output=True,
            text=True, timeout=5, stdin=subprocess.DEVNULL,
            env={"PATH":"/usr/bin:/bin","LANG":"C.UTF-8","TZ":"UTC"})
        assert version.returncode == 0 and capture["tcpdump_version"] in (version.stdout + version.stderr)[:1024]
else:
    assert "kernel_preflight" not in p and "kernel_dataplane" not in p and "network_testbed" not in p and "packet_capture" not in p
for raw in ("/tmp/fwrouter-home", "/tmp/fwrouter-acceptance-state",
            "/tmp/fwrouter-application-acceptance", "/tmp/fwrouter-receipts"):
    path = pathlib.Path(raw)
    assert not path.is_symlink()
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.stat()
    assert info.st_uid == __import__("os").getuid() and info.st_mode & 0o077 == 0
print(json.dumps({"status":"passed","profile_nonce":p["suite_nonce"],"playwright":importlib.metadata.version("playwright")}))
'''


def kernel_preflight_code() -> str:
    """Run actual nft/ip capability checks inside the owned app netns."""
    return r'''import hashlib, json, os, pathlib, re, shutil, stat, subprocess
p = json.loads(pathlib.Path("/run/fwrouter-acceptance/profile.json").read_text())
assert p["schema"] == "fwrouter-acceptance-profile/v2" and p["profile"] in {"hosted-kernel-preflight", "hosted-kernel-dataplane", "hosted-kernel-packet"}
k = p["kernel_preflight"] if p["profile"] == "hosted-kernel-preflight" else p["kernel_dataplane"]
assert os.geteuid() == 0
status = pathlib.Path("/proc/self/status").read_text()
cap = next(line for line in status.splitlines() if line.startswith("CapEff:"))
expected_cap_eff = ((1 << 12) | (1 << 13)) if p["profile"] == "hosted-kernel-packet" else (1 << 12)
assert int(cap.split()[1], 16) == expected_cap_eff
ns = os.stat("/proc/self/ns/net").st_ino
assert ns == os.stat("/proc/1/ns/net").st_ino
assert hashlib.sha256(str(ns).encode()).hexdigest() != k["host_netns_inode_sha256"]
def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(65536), b""): h.update(block)
    return h.hexdigest()
paths = {
 "host/libexec/fwrouter/dataplane-common.sh":"/usr/local/libexec/fwrouter/dataplane-common.sh",
 "host/libexec/fwrouter/dataplane-check.sh":"/usr/local/libexec/fwrouter/dataplane-check.sh",
 "host/libexec/fwrouter/dataplane-apply.sh":"/usr/local/libexec/fwrouter/dataplane-apply.sh",
 "host/libexec/fwrouter/dataplane-rollback.sh":"/usr/local/libexec/fwrouter/dataplane-rollback.sh",
}
env = {"PATH":"/usr/sbin:/usr/bin:/sbin:/bin", "HOME":"/tmp", "LANG":"C.UTF-8", "TZ":"UTC"}
last_command, last_stderr, last_exit = "", "", None
def redact(text):
    text = re.sub(r"(?i)(password|passwd|secret|token|authorization|api[_-]?key)\s*[:=]\s*[^\s,;]+", r"\1=[REDACTED]", text)
    text = re.sub(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,}\b", "[UUID]", text)
    text = re.sub(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b", "[EMAIL]", text)
    return text[-1024:]
def run(argv):
    proc = subprocess.run(argv, capture_output=True, timeout=8, check=False, env=env)
    assert len(proc.stdout) <= 8192 and len(proc.stderr) <= 4096
    global last_command, last_stderr, last_exit
    last_command = pathlib.Path(argv[0]).name + " " + " ".join(argv[1:3])
    last_stderr = redact(proc.stderr.decode("utf-8", "replace"))
    last_exit = proc.returncode
    return proc
def checked(argv):
    proc = run(argv)
    if proc.returncode != 0:
        raise RuntimeError("native command failed")
    return proc
result = {"schema":"fwrouter-kernel-preflight/v1", "status":"failed", "tools":{},
          "packages":{}, "scripts":{}, "tproxy_rule_readback":False,
          "nft_check":False, "policy_route_readback":False, "baseline_nft_objects_preserved":False,
          "cleanup":"not_started", "netns_distinct_from_host":True,
          "capabilities_verified":True}
assert set(k["dataplane_scripts"]) == set(paths)
for source, target in paths.items():
    expected = k["dataplane_scripts"][source]
    checks = []
    for name, path in (("source", pathlib.Path("/workspace", source)), ("target", pathlib.Path(target))):
        checks.append({"location":name, "exists":path.is_file() and not path.is_symlink(),
                       "sha256_match":path.is_file() and not path.is_symlink() and digest(path) == expected,
                       "mode_0755":path.is_file() and not path.is_symlink() and stat.S_IMODE(path.stat().st_mode) == 0o755})
    result["scripts"][target] = checks
for name in ("nftables", "iproute2"):
    proc = run(["dpkg-query", "-W", "-f=${db:Status-Status}:${Version}", name])
    value = proc.stdout.decode("ascii", "replace").strip() if proc.returncode == 0 else "unavailable"
    result["packages"][name] = {"installed":proc.returncode == 0 and value.startswith("installed:"),
                                "version":value.split(":", 1)[1] if value.startswith("installed:") else None}
nft, ip = shutil.which("nft", path=env["PATH"]), shutil.which("ip", path=env["PATH"])
result["tools"] = {"nft":nft, "ip":ip}
if (not all(check["exists"] and check["sha256_match"] and check["mode_0755"]
            for checks in result["scripts"].values() for check in checks)
        or not all(value["installed"] for value in result["packages"].values()) or not nft or not ip):
    result["failure_stage"] = "script_or_tool_preflight"
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    raise SystemExit(2)
def table_routes():
    rows = json.loads(checked([ip, "-j", "-4", "route", "show", "table", "all"]).stdout)
    return [row for row in rows if row.get("table") in (100, "100")]
def table_names(ruleset):
    return {(row["table"].get("family"), row["table"].get("name"))
            for row in ruleset.get("nftables", []) if isinstance(row, dict)
            and isinstance(row.get("table"), dict)}
def baseline_objects(ruleset, own_table):
    def stable(value):
        if isinstance(value, dict):
            if "counter" in value:
                return {key: stable(item) for key, item in value.items() if key not in {"handle", "packets", "bytes"}}
            return {key: stable(item) for key, item in value.items()
                    if key not in {"metainfo", "handle", "packets", "bytes", "expires"}}
        if isinstance(value, list): return [stable(item) for item in value]
        return value
    result = []
    for row in ruleset.get("nftables", []):
        if not isinstance(row, dict): continue
        objects = [value for value in row.values() if isinstance(value, dict)]
        if any(value.get("family") == "inet" and value.get("table") == own_table for value in objects):
            continue
        result.append(json.dumps(stable(row), sort_keys=True, separators=(",", ":")))
    return sorted(result)
table = "fwrouter_preflight_" + p["suite_nonce"][:10]
created_table = created_route = created_rule = False
stage = "initial_namespace_readback"
failed = None
initial_tables, initial_objects = set(), None
try:
    initial = json.loads(checked([nft, "-j", "list", "ruleset"]).stdout)
    initial_tables = table_names(initial)
    assert ("inet", table) not in initial_tables
    initial_objects = baseline_objects(initial, table)
    rules = json.loads(checked([ip, "-j", "-4", "rule", "show"]).stdout)
    routes = table_routes()
    assert not routes and not any(row.get("priority") == 100 or row.get("fwmark") in ("0x100", "256") for row in rules)
    stage = "nft_tproxy_apply_readback"
    checked([nft,"add","table","inet",table]); created_table = True
    checked([nft,"add","chain","inet",table,"prerouting","{","type","filter","hook","prerouting","priority","mangle",";","policy","accept",";","}"])
    checked([nft,"-c","add","rule","inet",table,"prerouting","meta","l4proto","udp","tproxy","to",":12345","meta","mark","set","0x100"])
    result["nft_check"] = True
    checked([nft,"add","rule","inet",table,"prerouting","meta","l4proto","udp","tproxy","to",":12345","meta","mark","set","0x100"])
    listed = checked([nft,"-j","list","table","inet",table]).stdout.decode("utf-8","strict")
    assert "tproxy" in listed and "12345" in listed and ("0x100" in listed or "256" in listed)
    result["tproxy_rule_readback"] = True
    stage = "ip_policy_route_apply_readback"
    checked([ip,"-4","route","replace","local","default","dev","lo","table","100"]); created_route = True
    checked([ip,"-4","rule","add","priority","100","fwmark","0x100","table","100"]); created_rule = True
    route_rows = table_routes()
    rule_rows = json.loads(checked([ip,"-j","-4","rule","show"]).stdout)
    assert any(row.get("type")=="local" and row.get("dst")=="default" and row.get("dev")=="lo" for row in route_rows)
    assert any(row.get("priority")==100 and row.get("fwmark") in ("0x100","256") and row.get("table") in (100,"100") for row in rule_rows)
    result["policy_route_readback"] = True
except Exception as exc:
    failed = type(exc).__name__
    result["failure_stage"] = stage
    result["failure_type"] = failed
    result["failure_command"] = last_command[:160]
    result["failure_exit_code"] = last_exit
    result["stderr_tail"] = redact(last_stderr)
finally:
    stage = "cleanup"
    cleanup = True
    if created_rule: cleanup &= run([ip,"-4","rule","del","priority","100","fwmark","0x100","table","100"]).returncode == 0
    if created_route: cleanup &= run([ip,"-4","route","del","local","default","dev","lo","table","100"]).returncode == 0
    if created_table: cleanup &= run([nft,"delete","table","inet",table]).returncode == 0
    try:
        final_ruleset = json.loads(checked([nft,"-j","list","ruleset"]).stdout)
        final_tables = table_names(final_ruleset)
        final_objects = baseline_objects(final_ruleset, table)
        remaining_routes = table_routes()
        remaining_rules = json.loads(checked([ip,"-j","-4","rule","show"]).stdout)
        baseline_preserved = initial_objects is not None and initial_objects == final_objects
        result["baseline_nft_objects_preserved"] = baseline_preserved
        clean = (cleanup and ("inet", table) not in final_tables and initial_tables.issubset(final_tables)
                 and baseline_preserved
                 and not remaining_routes
                 and not any(row.get("priority")==100 and row.get("fwmark") in ("0x100","256") for row in remaining_rules))
        result["cleanup"] = "verified" if clean else "failed"
        if not clean:
            result["failure_stage"] = "cleanup"
            failed = failed or "CleanupVerificationFailed"
    except Exception as exc:
        result["cleanup"] = "failed"
        result["failure_stage"] = "cleanup"
        result["failure_type"] = type(exc).__name__
        failed = failed or type(exc).__name__
if failed is None:
    result["status"] = "passed"
print(json.dumps(result, sort_keys=True, separators=(",", ":")))
raise SystemExit(0 if result["status"] == "passed" else 2)
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


def valid_image_measurements(value: Any) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "base_image_present_before_build", "base_image_inspect_seconds",
        "compose_build_seconds", "built_image_bytes",
    }:
        return False
    if not isinstance(value["base_image_present_before_build"], bool):
        return False
    for field in ("base_image_inspect_seconds", "compose_build_seconds"):
        duration = value[field]
        if not isinstance(duration, (int, float)) or isinstance(duration, bool):
            return False
        try:
            valid = math.isfinite(float(duration)) and duration >= 0
        except (OverflowError, TypeError, ValueError):
            valid = False
        if not valid:
            return False
    size = value["built_image_bytes"]
    return size is None or (isinstance(size, int) and not isinstance(size, bool) and size > 0)


def _base_image_cache_presence(docker: str, image: str, *, cwd: Path,
                               env: dict[str, str]) -> tuple[bool, float]:
    started = time.monotonic()
    try:
        proc = subprocess.run([docker, "image", "inspect", image], cwd=cwd, env=env,
                              capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise NotRun("bounded base-image cache inspection failed") from exc
    elapsed = round(time.monotonic() - started, 3)
    if proc.returncode == 0:
        if len(proc.stdout) > 16 * 1024:
            raise NotRun("base-image cache inspection exceeded its output bound")
        try:
            observed = json.loads(proc.stdout)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise NotRun("base-image cache inspection returned invalid JSON") from exc
        if (not isinstance(observed, list) or len(observed) != 1
                or not isinstance(observed[0], dict) or not observed[0].get("Id")):
            raise NotRun("base-image cache inspection returned an invalid image identity")
        return True, elapsed
    stderr = proc.stderr[:4096].decode("utf-8", "replace").lower()
    if proc.returncode == 1 and "no such image" in stderr:
        return False, elapsed
    raise NotRun("base-image cache presence could not be determined")


def run_hosted_acceptance(env: dict[str, str], facts: dict[str, Any], *, suite: str,
                          allow_recovery: bool = False) -> dict[str, Any]:
    if suite not in {"functional", "recovery", *_DIAGNOSTIC_SUITES, *_KERNEL_SUITES} or (suite == "recovery") != allow_recovery:
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
    packet_mode = suite == _PACKET_DIAGNOSTIC_SUITE
    packet_profile_paths = {
        "router": profile_path,
        "client": profile_dir / "client-profile.json",
        "endpoint": profile_dir / "endpoint-profile.json",
    }
    report_path = artifact_dir / "hosted-acceptance-report.json"
    junit_host = artifact_dir / "application-acceptance.xml"
    output_host = artifact_dir / "pytest-output.txt"
    compose_file = ROOT / "tests/acceptance/compose.yaml"
    compose_files = [compose_file]
    kernel_preflight = suite in _KERNEL_SUITES
    kernel_dataplane = suite in {_KERNEL_RECOVERY_SUITE, _PACKET_DIAGNOSTIC_SUITE}
    kernel_profile = kernel_preflight or kernel_dataplane
    if kernel_profile:
        compose_files.append(ROOT / "tests/acceptance/compose.kernel-preflight.yaml")
    if packet_mode:
        compose_files.append(ROOT / "tests/acceptance/compose.packet.yaml")
    compose_args = [part for path in compose_files for part in ("-f", str(path))]
    context = root / "context"
    receipt: dict[str, Any] = {
        "schema_version": 1, "status": "NOTRUN",
        "scope": "hosted-kernel-preflight" if kernel_preflight else "hosted-kernel-packet" if packet_mode else "hosted-kernel-dataplane" if kernel_dataplane else
                 "hosted-native-diagnostic" if suite in _DIAGNOSTIC_SUITES else "hosted-native-process",
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
                      "kernel_preflight": str(artifact_dir / "kernel-preflight.json"),
                      "state_summary": str(artifact_dir / "state-snapshot.json"),
                      "compose_logs": str(artifact_dir / "compose-logs.txt")},
        "diagnostic_only": suite in _DIAGNOSTIC_SUITES or kernel_profile,
        "expected_ids": sorted(expected_acceptance_nodeids(suite)),
        "container_confinement": "not_checked", "tests": None,
    }
    container_id: str | None = None
    network_id: str | None = None
    packet_container_ids: dict[str, str] = {}
    packet_network_ids: dict[str, str] = {}
    packet_capture_started = False
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
    if packet_mode:
        docker_env.update({
            "FWROUTER_PACKET_CLIENT_PROFILE_FILE": str(packet_profile_paths["client"]),
            "FWROUTER_PACKET_ENDPOINT_PROFILE_FILE": str(packet_profile_paths["endpoint"]),
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
            "schema": PROFILE_SCHEMA,
            "profile": "hosted-kernel-preflight" if kernel_preflight else
                       "hosted-kernel-packet" if packet_mode else
                       "hosted-kernel-dataplane" if kernel_dataplane else "hosted-native-process",
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
        if kernel_profile:
            kernel_profile_field = "kernel_preflight" if kernel_preflight else "kernel_dataplane"
            profile[kernel_profile_field] = {
                "host_netns_inode_sha256": hashlib.sha256(
                    str(os.stat("/proc/self/ns/net").st_ino).encode()).hexdigest(),
                "dataplane_scripts": {relative: sha256_file(context / relative)
                                      for relative in KERNEL_SCRIPT_SOURCES},
            }
        if packet_mode:
            profile["profile_owner_uid"] = os.getuid()
            host_netns_hash = hashlib.sha256(str(os.stat("/proc/self/ns/net").st_ino).encode("ascii")).hexdigest()
            profile["network_testbed"] = {"role": "router", "host_netns_inode_sha256": host_netns_hash}
            profile["packet_capture"] = {"tcpdump_version": PACKET_TCPDUMP_VERSION}
        required_versions = [profile["xray"]["version"], profile["mihomo"]["version"],
                             profile["chromium"]["version"], profile["playwright_python"]]
        if not all(isinstance(value, str) and value.strip() for value in required_versions):
            raise NotRun("pinned Xray/Mihomo/Chromium/Playwright version inputs are incomplete")
        if profile["playwright_python"] != "1.55.0":
            raise NotRun("Playwright Python must match the hashed acceptance lock (1.55.0)")
        profile_path.write_text(json.dumps(profile, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        profile_path.chmod(0o444)
        if packet_mode:
            validate_packet_profile_file(profile_path, owner_uid=os.getuid())
        shutil.copyfile(profile_path, artifact_dir / "profile.json")
        (artifact_dir / "profile.json").chmod(0o444)
        if packet_mode:
            for role in ("client", "endpoint"):
                role_profile = {
                    "schema": PROFILE_SCHEMA,
                    "profile": "hosted-kernel-packet",
                    "suite_nonce": run_id,
                    "profile_owner_uid": os.getuid(),
                    "network_testbed": {"role": role, "host_netns_inode_sha256": host_netns_hash},
                    "xray": profile["xray"],
                }
                role_path = packet_profile_paths[role]
                role_path.write_text(json.dumps(role_profile, sort_keys=True, indent=2) + "\n", encoding="utf-8")
                role_path.chmod(0o444)
                validate_packet_profile_file(role_path, owner_uid=os.getuid())
                role_artifact = artifact_dir / f"{role}-profile.json"
                shutil.copyfile(role_path, role_artifact)
                role_artifact.chmod(0o444)
            receipt["packet_host_preflight"] = packet_host_preflight()
            packet_host_path = artifact_dir / "packet-host-preflight.json"
            packet_host_path.write_text(json.dumps(receipt["packet_host_preflight"], sort_keys=True, indent=2)
                                        + "\n", encoding="utf-8")
            receipt["artifacts"]["packet_host_preflight"] = str(packet_host_path)
            receipt["artifacts"]["role_profiles"] = {
                role: str(artifact_dir / f"{role}-profile.json") for role in ("client", "endpoint")}
        config = _docker_json([docker, "compose", *compose_args, "-p", project, "config", "--format", "json"],
                              cwd=ROOT, env=docker_env)
        if packet_mode:
            validate_packet_compose_config(config, run_id=run_id, profile_paths=packet_profile_paths)
        else:
            validate_compose_config(config, run_id=run_id, profile_path=profile_path,
                                    kernel_preflight=kernel_profile)
        base_image_present, base_inspect_seconds = _base_image_cache_presence(
            docker, env["FWROUTER_ACCEPTANCE_BASE_IMAGE"], cwd=ROOT, env=docker_env)
        receipt["image_measurements"] = {
            "base_image_present_before_build": base_image_present,
            "base_image_inspect_seconds": base_inspect_seconds,
            "compose_build_seconds": 0.0,
            "built_image_bytes": None,
        }
        build_started = time.monotonic()
        try:
            subprocess.run([docker, "compose", *compose_args, "-p", project, "build", "application"],
                           cwd=ROOT, env=docker_env, check=True, timeout=300,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        finally:
            receipt["image_measurements"]["compose_build_seconds"] = round(time.monotonic() - build_started, 3)
        image_raw = _docker_json([docker, "image", "inspect", f"fwrouter-acceptance:{run_id}"], cwd=ROOT, env=docker_env)
        if not isinstance(image_raw, list) or len(image_raw) != 1:
            raise NotRun("acceptance image inspect failed")
        receipt["image_measurements"]["built_image_bytes"] = image_raw[0].get("Size")
        if not valid_image_measurements(receipt["image_measurements"]):
            raise NotRun("bounded image measurements are invalid")
        image_labels = image_raw[0].get("Config", {}).get("Labels", {})
        image_id = image_raw[0].get("Id")
        if (image_labels.get(OWNER_LABEL) != OWNER_VALUE or image_labels.get(RUN_LABEL) != run_id
                or image_labels.get("org.opencontainers.image.revision") != receipt["source_revision"] or not image_id):
            raise NotRun("built image lacks acceptance ownership label")
        if image_raw[0].get("Architecture") != "amd64" or image_raw[0].get("Os") != "linux":
            raise NotRun("acceptance image must be Linux amd64")
        roles = ("application", "lanclient", "endpoint") if packet_mode else ("application",)
        subprocess.run([docker, "compose", *compose_args, "-p", project, "create", *roles],
                       cwd=ROOT, env=docker_env, check=True, timeout=60,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        role_ids: dict[str, str] = {}
        packet_container_ids = role_ids if packet_mode else {}
        for role in roles:
            role_key = "router" if role == "application" else "client" if role == "lanclient" else "endpoint"
            role_ids[role_key] = _docker_text(
                compose_service_lookup_argv(docker, compose_args, project, role),
                cwd=ROOT, env=docker_env)
        container_id = role_ids["router"]
        if not re.fullmatch(r"[0-9a-f]{64}", container_id or ""):
            raise NotRun("could not resolve exact created container identity")
        if packet_mode and (set(role_ids) != {"router", "client", "endpoint"}
                            or any(not re.fullmatch(r"[0-9a-f]{64}", item or "") for item in role_ids.values())
                            or len(set(role_ids.values())) != 3):
            raise NotRun("could not resolve three distinct exact packet-role container identities")
        network_raw = (None if packet_mode else _docker_json(
            [docker, "network", "inspect", f"{project}_isolated"], cwd=ROOT, env=docker_env))
        if packet_mode:
            if not isinstance(network_raw, list) or len(network_raw) != 2:
                raise NotRun("packet Compose did not create exactly two owned networks")
            for name in ("lan", "wan"):
                rows = _docker_json([docker, "network", "inspect", f"{project}_{name}"], cwd=ROOT, env=docker_env)
                if not isinstance(rows, list) or len(rows) != 1:
                    raise NotRun("packet network inspect returned an unexpected result")
                validate_packet_network_inspect(rows[0], project=project, run_id=run_id, network_name=name)
                packet_network_ids[name] = rows[0].get("Id")
            if any(not value for value in packet_network_ids.values()):
                raise NotRun("packet network inspect omitted an owned network identity")
            confinement_rows = {}
            for role, identifier in packet_container_ids.items():
                inspected = _docker_json([docker, "inspect", identifier], cwd=ROOT, env=docker_env)
                if not isinstance(inspected, list) or len(inspected) != 1:
                    raise NotRun("packet container inspect returned an unexpected result")
                validate_packet_container_inspect(inspected[0], project=project, run_id=run_id,
                    image_id=image_id, role=role, profile_path=packet_profile_paths[role])
                confinement_rows[role] = stopped_container_confinement_summary(inspected[0])
            receipt["stopped_container_confinement"] = confinement_rows
            receipt["container_confinement"] = "passed"
        else:
            if not isinstance(network_raw, list) or len(network_raw) != 1:
                raise NotRun("network inspect returned an unexpected result")
            validate_network_inspect(network_raw[0], project=project, run_id=run_id, container_id=container_id)
            network_id = network_raw[0].get("Id")
            if not network_id:
                raise NotRun("network inspect omitted owned network id")
            inspected = _docker_json([docker, "inspect", container_id], cwd=ROOT, env=docker_env)
            if not isinstance(inspected, list) or len(inspected) != 1:
                raise NotRun("container inspect returned an unexpected result")
            confinement = stopped_container_confinement_summary(inspected[0])
            receipt["stopped_container_confinement"] = confinement
            try:
                validate_container_inspect(inspected[0], project=project, run_id=run_id, profile_path=profile_path,
                                           image_id=image_id, kernel_preflight=kernel_profile)
            except NotRun as exc:
                confinement["validator_result"] = "rejected"
                confinement["validator_reason"] = str(exc)[:192]
                raise
            confinement["validator_result"] = "accepted"
            network_raw = _docker_json([docker, "network", "inspect", network_id], cwd=ROOT, env=docker_env)
            if not isinstance(network_raw, list) or len(network_raw) != 1:
                raise NotRun("network inspect returned an unexpected result")
            validate_network_inspect(network_raw[0], project=project, run_id=run_id, container_id=container_id)
            receipt["container_confinement"] = "passed"
        subprocess.run([docker, "compose", *compose_args, "-p", project, "start", *roles],
                       cwd=ROOT, env=docker_env, check=True, timeout=30,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if packet_mode:
            for name, network_id_value in packet_network_ids.items():
                rows = _docker_json([docker, "network", "inspect", network_id_value], cwd=ROOT, env=docker_env)
                expected = {packet_container_ids["router"], packet_container_ids["client"]} if name == "lan" else {
                    packet_container_ids["router"], packet_container_ids["endpoint"]}
                if not isinstance(rows, list) or len(rows) != 1:
                    raise NotRun("started packet network inspect returned an unexpected result")
                validate_packet_network_inspect(rows[0], project=project, run_id=run_id,
                                                network_name=name, attached_ids=expected)
            for role, identifier in packet_container_ids.items():
                inspected = _docker_json([docker, "inspect", identifier], cwd=ROOT, env=docker_env)
                if not isinstance(inspected, list) or len(inspected) != 1:
                    raise NotRun("started packet container inspect returned an unexpected result")
                validate_packet_container_inspect(inspected[0], project=project, run_id=run_id,
                    image_id=image_id, role=role, profile_path=packet_profile_paths[role], require_networks=True)
            receipt["packet_topology"] = packet_namespace_setup(docker, packet_container_ids, cwd=ROOT, env=docker_env)
            router_interfaces = receipt["packet_topology"]["roles"]["router"]["interfaces"]
            guard = _docker_exec_capture(
                [docker, "exec", packet_container_ids["router"], "python", "-c",
                 packet_router_guard_setup_code(router_interfaces["lan"], router_interfaces["wan"])],
                cwd=ROOT, env=docker_env, timeout=30)
            try:
                guard_result = json.loads(guard.get("stdout", ""))
            except json.JSONDecodeError:
                guard_result = {}
            if guard["exit_code"] != 0 or guard_result.get("status") != "passed":
                raise NotRun("closed router packet namespace guard did not install and verify")
            receipt["packet_router_guard"] = guard_result
        else:
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
        if kernel_profile:
            kernel_path = artifact_dir / "kernel-preflight.json"
            command = _docker_exec_capture(
                [docker, "exec", container_id, "python", "-c", kernel_preflight_code()],
                cwd=ROOT, env=docker_env, timeout=60)
            try:
                kernel_result = json.loads(command.get("stdout", ""))
            except json.JSONDecodeError:
                kernel_result = {"schema": "fwrouter-kernel-preflight/v1", "status": "failed",
                                 "exit_code": command.get("exit_code"),
                                 "stderr": command.get("stderr", "")[:2048]}
            kernel_path.write_text(json.dumps(kernel_result, sort_keys=True, indent=2) + "\n", encoding="utf-8")
            receipt["kernel_preflight"] = kernel_result
            receipt["artifacts"]["kernel_preflight"] = str(kernel_path)
            if command["exit_code"] != 0 or kernel_result.get("status") != "passed":
                raise NotRun("kernel primitive preflight failed before application acceptance")
            if kernel_preflight:
                receipt["tests"] = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0,
                                    "nodeids": [], "node_status": {}}
                receipt["profile"] = profile
                receipt["image_id"] = image_id
                receipt["container_id"] = container_id
                receipt["network_id"] = network_id
                receipt["report_path"] = str(report_path)
                receipt["status"] = "passed"
                _write_receipt(report_path, receipt)
                return receipt
        if packet_mode:
            for role in ("client", "endpoint"):
                result = subprocess.run([docker, "exec", "-d", packet_container_ids[role], "python",
                                        "/workspace/tests/acceptance/network_fixture.py", role],
                                       cwd=ROOT, env=docker_env, capture_output=True, timeout=15, check=False)
                if result.returncode or len(result.stdout) > 1024 or len(result.stderr) > 1024:
                    raise NotRun(f"owned {role} packet fixture did not start")
            readiness = _docker_exec_capture(
                [docker, "exec", container_id, "python", "-c", packet_role_readiness_code()],
                cwd=ROOT, env=docker_env, timeout=20)
            if readiness["exit_code"] != 0:
                raise NotRun("fixed client and endpoint packet services did not become ready")
            wan_interface = receipt["packet_topology"]["roles"]["router"]["interfaces"]["wan"]
            evidence_setup = "import os,pathlib; p=pathlib.Path('/tmp/fwrouter-packet-evidence'); p.mkdir(mode=0o700); os.chmod(p,0o700)"
            setup = _docker_exec_capture([docker, "exec", container_id, "python", "-c", evidence_setup],
                                         cwd=ROOT, env=docker_env, timeout=10)
            if setup["exit_code"] != 0:
                raise NotRun("owned packet evidence directory could not be created")
            capture = _docker_exec_capture([docker, "exec", container_id, "python", "-c",
                                            packet_capture_start_code(), wan_interface],
                                           cwd=ROOT, env=docker_env, timeout=15)
            try:
                capture_result = json.loads(capture.get("stdout", ""))
            except json.JSONDecodeError:
                capture_result = {}
            packet_capture_started = capture_result.get("status") == "started"
            if (capture["exit_code"] != 0 or capture_result.get("status") != "started"
                    or capture_result.get("tcp_snaplen") != 54 or capture_result.get("udp_snaplen") != 42
                    or capture_result.get("packet_limit_per_capture") != PACKET_CAPTURE_LIMIT):
                raise NotRun("bounded header-only packet captures did not start")
            receipt["packet_capture_runtime"] = capture_result
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
        validate_application_receipt_scope(suite_receipt, profile)
        if (suite_receipt.get("schema") != "fwrouter-application-acceptance-receipt/v2"
                or suite_receipt.get("source_revision") != receipt["source_revision"]
                or suite_receipt.get("plan_digest") != receipt["plan_digest"]
                or suite_receipt.get("profile_sha256") != receipt["profile_sha256"]
                or suite_receipt.get("suite_nonce") != run_id
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
                                "kernel_preflight": str(artifact_dir / "kernel-preflight.json"),
                                "state_summary": str(artifact_dir / "state-snapshot.json"),
                                "compose_logs": str(artifact_dir / "compose-logs.txt")}
        if packet_mode:
            receipt["artifacts"].update({
                "packet_host_preflight": str(artifact_dir / "packet-host-preflight.json"),
                "router_profile": str(artifact_dir / "profile.json"),
                "client_profile": str(artifact_dir / "client-profile.json"),
                "endpoint_profile": str(artifact_dir / "endpoint-profile.json"),
                "packet_role_logs": str(artifact_dir / "packet-role-logs.json"),
                "tcp_capture": str(artifact_dir / "packet-tcp.pcap"),
                "udp_capture": str(artifact_dir / "packet-udp.pcap"),
            })
        if kernel_preflight:
            pass
        elif code == 0 and receipt["tests"]["failures"] == 0 and receipt["tests"]["errors"] == 0 and receipt["tests"]["skipped"] == 0:
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
        if packet_mode and container_id and packet_capture_started:
            stopped = _docker_exec_capture([docker, "exec", container_id, "python", "-c",
                                            packet_capture_stop_code()],
                                           cwd=ROOT, env=docker_env, timeout=15)
            try:
                stop_result = json.loads(stopped.get("stdout", ""))
            except json.JSONDecodeError:
                stop_result = {}
            if stopped["exit_code"] != 0 or stop_result.get("status") != "stopped":
                mark_cleanup_unconfirmed(receipt, "packet captures")
                receipt["packet_capture_stop"] = {"status": "unconfirmed"}
            else:
                receipt["packet_capture_stop"] = stop_result
            capture_exports = {}
            for name, protocol, snaplen, destinations in (
                ("tcp", 6, 54, {("203.0.113.53", 6): {9080}, ("198.18.240.2", 6): {5301}}),
                ("udp", 17, 42, {("203.0.113.53", 17): {9081, 5353}}),
            ):
                destination = artifact_dir / f"packet-{name}.pcap"
                if stopped["exit_code"] != 0 or stop_result.get("status") != "stopped":
                    capture_exports[name] = {"copied": False, "reason": "capture stop unconfirmed"}
                    continue
                quarantine = root / f"packet-{name}.pcap.quarantine"
                capture_exports[name] = export_validated_packet_capture(
                    lambda source, target: _docker_copy_capture(
                        docker, container_id, source, target, env=docker_env),
                    f"/tmp/fwrouter-packet-evidence/{name}.pcap", quarantine, destination,
                    owned_root=root, artifact_root=artifact_dir, protocol=protocol, snaplen=snaplen,
                    allowed_destinations=destinations, receipt=receipt, name=name)
            receipt["packet_capture_exports"] = capture_exports
        if container_id:
            # Save bounded runtime/service output and in-container diagnostic receipts
            # before deleting the exact owned container. Export statuses remain visible.
            if packet_mode:
                role_logs = {}
                for role, identifier in packet_container_ids.items():
                    logs = _docker_exec_capture([docker, "logs", "--timestamps", identifier],
                                                cwd=ROOT, env=docker_env, timeout=20)
                    role_logs[role] = {"exit_code": logs["exit_code"],
                                       "output": _redact_public(logs.get("stdout", "") + "\n" +
                                                                logs.get("stderr", ""), limit=16 * 1024)}
                (artifact_dir / "packet-role-logs.json").write_text(
                    json.dumps(role_logs, sort_keys=True, indent=2) + "\n", encoding="utf-8")
            else:
                logs = _docker_exec_capture([docker, "compose", *compose_args, "-p", project,
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
        cleanup_container_ids = dict(packet_container_ids) if packet_mode else ({"router": container_id} if container_id else {})
        if packet_mode:
            for role, service_name in (("router", "application"), ("client", "lanclient"), ("endpoint", "endpoint")):
                try:
                    discovered = _docker_text(
                        compose_service_lookup_argv(docker, compose_args, project, service_name),
                        cwd=ROOT, env=docker_env)
                    if discovered:
                        if not re.fullmatch(r"[0-9a-f]{64}", discovered):
                            raise NotRun("packet cleanup discovery returned an invalid container identity")
                        cleanup_container_ids[role] = discovered
                except (NotRun, OSError, subprocess.SubprocessError):
                    mark_cleanup_unconfirmed(receipt, f"container-discovery:{role}")
        for role, cleanup_id in cleanup_container_ids.items():
            if not isinstance(cleanup_id, str) or not re.fullmatch(r"[0-9a-f]{64}", cleanup_id):
                continue
            try:
                try:
                    inspected = _docker_json([docker, "inspect", cleanup_id], cwd=ROOT, env=docker_env)
                except NotRun:
                    if _docker_id_present("container", cleanup_id, cwd=ROOT, env=docker_env):
                        raise NotRun("container inspect failed while the resource still exists")
                    inspected = []
                if inspected:
                    if not isinstance(inspected, list) or len(inspected) != 1:
                        raise NotRun("container cleanup inspect returned an unexpected shape")
                    labels = inspected[0].get("Config", {}).get("Labels", {})
                    if (labels.get(OWNER_LABEL) != OWNER_VALUE or labels.get(RUN_LABEL) != run_id
                            or labels.get("com.docker.compose.project") != project):
                        raise NotRun("container cleanup ownership labels do not match this run")
                    subprocess.run([docker, "rm", "-f", cleanup_id], cwd=ROOT, env=docker_env,
                                   check=True, timeout=30, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    if _docker_id_present("container", cleanup_id, cwd=ROOT, env=docker_env):
                        raise NotRun("container remains after owned-resource removal")
            except (NotRun, OSError, subprocess.SubprocessError):
                mark_cleanup_unconfirmed(receipt, f"container:{role}")
        cleanup_network_names = (set(PACKET_NETWORKS) if packet_mode else ({"isolated"} if network_id else set()))
        for network_name in sorted(cleanup_network_names):
            try:
                cleanup_id = packet_network_ids.get(network_name) if packet_mode else network_id
                inspect_target = cleanup_id or f"{project}_{network_name}"
                raw = _docker_json([docker, "network", "inspect", inspect_target], cwd=ROOT, env=docker_env)
                if not isinstance(raw, list) or len(raw) != 1:
                    raise NotRun("network cleanup inspect returned an unexpected shape")
                if packet_mode:
                    validate_packet_network_inspect(raw[0], project=project, run_id=run_id,
                                                    network_name=network_name, attached_ids=set())
                else:
                    validate_network_inspect(raw[0], project=project, run_id=run_id)
                cleanup_id = raw[0].get("Id")
                if not isinstance(cleanup_id, str) or not cleanup_id:
                    raise NotRun("owned network identity is missing during cleanup")
                subprocess.run([docker, "network", "rm", cleanup_id], cwd=ROOT, env=docker_env,
                               check=True, timeout=20, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if _docker_id_present("network", cleanup_id, cwd=ROOT, env=docker_env):
                    raise NotRun("network remains after owned-resource removal")
            except (NotRun, OSError, subprocess.SubprocessError):
                mark_cleanup_unconfirmed(receipt, f"network:{network_name}")
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
        if packet_mode and isinstance(receipt.get("packet_host_preflight"), dict):
            try:
                after_host = packet_host_preflight()
                after_path = artifact_dir / "packet-host-post-cleanup.json"
                after_path.write_text(json.dumps(after_host, sort_keys=True, indent=2) + "\n", encoding="utf-8")
                receipt["packet_host_post_cleanup"] = after_host
                before_host = receipt["packet_host_preflight"]
                for key in ("host_netns_inode_sha256", "route_snapshot_sha256", "firewall_snapshot_sha256",
                            "route_count", "firewall_object_count"):
                    if after_host.get(key) != before_host.get(key):
                        raise NotRun("host route/firewall snapshot changed after packet resource cleanup")
                receipt["artifacts"]["packet_host_post_cleanup"] = str(after_path)
            except (NotRun, OSError, subprocess.SubprocessError, ValueError) as exc:
                receipt["packet_host_post_cleanup_error"] = str(exc)[:512]
                mark_cleanup_unconfirmed(receipt, "host route/firewall snapshot")
        any_owned_resources = bool(container_id or network_id or packet_container_ids or packet_network_ids or image_id)
        receipt.setdefault("cleanup", "owned_resources_removed" if any_owned_resources else "no_container_resources_created")
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
    parser.add_argument("--suite", choices=("functional", "recovery", "xray-diagnostic", "provider-diagnostic", "browser-diagnostic", "provider-cohort", "fence-diagnostic", "target-diagnostic", "recovery-diagnostic", "kernel-preflight", "kernel-recovery-diagnostic", "packet-diagnostic"), default="functional")
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
