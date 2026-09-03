from __future__ import annotations

from fastapi import APIRouter, Query

from fwrouter_api.schemas import ApiResponse
from fwrouter_api.services.state_projection import (
    build_module_state_projection,
    build_routing_state_projection,
    build_rules_state_projection,
    build_subject_state_projection,
    build_system_state_projection,
    build_vpn_state_projection,
    build_watchdog_state_projection,
    build_xray_state_projection,
)


router = APIRouter()


@router.get("/state/system", response_model=ApiResponse)
def get_state_system_endpoint() -> ApiResponse:
    return ApiResponse(ok=True, data={"state": build_system_state_projection()})


@router.get("/state/modules", response_model=ApiResponse)
def get_state_modules_endpoint() -> ApiResponse:
    return ApiResponse(ok=True, data={"modules": build_module_state_projection()})


@router.get("/state/subjects", response_model=ApiResponse)
def get_state_subjects_endpoint(
    include_deleted: bool = Query(default=False),
    limit: int = Query(default=500, ge=1, le=500),
) -> ApiResponse:
    return ApiResponse(
        ok=True,
        data={
            "subjects": build_subject_state_projection(
                include_deleted=include_deleted,
                limit=limit,
            )
        },
    )


@router.get("/state/subjects/{subject_id}", response_model=ApiResponse)
def get_state_subject_endpoint(
    subject_id: str,
    include_deleted: bool = Query(default=False),
) -> ApiResponse:
    result = build_subject_state_projection(
        subject_id=subject_id,
        include_deleted=include_deleted,
    )
    if result["subject"] is None:
        return ApiResponse(
            ok=False,
            data={},
            error={
                "code": "SUBJECT_NOT_FOUND",
                "message": f"Subject not found: {subject_id}",
            },
        )
    return ApiResponse(ok=True, data=result)


@router.get("/state/routing", response_model=ApiResponse)
def get_state_routing_endpoint() -> ApiResponse:
    return ApiResponse(ok=True, data=build_routing_state_projection())


@router.get("/state/watchdog", response_model=ApiResponse)
def get_state_watchdog_endpoint() -> ApiResponse:
    return ApiResponse(ok=True, data=build_watchdog_state_projection())


@router.get("/state/rules", response_model=ApiResponse)
def get_state_rules_endpoint() -> ApiResponse:
    return ApiResponse(ok=True, data=build_rules_state_projection())


@router.get("/state/xray", response_model=ApiResponse)
def get_state_xray_endpoint() -> ApiResponse:
    return ApiResponse(ok=True, data=build_xray_state_projection())


@router.get("/state/vpn", response_model=ApiResponse)
def get_state_vpn_endpoint() -> ApiResponse:
    return ApiResponse(ok=True, data=build_vpn_state_projection())
