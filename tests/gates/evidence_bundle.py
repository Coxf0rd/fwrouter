#!/usr/bin/env python3
"""Build and verify a bounded, run-bound FWRouter CI evidence bundle.

The bundle contains strict aggregate inputs and sealed producer inventories,
not raw test logs, packet captures, profiles, or runtime dumps.  Verification is
read-only and must be performed by a separately trusted checkout at the
recorded source commit; files carried inside the bundle are never executed.
"""
from __future__ import annotations

import argparse
import html
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import tempfile
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
GATES = ROOT / "tests/gates"
MAX_FILES = 6000
MAX_BYTES = 512 * 1024 * 1024
SCHEMA = "fwrouter-ci-evidence-bundle/v1"
REPOSITORY = "Coxf0rd/fwrouter"
WORKFLOW_NAME = "FWRouter affected CI"
REQUIRED_CHECK = "Affected L0-L5 with hosted profiles"
PREPR_REQUIRED_CHECK = "Pre-PR full safe qualification"
SOURCE_FILES = (
    ".github/workflows/test-gates.yml",
    "tests/gates/evidence_bundle.py",
    "tests/gates/parallel_ci.py",
    "tests/gates/routine_shards.py",
    "tests/gates/visibility_report.py",
    "tests/gates/aggregate_hosted.py",
    "tests/gates/gate.py",
    "backend/tests/gate_plugin.py",
    "tests/gates/ci_inputs.py",
    "tests/gates/verify_backend_wheel.py",
    "tests/gates/manifest.json",
    "tests/gates/requirements-build.txt",
    "tests/acceptance/scenarios.json",
    "tests/acceptance/qualified-child-scenarios.json",
    "tests/acceptance/provision.py",
    "tests/acceptance/launcher.py",
    "installer/safe_recovery.py",
    "installer/qualify_recovery.py",
    "knowledge/audits/test_architecture_cicd_2026-10-04/BASELINE_STATUS.json",
)
SAFE_RECEIPTS = {
    "fwrouter-affected-plan.json", "build-summary.json", "wheel-metadata.json",
    "fwrouter-affected-unit.json", "runtime-inputs.json", "hosted-acceptance-report.json",
    "shard.json", "visibility.json",
    "qualified-child-receipt.json", "qualified-xray-docker-receipt.json",
    "profile.json",
    "fwrouter-aggregate.json", "required-result.json", "qualification.json", "stage.json",
    "manifest.json", "source.tar", "producer.json", "measurements.json",
}
FORBIDDEN_SUFFIXES = {".pcap", ".pcapng", ".sqlite", ".sqlite3", ".db", ".pem", ".key", ".p12"}
FORBIDDEN_NAMES = {".env", "config.json", "config.yaml", "config.yml", "private.key"}

