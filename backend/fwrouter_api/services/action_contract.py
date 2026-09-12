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
    elif isinstance(result, dict) and result_key in result:
        result_payload = result.get(result_key)
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
        global_fixed = result.get(result_key) if isinstance(result.get(result_key), dict) else {}
        error = {
            "code": result.get("error_code") or global_fixed.get("error_code") or job.get("error_code") or "JOB_FAILED",
            "message": result.get("error_message") or global_fixed.get("error_message") or job.get("error_message") or "Job failed.",
            "stage": result.get("stage") or global_fixed.get("stage"),
            "server_id": result.get("server_id") or global_fixed.get("server_id"),
            "job_id": job.get("job_id"),
            "job_type": job.get("job_type"),
        }
        payload["error"] = error
        return ApiResponse(ok=False, data=payload, error=error)

    return ApiResponse(ok=True, data=payload)
