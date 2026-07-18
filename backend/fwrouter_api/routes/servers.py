from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel

from fwrouter_api.schemas import ApiResponse
from fwrouter_api.services.action_contract import (
    build_conflict_response,
    build_job_action_response,
)
from fwrouter_api.services.apply_orchestrator import submit_apply_mutation
from fwrouter_api.services.custom_servers import (
    create_custom_https_proxy_server,
    delete_custom_https_proxy_server,
    get_server_api,
    list_servers_api,
    update_custom_https_proxy_server,
)
from fwrouter_api.services.dataplane_status import build_runtime_enforcement_state
from fwrouter_api.services.jobs import JobLockConflictError
from fwrouter_api.services.servers import (
    apply_global_auto_server,
    get_routing_global_state,
    apply_global_fixed_server,
    get_subject_server_override,
    replace_vpn_auto_servers,
    sync_servers_from_mihomo,
    update_server_preferences,
)


router = APIRouter()


class SetGlobalFixedServerRequest(BaseModel):
    server_id: str
    requested_by: str | None = "admin"
    confirm_switch: bool = False
    timeout_ms: int = 10000
    post_check: bool = True


class SetSubjectServerOverrideRequest(BaseModel):
    server_id: str
    requested_by: str | None = "user"
    run_now: bool = True


class UpdateGlobalRoutingRequest(BaseModel):
    mode: str | None = None
    selective_default: str | None = None
    server_mode: str | None = None
    requested_by: str | None = "api"
    run_now: bool = True


class CustomHttpsProxyServerRequest(BaseModel):
    server_name: str
    proxy_type: str = "http"
    host: str
    port: int
    username: str | None = None
    password: str | None = None
    tls: bool = True
    sni: str | None = None
    skip_cert_verify: bool = False
    path: str | None = None
    vpn_auto: bool = False
    global_list: bool = True
    requested_by: str | None = "api"


class UpdateServerPreferencesRequest(BaseModel):
    vpn_auto: bool | None = None
    vpn_auto_priority: int | None = None
    global_list: bool | None = None
    reconcile_mihomo: bool = True
    requested_by: str | None = "api"


class ReplaceVpnAutoServersRequest(BaseModel):
    server_ids: list[str]
    reconcile_mihomo: bool = True
    requested_by: str | None = "api"


@router.get("/servers", response_model=ApiResponse)
def list_servers_endpoint(
    inventory_state: str | None = None,
    vpn_auto: bool | None = None,
    global_list: bool | None = None,
    limit: int = Query(default=500, ge=1, le=1000),
) -> ApiResponse:
    servers = list_servers_api(
        inventory_state=inventory_state,
        vpn_auto=vpn_auto,
        global_list=global_list,
        limit=limit,
    )
    return ApiResponse(ok=True, data={"servers": servers})


@router.post("/servers/sync/mihomo", response_model=ApiResponse)
def sync_servers_from_mihomo_endpoint() -> ApiResponse:
    result = sync_servers_from_mihomo()
    return ApiResponse(ok=True, data={"sync": result})


@router.get("/servers/{server_id}", response_model=ApiResponse)
def get_server_endpoint(server_id: str) -> ApiResponse:
    server = get_server_api(server_id)

    if server is None:
        return ApiResponse(
            ok=False,
            data={},
            error={
                "code": "SERVER_NOT_FOUND",
                "message": f"Server not found: {server_id}",
            },
        )

    return ApiResponse(ok=True, data={"server": server})


@router.patch("/servers/{server_id}/preferences", response_model=ApiResponse)
def update_server_preferences_endpoint(
    server_id: str,
    request: UpdateServerPreferencesRequest,
) -> ApiResponse:
    result = update_server_preferences(
        server_id,
        vpn_auto=request.vpn_auto,
        vpn_auto_priority=request.vpn_auto_priority,
        global_list=request.global_list,
        reconcile_mihomo=request.reconcile_mihomo,
        requested_by=request.requested_by or "api",
    )

    if not result["ok"]:
        return ApiResponse(
            ok=False,
            data={"server_preferences": result},
            error={
                "code": result.get("error_code") or "SERVER_PREFERENCES_UPDATE_FAILED",
                "message": result.get("error_message") or "Server preferences update failed.",
            },
        )

    return ApiResponse(ok=True, data={"server_preferences": result})


