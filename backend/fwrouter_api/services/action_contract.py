from __future__ import annotations

from typing import Any

from fwrouter_api.schemas import ApiResponse


FINAL_JOB_STATUSES = {"success", "failed", "cancelled"}


def build_conflict_response(exc: Any) -> ApiResponse:
    active_job = dict(getattr(exc, "active_job", None) or {})
    return ApiResponse(
        ok=False,
        data={
            "job": None,
            "status": "conflict",
            "error": {
                "code": "JOB_CONFLICT",
                "message": f"Job lock is already active: {getattr(exc, 'lock_key', 'unknown')}",
            },
            "conflict": {
                "active_job": active_job,
            },
            "active_job": active_job,
        },
        error={
            "code": "JOB_CONFLICT",
            "message": f"Job lock is already active: {getattr(exc, 'lock_key', 'unknown')}",
        },
    )


def build_job_action_response(
    job: dict[str, Any],
    *,
    result_key: str,
) -> ApiResponse:
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    mutation = result.get("mutation") if isinstance(result, dict) else None
    status = str(job.get("status") or "queued")
    result_payload = mutation or result or None
    if isinstance(mutation, dict) and result_key in mutation:
        result_payload = mutation.get(result_key)
    payload: dict[str, Any] = {
        "job": job,
        "status": status,
        "error": None,
        "conflict": None,
        result_key: result_payload,
    }

    if status in {"queued", "running"}:
        return ApiResponse(ok=True, data=payload)

    if isinstance(mutation, dict) and not mutation.get("ok", False):
        error = {
            "code": mutation.get("code") or job.get("error_code") or "JOB_FAILED",
            "message": mutation.get("message") or job.get("error_message") or "Mutation job failed.",
        }
        payload["error"] = error
        return ApiResponse(ok=False, data=payload, error=error)

    if status == "failed":
        error = {
            "code": job.get("error_code") or "JOB_FAILED",
            "message": job.get("error_message") or "Mutation job failed.",
        }
        payload["error"] = error
        return ApiResponse(ok=False, data=payload, error=error)

    return ApiResponse(ok=True, data=payload)