sys.path.insert(0, str(GATES))
import gate
import parallel_ci
import routine_shards
import aggregate_hosted as strict
import ci_inputs


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_json(path: Path, limit: int = 16 * 1024 * 1024) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
        raise gate.GateError(f"required evidence is missing, unsafe, or oversized: {path.name}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise gate.GateError(f"evidence root is not an object: {path.name}")
    return value


def safe_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (not value or "\\" in value or path.is_absolute() or path.as_posix() != value
            or any(part in {"", ".", ".."} for part in path.parts)):
        raise gate.GateError("unsafe evidence bundle path")
    if any(part.startswith(".") for part in path.parts) and value not in {
        ".github/workflows/test-gates.yml", "verifier-source/.github/workflows/test-gates.yml",
        ".github/workflows/test-full-suite.yml", "verifier-source/.github/workflows/test-full-suite.yml",
    }:
        raise gate.GateError("hidden paths are not allowed outside the pinned workflow source")
    if path.name.lower() in FORBIDDEN_NAMES or path.suffix.lower() in FORBIDDEN_SUFFIXES:
        raise gate.GateError("sensitive or raw diagnostic data is not eligible for the final bundle")
    return path


def inventory(root: Path) -> list[dict[str, Any]]:
    if root.is_symlink() or not root.is_dir():
        raise gate.GateError("bundle root is missing or unsafe")
    entries: list[dict[str, Any]] = []
    total = 0
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise gate.GateError("symlink in bundle")
        if path.is_dir():
            continue
        if not stat.S_ISREG(path.stat().st_mode):
            raise gate.GateError("special file in bundle")
        relative = path.relative_to(root).as_posix()
        safe_path(relative)
        if relative == "content-manifest.json":
            continue
        size = path.stat().st_size
        total += size
        if len(entries) >= MAX_FILES or total > MAX_BYTES:
            raise gate.GateError("bundle exceeds file or byte bound")
        entries.append({"path": relative, "bytes": size, "sha256": sha256(path)})
    return entries


def load_plan(evidence: Path) -> tuple[Path, dict[str, Any]]:
    candidates = list((evidence / "plan").glob("fwrouter-affected-plan.json"))
    if len(candidates) != 1:
        raise gate.GateError("exactly one immutable plan is required")
    plan_path = candidates[0]
    plan = read_json(plan_path, 8 * 1024 * 1024)
    gate.ensure_plan(plan, gate.load_manifest())
    return plan_path, plan


def main_smoke_for(event: str, ref: str) -> bool:
    return event == "push" and ref == "refs/heads/main"


def mode_for_plan(event: str, ref: str, plan: dict[str, Any]) -> str:
    if event == 'workflow_dispatch' and ref.startswith('refs/heads/') and ref != 'refs/heads/main':
        if plan.get('mandatory_suite_required') is not True:
            raise gate.GateError('pre-PR dispatch requires the complete safe mandatory plan')
        return 'pre-pr-full-safe'
    if event == 'push' and ref == 'refs/heads/main':
        if plan.get('mandatory_suite_required') is not True:
            raise gate.GateError('main push requires the complete safe mandatory plan')
        return 'main-mandatory'
    if event == 'pull_request' and ref.startswith('refs/pull/'):
        return 'pr-full-safe' if plan.get('mandatory_suite_required') is True else 'pr-affected'
    raise gate.GateError('event/ref cannot identify an affected CI qualification mode')


def check_eligibility(evidence: Path, required_root: Path, *, event: str, ref: str,
                      run_id: str, attempt: str, upstream: dict[str, Any],
                      require_prepr_scope: bool = False) -> tuple[dict[str, Any], dict[str, Any]]:
    plan_path, plan = load_plan(evidence)
    required_result = read_json(required_root / "required-result.json")
    report = read_json(required_root / "fwrouter-aggregate.json")
    main_smoke = main_smoke_for(event, ref)
    needed = parallel_ci.requirements(plan, main_smoke)
    mode = mode_for_plan(event, ref, plan)
    problems = parallel_ci.validate_results(upstream, needed)
    expected_identity = {
        "source_commit": plan["source_commit"], "plan_digest": plan["plan_digest"],
        "manifest_digest": plan["manifest_digest"], "manifest_version": plan["manifest_version"],
        "run_id": run_id, "run_attempt": attempt,
    }
    if (required_result.get("schema") != "fwrouter-parallel-required/v1"
            or required_result.get("eligible") is not True
            or any(required_result.get(key) != value for key, value in expected_identity.items())
            or required_result.get("upstream") != upstream):
        problems.append("strict required-result identity/status mismatch")
    recovery_required = (set(("installer/tests/test_safe_recovery.py", "backend/tests/test_network_contract.py",
                              "backend/tests/test_dnsmasq.py", "tests/application_acceptance/test_packet_dataplane.py"))
                        <= set(plan["selected_files"]))
    if required_result.get("safe_recovery_required") is not recovery_required:
        problems.append("safe-recovery requirement does not match the immutable plan")
    if require_prepr_scope and (mode != 'pre-pr-full-safe'
                                or required_result.get('qualification_scope') != parallel_ci.PREPR_SCOPE):
        problems.append('required-result is not bound to the non-promotional pre-PR scope')
    expected_scope = parallel_ci.qualification_scope(mode)
    if (required_result.get('qualification_scope') is not None
            and required_result.get('qualification_scope') != expected_scope):
        problems.append('required-result qualification scope does not match the trusted event and plan')
    if (report.get("eligible") is not True or report.get("source_commit") != plan["source_commit"]
            or report.get("plan_digest") != plan["plan_digest"]
            or report.get("manifest_version") != plan["manifest_version"]
            or report.get("main_smoke_required") is not main_smoke
            or report.get("blocked_reasons") != [] or report.get("missing_levels") != []):
        problems.append("strict aggregate is not a complete eligible result")
    if (required_result.get("safe_recovery_required") is True):
        recovery = required_root / "safe-recovery"
        for name in ("qualification.json", "stage.json", "artifact/manifest.json", "artifact/source.tar"):
            item = recovery / name
            if item.is_symlink() or not item.is_file():
                problems.append(f"required safe-recovery evidence missing: {name}")
        if not problems:
            import importlib.util
            spec = importlib.util.spec_from_file_location("fwrouter_bundle_safe_recovery", ROOT / "installer/safe_recovery.py")
            if spec is None or spec.loader is None:
                raise gate.GateError("safe-recovery verifier is unavailable")
            module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
            try:
                safe_manifest, _ = module.verify(recovery / "artifact", recovery / "qualification.json")
                stage = read_json(recovery / "stage.json")
                if (safe_manifest.get("source_sha") != plan["source_commit"]
                        or stage.get("status") != "isolated_stage_complete"
                        or stage.get("source_sha") != plan["source_commit"]):
                    problems.append("safe-recovery source or stage evidence mismatch")
            except (OSError, ValueError, module.RecoveryError) as exc:
                problems.append(f"safe-recovery evidence is invalid: {exc}")
    elif (required_root / "safe-recovery").exists():
        problems.append("unexpected safe-recovery evidence for an unrequired profile")
    if problems:
        raise gate.GateError("final evidence is not eligible: " + "; ".join(problems))
    return plan, report


def verify_producer_projection(root: Path, role: str, plan: dict[str, Any], *, run_id: str,
                               attempt: str, include_paths: list[str]) -> dict[str, Any]:
    producer = read_json(root / "producer.json", 64 * 1024)
    strict.verify_digest(producer, "producer_digest", f"{role} producer")
    expected = {
        "schema": "fwrouter-parallel-producer/v1", "role": role, "outcome": "success",
        "source_commit": plan["source_commit"], "plan_digest": plan["plan_digest"],
        "manifest_digest": plan["manifest_digest"], "manifest_version": plan["manifest_version"],
        "run_id": run_id, "run_attempt": attempt,
    }
    if any(producer.get(key) != value for key, value in expected.items()):
        raise gate.GateError(f"{role} producer identity or outcome is not qualified")
    files = producer.get("files")
    if not isinstance(files, dict):
        raise gate.GateError(f"{role} producer file inventory is malformed")
    validate_producer_inventory(files)
    for name in include_paths:
        safe_path(name)
        row = files.get(name)
        source = root / name
        if (not isinstance(row, dict) or source.is_symlink() or not source.is_file()
                or row != {"bytes": source.stat().st_size, "sha256": sha256(source)}):
            raise gate.GateError(f"{role} required evidence differs from its producer seal")
    return producer


def validate_producer_inventory(files: dict[str, Any]) -> None:
    if len(files) > MAX_FILES:
        raise gate.GateError("producer inventory exceeds file limit")
    total = 0
    for relative, row in files.items():
        if not isinstance(relative, str):
            raise gate.GateError("producer inventory contains a non-string relative path")
        path = PurePosixPath(relative)
        if (not relative or "\\" in relative
                or path.is_absolute() or path.as_posix() != relative
                or any(part in {"", ".", ".."} for part in path.parts)):
            raise gate.GateError("producer inventory contains an unsafe relative path")
        if (not isinstance(row, dict) or set(row) != {"bytes", "sha256"}
                or not isinstance(row["bytes"], int) or isinstance(row["bytes"], bool)
                or row["bytes"] < 0 or not isinstance(row["sha256"], str)
                or len(row["sha256"]) != 64
                or any(char not in "0123456789abcdef" for char in row["sha256"])):
            raise gate.GateError("producer inventory has malformed size or digest metadata")
        total += row["bytes"]
        if total > MAX_BYTES:
            raise gate.GateError("producer inventory exceeds byte limit")


def expected_evidence_paths(evidence: Path, plan: dict[str, Any], needed: dict[str, bool]) -> dict[str, list[str]]:
    """Derive the exact retained validator paths from producer contents and plan."""
    paths = {
        "plan": ["fwrouter-affected-plan.json"],
        "build": ["build/build-summary.json", "build/wheel-metadata.json"],
        "unit": ["fwrouter-affected-unit.json"],
    }
    if needed.get("unit"):
        for group in gate.ROUTINE_SHARD_GROUPS:
            shard_root = evidence / "unit" / "shards" / group
            parallel_ci.verify_producer(shard_root, "unit", plan)
            files = read_json(shard_root / "producer.json").get("files", {})
            paths["unit"].extend((f"shards/{group}/shard.json", f"shards/{group}/producer.json"))
            if "measurements.json" in files:
                parallel_ci.validate_measurement_receipt(parallel_ci.load_measurement_receipt(shard_root / "measurements.json"),
                                                         plan, "unit", shard_group=group)
                paths["unit"].append(f"shards/{group}/measurements.json")
    wheels = sorted((evidence / "build/build").glob("*.whl"))
    if needed.get("build"):
        if len(wheels) != 1 or wheels[0].is_symlink() or not wheels[0].is_file():
            raise gate.GateError("exactly one safe source-bound wheel is required")
        paths["build"].append(wheels[0].relative_to(evidence / "build").as_posix())
    for role in ("hosted", "provider", "packet"):
        if needed.get(role):
            reports = sorted((evidence / role).rglob("hosted-acceptance-report.json"))
            if len(reports) != 1 or any(p.is_symlink() or not p.is_file() for p in reports):
                raise gate.GateError(f"{role} must contain exactly one safe hosted report")
            paths[role] = [p.relative_to(evidence / role).as_posix() for p in reports] + ["runtime-inputs.json"]
            measurement = evidence / role / "measurements.json"
            if measurement.is_file() and not measurement.is_symlink():
                parallel_ci.validate_measurement_receipt(parallel_ci.load_measurement_receipt(measurement), plan, role)
                paths[role].append("measurements.json")
            elif measurement.exists() or measurement.is_symlink():
                raise gate.GateError(f"{role} worker measurement receipt is unsafe")
    if needed.get("native"):
        native_paths = ["runtime-inputs.json"]
        manifest_rows = {row["path"]: row for row in gate.load_manifest()["test_files"]}
        selected = set(plan.get("selected_files", []))
        needs_qcp = any(path in selected and row.get("execution_profile") == "qualified-child-process"
                        and path != "backend/tests/test_xray_native_readback.py"
                        for path, row in manifest_rows.items())
        needs_xray = "backend/tests/test_xray_native_readback.py" in selected
        profiles = (["qcp"] if needs_qcp else []) + (["xray"] if needs_xray else [])
        for profile_name in profiles:
            profile_root = evidence / "native/profiles" / profile_name
            parallel_ci.verify_producer(profile_root, "native", plan)
            producer = read_json(profile_root / "producer.json", 64 * 1024)
            files = producer.get("files")
            if not isinstance(files, dict) or "runtime-inputs.json" not in files:
                raise gate.GateError(f"native profile {profile_name} has no sealed runtime manifest")
            receipt_name = ("qualified-child-receipt.json" if profile_name == "qcp"
                            else "qualified-xray-docker-receipt.json")
            receipts = sorted(path for path in files if Path(path).name == receipt_name)
            if len(receipts) != 1:
                raise gate.GateError(f"native profile {profile_name} must retain exactly one sealed receipt")
            sibling = str(Path(receipts[0]).with_name("profile.json"))
            expected_files = {"runtime-inputs.json", receipts[0], sibling}
            if "measurements.json" in files:
                parallel_ci.validate_measurement_receipt(parallel_ci.load_measurement_receipt(profile_root / "measurements.json"),
                                                         plan, "native", native_profile=profile_name)
                expected_files.add("measurements.json")
            if set(files) != expected_files:
                raise gate.GateError(
                    f"native profile {profile_name} has missing or unapproved sealed files")
            for relative in sorted(files):
                safe_path(relative)
                candidate = profile_root / relative
                if candidate.is_symlink() or not candidate.is_file():
                    raise gate.GateError(f"native evidence is missing sealed {profile_name} closure: {relative}")
                native_paths.append(f"profiles/{profile_name}/{relative}")
            # The nested envelope itself is part of the combined native producer
            # inventory and must also survive final-bundle projection.
            native_paths.append(f"profiles/{profile_name}/producer.json")
        paths["native"] = native_paths
    return {role: sorted(items) for role, items in paths.items() if needed.get(role)}


def validate_projection_metadata(metadata: dict[str, Any], plan: dict[str, Any],
                                 needed: dict[str, bool], expected_paths: dict[str, list[str]]) -> None:
    projection = metadata.get("evidence_selection")
    if not isinstance(projection, dict) or set(projection) != set(expected_paths):
        raise gate.GateError("evidence projection roles do not match required profiles")
    for role, expected in expected_paths.items():
        actual = projection.get(role)
        if not isinstance(actual, list) or any(not isinstance(path, str) for path in actual):
            raise gate.GateError(f"{role} evidence selection is malformed")
        for path in actual:
            safe_path(path)
        if sorted(actual) != expected or len(actual) != len(set(actual)):
            raise gate.GateError(f"{role} evidence projection omits or adds required evidence")
        if role == "plan" and any("/" in path for path in actual):
            raise gate.GateError(f"{role} projection contains unexpected nested files")


def copy_evidence_file(source: Path, destination: Path) -> None:
    wheel_allowed = source.name.endswith(".whl") and source.parent.name == "build" and destination.parent.name == "build"
    for parent in (*source.parents, *destination.parents):
        if parent.is_symlink():
            raise gate.GateError("symlinked evidence path component")
    if source.is_symlink() or not source.is_file() or (source.name not in SAFE_RECEIPTS and not wheel_allowed):
        raise gate.GateError(f"unsafe or unapproved evidence file: {source.name}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination, follow_symlinks=False)


_DIAGNOSTIC_SECRET_KEY_PARTS = (
    "authorization", "password", "passwd", "token", "secret", "credential",
    "apikey", "privatekey", "accesskey", "cookie",
)


def project_diagnostic_value(value: Any) -> Any:
    """Return a redacted copy suitable for short-lived diagnostics only.

    This must never be used as validator input: strict evidence is copied and
    replayed through the original, hash-sealed receipts on successful runs.
    """
    if isinstance(value, dict):
        projected: dict[str, Any] = {}
        for key, child in value.items():
            safe_key = gate.redact_failure_output(str(key))
            normalized_key = "".join(char for char in str(key).lower() if char.isalnum())
            if any(part in normalized_key for part in _DIAGNOSTIC_SECRET_KEY_PARTS):
                projected[safe_key] = "[REDACTED]"
            else:
                projected[safe_key] = project_diagnostic_value(child)
        return projected
    if isinstance(value, list):
        return [project_diagnostic_value(child) for child in value]
    if isinstance(value, str):
        projected = gate.redact_failure_output(value)
        # Redact prose as well as KEY=value forms. Failure messages often
        # concatenate a credential label and value with whitespace.
        projected = re.sub(
            r"(?i)\b(?:authorization|password|passwd|token|secret|credential|api[_-]?key|"
            r"client[_-]?secret|private[_-]?key|access[_-]?key|cookie)\b(\s*[:=]\s*|\s+)"
            r"[^\s,;\"']+",
            r"\1[REDACTED]", projected,
        )
        return projected
    return value


def copy_diagnostic_projection(source: Path, destination: Path, limit: int) -> None:
    """Persist a bounded redacted projection, never the original receipt bytes."""
    value = read_json(source, limit)
    projection = project_diagnostic_value(value)
    if not isinstance(projection, dict):
        raise gate.GateError("diagnostic receipt projection must be an object")
    projection["_diagnostic_projection"] = {
        "redacted": True,
        "qualification_evidence": False,
        "source_receipt_validated": False,
    }
    gate.atomic_json(destination, projection)


def source_digests(destination: Path) -> list[dict[str, Any]]:
    result = []
    for relative in SOURCE_FILES:
        source = ROOT / relative
        if source.is_symlink() or not source.is_file():
            raise gate.GateError(f"required offline verifier source is missing: {relative}")
        target = destination / "verifier-source" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target, follow_symlinks=False)
        result.append({"path": relative, "bytes": source.stat().st_size, "sha256": sha256(source)})
    return result


def handoff(args: argparse.Namespace) -> dict[str, Any]:
    """Project only cross-job validator inputs; leave worker logs/state on its runner."""
    if args.role not in parallel_ci.ROLES or args.outcome not in {"success", "failure", "cancelled", "skipped"}:
        raise gate.GateError("invalid handoff role or outcome")
    if args.destination.exists() or args.destination.is_symlink():
        raise gate.GateError("handoff destination must be a fresh path")
    plan = read_json(args.plan, 8 * 1024 * 1024)
    gate.ensure_plan(plan, gate.load_manifest(), allow_l6=getattr(args, "l6", False))
    if getattr(args, "l6", False) and plan.get("full_suite_required") is not True:
        raise gate.GateError("L6 handoff requires the full immutable suite")
    source = args.source
    if source.is_symlink():
        raise gate.GateError("handoff source must not be a symlink")
    required = parallel_ci.requirements(plan, main_smoke_for(args.event, args.ref))
    if args.outcome == "success" and not required.get(args.role, False):
        raise gate.GateError("successful handoff is not selected by the immutable plan")

    if args.role == "plan":
        candidates = ["fwrouter-affected-plan.json"]
    elif args.role == "unit":
        group = args.shard_group
        if group not in gate.ROUTINE_SHARD_GROUPS:
            raise gate.GateError("unit evidence handoff requires a supported --shard-group")
        candidates = ["shard.json"]
        measurement = source / "measurements.json"
        if measurement.exists() or measurement.is_symlink():
            if measurement.is_symlink() or not measurement.is_file():
                raise gate.GateError("unit worker measurement receipt is unsafe")
            parallel_ci.validate_measurement_receipt(parallel_ci.load_measurement_receipt(measurement), plan, "unit",
                                                     shard_group=group)
            candidates.append("measurements.json")
        if args.outcome == "success":
            report = read_json(source / "shard.json")
            rows = routine_shards.expected_by_group(plan, gate.load_manifest())[group]
            run_id = os.environ.get("GITHUB_RUN_ID", "")
            attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "")
            routine_shards.verify_shard(report, group, plan, rows, run_id=run_id, attempt=attempt)
    elif args.role == "build":
        wheels = sorted((source / "build").glob("*.whl")) if source.is_dir() else []
        candidates = ["build/build-summary.json", "build/wheel-metadata.json"]
        candidates.extend(p.relative_to(source).as_posix() for p in wheels)
    elif args.role in {"hosted", "provider", "packet"}:
        candidates = ["runtime-inputs.json"]
        reports = sorted(source.rglob("hosted-acceptance-report.json")) if source.is_dir() else []
        candidates.extend(p.relative_to(source).as_posix() for p in reports)
        if args.outcome == "success" and len(reports) != 1:
            raise gate.GateError(f"{args.role} success must supply exactly one strict hosted report")
        measurement = source / "measurements.json"
        if measurement.exists() or measurement.is_symlink():
            if measurement.is_symlink() or not measurement.is_file():
                raise gate.GateError(f"{args.role} worker measurement receipt is unsafe")
            parallel_ci.validate_measurement_receipt(parallel_ci.load_measurement_receipt(measurement), plan, args.role)
            candidates.append("measurements.json")
    else:
        candidates = ["runtime-inputs.json"]
        selected = set(plan.get("selected_files", []))
        rows = {row["path"]: row for row in gate.load_manifest()["test_files"]}
        qcp_selected = any(path in selected and row.get("execution_profile") == "qualified-child-process"
                           and path != "backend/tests/test_xray_native_readback.py" for path, row in rows.items())
        xray_selected = "backend/tests/test_xray_native_readback.py" in selected
        native_profile = getattr(args, "native_profile", None)
        if native_profile not in {None, "qcp", "xray"}:
            raise gate.GateError("unsupported native profile handoff")
        if native_profile == "qcp" and not qcp_selected or native_profile == "xray" and not xray_selected:
            raise gate.GateError("native profile handoff is not selected by the immutable plan")
        want_qcp = qcp_selected and native_profile in {None, "qcp"}
        want_xray = xray_selected and native_profile in {None, "xray"}
        if want_qcp:
            found = sorted(source.rglob("qualified-child-receipt.json")) if source.is_dir() else []
            if args.outcome == "success" and len(found) != 1:
                raise gate.GateError("native success requires exactly one qualified-child receipt")
            candidates.extend(p.relative_to(source).as_posix() for p in found)
            for receipt in found:
                profile = receipt.with_name("profile.json")
                if args.outcome == "success" and (profile.is_symlink() or not profile.is_file()):
                    raise gate.GateError("native success requires the qualified-child sibling profile.json")
                if profile.is_file() and not profile.is_symlink():
                    candidates.append(profile.relative_to(source).as_posix())
        if want_xray:
            found = sorted(source.rglob("qualified-xray-docker-receipt.json")) if source.is_dir() else []
            if args.outcome == "success" and len(found) != 1:
                raise gate.GateError("native success requires exactly one Docker-Xray receipt")
            candidates.extend(p.relative_to(source).as_posix() for p in found)
            for receipt in found:
                profile = receipt.with_name("profile.json")
                if args.outcome == "success" and (profile.is_symlink() or not profile.is_file()):
                    raise gate.GateError("native success requires the Docker-Xray sibling profile.json")
                if profile.is_file() and not profile.is_symlink():
                    candidates.append(profile.relative_to(source).as_posix())
        measurement = source / "measurements.json"
        if measurement.exists() or measurement.is_symlink():
            if measurement.is_symlink() or not measurement.is_file():
                raise gate.GateError("native worker measurement receipt is unsafe")
            parallel_ci.validate_measurement_receipt(parallel_ci.load_measurement_receipt(measurement), plan, "native",
                                                     native_profile=native_profile)
            candidates.append("measurements.json")

    if args.destination.is_symlink():
        raise gate.GateError("handoff destination is unsafe")
    args.destination.mkdir(mode=0o700, parents=True)
    copied = []
    try:
        for relative in sorted(set(candidates)):
            candidate = source / relative
            if candidate.is_symlink():
                raise gate.GateError("symlink in validator handoff input")
            if not candidate.exists():
                if args.outcome == "success":
                    raise gate.GateError(f"successful {args.role} handoff is missing {relative}")
                continue
            safe_path(relative)
            copy_evidence_file(candidate, args.destination / relative)
            copied.append(relative)
        if args.outcome == "success":
            if args.role in {"hosted", "provider", "packet", "native"}:
                strict.validate_runtime_inputs(read_json(args.destination / "runtime-inputs.json"))
            if args.role == "build":
                with tempfile.TemporaryDirectory(prefix="fwrouter-handoff-build-") as scratch:
                    copied_build = Path(scratch) / "build"
                    shutil.copytree(args.destination, copied_build)
                    ci_inputs.verify(copied_build, args.plan)
        producer = parallel_ci.seal(args.destination, args.role, plan, args.outcome)
        return {"role": args.role, "shard_group": getattr(args, "shard_group", None),
                "native_profile": getattr(args, "native_profile", None),
                "outcome": args.outcome, "copied": copied,
                "producer_digest": producer["producer_digest"]}
    except BaseException:
        shutil.rmtree(args.destination, ignore_errors=True)
        raise


