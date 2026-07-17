from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from fwrouter_api.jobs.manager import get_default_job_manager
from fwrouter_api.schemas import ApiResponse
from fwrouter_api.services.jobs import JobLockConflictError
from fwrouter_api.services.traffic import (
    get_traffic_accounting_state,
    list_monthly_traffic,
)


router = APIRouter()


class TrafficCounterSampleRequest(BaseModel):
    counter_key: str
    subject_id: str
    path: str
    rx_bytes: int = Field(ge=0)
    tx_bytes: int = Field(ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class TrafficCollectRequest(BaseModel):
    requested_by: str | None = "api"
    collector: str | None = "api"
    dry_run: bool = False
    run_now: bool = True
    use_script: bool = True
    script_id: str = "traffic_collect"
    extra_args: list[str] = Field(default_factory=list)
    samples: list[TrafficCounterSampleRequest] = Field(default_factory=list)


@router.get("/traffic/state", response_model=ApiResponse)
def get_traffic_state_endpoint() -> ApiResponse:
    return ApiResponse(ok=True, data={"traffic": get_traffic_accounting_state()})


@router.get("/traffic/monthly", response_model=ApiResponse)
def list_monthly_traffic_endpoint(
    subject_id: str | None = None,
    period_month: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> ApiResponse:
    return ApiResponse(
        ok=True,
        data={
            "traffic_monthly": list_monthly_traffic(
                subject_id=subject_id,
                period_month=period_month,
                limit=limit,
            )
        },
    )


@router.post("/traffic/collect", response_model=ApiResponse)
def collect_traffic_endpoint(request: TrafficCollectRequest) -> ApiResponse:
    manager = get_default_job_manager()
    input_data = {
        "collector": request.collector,
        "dry_run": request.dry_run,
        "use_script": request.use_script,
        "script_id": request.script_id,
        "extra_args": request.extra_args,
        "samples": [sample.model_dump() for sample in request.samples],
    }

    try:
        job = manager.create(
            "traffic_accounting_collect",
            lock_key="traffic_accounting_collect",
            requested_by=request.requested_by,
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

    if request.run_now:
        job = manager.start_job_and_wait(job["job_id"]) or job

    return ApiResponse(ok=True, data={"job": job})
