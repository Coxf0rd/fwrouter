from __future__ import annotations

import base64
import json
from threading import Lock, Thread
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from fastapi import APIRouter, Query, Request, Response
from pydantic import BaseModel, Field

from fwrouter_api.schemas import ApiResponse
from fwrouter_api.services.logs import write_technical_log
from fwrouter_api.services.xray import (
    create_xray_client,
    delete_xray_client,
    export_xray_subscription,
    export_subscription_profile_text,
    export_xray_subscription_text,
    export_xray_vpn_auto_subscription_text,
    get_xray_status,
    list_xray_clients,
    reload_xray,
    reconcile_xray_subscription_profile_nodes, # <-- Added this import
    sync_xray_subjects,
    update_xray_client_alias,
    xray_service_call,
)


router = APIRouter()
public_router = APIRouter()
_PUBLIC_PROFILE_RECONCILE_LOCK = Lock()
_PUBLIC_PROFILE_RECONCILE_ACTIVE_TOKENS: set[str] = set()


CLASH_UA_MARKERS = (
    "clash",
    "mihomo",
    "stash",
    "shadowrocket",
    "clashx",
    "clash-verge",
    "clashmeta",
)


def _subscription_format_from_request(request: Request, format: str = "auto") -> str:
    explicit = str(format or "auto").strip().lower()
    if explicit in {"clash", "yaml", "mihomo"}:
        return "clash"
    if explicit in {"vless", "txt", "base64", "v2ray", "xray"}:
        return "vless"

    user_agent = str(request.headers.get("user-agent") or "").lower()
    if any(marker in user_agent for marker in CLASH_UA_MARKERS):
        return "clash"

    accept = str(request.headers.get("accept") or "").lower()
    if "yaml" in accept:
        return "clash"

    return "vless"


def _yaml_quote(value: Any) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def _first_query_value(params: dict[str, list[str]], key: str, default: str = "") -> str:
    values = params.get(key) or []
    return str(values[0]) if values else default


def _clash_yaml_from_vless_uris(uris: list[str]) -> str:
    proxies: list[dict[str, Any]] = []

    for uri in uris:
        parsed = urlsplit(uri)
        if parsed.scheme != "vless":
            continue

        params = parse_qs(parsed.query)
        name = unquote(parsed.fragment or parsed.hostname or "FWRouter Xray")
        uuid = parsed.username or ""
        server = parsed.hostname or ""
        port = parsed.port or 443
        path = _first_query_value(params, "path", "/vless")
        host = _first_query_value(params, "host", server)
        sni = _first_query_value(params, "sni", server)

        if not uuid or not server:
            continue

        proxies.append(
            {
                "name": name,
                "type": "vless",
                "server": server,
                "port": port,
                "uuid": uuid,
                "tls": True,
                "servername": sni,
                "udp": True,
                "network": "ws",
                "ws_path": path,
                "ws_host": host,
            }
        )

    lines: list[str] = []
    lines.append("proxies:")
    for proxy in proxies:
        lines.extend(
            [
                f"  - name: {_yaml_quote(proxy['name'])}",
                "    type: vless",
                f"    server: {_yaml_quote(proxy['server'])}",
                f"    port: {int(proxy['port'])}",
                f"    uuid: {_yaml_quote(proxy['uuid'])}",
                "    tls: true",
                f"    servername: {_yaml_quote(proxy['servername'])}",
                "    udp: true",
                "    network: ws",
                "    client-fingerprint: chrome",
                "    alpn:",
                "      - http/1.1",
                "    ws-opts:",
                f"      path: {_yaml_quote(proxy['ws_path'])}",
                "      headers:",
                f"        Host: {_yaml_quote(proxy['ws_host'])}",
            ]
        )

    lines.append("proxy-groups:")
    lines.append("  - name: FWRouter Xray")
    lines.append("    type: select")
    lines.append("    proxies:")
    for proxy in proxies:
        lines.append(f"      - {_yaml_quote(proxy['name'])}")
    lines.append("      - DIRECT")

    lines.append("rules:")
    lines.append("  - MATCH,FWRouter Xray")
    lines.append("")

    return "\n".join(lines)


def _subscription_text_response(
    *,
    payload: dict[str, Any],
    subscription_format: str,
    base64_encode: bool,
) -> Response:
    uris = [str(uri) for uri in (payload.get("uris") or []) if str(uri).strip()]

    if subscription_format == "clash":
        return Response(
            content=_clash_yaml_from_vless_uris(uris),
            media_type="application/yaml; charset=utf-8",
            headers={
                "Subscription-Userinfo": "upload=0; download=0; total=0; expire=0",
                "Profile-Title": "FWRouter Xray",
                "Cache-Control": "no-store",
            },
        )

    raw_content = "\n".join(uris) + ("\n" if uris else "")
    content = (
        base64.b64encode(raw_content.encode("utf-8")).decode("ascii")
        if base64_encode
        else raw_content
    )
    return Response(
        content=content,
        media_type="text/plain; charset=utf-8",
        headers={
            "Subscription-Userinfo": "upload=0; download=0; total=0; expire=0",
            "Profile-Title": "FWRouter Xray",
            "Cache-Control": "no-store",
        },
    )


