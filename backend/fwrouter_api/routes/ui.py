from __future__ import annotations

import re
import json
from typing import Any

import httpx
from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from fwrouter_api.schemas import ApiResponse
from fwrouter_api.services.external_collectors import run_external_connection_collector
from fwrouter_api.services.ui_display_settings import (
    ExternalConnectionValidationError,
    create_custom_external_connection,
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
from fwrouter_api.services.ui_state_settings import get_ui_display_snapshot
from fwrouter_api.services.api_client_attribution import redact_http_attribution
from fwrouter_api.services.presentation_client_list_admission import (
    PresentationAdmissionError,
    verified_presentation_write,
)
from fwrouter_api.services.ui_client_list_settings import (
    ClientListSettingsError,
    _record_client_list_denial,
    patch_show_inactive,
    read_client_list_settings,
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


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _client_list_data(resource: dict[str, Any]) -> dict[str, Any]:
    data = {
        "show_inactive": resource["show_inactive"],
        "etag": resource["etag"],
        "compatibility_status": resource["compatibility_status"],
        "provenance": resource["provenance"],
        "incarnation": resource["incarnation"],
        "revision": resource["revision"],
    }
    for key in ("persistence_status", "audit_delivery_status"):
        if key in resource:
            data[key] = resource[key]
    return data


def _client_list_error(exc: ClientListSettingsError | PresentationAdmissionError) -> JSONResponse:
    body = ApiResponse(
        ok=False,
        error={"code": exc.code, "message": "Client-list settings request was not accepted."},
    )
    headers = {"Retry-After": "1"} if getattr(exc, "retryable", False) else None
    return JSONResponse(status_code=exc.status_code, content=body.model_dump(mode="json"), headers=headers)


async def _read_bounded_json_object(request: Request, *, maximum: int = 4096) -> dict[str, Any]:
    content_types = [value for key, value in request.scope.get("headers", []) if key.lower() == b"content-type"]
    if len(content_types) != 1 or content_types[0].split(b";", 1)[0].strip().lower() != b"application/json":
        raise ClientListSettingsError("CLIENT_LIST_JSON_REQUIRED", 415)
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > maximum:
            raise ClientListSettingsError("CLIENT_LIST_BODY_TOO_LARGE", 413)
        chunks.append(chunk)
    try:
        value = json.loads(b"".join(chunks).decode("utf-8"), object_pairs_hook=_strict_json_object)
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise ClientListSettingsError("CLIENT_LIST_JSON_INVALID", 400) from exc
    if not isinstance(value, dict):
        raise ClientListSettingsError("CLIENT_LIST_OBJECT_REQUIRED", 422)
    return value


def _single_header(request: Request, name: bytes) -> str | None:
    values = [value for key, value in request.scope.get("headers", []) if key.lower() == name]
    if len(values) != 1:
        return None
    try:
        return values[0].decode("ascii")
    except UnicodeDecodeError:
        return None


@router.get("/ui/settings/client-list", response_model=ApiResponse)
def get_client_list_settings_endpoint(request: Request) -> Response:
    try:
        resource = read_client_list_settings()
    except ClientListSettingsError as exc:
        return _client_list_error(exc)
    etag = resource["etag"]
    if _single_header(request, b"if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    return JSONResponse(
        content=ApiResponse(ok=True, data={"client_list": _client_list_data(resource)}).model_dump(mode="json"),
        headers={"ETag": etag},
    )


@router.patch("/ui/settings/client-list", response_model=ApiResponse)
async def patch_client_list_settings_endpoint(request: Request) -> Response:
    try:
        with verified_presentation_write(request):
            try:
                payload = await _read_bounded_json_object(request)
                if set(payload) != {"show_inactive"} or type(payload.get("show_inactive")) is not bool:
                    raise ClientListSettingsError("CLIENT_LIST_BOOLEAN_REQUIRED", 422)
                if_match = _single_header(request, b"if-match")
                if if_match is None:
                    raise ClientListSettingsError("CLIENT_LIST_PRECONDITION_REQUIRED", 428)
                if not re.fullmatch(r'"cli1-[0-9a-f]{64}"', if_match):
                    raise ClientListSettingsError("CLIENT_LIST_PRECONDITION_INVALID", 400)
                resource = patch_show_inactive(payload["show_inactive"], if_match)
            except ClientListSettingsError as exc:
                if not exc.denial_recorded:
                    _record_client_list_denial(
                        exc.code, conflict=exc.status_code in {409, 412, 428}, error=exc,
                    )
                raise
    except (ClientListSettingsError, PresentationAdmissionError) as exc:
        if not getattr(exc, "denial_recorded", False):
            _record_client_list_denial(
                exc.code, conflict=getattr(exc, "status_code", 0) in {409, 412, 428}, error=exc,
            )
        return _client_list_error(exc)
    return JSONResponse(
        content=ApiResponse(ok=True, data={"client_list": _client_list_data(resource)}).model_dump(mode="json"),
        headers={"ETag": resource["etag"]},
    )


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
    return ApiResponse(ok=True, data=redact_http_attribution({"router": get_ui_router_summary()}))


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
    for attempt in range(2):
        snapshot = get_ui_display_snapshot()
        display_settings = snapshot["display_settings"]
        clients = list_ui_clients(display_settings=display_settings)
        current = read_client_list_settings()
        if current["etag"] == snapshot["etag"]:
            return ApiResponse(
                ok=True,
                data={
                    "display_settings": display_settings,
                    "clients": clients,
                    "panel_clients": filter_ui_clients(clients, display_settings=display_settings),
                },
            )
    return JSONResponse(
        status_code=503,
        content=ApiResponse(ok=False, error={
            "code": "CONFIG_READ_MODEL_STALE",
            "message": "Client-list settings changed while this view was being built.",
        }).model_dump(mode="json"),
        headers={"Retry-After": "1"},
    )


@router.get("/ui/settings/workspace", response_model=ApiResponse)
def get_ui_settings_workspace_endpoint() -> ApiResponse:
    try:
        return ApiResponse(ok=True, data=redact_http_attribution({"workspace": get_ui_settings_workspace()}))
    except ClientListSettingsError as exc:
        return _client_list_error(exc)


@router.get("/ui/settings/inventory", response_model=ApiResponse)
def get_ui_settings_inventory_endpoint(
    role: str = Query(default="all"),
    query: str = Query(default=""),
    limit: int = Query(default=200, ge=1, le=500),
    include_inactive: bool = Query(default=False),
    live_observations: bool = Query(default=True),
) -> ApiResponse:
    for _attempt in range(2):
        snapshot = get_ui_display_snapshot()
        items = list_ui_settings_inventory(
            role=role,
            query=query,
            limit=limit,
            include_inactive=include_inactive,
            live_observations=live_observations,
            display_settings=snapshot["display_settings"],
        )
        if read_client_list_settings()["etag"] == snapshot["etag"]:
            return ApiResponse(ok=True, data={"items": items})
    return JSONResponse(
        status_code=503,
        content=ApiResponse(ok=False, error={
            "code": "CONFIG_READ_MODEL_STALE",
            "message": "Client-list settings changed while this view was being built.",
        }).model_dump(mode="json"),
        headers={"Retry-After": "1"},
    )


@router.post("/ui/external-connections/preview", response_model=ApiResponse)
def preview_ui_external_connection_endpoint(request: ExternalConnectionSettingsRequest) -> ApiResponse:
    try:
        result = preview_custom_external_connection(request.model_dump(exclude_none=True))
    except ExternalConnectionValidationError as exc:
        return _external_connection_error(exc)
    return ApiResponse(ok=True, data=result)


@router.post("/ui/external-connections", response_model=ApiResponse)
def create_ui_external_connection_endpoint(request: ExternalConnectionSettingsRequest) -> ApiResponse:
    try:
        result = create_custom_external_connection(request.model_dump(exclude_none=True))
    except ExternalConnectionValidationError as exc:
        return _external_connection_error(exc)
    return ApiResponse(ok=True, data=result)


@router.put("/ui/external-connections/{connection_id}", response_model=ApiResponse)
def put_ui_external_connection_endpoint(connection_id: str, request: ExternalConnectionSettingsRequest) -> ApiResponse:
    try:
        result = upsert_custom_external_connection(
            connection_id,
            request.model_dump(exclude_none=True),
            partial=False,
        )
    except ExternalConnectionValidationError as exc:
        return _external_connection_error(exc)
    return ApiResponse(ok=True, data=result)


@router.patch("/ui/external-connections/{connection_id}", response_model=ApiResponse)
def patch_ui_external_connection_endpoint(connection_id: str, request: ExternalConnectionSettingsRequest) -> ApiResponse:
    try:
        result = upsert_custom_external_connection(
            connection_id,
            request.model_dump(exclude_none=True),
            partial=True,
        )
    except ExternalConnectionValidationError as exc:
        return _external_connection_error(exc)
    return ApiResponse(ok=True, data=result)


@router.delete("/ui/external-connections/{connection_id}", response_model=ApiResponse)
def delete_ui_external_connection_endpoint(connection_id: str) -> ApiResponse:
    try:
        result = delete_custom_external_connection(connection_id)
    except ExternalConnectionValidationError as exc:
        return _external_connection_error(exc)
    return ApiResponse(ok=True, data=result)


@router.get("/ui/external-connections/{connection_id}/contract", response_model=ApiResponse)
def get_ui_external_connection_contract_endpoint(connection_id: str) -> ApiResponse:
    contract = external_connection_contract(connection_id)
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


@router.post("/ui/external-connections/{connection_id}/collect", response_model=ApiResponse)
def collect_ui_external_connection_endpoint(connection_id: str, request: ExternalConnectionCollectRequest) -> ApiResponse:
    result = run_external_connection_collector(
        connection_id,
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
async def save_ui_display_settings_endpoint(request: Request) -> Response:
    raw_payload: dict[str, Any] = {}
    try:
        try:
            raw_payload = await _read_bounded_json_object(request, maximum=1024 * 1024)
        except ClientListSettingsError as exc:
            # A malformed legacy display PUT may still be an attempted
            # protected-field mutation. Authenticate supplied credentials
            # before recording it so a valid actor is retained.
            if request.headers.get("authorization"):
                try:
                    with verified_presentation_write(request):
                        _record_client_list_denial(exc.code, error=exc)
                except PresentationAdmissionError as admission_exc:
                    _record_client_list_denial(admission_exc.code, error=admission_exc)
                    raise
            else:
                _record_client_list_denial(exc.code, error=exc)
            raise
        if "show_inactive" in raw_payload:
            with verified_presentation_write(request):
                try:
                    if type(raw_payload["show_inactive"]) is not bool:
                        raise ClientListSettingsError("CLIENT_LIST_BOOLEAN_REQUIRED", 422)
                    if_match = _single_header(request, b"if-match")
                    if if_match is None:
                        raise ClientListSettingsError("CLIENT_LIST_PRECONDITION_REQUIRED", 428)
                    if not re.fullmatch(r'"cli1-[0-9a-f]{64}"', if_match):
                        raise ClientListSettingsError("CLIENT_LIST_PRECONDITION_INVALID", 400)
                    payload = UiDisplaySettingsRequest.model_validate(raw_payload).model_dump(exclude_none=True)
                    result = save_ui_display_settings(payload, client_list_expected_etag=if_match)
                except ClientListSettingsError as exc:
                    if not exc.denial_recorded:
                        _record_client_list_denial(
                            exc.code, conflict=exc.status_code in {409, 412, 428}, error=exc,
                        )
                    raise
        else:
            payload = UiDisplaySettingsRequest.model_validate(raw_payload).model_dump(exclude_none=True)
            result = save_ui_display_settings(payload)
    except (ClientListSettingsError, PresentationAdmissionError) as exc:
        if not getattr(exc, "denial_recorded", False) and (
            "show_inactive" in raw_payload or isinstance(exc, PresentationAdmissionError)
        ):
            _record_client_list_denial(
                exc.code, conflict=getattr(exc, "status_code", 0) in {409, 412, 428}, error=exc,
            )
        return _client_list_error(exc)
    except Exception as exc:
        from pydantic import ValidationError
        if isinstance(exc, ValidationError):
            return JSONResponse(
                status_code=422,
                content=ApiResponse(ok=False, error={
                    "code": "UI_DISPLAY_SETTINGS_INVALID",
                    "message": "Display settings were not accepted.",
                }).model_dump(mode="json"),
            )
        raise
    return JSONResponse(
        content=ApiResponse(ok=True, data={"display_settings": result}).model_dump(mode="json")
    )
