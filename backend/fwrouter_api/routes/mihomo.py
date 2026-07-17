from __future__ import annotations

import subprocess

from fastapi import APIRouter, Query

from fwrouter_api.schemas import ApiResponse
from fwrouter_api.services.mihomo import get_mihomo_status, sync_mihomo_inventory
from fwrouter_api.services.mihomo_config import (
    MIHOMO_CANDIDATE_CONFIG_PATH,
    get_mihomo_config_status,
    promote_mihomo_candidate_config,
    reconcile_mihomo_runtime,
)
from fwrouter_api.services.mihomo_runtime import (
    get_mihomo_container_status,
    restart_mihomo_container,
)


router = APIRouter()

MIHOMO_IMAGE = "metacubex/mihomo:v1.19.19"


def _validate_mihomo_candidate_config() -> dict[str, object]:
    validation = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{MIHOMO_CANDIDATE_CONFIG_PATH}:/config/config.yaml:ro",
            MIHOMO_IMAGE,
            "-t",
            "-f",
            "/config/config.yaml",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    return {
        "ok": validation.returncode == 0,
        "returncode": validation.returncode,
        "stdout_tail": validation.stdout[-1000:],
        "stderr_tail": validation.stderr[-1000:],
    }


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
    config_validation = _validate_mihomo_candidate_config()

    if not config_validation["ok"]:
        return ApiResponse(
            ok=False,
            data={
                "config": get_mihomo_config_status(),
                "config_validation": config_validation,
                "container_restarted": False,
            },
            error={
                "code": "MIHOMO_CONFIG_VALIDATION_FAILED",
                "message": "Mihomo candidate config failed validation.",
            },
        )

    result = promote_mihomo_candidate_config()
    return ApiResponse(
        ok=True,
        data={
            "config": result,
            "config_validation": config_validation,
            "container_restarted": False,
        },
    )


@router.post("/mihomo/config/reconcile", response_model=ApiResponse)
def reconcile_mihomo_config_endpoint() -> ApiResponse:
    result = reconcile_mihomo_runtime()

    if not result["ok"]:
        return ApiResponse(
            ok=False,
            data={"mihomo_reconcile": result},
            error={
                "code": "MIHOMO_RECONCILE_FAILED",
                "message": "Failed to reconcile Mihomo runtime.",
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
                "code": "MIHOMO_RESTART_FAILED",
                "message": "Mihomo container restart failed.",
            }
            if not result["ok"]
            else None
        ),
    )