def _redact_token(token: str) -> str:
    token_str = str(token or "")
    if len(token_str) <= 6:
        return "***"
    return f"{token_str[:3]}***{token_str[-2:]}"


def _reconcile_public_subscription_profile(token: str) -> None:
    try:
        try:
            ok_reconcile, reconcile_result_payload = xray_service_call(
                reconcile_xray_subscription_profile_nodes,
                requested_by=f"public_sub_endpoint:{token}",
                token_or_slug=token,
            )
            if not ok_reconcile:
                write_technical_log(
                    component="xray-route",
                    level="warning",
                    event_type="xray_public_subscription_reconcile_failed",
                    message="Failed to reconcile Xray public subscription profile nodes.",
                    details={
                        "token": _redact_token(token),
                        "result": reconcile_result_payload,
                    },
                )
        except Exception as exc:
            write_technical_log(
                component="xray-route",
                level="warning",
                event_type="xray_public_subscription_reconcile_crashed",
                message="Public Xray subscription reconcile worker crashed.",
                details={
                    "token": _redact_token(token),
                    "error": str(exc),
                },
            )
    finally:
        with _PUBLIC_PROFILE_RECONCILE_LOCK:
            _PUBLIC_PROFILE_RECONCILE_ACTIVE_TOKENS.discard(token)


def _schedule_public_subscription_reconcile(token: str) -> bool:
    token_str = str(token or "").strip()
    if not token_str:
        return False

    with _PUBLIC_PROFILE_RECONCILE_LOCK:
        if token_str in _PUBLIC_PROFILE_RECONCILE_ACTIVE_TOKENS:
            return False
        _PUBLIC_PROFILE_RECONCILE_ACTIVE_TOKENS.add(token_str)

    Thread(
        target=_reconcile_public_subscription_profile,
        args=(token_str,),
        name=f"fwrouter-xray-public-reconcile:{token_str[:16]}",
        daemon=True,
    ).start()
    return True


class XrayClientCreateRequest(BaseModel):
    alias: str | None = None
    email: str | None = None
    requested_by: str | None = "api"
    allow_blocked_egress: bool = False


class XrayClientAliasRequest(BaseModel):
    alias: str | None = None
    requested_by: str | None = "api"


class XrayRequestedByRequest(BaseModel):
    requested_by: str | None = "api"


@router.get("/xray", response_model=ApiResponse)
def get_xray_endpoint() -> ApiResponse:
    ok, payload = xray_service_call(get_xray_status)
    return ApiResponse(
        ok=ok,
        data={"xray": payload} if ok else {},
        error=None if ok else payload["error"],
    )


@router.get("/xray/clients", response_model=ApiResponse)
def list_xray_clients_endpoint() -> ApiResponse:
    ok, payload = xray_service_call(list_xray_clients)
    return ApiResponse(
        ok=ok,
        data={"clients": payload} if ok else {},
        error=None if ok else payload["error"],
    )


@router.post("/xray/clients", response_model=ApiResponse)
def create_xray_client_endpoint(request: XrayClientCreateRequest) -> ApiResponse:
    ok, payload = xray_service_call(
        create_xray_client,
        alias=request.alias,
        email=request.email,
        requested_by=request.requested_by or "api",
        allow_blocked_egress=request.allow_blocked_egress,
    )
    if ok and payload["ok"]:
        return ApiResponse(ok=True, data={"xray_client": payload})
    return ApiResponse(
        ok=False,
        data={"xray_client": payload} if ok else {},
        error=payload.get("error")
        if not ok
        else {
            "code": payload["result"]["error_code"] or "XRAY_CREATE_FAILED",
            "message": payload["result"]["message"],
        },
    )


@router.patch("/xray/clients/{client_id}", response_model=ApiResponse)
def update_xray_client_alias_endpoint(client_id: str, request: XrayClientAliasRequest) -> ApiResponse:
    ok, payload = xray_service_call(
        update_xray_client_alias,
        client_id,
        alias=request.alias,
        requested_by=request.requested_by or "api",
    )
    if ok and payload["ok"]:
        return ApiResponse(ok=True, data={"xray_client": payload})
    return ApiResponse(
        ok=False,
        data={"xray_client": payload} if ok else {},
        error=payload.get("error")
        if not ok
        else {
            "code": payload["result"]["error_code"] or "XRAY_ALIAS_UPDATE_FAILED",
            "message": payload["result"]["message"],
        },
    )


@router.delete("/xray/clients/{client_id}", response_model=ApiResponse)
def delete_xray_client_endpoint(client_id: str, request: XrayRequestedByRequest) -> ApiResponse:
    ok, payload = xray_service_call(delete_xray_client, client_id, requested_by=request.requested_by or "api")
    if ok and payload["ok"]:
        return ApiResponse(ok=True, data={"xray_client": payload})
    return ApiResponse(
        ok=False,
        data={"xray_client": payload} if ok else {},
        error=payload.get("error")
        if not ok
        else {
            "code": payload["result"]["error_code"] or "XRAY_DELETE_FAILED",
            "message": payload["result"]["message"],
        },
    )


