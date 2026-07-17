from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from fwrouter_api.schemas import ApiResponse
from fwrouter_api.services.modules import (
    ModuleNotFoundError,
    ModuleStateError,
    fetch_modules,
    find_module,
    run_module_action,
    set_module_desired_state,
)


router = APIRouter()


class SetModuleDesiredStateRequest(BaseModel):
    desired_state: str = Field(description="enabled or disabled")
    requested_by: str | None = "api"
    run_now: bool = True


@router.get("/modules", response_model=ApiResponse)
def list_modules() -> ApiResponse:
    modules = fetch_modules()
    return ApiResponse(ok=True, data={"modules": modules})


@router.get("/modules/{module_name}", response_model=ApiResponse)
def get_module(module_name: str) -> ApiResponse:
    modules = fetch_modules()
    module = find_module(modules, module_name)

    if module is None:
        return ApiResponse(
            ok=False,
            data={},
            error={
                "code": "MODULE_NOT_FOUND",
                "message": f"Module not found: {module_name}",
            },
        )

    return ApiResponse(ok=True, data={"module": module})


@router.post("/modules/{module_name}/desired-state", response_model=ApiResponse)
def set_module_desired_state_endpoint(
    module_name: str,
    request: SetModuleDesiredStateRequest,
) -> ApiResponse:
    try:
        result = set_module_desired_state(
            module_name,
            request.desired_state,
            requested_by=request.requested_by or "api",
            run_now=request.run_now,
        )
    except ModuleNotFoundError:
        return ApiResponse(
            ok=False,
            data={},
            error={
                "code": "MODULE_NOT_FOUND",
                "message": f"Module not found: {module_name}",
            },
        )
    except ModuleStateError as exc:
        return ApiResponse(
            ok=False,
            data={},
            error={
                "code": "MODULE_STATE_INVALID",
                "message": str(exc),
            },
        )

    return ApiResponse(ok=True, data=result)


@router.post("/modules/{module_name}/actions/{action}", response_model=ApiResponse)
def run_module_action_endpoint(
    module_name: str,
    action: str,
    requested_by: str = "api",
) -> ApiResponse:
    try:
        result = run_module_action(
            module_name,
            action,
            requested_by=requested_by,
        )
    except ModuleNotFoundError:
        return ApiResponse(
            ok=False,
            data={},
            error={
                "code": "MODULE_NOT_FOUND",
                "message": f"Module not found: {module_name}",
            },
        )
    except ModuleStateError as exc:
        return ApiResponse(
            ok=False,
            data={},
            error={
                "code": "MODULE_ACTION_INVALID",
                "message": str(exc),
            },
        )

    if not bool((result.get("action_result") or {}).get("ok")):
        return ApiResponse(
            ok=False,
            data=result,
            error={
                "code": str((result.get("action_result") or {}).get("error_code") or "MODULE_ACTION_FAILED"),
                "message": str((result.get("action_result") or {}).get("error_message") or "Module action failed."),
            },
        )

    return ApiResponse(ok=True, data=result)
