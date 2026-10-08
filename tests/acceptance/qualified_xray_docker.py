#!/usr/bin/env python3
"""Run the exact Xray Docker-adapter readback case on the disposable hosted daemon."""
from __future__ import annotations

import argparse
import hashlib
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
    nodeid = f"backend/tests/{case.get('classname', '')}.py::{case.get('name', '')}"
    status = "failed" if case.find("failure") is not None or case.find("error") is not None else (
        "skipped" if case.find("skipped") is not None else "passed")
    exact = nodeid == NODE_ID and status == "passed"
    return {"expected_ids": [NODE_ID], "node_status": {nodeid: status},
            "passed": int(status == "passed"), "failed": int(status == "failed"),
            "skipped": int(status == "skipped"), "contract_valid": exact}


def run(env: dict[str, str], facts: dict[str, Any]) -> dict[str, Any]:
    reasons = launcher.qualify_host(env, facts)
    if reasons:
        raise launcher.NotRun("host qualification failed: " + "; ".join(reasons))
    plan_digest = env.get("FWROUTER_ACCEPTANCE_PLAN_DIGEST", "")
    launcher.validate_plan_digest(plan_digest)
    runner_temp = Path(env["RUNNER_TEMP"]).resolve(strict=True)
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
    try:
        version = _docker([docker, "version", "--format", "{{.Server.Os}}|{{.Server.Arch}}"], env=docker_env)
        if version.stdout.decode("utf-8", "replace").strip() != "linux|x86_64":
            raise launcher.NotRun("qualified Xray Docker daemon is not a local Linux amd64 server")
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
        receipt["tests"] = _validate_junit(junit_path) if junit_path.exists() else None
        if receipt["log_bytes"] > MAX_LOG_BYTES:
            raise launcher.NotRun("qualified Xray test log exceeded its output cap")
        passed = (code == 0 and isinstance(receipt["tests"], dict)
                  and receipt["tests"].get("contract_valid") is True)
        receipt["status"] = "passed" if passed else "failed"
        if not passed:
            raise launcher.NotRun("exact qualified Xray test did not pass without skip")
    except (launcher.NotRun, OSError, subprocess.SubprocessError, ValueError, KeyError) as exc:
        receipt["status"] = "failed" if image_id else "NOTRUN"
        receipt["reason"] = str(exc)[:4096]
    finally:
        if image_id:
            try:
                containers = _docker([docker, "ps", "--all", "--quiet", "--no-trunc",
                    "--filter", f"label={OWNER_LABEL}={launcher.OWNER_VALUE}",
                    "--filter", f"label={RUN_LABEL}={nonce}"], env=docker_env).stdout.decode().split()
                for container_id in containers:
                    info_rows = _json_result(_docker([docker, "inspect", container_id], env=docker_env))
                    if not isinstance(info_rows, list) or len(info_rows) != 1:
                        raise launcher.NotRun("qualified Xray cleanup container inspect failed")
                    info = info_rows[0]
                    labels = info.get("Config", {}).get("Labels", {})
                    if (labels.get(OWNER_LABEL) != launcher.OWNER_VALUE or labels.get(RUN_LABEL) != nonce
                            or info.get("Name", "").lstrip("/") != name):
                        raise launcher.NotRun("qualified Xray cleanup refused a foreign test container")
                    subprocess.run([docker, "rm", "--force", container_id], cwd=ROOT, env=docker_env,
                                   check=True, timeout=30, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                image_rows = _json_result(_docker([docker, "image", "inspect", image_id], env=docker_env))
                if (not isinstance(image_rows, list) or len(image_rows) != 1
                        or image_rows[0].get("Config", {}).get("Labels", {}).get(RUN_LABEL) != nonce):
                    raise launcher.NotRun("qualified Xray cleanup refused an unowned image")
                subprocess.run([docker, "image", "rm", image_id], cwd=ROOT, env=docker_env,
                               check=True, timeout=30, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                absent = subprocess.run([docker, "image", "inspect", image_id], cwd=ROOT, env=docker_env,
                                        capture_output=True, timeout=10, check=False)
                if absent.returncode == 0:
                    raise launcher.NotRun("qualified Xray image cleanup was not confirmed")
                container_clean = True
            except (launcher.NotRun, OSError, subprocess.SubprocessError):
                container_clean = False
        receipt["cleanup"] = "owned_resources_removed" if container_clean else "cleanup_unconfirmed"
        if receipt["status"] == "passed" and not container_clean:
            receipt["status"] = "failed"
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
