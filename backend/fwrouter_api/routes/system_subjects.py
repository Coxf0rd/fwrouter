from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel

from fwrouter_api.schemas import ApiResponse
from fwrouter_api.services.action_contract import (
    build_conflict_response,
    build_job_action_response,
)
from fwrouter_api.services.apply_orchestrator import submit_apply_mutation
from fwrouter_api.services.jobs import JobLockConflictError
from fwrouter_api.services.system_subjects import (
    delete_system_subject,
    get_system_subject,
    list_system_subjects,
    request_system_subject_sync,
)


router = APIRouter()


class SetSystemSubjectModeRequest(BaseModel):
    mode: str
    requested_by: str | None = "api"
    run_now: bool = True


class SystemSubjectSyncRequest(BaseModel):
    requested_by: str | None = "api"
    run_now: bool = True
    discover_docker: bool = True
    discover_host: bool = True


@router.get("/system-subjects", response_model=ApiResponse)
def list_system_subjects_endpoint(
    is_active: bool | None = None,
    include_deleted: bool = False,
    limit: int = Query(default=100, ge=1, le=500),
) -> ApiResponse:
    subjects = list_system_subjects(
        is_active=is_active,
        include_deleted=include_deleted,
        limit=limit,
    )
    return ApiResponse(ok=True, data={"subjects": subjects})


@router.get("/system-subjects/{subject_id}", response_model=ApiResponse)
def get_system_subject_endpoint(subject_id: str) -> ApiResponse:
    subject = get_system_subject(subject_id)
    if subject is None:
        return ApiResponse(
            ok=False,
            data={},
            error={
                "code": "SYSTEM_SUBJECT_NOT_FOUND",
                "message": f"System subject not found: {subject_id}",
            },
        )
    return ApiResponse(ok=True, data={"subject": subject})


@router.post("/system-subjects/{subject_id}/mode", response_model=ApiResponse)
def set_system_subject_mode_endpoint(
    subject_id: str,
    request: SetSystemSubjectModeRequest,
) -> ApiResponse:
    try:
        job = submit_apply_mutation(
            intent="set_subject_admin_mode",
            payload={"subject_id": subject_id, "mode": request.mode},
            requested_by=request.requested_by or "api",
            run_now=request.run_now,
        )
    except JobLockConflictError as exc:
        return build_conflict_response(exc)

    return build_job_action_response(job, result_key="subject_mode")


@router.post("/system-subjects/sync", response_model=ApiResponse)
def sync_system_subjects_endpoint(request: SystemSubjectSyncRequest) -> ApiResponse:
    try:
        job = request_system_subject_sync(
            requested_by=request.requested_by or "api",
            run_now=request.run_now,
            discover_docker=request.discover_docker,
            discover_host=request.discover_host,
        )
    except JobLockConflictError as exc:
        return build_conflict_response(exc)

    return ApiResponse(ok=True, data={"job": job})


@router.delete("/system-subjects/{subject_id}", response_model=ApiResponse)
def delete_system_subject_endpoint(subject_id: str, requested_by: str = "api") -> ApiResponse:
    result = delete_system_subject(subject_id, requested_by=requested_by)
    if not result["ok"]:
        return ApiResponse(
            ok=False,
            data={"subject": result.get("subject")},
            error={
                "code": result["error_code"],
                "message": result["error_message"],
            },
        )
    return ApiResponse(ok=True, data=result)
