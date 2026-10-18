#!/usr/bin/env python3
"""Run a fixed hosted startup comparison across immutable source revisions."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import platform
import re
import shutil
import signal
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


OLD_QUALIFIED_SHA = "4f0c43cecb3b7515be111fe169038ecdbdad6df0"
ORIGINAL_QUALIFIED_SHA = "a9bf34d1d4015da106fcb4e2facd98fc505d13e2"
QUALIFIED_OLD_TREE_OID = "a3ff940aa985a6930dc7361848d845992034b399"
MAIN_BASELINE_SHA = "47cfd63fc220f2890e140da27ff54c91a8a2c7a1"
ATTEMPT_COUNT_PER_ARM = 3
MAX_ATTEMPTS = 9
HARD_STARTUP_SECONDS = 45
ENGINEERING_TARGET_SECONDS = 40
CONTAINER_MEMORY_LIMIT_BYTES = 2560 * 1024 ** 2
LAUNCHER_ORCHESTRATION_TIMEOUT_SECONDS = 1800
LAUNCHER_CLEANUP_GRACE_SECONDS = 30
LAUNCHER_TERMINATE_GRACE_SECONDS = 5
LAUNCHER_KILL_WAIT_SECONDS = 5
SUITE = "runtime-scale-diagnostic"
REVIEWED_HARNESS_PATCH_SHA256 = "715b4917a24e3a54b5bfd455da1572dec0e1aff0bda077a09963cf7bd7e0e023"
REVIEWED_CONFTEST_LINT_PATCH_SHA256 = "76b7b5a15c32f4a003f0da6d6648a3a99cced64c22769c3eb5d81e276bfe2cd9"
HARNESS_LINT_FILES = (
    "tests/acceptance/launcher.py",
    "tests/acceptance/provision.py",
    "tests/application_acceptance/native_runner.py",
    "tests/application_acceptance/test_core_provider_mihomo.py",
    "tests/application_acceptance/test_packet_dataplane.py",
    "tests/application_acceptance/test_xray_generation.py",
)
FROZEN_WORKLOAD_FILES = (
    "tests/application_acceptance/test_runtime_scale.py",
    "tests/application_acceptance/worker.py",
    "tests/application_acceptance/profile.py",
    "tests/application_acceptance/fixtures/mihomo.initial.yaml",
    "tests/application_acceptance/fixtures/xray.initial.json",
    "tests/acceptance/compose.yaml",
    "tests/acceptance/compose.packet.yaml",
    "tests/acceptance/Application.Dependencies.Dockerfile",
    "tests/acceptance/dependency-image.lock.json",
    "tests/acceptance/dependency-image-consumer.lock.json",
    "tests/acceptance/kernel_packet_profile_schema.json",
    "tests/gates/requirements-ci.txt",
)
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class ComparisonError(RuntimeError):
    """The fixed comparison inputs or one hosted attempt failed validation."""


def comparison_schedule(candidate_sha: str) -> list[dict[str, Any]]:
    """Return the fixed balanced nine-run schedule without retries."""
    if not isinstance(candidate_sha, str) or not SHA_RE.fullmatch(candidate_sha):
        raise ComparisonError("candidate revision must be a full lowercase commit SHA")
    revisions = (OLD_QUALIFIED_SHA, MAIN_BASELINE_SHA, candidate_sha)
    if len(set(revisions)) != 3:
        raise ComparisonError("startup comparison requires three distinct immutable source arms")
    labels = ("old-qualified", "main-baseline", "candidate")
    by_label = dict(zip(labels, revisions, strict=True))
    # Balanced Latin blocks put each arm once in each temporal position.
    order = ("old-qualified", "main-baseline", "candidate",
             "candidate", "old-qualified", "main-baseline",
             "main-baseline", "candidate", "old-qualified")
    result: list[dict[str, Any]] = []
    counts = {label: 0 for label in labels}
    for index, label in enumerate(order, 1):
        counts[label] += 1
        result.append({"attempt": index, "arm": label, "source_sha": by_label[label],
                       "arm_attempt": counts[label]})
    if len(result) != MAX_ATTEMPTS or any(count != ATTEMPT_COUNT_PER_ARM for count in counts.values()):
        raise ComparisonError("internal startup comparison schedule is outside its fixed bound")
    return result


def _run(argv: list[str], *, cwd: Path, timeout: int = 30) -> str:
    try:
        result = subprocess.run(argv, cwd=cwd, check=True, capture_output=True, text=True,
                                timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ComparisonError(f"fixed source inspection failed: {argv[0]}") from exc
    return result.stdout.strip()


def _blob_sha(repo: Path, revision: str, relative: str) -> str:
    content = subprocess.run(["git", "show", f"{revision}:{relative}"], cwd=repo,
                             check=True, capture_output=True, timeout=15).stdout
    return hashlib.sha256(content).hexdigest()


def _validate_candidate_lineage(repo: Path, candidate_sha: str) -> None:
    """Require the candidate to descend from the fixed main comparison arm."""
    try:
        _run(["git", "cat-file", "-e", f"{candidate_sha}^{{commit}}"], cwd=repo)
        _run(["git", "merge-base", "--is-ancestor", MAIN_BASELINE_SHA, candidate_sha], cwd=repo)
    except ComparisonError as exc:
        raise ComparisonError("candidate source must descend from the fixed main baseline") from exc


def _validate_old_arm_tree(repo: Path) -> dict[str, str]:
    """Bind the fetchable comparison arm to the previously measured exact source tree."""
    old_tree = _run(["git", "rev-parse", f"{OLD_QUALIFIED_SHA}^{{tree}}"], cwd=repo)
    if old_tree != QUALIFIED_OLD_TREE_OID:
        raise ComparisonError("historical qualified comparison arm does not match its reviewed source tree")
    try:
        original_tree = _run(["git", "rev-parse", f"{ORIGINAL_QUALIFIED_SHA}^{{tree}}"], cwd=repo)
    except ComparisonError:
        original_tree = "not-present-in-checkout"
    if original_tree not in {QUALIFIED_OLD_TREE_OID, "not-present-in-checkout"}:
        raise ComparisonError("original qualified commit differs from the pinned comparison tree")
    return {"comparison_arm_commit": OLD_QUALIFIED_SHA,
            "original_measured_commit": ORIGINAL_QUALIFIED_SHA,
            "exact_tree_oid": QUALIFIED_OLD_TREE_OID,
            "original_commit_tree_check": original_tree,
            "timing_attribution": (
                "historical timing belongs to the original measured commit; comparison arm is source-tree equivalent")}


def _known_lint_harness_changes(repo: Path) -> dict[str, dict[str, str]]:
    """Pin and report the six reviewed nonbehavioral lint-only file differences."""
    expected_paths = set(HARNESS_LINT_FILES)
    actual = set(_run(["git", "diff", "--name-only", OLD_QUALIFIED_SHA, MAIN_BASELINE_SHA,
                       "--", *HARNESS_LINT_FILES], cwd=repo).splitlines())
    if actual != expected_paths:
        raise ComparisonError("historical harness delta differs from the reviewed six-file lint-only patch")
    patch = subprocess.run(["git", "diff", "--binary", OLD_QUALIFIED_SHA, MAIN_BASELINE_SHA,
                            "--", *HARNESS_LINT_FILES], cwd=repo, check=True, capture_output=True,
                           timeout=30).stdout
    if hashlib.sha256(patch).hexdigest() != REVIEWED_HARNESS_PATCH_SHA256:
        raise ComparisonError("historical acceptance harness patch differs from reviewed lint-only evidence")
    result: dict[str, dict[str, str]] = {}
    for relative in HARNESS_LINT_FILES:
        result[relative] = {
            "old_qualified_sha256": _blob_sha(repo, OLD_QUALIFIED_SHA, relative),
            "main_baseline_sha256": _blob_sha(repo, MAIN_BASELINE_SHA, relative),
        }
    return result


def validate_source_arms(repo: Path, candidate_sha: str) -> dict[str, Any]:
    schedule = comparison_schedule(candidate_sha)
    _validate_candidate_lineage(repo, candidate_sha)
    old_arm_tree = _validate_old_arm_tree(repo)
    for row in schedule:
        _run(["git", "cat-file", "-e", f"{row['source_sha']}^{{commit}}"], cwd=repo)
    lint_changes = _known_lint_harness_changes(repo)
    conftest = "tests/application_acceptance/conftest.py"
    old_conftest = _blob_sha(repo, OLD_QUALIFIED_SHA, conftest)
    main_conftest = _blob_sha(repo, MAIN_BASELINE_SHA, conftest)
    candidate_conftest = _blob_sha(repo, candidate_sha, conftest)
    if old_conftest != main_conftest:
        raise ComparisonError("historical startup conftest differs from the qualified main harness")
    conftest_patch = subprocess.run(["git", "diff", "--binary", MAIN_BASELINE_SHA, candidate_sha,
                                     "--", conftest], cwd=repo, check=True, capture_output=True,
                                    timeout=30).stdout
    if hashlib.sha256(conftest_patch).hexdigest() != REVIEWED_CONFTEST_LINT_PATCH_SHA256:
        raise ComparisonError("candidate conftest differs from the exact reviewed unused-import cleanup")
    frozen: dict[str, dict[str, str]] = {}
    for relative in FROZEN_WORKLOAD_FILES:
        hashes = {sha: _blob_sha(repo, sha, relative)
                  for sha in (OLD_QUALIFIED_SHA, MAIN_BASELINE_SHA, candidate_sha)}
        if len(set(hashes.values())) != 1:
            raise ComparisonError(f"frozen startup workload input differs across arms: {relative}")
        frozen[relative] = {"sha256": next(iter(hashes.values()))}
    # The only accepted harness drift is the exact lint-only historical patch above;
    # candidate source must match the current main launcher/test harness byte-for-byte.
    launcher_hashes = {sha: _blob_sha(repo, sha, "tests/acceptance/launcher.py")
                       for sha in (MAIN_BASELINE_SHA, candidate_sha)}
    if launcher_hashes[MAIN_BASELINE_SHA] != launcher_hashes[candidate_sha]:
        raise ComparisonError("candidate launcher differs from the qualified main harness")
    for relative in HARNESS_LINT_FILES[1:]:
        if _blob_sha(repo, MAIN_BASELINE_SHA, relative) != _blob_sha(repo, candidate_sha, relative):
            raise ComparisonError(f"candidate acceptance harness differs from main: {relative}")
    return {"arms": {"old-qualified": OLD_QUALIFIED_SHA, "main-baseline": MAIN_BASELINE_SHA,
                      "candidate": candidate_sha},
            "frozen_workload_files": frozen, "reviewed_harness_lint_changes": lint_changes,
            "reviewed_candidate_conftest_lint_change": {
                "path": conftest, "old_qualified_sha256": old_conftest,
                "main_baseline_sha256": main_conftest, "candidate_sha256": candidate_conftest,
                "patch_sha256": REVIEWED_CONFTEST_LINT_PATCH_SHA256,
                "removed_unused_import": "DIAGNOSTIC_TOTAL_LIMIT"},
            "historical_qualified_tree_equivalence": old_arm_tree,
            "schedule": schedule, "launcher_sha256": {
                sha: _blob_sha(repo, sha, "tests/acceptance/launcher.py")
                for sha in (OLD_QUALIFIED_SHA, MAIN_BASELINE_SHA, candidate_sha)}}


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        return None


def _cgroup_files() -> dict[str, Path]:
    result = {}
    candidates = (Path("/sys/fs/cgroup"),)
    for root in candidates:
        for name in ("cpu.max", "cpu.stat", "memory.max", "memory.current", "memory.peak",
                     "memory.events", "io.stat", "cpuset.cpus.effective"):
            if (root / name).is_file():
                result[name] = root / name
    return result


def _host_snapshot() -> dict[str, Any]:
    model = None
    try:
        for line in Path("/proc/cpuinfo").read_text(encoding="ascii", errors="replace").splitlines():
            if line.lower().startswith("model name"):
                model = line.partition(":")[2].strip()
                break
    except OSError:
        pass
    try:
        proc_stat = Path("/proc/stat").read_text(encoding="ascii").splitlines()[0]
    except (OSError, IndexError):
        proc_stat = None
    cg = {}
    for name, path in _cgroup_files().items():
        cg[name] = _read_text(path)
    return {"runner_hostname": platform.node(), "cpu_model": model, "proc_stat_cpu_line": proc_stat,
            "cgroup_v2": cg}


def _counter_values(snapshot: dict[str, Any], name: str) -> dict[str, int]:
    raw = snapshot.get("cgroup_v2", {}).get(name)
    if not isinstance(raw, str):
        return {}
    values: dict[str, int] = {}
    for line in raw.splitlines():
        parts = line.split()
        if name == "io.stat" and len(parts) > 1:
            device = parts[0]
            for pair in parts[1:]:
                key, separator, value = pair.partition("=")
                if separator and value.isdigit():
                    values[f"{device}.{key}"] = int(value)
            continue
        if len(parts) >= 2 and parts[-1].isdigit():
            try:
                values[" ".join(parts[:-1])] = int(parts[-1])
            except ValueError:
                continue
    return values


def _runner_counter_deltas(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name in ("cpu.stat", "memory.events", "io.stat"):
        left, right = _counter_values(before, name), _counter_values(after, name)
        result[name] = ({key: right[key] - left[key] for key in left.keys() & right.keys()
                         if right[key] >= left[key]} if left and right else None)
    return result


def _launcher_module(checkout: Path):
    path = checkout / "tests/acceptance/launcher.py"
    spec = importlib.util.spec_from_file_location(f"startup_arm_launcher_{checkout.name}", path)
    if spec is None or spec.loader is None:
        raise ComparisonError("source arm launcher could not be loaded for provenance")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _source_manifest_sha256(checkout: Path) -> str:
    launcher = _launcher_module(checkout)
    if launcher.APPLICATION_MEMORY_LIMIT_BYTES != CONTAINER_MEMORY_LIMIT_BYTES:
        raise ComparisonError("source arm changed the fixed 2.5 GiB application memory limit")
    digest = hashlib.sha256()
    for relative in sorted(launcher.git_files()):
        source = checkout / relative
        digest.update(relative.encode("utf-8") + b"\0")
        digest.update(source.read_bytes())
    return digest.hexdigest()


def _parse_report(path: Path, *, expected_sha: str, plan_digest: str) -> dict[str, Any]:
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ComparisonError("launcher did not retain a parseable hosted report") from exc
    if (not isinstance(report, dict) or report.get("suite") != SUITE
            or report.get("source_revision") != expected_sha
            or report.get("plan_digest") != plan_digest or not report.get("suite_nonce")
            or report.get("scope") != "hosted-kernel-packet"):
        raise ComparisonError("hosted report does not bind the expected suite, source, plan and scope")
    return report


def _cleanup_confirmed(report: dict[str, Any]) -> bool:
    return (report.get("cleanup") in {"owned_resources_removed", "no_container_resources_created"}
            and report.get("temporary_artifacts_removed") is True
            and not report.get("cleanup_unconfirmed"))


def _workload_projection(metrics: dict[str, Any]) -> dict[str, Any] | None:
    """Extract frozen workload identity and observed resource ceilings."""
    if metrics.get("schema") != "fwrouter-runtime-scale-metrics/v1":
        return None
    waves = metrics.get("wave_resource_observations")
    limits, config, source = (metrics.get("startup_limits_seconds"), metrics.get("config"),
                              metrics.get("subscription_source"))
    if (not isinstance(waves, list) or not waves
            or not all(isinstance(value, dict) for value in (limits, config, source))):
        return None
    wave_ceilings = []
    previous_oom_events = None
    for wave in waves:
        before, after = wave.get("before"), wave.get("after")
        if not isinstance(before, dict) or not isinstance(after, dict):
            return None
        before_cg, after_cg = before.get("cgroup"), after.get("cgroup")
        if not isinstance(before_cg, dict) or not isinstance(after_cg, dict):
            return None
        if (before_cg.get("max") != CONTAINER_MEMORY_LIMIT_BYTES
                or after_cg.get("max") != CONTAINER_MEMORY_LIMIT_BYTES):
            return None
        before_events, after_events = before_cg.get("events"), after_cg.get("events")
        if not isinstance(before_events, dict) or not isinstance(after_events, dict):
            return None
        oom_keys = ("oom", "oom_kill", "oom_group_kill")
        if any(type(before_events.get(key)) is not int
               or type(after_events.get(key)) is not int
               or before_events[key] != after_events[key] for key in oom_keys):
            return None
        current_oom_events = {key: after_events[key] for key in oom_keys}
        if previous_oom_events is not None and current_oom_events != previous_oom_events:
            return None
        previous_oom_events = current_oom_events
        wave_ceilings.append({"max_bytes": after_cg["max"],
                              "oom_events": current_oom_events})
    return {
        "schema": metrics["schema"],
        "config": {key: config.get(key) for key in (
            "config_bytes", "config_sha256", "top_level_rule_count",
            "transparent_subrule_bytes", "transparent_subrule_count",
            "full_vpn_fallback", "selector_targets")},
        "subscription_source": source,
        "subscription_profile_accounts": metrics.get("subscription_profile_accounts"),
        "subscription_profile_nodes": metrics.get("subscription_profile_nodes"),
        "runtime_verified_profile_snapshots": metrics.get("runtime_verified_profile_snapshots"),
        "fixed_external_clients": metrics.get("fixed_external_clients"),
        "internal_vpn_auto_clients": metrics.get("internal_vpn_auto_clients"),
        "applied_profile_bindings": metrics.get("applied_profile_bindings"),
        "synthetic_xray_loaded_clients": metrics.get("synthetic_xray_loaded_clients"),
        "synthetic_xray_identities_preserved": metrics.get("synthetic_xray_identities_preserved"),
        "startup_limits_seconds": limits,
        "wave_resource_ceilings": wave_ceilings,
    }


def _launcher_command(python: str, checkout: Path) -> list[str]:
    return [python, str(checkout / "tests/acceptance/launcher.py"), "--run", "--suite", SUITE]


def _launcher_environment(base_env: dict[str, str], checkout: Path) -> dict[str, str]:
    """Keep the runner environment while binding the launcher to its exact worktree."""
    result = dict(base_env)
    result["GITHUB_WORKSPACE"] = str(checkout.resolve(strict=True))
    return result


def _run_launcher(argv: list[str], *, cwd: Path, env: dict[str, str], timeout: int =
                  LAUNCHER_ORCHESTRATION_TIMEOUT_SECONDS,
                  cleanup_grace: int = LAUNCHER_CLEANUP_GRACE_SECONDS,
                  terminate_grace: int = LAUNCHER_TERMINATE_GRACE_SECONDS,
                  kill_wait: int = LAUNCHER_KILL_WAIT_SECONDS,
                  popen_factory=None) -> tuple[subprocess.CompletedProcess[bytes], dict[str, Any] | None]:
    """Bound the orchestration wait while letting the launcher's finally cleanup run."""
    if not (1 <= cleanup_grace <= 30 and 1 <= terminate_grace <= 30 and 1 <= kill_wait <= 30):
        raise ComparisonError("launcher cleanup grace periods exceed their fixed bounds")
    factory = popen_factory or subprocess.Popen
    process = factory(argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                      start_new_session=True)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
        return subprocess.CompletedProcess(argv, process.returncode, stdout or b"", stderr or b""), None
    except subprocess.TimeoutExpired:
        pass

    lifecycle: dict[str, Any] = {"deadline_exceeded": True, "interrupt_sent": False,
                                 "terminate_group_sent": False, "kill_group_sent": False,
                                 "launcher_exited": False}
    stdout = stderr = b""
    try:
        process.send_signal(signal.SIGINT)
        lifecycle["interrupt_sent"] = True
    except (OSError, ProcessLookupError):
        pass
    try:
        stdout, stderr = process.communicate(timeout=cleanup_grace)
        lifecycle["launcher_exited"] = True
    except subprocess.TimeoutExpired as exc:
        stdout, stderr = exc.output or b"", exc.stderr or b""
        try:
            os.killpg(process.pid, signal.SIGTERM)
            lifecycle["terminate_group_sent"] = True
        except (OSError, ProcessLookupError):
            pass
        try:
            stdout, stderr = process.communicate(timeout=terminate_grace)
            lifecycle["launcher_exited"] = True
        except subprocess.TimeoutExpired as term_exc:
            stdout, stderr = term_exc.output or stdout, term_exc.stderr or stderr
            try:
                os.killpg(process.pid, signal.SIGKILL)
                lifecycle["kill_group_sent"] = True
            except (OSError, ProcessLookupError):
                pass
            try:
                stdout, stderr = process.communicate(timeout=kill_wait)
                lifecycle["launcher_exited"] = True
            except subprocess.TimeoutExpired as kill_exc:
                stdout, stderr = kill_exc.output or stdout, kill_exc.stderr or stderr
    return subprocess.CompletedProcess(argv, process.returncode if process.returncode is not None else 124,
                                        stdout or b"", stderr or b""), lifecycle


