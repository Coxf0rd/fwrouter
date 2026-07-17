from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from fwrouter_api.schemas import ApiResponse
from fwrouter_api.services.server_ping import (
    DEFAULT_SWEEP_LIMIT,
    DEFAULT_TIMEOUT_MS,
    check_active_server_delay,
    check_server_delay_sweep,
)


router = APIRouter()


class CheckActiveServerPingRequest(BaseModel):
    checked_by: str | None = "api"
    timeout_ms: int = DEFAULT_TIMEOUT_MS


class ServerPingSweepRequest(BaseModel):
    checked_by: str | None = "api_sweep"
    timeout_ms: int = DEFAULT_TIMEOUT_MS
    limit: int = Field(default=DEFAULT_SWEEP_LIMIT, ge=1, le=20)


@router.get("/server-ping/active", response_model=ApiResponse)
def get_active_server_ping_endpoint() -> ApiResponse:
    result = check_active_server_delay(
        update_state=False,
        checked_by="api_dry_run",
    )
    return ApiResponse(ok=result["ok"], data={"ping": result})


@router.post("/server-ping/active", response_model=ApiResponse)
def update_active_server_ping_endpoint(
    request: CheckActiveServerPingRequest,
) -> ApiResponse:
    result = check_active_server_delay(
        update_state=True,
        checked_by=request.checked_by or "api",
        timeout_ms=request.timeout_ms,
    )
    return ApiResponse(ok=result["ok"], data={"ping": result})


@router.get("/server-ping/sweep", response_model=ApiResponse)
def get_server_ping_sweep_endpoint(
    limit: int = Query(default=DEFAULT_SWEEP_LIMIT, ge=1, le=20),
) -> ApiResponse:
    result = check_server_delay_sweep(
        update_state=False,
        checked_by="api_sweep_dry_run",
        limit=limit,
    )
    return ApiResponse(ok=result["ok"], data={"sweep": result})


@router.post("/server-ping/sweep", response_model=ApiResponse)
def update_server_ping_sweep_endpoint(
    request: ServerPingSweepRequest,
) -> ApiResponse:
    result = check_server_delay_sweep(
        update_state=True,
        checked_by=request.checked_by or "api_sweep",
        timeout_ms=request.timeout_ms,
        limit=request.limit,
    )
    return ApiResponse(ok=result["ok"], data={"sweep": result})
