#!/usr/bin/env python3
"""One supplementary qualified packet run paired with the completed mandatory run."""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any

MAX_JSON_BYTES = 2 * 1024 * 1024
RETAIN = ("hosted-acceptance-report.json", "application-acceptance-receipt.json", "profile.json",
          "runtime-scale-metrics.json", "application-acceptance.xml", "worker-service-logs.json",
          "native-process-diagnostics.json", "diagnostic-export.json")


def read_json(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_JSON_BYTES:
        raise ValueError("missing or unbounded pair receipt")
    with path.open("rb") as stream:
        raw = stream.read(MAX_JSON_BYTES + 1)
    if len(raw) > MAX_JSON_BYTES:
        raise ValueError("pair receipt grew beyond bound")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("pair receipt is not an object")
    return value


def packet_reports(root: Path, sha: str, plan: str) -> list[Path]:
    found = []
    for path in root.glob("fwrouter-acceptance-*-artifacts/hosted-acceptance-report.json"):
        if path.parent.is_symlink():
            continue
        report = read_json(path)
        if report.get("suite") == "packet-diagnostic" and report.get("source_revision") == sha and report.get("plan_digest") == plan:
            found.append(path)
    return found


def validate_attempt(path: Path, sha: str, plan: str) -> dict[str, Any]:
    report = read_json(path)
    profile = read_json(path.parent / "profile.json")
    receipt = read_json(path.parent / "application-acceptance-receipt.json")
    metrics = read_json(path.parent / "runtime-scale-metrics.json")
    profile_hash = hashlib.sha256((path.parent / "profile.json").read_bytes()).hexdigest()
    for item in (report, receipt, profile):
        if item.get("source_revision") != sha or item.get("plan_digest") != plan:
            raise ValueError("paired attempt source or plan differs")
    nonce = report.get("suite_nonce")
    if (not re.fullmatch(r"[0-9a-f]{32}", str(nonce))
            or any(item.get("suite_nonce") != nonce for item in (profile, receipt))):
        raise ValueError("paired suite nonce binding missing or different")
    if report.get("profile_sha256") != profile_hash or receipt.get("profile_sha256") != profile_hash:
        raise ValueError("paired attempt profile digest differs")
    if receipt.get("cleanup_errors") != [] or report.get("cleanup") != "owned_resources_removed":
        raise ValueError("paired attempt cleanup is not confirmed")
    calibration = metrics.get("cpu_calibration_before_startup", {})
    if calibration.get("status") != "complete":
        raise ValueError("fixed CPU calibration did not complete; comparison unavailable")
    if metrics.get("startup_causality", {}).get("status") != "available":
        raise ValueError("worker counters unavailable; paired comparison is incomplete")
    manifest = metrics.get("startup_input_manifest")
    if not isinstance(manifest, dict) or manifest.get("schema") != "fwrouter-startup-inputs/v1":
        raise ValueError("startup input manifest missing")
    runtime = {key: profile.get(key) for key in ("xray", "mihomo", "dns_runtime", "playwright_python")}
    for name in ("xray", "mihomo"):
        native = runtime.get(name)
        if (not isinstance(native, dict) or not isinstance(native.get("version"), str)
                or not native["version"] or not re.fullmatch(r"[0-9a-f]{64}", str(native.get("sha256", "")))):
            raise ValueError("paired native runtime fingerprint missing")
    dns = runtime.get("dns_runtime")
    if (not isinstance(dns, dict) or not dns
            or any(not isinstance(dns.get(key), str) or not dns[key] for key in (
                "dnsmasq_base_package_version", "bind9_dnsutils_package_version", "iptables_package_version"))
            or not isinstance(runtime.get("playwright_python"), str) or not runtime["playwright_python"]):
        raise ValueError("paired DNS or dependency version missing")
    dependency_digest = report.get("dependency_base_image", {}).get("digest")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(dependency_digest)):
        raise ValueError("paired dependency image digest missing")
    return {"suite_nonce": report.get("suite_nonce"), "status": report.get("status"),
            "startup_ready": metrics.get("startup_ready"),
            "startup_wall_seconds": metrics.get("startup_wall_seconds"),
            "inputs": manifest, "calibration": calibration,
            "runtime": runtime,
            "dependency_image": dependency_digest,
            "profile_sha256": profile_hash}


