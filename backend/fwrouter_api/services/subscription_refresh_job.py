from __future__ import annotations

from typing import Any

from fwrouter_api.jobs.manager import JobManager
from fwrouter_api.services.jobs import update_job_running_result
from fwrouter_api.services.subscription import compact_subscription_metadata
from fwrouter_api.services.subscription_pipeline import apply_subscription_refresh


SUBSCRIPTION_REFRESH_OPERATION = "subscription_refresh"
SUBSCRIPTION_REFRESH_LOCK_KEY = "subscription_refresh"
SUBSCRIPTION_REFRESH_STAGES = [
    "download",
    "parse",
    "persist",
    "prepare",
    "validate",
    "apply_runtime",
    "verify",
]


def _redact_subscription_state(state: dict[str, Any] | None) -> dict[str, Any] | None:
    if state is None:
        return None

    public = dict(state)
    public["url_saved"] = bool(public.get("url"))
    public.pop("url", None)

    metadata = public.get("metadata")
    if isinstance(metadata, dict):
        public["metadata"] = compact_subscription_metadata(metadata, redact_urls=True)

    return public


def _redact_validation(validation: dict[str, Any] | None) -> dict[str, Any] | None:
    if validation is None:
        return None

    public = dict(validation)
    public["url_saved"] = bool(public.get("normalized_url"))
    public.pop("normalized_url", None)
    return public


def _redact_adapter_refresh(refresh: dict[str, Any] | None) -> dict[str, Any] | None:
    if refresh is None:
        return None

    public = dict(refresh)
    servers = public.pop("servers", []) or []
    public["servers_count"] = len(servers)

    metadata = public.get("metadata")
    if isinstance(metadata, dict):
        metadata_public = dict(metadata)
        metadata_public.pop("url", None)
        public["metadata"] = metadata_public

    return public


def _redact_subscription_refresh_result(result: dict[str, Any]) -> dict[str, Any]:
    public = dict(result)
    refresh = dict(public.get("refresh") or {})

    refresh["validation"] = _redact_validation(refresh.get("validation"))
    refresh["state"] = _redact_subscription_state(refresh.get("state"))
    refresh["refresh"] = _redact_adapter_refresh(refresh.get("refresh"))

    batch = refresh.get("batch")
    if isinstance(batch, dict):
        compact_items = []
        for item in batch.get("items") or []:
            if not isinstance(item, dict):
                continue
            item_public = dict(item)
            item_public["refresh"] = _redact_adapter_refresh(item_public.get("refresh"))
            compact_items.append(item_public)
        refresh["batch"] = {**batch, "items": compact_items}

    public["refresh"] = refresh
    prepared_metadata = public.get("prepared_candidate_metadata")
    if isinstance(prepared_metadata, dict):
        public["prepared_candidate_metadata"] = {
            key: value
            for key, value in prepared_metadata.items()
            if not str(key).startswith("_")
        }
    candidate = public.get("candidate")
    if isinstance(candidate, dict):
        candidate_public = dict(candidate)
        candidate_public.pop("config", None)
        candidate_public.pop("_candidate_config", None)
        rules = candidate_public.pop("rules", None)
        handoff = candidate_public.pop("handoff_assignments", None)
        candidate_public.setdefault("rules_count", len(rules or []))
        candidate_public.setdefault("handoff_assignments_count", len(handoff or []))
        public["candidate"] = candidate_public
    return public


def subscription_refresh_stage(result: dict[str, Any]) -> str:
    """Map Phase 2 refresh internals to the long-operation stage contract."""

    if result.get("ok"):
        return "verify"

    stage = str(result.get("stage") or "").strip()
    error = result.get("error") if isinstance(result.get("error"), dict) else {}
    error_code = str(error.get("code") or result.get("error_code") or "").upper()

    if stage == "download_parse":
        if "PARSE" in error_code or "FORMAT" in error_code:
            return "parse"
        return "download"
    if stage == "config_validation":
        return "validate"
    if stage in {"candidate_validated", "prepare"}:
        return "prepare"
    if stage in {"applied", "already_current"}:
        return "verify"
    if stage in SUBSCRIPTION_REFRESH_STAGES:
        return stage
    return stage or "verify"


def _subscription_refresh_failure(
    *,
    job_id: str,
    stage: str,
    result: dict[str, Any],
) -> dict[str, Any]:
    error = result.get("error") if isinstance(result.get("error"), dict) else {}
    refresh = result.get("refresh") if isinstance(result.get("refresh"), dict) else {}
    refresh_error = refresh.get("error") if isinstance(refresh.get("error"), dict) else {}
    code = str(
        error.get("code")
        or refresh_error.get("code")
        or result.get("error_code")
        or "SUBSCRIPTION_REFRESH_FAILED"
    )
    message = str(
        error.get("message")
        or refresh_error.get("message")
        or result.get("error_message")
        or "Subscription refresh failed."
    )
    source = None
    validation = refresh.get("validation") if isinstance(refresh.get("validation"), dict) else {}
    if isinstance(validation, dict):
        source = {
            "url_saved": bool(validation.get("url_saved") or validation.get("normalized_url")),
        }

    return {
        "job_status": "failed",
        "job_id": job_id,
        "operation": SUBSCRIPTION_REFRESH_OPERATION,
        "stages": SUBSCRIPTION_REFRESH_STAGES,
        "stage": stage,
        "error_code": code,
        "error_message": message,
        "message": message,
        "source": source,
        "subscription": _redact_subscription_refresh_result(result),
    }


def run_subscription_refresh_job(job: dict[str, Any]) -> dict[str, Any]:
    """Run full job-backed subscription refresh through runtime verification."""

    job_id = str(job["job_id"])
    update_job_running_result(
        job_id,
        result={
            "job_status": "running",
            "job_id": job_id,
            "operation": SUBSCRIPTION_REFRESH_OPERATION,
            "stages": SUBSCRIPTION_REFRESH_STAGES,
            "stage": "download",
            "message": "Downloading subscription sources.",
        },
    )
    result = apply_subscription_refresh()
    stage = subscription_refresh_stage(result)

    if not result.get("ok"):
        return _subscription_refresh_failure(job_id=job_id, stage=stage, result=result)

    public = _redact_subscription_refresh_result(result)
    return {
        "job_status": "success",
        "job_id": job_id,
        "operation": SUBSCRIPTION_REFRESH_OPERATION,
        "stages": SUBSCRIPTION_REFRESH_STAGES,
        "stage": "verify",
        "message": "Subscription refresh completed after Mihomo runtime verification.",
        "runtime_verified": True,
        "subscription": public,
        "candidate": public.get("candidate"),
        "config_validation": public.get("config_validation"),
        "promoted": bool(public.get("promoted")),
        "container_restarted": bool(public.get("container_restarted")),
        "applied": bool(public.get("applied")),
        "reconcile_action": public.get("reconcile_action"),
        "reconcile_reason": public.get("reconcile_reason"),
    }


def register_subscription_refresh_handler(manager: JobManager) -> None:
    """Register/refresh the tracked subscription refresh job handler."""

    manager.register_handler(SUBSCRIPTION_REFRESH_OPERATION, run_subscription_refresh_job)