@router.put("/servers/vpn-auto", response_model=ApiResponse)
def replace_vpn_auto_servers_endpoint(
    request: ReplaceVpnAutoServersRequest,
) -> ApiResponse:
    result = replace_vpn_auto_servers(
        request.server_ids,
        reconcile_mihomo=request.reconcile_mihomo,
        requested_by=request.requested_by or "api",
    )

    if not result["ok"]:
        return ApiResponse(
            ok=False,
            data={"vpn_auto": result},
            error={
                "code": result.get("error_code") or "VPN_AUTO_REPLACE_FAILED",
                "message": result.get("error_message") or "VPN-auto list replacement failed.",
            },
        )

    return ApiResponse(ok=True, data={"vpn_auto": result})


@router.post("/servers/custom/https", response_model=ApiResponse)
def create_custom_https_proxy_server_endpoint(
    request: CustomHttpsProxyServerRequest,
) -> ApiResponse:
    result = create_custom_https_proxy_server(
        server_name=request.server_name,
        proxy_type=request.proxy_type,
        host=request.host,
        port=request.port,
        username=request.username,
        password=request.password,
        tls=request.tls,
        sni=request.sni,
        skip_cert_verify=request.skip_cert_verify,
        path=request.path,
        vpn_auto=request.vpn_auto,
        global_list=request.global_list,
        requested_by=request.requested_by or "api",
    )
    if not result["ok"]:
        return ApiResponse(
            ok=False,
            data={"custom_server": result},
            error={
                "code": result["error_code"],
                "message": result["error_message"],
            },
        )
    return ApiResponse(ok=True, data={"custom_server": result})


@router.put("/servers/custom/https/{server_id}", response_model=ApiResponse)
def update_custom_https_proxy_server_endpoint(
    server_id: str,
    request: CustomHttpsProxyServerRequest,
) -> ApiResponse:
    result = update_custom_https_proxy_server(
        server_id,
        server_name=request.server_name,
        proxy_type=request.proxy_type,
        host=request.host,
        port=request.port,
        username=request.username,
        password=request.password,
        tls=request.tls,
        sni=request.sni,
        skip_cert_verify=request.skip_cert_verify,
        path=request.path,
        vpn_auto=request.vpn_auto,
        global_list=request.global_list,
        requested_by=request.requested_by or "api",
    )
    if not result["ok"]:
        return ApiResponse(
            ok=False,
            data={"custom_server": result},
            error={
                "code": result["error_code"],
                "message": result["error_message"],
            },
        )
    return ApiResponse(ok=True, data={"custom_server": result})


@router.post("/servers/custom/proxy", response_model=ApiResponse)
def create_custom_proxy_server_endpoint(
    request: CustomHttpsProxyServerRequest,
) -> ApiResponse:
    return create_custom_https_proxy_server_endpoint(request)


@router.put("/servers/custom/proxy/{server_id}", response_model=ApiResponse)
def update_custom_proxy_server_endpoint(
    server_id: str,
    request: CustomHttpsProxyServerRequest,
) -> ApiResponse:
    return update_custom_https_proxy_server_endpoint(server_id, request)


@router.delete("/servers/custom/proxy/{server_id}", response_model=ApiResponse)
def delete_custom_proxy_server_endpoint(
    server_id: str,
    requested_by: str = Query(default="api"),
) -> ApiResponse:
    return delete_custom_https_proxy_server_endpoint(server_id, requested_by)


@router.delete("/servers/custom/https/{server_id}", response_model=ApiResponse)
def delete_custom_https_proxy_server_endpoint(
    server_id: str,
    requested_by: str = Query(default="api"),
) -> ApiResponse:
    result = delete_custom_https_proxy_server(server_id, requested_by=requested_by)
    if not result["ok"]:
        return ApiResponse(
            ok=False,
            data={"custom_server": result},
            error={
                "code": result["error_code"],
                "message": result["error_message"],
            },
        )
    return ApiResponse(ok=True, data={"custom_server": result})


@router.get("/routing/global", response_model=ApiResponse)
def get_global_routing_endpoint() -> ApiResponse:
    state = get_routing_global_state()

    if state is None:
        state = {
            "desired_mode": "direct",
            "applied_mode": None,
            "selective_default": "direct",
            "server_mode": "auto",
            "desired_fixed_server_id": None,
            "applied_fixed_server_id": None,
            "fixed_server_until": None,
            "active_auto_server_id": None,
            "apply_state": "pending",
            "error_code": None,
            "error_message": None,
            "updated_at": None,
        }

    return ApiResponse(
        ok=True,
        data={
            "routing": state,
            "runtime_enforcement": build_runtime_enforcement_state(),
        },
    )