def compare_attempts(first: dict[str, Any], second: dict[str, Any]) -> list[str]:
    errors = []
    if (not first.get("suite_nonce") or not second.get("suite_nonce")
            or first.get("suite_nonce") == second.get("suite_nonce")):
        errors.append("pair did not use two independent suites")
    for key in ("inputs", "runtime", "dependency_image"):
        if first.get(key) != second.get(key):
            errors.append(f"paired {key} differ")
    for key in ("workload_sha256", "input_bytes", "objects_per_decode", "iterations_per_sample", "samples_required"):
        if first.get("calibration", {}).get(key) != second.get("calibration", {}).get(key):
            errors.append(f"calibration {key} differs")
    return errors


def run_pair() -> int:
    root = Path(os.environ["RUNNER_TEMP"]).resolve(strict=True)
    repo = Path(__file__).resolve().parents[2]
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    plan = os.environ["FWROUTER_ACCEPTANCE_PLAN_DIGEST"]
    output = root / "fwrouter-d12-pair"
    output.mkdir(mode=0o700, exist_ok=False)
    summary: dict[str, Any] = {"schema": "fwrouter-startup-pair/v1", "source_revision": sha,
                              "plan_digest": plan, "supplementary_runs_maximum": 1,
                              "comparison_valid": False, "attempts": [], "errors": []}
    supplementary: Path | None = None
    before: set[Path] | None = None
    try:
        primary = packet_reports(root, sha, plan)
        if len(primary) != 1:
            raise ValueError("expected exactly one completed mandatory packet suite")
        first = validate_attempt(primary[0], sha, plan)
        summary["attempts"].append(first)
        primary_dir = output / "mandatory"
        primary_dir.mkdir()
        for name in RETAIN:
            source = primary[0].parent / name
            if source.is_symlink() or not source.is_file() or source.stat().st_size > MAX_JSON_BYTES:
                raise ValueError("primary paired artifact missing or unbounded")
            shutil.copyfile(source, primary_dir / name)
        before = set(root.glob("fwrouter-acceptance-*-artifacts/hosted-acceptance-report.json"))
        with (output / "supplementary-launcher.log").open("wb") as log:
            # Exactly one additional invocation, no retries or altered test selection.
            result = subprocess.run([sys.executable, "tests/acceptance/launcher.py", "--run", "--suite", "packet-diagnostic"],
                                    cwd=repo, env=dict(os.environ), stdout=log, stderr=subprocess.STDOUT,
                                    timeout=1200, check=False)
        created = set(root.glob("fwrouter-acceptance-*-artifacts/hosted-acceptance-report.json")) - before
        if len(created) != 1:
            raise ValueError("supplementary suite did not produce exactly one receipt")
        supplementary = next(iter(created))
        second = validate_attempt(supplementary, sha, plan)
        summary["attempts"].append(second)
        summary["supplementary_exit_code"] = result.returncode
        summary["errors"] = compare_attempts(first, second)
        summary["comparison_valid"] = not summary["errors"]
    except Exception as exc:
        reason = str(exc) if type(exc) is ValueError else "paired evidence validation or launch failed"
        summary["errors"].append(type(exc).__name__ + ": " + reason)
    finally:
        if supplementary is None and before is not None:
            created = set(root.glob("fwrouter-acceptance-*-artifacts/hosted-acceptance-report.json")) - before
            if len(created) == 1:
                supplementary = next(iter(created))
        if supplementary is not None:
            # Keep the supplementary receipt outside the normal mandatory archive;
            # aggregation has already completed and its result is never rewritten.
            shutil.move(str(supplementary.parent), str(output / "supplementary"))
        (output / "pair-summary.json").write_text(json.dumps(summary, sort_keys=True, indent=2) + "\n")
    return 0 if summary["comparison_valid"] and len(summary["attempts"]) == 2 and all(
        item.get("status") == "passed" and item.get("startup_ready") is True for item in summary["attempts"]
    ) else 1


if __name__ == "__main__":
    raise SystemExit(run_pair())
