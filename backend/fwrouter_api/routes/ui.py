from __future__ import annotations

import re
from typing import Any

import httpx
from fastapi import APIRouter, Query
from pydantic import BaseModel

from fwrouter_api.schemas import ApiResponse
from fwrouter_api.services.external_collectors import run_external_connection_collector
from fwrouter_api.services.ui_display_settings import (
    ExternalConnectionValidationError,
    delete_custom_external_connection,
    external_connection_contract,
    preview_custom_external_connection,
    upsert_custom_external_connection,
)
from fwrouter_api.services.ui_state import (
    filter_ui_clients,
    get_ui_display_settings,
    list_ui_settings_inventory,
    get_ui_router_summary,
    get_ui_settings_workspace,
    list_ui_clients,
    save_ui_display_settings,
)


router = APIRouter()
DEFAULT_EXTERNAL_IP_URL = "https://api.ipify.org?format=json"
MIHOMO_MIXED_PROXY_URL = "http://127.0.0.1:5201"
IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
IPV6_RE = re.compile(r"\b(?:[0-9a-f]{1,4}:){2,7}[0-9a-f]{1,4}\b", re.IGNORECASE)


class UiDisplaySettingsRequest(BaseModel):
    system_visibility: dict[str, bool] | None = None
    custom_external_systems: list[dict[str, Any]] | None = None
    show_inactive: bool | None = None
    show_internal_vless: bool | None = None
    hidden_subject_ids: list[str] | None = None
    subject_traffic_preferences: dict[str, list[str]] | None = None


class ExternalConnectionCollectRequest(BaseModel):
    dry_run: bool = True
    requested_by: str | None = "api"


class ExternalConnectionSettingsRequest(BaseModel):
    connection_id: str | None = None
    system_id: str | None = None
    label: str | None = None
    name: str | None = None
    connection_type: str | None = None
    location: str | None = None
    address: str | None = None
    runtime_type: str | None = None
    replacement_target: str | None = None
    replaces: str | None = None
    endpoints: dict[str, Any] | None = None
    capabilities: dict[str, Any] | None = None
    integration_mode: str | None = None
    refresh_mode: str | None = None
    collector_config: dict[str, Any] | None = None
    collector: dict[str, Any] | None = None
    description: str | None = None


def _external_connection_error(exc: ExternalConnectionValidationError) -> ApiResponse:
    return ApiResponse(
        ok=False,
        error={
            "code": exc.code,
            "message": exc.message,
            "fields": exc.field_errors,
        },
    )


def _extract_external_ip(text: str) -> str:
    source = str(text or "")
    match = IPV4_RE.search(source)
    if match:
        return match.group(0)
    match = IPV6_RE.search(source)
    return match.group(0) if match else ""


def _fetch_external_ip(*, proxy_url: str | None = None) -> tuple[str, str | None]:
    try:
        with httpx.Client(timeout=4.5, trust_env=False, proxy=proxy_url) as client:
            response = client.get(DEFAULT_EXTERNAL_IP_URL)
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").lower()
            ip = ""
            if "application/json" in content_type:
                payload = response.json()
                if isinstance(payload, dict):
                    ip = str(
                        payload.get("ip")
                        or payload.get("query")
                        or payload.get("origin")
                        or payload.get("address")
                        or ""
                    ).strip()
                if not ip:
                    ip = _extract_external_ip(response.text)
            else:
                ip = _extract_external_ip(response.text)
            return ip, None
    except Exception as exc:  # pragma: no cover - depends on external network/runtime proxy
        return "", str(exc)


@router.get("/ui/router-summary", response_model=ApiResponse)
def get_ui_router_summary_endpoint() -> ApiResponse:
    return ApiResponse(ok=True, data={"router": get_ui_router_summary()})


@router.get("/ui/external-ip", response_model=ApiResponse)
def get_ui_external_ip_endpoint() -> ApiResponse:
    current_ip, current_error = _fetch_external_ip()
    vpn_ip, vpn_error = _fetch_external_ip(proxy_url=MIHOMO_MIXED_PROXY_URL)

    return ApiResponse(
        ok=True,
        data={
            "ip": current_ip,
            "current_ip": current_ip,
            "vpn_ip": vpn_ip,
            "source": "backend",
            "current_source": "backend",
            "vpn_source": "mihomo-mixed",
            "error": current_error or vpn_error,
            "current_error": current_error,
            "vpn_error": vpn_error,
        },
    )


