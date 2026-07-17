from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from fwrouter_api.schemas import ApiResponse
from fwrouter_api.services.core_bypass import (
    JobLockConflictError,
    get_core_bypass_state,
    submit_core_bypass_job,
)


router = APIRouter()


class CoreBypassActionRequest(BaseModel):
    requested_by: str | None = "api"
    reason: str | None = None
    confirm_apply: bool = False
    run_now: bool = True


def _available_actions(state: dict[str, object]) -> dict[str, bool]:
    enabled = bool(state.get("enabled"))
    return {
        "enable": not enabled,
        "disable": enabled,
    }


@router.get("/core/bypass", response_model=ApiResponse)
def get_core_bypass_endpoint() -> ApiResponse:
    state = get_core_bypass_state()
    return ApiResponse(
        ok=True,
        data={
            "bypass": state,
            "available_actions": _available_actions(state),
        },
    )


@router.post("/core/bypass/enable", response_model=ApiResponse)
def enable_core_bypass_endpoint(request: CoreBypassActionRequest) -> ApiResponse:
    if not request.confirm_apply:
        return ApiResponse(
            ok=False,
            data={},
            error={
                "code": "CORE_BYPASS_CONFIRMATION_REQUIRED",
                "message": "Set confirm_apply=true to enable FWRouter core bypass.",
            },
        )

    try:
        job = submit_core_bypass_job(
            action="enable",
            requested_by=request.requested_by or "api",
            reason=request.reason or "api_core_bypass_enable",
            run_now=request.run_now,
        )
    except JobLockConflictError as exc:
        return ApiResponse(
            ok=False,
            data={"active_job": exc.active_job},
            error={
                "code": "JOB_CONFLICT",
                "message": f"Job lock is already active: {exc.lock_key}",
            },
        )

    if not request.run_now:
        return ApiResponse(ok=True, data={"job": job})
    if job.get("status") == "running":
        return ApiResponse(ok=True, data={"job": job})

    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    if job.get("status") == "failed":
        return ApiResponse(
            ok=False,
            data={"job": job, "bypass_action": result},
            error={
                "code": job.get("error_code") or "CORE_BYPASS_ENABLE_FAILED",
                "message": job.get("error_message") or "Failed to enable FWRouter core bypass.",
            },
        )

    return ApiResponse(ok=True, data={"job": job, "bypass_action": result})


@router.post("/core/bypass/disable", response_model=ApiResponse)
def disable_core_bypass_endpoint(request: CoreBypassActionRequest) -> ApiResponse:
    if not request.confirm_apply:
        return ApiResponse(
            ok=False,
            data={},
            error={
                "code": "CORE_BYPASS_CONFIRMATION_REQUIRED",
                "message": "Set confirm_apply=true to disable FWRouter core bypass.",
            },
        )

    try:
        job = submit_core_bypass_job(
            action="disable",
            requested_by=request.requested_by or "api",
            reason=request.reason or "api_core_bypass_disable",
            run_now=request.run_now,
        )
    except JobLockConflictError as exc:
        return ApiResponse(
            ok=False,
            data={"active_job": exc.active_job},
            error={
                "code": "JOB_CONFLICT",
                "message": f"Job lock is already active: {exc.lock_key}",
            },
        )

    if not request.run_now:
        return ApiResponse(ok=True, data={"job": job})
    if job.get("status") == "running":
        return ApiResponse(ok=True, data={"job": job})

    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    if job.get("status") == "failed":
        return ApiResponse(
            ok=False,
            data={"job": job, "bypass_action": result},
            error={
                "code": job.get("error_code") or "CORE_BYPASS_DISABLE_FAILED",
                "message": job.get("error_message") or "Failed to disable FWRouter core bypass.",
            },
        )

    return ApiResponse(ok=True, data={"job": job, "bypass_action": result})
