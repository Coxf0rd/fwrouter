from __future__ import annotations

from typing import Any

from fwrouter_api.jobs.manager import get_default_job_manager
from fwrouter_api.services.jobs import JobLockConflictError
from fwrouter_api.services.rules import submit_rules_full_update
from fwrouter_api.services.subscription_pipeline import apply_subscription_refresh
from fwrouter_api.services.system_subjects import request_system_subject_sync
from fwrouter_api.services.xray import sync_xray_subjects


def _run_subject_inventory_sync(*, requested_by: str) -> dict[str, Any]:
    manager = get_default_job_manager()
    job = manager.create(
        "subject_inventory_sync",
        lock_key="subject_inventory_sync",
        requested_by=requested_by,
        input_data={
            "discover_docker": True,
            "discover_host": True,
            "discover_tailscale": True,
            "discover_xray": True,
        },
    )
    return manager.start_job_and_wait(job["job_id"]) or job


def run_full_refresh(*, requested_by: str = "api") -> dict[str, Any]:
    steps: list[dict[str, Any]] = []

    try:
        system_job = request_system_subject_sync(
            requested_by=requested_by,
            run_now=True,
            discover_docker=True,
            discover_host=True,
        )
        steps.append({"step": "system_subjects_sync", "ok": system_job.get("status") == "success", "job": system_job})
        if system_job.get("status") != "success":
            return {
                "ok": False,
                "stage": "system_subjects_sync",
                "steps": steps,
                "error": {
                    "code": system_job.get("error_code") or "SYSTEM_SUBJECTS_SYNC_FAILED",
                    "message": system_job.get("error_message") or "System subjects sync failed.",
                },
            }

        subjects_job = _run_subject_inventory_sync(requested_by=requested_by)
        steps.append({"step": "subjects_sync", "ok": subjects_job.get("status") == "success", "job": subjects_job})
        if subjects_job.get("status") != "success":
            return {
                "ok": False,
                "stage": "subjects_sync",
                "steps": steps,
                "error": {
                    "code": subjects_job.get("error_code") or "SUBJECTS_SYNC_FAILED",
                    "message": subjects_job.get("error_message") or "Subject inventory sync failed.",
                },
            }
    except JobLockConflictError as exc:
        return {
            "ok": False,
            "stage": "job_conflict",
            "steps": steps,
            "error": {
                "code": "JOB_CONFLICT",
                "message": f"Job lock is already active: {exc.lock_key}",
            },
            "active_job": exc.active_job,
        }

    xray_result = sync_xray_subjects(requested_by=requested_by)
    steps.append({"step": "xray_sync_subjects", "ok": bool(xray_result.get("ok")), "result": xray_result})
    if not xray_result.get("ok"):
        error = xray_result.get("error") or {}
        return {
            "ok": False,
            "stage": "xray_sync_subjects",
            "steps": steps,
            "error": {
                "code": error.get("code") or "XRAY_SYNC_FAILED",
                "message": error.get("message") or "Xray subject sync failed.",
            },
        }

    try:
        rules_job = submit_rules_full_update(requested_by=requested_by, run_now=True)
    except JobLockConflictError as exc:
        return {
            "ok": False,
            "stage": "job_conflict",
            "steps": steps,
            "error": {
                "code": "JOB_CONFLICT",
                "message": f"Job lock is already active: {exc.lock_key}",
            },
            "active_job": exc.active_job,
        }
    steps.append({"step": "rules_full_update", "ok": rules_job.get("status") == "success", "job": rules_job})
    if rules_job.get("status") != "success":
        return {
            "ok": False,
            "stage": "rules_full_update",
            "steps": steps,
            "error": {
                "code": rules_job.get("error_code") or "RULES_FULL_UPDATE_FAILED",
                "message": rules_job.get("error_message") or "Rules full update failed.",
            },
        }

    subscription = apply_subscription_refresh()
    subscription_ok = bool(subscription.get("ok"))
    steps.append({"step": "subscription_refresh", "ok": subscription_ok, "result": subscription, "optional": True})

    return {
        "ok": True,
        "stage": "completed_with_optional_step",
        "steps": steps,
        "subscription_optional_failure": None if subscription_ok else {
            "code": (subscription.get("error") or {}).get("code") or "SUBSCRIPTION_REFRESH_FAILED",
            "message": (subscription.get("error") or {}).get("message") or "Subscription refresh failed.",
        },
    }