def _may_continue_attempts(*, orchestration_deadline_exceeded: bool,
                           cleanup_confirmed: bool) -> bool:
    """Never start another matched run after the orchestration deadline."""
    return not orchestration_deadline_exceeded and cleanup_confirmed


def _numeric_distribution(values: list[object]) -> dict[str, Any]:
    numbers = [float(value) for value in values
               if isinstance(value, (int, float)) and not isinstance(value, bool)]
    return {"n": len(numbers), "min": min(numbers) if numbers else None,
            "median": statistics.median(numbers) if numbers else None,
            "max": max(numbers) if numbers else None}


def _arm_summary(outcomes: list[dict[str, Any]]) -> dict[str, Any]:
    result = {}
    for arm in ("old-qualified", "main-baseline", "candidate"):
        rows = [row for row in outcomes if row.get("arm") == arm]
        metrics = [row.get("runtime_metrics") for row in rows]
        result[arm] = {
            "attempts": len(rows),
            "startup_wall_seconds": _numeric_distribution([
                item.get("startup_wall_seconds") if isinstance(item, dict) else None for item in metrics]),
            "api_process_cpu_seconds_at_readiness": _numeric_distribution([
                item.get("api_process_cpu_seconds_at_readiness") if isinstance(item, dict) else None
                for item in metrics]),
            "engineering_target_met_count": sum(
                isinstance(item, dict) and isinstance(item.get("startup_wall_seconds"), (int, float))
                and not isinstance(item.get("startup_wall_seconds"), bool)
                and item["startup_wall_seconds"] < ENGINEERING_TARGET_SECONDS for item in metrics),
        }
    return result


