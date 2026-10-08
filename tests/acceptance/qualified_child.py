#!/usr/bin/env python3
"""Run the exact child-process/native acceptance cohorts in a qualified container."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
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
COMPOSE = ROOT / "tests/acceptance/qualified-child.compose.yaml"
SERVICE = "qualified_child"
MOUNT_TARGET = "/run/fwrouter-acceptance/qualified-child-profile.json"
EXPECTED_TOTAL = 26


def _small(argv: list[str], *, env: dict[str, str], timeout: int = 20) -> str:
    result = subprocess.run(argv, cwd=ROOT, env=env, capture_output=True, timeout=timeout, check=False)
    if result.returncode or len(result.stdout) > 64 * 1024 or len(result.stderr) > 64 * 1024:
        raise launcher.NotRun("qualified-child bounded Docker operation failed")
    return result.stdout.decode("utf-8", "replace")


def _json(argv: list[str], *, env: dict[str, str], timeout: int = 20) -> Any:
    try:
        return json.loads(_small(argv, env=env, timeout=timeout))
    except (ValueError, json.JSONDecodeError) as exc:
        raise launcher.NotRun("qualified-child Docker output was not valid JSON") from exc


def _validate_config(config: dict[str, Any], *, run_id: str, profile: Path) -> None:
    services, networks = config.get("services"), config.get("networks")
    if not isinstance(services, dict) or set(services) != {SERVICE}:
        raise launcher.NotRun("qualified-child Compose must define exactly one service")
    if not isinstance(networks, dict) or set(networks) != {"isolated"} or networks["isolated"].get("internal") is not True:
        raise launcher.NotRun("qualified-child Compose requires one internal network")
    network_labels = networks["isolated"].get("labels", {})
    if (network_labels.get(launcher.OWNER_LABEL) != launcher.OWNER_VALUE
            or network_labels.get(launcher.RUN_LABEL) != run_id or networks["isolated"].get("external") is True):
        raise launcher.NotRun("qualified-child internal network ownership is not proven")
    service = services[SERVICE]
    if {"env_file", "secrets", "configs", "volumes_from", "extra_hosts", "sysctls",
            "runtime", "devices", "pid", "ipc", "privileged"} & service.keys():
        raise launcher.NotRun("qualified-child Compose has unreviewed host/runtime integration")
    if (service.get("user") != "10001:10001" or service.get("read_only") is not True
            or service.get("cap_drop") != ["ALL"]
            or service.get("security_opt") != ["no-new-privileges:true"]
            or service.get("privileged") or service.get("ports") or service.get("devices")
            or service.get("cap_add") or service.get("network_mode") or service.get("pid")
            or service.get("ipc") or service.get("restart") != "no"
            or service.get("command") != ["-c", "import signal; signal.pause()"]):
        raise launcher.NotRun("qualified-child service confinement differs from policy")
    labels = service.get("labels", {})
    attached = service.get("networks")
    attached_names = set(attached if isinstance(attached, list) else attached) if isinstance(attached, (list, dict)) else set()
    if attached_names != {"isolated"}:
        raise launcher.NotRun("qualified-child may attach only to its isolated internal network")
    if (labels.get(launcher.OWNER_LABEL) != launcher.OWNER_VALUE
            or labels.get(launcher.RUN_LABEL) != run_id
            or network_labels.get(launcher.OWNER_LABEL) != launcher.OWNER_VALUE
            or network_labels.get(launcher.RUN_LABEL) != run_id):
        raise launcher.NotRun("qualified-child ownership labels do not match this run")
    volumes = service.get("volumes", [])
    if (len(volumes) != 1 or volumes[0].get("target") != MOUNT_TARGET
            or volumes[0].get("read_only") is not True
            or Path(volumes[0].get("source", "")).resolve() != profile.resolve()):
        raise launcher.NotRun("qualified-child must have only its read-only profile bind")
    environment = service.get("environment", {})
    expected_environment = {
        "FWROUTER_ENVIRONMENT": "test", "FWROUTER_STATE_DIR": "/tmp/fwrouter-qualified-child-state",
        "FWROUTER_STARTUP_TASKS_ENABLED": "0", "FWROUTER_QUALIFIED_CHILD_PROFILE": MOUNT_TARGET,
        "FWROUTER_TEST_MIHOMO_BINARY": "/opt/fwrouter-test/bin/mihomo",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTHONDONTWRITEBYTECODE": "1",
        "HOME": "/tmp/fwrouter-home",
    }
    if environment != expected_environment:
        raise launcher.NotRun("qualified-child has missing or unreviewed process environment")
    if service.get("pids_limit") != 256 or float(service.get("cpus", 0)) > 2:
        raise launcher.NotRun("qualified-child CPU/process bounds are missing or excessive")
    def bytes_limit(value: object) -> int:
        raw = str(value).strip().lower()
        match = re.fullmatch(r"(\d+(?:\.\d+)?)([kmgt]?i?b?)?", raw)
        if not match:
            return 0
        unit = match.group(2) or ""
        power = {"": 0, "b": 0, "k": 1, "kb": 1, "kib": 1,
                 "m": 2, "mb": 2, "mib": 2, "g": 3, "gb": 3, "gib": 3,
                 "t": 4, "tb": 4, "tib": 4}.get(unit)
        return 0 if power is None else int(float(match.group(1)) * (1024 ** power))
    memory = bytes_limit(service.get("mem_limit"))
    memory_swap = bytes_limit(service.get("memswap_limit"))
    if not 0 < memory <= 2 * 1024**3 or memory_swap != memory:
        raise launcher.NotRun("qualified-child memory/swap bounds differ from 2 GiB policy")
    tmpfs = service.get("tmpfs", [])
    if len(tmpfs) != 1 or not tmpfs[0].startswith("/tmp:rw,") or "size=512m" not in tmpfs[0]:
        raise launcher.NotRun("qualified-child requires one bounded /tmp tmpfs")
    if "noexec" in tmpfs[0] or not all(flag in tmpfs[0] for flag in ("nosuid", "nodev")):
        raise launcher.NotRun("qualified-child /tmp must allow isolated fake executables while remaining nosuid/nodev")
    if json.dumps(config).find("/var/lib/fwrouter-v2") >= 0 or json.dumps(config).find("/opt/fwrouter-api/.env") >= 0:
        raise launcher.NotRun("qualified-child config references production state/secrets")


def _validate_runtime(value: dict[str, Any], *, project: str, run_id: str,
                      profile: Path, image_id: str) -> None:
    config, host = value.get("Config", {}), value.get("HostConfig", {})
    labels = config.get("Labels", {})
    if (labels.get(launcher.OWNER_LABEL) != launcher.OWNER_VALUE
            or labels.get(launcher.RUN_LABEL) != run_id
            or labels.get("com.docker.compose.project") != project):
        raise launcher.NotRun("qualified-child container ownership was not proven")
    if value.get("State", {}).get("Status") != "created" or value.get("Image") != image_id:
        raise launcher.NotRun("qualified-child must be stopped and pinned to the inspected image")
    if config.get("User") != "10001:10001" or host.get("ReadonlyRootfs") is not True:
        raise launcher.NotRun("qualified-child must run non-root with read-only root")
    if (config.get("Entrypoint") != ["python"]
            or config.get("Cmd") != ["-c", "import signal; signal.pause()"]):
        raise launcher.NotRun("qualified-child was not inspected at the reviewed idle entrypoint")
    if host.get("Privileged") or host.get("NetworkMode") != f"{project}_isolated":
        raise launcher.NotRun("qualified-child uses forbidden privilege/network mode")
    if host.get("CapAdd") or host.get("Devices") or host.get("PidMode") or host.get("IpcMode") == "host":
        raise launcher.NotRun("qualified-child has forbidden capability/device/host namespace")
    if host.get("CapDrop") != ["ALL"] or "no-new-privileges:true" not in (host.get("SecurityOpt") or []):
        raise launcher.NotRun("qualified-child capability isolation is missing")
    if host.get("PortBindings"):
        raise launcher.NotRun("qualified-child has unexpected published host ports")
    mounts = value.get("Mounts", [])
    binds = [mount for mount in mounts if mount.get("Type") == "bind"]
    # Docker Engine versions vary on whether an explicit tmpfs appears in
    # `.Mounts`; HostConfig.Tmpfs below remains the authoritative exact check.
    if (len(mounts) not in (1, 2) or len(binds) != 1 or binds[0].get("Destination") != MOUNT_TARGET
            or binds[0].get("RW") is not False or Path(binds[0].get("Source", "")).resolve() != profile.resolve()):
        raise launcher.NotRun("qualified-child runtime mounts differ from the single read-only profile bind")
    if len(mounts) == 2:
        tmpfs_mounts = [mount for mount in mounts if mount.get("Type") == "tmpfs"]
        if len(tmpfs_mounts) != 1 or tmpfs_mounts[0].get("Destination") != "/tmp":
            raise launcher.NotRun("qualified-child runtime has an unexpected second mount")
    tmpfs = host.get("Tmpfs", {})
    if not isinstance(tmpfs, dict) or set(tmpfs) != {"/tmp"}:
        raise launcher.NotRun("qualified-child runtime /tmp tmpfs is missing or unbounded")
    options = str(tmpfs["/tmp"]).lower()
    if "noexec" in options or not all(flag in options for flag in ("nosuid", "nodev", "size=512m")):
        raise launcher.NotRun("qualified-child runtime /tmp must allow isolated fake executables with nosuid/nodev and a size bound")
    if host.get("PidsLimit") != 256 or not 0 < host.get("Memory", 0) <= 2 * 1024**3:
        raise launcher.NotRun("qualified-child runtime resource bounds are missing")
    if not 0 < host.get("NanoCpus", 0) <= 2_000_000_000:
        raise launcher.NotRun("qualified-child runtime CPU bound is missing")


def _preflight_code() -> str:
    return r'''import hashlib,json,os,pathlib,sys
p=pathlib.Path('/run/fwrouter-acceptance/qualified-child-profile.json')
raw=p.read_bytes(); d=json.loads(raw)
assert d['schema']=='fwrouter-qualified-child-profile/v1' and d['profile']=='qualified-child-process'
assert d['suite_nonce'] and d['source_revision'] and d['plan_digest']
assert os.getuid()==10001 and os.getgid()==10001 and pathlib.Path('/.dockerenv').is_file()
status=dict(line.split(':',1) for line in pathlib.Path('/proc/self/status').read_text().splitlines() if ':' in line)
assert status['CapEff'].strip()=='0000000000000000' and status['NoNewPrivs'].strip()=='1'
def ro(target):
 for line in pathlib.Path('/proc/self/mountinfo').read_text().splitlines():
  f=line.split()
  if len(f)>5 and f[4].replace('\\040',' ')==target: return 'ro' in f[5].split(',')
 return False
assert ro('/') and ro(str(p)) and not pathlib.Path('/var/run/docker.sock').exists()
tmp_options=[]
for line in pathlib.Path('/proc/self/mountinfo').read_text().splitlines():
 f=line.split()
 if len(f)>5 and f[4]=='/tmp': tmp_options=f[5].split(','); break
assert tmp_options and 'noexec' not in tmp_options and 'nosuid' in tmp_options and 'nodev' in tmp_options
m=d['mihomo']; b=pathlib.Path(m['path']); assert b.is_file() and not b.is_symlink()
h=hashlib.sha256(b.read_bytes()).hexdigest(); assert h==m['sha256']
v=__import__('subprocess').run([str(b),'-v'],capture_output=True,text=True,timeout=8,env={'PATH':'/usr/bin:/bin','HOME':'/tmp','TMPDIR':'/tmp','LANG':'C.UTF-8'})
assert v.returncode==0 and v.stdout.startswith(m['version']+' ')
print(json.dumps({'status':'passed','nonce':d['suite_nonce'],'profile_sha256':hashlib.sha256(raw).hexdigest()}))
'''


def _read_junit(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink() or path.stat().st_size > launcher.MAX_RECEIPT_BYTES:
        raise launcher.NotRun("qualified-child JUnit report is missing or oversized")
    try:
        cases = ET.parse(path).getroot().findall(".//testcase")
    except (ET.ParseError, OSError) as exc:
        raise launcher.NotRun("qualified-child JUnit report is invalid") from exc
    statuses: dict[str, str] = {}
    for case in cases:
        classname, name = case.get("classname", ""), case.get("name", "")
        if not classname.startswith("test_") or not name:
            raise launcher.NotRun("qualified-child JUnit has an unrecognized test identity")
        nodeid = f"backend/tests/{classname}.py::{name}"
        status = "failed" if case.find("failure") is not None or case.find("error") is not None else (
            "skipped" if case.find("skipped") is not None else "passed")
        if nodeid in statuses:
            raise launcher.NotRun("qualified-child JUnit repeats a node ID")
        statuses[nodeid] = status
    errors = []
    scenarios_path = ROOT / "tests/acceptance/qualified-child-scenarios.json"
    try:
        scenarios = json.loads(scenarios_path.read_text(encoding="utf-8"))
        expected_ids = scenarios["nodeids"]
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise launcher.NotRun("qualified-child exact scenario registry is unavailable") from exc
    expected = set(expected_ids) if isinstance(expected_ids, list) else set()
    protocol_ids = [node for node in expected if node.startswith("backend/tests/test_protocol_native_validation.py::")]
    if (scenarios.get("schema") != "fwrouter-qualified-child-scenarios/v1"
            or scenarios.get("protocol_case_count") != 20
            or len(expected) != EXPECTED_TOTAL or len(protocol_ids) != 20
            or len(statuses) != EXPECTED_TOTAL or set(statuses) != expected):
        errors.append("observed node IDs differ from the checked-in exact 20 protocol plus six cohort contract")
    counts = {name: sum(status == name for status in statuses.values())
              for name in ("passed", "failed", "skipped")}
    if counts["failed"] or counts["skipped"]:
        errors.append("one or more qualified-child tests failed or skipped")
    return {"tests": len(statuses), **counts, "expected_ids": sorted(expected),
            "node_status": dict(sorted(statuses.items())),
            "contract_valid": not errors, "contract_errors": errors}


def run(env: dict[str, str], facts: dict[str, Any]) -> dict[str, Any]:
    reasons = launcher.qualify_host(env, facts)
    if reasons:
        raise launcher.NotRun("host qualification failed: " + "; ".join(reasons))
    plan_digest = env.get("FWROUTER_ACCEPTANCE_PLAN_DIGEST", "")
    if not re.fullmatch(r"[0-9a-f]{64}", plan_digest):
        raise launcher.NotRun("affected plan digest must be a lowercase SHA-256")
    temp = Path(env["RUNNER_TEMP"]).resolve(strict=True)
    binaries = launcher.validate_manifest_inputs(env, temp)
    if env.get("FWROUTER_ACCEPTANCE_MIHOMO_VERSION") != "Mihomo Meta v1.19.31":
        raise launcher.NotRun("qualified-child requires pinned Mihomo Meta v1.19.31")
    run_id = uuid.uuid4().hex
    project = f"fwrouter-qcp-{run_id}"
    docker_env = {key: value for key, value in env.items() if key == "PATH"}
    docker = shutil.which("docker", path=docker_env.get("PATH"))
    if not docker:
        raise launcher.NotRun("Docker CLI is unavailable on the qualified hosted runner")
    root = temp / project
    root.mkdir(mode=0o700)
    artifact = temp / f"{project}-artifacts"
    artifact.mkdir(mode=0o700)
    profile_dir = temp / f"{project}-profile"
    profile_dir.mkdir(mode=0o755)
    profile_path = profile_dir / "qualified-child-profile.json"
    report_path = artifact / "qualified-child-receipt.json"
    junit_host = artifact / "qualified-child.xml"
    log_host = artifact / "qualified-child.log"
    revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True,
                              capture_output=True, check=True, timeout=10).stdout.strip()
    receipt: dict[str, Any] = {
        "schema": "fwrouter-qualified-child-receipt/v1", "scope": "qualified-child-process",
        "status": "NOTRUN", "source_revision": revision, "plan_digest": plan_digest,
        "suite_nonce": run_id, "tests": None,
        "artifacts": {"report": str(report_path), "junit": str(junit_host), "log": str(log_host)},
        "cleanup": "not_started",
    }
    context = root / "context"
    docker_env.update({
        "FWROUTER_ACCEPTANCE_CONTEXT": str(context),
        "FWROUTER_ACCEPTANCE_BASE_IMAGE": env["FWROUTER_ACCEPTANCE_BASE_IMAGE"],
        "FWROUTER_ACCEPTANCE_RUN_ID": run_id,
        "FWROUTER_ACCEPTANCE_PROFILE_FILE": str(profile_path),
        "FWROUTER_ACCEPTANCE_SOURCE_REVISION": revision,
        "COMPOSE_DISABLE_ENV_FILE": "1",
        "HOME": str(root / "home"), "DOCKER_CONFIG": str(root / "docker-config"),
    })
    Path(docker_env["HOME"]).mkdir(mode=0o700)
    Path(docker_env["DOCKER_CONFIG"]).mkdir(mode=0o700)
    container_id = network_id = image_id = None
    try:
        tracked = launcher.git_files()
        copied = launcher.export_build_context(ROOT, context, tracked=tracked,
            binaries={key: Path(binaries[key]["source"]) for key in ("xray", "mihomo")},
            browser_bundle=Path(binaries["chromium"]["source"]), source_revision=revision)
        if copied["chromium_executable_sha256"] != env.get("FWROUTER_ACCEPTANCE_CHROMIUM_BINARY_SHA256"):
            raise launcher.NotRun("normalized Chromium executable SHA-256 differs from the provisioned pin")
        profile = {
            "schema": "fwrouter-qualified-child-profile/v1", "profile": "qualified-child-process",
            "source_revision": revision, "plan_digest": plan_digest, "suite_nonce": run_id,
            "mihomo": {"path": "/opt/fwrouter-test/bin/mihomo", "sha256": binaries["mihomo"]["sha256"],
                       "version": env["FWROUTER_ACCEPTANCE_MIHOMO_VERSION"]},
            "source_manifest_sha256": copied["source_manifest_sha256"],
        }
        payload = json.dumps(profile, sort_keys=True, indent=2) + "\n"
        profile_path.write_text(payload, encoding="utf-8")
        profile_path.chmod(0o444)
        profile_sha = hashlib.sha256(profile_path.read_bytes()).hexdigest()
        shutil.copyfile(profile_path, artifact / "profile.json")
        (artifact / "profile.json").chmod(0o444)
        compose = [docker, "compose", "-f", str(COMPOSE), "-p", project]
        rendered = _json([*compose, "config", "--format", "json"], env=docker_env)
        _validate_config(rendered, run_id=run_id, profile=profile_path)
        subprocess.run([*compose, "build", SERVICE], cwd=ROOT, env=docker_env, check=True,
                       timeout=300, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        image_data = _json([docker, "image", "inspect", f"fwrouter-acceptance-qcp:{run_id}"], env=docker_env)
        if not isinstance(image_data, list) or len(image_data) != 1:
            raise launcher.NotRun("qualified-child image inspect returned unexpected result")
        image_id = image_data[0].get("Id")
        image_labels = image_data[0].get("Config", {}).get("Labels", {})
        if (not image_id or image_labels.get(launcher.OWNER_LABEL) != launcher.OWNER_VALUE
                or image_labels.get(launcher.RUN_LABEL) != run_id
                or image_labels.get("org.opencontainers.image.revision") != revision):
            raise launcher.NotRun("qualified-child image identity labels do not match this run")
        subprocess.run([*compose, "create", SERVICE], cwd=ROOT, env=docker_env, check=True,
                       timeout=60, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        container_id = _small([*compose, "ps", "--all", "-q", SERVICE], env=docker_env).strip()
        if not re.fullmatch(r"[0-9a-f]{64}", container_id):
            raise launcher.NotRun("qualified-child exact container ID is unavailable")
        network = _json([docker, "network", "inspect", f"{project}_isolated"], env=docker_env)
        if not isinstance(network, list) or len(network) != 1:
            raise launcher.NotRun("qualified-child network inspect returned unexpected result")
        net = network[0]
        labels = net.get("Labels", {})
        if (net.get("Internal") is not True or labels.get(launcher.OWNER_LABEL) != launcher.OWNER_VALUE
                or labels.get(launcher.RUN_LABEL) != run_id):
            raise launcher.NotRun("qualified-child network is not this run's internal network")
        network_id = net.get("Id")
        if not network_id:
            raise launcher.NotRun("qualified-child network ID is missing")
        inspect = _json([docker, "inspect", container_id], env=docker_env)
        if not isinstance(inspect, list) or len(inspect) != 1:
            raise launcher.NotRun("qualified-child runtime inspect returned unexpected result")
        _validate_runtime(inspect[0], project=project, run_id=run_id, profile=profile_path, image_id=image_id)
        subprocess.run([*compose, "start", SERVICE], cwd=ROOT, env=docker_env, check=True,
                       timeout=30, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        preflight = json.loads(_small([docker, "exec", container_id, "python", "-c", _preflight_code()],
                                      env=docker_env))
        if preflight.get("status") != "passed" or preflight.get("nonce") != run_id or preflight.get("profile_sha256") != profile_sha:
            raise launcher.NotRun("qualified-child in-container isolation preflight did not pass")
        selectors = [
            "tests/test_protocol_native_validation.py",
            "tests/test_traffic_accounting.py::test_traffic_collect_script_reads_global_vpn_mark_and_xray_stats",
            "tests/test_vpn_auto_writer_guard.py::test_writer_guard_serializes_processes_and_cleans_up_after_flock_timeout",
            "tests/test_xray.py::test_xray_writer_guard_serializes_processes",
            "tests/test_xray_default_runner_archive.py",
        ]
        in_junit = "/tmp/fwrouter-qualified-child.xml"
        cmd = [docker, "exec", "--env", "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1", container_id,
               "python", "-m", "pytest", "-p", "no:cacheprovider", f"--junitxml={in_junit}",
               "-q", "--tb=short", *selectors]
        started = time.monotonic()
        import resource
        with log_host.open("wb") as output:
            child = subprocess.Popen(cmd, cwd=ROOT, env=docker_env, stdout=output, stderr=subprocess.STDOUT,
                                     start_new_session=True, preexec_fn=lambda: resource.setrlimit(
                                         resource.RLIMIT_FSIZE,
                                         (launcher.MAX_TEST_OUTPUT_BYTES, launcher.MAX_TEST_OUTPUT_BYTES)))
            try:
                code = child.wait(timeout=launcher.MAX_TEST_SECONDS)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, 9)
                child.wait(timeout=5)
                code = 124
        receipt["elapsed_seconds"] = round(time.monotonic() - started, 3)
        receipt["pytest_exit_code"] = code
        subprocess.run([docker, "cp", f"{container_id}:{in_junit}", str(junit_host)], cwd=ROOT,
                       env=docker_env, check=True, timeout=20, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
        receipt["tests"] = _read_junit(junit_host)
        receipt["expected_ids"] = receipt["tests"]["expected_ids"]
        log_bytes = log_host.stat().st_size
        if log_bytes > launcher.MAX_TEST_OUTPUT_BYTES:
            raise launcher.NotRun("qualified-child log exceeded its configured size limit")
        receipt["log_bytes"] = log_bytes
        receipt["profile_sha256"] = profile_sha
        receipt["status"] = "passed" if code == 0 and receipt["tests"]["contract_valid"] else "failed"
        if receipt["status"] != "passed":
            raise launcher.NotRun("qualified-child pytest returned failure")
    except (launcher.NotRun, OSError, subprocess.SubprocessError, ValueError, KeyError) as exc:
        receipt["status"] = "failed" if container_id else "NOTRUN"
        receipt["reason"] = str(exc)[:2048]
    finally:
        cleanup_ok = True
        if container_id:
            try:
                owned = _json([docker, "inspect", container_id], env=docker_env)
                if (not isinstance(owned, list) or len(owned) != 1
                        or owned[0].get("Config", {}).get("Labels", {}).get(launcher.OWNER_LABEL) != launcher.OWNER_VALUE
                        or owned[0].get("Config", {}).get("Labels", {}).get(launcher.RUN_LABEL) != run_id):
                    raise launcher.NotRun("qualified-child cleanup refused a container without exact ownership labels")
                subprocess.run([docker, "rm", "--force", container_id], cwd=ROOT, env=docker_env,
                               check=True, timeout=30, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                present = _small([docker, "ps", "--all", "--quiet", "--no-trunc", "--filter", f"id={container_id}"], env=docker_env)
                if present.strip():
                    raise launcher.NotRun("qualified-child container cleanup was not confirmed")
            except (OSError, subprocess.SubprocessError, launcher.NotRun):
                cleanup_ok = False
        else:
            try:
                owned_ids = _small([docker, "ps", "--all", "--quiet", "--no-trunc",
                                    "--filter", f"label={launcher.OWNER_LABEL}={launcher.OWNER_VALUE}",
                                    "--filter", f"label={launcher.RUN_LABEL}={run_id}"], env=docker_env)
                if owned_ids.strip():
                    cleanup_ok = False
            except (OSError, subprocess.SubprocessError, launcher.NotRun):
                cleanup_ok = False
        if network_id:
            try:
                net = _json([docker, "network", "inspect", network_id], env=docker_env)
                if (not isinstance(net, list) or len(net) != 1
                        or net[0].get("Labels", {}).get(launcher.OWNER_LABEL) != launcher.OWNER_VALUE
                        or net[0].get("Labels", {}).get(launcher.RUN_LABEL) != run_id):
                    raise launcher.NotRun("qualified-child cleanup refused a network without exact ownership labels")
                subprocess.run([docker, "network", "rm", network_id], cwd=ROOT, env=docker_env,
                               check=True, timeout=30, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if _small([docker, "network", "ls", "--quiet", "--no-trunc", "--filter", f"id={network_id}"], env=docker_env).strip():
                    raise launcher.NotRun("qualified-child network cleanup was not confirmed")
            except (OSError, subprocess.SubprocessError, launcher.NotRun):
                cleanup_ok = False
        if image_id:
            try:
                img = _json([docker, "image", "inspect", image_id], env=docker_env)
                if (not isinstance(img, list) or len(img) != 1
                        or img[0].get("Config", {}).get("Labels", {}).get(launcher.OWNER_LABEL) != launcher.OWNER_VALUE
                        or img[0].get("Config", {}).get("Labels", {}).get(launcher.RUN_LABEL) != run_id):
                    raise launcher.NotRun("qualified-child cleanup refused an image without exact ownership labels")
                subprocess.run([docker, "image", "rm", image_id], cwd=ROOT, env=docker_env,
                               check=True, timeout=30, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if _small([docker, "image", "ls", "--quiet", "--no-trunc", "--filter", f"reference=fwrouter-acceptance-qcp:{run_id}"], env=docker_env).strip():
                    raise launcher.NotRun("qualified-child image cleanup was not confirmed")
            except (OSError, subprocess.SubprocessError, launcher.NotRun):
                cleanup_ok = False
        try:
            ownership_filters = ["--filter", f"label={launcher.OWNER_LABEL}={launcher.OWNER_VALUE}",
                                 "--filter", f"label={launcher.RUN_LABEL}={run_id}"]
            remaining = (
                _small([docker, "ps", "--all", "--quiet", "--no-trunc", *ownership_filters], env=docker_env),
                _small([docker, "network", "ls", "--quiet", "--no-trunc", *ownership_filters], env=docker_env),
                _small([docker, "image", "ls", "--quiet", "--no-trunc", *ownership_filters], env=docker_env),
            )
            if any(value.strip() for value in remaining):
                cleanup_ok = False
        except (OSError, subprocess.SubprocessError, launcher.NotRun):
            cleanup_ok = False
        receipt["cleanup"] = "owned_resources_removed" if cleanup_ok and container_id else (
            "no_container_resources_created" if cleanup_ok else "cleanup_unconfirmed")
        if receipt["status"] == "passed" and receipt["cleanup"] != "owned_resources_removed":
            receipt["status"] = "failed"
        report_path.write_text(json.dumps(receipt, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        if profile_path.exists():
            profile_path.chmod(0o600)
        shutil.rmtree(profile_dir, ignore_errors=True)
        shutil.rmtree(root, ignore_errors=True)
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="execute in the qualified GitHub-hosted container")
    args = parser.parse_args()
    if not args.run:
        print(json.dumps({"status": "NOTRUN", "reason": "explicit --run is required"}))
        return 2
    try:
        receipt = run(dict(os.environ), launcher.host_facts())
    except (launcher.NotRun, KeyError, OSError, subprocess.SubprocessError) as exc:
        receipt = {"schema": "fwrouter-qualified-child-receipt/v1", "scope": "qualified-child-process",
                   "status": "NOTRUN", "reason": str(exc)[:2048]}
        print(json.dumps(receipt, sort_keys=True), file=sys.stderr)
        return 2
    print(json.dumps(receipt, sort_keys=True))
    return 0 if receipt.get("status") == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
