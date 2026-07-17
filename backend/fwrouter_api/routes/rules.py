from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from fwrouter_api.schemas import ApiResponse
from fwrouter_api.services.action_contract import (
    build_conflict_response,
    build_job_action_response,
)
from fwrouter_api.services.apply_orchestrator import submit_apply_mutation
from fwrouter_api.services.dataplane_status import build_runtime_enforcement_state
from fwrouter_api.services.jobs import JobLockConflictError, get_job
from fwrouter_api.services.rules import (
    get_effective_rules,
    get_rules_overview,
    get_rules_summary,
    save_manual_draft,
    submit_rules_full_update,
    validate_manual_rules,
)


router = APIRouter()


class ManualRulesRequest(BaseModel):
    text: str = Field(default="")


class ApplyManualRulesRequest(BaseModel):
    requested_by: str | None = "api"
    run_now: bool = True


class FullRulesUpdateRequest(BaseModel):
    requested_by: str | None = "api"
    run_now: bool = True


@router.get("/rules", response_model=ApiResponse)
def get_rules_endpoint() -> ApiResponse:
    overview = get_rules_overview()
    return ApiResponse(
        ok=True,
        data={
            "rules": overview,
            "runtime_enforcement": build_runtime_enforcement_state(),
        },
    )


@router.get("/rules/summary", response_model=ApiResponse)
def get_rules_summary_endpoint() -> ApiResponse:
    summary = get_rules_summary()
    return ApiResponse(ok=True, data={"rules": summary})


@router.get("/rules/effective", response_model=ApiResponse)
def get_effective_rules_endpoint() -> ApiResponse:
    return ApiResponse(ok=True, data={"rules": get_effective_rules()})


@router.post("/rules/manual/validate", response_model=ApiResponse)
def validate_manual_rules_endpoint(request: ManualRulesRequest) -> ApiResponse:
    validation = validate_manual_rules(request.text)
    return ApiResponse(
        ok=validation["valid"],
        data={"validation": validation},
        error=(
            {
                "code": "RULES_VALIDATION_FAILED",
                "message": "Manual rules validation failed.",
                "errors": validation["errors"],
            }
            if not validation["valid"]
            else None
        ),
    )


@router.post("/rules/manual", response_model=ApiResponse)
def save_manual_rules_draft_endpoint(request: ManualRulesRequest) -> ApiResponse:
    overview = save_manual_draft(request.text)
    validation: dict[str, Any] = overview["manual"]["draft_validation"]

    return ApiResponse(
        ok=validation["valid"],
        data={"rules": overview},
        error=(
            {
                "code": "RULES_VALIDATION_FAILED",
                "message": "Manual rules draft was saved, but validation failed.",
                "errors": validation["errors"],
            }
            if not validation["valid"]
            else None
        ),
    )


@router.post("/rules/manual/apply", response_model=ApiResponse)
def apply_manual_rules_endpoint(request: ApplyManualRulesRequest) -> ApiResponse:
    try:
        job = submit_apply_mutation(
            intent="apply_manual_rules",
            payload={},
            requested_by=request.requested_by or "api",
            run_now=request.run_now,
        )
    except JobLockConflictError as exc:
        return build_conflict_response(exc)

    return build_job_action_response(job, result_key="rules_apply")


@router.post("/rules/full-update", response_model=ApiResponse)
def rules_full_update_endpoint(request: FullRulesUpdateRequest) -> ApiResponse:
    try:
        job = submit_rules_full_update(
            requested_by=request.requested_by or "api",
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

    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    payload = {
        "job_id": job["job_id"],
        "status": job["status"],
        "stage": result.get("stage"),
        "rules_state": result.get("rules_state"),
    }

    if request.run_now and job["status"] == "failed":
        return ApiResponse(
            ok=False,
            data={"job": job, **payload},
            error={
                "code": job.get("error_code") or result.get("error_code"),
                "message": job.get("error_message") or result.get("error_message"),
            },
        )

    return ApiResponse(ok=True, data={"job": job, **payload})


@router.get("/rules/jobs/{job_id}", response_model=ApiResponse)
def get_rules_job_endpoint(job_id: str) -> ApiResponse:
    job = get_job(job_id)
    if job is None:
        return ApiResponse(
            ok=False,
            error={
                "code": "JOB_NOT_FOUND",
                "message": f"Job not found: {job_id}",
            },
        )
    return ApiResponse(ok=True, data={"job": job})