def _copy_attempt_artifacts(artifact_dir: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    for source in sorted(artifact_dir.iterdir()):
        if source.is_symlink() or not source.is_file() or source.stat().st_size > 4 * 1024 * 1024:
            raise ComparisonError("hosted attempt artifact is unsafe or exceeds its fixed size bound")
        shutil.copyfile(source, destination / source.name)


def _redact_launcher_output(checkout: Path, value: bytes) -> bytes:
    return _launcher_module(checkout)._redact_public(value, limit=65536).encode("utf-8")


def run_comparison(repo: Path, runner_temp: Path, candidate_sha: str, plan_digest: str,
                   runtime_inputs: Path) -> int:
    if not re.fullmatch(r"[0-9a-f]{64}", plan_digest):
        raise ComparisonError("Phase D plan digest must be a full SHA-256")
    if not runtime_inputs.is_file() or runtime_inputs.is_symlink():
        raise ComparisonError("provisioned hosted runtime input receipt is missing or unsafe")
    provenance = validate_source_arms(repo, candidate_sha)
    input_bytes = runtime_inputs.read_bytes()
    if len(input_bytes) > 1024 * 1024:
        raise ComparisonError("provisioned runtime input receipt exceeds its fixed size bound")
    input_sha = hashlib.sha256(input_bytes).hexdigest()
    try:
        input_manifest = json.loads(input_bytes)
    except json.JSONDecodeError as exc:
        raise ComparisonError("provisioned hosted runtime input receipt is invalid JSON") from exc
    if (not isinstance(input_manifest, dict)
            or input_manifest.get("schema") != "fwrouter-hosted-inputs/v1"
            or not isinstance(input_manifest.get("dependency_image"), str)
            or "@sha256:" not in input_manifest["dependency_image"]
            or not re.fullmatch(r"[0-9a-f]{64}", input_manifest.get("dependency_lock_sha256", ""))):
        raise ComparisonError("provisioned runtime receipt lacks the pinned dependency image or lock digest")
    native_inputs = input_manifest.get("native")
    chromium_input = input_manifest.get("chromium")
    if (not isinstance(native_inputs, dict) or not isinstance(chromium_input, dict)
            or any(not isinstance(native_inputs.get(name), dict) for name in ("xray", "mihomo"))):
        raise ComparisonError("provisioned runtime receipt has invalid native/browser input records")
    expected_binaries = {
        name: native_inputs[name].get("binary_sha256") for name in ("xray", "mihomo")
    }
    expected_binaries["chromium"] = chromium_input.get("executable_sha256")
    if any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
           for value in expected_binaries.values()):
        raise ComparisonError("provisioned runtime receipt lacks fixed native/browser binary hashes")
    root = runner_temp / "phase-d-startup-comparison"
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    (root / "runtime-inputs.json").write_bytes(runtime_inputs.read_bytes())
    (root / "source-provenance.json").write_text(json.dumps(provenance, sort_keys=True, indent=2) + "\n")
    provenance["runtime_inputs_sha256"] = input_sha
    worktrees: dict[str, Path] = {}
    source_manifest_hashes: dict[str, str] = {}
    outcomes: list[dict[str, Any]] = []
    profile_projections: list[dict[str, Any]] = []
    workload_projections: list[dict[str, Any]] = []
    seen_nonces: set[str] = set()
    failed = False
    stop_reason = None
    try:
        for label, revision in provenance["arms"].items():
            checkout = root / "source-arms" / label
            checkout.parent.mkdir(mode=0o700, exist_ok=True)
            _run(["git", "worktree", "add", "--detach", str(checkout), revision], cwd=repo, timeout=60)
            worktrees[label] = checkout
            source_manifest_hashes[label] = _source_manifest_sha256(checkout)
            if _run(["git", "status", "--porcelain"], cwd=checkout):
                raise ComparisonError(f"source arm is not clean after worktree creation: {label}")
        provenance["source_manifest_sha256"] = source_manifest_hashes
        (root / "source-provenance.json").write_text(json.dumps(provenance, sort_keys=True, indent=2) + "\n")
        for item in provenance["schedule"]:
            attempt_dir = root / f"attempt-{item['attempt']:02d}"
            attempt_dir.mkdir(mode=0o700)
            before = _host_snapshot()
            started = time.monotonic()
            command = _launcher_command(sys.executable, worktrees[item["arm"]])
            process, orchestration_timeout = _run_launcher(
                command, cwd=worktrees[item["arm"]],
                env=_launcher_environment(dict(os.environ), worktrees[item["arm"]]))
            elapsed = time.monotonic() - started
            (attempt_dir / "launcher-stdout.txt").write_bytes(
                _redact_launcher_output(worktrees[item["arm"]], process.stdout))
            (attempt_dir / "launcher-stderr.txt").write_bytes(
                _redact_launcher_output(worktrees[item["arm"]], process.stderr))
            artifact_dirs = sorted(runner_temp.glob("fwrouter-acceptance-*-artifacts"),
                                   key=lambda path: path.stat().st_mtime_ns)
            candidates = []
            for path in artifact_dirs:
                if not path.is_dir() or path.is_symlink():
                    continue
                report_path = path / "hosted-acceptance-report.json"
                if (report_path.is_symlink() or not report_path.is_file()
                        or report_path.stat().st_size > 4 * 1024 * 1024):
                    continue
                try:
                    row = json.loads(report_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if not isinstance(row, dict):
                    continue
                if (row.get("suite") == SUITE and row.get("source_revision") == item["source_sha"]
                        and row.get("suite_nonce") not in seen_nonces):
                    candidates.append((path, row))
            if len(candidates) != 1:
                failed = True
                outcomes.append({**item, "status": "failed", "exit_code": process.returncode,
                                 "error": "attempt report missing or ambiguous", "runner_before": before,
                                 "runner_after": _host_snapshot(), "elapsed_wall_seconds": elapsed})
                if orchestration_timeout:
                    outcomes[-1]["orchestration_timeout"] = orchestration_timeout
                    outcomes[-1]["cleanup_confirmed"] = False
                    outcomes[-1]["cleanup_state"] = "unknown; launcher report unavailable"
                    stop_reason = "launcher orchestration deadline exceeded; cleanup report unavailable"
                else:
                    stop_reason = "attempt report missing or ambiguous; cleanup cannot be established"
                break
            artifact_dir = candidates[0][0]
            report = _parse_report(artifact_dir / "hosted-acceptance-report.json",
                                   expected_sha=item["source_sha"], plan_digest=plan_digest)
            nonce = report["suite_nonce"]
            if nonce in seen_nonces:
                raise ComparisonError("launcher reused a suite nonce")
            seen_nonces.add(nonce)
            _copy_attempt_artifacts(artifact_dir, attempt_dir / "artifacts")
            metrics_path = artifact_dir / "runtime-scale-metrics.json"
            metrics: dict[str, Any] = {}
            if metrics_path.is_file() and not metrics_path.is_symlink():
                metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
            if not isinstance(metrics, dict):
                metrics = {}
            profile_path = artifact_dir / "profile.json"
            application_receipt_path = artifact_dir / "application-acceptance-receipt.json"
            try:
                app_receipt = json.loads(application_receipt_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                app_receipt = {}
            if not isinstance(app_receipt, dict):
                app_receipt = {}
            try:
                profile = json.loads(profile_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                profile = {}
            if not isinstance(profile, dict):
                profile = {}
            profile_projection = {key: value for key, value in profile.items()
                                  if key not in {"source_revision", "suite_nonce"}}
            profile_sha = (hashlib.sha256(profile_path.read_bytes()).hexdigest()
                           if profile_path.is_file() and not profile_path.is_symlink() else None)
            profile_bound = (
                profile.get("schema") == "fwrouter-acceptance-profile/v2"
                and profile.get("source_revision") == item["source_sha"]
                and profile.get("suite_nonce") == nonce
                and profile.get("plan_digest") == plan_digest
                and report.get("profile_sha256") == profile_sha
                and app_receipt.get("profile_sha256") == profile_sha
                and app_receipt.get("suite_nonce") == nonce
            )
            limits = metrics.get("startup_limits_seconds", {})
            limit_binding_valid = (isinstance(limits, dict)
                                   and limits.get("hard_deadline") == HARD_STARTUP_SECONDS
                                   and limits.get("engineering_target") == ENGINEERING_TARGET_SECONDS)
            workload_projection = _workload_projection(metrics)
            workload_binding_valid = workload_projection is not None
            native_binding_valid = (
                report.get("base_image") == input_manifest["dependency_image"]
                and isinstance(report.get("binary_sha256"), dict)
                and all(report["binary_sha256"].get(name) == value
                        for name, value in expected_binaries.items())
            )
            if profile_projection:
                profile_projections.append(profile_projection)
            if workload_projection is not None:
                workload_projections.append(workload_projection)
            after = _host_snapshot()
            outcome = {**item, "status": "failed" if orchestration_timeout else report.get("status"),
                       "launcher_report_status": report.get("status"), "exit_code": process.returncode,
                       "suite_nonce": nonce, "plan_digest": plan_digest,
                       "launcher_sha256": provenance["launcher_sha256"][item["source_sha"]],
                       "source_manifest_sha256": source_manifest_hashes[item["arm"]],
                       "reported_native_inputs": report.get("binary_sha256"),
                       "dependency_image": report.get("base_image"),
                       "dependency_lock_digest": input_manifest["dependency_lock_sha256"],
                       "native_inputs_binding_valid": native_binding_valid,
                       "profile_sha256": profile_sha, "profile_binding_valid": profile_bound,
                       "startup_limits_binding_valid": limit_binding_valid,
                       "workload_binding_valid": workload_binding_valid,
                       "workload_projection": workload_projection,
                       "runtime_metrics": metrics, "report_cleanup": {
                           "state": report.get("cleanup"),
                           "temporary_artifacts_removed": report.get("temporary_artifacts_removed"),
                           "cleanup_unconfirmed": report.get("cleanup_unconfirmed", [])},
                       "runner_before": before, "runner_after": after,
                       "runner_counter_deltas": _runner_counter_deltas(before, after),
                       "elapsed_wall_seconds": round(elapsed, 4),
                       "orchestration_timeout": orchestration_timeout,
                       "cleanup_confirmed": _cleanup_confirmed(report),
                       "phase_cpu_interpretation": (
                           "existing process-wide process_time; nested phase wrappers overlap and are not exclusive"),
                       "thread_cpu_measurement": "NOT_AVAILABLE; no worker instrumentation added",
                       "process_io_measurement": "runner cgroup io.stat only; aggregate runner scope"}
            (attempt_dir / "attempt.json").write_text(json.dumps(outcome, sort_keys=True, indent=2) + "\n")
            outcomes.append(outcome)
            if (process.returncode != 0 or report.get("status") != "passed" or not profile_bound
                    or not limit_binding_valid or not workload_binding_valid or not native_binding_valid
                    or not _cleanup_confirmed(report)
                    or not isinstance(metrics.get("startup_wall_seconds"), (int, float))
                    or isinstance(metrics.get("startup_wall_seconds"), bool)
                    or metrics["startup_wall_seconds"] >= HARD_STARTUP_SECONDS):
                failed = True
            if not _may_continue_attempts(
                    orchestration_deadline_exceeded=orchestration_timeout is not None,
                    cleanup_confirmed=_cleanup_confirmed(report)):
                if orchestration_timeout:
                    failed = True
                    stop_reason = "launcher orchestration deadline exceeded; no further attempt started"
                else:
                    stop_reason = "owned-resource cleanup was not confirmed; remaining attempts stopped"
                break
            if profile_projections and any(value != profile_projections[0] for value in profile_projections[1:]):
                failed = True
            if workload_projections and any(value != workload_projections[0]
                                            for value in workload_projections[1:]):
                failed = True
        summary = {"schema": "fwrouter-startup-comparison/v1",
                   "status": "failed" if failed or stop_reason else "passed",
                   "candidate_sha": candidate_sha, "plan_digest": plan_digest,
                   "runtime_inputs_sha256": input_sha, "attempt_limit": MAX_ATTEMPTS,
                   "attempts_completed": len(outcomes), "arm_summary": _arm_summary(outcomes),
                   "workload_projection_consistent": (
                       len(workload_projections) == MAX_ATTEMPTS
                       and all(value == workload_projections[0]
                               for value in workload_projections[1:])),
                   "attempts": outcomes,
                   "stop_reason": stop_reason}
        (root / "comparison-summary.json").write_text(json.dumps(summary, sort_keys=True, indent=2) + "\n")
        exit_code = 1 if failed or stop_reason or len(outcomes) != MAX_ATTEMPTS else 0
    finally:
        removal_errors = []
        for label, checkout in reversed(list(worktrees.items())):
            if checkout.parent != (root / "source-arms") or checkout.is_symlink():
                removal_errors.append(f"unsafe worktree path: {label}")
                continue
            result = subprocess.run(["git", "worktree", "remove", str(checkout)], cwd=repo,
                                    capture_output=True, text=True, timeout=60, check=False)
            if result.returncode != 0:
                removal_errors.append(f"worktree cleanup failed: {label}")
        if removal_errors:
            with (root / "cleanup-errors.txt").open("w", encoding="utf-8") as stream:
                stream.write("\n".join(removal_errors) + "\n")
            exit_code = 1
    if removal_errors:
        try:
            summary_path = root / "comparison-summary.json"
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            summary["status"] = "failed"
            summary["worktree_cleanup_errors"] = removal_errors
            summary_path.write_text(json.dumps(summary, sort_keys=True, indent=2) + "\n")
        except (OSError, json.JSONDecodeError):
            pass
    return exit_code


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-sha", required=True)
    parser.add_argument("--plan-digest", required=True)
    parser.add_argument("--runtime-inputs", type=Path, required=True)
    parser.add_argument("--runner-temp", type=Path, required=True)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    args = parser.parse_args()
    try:
        return run_comparison(args.repo.resolve(), args.runner_temp.resolve(), args.candidate_sha,
                              args.plan_digest, args.runtime_inputs.resolve())
    except (ComparisonError, OSError, subprocess.SubprocessError, ValueError, KeyError) as exc:
        print(f"startup comparison failed closed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
