from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from fwrouter_api.schemas import ApiResponse
from fwrouter_api.services.subscription_pipeline import (
    apply_subscription_import_result,
    apply_subscription_refresh,
)
from fwrouter_api.services.subscription import (
    get_subscription_state,
    refresh_subscription_inventory_batch,
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
        metadata_public = dict(metadata)
        metadata_public.pop("url", None)
        public["metadata"] = metadata_public

    return public


def _redact_validation(validation: dict[str, Any] | None) -> dict[str, Any] | None:
    if validation is None:
        return None

    public = dict(validation)
    public["url_saved"] = bool(public.get("normalized_url"))
    public.pop("normalized_url", None)
    return public


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

    return public


def _redact_refresh_response(refresh_result: dict[str, Any]) -> dict[str, Any]:
    public = dict(refresh_result)
    public["validation"] = _redact_validation(public.get("validation"))
    public["state"] = _redact_subscription_state(public.get("state"))
    public["refresh"] = _redact_adapter_refresh(public.get("refresh"))
    return public


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
    return public


def _redact_batch_apply_response(apply_result: dict[str, Any]) -> dict[str, Any]:
    public = dict(apply_result)
    refresh = public.get("refresh")
    public["refresh"] = (
        _redact_batch_response(refresh)
        if isinstance(refresh, dict) and isinstance(refresh.get("batch"), dict)
        else _redact_refresh_response(refresh or {})
    )
    return public



class SubscriptionUrlRequest(BaseModel):
    url: str = Field(default="")
    urls: list[str] | None = None
    metadata: dict[str, Any] | None = None


@router.get("/subscription", response_model=ApiResponse)
def get_subscription_endpoint() -> ApiResponse:
    state = get_subscription_state()
    return ApiResponse(ok=True, data={"subscription": _redact_subscription_state(state)})


@router.post("/subscription/validate", response_model=ApiResponse)
def validate_subscription_endpoint(request: SubscriptionUrlRequest) -> ApiResponse:
    validation = validate_subscription_url(request.url)

    return ApiResponse(
        ok=validation["valid"],
        data={"validation": validation},
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
        import_result = refresh_subscription_inventory_batch(
            request.urls,
            metadata=request.metadata,
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
                "candidate": result.get("candidate"),
                "config_validation": result.get("config_validation"),
                "promoted": bool(result.get("promoted")),
                "container_restarted": bool(result.get("container_restarted")),
                "applied": bool(result.get("applied")),
                "auto_select": result.get("auto_select"),
            },
            error=result.get("error") if not result["ok"] else None,
        )

    result = save_subscription_url(
        request.url,
        metadata=request.metadata,
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
    refresh_result = apply_subscription_refresh()

    if not refresh_result["ok"]:
        return ApiResponse(
            ok=False,
            data={"refresh": _redact_refresh_response(refresh_result)},
            error=refresh_result.get("error"),
        )

    return ApiResponse(
        ok=True,
        data={
            "refresh": _redact_refresh_response(refresh_result),
            "candidate": refresh_result.get("candidate"),
            "config_validation": refresh_result.get("config_validation"),
            "promoted": bool(refresh_result.get("promoted")),
            "container_restarted": bool(refresh_result.get("container_restarted")),
        },
    )
