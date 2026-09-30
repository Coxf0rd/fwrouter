from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from fwrouter_api.jobs.manager import get_default_job_manager
from fwrouter_api.schemas import ApiResponse
from fwrouter_api.services.jobs import JobLockConflictError
from fwrouter_api.services.subscription_pipeline import apply_subscription_import_result
from fwrouter_api.services.subscription_refresh_job import (
    SUBSCRIPTION_REFRESH_LOCK_KEY,
    SUBSCRIPTION_REFRESH_OPERATION,
    SUBSCRIPTION_REFRESH_STAGES,
    SUBSCRIPTION_SOURCE_DELETE_OPERATION,
    register_subscription_refresh_handler,
)
from fwrouter_api.services.subscription import (
    compact_subscription_metadata,
    get_subscription_state,
    refresh_subscription_inventory_batch,
    redact_subscription_public_value,
    save_subscription_url,
    validate_subscription_url,
)


router = APIRouter()

def _redact_subscription_state(state: dict[str, Any] | None) -> dict[str, Any] | None:
    if state is None:
        return None

    public = dict(state)
    public["url_saved"] = bool(public.get("url"))
    public.pop("url", None)

    metadata = public.get("metadata")
    if isinstance(metadata, dict):
        public["metadata"] = compact_subscription_metadata(metadata, redact_urls=True)

    return redact_subscription_public_value(public)


def _redact_validation(validation: dict[str, Any] | None) -> dict[str, Any] | None:
    if validation is None:
        return None

    public = dict(validation)
    public["url_saved"] = bool(public.get("normalized_url"))
    public.pop("normalized_url", None)
    return redact_subscription_public_value(public)


def _redact_adapter_refresh(refresh: dict[str, Any] | None) -> dict[str, Any] | None:
    if refresh is None:
        return None

    public = dict(refresh)
    servers = public.pop("servers", []) or []
    public["servers_count"] = len(servers)

    metadata = public.get("metadata")
    if isinstance(metadata, dict):
        metadata_public = dict(metadata)
        metadata_public.pop("url", None)
        public["metadata"] = metadata_public

    return redact_subscription_public_value(public)


def _redact_refresh_response(refresh_result: dict[str, Any]) -> dict[str, Any]:
    public = dict(refresh_result)
    public["validation"] = _redact_validation(public.get("validation"))
    public["state"] = _redact_subscription_state(public.get("state"))
    public["refresh"] = _redact_adapter_refresh(public.get("refresh"))
    return redact_subscription_public_value(public)


def _redact_batch_response(batch_result: dict[str, Any]) -> dict[str, Any]:
    public = dict(batch_result)
    public["state"] = _redact_subscription_state(public.get("state"))
    batch = dict(public.get("batch") or {})
    items: list[dict[str, Any]] = []
    for index, item in enumerate(batch.get("items") or [], start=1):
        item_public = dict(item)
        item_public["url_index"] = index
        item_public["url_saved"] = bool(item_public.get("url"))
        item_public.pop("url", None)
        item_public["refresh"] = _redact_adapter_refresh(item_public.get("refresh"))
        items.append(item_public)
    batch["items"] = items
    public["batch"] = batch
    return redact_subscription_public_value(public)


def _redact_batch_apply_response(apply_result: dict[str, Any]) -> dict[str, Any]:
    public = dict(apply_result)
    refresh = public.get("refresh")
    public["refresh"] = (
        _redact_batch_response(refresh)
        if isinstance(refresh, dict) and isinstance(refresh.get("batch"), dict)
        else _redact_refresh_response(refresh or {})
    )
    return redact_subscription_public_value(public)


def _same_refresh_scope(job: dict[str, Any], source_ref: str | None) -> bool:
    if str(job.get("job_type") or "") != SUBSCRIPTION_REFRESH_OPERATION:
        return False
    payload = job.get("input") if isinstance(job.get("input"), dict) else {}
    active_ref = str(payload.get("source_ref") or "").strip() or None
    return active_ref == source_ref