@router.post("/routing/global", response_model=ApiResponse)
def update_global_routing_endpoint(request: UpdateGlobalRoutingRequest) -> ApiResponse:
    has_mode = request.mode is not None
    has_selective = request.selective_default is not None
    has_server_mode = request.server_mode is not None

    if (int(has_mode) + int(has_selective) + int(has_server_mode)) != 1:
        return ApiResponse(
            ok=False,
            data={},
            error={
                "code": "GLOBAL_ROUTING_REQUEST_INVALID",
                "message": "Provide exactly one of: mode, selective_default, or server_mode.",
            },
        )

    if has_mode:
        intent = "set_global_mode"
        payload = {"mode": request.mode}
    elif has_selective:
        intent = "set_selective_default"
        payload = {"selective_default": request.selective_default}
    else:
        intent = "set_global_server_mode"
        payload = {"server_mode": request.server_mode}

    try:
        job = submit_apply_mutation(
            intent=intent,
            payload=payload,
            requested_by=request.requested_by or "api",
            run_now=request.run_now,
        )
    except JobLockConflictError as exc:
        return build_conflict_response(exc)

    return build_job_action_response(job, result_key="routing_mutation")


@router.post("/routing/global/fixed-server", response_model=ApiResponse)
def set_global_fixed_server_endpoint(
    request: SetGlobalFixedServerRequest,
) -> ApiResponse:
    if not request.confirm_switch:
        return ApiResponse(
            ok=False,
            data={},
            error={
                "code": "GLOBAL_FIXED_SERVER_CONFIRMATION_REQUIRED",
                "message": "Set confirm_switch=true to switch Mihomo vpn-global selector.",
            },
        )

    result = apply_global_fixed_server(
        request.server_id,
        requested_by=request.requested_by or "admin",
        timeout_ms=request.timeout_ms,
        post_check=request.post_check,
    )

    if not result["ok"]:
        return ApiResponse(
            ok=False,
            data={"global_fixed_server": result},
            error={
                "code": result["error_code"],
                "message": result["error_message"],
            },
        )

    return ApiResponse(ok=True, data={"global_fixed_server": result})


@router.delete("/routing/global/fixed-server", response_model=ApiResponse)
def clear_global_fixed_server_endpoint(
    confirm_switch: bool = Query(default=False),
    requested_by: str = Query(default="admin"),
) -> ApiResponse:
    if not confirm_switch:
        return ApiResponse(
            ok=False,
            data={},
            error={
                "code": "GLOBAL_AUTO_CONFIRMATION_REQUIRED",
                "message": "Set confirm_switch=true to switch Mihomo vpn-global selector back to vpn-auto.",
            },
        )

    result = apply_global_auto_server(requested_by=requested_by)

    if not result["ok"]:
        return ApiResponse(
            ok=False,
            data={"global_fixed_server": result},
            error={
                "code": result["error_code"],
                "message": result["error_message"],
            },
        )

    return ApiResponse(ok=True, data={"global_fixed_server": result})


@router.get("/subjects/{subject_id}/server-override", response_model=ApiResponse)
def get_subject_server_override_endpoint(subject_id: str) -> ApiResponse:
    override = get_subject_server_override(subject_id)
    return ApiResponse(ok=True, data={"server_override": override})


@router.post("/subjects/{subject_id}/server-override", response_model=ApiResponse)
def set_subject_server_override_endpoint(
    subject_id: str,
    request: SetSubjectServerOverrideRequest,
) -> ApiResponse:
    try:
        job = submit_apply_mutation(
            intent="set_subject_server_override",
            payload={"subject_id": subject_id, "server_id": request.server_id},
            requested_by=request.requested_by or "user",
            run_now=request.run_now,
        )
    except JobLockConflictError as exc:
        return build_conflict_response(exc)

    return build_job_action_response(job, result_key="server_override")


@router.delete("/subjects/{subject_id}/server-override", response_model=ApiResponse)
def clear_subject_server_override_endpoint(subject_id: str) -> ApiResponse:
    try:
        job = submit_apply_mutation(
            intent="clear_subject_server_override",
            payload={"subject_id": subject_id},
            requested_by="user",
            run_now=True,
        )
    except JobLockConflictError as exc:
        return build_conflict_response(exc)

    return build_job_action_response(job, result_key="server_override")
