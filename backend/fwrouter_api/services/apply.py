from __future__ import annotations

import json
from enum import Enum
import resource
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from fwrouter_api.adapters.dataplane import (
    DEFAULT_DATAPLANE_ADAPTER,
    DataplaneOperation,
    DataplanePlan,
    DataplaneResult,
)
from fwrouter_api.db.connection import db_session
from fwrouter_api.services.dataplane_status import get_dataplane_capability
from fwrouter_api.services.dataplane_status import build_bypass_runtime_enforcement
from fwrouter_api.services.dataplane_global import (
    build_applied_runtime_enforcement,
    build_global_preflight,
)
from fwrouter_api.services.dataplane_nft import promote_last_good
from fwrouter_api.services.dataplane_live import (
    live_mode_matches_intent,
    probe_live_global_mode,
)
from fwrouter_api.services.live_probe_cache import clear_live_probe_cache
from fwrouter_api.services.artifacts import (
    build_artifact_summary,
    atomic_write_json,
    write_job_json_artifact,
)
from fwrouter_api.services.logs import write_operational_log
from fwrouter_api.services.dnsmasq import reconcile_dnsmasq_rules
from fwrouter_api.services.runtime_prewarm import prime_runtime_read_models_async
from fwrouter_api.services.jobs import (
    get_job,
    get_job_without_cleanup,
    touch_job_running,
    update_job_running_result,
)
from fwrouter_api.services.routing_manifest import (
    build_dataplane_manifest,
    build_dataplane_manifest_from_state,
    write_dataplane_manifest,
)
from fwrouter_api.services.server_layout import SERVER_LAYOUT_CONTRACT_VERSION
from fwrouter_api.core.config import get_settings


class ApplyMode(str, Enum):
    DRY_RUN = "dry_run"
    APPLY = "apply"


class ApplyPhaseTimeoutError(TimeoutError):
    """Raised when one bounded apply phase exceeds its configured timeout."""


class ApplyJobAbortedError(RuntimeError):
    """Raised when the job is no longer active while apply side effects are in flight."""


_FAST_SUBJECT_APPLY_TYPES = {"lan", "tailscale", "tailscale_node"}
_FAST_SUBJECT_APPLY_MODES = {"direct", "selective", "vpn"}
_GLOBAL_MODE_HOT_SWAP_INTENTS = {"set_global_mode"}
_NFT_COMMENT_PATTERN = re.compile(r'comment "([^"]+)"')


class ApplyPhaseTracker:
    """Record apply lifecycle phases and refresh the running job lease."""

    def __init__(self, *, job_id: str, apply_id: str) -> None:
        self.job_id = job_id
        self.apply_id = apply_id
        self.timeout_seconds = int(get_settings().apply_phase_timeout_seconds)
        self.events: list[dict[str, Any]] = []
        self.current_phase: str | None = None
        self.current_started_at: float | None = None
        self._write()

    def begin(self, phase: str, **details: Any) -> None:
        self.current_phase = phase
        self.current_started_at = time.monotonic()
        touch_job_running(self.job_id)
        self.events.append(
            {
                "phase": phase,
                "event": "start",
                "ts": time.time(),
                "details": details,
            }
        )
        self._write()

    def finish(self, **details: Any) -> None:
        phase = self.current_phase or "unknown"
        duration = None
        if self.current_started_at is not None:
            duration = time.monotonic() - self.current_started_at
        touch_job_running(self.job_id)
        self.events.append(
            {
                "phase": phase,
                "event": "finish",
                "ts": time.time(),
                "duration_seconds": duration,
                "details": details,
            }
        )
        self._write()
        self.current_phase = None
        self.current_started_at = None
        if duration is not None and duration > self.timeout_seconds:
            raise ApplyPhaseTimeoutError(
                f"Apply phase exceeded timeout: {phase} took {duration:.1f}s > {self.timeout_seconds}s."
            )

    def _write(self) -> None:
        snapshot = {
            "apply_id": self.apply_id,
            "current_phase": self.current_phase,
            "events": self.events,
        }
        write_job_json_artifact(
            self.job_id,
            "dataplane/phases.json",
            snapshot,
        )
        update_job_running_result(
            self.job_id,
            result={
                "job_status": "running",
                "stage": self.current_phase or "queued",
                "apply": snapshot,
            },
        )


def _memory_snapshot() -> dict[str, Any]:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return {
        "rss_kb": int(usage.ru_maxrss),
        "user_cpu_seconds": round(float(usage.ru_utime), 3),
        "system_cpu_seconds": round(float(usage.ru_stime), 3),
    }