def build_bundle(args: argparse.Namespace) -> dict[str, Any]:
    run_id, attempt = str(args.run_id), str(args.attempt)
    if not run_id.isdigit() or int(run_id) <= 0 or not attempt.isdigit() or int(attempt) <= 0:
        raise gate.GateError("positive workflow run ID and attempt are required")
    workflow_kind = getattr(args, 'workflow_kind', 'main')
    prepr = workflow_kind == 'prepr'
    if args.event not in ({"workflow_dispatch"} if prepr else {"push", "pull_request"}):
        raise gate.GateError("event does not match this CI evidence profile")
    if prepr and (not args.ref.startswith('refs/heads/') or args.ref == 'refs/heads/main'):
        raise gate.GateError('pre-PR evidence is restricted to a non-main branch dispatch')
    if args.event == "push" and args.ref != "refs/heads/main":
        raise gate.GateError("push evidence bundle is restricted to refs/heads/main")
    upstream = read_json(args.jobs)
    plan, report = check_eligibility(args.evidence, args.required, event=args.event, ref=args.ref,
                                     run_id=run_id, attempt=attempt, upstream=upstream,
                                     require_prepr_scope=prepr)
    main_smoke = main_smoke_for(args.event, args.ref)
    needed = parallel_ci.requirements(plan, main_smoke)
    os.environ["GITHUB_RUN_ID"] = run_id
    os.environ["GITHUB_RUN_ATTEMPT"] = attempt
    os.environ["FWROUTER_CI_PLAN_DIGEST"] = plan["plan_digest"]
    if args.destination.exists() or args.destination.is_symlink():
        raise gate.GateError("bundle destination must be a fresh path")
    args.destination.mkdir(mode=0o700, parents=True)
    try:
        selection = expected_evidence_paths(args.evidence, plan, needed)
        for role, paths in selection.items():
            if not needed[role]:
                continue
            producer_root = args.evidence / role
            # Strict aggregation already verified complete inventories; re-check it before
            # selecting a non-log evidence projection for longer retention.
            parallel_ci.verify_producer(producer_root, role, plan)
            verify_producer_projection(producer_root, role, plan, run_id=run_id,
                                       attempt=attempt, include_paths=paths)
            for relative in paths:
                copy_evidence_file(producer_root / relative, args.destination / "evidence" / role / relative)
            copy_evidence_file(producer_root / "producer.json",
                               args.destination / "evidence" / role / "producer.json")
        with tempfile.TemporaryDirectory(prefix="fwrouter-bundle-build-verify-") as scratch:
            copied_build = Path(scratch) / "build"
            shutil.copytree(args.evidence / "build", copied_build)
            ci_inputs.verify(copied_build, args.evidence / "plan/fwrouter-affected-plan.json")
        for role in ("hosted", "provider", "packet", "native"):
            if needed[role]:
                strict.validate_runtime_inputs(strict.read_json(args.evidence / role / "runtime-inputs.json"))
        replay = replay_strict(args.evidence, plan, main_smoke=main_smoke)
        saved_aggregate = read_json(args.required / "fwrouter-aggregate.json")
        if gate.canonical_digest(replay) != gate.canonical_digest(saved_aggregate) or replay.get("eligible") is not True:
            raise gate.GateError("strict aggregate replay differs from the required report")
        copy_evidence_file(args.required / "fwrouter-aggregate.json",
                           args.destination / "reports/fwrouter-aggregate.json")
        copy_evidence_file(args.required / "required-result.json",
                           args.destination / "reports/required-result.json")
        import visibility_report
        visibility = visibility_report.collect(args.evidence, args.required, plan)
        saved_visibility = read_json(args.required / "visibility.json")
        if (gate.canonical_digest(visibility) != gate.canonical_digest(saved_visibility)
                or visibility.get("qualification_status") != "passed"
                or visibility.get("not_run_selected_files")
                or visibility.get("diagnostic_limitations")):
            raise gate.GateError("aggregate visibility report is missing, incomplete, or stale")
        copy_evidence_file(args.required / "visibility.json",
                           args.destination / "reports/visibility.json")
        if read_json(args.required / "required-result.json").get("safe_recovery_required") is True:
            for relative in ("qualification.json", "stage.json", "artifact/manifest.json", "artifact/source.tar"):
                copy_evidence_file(args.required / "safe-recovery" / relative,
                                   args.destination / "reports/safe-recovery" / relative)
        verifier_sources = source_digests(args.destination)
        recovery_required = (set(("installer/tests/test_safe_recovery.py", "backend/tests/test_network_contract.py",
                                  "backend/tests/test_dnsmasq.py", "tests/application_acceptance/test_packet_dataplane.py"))
                            <= set(plan["selected_files"]))
        metadata = {
            "schema": SCHEMA, "source_sha": plan["source_commit"],
            "repository": REPOSITORY, "workflow": WORKFLOW_NAME,
            "workflow_path": ".github/workflows/test-gates.yml",
            "required_check": PREPR_REQUIRED_CHECK if prepr else REQUIRED_CHECK,
            "event": args.event, "ref": args.ref, "run_id": run_id, "run_attempt": attempt,
            **({"qualification_scope": parallel_ci.PREPR_SCOPE, "deploy_eligibility": False} if prepr else {}),
            "plan_digest": plan["plan_digest"], "manifest_digest": plan["manifest_digest"],
            "manifest_version": plan["manifest_version"], "strict_aggregate": "passed",
            "main_smoke_required": main_smoke, "jobs": upstream,
            "safe_recovery_required": recovery_required,
            "aggregate_digest": gate.canonical_digest(report),
            "evidence_selection": {role: paths for role, paths in selection.items() if needed.get(role)},
            "producer_payload_omissions": "Only raw command logs, packet captures, profiles, disposable state and other non-validator payloads are omitted; original sealed producer inventories and every validator input are retained.",
            "verifier_sources": verifier_sources,
            "verification_limit": "The archive is integrity-checked, not cryptographically signed. Verify with a trusted FWRouter checkout at source_sha and trusted GitHub run metadata.",
        }
        gate.atomic_json(args.destination / "metadata.json", metadata)
        files = inventory(args.destination)
        gate.atomic_json(args.destination / "content-manifest.json", {
            "schema": "fwrouter-ci-evidence-content/v1", "metadata_sha256": sha256(args.destination / "metadata.json"),
            "file_count": len(files), "total_bytes": sum(row["bytes"] for row in files), "files": files,
        })
        verify_bundle(argparse.Namespace(bundle=args.destination, expected_sha=plan["source_commit"],
                                         event=args.event, ref=args.ref, run_id=run_id, attempt=attempt,
                                         require_main=False, workflow_kind=workflow_kind))
        return metadata
    except BaseException:
        # A failed publication build is retained by the failure-diagnostic path only.
        raise


