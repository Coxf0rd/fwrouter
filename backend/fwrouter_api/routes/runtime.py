from __future__ import annotations

from fastapi import APIRouter

from fwrouter_api.schemas import ApiResponse
from fwrouter_api.services.runtime import (
    get_runtime_summary,
    get_scoped_egress_runtime_summary,
)


router = APIRouter()


@router.get("/runtime", response_model=ApiResponse, summary='Get Runtime Status')
def get_runtime_endpoint() -> ApiResponse:
    runtime = get_runtime_summary()
    return ApiResponse(ok=True, data={"runtime": runtime})


@router.get("/runtime/scoped-egress", response_model=ApiResponse, summary='Get Scoped Egress Runtime')
def get_scoped_egress_runtime_endpoint() -> ApiResponse:
    scoped_egress = get_scoped_egress_runtime_summary()
    return ApiResponse(ok=True, data={"scoped_egress": scoped_egress})
