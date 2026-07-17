from __future__ import annotations

import re

import httpx
from fastapi import APIRouter, Query
from pydantic import BaseModel

from fwrouter_api.schemas import ApiResponse
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
    show_lan: bool | None = None
    show_tailscale: bool | None = None
    show_xray: bool | None = None
    show_docker: bool | None = None
    show_host: bool | None = None
    show_inactive: bool | None = None
    show_internal_xray: bool | None = None
    hidden_subject_ids: list[str] | None = None
    subject_traffic_preferences: dict[str, list[str]] | None = None


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
    kind: str = Query(default="all"),
    query: str = Query(default=""),
    limit: int = Query(default=200, ge=1, le=500),
) -> ApiResponse:
    return ApiResponse(
        ok=True,
        data={
            "items": list_ui_settings_inventory(kind=kind, query=query, limit=limit),
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