def verify_content(root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    metadata = read_json(root / "metadata.json")
    manifest = read_json(root / "content-manifest.json", 32 * 1024 * 1024)
    if metadata.get("schema") != SCHEMA or manifest.get("schema") != "fwrouter-ci-evidence-content/v1":
        raise gate.GateError("unsupported evidence bundle schema")
    actual = inventory(root)
    if (actual != manifest.get("files") or manifest.get("file_count") != len(actual)
            or manifest.get("total_bytes") != sum(row["bytes"] for row in actual)
            or manifest.get("metadata_sha256") != sha256(root / "metadata.json")):
        raise gate.GateError("bundle content is missing, extra, unsafe, or modified")
    return metadata, manifest


def verify_bundle(args: argparse.Namespace) -> dict[str, Any]:
    workflow_kind = getattr(args, 'workflow_kind', 'main')
    prepr = workflow_kind == 'prepr'
    metadata, _manifest = verify_content(args.bundle)
    expected = {
        "source_sha": args.expected_sha,
        "run_id": str(args.run_id), "run_attempt": str(args.attempt),
        "event": args.event, "ref": args.ref, "repository": REPOSITORY,
        "workflow": WORKFLOW_NAME, "workflow_path": ".github/workflows/test-gates.yml",
        "required_check": PREPR_REQUIRED_CHECK if prepr else REQUIRED_CHECK,
    }
    if any(metadata.get(key) != value for key, value in expected.items()):
        raise gate.GateError("bundle source/run/attempt differs from the independently supplied expectation")
    if metadata.get("strict_aggregate") != "passed":
        raise gate.GateError("bundle has no strict aggregate PASS")
    if args.require_main and not (args.event == "push" and args.ref == "refs/heads/main"):
        raise gate.GateError("deployment verification requires an actual main push evidence bundle")
    if prepr:
        if (args.event != 'workflow_dispatch' or not args.ref.startswith('refs/heads/')
                or args.ref == 'refs/heads/main'
                or metadata.get('qualification_scope') != parallel_ci.PREPR_SCOPE
                or metadata.get('deploy_eligibility') is not False
                or args.require_main):
            raise gate.GateError('pre-PR evidence cannot be promoted to PR/main/deployment qualification')
    else:
        if (args.event not in {"push", "pull_request"}
                or (args.event == "push" and args.ref != "refs/heads/main")):
            raise gate.GateError("bundle event/ref is outside the supported workflow contract")
        if args.event == "pull_request" and not args.ref.startswith("refs/pull/"):
            raise gate.GateError("pull-request bundle does not identify a GitHub PR merge ref")
        if metadata.get('deploy_eligibility') is not None:
            raise gate.GateError('PR/main bundle qualification metadata is invalid')
    if metadata.get("main_smoke_required") != main_smoke_for(args.event, args.ref):
        raise gate.GateError("main smoke policy does not match the trusted event/ref")
    if not (ROOT / ".git").exists() or gate.git("rev-parse", "HEAD") != metadata["source_sha"]:
        raise gate.GateError("trusted verification checkout is not at the recorded source SHA")
    status = gate.git("status", "--porcelain", "--untracked-files=all")
    if status:
        raise gate.GateError("trusted verification checkout is not clean")
    source_rows = metadata.get("verifier_sources", [])
    if (not isinstance(source_rows, list)
            or [row.get("path") for row in source_rows if isinstance(row, dict)] != list(SOURCE_FILES)):
        raise gate.GateError("bundle verifier source inventory is missing, duplicated, or reordered")
    local_sources = []
    for row in source_rows:
        path = row.get("path") if isinstance(row, dict) else None
        if not isinstance(path, str):
            raise gate.GateError("verifier source inventory is malformed")
        safe_path(path)
        local = ROOT / path
        packaged = args.bundle / "verifier-source" / path
        if (local.is_symlink() or not local.is_file() or packaged.is_symlink() or not packaged.is_file()
                or row != {"path": path, "bytes": local.stat().st_size, "sha256": sha256(local)}
                or sha256(packaged) != row["sha256"]):
            raise gate.GateError(f"trusted verifier source differs from bundled source digest: {path}")
        local_sources.append(path)
    if set(local_sources) != set(SOURCE_FILES):
        raise gate.GateError("bundle lacks the complete offline verifier source inventory")
    evidence = args.bundle / "evidence"
    plan_path = evidence / "plan/fwrouter-affected-plan.json"
    plan = read_json(plan_path, 8 * 1024 * 1024)
    gate.ensure_plan(plan, gate.load_manifest())
    if (plan.get("source_commit") != metadata["source_sha"]
            or plan.get("plan_digest") != metadata.get("plan_digest")
            or plan.get("manifest_digest") != metadata.get("manifest_digest")
            or plan.get("manifest_version") != metadata.get("manifest_version")):
        raise gate.GateError("bundled plan identity differs from bundle metadata")
    expected_scope = parallel_ci.qualification_scope(mode_for_plan(args.event, args.ref, plan))
    if metadata.get('qualification_scope') not in (None, expected_scope):
        raise gate.GateError('bundle qualification scope does not match the immutable plan and event')
    os.environ["GITHUB_RUN_ID"] = metadata["run_id"]
    os.environ["GITHUB_RUN_ATTEMPT"] = metadata["run_attempt"]
    os.environ["FWROUTER_CI_PLAN_DIGEST"] = plan["plan_digest"]
    if prepr and plan.get('mandatory_suite_required') is not True:
        raise gate.GateError('pre-PR bundle must contain the full safe mandatory plan')
    needed = parallel_ci.requirements(plan, main_smoke_for(args.event, args.ref))
    bundled_required = read_json(args.bundle / 'reports/required-result.json')
    bundled_scope = bundled_required.get('qualification_scope')
    if bundled_scope is not None and bundled_scope != expected_scope:
        raise gate.GateError('offline required-result scope does not match the immutable plan and event')
    if prepr and bundled_scope != parallel_ci.PREPR_SCOPE:
        raise gate.GateError('offline required-result does not bind this archive to pre-PR non-promotional scope')
    expected_paths = expected_evidence_paths(evidence, plan, needed)
    validate_projection_metadata(metadata, plan, needed, expected_paths)
    expected_bundle_paths = {
        "metadata.json", "content-manifest.json",
        "reports/fwrouter-aggregate.json", "reports/required-result.json",
        "reports/visibility.json",
    }
    expected_bundle_paths.update(f"verifier-source/{path}" for path in SOURCE_FILES)
    for role, paths in expected_paths.items():
        expected_bundle_paths.update(f"evidence/{role}/{path}" for path in paths)
        expected_bundle_paths.add(f"evidence/{role}/producer.json")
    if metadata.get("safe_recovery_required") is True:
        expected_bundle_paths.update({
            "reports/safe-recovery/qualification.json", "reports/safe-recovery/stage.json",
            "reports/safe-recovery/artifact/manifest.json", "reports/safe-recovery/artifact/source.tar",
        })
    actual_bundle_paths = {row["path"] for row in _manifest["files"]} | {"content-manifest.json"}
    if actual_bundle_paths != expected_bundle_paths:
        raise gate.GateError("bundle contains omitted or unexpected files for the immutable evidence selection")
    jobs = metadata.get("jobs")
    if not isinstance(jobs, dict) or parallel_ci.validate_results(jobs, needed):
        raise gate.GateError("bundled job result set is incomplete or has failed/skipped evidence")
    import visibility_report
    visibility = visibility_report.collect(evidence, args.bundle / "reports", plan)
    saved_visibility = read_json(args.bundle / "reports/visibility.json")
    if (gate.canonical_digest(visibility) != gate.canonical_digest(saved_visibility)
            or visibility.get("qualification_status") != "passed"
            or visibility.get("not_run_selected_files")
            or visibility.get("diagnostic_limitations")):
        raise gate.GateError("bundle visibility report is missing, stale, or incomplete")
    for role, required in needed.items():
        if not required:
            continue
        producer_root = evidence / role
        producer = read_json(producer_root / "producer.json", 64 * 1024)
        strict.verify_digest(producer, "producer_digest", f"{role} producer")
        expected = {
            "schema": "fwrouter-parallel-producer/v1", "role": role, "outcome": "success",
            "source_commit": metadata["source_sha"], "plan_digest": metadata["plan_digest"],
            "manifest_digest": metadata["manifest_digest"], "manifest_version": metadata["manifest_version"],
            "run_id": metadata["run_id"], "run_attempt": metadata["run_attempt"],
        }
        if any(producer.get(key) != value for key, value in expected.items()):
            raise gate.GateError(f"{role} producer identity/status does not match metadata")
        files = producer.get("files")
        if not isinstance(files, dict):
            raise gate.GateError(f"{role} producer inventory is malformed")
        validate_producer_inventory(files)
        for relative in expected_paths[role]:
            row = files.get(relative)
            included = producer_root / relative
            if (not isinstance(row, dict) or included.is_symlink() or not included.is_file()
                    or row != {"bytes": included.stat().st_size, "sha256": sha256(included)}):
                raise gate.GateError(f"{role} selected evidence fails producer digest")
    with tempfile.TemporaryDirectory(prefix="fwrouter-bundle-verify-") as scratch:
        copied_build = Path(scratch) / "build"
        shutil.copytree(evidence / "build", copied_build)
        ci_inputs.verify(copied_build, plan_path)
    replay = replay_strict(evidence, plan, main_smoke=metadata.get("main_smoke_required") is True)
    saved = read_json(args.bundle / "reports/fwrouter-aggregate.json")
    if (gate.canonical_digest(replay) != gate.canonical_digest(saved) or replay.get("eligible") is not True
            or gate.canonical_digest(saved) != metadata.get("aggregate_digest")):
        raise gate.GateError("offline strict aggregate replay does not match the retained PASS report")
    required_result = read_json(args.bundle / "reports/required-result.json")
    derived_recovery = (set(("installer/tests/test_safe_recovery.py", "backend/tests/test_network_contract.py",
                             "backend/tests/test_dnsmasq.py", "tests/application_acceptance/test_packet_dataplane.py"))
                        <= set(plan["selected_files"]))
    if (required_result.get("eligible") is not True
            or required_result.get("source_commit") != metadata["source_sha"]
            or required_result.get("plan_digest") != metadata["plan_digest"]
            or required_result.get("run_id") != metadata["run_id"]
            or required_result.get("run_attempt") != metadata["run_attempt"]
            or required_result.get("manifest_digest") != metadata["manifest_digest"]
            or required_result.get("manifest_version") != metadata["manifest_version"]
            or required_result.get("upstream") != jobs
            or required_result.get("safe_recovery_required") is not derived_recovery
            or metadata.get("safe_recovery_required") is not derived_recovery):
        raise gate.GateError("retained strict required-result identity/status is invalid")
    if required_result.get("safe_recovery_required"):
        import importlib.util
        spec = importlib.util.spec_from_file_location("fwrouter_bundle_safe_recovery", ROOT / "installer/safe_recovery.py")
        if spec is None or spec.loader is None:
            raise gate.GateError("safe-recovery verifier is unavailable")
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        recovery = args.bundle / "reports/safe-recovery"
        module.verify(recovery / "artifact", recovery / "qualification.json")
        stage = read_json(recovery / "stage.json")
        if stage.get("status") != "isolated_stage_complete" or stage.get("source_sha") != metadata["source_sha"]:
            raise gate.GateError("safe-recovery stage evidence failed offline verification")
    return {"status": "evidence_verified_not_deploy_authorized", "source_sha": metadata["source_sha"],
            "run_id": metadata["run_id"], "run_attempt": metadata["run_attempt"],
            "strict_aggregate": "passed", "offline_source_checkout": "verified"}


def replay_strict(evidence: Path, plan: dict[str, Any], *, main_smoke: bool) -> dict[str, Any]:
    verify_routine_shards(evidence, plan)
    plan_path = evidence / "plan/fwrouter-affected-plan.json"
    unit_path = evidence / "unit/fwrouter-affected-unit.json"
    needed = parallel_ci.requirements(plan, main_smoke)
    if needed["native"]:
        verify_native_profiles(evidence, plan)
    hosted: list[Path] = []
    for role in ("hosted", "provider", "packet"):
        if needed[role]:
            hosted.extend((evidence / role).rglob("hosted-acceptance-report.json"))
    qcp = list((evidence / "native").rglob("qualified-child-receipt.json")) if needed["native"] else []
    xray = list((evidence / "native").rglob("qualified-xray-docker-receipt.json")) if needed["native"] else []
    runtime_files = [evidence / role / "runtime-inputs.json"
                     for role in ("hosted", "provider", "packet", "native") if needed[role]]
    for runtime_file in runtime_files:
        strict.validate_runtime_inputs(strict.read_json(runtime_file))
    runtime_path = runtime_files[0] if runtime_files else None
    smoke = None
    if main_smoke:
        suite = "functional-ordinary" if parallel_ci.requirements(plan)["provider"] else "xray-diagnostic"
        candidates = [p for p in (evidence / "hosted").rglob("hosted-acceptance-report.json")
                      if strict.read_json(p).get("suite") == suite]
        if len(candidates) != 1:
            raise gate.GateError("offline main smoke report is missing or duplicated")
        smoke = candidates[0]
        if suite == "xray-diagnostic":
            hosted = [p for p in hosted if p != smoke]
    return strict.aggregate(plan_path, unit_path, hosted, qcp, xray, minimum_repeats=1,
                            runtime_inputs_path=runtime_path, require_main_smoke=main_smoke,
                            main_smoke_path=smoke)


def verify_native_profiles(evidence: Path, plan: dict[str, Any]) -> None:
    """Replay every profile producer seal and prove its runtime pins agree."""
    profiles = parallel_ci.native_profiles(plan)
    if not profiles:
        raise gate.GateError("native profile evidence is required but no profile is plan-selected")
    manifests = []
    for profile in profiles:
        root = evidence / "native/profiles" / profile
        parallel_ci.verify_producer(root, "native", plan)
        runtime = read_json(root / "runtime-inputs.json")
        strict.validate_runtime_inputs(runtime)
        manifests.append(runtime)
    if any(parallel_ci.runtime_identity(value) != parallel_ci.runtime_identity(manifests[0])
           for value in manifests[1:]):
        raise gate.GateError("offline native profile runners disagree on pinned runtime inputs")
    combined = read_json(evidence / "native/runtime-inputs.json")
    strict.validate_runtime_inputs(combined)
    if parallel_ci.runtime_identity(combined) != parallel_ci.runtime_identity(manifests[0]):
        raise gate.GateError("combined native runtime input differs from profile-bound manifests")


def verify_routine_shards(evidence: Path, plan: dict[str, Any]) -> None:
    """Re-validate every matrix producer and prove its reports merge to the strict unit input."""
    manifest = gate.load_manifest()
    reports = {}
    for group in gate.ROUTINE_SHARD_GROUPS:
        root = evidence / "unit/shards" / group
        parallel_ci.verify_producer(root, "unit", plan)
        report = read_json(root / "shard.json")
        routine_shards.verify_shard(
            report, group, plan, routine_shards.expected_by_group(plan, manifest)[group],
            run_id=os.environ["GITHUB_RUN_ID"], attempt=os.environ["GITHUB_RUN_ATTEMPT"],
        )
        reports[group] = report
    merged = routine_shards.merge_reports(
        plan, manifest, reports, run_id=os.environ["GITHUB_RUN_ID"],
        attempt=os.environ["GITHUB_RUN_ATTEMPT"],
    )
    saved = read_json(evidence / "unit/fwrouter-affected-unit.json")
    if gate.canonical_digest(merged) != gate.canonical_digest(saved):
        raise gate.GateError("offline routine-shard replay differs from the strict aggregate unit input")


def diagnostics(args: argparse.Namespace) -> dict[str, Any]:
    """Create a small fail/incomplete report without claiming successful qualification."""
    if args.destination.exists() or args.destination.is_symlink():
        raise gate.GateError("diagnostic destination must be a fresh path")
    args.destination.mkdir(mode=0o700, parents=True)
    payload: dict[str, Any] = {
        "schema": "fwrouter-ci-diagnostic/v1", "status": "failed_or_incomplete",
        "workflow": "FWRouter L6 full regression" if getattr(args, "l6", False) else "FWRouter affected CI", "event": args.event, "ref": args.ref,
        "run_id": str(args.run_id), "run_attempt": str(args.attempt),
        "source_sha": args.source_sha if args.source_sha else None,
        "reason": "strict qualification or final evidence publication did not complete",
        "receipt_policy": "allowlisted redacted diagnostic projections only; not original validated receipts or qualification evidence",
        "evidence_included": [],
        "raw_logs_packet_captures_profiles_and_disposable_state": "omitted",
    }
    for source, relative in (
        (args.required / "fwrouter-aggregate.json", "reports/fwrouter-aggregate.diagnostic-projection.json"),
        (args.required / "required-result.json", "reports/required-result.diagnostic-projection.json"),
    ):
        if source.is_file() and not source.is_symlink() and source.stat().st_size <= 256 * 1024:
            copy_diagnostic_projection(source, args.destination / relative, 256 * 1024)
            payload["evidence_included"].append(relative)
    if args.jobs.is_file() and not args.jobs.is_symlink() and args.jobs.stat().st_size <= 256 * 1024:
        jobs = read_json(args.jobs, 256 * 1024)
        payload["jobs"] = project_diagnostic_value({name: row.get("result") for name, row in jobs.items()
                                                     if name in parallel_ci.ROLES and isinstance(row, dict)})
    safe_names = SAFE_RECEIPTS - {"source.tar", "manifest.json"}
    if args.evidence.is_dir() and not args.evidence.is_symlink():
        for role in parallel_ci.ROLES:
            allowed_roots = [args.evidence / role,
                             args.evidence / f"fwrouter-v2-{role}-{args.run_id}-{args.attempt}"]
            if role == "unit":
                allowed_roots.extend(args.evidence / f"fwrouter-v2-unit-{group}-{args.run_id}-{args.attempt}"
                                     for group in gate.ROUTINE_SHARD_GROUPS)
            elif role == "native":
                allowed_roots.extend(args.evidence / f"fwrouter-v2-native-{profile}-{args.run_id}-{args.attempt}"
                                     for profile in ("qcp", "xray"))
            if getattr(args, "l6", False):
                import l6_ci
                allowed_roots.extend(args.evidence / child for child in l6_ci.expected_children()[role])
                allowed_roots.extend(args.evidence / f"fwrouter-l6-{child}-{args.run_id}-{args.attempt}"
                                     for child in l6_ci.expected_children()[role])
            for producer_root in allowed_roots:
                if producer_root.is_symlink() or not producer_root.is_dir():
                    continue
                for source in sorted(producer_root.rglob("*.json")):
                    if source.is_symlink() or source.name not in safe_names or source.stat().st_size > 512 * 1024:
                        continue
                    relative = source.relative_to(producer_root)
                    try:
                        safe_path(relative.as_posix())
                        read_json(source, 512 * 1024)
                    except (OSError, ValueError, gate.GateError):
                        continue
                    # Producer envelopes contain identity and file digests only; exclude
                    # raw test output and test-provided profile/state payloads.
                    target = args.destination / "receipts" / producer_root.name / f"{relative}.diagnostic-projection.json"
                    copy_diagnostic_projection(source, target, 512 * 1024)
                    payload["evidence_included"].append(target.relative_to(args.destination).as_posix())
    projected_payload = project_diagnostic_value(payload)
    gate.atomic_json(args.destination / "diagnostic.json", projected_payload)
    return projected_payload


def _summary_json(path: Path | None) -> dict[str, Any] | None:
    if path is None or path.is_symlink() or not path.is_file():
        return None
    try:
        value = read_json(path)
    except (OSError, ValueError, gate.GateError):
        return None
    return value if isinstance(value, dict) else None


def _record_digest_valid(value: dict[str, Any], field: str) -> bool:
    digest = value.get(field)
    unsigned = dict(value)
    unsigned.pop(field, None)
    return isinstance(digest, str) and digest == gate.canonical_digest(unsigned)


def _summary_profile(event: str, ref: str, workflow_kind: str) -> str:
    if workflow_kind == 'prepr':
        return 'Pre-PR full safe qualification; non-promotional'
    if workflow_kind == 'l6':
        return 'L6 main regression' if ref == 'refs/heads/main' else 'L6 non-promotional branch regression'
    if event == 'pull_request':
        return 'PR required qualification'
    if event == 'push' and ref == 'refs/heads/main':
        return 'main required qualification'
    if event == 'push' and ref.startswith('refs/heads/'):
        return 'branch push qualification'
    return 'profile unavailable'


def _summary_context_valid(event: str, ref: str, workflow_kind: str) -> bool:
    if workflow_kind == 'prepr':
        return event == 'workflow_dispatch' and ref.startswith('refs/heads/') and ref != 'refs/heads/main'
    if workflow_kind == 'l6':
        return (event in {'schedule', 'workflow_dispatch'} and ref.startswith('refs/heads/')
                and (event != 'schedule' or ref == 'refs/heads/main'))
    return ((event == 'pull_request' and ref.startswith('refs/pull/'))
            or (event == 'push' and ref == 'refs/heads/main'))


def _safe_summary_text(value: Any, limit: int = 240) -> str:
    redacted = gate.redact_failure_output(str(value)).replace('\n', ' ').replace('\r', ' ')
    return html.escape(redacted[:limit], quote=True).replace('|', '&#124;').replace('`', '&#96;')


def summary(args: argparse.Namespace) -> int:
    """Write compact, fail-closed human visibility for a completed workflow job."""
    outcomes = {"aggregate": getattr(args, 'aggregate_outcome', 'unknown'),
                "bundle": getattr(args, 'bundle_outcome', 'unknown'),
                "publication": getattr(args, 'publish_outcome', 'unknown')}
    run_id, attempt = str(getattr(args, 'run_id', '')), str(getattr(args, 'attempt', ''))
    expected_sha = str(getattr(args, 'expected_sha', '') or '')
    event, ref = str(getattr(args, 'event', '') or ''), str(getattr(args, 'ref', '') or '')
    workflow_kind = getattr(args, 'workflow_kind', 'main')
    manifest = gate.load_manifest()
    jobs = _summary_json(getattr(args, 'jobs', None))
    plan_path = getattr(args, 'plan', None)
    plan = _summary_json(plan_path)
    plan_valid = False
    if plan is not None and expected_sha and run_id.isdigit() and attempt.isdigit():
        try:
            gate.ensure_plan(plan, manifest, allow_l6=workflow_kind == 'l6')
            plan_valid = plan.get('source_commit') == expected_sha
        except (gate.GateError, TypeError, ValueError):
            plan_valid = False
    context_valid = bool(plan_valid and len(expected_sha) == 40
                         and all(char in '0123456789abcdef' for char in expected_sha)
                         and run_id.isdigit() and attempt.isdigit()
                         and _summary_context_valid(event, ref, workflow_kind))
    required = _summary_json(Path(args.required) / 'required-result.json')
    aggregate = _summary_json(Path(args.required) / 'fwrouter-aggregate.json')
    visibility = _summary_json(Path(args.required) / 'visibility.json')
    visibility_problem = 'visibility report is missing, malformed, or has a mismatched digest/identity'
    identity: dict[str, Any] = {}
    if plan_valid:
        identity = {'source_commit': expected_sha, 'plan_digest': plan.get('plan_digest'),
                    'manifest_digest': plan.get('manifest_digest'), 'manifest_version': plan.get('manifest_version'),
                    'run_id': run_id, 'run_attempt': attempt}
    schema = 'fwrouter-l6-required/v1' if workflow_kind == 'l6' else 'fwrouter-parallel-required/v1'
    required_valid = bool(required is not None and identity and required.get('schema') == schema
                          and required.get('eligible') is True
                          and all(type(required.get(key)) is type(value) and required.get(key) == value
                                  for key, value in identity.items()))
    recovery_files = {'installer/tests/test_safe_recovery.py', 'backend/tests/test_network_contract.py',
                      'backend/tests/test_dnsmasq.py', 'tests/application_acceptance/test_packet_dataplane.py'}
    recovery_required = bool(plan_valid and recovery_files <= set(plan.get('selected_files', [])))
    if required_valid:
        if workflow_kind == 'l6':
            expected_scope = ('main-regression' if ref == 'refs/heads/main'
                              else 'branch-regression-non-promotional')
            required_valid = (required.get('qualification_scope') == expected_scope
                              and required.get('minimum_repeats') == 2
                              and required.get('safe_recovery_required') is True)
        elif workflow_kind == 'prepr':
            required_valid = (plan.get('mandatory_suite_required') is True
                              and required.get('qualification_scope') == parallel_ci.PREPR_SCOPE)
        elif event == 'push' and ref == 'refs/heads/main':
            required_valid = (plan.get('mandatory_suite_required') is True
                              and required.get('safe_recovery_required') is recovery_required)
        else:
            required_valid = required.get('safe_recovery_required') is recovery_required
    current_jobs_valid = bool(isinstance(jobs, dict) and set(jobs) == set(parallel_ci.ROLES)
                              and all(isinstance(row, dict) and row.get('result') in
                                      {'success', 'failure', 'cancelled', 'skipped'} for row in jobs.values()))
    if current_jobs_valid and required is not None:
        required_valid = required_valid and required.get('upstream') == jobs
    strict_valid = bool(aggregate is not None and identity
                        and aggregate.get('source_commit') == expected_sha
                        and aggregate.get('plan_digest') == identity['plan_digest']
                        and aggregate.get('manifest_version') == identity['manifest_version']
                        and aggregate.get('selected_files') == sorted(plan.get('selected_files', []))
                        and aggregate.get('main_smoke_required') is (event == 'push' and ref == 'refs/heads/main'
                                                                       and workflow_kind != 'l6')
                        and aggregate.get('host_qualification_status') == 'NOT_VERIFIED'
                        and aggregate.get('hosted_repeats_required') == (2 if workflow_kind == 'l6' else 1)
                        and type(aggregate.get('eligible')) is bool
                        and isinstance(aggregate.get('blocked_reasons'), list)
                        and isinstance(aggregate.get('missing_levels'), list))
    visibility_valid = bool(visibility is not None and identity
                            and visibility.get('schema') == 'fwrouter-ci-visibility/v1'
                            and visibility.get('source_commit') == expected_sha
                            and visibility.get('plan_digest') == identity['plan_digest']
                            and visibility.get('run_id') == run_id
                            and visibility.get('run_attempt') == attempt
                            and visibility.get('discovered_suite_file_count') == len(manifest.get('test_files', []))
                            and type(visibility.get('selected_suite_file_count')) is int
                            and type(visibility.get('observed_selected_suite_file_count')) is int
                            and _record_digest_valid(visibility, 'visibility_digest'))
    if visibility is None:
        visibility_problem = 'visibility report is missing or malformed'
    elif visibility.get('schema') != 'fwrouter-ci-visibility/v1':
        visibility_problem = 'visibility report schema does not match the current contract'
    elif visibility.get('source_commit') != expected_sha:
        visibility_problem = 'visibility source SHA does not match the expected workflow SHA'
    elif visibility.get('plan_digest') != identity.get('plan_digest'):
        visibility_problem = 'visibility plan digest does not match the current immutable plan'
    elif visibility.get('run_id') != run_id:
        visibility_problem = 'visibility run ID does not match this workflow run'
    elif visibility.get('run_attempt') != attempt:
        visibility_problem = 'visibility run attempt does not match this workflow attempt'
    elif not _record_digest_valid(visibility, 'visibility_digest'):
        visibility_problem = 'visibility digest is invalid'
    elif visibility.get('discovered_suite_file_count') != len(manifest.get('test_files', [])):
        visibility_problem = 'discovered suite count does not match the checked-out manifest'
    if visibility_valid:
        strict_eligible = aggregate.get('eligible') is True if aggregate else False
        required_eligible = required.get('eligible') is True if required else False
        visibility_valid = (visibility.get('strict_aggregate_eligible') is strict_eligible
                            and visibility.get('required_result_eligible') is required_eligible
                            and visibility.get('qualification_status') ==
                            ('passed' if strict_eligible and required_eligible else 'incomplete_or_failed'))
        executions = visibility.get('executions')
        statuses = visibility.get('statuses')
        visibility_pass_valid = False
        if not isinstance(executions, list) or not isinstance(statuses, dict):
            visibility_valid = False
        else:
            observed_ids = [row.get('node_id') for row in executions if isinstance(row, dict)]
            observed_statuses = [row.get('status') for row in executions if isinstance(row, dict)]
            expected_status_counts = {key: sum(value == key for value in observed_statuses)
                                      for key in ('passed', 'failed', 'skipped', 'blocked', 'not_run')}
            selected_files = set(plan.get('selected_files', [])) if plan_valid else set()
            observed_files = {node.split('::', 1)[0] for node in observed_ids if isinstance(node, str)}
            unique_node_status: dict[str, str] = {}
            skip_reasons: dict[str, Any] = {}
            conflicting_nodes: set[str] = set()
            for row in executions:
                if not isinstance(row, dict) or not isinstance(row.get('node_id'), str):
                    continue
                node = row['node_id']
                status = row.get('status')
                if node in unique_node_status and unique_node_status[node] != status:
                    conflicting_nodes.add(node)
                else:
                    unique_node_status[node] = status
                if status == 'skipped':
                    reason = row.get('skip_reason')
                    if node in skip_reasons and skip_reasons[node] != reason:
                        conflicting_nodes.add(node)
                    else:
                        skip_reasons[node] = reason
            selected_observed_files = selected_files & observed_files
            not_run_files = sorted(selected_files - selected_observed_files)
            observed_path_binding_valid = (
                observed_files <= selected_files
                and visibility.get('selected_suite_file_count') == len(selected_files)
                and visibility.get('observed_selected_suite_file_count') == len(selected_observed_files)
                and visibility.get('not_run_selected_files') == not_run_files)
            if not observed_path_binding_valid:
                visibility_problem = 'observed case suite paths/counts do not match the immutable selected plan'
            try:
                gate.validate_authorized_node_skips(
                    manifest, unique_node_status, skip_reasons, required_paths=selected_files)
                skip_policy_valid = not conflicting_nodes
            except (gate.GateError, TypeError, ValueError) as exc:
                skip_policy_valid = False
                visibility_problem = str(exc)
            if conflicting_nodes:
                visibility_problem = 'repeated case IDs have conflicting terminal statuses or SKIP reasons'
            try:
                visibility_pass_valid = (skip_policy_valid and gate.policy_complete_node_results(
                    manifest, unique_node_status, skip_reasons, required_paths=selected_files))
            except (gate.GateError, TypeError, ValueError):
                visibility_pass_valid = False
            visibility_valid = (visibility_valid and len(observed_ids) == len(executions)
                                and all(isinstance(node, str) for node in observed_ids)
                                and all(isinstance(value, str) and value in expected_status_counts
                                        for value in observed_statuses)
                                and type(visibility.get('unique_test_coverage')) is int
                                and visibility.get('unique_test_coverage') == len(set(observed_ids))
                                and type(visibility.get('total_test_executions')) is int
                                and visibility.get('total_test_executions') == len(executions)
                                and statuses == expected_status_counts
                                and observed_path_binding_valid and skip_policy_valid
                                and type(visibility.get('selected_suite_file_count')) is int
                                and type(visibility.get('observed_selected_suite_file_count')) is int
                                and 0 <= visibility['observed_selected_suite_file_count']
                                <= visibility['selected_suite_file_count']
                                and isinstance(visibility.get('not_run_selected_files'), list)
                                and len(visibility['not_run_selected_files'])
                                <= visibility['selected_suite_file_count'])
    artifact_id = str(getattr(args, 'artifact_id', '') or '').strip()
    artifact_digest = str(getattr(args, 'artifact_digest', '') or '').strip().lower().removeprefix('sha256:')
    artifact_valid = (artifact_id.isdigit() and len(artifact_id) <= 20 and int(artifact_id or '0') > 0 and len(artifact_digest) == 64
                      and all(char in '0123456789abcdef' for char in artifact_digest))
    run_outcomes_pass = all(value == 'success' for value in outcomes.values())
    required_roles = parallel_ci.requirements(plan, main_smoke=(event == 'push' and ref == 'refs/heads/main')) if plan_valid else {}
    failing_jobs = [role for role in parallel_ci.ROLES if required_roles.get(role)
                    and current_jobs_valid and jobs[role]['result'] == 'failure']
    incomplete_jobs = [role for role in parallel_ci.ROLES if required_roles.get(role)
                       and (not current_jobs_valid or jobs[role]['result'] != 'success')]
    failing_cases = []
    if visibility_valid:
        failing_cases = [row for row in visibility.get('executions', []) if isinstance(row, dict)
                         and row.get('status') == 'failed']
    explicit_failure = bool(failing_jobs or failing_cases)
    if context_valid and explicit_failure and not run_outcomes_pass:
        status = 'FAIL'
    elif (context_valid and run_outcomes_pass and current_jobs_valid and not incomplete_jobs
          and required_valid and strict_valid and aggregate.get('eligible') is True
          and not aggregate.get('blocked_reasons') and not aggregate.get('missing_levels')
          and aggregate.get('host_qualification_status') == 'NOT_VERIFIED'
          and (workflow_kind != 'l6' or aggregate.get('hosted_repeats_required') == 2)
          and visibility_valid and visibility_pass_valid and visibility.get('qualification_status') == 'passed'
          and not visibility.get('not_run_selected_files') and not visibility.get('diagnostic_limitations')
          and artifact_valid):
        status = 'PASS'
    else:
        status = 'INCOMPLETE'

    run_url = str(getattr(args, 'run_url', '') or '')
    if not run_url and os.environ.get('GITHUB_SERVER_URL') and os.environ.get('GITHUB_REPOSITORY') and run_id.isdigit():
        run_url = f"{os.environ['GITHUB_SERVER_URL'].rstrip('/')}/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{run_id}"
    run_url = run_url if run_url.startswith(('https://', 'http://')) and not any(c in run_url for c in '\r\n() ') else ''
    profile = _summary_profile(event, ref, workflow_kind)
    job_outcomes = ', '.join(
        f"{role}={_safe_summary_text(jobs[role].get('result', 'missing'))}"
        for role in parallel_ci.ROLES if current_jobs_valid
    ) or 'NOT AVAILABLE'
    lines = ["## FWRouter CI qualification summary", "", f"- Overall result: **{status}**",
             f"- Profile: **{_safe_summary_text(profile)}**",
             f"- Source SHA: `{_safe_summary_text(expected_sha or 'NOT AVAILABLE')}`",
             f"- Event/ref: `{_safe_summary_text(event or 'NOT AVAILABLE')}` / `{_safe_summary_text(ref or 'NOT AVAILABLE')}`",
             f"- Run: `{_safe_summary_text(run_id or 'NOT AVAILABLE')}`, attempt `{_safe_summary_text(attempt or 'NOT AVAILABLE')}`",
             f"- Aggregate / bundle / publication step outcomes: `{_safe_summary_text(outcomes['aggregate'])}` / `"
             f"{_safe_summary_text(outcomes['bundle'])}` / `{_safe_summary_text(outcomes['publication'])}`",
             f"- Expected job outcomes: {job_outcomes}",
             f"- Result bundle artifact ID/digest: `{_safe_summary_text(artifact_id or 'NOT AVAILABLE')}` / `"
             f"{_safe_summary_text(artifact_digest or 'NOT AVAILABLE')}`"]
    if run_url:
        lines.append(f"- [Open this run and its job logs]({run_url})")
    if artifact_valid and context_valid:
        artifact_name = (f"fwrouter-l6-evidence-{expected_sha}-{run_id}-{attempt}" if workflow_kind == 'l6'
                         else f"fwrouter-pre-pr-evidence-{expected_sha}-{run_id}-{attempt}" if workflow_kind == 'prepr'
                         else f"fwrouter-main-evidence-{expected_sha}-{run_id}-{attempt}"
                         if event == 'push' and ref == 'refs/heads/main'
                         else f"fwrouter-pr-evidence-{expected_sha}-{run_id}-{attempt}")
        lines.append(f"- Independently replayable evidence artifact: `{_safe_summary_text(artifact_name)}`")
    if not context_valid:
        lines.append('- Qualification identity: **INCOMPLETE**; plan/source/run/event/ref did not validate against this attempt.')
    elif status == 'INCOMPLETE' and not visibility_valid:
        lines.append('- Visibility validation: ' + _safe_summary_text(visibility_problem, 360))
    if current_jobs_valid and required_roles:
        lines += ['', '| Required job | Result |', '|---|---|']
        for role in parallel_ci.ROLES:
            if required_roles.get(role):
                lines.append(f"| {role} | {_safe_summary_text(jobs[role]['result'])} |")
    if failing_jobs:
        lines.append('- Required jobs with explicit failure: ' + ', '.join(_safe_summary_text(role) for role in failing_jobs) + '.')
    if required is not None and required.get('error'):
        lines.append('- Aggregation detail: ' + _safe_summary_text(required['error'], 360))
    if strict_valid and isinstance(aggregate.get('blocked_reasons'), list):
        for reason in aggregate['blocked_reasons'][:5]:
            lines.append('- Qualification detail: ' + _safe_summary_text(reason, 240))
    if visibility_valid:
        import visibility_report
        lines += ['', visibility_report.summary(visibility, profile=profile, run_url=run_url)]
    else:
        lines += ['', '## CI test execution', '', '**Observed test counts and coverage: NOT AVAILABLE from a validated current-attempt projection.**',
                  '- Counts withheld: ' + _safe_summary_text(visibility_problem, 360)]
    if status != 'PASS' and outcomes.get('publication') != 'success':
        lines += ['', '- Successful evidence bundle: NOT PUBLISHED; inspect the run logs and 3-day diagnostic artifact if available.']
    lines += ['', '- The downloadable main/L6 result bundle remains the independently replayable machine evidence; this Job Summary is presentation only.', '']
    content = '\n'.join(lines)
    max_bytes = 16 * 1024
    encoded = content.encode('utf-8')
    if len(encoded) > max_bytes:
        content = encoded[:max_bytes - 64].decode('utf-8', errors='ignore') + '\n\n[Summary truncated; consult run logs.]\n'
    Path(args.output).write_text(content, encoding='utf-8')
    return 2 if status != 'PASS' else 0


def quick_summary(args: argparse.Namespace) -> int:
    """Render branch-only quick checks without retaining a downloadable artifact."""
    run_id, attempt = str(args.run_id), str(args.attempt)
    expected_sha = str(args.expected_sha)
    event, ref = str(args.event), str(args.ref)
    try:
        state = (args.state.read_text(encoding='utf-8').strip()
                 if not args.state.is_symlink() and args.state.is_file() and args.state.stat().st_size <= 128
                 else 'unknown')
    except OSError:
        state = 'unknown'
    plan = _summary_json(args.plan)
    report = _summary_json(args.report)
    status = 'INCOMPLETE'
    profile = 'non-promotional branch quick selection'
    verified = False
    try:
        if plan is not None and report is not None:
            gate.ensure_plan(plan, gate.load_manifest())
            verified = (event == 'push' and ref.startswith('refs/heads/') and ref != 'refs/heads/main'
                        and run_id.isdigit() and attempt.isdigit() and len(expected_sha) == 40
                        and all(char in '0123456789abcdef' for char in expected_sha)
                        and plan.get('source_commit') == expected_sha
                        and report.get('source_commit') == expected_sha
                        and report.get('plan_digest') == plan.get('plan_digest')
                        and _record_digest_valid(report, 'report_digest'))
    except (OSError, ValueError, gate.GateError, TypeError):
        verified = False
    statuses = report.get('node_status', {}) if isinstance(report, dict) else {}
    statuses = statuses if isinstance(statuses, dict) else {}
    counts = {key: sum(value == key for value in statuses.values())
              for key in ('passed', 'failed', 'skipped', 'blocked', 'not_run')}
    skip_groups: dict[str, int] = {}
    if verified and isinstance(report.get('authorized_skip_reasons'), dict):
        for reason in report['authorized_skip_reasons'].values():
            code = reason.get('reason_code') if isinstance(reason, dict) else None
            if isinstance(code, str):
                skip_groups[code] = skip_groups.get(code, 0) + 1
    if state == 'unchanged':
        status = 'NOT RUN'
    elif verified:
        if any(value == 'failed' for value in statuses.values()):
            status = 'FAIL'
        elif args.execution_outcome == 'success' and report.get('eligible') is True \
                and report.get('blocked_reasons') == []:
            status = 'PASS'
        else:
            status = 'INCOMPLETE'
    run_url = str(args.run_url or '')
    run_url = run_url if run_url.startswith(('https://', 'http://')) and not any(c in run_url for c in '\r\n() ') else ''
    lines = ["## FWRouter branch quick check", "", f"- Quick-scope result: **{status}** (L0-L1 only; non-promotional)",
             f"- Profile: **{_safe_summary_text(profile)}**",
             f"- Source SHA: `{_safe_summary_text(expected_sha or 'NOT AVAILABLE')}`; run `{_safe_summary_text(run_id or 'NOT AVAILABLE')}` / attempt `{_safe_summary_text(attempt or 'NOT AVAILABLE')}`",
             f"- Event/ref: `{_safe_summary_text(event)}` / `{_safe_summary_text(ref)}`",
             f"- Test execution: `{_safe_summary_text(args.execution_outcome)}`",
             f"- Selected suite files: **{len(plan.get('selected_files', [])) if verified else 'NOT AVAILABLE'}**; observed case IDs: **{len(set(statuses)) if verified else 'NOT AVAILABLE'}** unique",
             f"- Outcomes: PASS {counts['passed'] if verified else 'NOT AVAILABLE'}; FAIL {counts['failed'] if verified else 'NOT AVAILABLE'}; SKIP {counts['skipped'] if verified else 'NOT AVAILABLE'}; BLOCKED {counts['blocked'] if verified else 'NOT AVAILABLE'}; NOT RUN {counts['not_run'] if verified else 'NOT AVAILABLE'}"]
    lines.append('- Authorized SKIP groups: ' +
                 (', '.join(f'{_safe_summary_text(key)} ({count})' for key, count in sorted(skip_groups.items()))
                  if skip_groups else ('none observed' if verified else 'NOT AVAILABLE')))
    host_status = report.get('host_qualification_status', 'NOT_VERIFIED') if verified else 'NOT AVAILABLE'
    lines.append(f'- Host qualification: **{_safe_summary_text(host_status)}** (not established by this quick lane).')
    lines.append('- Line/branch coverage: NOT COLLECTED; L5 regression coverage: NOT AVAILABLE in quick scope.')
    if run_url:
        lines.append(f"- [Open this run and its job logs]({run_url})")
    failed_ids = [(node, value) for node, value in statuses.items()
                  if value in {'failed', 'blocked', 'not_run'}] if verified else []
    if failed_ids:
        lines += ['', '### Non-passing case IDs']
        for node, value in failed_ids[:12]:
            lines.append(f"- `{_safe_summary_text(node, 240)}` — {_safe_summary_text(value)}")
        if len(failed_ids) > 12:
            lines.append(f"- {len(failed_ids) - 12} additional IDs are in the runner report/logs.")
    diagnostics = report.get('failure_diagnostics', []) if isinstance(report, dict) else []
    for item in diagnostics[:4] if isinstance(diagnostics, list) else []:
        if isinstance(item, dict):
            cause = item.get('path') or item.get('suite') or item.get('captured_output_redacted') or 'failure details unavailable'
            lines.append('- Failure detail: ' + _safe_summary_text(cause, 240))
    blocked = report.get('blocked_reasons', []) if isinstance(report, dict) else []
    for reason in blocked[:5] if isinstance(blocked, list) else []:
        if isinstance(reason, str):
            lines.append('- Failure cause: ' + _safe_summary_text(reason, 240))
    if state == 'unchanged':
        lines.append('- Source tree unchanged; test execution was NOT RUN. This is not a qualification result.')
    elif not verified:
        lines.append('- Plan/report source, digest, or run context did not validate; counts are NOT AVAILABLE.')
    lines.append('- This quick branch lane does not establish required CI, main, L6, deployment, or live qualification.')
    content = '\n'.join(lines) + '\n'
    encoded = content.encode('utf-8')
    if len(encoded) > 8192:
        content = encoded[:8128].decode('utf-8', errors='ignore') + '\n[Summary truncated; consult run logs.]\n'
    Path(args.output).write_text(content, encoding='utf-8')
    # This reporting step must not mask the quick test step's own result.
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build")
    build.add_argument("--evidence", type=Path, required=True)
    build.add_argument("--required", type=Path, required=True)
    build.add_argument("--jobs", type=Path, required=True)
    build.add_argument("--destination", type=Path, required=True)
    build.add_argument("--event", required=True)
    build.add_argument("--ref", required=True)
    build.add_argument("--run-id", required=True)
    build.add_argument("--attempt", required=True)
    build.add_argument("--workflow-kind", choices=("main", "prepr"), default="main")
    verify = commands.add_parser("verify")
    verify.add_argument("--bundle", type=Path, required=True)
    verify.add_argument("--expected-sha", required=True)
    verify.add_argument("--event", choices=("push", "pull_request", "workflow_dispatch"), required=True)
    verify.add_argument("--ref", required=True)
    verify.add_argument("--run-id", required=True)
    verify.add_argument("--attempt", required=True)
    verify.add_argument("--require-main", action="store_true")
    verify.add_argument("--workflow-kind", choices=("main", "prepr"), default="main")
    diag = commands.add_parser("diagnostics")
    diag.add_argument("--l6", action="store_true")
    diag.add_argument("--required", type=Path, required=True)
    diag.add_argument("--jobs", type=Path, required=True)
    diag.add_argument("--destination", type=Path, required=True)
    diag.add_argument("--event", required=True)
    diag.add_argument("--ref", required=True)
    diag.add_argument("--run-id", required=True)
    diag.add_argument("--attempt", required=True)
    diag.add_argument("--source-sha", default="")
    diag.add_argument("--evidence", type=Path)
    handoff_parser = commands.add_parser("handoff")
    handoff_parser.add_argument("--source", type=Path, required=True)
    handoff_parser.add_argument("--destination", type=Path, required=True)
    handoff_parser.add_argument("--plan", type=Path, required=True)
    handoff_parser.add_argument("--role", choices=parallel_ci.ROLES, required=True)
    handoff_parser.add_argument("--shard-group", choices=gate.ROUTINE_SHARD_GROUPS)
    handoff_parser.add_argument("--native-profile", choices=("qcp", "xray"))
    handoff_parser.add_argument("--l6", action="store_true")
    handoff_parser.add_argument("--outcome", choices=("success", "failure", "cancelled", "skipped"), required=True)
    handoff_parser.add_argument("--event", required=True)
    handoff_parser.add_argument("--ref", required=True)
    summary_parser = commands.add_parser("summary")
    summary_parser.add_argument("--required", type=Path, required=True)
    summary_parser.add_argument("--plan", type=Path)
    summary_parser.add_argument("--jobs", type=Path)
    summary_parser.add_argument("--output", required=True)
    summary_parser.add_argument("--run-id", required=True)
    summary_parser.add_argument("--attempt", required=True)
    summary_parser.add_argument("--expected-sha", default="")
    summary_parser.add_argument("--event", default="")
    summary_parser.add_argument("--ref", default="")
    summary_parser.add_argument("--workflow-kind", choices=("main", "l6", "prepr"), default="main")
    summary_parser.add_argument("--aggregate-outcome", required=True)
    summary_parser.add_argument("--bundle-outcome", required=True)
    summary_parser.add_argument("--publish-outcome", required=True)
    summary_parser.add_argument("--artifact-id", default="")
    summary_parser.add_argument("--artifact-digest", default="")
    summary_parser.add_argument("--run-url", default="")
    quick_summary_parser = commands.add_parser("quick-summary")
    quick_summary_parser.add_argument("--plan", type=Path, required=True)
    quick_summary_parser.add_argument("--report", type=Path, required=True)
    quick_summary_parser.add_argument("--state", type=Path, required=True)
    quick_summary_parser.add_argument("--output", required=True)
    quick_summary_parser.add_argument("--expected-sha", required=True)
    quick_summary_parser.add_argument("--run-id", required=True)
    quick_summary_parser.add_argument("--attempt", required=True)
    quick_summary_parser.add_argument("--event", required=True)
    quick_summary_parser.add_argument("--ref", required=True)
    quick_summary_parser.add_argument("--execution-outcome", required=True)
    quick_summary_parser.add_argument("--run-url", default="")
    args = parser.parse_args()
    try:
        if args.command == "build":
            result = build_bundle(args)
            print(json.dumps({"status": "eligible", "source_sha": result["source_sha"],
                              "run_id": result["run_id"], "run_attempt": result["run_attempt"]}, sort_keys=True))
        elif args.command == "verify":
            print(json.dumps(verify_bundle(args), sort_keys=True))
        elif args.command == "diagnostics":
            print(json.dumps(diagnostics(args), sort_keys=True))
        elif args.command == "handoff":
            print(json.dumps(handoff(args), sort_keys=True))
        elif args.command == "quick-summary":
            return quick_summary(args)
        else:
            return summary(args)
        return 0
    except Exception as exc:
        print(f"CI evidence bundle: {gate.redact_failure_output(str(exc))[:512]}", file=sys.stderr)
        if args.command in {'summary', 'quick-summary'}:
            status = 'INCOMPLETE'
            profile = (_summary_profile(getattr(args, 'event', ''), getattr(args, 'ref', ''),
                                        getattr(args, 'workflow_kind', 'main'))
                       if args.command == 'summary' else 'non-promotional branch quick selection')
            sha = getattr(args, 'expected_sha', '') or 'NOT AVAILABLE'
            run_id, attempt = getattr(args, 'run_id', ''), getattr(args, 'attempt', '')
            fallback = [f"## FWRouter {'CI qualification' if args.command == 'summary' else 'branch quick check'} summary",
                        '', f"- Overall result: **{status}**", f"- Profile: **{_safe_summary_text(profile)}**",
                        f"- Source SHA: `{_safe_summary_text(sha)}`; run `{_safe_summary_text(run_id)}` / attempt `{_safe_summary_text(attempt)}`",
                        '- Counts, coverage and evidence identity: NOT AVAILABLE; summary validation failed.',
                        '- Inspect this workflow run and its job logs; this summary does not establish qualification.', '']
            Path(args.output).write_text('\n'.join(fallback), encoding='utf-8')
            return 0 if args.command == 'quick-summary' else 2
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
