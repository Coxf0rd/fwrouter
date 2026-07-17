from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from fwrouter_api.jobs.manager import get_default_job_manager
from fwrouter_api.schemas import ApiResponse
from fwrouter_api.services.full_refresh import run_full_refresh
from fwrouter_api.services.jobs import JobLockConflictError


router = APIRouter()


class ApplyDryRunRequest(BaseModel):
    requested_by: str | None = "api"
    input_data: dict[str, Any] | None = Field(default=None)
    run_now: bool = True


class MaintenanceCleanupRequest(BaseModel):
    requested_by: str | None = "api"
    dry_run: bool = True
    run_now: bool = True


class FullRefreshRequest(BaseModel):
    requested_by: str | None = "api"


def _create_or_conflict(
    *,
    job_type: str,
    lock_key: str,
    requested_by: str | None,
    input_data: dict[str, Any] | None,
    run_now: bool,
) -> ApiResponse:
    manager = get_default_job_manager()

    try:
        job = manager.create(
            job_type,
            lock_key=lock_key,
            requested_by=requested_by,
            input_data=input_data,
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

    if run_now:
        job = manager.start_job_and_wait(job["job_id"]) or job

    return ApiResponse(ok=True, data={"job": job})


@router.post("/apply/dry-run", response_model=ApiResponse)
def apply_control_plane_dry_run_endpoint(request: ApplyDryRunRequest) -> ApiResponse:
    return _create_or_conflict(
        job_type="apply_control_plane_dry_run",
        lock_key="apply_control_plane_dry_run",
        requested_by=request.requested_by,
        input_data=request.input_data,
        run_now=request.run_now,
    )


@router.post("/maintenance/cleanup", response_model=ApiResponse)
def maintenance_cleanup_endpoint(request: MaintenanceCleanupRequest) -> ApiResponse:
    return _create_or_conflict(
        job_type="maintenance_cleanup",
        lock_key="maintenance_cleanup",
        requested_by=request.requested_by,
        input_data={"dry_run": request.dry_run},
        run_now=request.run_now,
    )


@router.post("/full-refresh", response_model=ApiResponse)
def full_refresh_endpoint(request: FullRefreshRequest) -> ApiResponse:
    result = run_full_refresh(requested_by=request.requested_by or "api")
    if result["ok"]:
        return ApiResponse(ok=True, data={"refresh": result})
    return ApiResponse(
        ok=False,
        data={"refresh": result},
        error=result.get("error"),
    )
