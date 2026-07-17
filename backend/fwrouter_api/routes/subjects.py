from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from fwrouter_api.jobs.manager import get_default_job_manager
from fwrouter_api.schemas import ApiResponse
from fwrouter_api.services.action_contract import (
    build_conflict_response,
    build_job_action_response,
)
from fwrouter_api.services.apply_orchestrator import submit_apply_mutation
from fwrouter_api.services.jobs import JobLockConflictError
from fwrouter_api.services.subject_groups import resolve_xray_subscription_group_subject_ids
from fwrouter_api.services.subject_policy import (
    get_subject_with_effective_state,
    list_subjects_with_effective_state,
)
from fwrouter_api.services.subjects import find_subject_by_ip, update_subject_alias


router = APIRouter()


class SetSubjectModeRequest(BaseModel):
    mode: str
    actor_scope: str = "admin"
    requested_by: str | None = "api"
    run_now: bool = True


class SubjectSyncRequest(BaseModel):
    requested_by: str | None = "api"
    run_now: bool = True
    discover_docker: bool = True
    discover_host: bool = False
    discover_tailscale: bool = False
    discover_xray: bool = False
    include_all_tailscale_peers: bool = False
    lan_clients: list[dict[str, Any]] = Field(default_factory=list)
    tailscale_nodes: list[dict[str, Any]] = Field(default_factory=list)
    host_services: list[dict[str, Any]] = Field(default_factory=list)


class SetSubjectAliasRequest(BaseModel):
    alias: str | None = None


def _client_ip_from_request(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for") or request.headers.get("x-real-ip") or ""
    if forwarded:
        return forwarded.split(",", 1)[0].strip()
    client = request.client
    return str(client.host).strip() if client and client.host else ""


@router.get("/subjects", response_model=ApiResponse)
def list_subjects_endpoint(
    subject_type: str | None = None,
    is_active: bool | None = None,
    include_deleted: bool = False,
    limit: int = Query(default=100, ge=1, le=500),
) -> ApiResponse:
    subjects = list_subjects_with_effective_state(
        subject_type=subject_type,
        is_active=is_active,
        include_deleted=include_deleted,
        limit=limit,
    )
    return ApiResponse(ok=True, data={"subjects": subjects})


@router.get("/subjects/{subject_id}", response_model=ApiResponse)
def get_subject_endpoint(subject_id: str) -> ApiResponse:
    subject = get_subject_with_effective_state(subject_id)

    if subject is None:
        return ApiResponse(
            ok=False,
            data={},
            error={
                "code": "SUBJECT_NOT_FOUND",
                "message": f"Subject not found: {subject_id}",
            },
        )

    return ApiResponse(ok=True, data={"subject": subject})


@router.get("/ui/whoami", response_model=ApiResponse)
def get_ui_whoami_endpoint(request: Request) -> ApiResponse:
    client_ip = _client_ip_from_request(request)
    matched_subject = find_subject_by_ip(client_ip)
    subject = (
        get_subject_with_effective_state(str(matched_subject["subject_id"]))
        if matched_subject is not None
        else None
    )
    return ApiResponse(
        ok=True,
        data={
            "whoami": {
                "client_ip": client_ip,
                "subject": subject,
            }
        },
    )


@router.patch("/subjects/{subject_id}/alias", response_model=ApiResponse)
def set_subject_alias_endpoint(subject_id: str, request: SetSubjectAliasRequest) -> ApiResponse:
    subject = update_subject_alias(subject_id, request.alias)
    if subject is None:
        return ApiResponse(
            ok=False,
            data={},
            error={
                "code": "SUBJECT_NOT_FOUND",
                "message": f"Subject not found: {subject_id}",
            },
        )
    return ApiResponse(ok=True, data={"subject": subject})


@router.post("/subjects/{subject_id}/mode", response_model=ApiResponse)
def set_subject_mode_endpoint(subject_id: str, request: SetSubjectModeRequest) -> ApiResponse:
    intent = (
        "set_subject_user_mode"
        if request.actor_scope == "user"
        else "set_subject_admin_mode"
    )
    subject_ids = resolve_xray_subscription_group_subject_ids(subject_id)
    payload: dict[str, Any]
    if subject_ids:
        payload = {"subject_id": subject_id, "subject_ids": subject_ids, "mode": request.mode}
    else:
        payload = {"subject_id": subject_id, "mode": request.mode}

    try:
        job = submit_apply_mutation(
            intent=intent,
            payload=payload,
            requested_by=request.requested_by or "api",
            run_now=request.run_now,
        )
    except JobLockConflictError as exc:
        return build_conflict_response(exc)

    return build_job_action_response(job, result_key="subject_mode")


@router.post("/subjects/sync", response_model=ApiResponse)
def sync_subject_inventory_endpoint(request: SubjectSyncRequest) -> ApiResponse:
    manager = get_default_job_manager()
    input_data = {
        "discover_docker": request.discover_docker,
        "discover_host": request.discover_host,
        "discover_tailscale": request.discover_tailscale,
        "discover_xray": request.discover_xray,
        "include_all_tailscale_peers": request.include_all_tailscale_peers,
        "lan_clients": request.lan_clients,
        "tailscale_nodes": request.tailscale_nodes,
        "host_services": request.host_services,
    }

    try:
        job = manager.create(
            "subject_inventory_sync",
            lock_key="subject_inventory_sync",
            requested_by=request.requested_by,
            input_data=input_data,
        )
    except JobLockConflictError as exc:
        return build_conflict_response(exc)

    if request.run_now:
        job = manager.start_job_and_wait(job["job_id"]) or job

    return ApiResponse(ok=True, data={"job": job})
