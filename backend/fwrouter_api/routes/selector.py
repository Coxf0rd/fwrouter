from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from fwrouter_api.schemas import ApiResponse
from fwrouter_api.services.management_attribution import (
    build_incomplete_attribution_error,
    build_management_attribution,
)
from fwrouter_api.services.selector import (
    DEFAULT_ON_DEMAND_LIMIT,
    DEFAULT_ON_DEMAND_TIMEOUT_MS,
    get_vpn_auto_state,
    select_vpn_auto_server,
)


router = APIRouter()


class SwitchVpnAutoRequest(BaseModel):
    confirm_switch: bool = Field(default=False)
    exclude_active: bool = True
    update_ping_state: bool = True
    limit: int = Field(default=DEFAULT_ON_DEMAND_LIMIT, ge=1, le=20)
    timeout_ms: int = Field(default=DEFAULT_ON_DEMAND_TIMEOUT_MS, ge=1000, le=30000)
    reason: str | None = "api_controlled_switch"
    requested_by: str | None = "api"
    management_context: dict[str, object] | None = None


@router.get("/selector/vpn-auto", response_model=ApiResponse)
def get_vpn_auto_selector_endpoint(
    check_on_demand: bool = Query(default=False),
    exclude_active: bool = Query(default=False),
    update_ping_state: bool = Query(default=False),
    limit: int = Query(default=DEFAULT_ON_DEMAND_LIMIT, ge=1, le=20),
    timeout_ms: int = Query(default=DEFAULT_ON_DEMAND_TIMEOUT_MS, ge=1000, le=30000),
) -> ApiResponse:
    result = select_vpn_auto_server(
        apply=False,
        reason="api_selector_dry_run",
        check_on_demand=check_on_demand,
        update_ping_state=update_ping_state,
        on_demand_limit=limit,
        timeout_ms=timeout_ms,
        exclude_active=exclude_active,
    )
    return ApiResponse(ok=result["ok"], data={"selector": result})


@router.get("/selector/vpn-auto/state", response_model=ApiResponse)
def get_vpn_auto_selector_state_endpoint() -> ApiResponse:
    return ApiResponse(ok=True, data={"vpn_auto": get_vpn_auto_state()})


@router.post("/selector/vpn-auto/switch", response_model=ApiResponse)
def switch_vpn_auto_selector_endpoint(request: SwitchVpnAutoRequest) -> ApiResponse:
    if not request.confirm_switch:
        return ApiResponse(
            ok=False,
            data={},
            error={
                "code": "SWITCH_CONFIRMATION_REQUIRED",
                "message": "Set confirm_switch=true to switch Mihomo vpn-auto selector.",
            },
        )

    attribution = build_management_attribution(
        requested_by=request.requested_by or "api",
        context=request.management_context,
    )
    attribution_error = build_incomplete_attribution_error(attribution)
    if attribution_error is not None:
        return ApiResponse(
            ok=False,
            data={"management_attribution": attribution},
            error=attribution_error,
        )

    result = select_vpn_auto_server(
        apply=True,
        reason=request.reason or "api_controlled_switch",
        requested_by=request.requested_by or "api",
        management_context=request.management_context,
        check_on_demand=True,
        update_ping_state=request.update_ping_state,
        on_demand_limit=request.limit,
        timeout_ms=request.timeout_ms,
        exclude_active=request.exclude_active,
        post_check=True,
    )
    return ApiResponse(ok=result["ok"], data={"selector": result})