@router.get("/ui/clients", response_model=ApiResponse)
def get_ui_clients_endpoint() -> ApiResponse:
    display_settings = get_ui_display_settings()
    clients = list_ui_clients()
    return ApiResponse(
        ok=True,
        data={
            "display_settings": display_settings,
            "clients": clients,
            "panel_clients": filter_ui_clients(clients, display_settings=display_settings),
        },
    )


@router.get("/ui/settings/workspace", response_model=ApiResponse)
def get_ui_settings_workspace_endpoint() -> ApiResponse:
    return ApiResponse(ok=True, data={"workspace": get_ui_settings_workspace()})


@router.get("/ui/settings/inventory", response_model=ApiResponse)
def get_ui_settings_inventory_endpoint(
    role: str = Query(default="all"),
    query: str = Query(default=""),
    limit: int = Query(default=200, ge=1, le=500),
    include_inactive: bool = Query(default=False),
) -> ApiResponse:
    return ApiResponse(
        ok=True,
        data={
            "items": list_ui_settings_inventory(
                role=role,
                query=query,
                limit=limit,
                include_inactive=include_inactive,
            ),
        },
    )


@router.post("/ui/external-connections/preview", response_model=ApiResponse)
def preview_ui_external_connection_endpoint(request: ExternalConnectionSettingsRequest) -> ApiResponse:
    try:
        result = preview_custom_external_connection(request.model_dump(exclude_none=True))
    except ExternalConnectionValidationError as exc:
        return _external_connection_error(exc)
    return ApiResponse(ok=True, data=result)


@router.put("/ui/external-connections/{system_id}", response_model=ApiResponse)
def put_ui_external_connection_endpoint(system_id: str, request: ExternalConnectionSettingsRequest) -> ApiResponse:
    try:
        result = upsert_custom_external_connection(
            system_id,
            request.model_dump(exclude_none=True),
            partial=False,
        )
    except ExternalConnectionValidationError as exc:
        return _external_connection_error(exc)
    return ApiResponse(ok=True, data=result)


@router.patch("/ui/external-connections/{system_id}", response_model=ApiResponse)
def patch_ui_external_connection_endpoint(system_id: str, request: ExternalConnectionSettingsRequest) -> ApiResponse:
    try:
        result = upsert_custom_external_connection(
            system_id,
            request.model_dump(exclude_none=True),
            partial=True,
        )
    except ExternalConnectionValidationError as exc:
        return _external_connection_error(exc)
    return ApiResponse(ok=True, data=result)


@router.delete("/ui/external-connections/{system_id}", response_model=ApiResponse)
def delete_ui_external_connection_endpoint(system_id: str) -> ApiResponse:
    try:
        result = delete_custom_external_connection(system_id)
    except ExternalConnectionValidationError as exc:
        return _external_connection_error(exc)
    return ApiResponse(ok=True, data=result)


@router.get("/ui/external-connections/{system_id}/contract", response_model=ApiResponse)
def get_ui_external_connection_contract_endpoint(system_id: str) -> ApiResponse:
    contract = external_connection_contract(system_id)
    if not contract:
        return ApiResponse(
            ok=False,
            error={
                "code": "EXTERNAL_CONNECTION_NOT_FOUND",
                "message": "External connection is not registered in UI display settings.",
            },
        )
    return ApiResponse(
        ok=True,
        data={
            "external_connection": contract,
            "contract": contract.get("api_guide"),
        },
    )


@router.post("/ui/external-connections/{system_id}/collect", response_model=ApiResponse)
def collect_ui_external_connection_endpoint(system_id: str, request: ExternalConnectionCollectRequest) -> ApiResponse:
    result = run_external_connection_collector(
        system_id,
        dry_run=request.dry_run,
        requested_by=request.requested_by or "api",
    )
    return ApiResponse(
        ok=bool(result.get("ok")),
        data={"collector": result},
        error=None if result.get("ok") else {
            "code": str(result.get("error_code") or "EXTERNAL_COLLECTOR_FAILED"),
            "message": str(result.get("error_message") or "External collector failed."),
        },
    )


@router.get("/ui/settings/display", response_model=ApiResponse)
def get_ui_display_settings_endpoint() -> ApiResponse:
    return ApiResponse(ok=True, data={"display_settings": get_ui_display_settings()})


@router.put("/ui/settings/display", response_model=ApiResponse)
def save_ui_display_settings_endpoint(request: UiDisplaySettingsRequest) -> ApiResponse:
    payload = request.model_dump(exclude_none=True)
    return ApiResponse(
        ok=True,
        data={"display_settings": save_ui_display_settings(payload)},
    )