class SubscriptionUrlRequest(BaseModel):
    url: str = Field(default="")
    urls: list[str] | None = None
    metadata: dict[str, Any] | None = None
    requested_by: str | None = "api"


@router.get("/subscription", response_model=ApiResponse)
def get_subscription_endpoint() -> ApiResponse:
    state = get_subscription_state()
    return ApiResponse(ok=True, data={"subscription": _redact_subscription_state(state)})


@router.post("/subscription/validate", response_model=ApiResponse)
def validate_subscription_endpoint(request: SubscriptionUrlRequest) -> ApiResponse:
    validation = validate_subscription_url(request.url)

    return ApiResponse(
        ok=validation["valid"],
        data={"validation": _redact_validation(validation)},
        error=(
            {
                "code": validation["error"]["code"],
                "message": validation["error"]["message"],
            }
            if not validation["valid"]
            else None
        ),
    )


@router.post("/subscription", response_model=ApiResponse)
def save_subscription_endpoint(request: SubscriptionUrlRequest) -> ApiResponse:
    if request.urls is not None:
        from fwrouter_api.adapters.xray_common import xray_writer_guard
        with xray_writer_guard():
            import_result = refresh_subscription_inventory_batch(
                request.urls,
                metadata=request.metadata,
                requested_by=request.requested_by or "api",
            )
            result = apply_subscription_import_result(import_result)
        refresh_public = _redact_batch_response(result.get("refresh") or import_result)
        apply_public = _redact_batch_apply_response(result)
        return ApiResponse(
            ok=result["ok"],
            data={
                "subscription": _redact_subscription_state((result.get("refresh") or import_result).get("state")),
                "refresh": apply_public,
                "batch": refresh_public.get("batch"),
                "refresh_started": False,
                "candidate": apply_public.get("candidate"),
                "config_validation": apply_public.get("config_validation"),
                "promoted": bool(result.get("promoted")),
                "container_restarted": bool(result.get("container_restarted")),
                "applied": bool(result.get("applied")),
                "auto_select": result.get("auto_select"),
            },
            error=redact_subscription_public_value(result.get("error")) if not result["ok"] else None,
        )

    result = save_subscription_url(
        request.url,
        metadata=request.metadata,
        requested_by=request.requested_by or "api",
    )

    validation = result["validation"]

    return ApiResponse(
        ok=result["saved"],
        data={
            "subscription": _redact_subscription_state(result["state"]),
            "validation": _redact_validation(validation),
            "refresh_started": False,
        },
        error=(
            {
                "code": validation["error"]["code"],
                "message": validation["error"]["message"],
            }
            if not result["saved"]
            else None
        ),
    )

@router.post("/subscription/refresh", response_model=ApiResponse)
def refresh_subscription_endpoint() -> ApiResponse:
    manager = get_default_job_manager()
    register_subscription_refresh_handler(manager)
    try:
        job = manager.create(
            SUBSCRIPTION_REFRESH_OPERATION,
            lock_key=SUBSCRIPTION_REFRESH_LOCK_KEY,
            requested_by="api.subscription.refresh",
            input_data={"operation": SUBSCRIPTION_REFRESH_OPERATION},
        )
        job = manager.start_job(job["job_id"]) or job
        accepted = True
        already_running = False
    except JobLockConflictError as exc:
        job = exc.active_job
        if not _same_refresh_scope(job, None):
            return ApiResponse(
                ok=False,
                data={"accepted": False, "already_running": True, "operation": SUBSCRIPTION_REFRESH_OPERATION},
                error={"code": "SUBSCRIPTION_OPERATION_IN_PROGRESS", "message": "A different subscription operation is already running. Try again when it finishes."},
            )
        accepted = False
        already_running = True

    return ApiResponse(
        ok=True,
        data={
            "accepted": accepted,
            "already_running": already_running,
            "status": job.get("status"),
            "job": job,
            "job_id": job.get("job_id"),
            "operation": SUBSCRIPTION_REFRESH_OPERATION,
            "stages": SUBSCRIPTION_REFRESH_STAGES,
            "refresh_started": True,
        },
    )


