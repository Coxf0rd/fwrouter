from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from fwrouter_api.schemas import ApiResponse
from fwrouter_api.services.watchdog import (
    DEFAULT_WATCHDOG_CANDIDATE_LIMIT,
    DEFAULT_WATCHDOG_TIMEOUT_MS,
    run_vpn_watchdog_auto_check,
    run_vpn_watchdog_check,
)


router = APIRouter()


class VpnWatchdogCheckRequest(BaseModel):
    traffic_attempts_observed: bool = False
    allow_switch: bool = False
    confirm_switch: bool = False
    update_ping_state: bool = True
    timeout_ms: int = Field(default=DEFAULT_WATCHDOG_TIMEOUT_MS, ge=1000, le=30000)
    candidate_limit: int = Field(default=DEFAULT_WATCHDOG_CANDIDATE_LIMIT, ge=1, le=20)
    reason: str | None = "api_watchdog_check"
    log_events: bool = False


class VpnWatchdogAutoCheckRequest(BaseModel):
    allow_switch: bool = True
    confirm_switch: bool = False
    update_ping_state: bool = True
    timeout_ms: int = Field(default=DEFAULT_WATCHDOG_TIMEOUT_MS, ge=1000, le=30000)
    candidate_limit: int = Field(default=DEFAULT_WATCHDOG_CANDIDATE_LIMIT, ge=1, le=20)
    traffic_window_seconds: int | None = Field(default=None, ge=30, le=3600)
    reason: str | None = "api_watchdog_auto_check"
    log_events: bool = False


@router.post("/watchdog/vpn/check", response_model=ApiResponse)
def check_vpn_watchdog_endpoint(request: VpnWatchdogCheckRequest) -> ApiResponse:
    if request.allow_switch and not request.confirm_switch:
        return ApiResponse(
            ok=False,
            data={},
            error={
                "code": "WATCHDOG_SWITCH_CONFIRMATION_REQUIRED",
                "message": "Set confirm_switch=true to allow watchdog to switch Mihomo vpn-auto selector.",
            },
        )

    result = run_vpn_watchdog_check(
        traffic_attempts_observed=request.traffic_attempts_observed,
        allow_switch=request.allow_switch,
        update_ping_state=request.update_ping_state,
        timeout_ms=request.timeout_ms,
        candidate_limit=request.candidate_limit,
        reason=request.reason or "api_watchdog_check",
        log_events=request.log_events,
    )

    return ApiResponse(ok=result["ok"], data={"watchdog": result})


@router.post("/watchdog/vpn/auto-check", response_model=ApiResponse)
def check_vpn_watchdog_auto_endpoint(request: VpnWatchdogAutoCheckRequest) -> ApiResponse:
    if request.allow_switch and not request.confirm_switch:
        return ApiResponse(
            ok=False,
            data={},
            error={
                "code": "WATCHDOG_SWITCH_CONFIRMATION_REQUIRED",
                "message": "Set confirm_switch=true to allow watchdog to switch Mihomo vpn-auto selector.",
            },
        )

    result = run_vpn_watchdog_auto_check(
        allow_switch=request.allow_switch,
        update_ping_state=request.update_ping_state,
        timeout_ms=request.timeout_ms,
        candidate_limit=request.candidate_limit,
        traffic_window_seconds=request.traffic_window_seconds,
        reason=request.reason or "api_watchdog_auto_check",
        log_events=request.log_events,
    )

    return ApiResponse(ok=result["ok"], data={"watchdog": result})
