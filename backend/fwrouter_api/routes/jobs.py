from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from fwrouter_api.jobs.manager import get_default_job_manager
from fwrouter_api.schemas import ApiResponse
from fwrouter_api.services.action_contract import build_conflict_response
from fwrouter_api.services.jobs import (
    JobLockConflictError,
    get_job,
    list_jobs,
)


router = APIRouter()

SAFE_API_JOB_TYPES = {"noop", "runtime_probe", "apply_dry_run", "subscription_refresh_prepare", "jobs_retention_cleanup", "server_ping_sweep"}


class CreateJobRequest(BaseModel):
    job_type: str
    lock_key: str | None = None
    requested_by: str | None = "api"
    input_data: dict[str, Any] | None = Field(default=None)
    run_now: bool = False


@router.get("/jobs", response_model=ApiResponse)
def list_recent_jobs(
    limit: int = Query(default=50, ge=1, le=200),
    job_type: str | None = None,
    status: str | None = None,
) -> ApiResponse:
    jobs = list_jobs(limit=limit, job_type=job_type, status=status)
    return ApiResponse(ok=True, data={"jobs": jobs})


@router.post("/jobs", response_model=ApiResponse)
def create_job_endpoint(request: CreateJobRequest) -> ApiResponse:
    if request.job_type not in SAFE_API_JOB_TYPES:
        return ApiResponse(
            ok=False,
            data={},
            error={
                "code": "JOB_TYPE_NOT_ALLOWED",
                "message": f"Job type is not allowed through API: {request.job_type}",
            },
        )

    if (
        request.job_type == "jobs_retention_cleanup"
        and (request.input_data or {}).get("dry_run") is False
    ):
        return ApiResponse(
            ok=False,
            data={},
            error={
                "code": "JOB_TYPE_NOT_ALLOWED",
                "message": "jobs_retention_cleanup can only run as dry_run through generic Jobs API.",
            },
        )

    if (
        request.job_type == "server_ping_sweep"
        and (request.input_data or {}).get("update_state") is True
    ):
        return ApiResponse(
            ok=False,
            data={},
            error={
                "code": "JOB_TYPE_NOT_ALLOWED",
                "message": "server_ping_sweep can only run as dry-run through generic Jobs API.",
            },
        )

    manager = get_default_job_manager()

    try:
        job = manager.create(
            request.job_type,
            lock_key=request.lock_key,
            requested_by=request.requested_by,
            input_data=request.input_data,
        )
    except JobLockConflictError as exc:
        return build_conflict_response(exc)

    if request.run_now:
        job = manager.start_job_and_wait(job["job_id"]) or job

    return ApiResponse(
        ok=True,
        data={
            "job": job,
            "status": job.get("status"),
            "error": (
                {
                    "code": job.get("error_code"),
                    "message": job.get("error_message"),
                }
                if job.get("status") == "failed"
                else None
            ),
        },
    )


@router.get("/jobs/{job_id}", response_model=ApiResponse)
def get_job_detail(job_id: str) -> ApiResponse:
    job = get_job(job_id)

    if job is None:
        return ApiResponse(
            ok=False,
            data={},
            error={
                "code": "JOB_NOT_FOUND",
                "message": f"Job not found: {job_id}",
            },
        )

    return ApiResponse(
        ok=True,
        data={
            "job": job,
            "status": job.get("status"),
            "error": (
                {
                    "code": job.get("error_code"),
                    "message": job.get("error_message"),
                }
                if job.get("status") == "failed"
                else None
            ),
        },
    )


@router.post("/jobs/{job_id}/run", response_model=ApiResponse)
def run_job_endpoint(job_id: str) -> ApiResponse:
    manager = get_default_job_manager()
    job = manager.get_job(job_id)

    if job is None:
        return ApiResponse(
            ok=False,
            data={},
            error={
                "code": "JOB_NOT_FOUND",
                "message": f"Job not found: {job_id}",
            },
        )

    if job["job_type"] not in SAFE_API_JOB_TYPES:
        return ApiResponse(
            ok=False,
            data={"job": job},
            error={
                "code": "JOB_TYPE_NOT_ALLOWED",
                "message": f"Job type is not allowed through API: {job['job_type']}",
            },
        )

    if job["job_type"] == "jobs_retention_cleanup":
        input_data = job.get("input") or {}
        if input_data.get("dry_run") is False:
            return ApiResponse(
                ok=False,
                data={"job": job},
                error={
                    "code": "JOB_TYPE_NOT_ALLOWED",
                    "message": "jobs_retention_cleanup can only run as dry_run through generic Jobs API.",
                },
            )

    if job["job_type"] == "server_ping_sweep":
        input_data = job.get("input") or {}
        if input_data.get("update_state") is True:
            return ApiResponse(
                ok=False,
                data={"job": job},
                error={
                    "code": "JOB_TYPE_NOT_ALLOWED",
                    "message": "server_ping_sweep can only run as dry-run through generic Jobs API.",
                },
            )

    result = manager.start_job_and_wait(job_id)

    return ApiResponse(
        ok=True,
        data={
            "job": result,
            "status": result.get("status") if isinstance(result, dict) else None,
            "error": (
                {
                    "code": result.get("error_code"),
                    "message": result.get("error_message"),
                }
                if isinstance(result, dict) and result.get("status") == "failed"
                else None
            ),
        },
    )