def _render_failure_result(
    *,
    plan: dict[str, Any],
    stage: str,
    error_code: str,
    error_message: str,
    manifest_state: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "ok": False,
        "apply_id": plan["apply_id"],
        "job_id": plan["job_id"],
        "mode": plan["mode"],
        "reason": plan["reason"],
        "dataplane_capability": "nft_owned_table",
        "enforcement_level": "unknown",
        "traffic_enforcement_guaranteed": False,
        "supported_modes": {},
        "missing_runtime_requirements": [],
        "stage": stage,
        "manifest": {
            "summary": {
                "render_failed": True,
                "manifest_state_provided": manifest_state is not None,
            },
            "paths": {},
            "contract_version": SERVER_LAYOUT_CONTRACT_VERSION,
            "owned_table": None,
            "required_chains": [],
            "generated_at": None,
            "profile": None,
        },
        "scoped_egress": {},
        "preflight": {},
        "dataplane": {
            "ok": False,
            "operation": DataplaneOperation.CHECK.value,
            "message": error_message,
            "error_code": error_code,
            "error_message": error_message,
            "details": {
                "stage": stage,
                "memory": _memory_snapshot(),
            },
        },
        "rollback": None,
    }


def build_apply_plan(
    *,
    job_id: str,
    reason: str,
    mode: ApplyMode = ApplyMode.DRY_RUN,
    input_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build an apply plan DTO without changing runtime state."""

    apply_id = str(uuid4())
    artifacts = build_artifact_summary(job_id)

    return {
        "apply_id": apply_id,
        "job_id": job_id,
        "reason": reason,
        "mode": mode.value,
        "input": input_data or {},
        "artifacts": artifacts,
        "dataplane": {
            "operation": DataplaneOperation.CHECK.value,
            "adapter": "nft-owned-table",
            "contract_version": SERVER_LAYOUT_CONTRACT_VERSION,
            "dataplane_capability": get_dataplane_capability(),
        },
    }


def _last_good_manifest_path() -> Path:
    return get_settings().paths.generated_dir / "dataplane" / "last-good-manifest.json"


def _result_manifest_path() -> Path:
    return get_settings().paths.generated_dir / "dataplane" / "last-result.json"


def _ensure_job_context(job_id: str) -> None:
    if get_job(job_id) is None:
        raise ValueError(
            "Apply pipeline requires an existing jobs row before transaction start: "
            f"{job_id}"
        )


def _runtime_mode_from_manifest(manifest: dict[str, Any]) -> str:
    routing = manifest.get("routing_global_state")
    if not isinstance(routing, dict):
        return "direct"
    return str(routing.get("desired_mode") or routing.get("applied_mode") or "direct")


def _manifest_requests_core_bypass(manifest: dict[str, Any]) -> bool:
    extra = manifest.get("extra")
    if not isinstance(extra, dict):
        return False
    core_bypass = extra.get("core_bypass")
    return isinstance(core_bypass, dict) and bool(core_bypass.get("enabled"))


def _fast_subject_apply_context(
    *,
    input_data: dict[str, Any] | None,
    manifest: dict[str, Any],
) -> dict[str, Any] | None:
    if not isinstance(input_data, dict):
        return None
    fast_apply = input_data.get("fast_subject_apply")
    if not isinstance(fast_apply, dict) or not bool(fast_apply.get("enabled")):
        return None

    global_mode = _runtime_mode_from_manifest(manifest).strip().lower()
    if global_mode != "direct":
        return None

    subject_id = str(fast_apply.get("subject_id") or "").strip()
    subject_type = str(fast_apply.get("subject_type") or "").strip().lower()
    target_mode = str(fast_apply.get("target_mode") or "").strip().lower()
    if not subject_id or subject_type not in _FAST_SUBJECT_APPLY_TYPES or target_mode not in _FAST_SUBJECT_APPLY_MODES:
        return None

    subjects = manifest.get("subjects") if isinstance(manifest, dict) else None
    if not isinstance(subjects, list):
        return None
    manifest_subject = next(
        (
            subject
            for subject in subjects
            if isinstance(subject, dict) and str(subject.get("subject_id") or "") == subject_id
        ),
        None,
    )
    if not isinstance(manifest_subject, dict):
        return None
    expected_path = "vpn" if target_mode == "vpn" else target_mode
    manifest_path = str(manifest_subject.get("dataplane_path") or "").strip().lower()
    if manifest_path != expected_path:
        return None

    return {
        "subject_id": subject_id,
        "subject_type": subject_type,
        "target_mode": target_mode,
        "manifest_subject": manifest_subject,
        "global_mode": global_mode,
    }


def _verify_fast_subject_apply(context: dict[str, Any]) -> dict[str, Any]:
    subject_id = str(context.get("subject_id") or "")
    target_mode = str(context.get("target_mode") or "")
    manifest_subject = context.get("manifest_subject") if isinstance(context.get("manifest_subject"), dict) else {}
    scoped_runtime = manifest_subject.get("scoped_runtime") if isinstance(manifest_subject, dict) else {}
    matcher = scoped_runtime.get("matcher") if isinstance(scoped_runtime, dict) else {}
    family = str(matcher.get("family") or "").strip().lower() if isinstance(matcher, dict) else ""

    try:
        completed = subprocess.run(
            ["nft", "list", "chain", "inet", "fwrouter_v2", "fwrouter_classify"],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        return {
            "ok": False,
            "error_code": "NFT_NOT_AVAILABLE",
            "error_message": str(exc),
            "subject_id": subject_id,
            "target_mode": target_mode,
            "raw_chain": "",
        }
    except subprocess.CalledProcessError as exc:
        return {
            "ok": False,
            "error_code": "NFT_CHAIN_READ_FAILED",
            "error_message": (exc.stderr or exc.stdout or str(exc)).strip(),
            "subject_id": subject_id,
            "target_mode": target_mode,
            "raw_chain": exc.stdout or "",
        }

    raw_chain = completed.stdout
    if target_mode == "direct":
        ok = f'scoped direct override: {subject_id}' in raw_chain
    elif target_mode == "vpn":
        ok = f'scoped vpn override: {subject_id}' in raw_chain
    else:
        if family == "ipv6":
            direct_branch = f'scoped selective direct IPv6: {subject_id}'
            vpn_branch = (
                f'scoped selective vpn IPv6: {subject_id}' in raw_chain
                or f'scoped selective degraded block VPN IPv6: {subject_id}' in raw_chain
            )
        else:
            direct_branch = f'scoped selective direct IPv4: {subject_id}'
            vpn_branch = (
                f'scoped selective vpn IPv4: {subject_id}' in raw_chain
                or f'scoped selective degraded block VPN IPv4: {subject_id}' in raw_chain
            )
        default_branch = (
            f'scoped selective default ' in raw_chain and subject_id in raw_chain
        ) or f'scoped selective degraded default direct: {subject_id}' in raw_chain
        ok = direct_branch in raw_chain and vpn_branch and default_branch

    return {
        "ok": ok,
        "error_code": None if ok else "FAST_SUBJECT_APPLY_VERIFY_FAILED",
        "error_message": None if ok else f"Live classify chain is missing expected subject rule for {subject_id}.",
        "subject_id": subject_id,
        "target_mode": target_mode,
        "raw_chain": raw_chain,
    }


def _extract_classify_rules(candidate_path: str | None) -> list[str]:
    if not candidate_path:
        return []
    path = Path(candidate_path)
    if not path.exists():
        return []

    rules: list[str] = []
    in_chain = False
    depth = 0
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        stripped = raw_line.strip()
        if not in_chain:
            if stripped == "chain fwrouter_classify {":
                in_chain = True
                depth = 1
            continue

        depth += stripped.count("{")
        depth -= stripped.count("}")
        if depth <= 0:
            break
        if stripped:
            rules.append(stripped)
    return rules


def _global_mode_hot_swap_context(
    *,
    input_data: dict[str, Any] | None,
    manifest: dict[str, Any],
    check_details: dict[str, Any],
    preflight: dict[str, Any],
    candidate_path: str | None,
) -> dict[str, Any] | None:
    if not isinstance(input_data, dict):
        return None
    if str(input_data.get("intent") or "").strip() not in _GLOBAL_MODE_HOT_SWAP_INTENTS:
        return None
    if _manifest_requests_core_bypass(manifest):
        return None

    required_chains = check_details.get("required_chains")
    if not bool(check_details.get("table_exists")):
        return None
    if not isinstance(required_chains, dict) or not all(bool(value) for value in required_chains.values()):
        return None

    if bool((manifest.get("summary") or {}).get("requires_vpn_policy_routing")):
        if not bool(check_details.get("vpn_external_path_verified")):
            return None

    if not bool(preflight.get("can_enforce_global_direct")):
        return None

    rules = _extract_classify_rules(candidate_path)
    if not rules:
        return None

    return {
        "target_mode": _runtime_mode_from_manifest(manifest),
        "rules": rules,
        "candidate_path": candidate_path,
    }


def _apply_global_mode_hot_swap(
    *,
    context: dict[str, Any],
    plan: DataplanePlan,
    check_details: dict[str, Any],
) -> DataplaneResult:
    rules = [str(rule) for rule in context.get("rules") or [] if str(rule).strip()]
    commands = ["flush chain inet fwrouter_v2 fwrouter_classify"]
    commands.extend(
        f"add rule inet fwrouter_v2 fwrouter_classify {rule}"
        for rule in rules
    )
    payload = "\n".join(commands) + "\n"

    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix="fwrouter-global-hot-swap-",
        suffix=".nft",
        delete=False,
    ) as handle:
        handle.write(payload)
        command_path = handle.name

    def _run_nft_payload() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["nft", "-f", command_path],
            check=False,
            capture_output=True,
            text=True,
        )

    def _verify_live_comments() -> dict[str, Any]:
        expected_comments = _NFT_COMMENT_PATTERN.findall(payload)
        try:
            live = subprocess.run(
                ["nft", "list", "chain", "inet", "fwrouter_v2", "fwrouter_classify"],
                check=False,
                capture_output=True,
                text=True,
            )
        except FileNotFoundError as exc:
            return {
                "ok": False,
                "error_code": "NFT_NOT_AVAILABLE",
                "error_message": str(exc),
                "missing_comments": expected_comments,
            }
        if live.returncode != 0:
            return {
                "ok": False,
                "error_code": "NFT_CHAIN_READ_FAILED",
                "error_message": (live.stderr or live.stdout or "Failed to read live classify chain.").strip(),
                "missing_comments": expected_comments,
            }
        raw_chain = live.stdout
        missing = [comment for comment in expected_comments if comment not in raw_chain]
        return {
            "ok": not missing,
            "error_code": None if not missing else "NFT_GLOBAL_MODE_HOT_SWAP_VERIFY_FAILED",
            "error_message": None if not missing else "Live classify chain is missing hot-swapped rule markers.",
            "missing_comments": missing,
            "raw_chain": raw_chain,
        }

    try:
        completed = _run_nft_payload()
        hot_swap_verify = _verify_live_comments() if completed.returncode == 0 else {
            "ok": False,
            "error_code": "NFT_GLOBAL_MODE_HOT_SWAP_FAILED",
            "error_message": (completed.stderr or completed.stdout or "nft hot-swap failed.").strip(),
            "missing_comments": [],
        }
        retried = False
        if completed.returncode == 0 and not bool(hot_swap_verify.get("ok")):
            retried = True
            completed = _run_nft_payload()
            hot_swap_verify = _verify_live_comments() if completed.returncode == 0 else hot_swap_verify
    finally:
        try:
            Path(command_path).unlink()
        except FileNotFoundError:
            pass

    details = {
        **check_details,
        "adapter": "nft-owned-table",
        "operation": DataplaneOperation.APPLY.value,
        "stage": "verify" if completed.returncode == 0 else "apply",
        "hot_swap": True,
        "hot_swap_scope": "fwrouter_classify",
        "hot_swap_rules_count": len(rules),
        "hot_swap_verify": hot_swap_verify,
        "hot_swap_retried": retried,
        "candidate_path": context.get("candidate_path"),
        "script": {
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        },
    }
    ok = completed.returncode == 0 and bool(hot_swap_verify.get("ok"))
    return DataplaneResult(
        ok=ok,
        operation=DataplaneOperation.APPLY,
        message=(
            "FWRouter global mode classify chain hot-swapped."
            if ok
            else "FWRouter global mode classify chain hot-swap failed."
        ),
        details=details,
        error_code=None if ok else str(hot_swap_verify.get("error_code") or "NFT_GLOBAL_MODE_HOT_SWAP_FAILED"),
        error_message=None if ok else str(
            hot_swap_verify.get("error_message")
            or completed.stderr
            or completed.stdout
            or "nft hot-swap failed."
        ).strip(),
    )


def _require_job_running(job_id: str, *, phase: str) -> None:
    job = get_job_without_cleanup(job_id)
    if job is None or job.get("status") not in {"queued", "running"}:
        raise ApplyJobAbortedError(
            f"Apply job is no longer active during phase {phase}."
        )


def _materialize_manifest(
    *,
    prebuilt_manifest: dict[str, Any],
    plan_id: str,
    reason: str,
    input_data: dict[str, Any] | None,
) -> dict[str, Any]:
    manifest = dict(prebuilt_manifest)
    manifest["plan_id"] = plan_id
    manifest["reason"] = reason
    manifest["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    manifest["input"] = input_data or {}
    return manifest


def run_apply_pipeline(
    *,
    job_id: str,
    reason: str,
    mode: ApplyMode = ApplyMode.DRY_RUN,
    input_data: dict[str, Any] | None = None,
    manifest_state: dict[str, Any] | None = None,
    prebuilt_manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run Wave 2 dataplane pipeline for the FWRouter-owned nftables table only."""
    _ensure_job_context(job_id)

    plan = build_apply_plan(
        job_id=job_id,
        reason=reason,
        mode=mode,
        input_data=input_data,
    )
    phase_tracker = ApplyPhaseTracker(job_id=job_id, apply_id=str(plan["apply_id"]))

    write_job_json_artifact(job_id, "input.json", plan["input"])
    write_job_json_artifact(job_id, "plan.json", plan)
    phase_tracker.begin("render_candidate", reason=reason, mode=mode.value)
    render_started_at = time.monotonic()
    render_memory_before = _memory_snapshot()
    try:
        manifest = (
            _materialize_manifest(
                prebuilt_manifest=prebuilt_manifest,
                plan_id=plan["apply_id"],
                reason=reason,
                input_data=plan["input"],
            )
            if prebuilt_manifest is not None
            else (
                build_dataplane_manifest_from_state(
                    plan_id=plan["apply_id"],
                    reason=reason,
                    routing=manifest_state["routing_global_state"],
                    subjects=manifest_state["subjects"],
                    input_data=plan["input"],
                    extra=manifest_state.get("extra"),
                )
                if manifest_state is not None
                else build_dataplane_manifest(
                    plan_id=plan["apply_id"],
                    reason=reason,
                    input_data=plan["input"],
                )
            )
        )
        manifest_paths = write_dataplane_manifest(
            job_id=job_id,
            plan_id=plan["apply_id"],
            manifest=manifest,
        )
        fast_subject_apply = _fast_subject_apply_context(
            input_data=plan["input"],
            manifest=manifest,
        )
        phase_tracker.finish(
            candidate_manifest_path=manifest_paths["candidate_manifest_path"],
            versioned_manifest_path=manifest_paths["versioned_manifest_path"],
            memory_before=render_memory_before,
            memory_after=_memory_snapshot(),
            render_duration_seconds=round(time.monotonic() - render_started_at, 3),
            subjects_count=manifest["summary"].get("subjects_count"),
            path_counts=manifest["summary"].get("path_counts"),
            extra_keys=manifest["summary"].get("extra_keys"),
            precompiled_profile=prebuilt_manifest is not None,
        )
    except ApplyPhaseTimeoutError as exc:
        result = _render_failure_result(
            plan=plan,
            stage="render_candidate",
            error_code="APPLY_RENDER_CANDIDATE_TIMEOUT",
            error_message=str(exc),
            manifest_state=manifest_state,
        )
        write_job_json_artifact(job_id, "dataplane/result.json", result)
        atomic_write_json(_result_manifest_path(), result)
        raise
    except Exception as exc:
        result = _render_failure_result(
            plan=plan,
            stage="render_candidate",
            error_code="APPLY_RENDER_CANDIDATE_FAILED",
            error_message=str(exc),
            manifest_state=manifest_state,
        )
        write_job_json_artifact(job_id, "dataplane/result.json", result)
        atomic_write_json(_result_manifest_path(), result)
        raise

    dataplane_plan = DataplanePlan(
        plan_id=plan["apply_id"],
        operation=DataplaneOperation.CHECK,
        manifest_path=manifest_paths["candidate_manifest_path"],
        generated_path=manifest_paths["candidate_nft_path"],
        rollback_path=manifest_paths["snapshot_before_nft_path"],
        artifact_paths=manifest_paths,
        contract_version=SERVER_LAYOUT_CONTRACT_VERSION,
        metadata={"job_id": job_id, "reason": reason},
    )
    phase_tracker.begin("check")
    check_result = DEFAULT_DATAPLANE_ADAPTER.check(dataplane_plan)
    phase_tracker.finish(
        ok=check_result.ok,
        error_code=check_result.error_code,
        stage=check_result.details.get("stage") or check_result.details.get("error_stage"),
    )
    preflight = build_global_preflight(
        routing=manifest.get("routing_global_state") if isinstance(manifest.get("routing_global_state"), dict) else None,
        check_details=check_result.details,
        effective_rules_artifact=(manifest.get("extra") or {}).get("rules_effective")
        if isinstance((manifest.get("extra") or {}).get("rules_effective"), dict)
        else None,
        require_runtime_verify=False,
    )
    rollback_result = None
    stage = "check"
    operation_result = check_result
    result_runtime_enforcement = dict(manifest["runtime_enforcement"])
    bypass_requested = _manifest_requests_core_bypass(manifest)
    global_mode_hot_swap = _global_mode_hot_swap_context(
        input_data=plan["input"],
        manifest=manifest,
        check_details=check_result.details,
        preflight=preflight,
        candidate_path=manifest_paths["candidate_nft_path"],
    )
    if bypass_requested:
        result_runtime_enforcement = build_bypass_runtime_enforcement(preflight=preflight)

    if mode == ApplyMode.APPLY and check_result.ok:
        _require_job_running(job_id, phase="before_apply_nft")
        apply_plan = DataplanePlan(
            plan_id=dataplane_plan.plan_id,
            operation=DataplaneOperation.APPLY,
            generated_path=dataplane_plan.generated_path,
            manifest_path=dataplane_plan.manifest_path,
            rollback_path=dataplane_plan.rollback_path,
            artifact_paths=dataplane_plan.artifact_paths,
            contract_version=dataplane_plan.contract_version,
            metadata=dataplane_plan.metadata,
        )
        apply_phase_name = "apply_global_mode_hot_swap" if global_mode_hot_swap is not None else "apply_nft"
        phase_tracker.begin(apply_phase_name)
        apply_result = (
            _apply_global_mode_hot_swap(
                context=global_mode_hot_swap,
                plan=apply_plan,
                check_details=check_result.details,
            )
            if global_mode_hot_swap is not None
            else DEFAULT_DATAPLANE_ADAPTER.apply(apply_plan)
        )
        phase_tracker.finish(
            ok=apply_result.ok,
            error_code=apply_result.error_code,
            stage=apply_result.details.get("stage") or apply_result.details.get("error_stage"),
        )
        operation_result = apply_result
        stage = str(apply_result.details.get("stage") or apply_result.details.get("error_stage") or "apply")

        if not apply_result.ok:
            phase_tracker.begin("rollback")
            rollback = DEFAULT_DATAPLANE_ADAPTER.rollback(apply_plan)
            rollback_result = {
                "ok": rollback.ok,
                "operation": rollback.operation.value,
                "message": rollback.message,
                "error_code": rollback.error_code,
                "error_message": rollback.error_message,
                "details": rollback.details,
            }
            phase_tracker.finish(
                ok=rollback.ok,
                error_code=rollback.error_code,
                stage=rollback.details.get("stage") or rollback.details.get("error_stage"),
            )
        else:
            stage = "verify"
            dnsmasq_reconcile = None
            selective_rules = (
                preflight.get("selective_rules")
                if isinstance(preflight.get("selective_rules"), dict)
                else {}
            )
            if (
                global_mode_hot_swap is None
                and str(selective_rules.get("path_kind") or "") == "domain_aware"
            ):
                phase_tracker.begin("reconcile_dnsmasq")
                dnsmasq_reconcile = reconcile_dnsmasq_rules()
                phase_tracker.finish(
                    ok=bool(dnsmasq_reconcile.get("ok")),
                    error_code=dnsmasq_reconcile.get("error_code"),
                    router_dns_ipv4=dnsmasq_reconcile.get("router_dns_ipv4"),
                )
                if not dnsmasq_reconcile.get("ok"):
                    phase_tracker.begin("rollback")
                    rollback = DEFAULT_DATAPLANE_ADAPTER.rollback(apply_plan)
                    rollback_result = {
                        "ok": rollback.ok,
                        "operation": rollback.operation.value,
                        "message": rollback.message,
                        "error_code": rollback.error_code,
                        "error_message": rollback.error_message,
                        "details": rollback.details,
                    }
                    phase_tracker.finish(
                        ok=rollback.ok,
                        error_code=rollback.error_code,
                        stage=rollback.details.get("stage") or rollback.details.get("error_stage"),
                    )
                    operation_result = type(apply_result)(
                        ok=False,
                        operation=apply_result.operation,
                        message="Dnsmasq selective contract reconcile failed after nft apply.",
                        details={
                            **apply_result.details,
                            "dnsmasq_reconcile": dnsmasq_reconcile,
                        },
                        error_code=str(dnsmasq_reconcile.get("error_code") or "DNSMASQ_RECONCILE_FAILED"),
                        error_message=str(dnsmasq_reconcile.get("message") or "Dnsmasq reconcile failed."),
                    )

            if operation_result.ok:
                phase_tracker.begin("verify_runtime")
                apply_mode = _runtime_mode_from_manifest(manifest)
                expected_selective_default = str(
                    (manifest.get("routing_global_state") or {}).get("selective_default")
                    or "direct"
                ).lower()
                if apply_mode == "selective" and bool(preflight.get("selective_degraded")):
                    expected_selective_default = "direct"
                fast_subject_verify = None
                if fast_subject_apply is not None:
                    fast_subject_verify = _verify_fast_subject_apply(fast_subject_apply)
                    live_mode_probe = {
                        "ok": True,
                        "mode": apply_mode,
                        "selective_default": expected_selective_default if apply_mode == "selective" else None,
                        "error_code": None,
                        "error_message": None,
                        "raw_chain": None,
                        "fast_subject_apply": True,
                    }
                    live_mode_ok = bool(fast_subject_verify.get("ok"))
                    apply_result.details["fast_subject_verify"] = fast_subject_verify
                else:
                    clear_live_probe_cache()
                    live_mode_probe = probe_live_global_mode()
                    live_mode_ok = bypass_requested or live_mode_matches_intent(
                        expected_mode=apply_mode,
                        expected_selective_default=expected_selective_default,
                        probe=live_mode_probe,
                    )
                    live_mode_probe_retries: list[dict[str, Any]] = []
                    if not live_mode_ok and not bypass_requested:
                        for attempt in range(1, 4):
                            time.sleep(0.25 * attempt)
                            clear_live_probe_cache()
                            retry_probe = probe_live_global_mode()
                            retry_ok = live_mode_matches_intent(
                                expected_mode=apply_mode,
                                expected_selective_default=expected_selective_default,
                                probe=retry_probe,
                            )
                            live_mode_probe_retries.append(
                                {
                                    "attempt": attempt,
                                    "ok": retry_ok,
                                    "mode": retry_probe.get("mode"),
                                    "selective_default": retry_probe.get("selective_default"),
                                    "error_code": retry_probe.get("error_code"),
                                    "error_message": retry_probe.get("error_message"),
                                }
                            )
                            if retry_ok:
                                live_mode_probe = retry_probe
                                live_mode_ok = True
                                break
                    if live_mode_probe_retries:
                        apply_result.details["live_mode_probe_retries"] = live_mode_probe_retries
                apply_result.details["live_mode_probe"] = live_mode_probe
                if dnsmasq_reconcile is not None:
                    apply_result.details["dnsmasq_reconcile"] = dnsmasq_reconcile
                apply_result.details["active_mode_matches_intent"] = live_mode_ok
                applied_preflight = build_global_preflight(
                    routing=manifest.get("routing_global_state") if isinstance(manifest.get("routing_global_state"), dict) else None,
                    check_details=apply_result.details,
                    effective_rules_artifact=(manifest.get("extra") or {}).get("rules_effective")
                    if isinstance((manifest.get("extra") or {}).get("rules_effective"), dict)
                    else None,
                    require_runtime_verify=apply_mode == "vpn",
                )
                if bypass_requested:
                    result_runtime_enforcement = build_bypass_runtime_enforcement(
                        preflight=applied_preflight,
                    )
                else:
                    result_runtime_enforcement = build_applied_runtime_enforcement(
                        routing=manifest.get("routing_global_state") if isinstance(manifest.get("routing_global_state"), dict) else None,
                        preflight=applied_preflight,
                        live_mode_probe=live_mode_probe,
                        mode_override=apply_mode,
                    )
                if (
                    not bypass_requested
                    and apply_mode == "vpn"
                    and not result_runtime_enforcement["traffic_enforcement_guaranteed"]
                    and bool(apply_result.details.get("vpn_external_path_verified"))
                    and bool(apply_result.details.get("vpn_contract_ready"))
                    and bool(preflight.get("can_enforce_global_vpn"))
                ):
                    applied_preflight = dict(preflight)
                    applied_preflight["vpn_external_path_verified"] = True
                    result_runtime_enforcement = build_applied_runtime_enforcement(
                        routing=manifest.get("routing_global_state") if isinstance(manifest.get("routing_global_state"), dict) else None,
                        preflight=applied_preflight,
                        live_mode_probe=live_mode_probe,
                        mode_override=apply_mode,
                    )
                phase_tracker.finish(
                    ok=live_mode_ok,
                    traffic_enforcement_guaranteed=result_runtime_enforcement["traffic_enforcement_guaranteed"],
                    enforcement_level=result_runtime_enforcement["enforcement_level"],
                    live_global_mode=live_mode_probe.get("mode"),
                    active_mode_matches_intent=live_mode_ok,
                )
                if not live_mode_ok:
                    phase_tracker.begin("rollback")
                    rollback = DEFAULT_DATAPLANE_ADAPTER.rollback(apply_plan)
                    rollback_result = {
                        "ok": rollback.ok,
                        "operation": rollback.operation.value,
                        "message": rollback.message,
                        "error_code": rollback.error_code,
                        "error_message": rollback.error_message,
                        "details": rollback.details,
                    }
                    phase_tracker.finish(
                        ok=rollback.ok,
                        error_code=rollback.error_code,
                        stage=rollback.details.get("stage") or rollback.details.get("error_stage"),
                    )
                    operation_result = type(apply_result)(
                        ok=False,
                        operation=apply_result.operation,
                        message=(
                            str(fast_subject_verify.get("error_message"))
                            if isinstance(fast_subject_verify, dict) and fast_subject_verify.get("error_message")
                            else (
                                f"Active dataplane mode mismatch after apply: expected {apply_mode}, "
                                f"got {live_mode_probe.get('mode') or 'unknown'}."
                            )
                        ),
                        details={
                            **apply_result.details,
                            "failed_runtime_enforcement": result_runtime_enforcement,
                        },
                        error_code=(
                            str(fast_subject_verify.get("error_code"))
                            if isinstance(fast_subject_verify, dict) and fast_subject_verify.get("error_code")
                            else "ACTIVE_DATAPLANE_MODE_MISMATCH"
                        ),
                        error_message=(
                            str(fast_subject_verify.get("error_message"))
                            if isinstance(fast_subject_verify, dict) and fast_subject_verify.get("error_message")
                            else f"Active nftables classify chain does not match requested mode {apply_mode}."
                        ),
                    )
                elif (
                    not bypass_requested
                    and apply_mode == "vpn"
                    and not result_runtime_enforcement["traffic_enforcement_guaranteed"]
                ):
                    phase_tracker.begin("rollback")
                    rollback = DEFAULT_DATAPLANE_ADAPTER.rollback(apply_plan)
                    rollback_result = {
                        "ok": rollback.ok,
                        "operation": rollback.operation.value,
                        "message": rollback.message,
                        "error_code": rollback.error_code,
                        "error_message": rollback.error_message,
                        "details": rollback.details,
                    }
                    phase_tracker.finish(
                        ok=rollback.ok,
                        error_code=rollback.error_code,
                        stage=rollback.details.get("stage") or rollback.details.get("error_stage"),
                    )
                    operation_result = type(apply_result)(
                        ok=False,
                        operation=apply_result.operation,
                        message="Global VPN apply did not produce verified VPN enforcement.",
                        details={
                            **apply_result.details,
                            "vpn_external_path_verified": False,
                            "failed_runtime_enforcement": result_runtime_enforcement,
                        },
                        error_code="GLOBAL_VPN_VERIFY_FAILED",
                        error_message="Global VPN apply finished without verified VPN path.",
                    )
                else:
                    _require_job_running(job_id, phase="promote_manifest")
                    phase_tracker.begin("promote_manifest")
                    applied_manifest = dict(manifest)
                    applied_manifest["runtime_enforcement"] = result_runtime_enforcement
                    applied_manifest["global_preflight"] = applied_preflight
                    promote_last_good(manifest=applied_manifest, artifact_paths=manifest_paths)
                    phase_tracker.finish(
                        applied_manifest_path=manifest_paths["applied_manifest_path"],
                        current_manifest_path=manifest_paths["current_manifest_path"],
                    )
    elif mode == ApplyMode.DRY_RUN:
        stage = str(check_result.details.get("stage") or check_result.details.get("error_stage") or "check")

    if mode == ApplyMode.APPLY and operation_result.ok:
        try:
            _require_job_running(job_id, phase="finalize")
            touch_job_running(job_id)
        except ApplyJobAbortedError as exc:
            if "apply_plan" in locals():
                phase_tracker.begin("rollback")
                rollback = DEFAULT_DATAPLANE_ADAPTER.rollback(apply_plan)
                rollback_result = {
                    "ok": rollback.ok,
                    "operation": rollback.operation.value,
                    "message": rollback.message,
                    "error_code": rollback.error_code,
                    "error_message": rollback.error_message,
                    "details": rollback.details,
                }
                phase_tracker.finish(
                    ok=rollback.ok,
                    error_code=rollback.error_code,
                    stage=rollback.details.get("stage") or rollback.details.get("error_stage"),
                )
            operation_result = type(operation_result)(
                ok=False,
                operation=operation_result.operation,
                message=str(exc),
                details={**operation_result.details, "aborted_phase": "finalize"},
                error_code="APPLY_JOB_ABORTED",
                error_message=str(exc),
            )
            stage = "finalize"

    result = {
        "ok": operation_result.ok,
        "apply_id": plan["apply_id"],
        "job_id": job_id,
        "mode": mode.value,
        "reason": reason,
        "dataplane_capability": result_runtime_enforcement["dataplane_capability"],
        "enforcement_level": result_runtime_enforcement["enforcement_level"],
        "traffic_enforcement_guaranteed": result_runtime_enforcement["traffic_enforcement_guaranteed"],
        "supported_modes": result_runtime_enforcement.get("supported_modes", {}),
        "missing_runtime_requirements": result_runtime_enforcement.get("missing_runtime_requirements", []),
        "stage": stage,
        "manifest": {
            "summary": manifest["summary"],
            "paths": manifest_paths,
            "contract_version": manifest["contract_version"],
            "owned_table": manifest["owned_table"],
            "required_chains": manifest["required_chains"],
            "generated_at": manifest["generated_at"],
            "profile": manifest.get("dataplane_profile"),
        },
        "scoped_egress": manifest.get("scoped_egress", {}),
        "preflight": preflight,
        "dataplane": {
            "ok": operation_result.ok,
            "operation": operation_result.operation.value,
            "message": operation_result.message,
            "error_code": operation_result.error_code,
            "error_message": operation_result.error_message,
            "details": operation_result.details,
        },
        "rollback": rollback_result,
    }

    write_job_json_artifact(job_id, "dataplane/result.json", result)
    atomic_write_json(_result_manifest_path(), result)

    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO apply_versions (
                apply_id,
                job_id,
                manifest_path,
                artifact_dir,
                promoted_at,
                status,
                summary_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(apply_id) DO UPDATE SET
                manifest_path = excluded.manifest_path,
                artifact_dir = excluded.artifact_dir,
                promoted_at = excluded.promoted_at,
                status = excluded.status,
                summary_json = excluded.summary_json
            """,
            (
                plan["apply_id"],
                job_id,
                manifest_paths["versioned_manifest_path"],
                plan["artifacts"]["artifact_dir"],
                manifest["generated_at"] if result["ok"] and mode == ApplyMode.APPLY else None,
                (
                    "generated"
                    if mode == ApplyMode.DRY_RUN
                    else (
                        "applied"
                        if result["ok"]
                        else ("rolled_back" if rollback_result and rollback_result["ok"] else "failed")
                    )
                ),
                json.dumps(
                    {
                        "mode": mode.value,
                        "reason": reason,
                        "path_counts": manifest["summary"]["path_counts"],
                        "dataplane_capability": result_runtime_enforcement["dataplane_capability"],
                        "enforcement_level": result_runtime_enforcement["enforcement_level"],
                        "owned_table": manifest["owned_table"],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            ),
        )

    if result["ok"]:
        if mode == ApplyMode.APPLY:
            prime_runtime_read_models_async(include_global_profiles=reason != "set_global_mode")
        write_operational_log(
            event_type="apply_dry_run_completed"
            if mode == ApplyMode.DRY_RUN
            else "apply_completed",
            message="Apply pipeline dry-run completed."
            if mode == ApplyMode.DRY_RUN
            else "Apply pipeline completed for the FWRouter-owned nftables table.",
            details={
                "job_id": job_id,
                "apply_id": plan["apply_id"],
                "mode": mode.value,
                "reason": reason,
                "owned_table": manifest["owned_table"],
            },
        )
    else:
        write_operational_log(
            event_type="apply_failed",
            level="warning",
            message="Apply pipeline failed in the FWRouter-owned nftables contour.",
            details={
                "job_id": job_id,
                "apply_id": plan["apply_id"],
                "mode": mode.value,
                "reason": reason,
                "stage": stage,
                "owned_table": manifest["owned_table"],
                "error_code": result["dataplane"]["error_code"],
                "error_message": result["dataplane"]["error_message"],
            },
        )

    return result