@router.post("/subscription/sources/{source_ref}/refresh", response_model=ApiResponse)
def refresh_subscription_source_endpoint(source_ref: str) -> ApiResponse:
    if re.fullmatch(r"src:[0-9a-f]{64}", source_ref or "") is None:
        return ApiResponse(
            ok=False,
            data={"accepted": False, "operation": SUBSCRIPTION_REFRESH_OPERATION},
            error={"code": "SUBSCRIPTION_SOURCE_REF_INVALID", "message": "Subscription source reference is invalid."},
        )
    manager = get_default_job_manager()
    register_subscription_refresh_handler(manager)
    try:
        job = manager.create(
            SUBSCRIPTION_REFRESH_OPERATION,
            lock_key=SUBSCRIPTION_REFRESH_LOCK_KEY,
            requested_by="api.subscription.source.refresh",
            input_data={"operation": SUBSCRIPTION_REFRESH_OPERATION, "source_ref": source_ref},
        )
        job = manager.start_job(job["job_id"]) or job
        accepted = True
        already_running = False
    except JobLockConflictError as exc:
        job = exc.active_job
        if not _same_refresh_scope(job, source_ref):
            return ApiResponse(
                ok=False,
                data={"accepted": False, "already_running": True, "operation": SUBSCRIPTION_REFRESH_OPERATION, "source_ref": source_ref},
                error={"code": "SUBSCRIPTION_OPERATION_IN_PROGRESS", "message": "A different subscription operation is already running. Try again when it finishes."},
            )
        accepted = False
        already_running = True
    return ApiResponse(
        ok=True,
        data={
            "accepted": accepted,
            "already_running": already_running,
            "status": job.get("status"),
            "job": job,
            "job_id": job.get("job_id"),
            "operation": SUBSCRIPTION_REFRESH_OPERATION,
            "source_ref": source_ref,
            "stages": SUBSCRIPTION_REFRESH_STAGES,
            "refresh_started": True,
        },
    )


@router.delete("/subscription/sources/{source_ref}", response_model=ApiResponse)
def delete_subscription_source_endpoint(source_ref: str) -> ApiResponse:
    if re.fullmatch(r"src:[0-9a-f]{64}", source_ref or "") is None:
        return ApiResponse(
            ok=False,
            data={"accepted": False, "operation": SUBSCRIPTION_SOURCE_DELETE_OPERATION},
            error={"code": "SUBSCRIPTION_SOURCE_REF_INVALID", "message": "Subscription source reference is invalid."},
        )
    manager = get_default_job_manager()
    register_subscription_refresh_handler(manager)
    try:
        job = manager.create(
            SUBSCRIPTION_SOURCE_DELETE_OPERATION,
            lock_key=SUBSCRIPTION_REFRESH_LOCK_KEY,
            requested_by="api.subscription.source.delete",
            input_data={"source_ref": source_ref},
        )
        job = manager.start_job(job["job_id"]) or job
    except JobLockConflictError:
        return ApiResponse(
            ok=False,
            data={"accepted": False, "already_running": True, "operation": SUBSCRIPTION_SOURCE_DELETE_OPERATION},
            error={"code": "SUBSCRIPTION_OPERATION_IN_PROGRESS", "message": "A subscription refresh or source operation is already running. Try again when it finishes."},
        )
    stages = ["intent", "inventory", "prepare", "validate", "apply_runtime", "verify"]
    return ApiResponse(
        ok=True,
        data={
            "accepted": True,
            "already_running": False,
            "status": job.get("status"),
            "job": job,
            "job_id": job.get("job_id"),
            "operation": SUBSCRIPTION_SOURCE_DELETE_OPERATION,
            "source_ref": source_ref,
            "stages": stages,
        },
    )
