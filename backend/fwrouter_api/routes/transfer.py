from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from fwrouter_api.schemas import ApiResponse
from fwrouter_api.services.control_plane_transfer import (
    export_control_plane_snapshot,
    import_control_plane_snapshot,
    list_control_plane_snapshot_files,
    plan_control_plane_import,
    resolve_control_plane_snapshot_source,
    validate_control_plane_snapshot,
)


router = APIRouter()


class ControlPlaneSnapshotRequest(BaseModel):
    snapshot: dict = Field(default_factory=dict)
    file_path: str | None = None


class ControlPlaneImportRequest(BaseModel):
    snapshot: dict = Field(default_factory=dict)
    file_path: str | None = None
    normalize_runtime_state: bool = True


class ControlPlanePlanRequest(BaseModel):
    snapshot: dict = Field(default_factory=dict)
    file_path: str | None = None
    normalize_runtime_state: bool = True


@router.get("/transfer/control-plane/export", response_model=ApiResponse)
def export_control_plane_endpoint(
    include_secrets: bool = Query(default=False),
    write_file: bool = Query(default=True),
) -> ApiResponse:
    exported = export_control_plane_snapshot(
        include_secrets=include_secrets,
        write_file=write_file,
    )
    return ApiResponse(ok=True, data=exported)


@router.get("/transfer/control-plane/files", response_model=ApiResponse)
def list_control_plane_snapshot_files_endpoint() -> ApiResponse:
    return ApiResponse(ok=True, data=list_control_plane_snapshot_files())


@router.post("/transfer/control-plane/validate", response_model=ApiResponse)
def validate_control_plane_endpoint(request: ControlPlaneSnapshotRequest) -> ApiResponse:
    resolved = resolve_control_plane_snapshot_source(
        snapshot=request.snapshot,
        file_path=request.file_path,
    )
    if not resolved["ok"]:
        return ApiResponse(ok=False, error=resolved["error"])
    validation = validate_control_plane_snapshot(resolved["snapshot"])
    source = resolved.get("source") if isinstance(resolved.get("source"), dict) else {}
    return ApiResponse(ok=validation["ok"], data={"validation": validation, "source": source})


@router.post("/transfer/control-plane/plan", response_model=ApiResponse)
def plan_control_plane_endpoint(request: ControlPlanePlanRequest) -> ApiResponse:
    resolved = resolve_control_plane_snapshot_source(
        snapshot=request.snapshot,
        file_path=request.file_path,
    )
    if not resolved["ok"]:
        return ApiResponse(ok=False, error=resolved["error"])
    plan = plan_control_plane_import(
        resolved["snapshot"],
        normalize_runtime_state=request.normalize_runtime_state,
    )
    source = resolved.get("source") if isinstance(resolved.get("source"), dict) else {}
    return ApiResponse(ok=plan["ok"], data={"plan": plan, "source": source})


@router.post("/transfer/control-plane/import", response_model=ApiResponse)
def import_control_plane_endpoint(request: ControlPlaneImportRequest) -> ApiResponse:
    resolved = resolve_control_plane_snapshot_source(
        snapshot=request.snapshot,
        file_path=request.file_path,
    )
    if not resolved["ok"]:
        return ApiResponse(ok=False, error=resolved["error"])
    result = import_control_plane_snapshot(
        resolved["snapshot"],
        normalize_runtime_state=request.normalize_runtime_state,
    )
    if not result["ok"]:
        return ApiResponse(
            ok=False,
            data={
                "import": result,
                "source": resolved.get("source") if isinstance(resolved.get("source"), dict) else {},
            },
            error={
                "code": "CONTROL_PLANE_SNAPSHOT_INVALID",
                "message": "Control-plane snapshot validation failed.",
            },
        )
    return ApiResponse(
        ok=True,
        data={
            "import": result,
            "source": resolved.get("source") if isinstance(resolved.get("source"), dict) else {},
        },
    )
