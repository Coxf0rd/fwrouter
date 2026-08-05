from __future__ import annotations

from fastapi import APIRouter, Query

from fwrouter_api.schemas import ApiResponse
from fwrouter_api.services.mihomo import get_mihomo_status, sync_mihomo_inventory
from fwrouter_api.services.mihomo_config import (
    get_mihomo_config_status,
    reconcile_mihomo_runtime,
    validate_and_promote_mihomo_candidate_config,
)
from fwrouter_api.services.mihomo_runtime import (
    get_mihomo_container_status,
    restart_mihomo_container,
)


router = APIRouter()


@router.get("/mihomo", response_model=ApiResponse)
def get_mihomo_endpoint() -> ApiResponse:
    status = get_mihomo_status()
    return ApiResponse(ok=True, data={"mihomo": status})


@router.post("/mihomo/sync", response_model=ApiResponse)
def sync_mihomo_endpoint() -> ApiResponse:
    result = sync_mihomo_inventory()
    return ApiResponse(ok=True, data={"sync": result})


@router.get("/mihomo/config", response_model=ApiResponse)
def get_mihomo_config_endpoint(
    include_config: bool = Query(
        default=False,
        description="Include full Mihomo base/candidate YAML payloads. Expensive on large rule sets.",
    ),
) -> ApiResponse:
    status = get_mihomo_config_status(include_config=include_config)
    return ApiResponse(ok=True, data={"config": status})


@router.post("/mihomo/config/promote", response_model=ApiResponse)
def promote_mihomo_config_endpoint() -> ApiResponse:
    result = validate_and_promote_mihomo_candidate_config()
    if not result["ok"]:
        return ApiResponse(
            ok=False,
            data=result,
            error={
                "code": str(result.get("error_code") or "MIHOMO_CONFIG_PROMOTE_FAILED"),
                "message": str(result.get("error_message") or "Mihomo candidate config promote failed."),
            },
        )

    return ApiResponse(
        ok=True,
        data=result,
    )


@router.post("/mihomo/config/reconcile", response_model=ApiResponse)
def reconcile_mihomo_config_endpoint() -> ApiResponse:
    result = reconcile_mihomo_runtime()

    if not result["ok"]:
        return ApiResponse(
            ok=False,
            data={"mihomo_reconcile": result},
            error={
                "code": str(result.get("error_code") or "MIHOMO_RECONCILE_FAILED"),
                "message": str(result.get("error_message") or "Failed to reconcile Mihomo runtime."),
            },
        )

    return ApiResponse(ok=True, data={"mihomo_reconcile": result})

@router.get("/mihomo/container", response_model=ApiResponse)
def get_mihomo_container_endpoint() -> ApiResponse:
    status = get_mihomo_container_status()
    return ApiResponse(ok=status["ok"], data={"container": status})


@router.post("/mihomo/restart", response_model=ApiResponse)
def restart_mihomo_endpoint() -> ApiResponse:
    result = restart_mihomo_container()

    return ApiResponse(
        ok=result["ok"],
        data={
            "container": result,
        },
        error=(
            {
                "code": str(result.get("error_code") or "MIHOMO_RESTART_FAILED"),
                "message": str(result.get("error_message") or "Mihomo container restart failed."),
            }
            if not result["ok"]
            else None
        ),
    )