@router.post("/xray/reload", response_model=ApiResponse)
def reload_xray_endpoint(request: XrayRequestedByRequest) -> ApiResponse:
    ok, payload = xray_service_call(reload_xray, requested_by=request.requested_by or "api")
    if ok and payload["ok"]:
        return ApiResponse(ok=True, data={"xray": payload})
    return ApiResponse(
        ok=False,
        data={"xray": payload} if ok else {},
        error=payload.get("error")
        if not ok
        else {
            "code": payload["result"]["error_code"] or "XRAY_RELOAD_FAILED",
            "message": payload["result"]["message"],
        },
    )


@router.post("/xray/sync-subjects", response_model=ApiResponse)
def sync_xray_subjects_endpoint(request: XrayRequestedByRequest) -> ApiResponse:
    ok, payload = xray_service_call(sync_xray_subjects, requested_by=request.requested_by or "api")
    if ok and payload["ok"]:
        return ApiResponse(ok=True, data={"xray": payload})
    return ApiResponse(
        ok=False,
        data={"xray": payload} if ok else {},
        error=payload.get("error")
        if not ok
        else {
            "code": "XRAY_SYNC_FAILED",
            "message": "Xray subject sync failed.",
        },
    )


@router.get("/xray/clients/{client_id}/subscription", response_model=ApiResponse)
def export_xray_subscription_endpoint(client_id: str) -> ApiResponse:
    ok, payload = xray_service_call(export_xray_subscription, client_id)
    if ok and payload["ok"]:
        return ApiResponse(ok=True, data={"subscription": payload})
    return ApiResponse(
        ok=False,
        data={"subscription": payload} if ok else {},
        error=payload.get("error")
        if not ok
        else {
            "code": payload["result"]["error_code"] or "XRAY_EXPORT_FAILED",
            "message": payload["result"]["message"],
        },
    )

@router.get("/xray/clients/{client_id}/subscription.txt")
def export_xray_subscription_text_endpoint(
    client_id: str,
    request: Request,
    base64_encode: bool = True,
    subscription_format: str = Query("auto", alias="format"),
) -> Response:
    detected_subscription_format = _subscription_format_from_request(
        request,
        format=subscription_format,
    )

    if client_id == "vpn-auto":
        ok, payload = xray_service_call(
            export_xray_vpn_auto_subscription_text,
            base64_encode=False,
            requested_by="vpn_auto_subscription",
        )
    else:
        ok, payload = xray_service_call(
            export_xray_subscription_text,
            client_id,
            base64_encode=False,
        )

    if not ok or not payload.get("ok"):
        message = "Xray subscription export failed."
        if isinstance(payload, dict):
            message = (
                payload.get("error_message")
                or ((payload.get("error") or {}).get("message") if isinstance(payload.get("error"), dict) else None)
                or message
            )

        return Response(
            content=str(message),
            media_type="text/plain; charset=utf-8",
            status_code=404,
        )

    user_agent = str(request.headers.get("user-agent") or "").lower()
    effective_base64_encode = bool(base64_encode)

    # Happ reliably fetches the subscription, but may not activate nodes from
    # base64-encoded V2Ray subscription bodies in this integration path.
    # For Happ, return plain vless:// lines under the same public URL.
    if "happ/" in user_agent:
        effective_base64_encode = False

    return _subscription_text_response(
        payload=payload,
        subscription_format=detected_subscription_format,
        base64_encode=effective_base64_encode,
    )


@public_router.get("/s/{token}")
def export_public_subscription_endpoint(
    token: str,
    request: Request,
    subscription_format: str = Query("auto", alias="format"),
) -> Response:
    ok, payload = xray_service_call(
        export_subscription_profile_text,
        token,
        user_agent=str(request.headers.get("user-agent") or ""),
        requested_format=subscription_format,
    )
    if not ok or not payload.get("ok"):
        message = "Subscription export failed."
        if isinstance(payload, dict):
            message = (
                payload.get("error_message")
                or ((payload.get("error") or {}).get("message") if isinstance(payload.get("error"), dict) else None)
                or message
            )
        return Response(
            content=str(message),
            media_type="text/plain; charset=utf-8",
            status_code=404,
        )
    if not ok or not payload.get("ok"):
        message = "Subscription export failed."
        if isinstance(payload, dict):
            message = (
                payload.get("error_message")
                or ((payload.get("error") or {}).get("message") if isinstance(payload.get("error"), dict) else None)
                or message
            )
        return Response(
            content=str(message),
            media_type="text/plain; charset=utf-8",
            status_code=404,
        )

    # Do not block subscription export on Xray profile reconciliation, and do
    # not let Uvicorn wait for this work during graceful shutdown.
    _schedule_public_subscription_reconcile(token)

    return Response(
        content=str(payload.get("content") or ""),
        media_type=str(payload.get("media_type") or "text/plain; charset=utf-8"),
        headers=dict(payload.get("headers") or {}),
    )
