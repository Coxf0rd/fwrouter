from __future__ import annotations

import sqlite3
from fastapi import APIRouter
from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import get_cached_schema_state
from fwrouter_api.db.schema_state import summarize_schema_state
from fwrouter_api.schemas import ApiResponse
from fwrouter_api.services.system_summary import build_system_summary


router = APIRouter()


@router.get("/health", response_model=ApiResponse)
def health() -> ApiResponse:
    settings = get_settings()
    db_status = "unknown"
    db_error = None
    schema_summary = None

    try:
        schema_state = get_cached_schema_state()
        schema_summary = summarize_schema_state(schema_state)
        db_status = "ok" if schema_summary["ok"] else "degraded"
    except sqlite3.Error as exc:
        db_status = "error"
        db_error = str(exc)

    return ApiResponse(
        ok=db_status == "ok",
        data={
            "service": "fwrouter-api",
            "version": settings.app_version,
            "status": "ok" if db_status == "ok" else "degraded",
            "environment": settings.environment,
            "database": {
                "status": db_status,
                "path": str(settings.paths.db_path),
                "schema": schema_summary,
            },
        },
        error=(
            {
                "code": (
                    "DATABASE_ERROR"
                    if db_error
                    else "DATABASE_SCHEMA_MISMATCH"
                ),
                "message": (
                    db_error
                    if db_error
                    else "SQLite schema drift detected. Rebuild the control-plane database."
                ),
            }
            if db_error or (schema_summary is not None and not schema_summary["ok"])
            else None
        ),
    )


@router.get("/system/summary", response_model=ApiResponse)
def system_summary() -> ApiResponse:
    try:
        schema_state = get_cached_schema_state()
        data = build_system_summary(schema_state=schema_state)
    except sqlite3.Error as exc:
        settings = get_settings()
        return ApiResponse(
            ok=False,
            data={
                "backend": {
                    "status": "degraded",
                    "version": settings.app_version,
                    "environment": settings.environment,
                }
            },
            error={
                "code": "DATABASE_ERROR",
                "message": str(exc),
            },
        )

    return ApiResponse(ok=True, data=data)


