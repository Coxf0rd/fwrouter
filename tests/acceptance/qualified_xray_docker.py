#!/usr/bin/env python3
"""Run the exact Xray Docker-adapter readback case on the disposable hosted daemon."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import resource
import shutil
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import launcher


ROOT = launcher.ROOT
NODE_ID = "backend/tests/test_xray_native_readback.py::test_loaded_client_readback_against_isolated_xray_26_2_6"
OWNER_LABEL = launcher.OWNER_LABEL
RUN_LABEL = launcher.RUN_LABEL
PROFILE_PATH_REL = Path("fwrouter-xray-native-profile/qualified-xray-profile.json")
MAX_SECONDS = 120
MAX_LOG_BYTES = 4 * 1024 * 1024
MAX_FAILURE_LOG_BYTES = 8 * 1024
MAX_FAILURE_TEXT_BYTES = 4 * 1024
MAX_CLEANUP_STDERR_BYTES = 2 * 1024


def _native_runner():
    path = ROOT / "tests/application_acceptance/native_runner.py"
    spec = importlib.util.spec_from_file_location("fwrouter_qualified_xray_native_runner", path)
    if spec is None or spec.loader is None:
        raise launcher.NotRun("trusted native diagnostic redactor is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_NATIVE_RUNNER = _native_runner()
_PEM_MARKER = re.compile(rb"-----\s*(?:BEGIN|END)\b", re.IGNORECASE)
_PEM_REDACTED = "[REDACTED PEM diagnostic]"


def _contains_pem_marker(value: bytes | str) -> bool:
    raw = value if isinstance(value, bytes) else str(value).encode("utf-8", "replace")
    return _PEM_MARKER.search(raw) is not None


def _redact_diagnostic(value: bytes | str, *, limit: int) -> str:
    if _contains_pem_marker(value):
        return _PEM_REDACTED[:max(0, limit)]
    return _NATIVE_RUNNER._redact_diagnostic(value, limit=limit)


def _read_failure_log(path: Path) -> dict[str, Any] | None:
    if not path.is_file() or path.is_symlink():
        return None
    try:
        size = path.stat().st_size
        if size >= MAX_LOG_BYTES:
            return {"omitted": "log_at_or_above_safe_redaction_cap"}
        with path.open("rb") as stream:
            raw = stream.read(MAX_LOG_BYTES + 1)
    except OSError:
        return None
    if len(raw) != size or len(raw) >= MAX_LOG_BYTES:
        return {"omitted": "log_size_changed_or_exceeded_safe_redaction_cap"}
    return {
        "tail": _redact_diagnostic(raw, limit=MAX_FAILURE_LOG_BYTES),
        "earlier_bytes_omitted": size > MAX_FAILURE_LOG_BYTES,
    }


def _junit_failure_projection(path: Path) -> dict[str, Any] | None:
    if not path.is_file() or path.is_symlink() or path.stat().st_size > launcher.MAX_RECEIPT_BYTES:
        return None
    try:
        cases = ET.parse(path).getroot().findall(".//testcase")
    except (ET.ParseError, OSError):
        return None
    if len(cases) != 1:
        return None
    case = cases[0]
    classname = case.get("classname", "")
    if classname == "tests.test_xray_native_readback":
        classname = "test_xray_native_readback"
    nodeid = f"backend/tests/{classname}.py::{case.get('name', '')}"
    if nodeid != NODE_ID:
        return None
    failure = case.find("failure")
    error = case.find("error")
    skipped = case.find("skipped")
    detail = failure if failure is not None else error
    status = "failed" if detail is not None else "skipped" if skipped is not None else "passed"
    projection: dict[str, Any] = {"node_id": NODE_ID, "status": status}
    if detail is not None:
        projection["failure_type"] = "failure" if failure is not None else "error"
        message = detail.get("message", "")
        traceback = detail.text or ""
        if _contains_pem_marker(f"{message}\n{traceback}"):
            projection["message"] = _PEM_REDACTED[:1024]
            projection["traceback"] = _PEM_REDACTED[:MAX_FAILURE_TEXT_BYTES]
        else:
            projection["message"] = _redact_diagnostic(message, limit=1024)
            projection["traceback"] = _redact_diagnostic(traceback, limit=MAX_FAILURE_TEXT_BYTES)
    return projection


def _owned_container_state(info: dict[str, Any]) -> dict[str, Any]:
    state = info.get("State") if isinstance(info.get("State"), dict) else {}
    host = info.get("HostConfig") if isinstance(info.get("HostConfig"), dict) else {}
    status = state.get("Status")
    result: dict[str, Any] = {
        "status": status if status in {"created", "running", "paused", "restarting", "removing", "exited", "dead"}
        else "unknown",
        "limits": {
            "nano_cpus": host.get("NanoCpus") if type(host.get("NanoCpus")) is int else None,
            "memory_bytes": host.get("Memory") if type(host.get("Memory")) is int else None,
            "pids_limit": host.get("PidsLimit") if type(host.get("PidsLimit")) is int else None,
        },
    }
    for key in ("Running", "OOMKilled"):
        if type(state.get(key)) is bool:
            result["oom_killed" if key == "OOMKilled" else "running"] = state[key]
    if type(state.get("ExitCode")) is int and -255 <= state["ExitCode"] <= 255:
        result["exit_code"] = state["ExitCode"]
    if type(info.get("RestartCount")) is int and 0 <= info["RestartCount"] <= 1_000_000:
        result["restart_count"] = info["RestartCount"]
    return result


def _container_is_owned(info: dict[str, Any], *, name: str, nonce: str) -> bool:
    labels = info.get("Config", {}).get("Labels", {})
    return (isinstance(labels, dict) and labels.get(OWNER_LABEL) == launcher.OWNER_VALUE
            and labels.get(RUN_LABEL) == nonce and info.get("Name", "").lstrip("/") == name)


def _image_is_owned(info: dict[str, Any], *, image_id: str, nonce: str) -> bool:
    config = info.get("Config") if isinstance(info, dict) else None
    labels = config.get("Labels") if isinstance(config, dict) else None
    return (isinstance(labels, dict) and info.get("Id") == image_id
            and labels.get(OWNER_LABEL) == launcher.OWNER_VALUE and labels.get(RUN_LABEL) == nonce)


def _record_cleanup_failure(receipt: dict[str, Any], operation: str, exc: Exception) -> None:
    details: dict[str, Any] = {"operation": operation, "error_class": type(exc).__name__}
    if isinstance(exc, subprocess.CalledProcessError):
        details["exit_code"] = exc.returncode
        if getattr(exc, "diagnostic_stderr_omitted", False):
            details["stderr_omitted"] = "bounded capture may have ended inside sensitive content"
        elif exc.stderr:
            details["stderr"] = _redact_diagnostic(exc.stderr, limit=MAX_CLEANUP_STDERR_BYTES)
    elif isinstance(exc, subprocess.TimeoutExpired):
        details["timeout_seconds"] = exc.timeout
        if getattr(exc, "diagnostic_stderr_omitted", False):
            details["stderr_omitted"] = "bounded capture may have ended inside sensitive content"
        elif exc.stderr:
            details["stderr"] = _redact_diagnostic(exc.stderr, limit=MAX_CLEANUP_STDERR_BYTES)
    elif isinstance(exc, launcher.NotRun):
        details["reason"] = _redact_diagnostic(str(exc), limit=MAX_CLEANUP_STDERR_BYTES)
    receipt.setdefault("diagnostics", {})["cleanup_failure"] = details


def _retain_failure_diagnostics_only(receipt: dict[str, Any]) -> None:
    if receipt.get("status") == "passed" and receipt.get("cleanup") == "owned_resources_removed":
        receipt.pop("diagnostics", None)


def _cleanup_command(argv: list[str], *, env: dict[str, str], timeout: int) -> subprocess.CompletedProcess[bytes]:
    started = time.monotonic()
    result = _NATIVE_RUNNER._bounded_child(
        argv, cwd=ROOT, env=env, timeout=timeout, stdout_limit=4096,
    )
    stderr_truncated = len(result.stderr) >= _NATIVE_RUNNER.DIAGNOSTIC_STREAM_LIMIT
    stderr = None if stderr_truncated or result.returncode == 124 else result.stderr
    if result.returncode:
        if result.returncode == 124 and time.monotonic() - started >= timeout:
            exc = subprocess.TimeoutExpired(argv, timeout, output=result.stdout, stderr=stderr)
        else:
            exc = subprocess.CalledProcessError(result.returncode, argv, output=result.stdout, stderr=stderr)
        if stderr_truncated or result.returncode == 124:
            exc.diagnostic_stderr_omitted = True
        raise exc
    return result


def _verify_empty_inventory(argv: list[str], *, env: dict[str, str], timeout: int,
                            resource_name: str) -> None:
    result = _cleanup_command(argv, env=env, timeout=timeout)
    if not isinstance(result.stdout, bytes) or result.stdout != b"":
        raise launcher.NotRun(f"qualified Xray {resource_name} cleanup inventory was not exactly empty")


def _digest_sources() -> str:
    tracked = launcher.git_files()
    value = hashlib.sha256()
    for relative in sorted(tracked):
        if not launcher._safe_source(relative):
            raise launcher.NotRun(f"source outside acceptance allowlist: {relative}")
        path = ROOT / relative
        value.update(relative.encode("utf-8") + b"\0")
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                value.update(block)
    return value.hexdigest()


def _docker(argv: list[str], *, env: dict[str, str], timeout: int = 20,
            check: bool = True) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(argv, cwd=ROOT, env=env, capture_output=True, timeout=timeout, check=False)
    if len(result.stdout) > 65536 or len(result.stderr) > 65536:
        raise launcher.NotRun("qualified Xray Docker command exceeded output bounds")
    if check and result.returncode:
        raise launcher.NotRun(
            "qualified Xray Docker command failed "
            f"(exit={result.returncode}, stdout={result.stdout[:4096]!r}, stderr={result.stderr[:4096]!r})"
        )
    return result


def _json_result(result: subprocess.CompletedProcess[bytes]) -> Any:
    try:
        return json.loads(result.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise launcher.NotRun("qualified Xray Docker response was invalid JSON") from exc


def _validate_junit(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink() or path.stat().st_size > launcher.MAX_RECEIPT_BYTES:
        raise launcher.NotRun("qualified Xray JUnit report is missing or oversized")
    try:
        cases = ET.parse(path).getroot().findall(".//testcase")
    except (ET.ParseError, OSError) as exc:
        raise launcher.NotRun("qualified Xray JUnit report is invalid") from exc
    if len(cases) != 1:
        return {"expected_ids": [NODE_ID], "node_status": {}, "passed": 0,
                "failed": 0, "skipped": 0, "contract_valid": False}
    case = cases[0]
    classname = case.get("classname", "")
    if classname == "tests.test_xray_native_readback":
        classname = "test_xray_native_readback"
    nodeid = f"backend/tests/{classname}.py::{case.get('name', '')}"
    status = "failed" if case.find("failure") is not None or case.find("error") is not None else (
        "skipped" if case.find("skipped") is not None else "passed")
    exact = nodeid == NODE_ID and status == "passed"
    return {"expected_ids": [NODE_ID], "node_status": {nodeid: status},
            "passed": int(status == "passed"), "failed": int(status == "failed"),
            "skipped": int(status == "skipped"), "contract_valid": exact}


def run(env: dict[str, str], facts: dict[str, Any]) -> dict[str, Any]:
    active_phase = "host_qualification"
    reasons = launcher.qualify_host(env, facts)
    if reasons:
        raise launcher.NotRun("host qualification failed: " + "; ".join(reasons))
    active_phase = "plan_validation"
    plan_digest = env.get("FWROUTER_ACCEPTANCE_PLAN_DIGEST", "")
    launcher.validate_plan_digest(plan_digest)
    runner_temp = Path(env["RUNNER_TEMP"]).resolve(strict=True)
    active_phase = "binary_validation"
    xray_input = launcher.validate_binary(env.get("FWROUTER_ACCEPTANCE_XRAY_BINARY", ""),
        env.get("FWROUTER_ACCEPTANCE_XRAY_SHA256", ""), runner_temp)
    if (xray_input["sha256"] != "3f650abf1fc4a4fbf5abe7fc9990a2658020907cd984214e9c075b4b00989fea"
            or env.get("FWROUTER_ACCEPTANCE_XRAY_VERSION") != "26.2.6"):
        raise launcher.NotRun("qualified Xray binary does not match the pinned v26.2.6 input")
    docker = shutil.which("docker", path=env.get("PATH", "/usr/local/bin:/usr/bin:/bin"))
    if not docker or env.get("DOCKER_HOST"):
        raise launcher.NotRun("qualified Xray requires the local hosted Docker CLI/daemon")
    nonce = uuid.uuid4().hex
    revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True,
                              capture_output=True, check=True, timeout=10).stdout.strip()
    source_digest = _digest_sources()
    # Pytest's isolation bootstrap only grants writes in its private /tmp root;
    # keep artifacts on RUNNER_TEMP but never make that broader tree writable.
    temp = Path("/tmp") / f"fwrouter-xray-native-{nonce}"
    if temp.exists() or temp.is_symlink():
        raise launcher.NotRun("qualified Xray private test root unexpectedly already exists")
    temp.mkdir(mode=0o700)
    artifacts = runner_temp / f"fwrouter-xray-native-{nonce}-artifacts"
    artifacts.mkdir(mode=0o700)
    build_context = temp / "image"
    build_context.mkdir(mode=0o700)
    binary_path = Path(xray_input["source"])
    shutil.copyfile(binary_path, build_context / "xray")
    (build_context / "xray").chmod(0o755)
    shutil.copyfile(ROOT / "tests/acceptance/XrayTest.Dockerfile", build_context / "Dockerfile")
    name = f"native-xray-readback-test-{nonce[:12]}"
    image_tag = f"fwrouter-xray-native:{nonce}"
    profile_dir = runner_temp / "fwrouter-xray-native-profile"
    if profile_dir.exists() or profile_dir.is_symlink():
        shutil.rmtree(temp, ignore_errors=True)
        shutil.rmtree(artifacts, ignore_errors=True)
        raise launcher.NotRun("qualified Xray profile directory unexpectedly already exists")
    profile_dir.mkdir(mode=0o700)
    profile_path = profile_dir / "qualified-xray-profile.json"
    profile = {
        "schema": "fwrouter-qualified-docker-xray-profile/v1",
        "profile": "qualified-docker-xray", "source_revision": revision,
        "source_manifest_sha256": source_digest, "plan_digest": plan_digest,
        "suite_nonce": nonce, "image_id": "", "container_name": name,
        "owned_root": str(temp), "xray_binary": str(binary_path),
        "xray_binary_sha256": xray_input["sha256"], "xray_version": "26.2.6",
    }
    report_path = artifacts / "qualified-xray-docker-receipt.json"
    junit_path = artifacts / "qualified-xray-docker.xml"
    junit_inside = temp / "qualified-xray-docker.xml"
    log_path = artifacts / "qualified-xray-docker.log"
    receipt: dict[str, Any] = {
        "schema": "fwrouter-qualified-docker-xray-receipt/v1", "scope": "qualified-docker-xray",
        "status": "NOTRUN", "source_revision": revision, "plan_digest": plan_digest,
        "suite_nonce": nonce, "image_id": None, "xray_binary_sha256": xray_input["sha256"],
        "expected_ids": [NODE_ID], "tests": None, "cleanup": "not_started",
        "artifacts": {"report": str(report_path), "junit": str(junit_path), "log": str(log_path)},
    }
    docker_env = {key: value for key, value in env.items()
                  if key in {"PATH", "HOME", "DOCKER_CONFIG", "DOCKER_HOST", "DOCKER_CONTEXT"}}
    docker_env.pop("DOCKER_HOST", None)
    docker_env.pop("DOCKER_CONTEXT", None)
    docker_env["DOCKER_BUILDKIT"] = "1"
    image_id: str | None = None
    container_clean = False
    owned_container_info: dict[str, Any] | None = None
    try:
        active_phase = "docker_server_version"
        version = _docker([docker, "version", "--format", "{{.Server.Os}}|{{.Server.Arch}}"], env=docker_env)
        observed_platform = version.stdout.decode("utf-8", "replace").strip().split("|")
        if (len(observed_platform) != 2 or observed_platform[0] != "linux"
                or observed_platform[1] not in {"amd64", "x86_64"}):
            raise launcher.NotRun("qualified Xray Docker daemon is not a local Linux amd64 server")
        active_phase = "image_build"
        subprocess.run([docker, "build", "--pull=false", "--no-cache", "--network=none",
                        "--build-arg", f"ACCEPTANCE_RUN_ID={nonce}", "--build-arg",
                        f"SOURCE_REVISION={revision}", "--tag", image_tag, str(build_context)],
                       cwd=ROOT, env=docker_env, check=True, timeout=180,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        image_rows = _json_result(_docker([docker, "image", "inspect", image_tag], env=docker_env))
        if not isinstance(image_rows, list) or len(image_rows) != 1:
            raise launcher.NotRun("qualified Xray image inspect returned unexpected result")
        image = image_rows[0]
        labels = image.get("Config", {}).get("Labels", {})
        image_id = image.get("Id")
        if (not isinstance(image_id, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id)
                or labels.get(OWNER_LABEL) != launcher.OWNER_VALUE or labels.get(RUN_LABEL) != nonce
                or labels.get("org.opencontainers.image.revision") != revision
                or image.get("Architecture") != "amd64" or image.get("Os") != "linux"):
            raise launcher.NotRun("qualified Xray image identity/platform is not pinned to this run")
        profile["image_id"] = image_id
        profile_path.write_text(json.dumps(profile, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        profile_path.chmod(0o444)
        shutil.copyfile(profile_path, artifacts / "profile.json")
        (artifacts / "profile.json").chmod(0o444)
        receipt["profile_sha256"] = hashlib.sha256(profile_path.read_bytes()).hexdigest()
        receipt["image_id"] = image_id
        test_env = {
            "PATH": env.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": str(temp / "home"), "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TZ": "UTC",
            "GITHUB_ACTIONS": env.get("GITHUB_ACTIONS", ""),
            "RUNNER_ENVIRONMENT": env.get("RUNNER_ENVIRONMENT", ""),
            "GITHUB_WORKSPACE": str(ROOT), "RUNNER_TEMP": str(runner_temp),
            "FWROUTER_QUALIFIED_DOCKER_XRAY_PROFILE": str(profile_path),
            "FWROUTER_PYTEST_COORDINATOR_ROOT": str(temp),
            "FWROUTER_ACCEPTANCE_XRAY_BINARY": str(binary_path),
            "FWROUTER_ACCEPTANCE_XRAY_SHA256": xray_input["sha256"],
            "FWROUTER_ACCEPTANCE_XRAY_VERSION": "26.2.6",
            "PYTHONPATH": str(ROOT / "backend"), "PYTHONDONTWRITEBYTECODE": "1",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        }
        (temp / "home").mkdir(mode=0o700)
        marker = temp / ".fwrouter-gate-test-root-owned"
        marker.write_text("FWROUTER_GATE_TEST_ROOT_V1\n", encoding="utf-8")
        marker.chmod(0o600)
        command = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", "--tb=short",
                   f"--basetemp={temp / 'pytest-tmp'}", f"--junitxml={junit_inside}",
                   "tests/test_xray_native_readback.py::test_loaded_client_readback_against_isolated_xray_26_2_6"]
        active_phase = "pytest"
        started = time.monotonic()
        with log_path.open("wb") as output:
            process = subprocess.Popen(command, cwd=ROOT / "backend", env=test_env,
                stdout=output, stderr=subprocess.STDOUT, start_new_session=True,
                preexec_fn=lambda: resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_LOG_BYTES, MAX_LOG_BYTES)))
            try:
                code = process.wait(timeout=MAX_SECONDS)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, 9)
                process.wait(timeout=5)
                code = 124
        receipt["elapsed_seconds"] = round(time.monotonic() - started, 3)
        receipt["pytest_exit_code"] = code
        receipt["log_bytes"] = log_path.stat().st_size if log_path.exists() else 0
        if junit_inside.is_file():
            shutil.copyfile(junit_inside, junit_path)
        if code != 0:
            receipt.setdefault("diagnostics", {})["pytest"] = {
                "exit_code": code,
                "junit_failure": _junit_failure_projection(junit_path),
                "log_tail": _read_failure_log(log_path),
            }
        receipt["tests"] = _validate_junit(junit_path) if junit_path.exists() else None
        if receipt["log_bytes"] > MAX_LOG_BYTES:
            raise launcher.NotRun("qualified Xray test log exceeded its output cap")
        passed = (code == 0 and isinstance(receipt["tests"], dict)
                  and receipt["tests"].get("contract_valid") is True)
        receipt["status"] = "passed" if passed else "failed"
        if not passed:
            receipt.setdefault("diagnostics", {}).setdefault("pytest", {
                "exit_code": code,
                "junit_failure": _junit_failure_projection(junit_path),
                "log_tail": _read_failure_log(log_path),
            })
        if not passed:
            raise launcher.NotRun("exact qualified Xray test did not pass without skip")
    except (launcher.NotRun, OSError, subprocess.SubprocessError, ValueError, KeyError) as exc:
        receipt["status"] = "failed" if image_id else "NOTRUN"
        safe_reason = _redact_diagnostic(str(exc), limit=MAX_FAILURE_TEXT_BYTES)
        receipt["reason"] = safe_reason
        receipt.setdefault("diagnostics", {})["failure"] = {
            "phase": active_phase, "error_class": type(exc).__name__, "reason": safe_reason,
        }
    finally:
        cleanup_operation = "list_owned_containers"
        if image_id:
            try:
                containers = _docker([docker, "ps", "--all", "--quiet", "--no-trunc",
                    "--filter", f"label={OWNER_LABEL}={launcher.OWNER_VALUE}",
                    "--filter", f"label={RUN_LABEL}={nonce}"], env=docker_env).stdout.decode().split()
                if not containers:
                    receipt.setdefault("diagnostics", {})["container_state"] = {"found": False}
                for container_id in containers:
                    cleanup_operation = "inspect_owned_container"
                    info_rows = _json_result(_docker([docker, "inspect", container_id], env=docker_env))
                    if not isinstance(info_rows, list) or len(info_rows) != 1:
                        raise launcher.NotRun("qualified Xray cleanup container inspect failed")
                    info = info_rows[0]
                    if not _container_is_owned(info, name=name, nonce=nonce):
                        raise launcher.NotRun("qualified Xray cleanup refused a foreign test container")
                    owned_container_info = info
                    if receipt.get("status") != "passed":
                        receipt.setdefault("diagnostics", {})["container_state"] = {
                            "found": True, **_owned_container_state(info),
                        }
                    cleanup_operation = "remove_owned_container"
                    _cleanup_command([docker, "rm", "--force", container_id], env=docker_env, timeout=30)
                cleanup_operation = "inspect_owned_image"
                image_rows = _json_result(_docker([docker, "image", "inspect", image_id], env=docker_env))
                if (not isinstance(image_rows, list) or len(image_rows) != 1
                        or not _image_is_owned(image_rows[0], image_id=image_id, nonce=nonce)):
                    raise launcher.NotRun("qualified Xray cleanup refused an unowned image")
                cleanup_operation = "remove_owned_image"
                _cleanup_command([docker, "image", "rm", image_id], env=docker_env, timeout=30)
                cleanup_operation = "verify_owned_image_absent"
                _verify_empty_inventory([docker, "image", "ls", "--all", "--quiet", "--no-trunc",
                    "--filter", f"label={OWNER_LABEL}={launcher.OWNER_VALUE}",
                    "--filter", f"label={RUN_LABEL}={nonce}"], env=docker_env, timeout=10,
                    resource_name="owned image")
                cleanup_operation = "verify_owned_containers_absent"
                _verify_empty_inventory([docker, "ps", "--all", "--quiet", "--no-trunc",
                    "--filter", f"label={OWNER_LABEL}={launcher.OWNER_VALUE}",
                    "--filter", f"label={RUN_LABEL}={nonce}"], env=docker_env, timeout=20,
                    resource_name="owned container")
                container_clean = True
            except (launcher.NotRun, OSError, subprocess.SubprocessError) as exc:
                _record_cleanup_failure(receipt, cleanup_operation, exc)
                if owned_container_info is not None:
                    receipt.setdefault("diagnostics", {})["container_state"] = {
                        "found": True, **_owned_container_state(owned_container_info),
                    }
                container_clean = False
        receipt["cleanup"] = "owned_resources_removed" if container_clean else "cleanup_unconfirmed"
        if receipt["status"] == "passed" and not container_clean:
            receipt["status"] = "failed"
        _retain_failure_diagnostics_only(receipt)
        report_path.write_text(json.dumps(receipt, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        profile_path.chmod(0o600)
        shutil.rmtree(profile_dir, ignore_errors=True)
        shutil.rmtree(temp, ignore_errors=True)
        shutil.rmtree(build_context, ignore_errors=True)
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="execute only in qualified GitHub-hosted job")
    args = parser.parse_args()
    if not args.run:
        print(json.dumps({"status": "NOTRUN", "reason": "explicit --run is required"}))
        return 2
    try:
        result = run(dict(os.environ), launcher.host_facts())
    except (launcher.NotRun, KeyError, OSError, subprocess.SubprocessError) as exc:
        print(json.dumps({"schema": "fwrouter-qualified-docker-xray-receipt/v1", "scope": "qualified-docker-xray",
                          "status": "NOTRUN", "reason": str(exc)[:4096]}, sort_keys=True), file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("status") == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
