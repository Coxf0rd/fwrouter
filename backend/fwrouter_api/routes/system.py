from __future__ import annotations

import sqlite3
import logging
from fastapi import APIRouter
from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import db_session, readonly_db_session_scope
from fwrouter_api.db.schema_state import inspect_database_schema, summarize_schema_state
from fwrouter_api.schemas import ApiResponse
from fwrouter_api.services.system_summary import build_system_summary
from fwrouter_api.services.database_schema_read_model import get_cached_schema_state


router = APIRouter()
logger = logging.getLogger(__name__)

_HEALTH_RESPONSE_EXAMPLE = {
    "ok": True,
    "data": {
        "service": "fwrouter-api",
        "version": "x.y.z",
        "status": "healthy",
        "environment": "example",
        "database": {
            "status": "healthy",
            "schema": {
                "ok": True,
                "status": "ok",
                "expected_schema_version": "27",
                "actual_schema_version": "27",
                "rebuild_required": False,
                "problem_count": 0,
                "drifted_tables": [],
            },
        },
    },
    "error": None,
}

_SYSTEM_SUMMARY_EXAMPLE = {
    "ok": True,
    "data": {
        "core": {
            "desired_state": "enabled",
            "runtime_state": "running",
            "apply_state": "clean",
        },
        "backend": {"status": "healthy", "version": "x.y.z", "environment": "example"},
        "modules": [{"module_name": "core", "desired_state": "enabled", "runtime_state": "running"}],
        "warnings": [],
    },
    "error": None,
}


@router.get(
    "/health",
    response_model=ApiResponse,
    summary="Read API and database health",
    description="Read-only API and SQLite schema status. A healthy response does not establish dataplane or external connectivity.",
    responses={
        "200": {
            "content": {
                "application/json": {
                    "examples": {
                        "healthy": {
                            "summary": "Healthy API and database",
                            "value": _HEALTH_RESPONSE_EXAMPLE,
                        },
                    },
                },
            },
        },
    },
)
def health() -> ApiResponse:
    settings = get_settings()
    db_status = "unknown"
    db_unavailable = False
    schema_summary = None

    try:
        schema_state = get_cached_schema_state()
        schema_summary = summarize_schema_state(schema_state)
        db_status = "healthy" if schema_summary["ok"] else "failed"
    except sqlite3.Error:
        db_status = "failed"
        db_unavailable = True
        logger.exception("Database health check failed")

    return ApiResponse(
        ok=db_status == "healthy",
        data={
            "service": "fwrouter-api",
            "version": settings.app_version,
            "status": db_status,
            "environment": settings.environment,
            "database": {
                "status": db_status,
                "schema": schema_summary,
            },
        },
        error=(
            {
                "code": (
                    "DATABASE_UNAVAILABLE"
                    if db_unavailable
                    else "DATABASE_SCHEMA_MISMATCH"
                ),
                "message": (
                    "The control-plane database is unavailable."
                    if db_unavailable
                    else "SQLite schema drift detected. Rebuild the control-plane database."
                ),
            }
            if db_unavailable or (schema_summary is not None and not schema_summary["ok"])
            else None
        ),
    )


@router.get(
    "/system/summary",
    response_model=ApiResponse,
    summary="Read system and service status",
    description="Read-only summary of Core policy state, backend readiness, modules, and current warnings. Values reflect current observations and persisted state.",
    responses={
        "200": {
            "content": {
                "application/json": {
                    "examples": {
                        "system_status": {
                            "summary": "System and service status",
                            "description": "Illustrative compact values; the live endpoint returns additional policy and module details.",
                            "value": _SYSTEM_SUMMARY_EXAMPLE,
                        },
                    },
                },
            },
        },
    },
)
def system_summary() -> ApiResponse:
    try:
        with readonly_db_session_scope(), db_session() as connection:
            schema_state = inspect_database_schema(connection)
        data = build_system_summary(schema_state=schema_state)
        # Filesystem layout is an operator diagnostic and is not part of the public API contract.
        data.pop("paths", None)
    except sqlite3.Error:
        settings = get_settings()
        logger.exception("System summary database query failed")
        return ApiResponse(
            ok=False,
            data={
                "backend": {
                    "status": "failed",
                    "version": settings.app_version,
                    "environment": settings.environment,
                }
            },
            error={
                "code": "DATABASE_UNAVAILABLE",
                "message": "The control-plane database is unavailable.",
            },
        )

    return ApiResponse(ok=True, data=data)
