from __future__ import annotations

from typing import Any

from fwrouter_api.adapters.xray import XrayAdapterError, XrayApplyResult, XrayClient
from fwrouter_api.jobs.manager import get_default_job_manager
from fwrouter_api.services.jobs import JobLockConflictError, get_active_lock_lease, get_job_without_cleanup
from fwrouter_api.services.logs import write_operational_log, write_technical_log
from fwrouter_api.services.subscription_profiles import ensure_subscription_identity
from fwrouter_api.services.xray_client_state import (
    _client_alias_map,
    _serialize_client,
    _set_local_alias,
    _sync_xray_inventory,
    _tombstone_local_xray_subject,
    cleanup_xray_client_projection,
)
from fwrouter_api.services.xray_common import (
    _materialize_xray_runtime_bindings,
    _strip_raw_payload,
    _xray_adapter,
    _xray_client_create_preflight,
    _xray_managed_runtime_blocked,
)


XRAY_CLIENT_CREATE_JOB_TYPE = "xray_client_create"
XRAY_CLIENT_DELETE_JOB_TYPE = "xray_client_delete"


def _normalize_xray_create_identity(*, alias: str | None, email: str | None) -> str:
    normalized_email = str(email or "").strip().lower()
    if normalized_email:
        return f"email:{normalized_email}"
    normalized_alias = str(alias or "").strip().lower()
    return f"alias:{normalized_alias}" if normalized_alias else "anonymous"


def _existing_xray_client_by_email(email: str | None) -> dict[str, Any] | None:
    normalized = str(email or "").strip().lower()
    if not normalized:
        return None
    aliases = _client_alias_map()
    for client in _xray_adapter().list_clients():
        if str(client.email or "").strip().lower() != normalized:
            continue
        payload = _serialize_client(
            client,
            alias_override=aliases.get(client.client_id) or aliases.get(client.client_uuid),
        )
        payload.pop("raw", None)
        return payload
    return None


def list_xray_clients() -> list[dict[str, Any]]:
    aliases = _client_alias_map()
    return [
        _serialize_client(client, alias_override=aliases.get(client.client_id) or aliases.get(client.client_uuid))
        for client in _xray_adapter().list_clients()
    ]


def create_xray_client(
    *,
    alias: str | None = None,
    email: str | None = None,
    requested_by: str = "api",
    allow_blocked_egress: bool = False,
) -> dict[str, Any]:
    blocked = _xray_managed_runtime_blocked("xray_client_create")
    if blocked is not None:
        return blocked

    preflight = _xray_client_create_preflight(allow_blocked_egress=allow_blocked_egress)
    if not preflight["ok"]:
        payload = {
            "ok": False,
            "status": "blocked",
            "stage": "preflight",
            "client": None,
            "subscription_uri": None,
            "preflight": preflight,
            "result": {
                "message": preflight["message"],
                "error_code": preflight["code"],
                "details": preflight,
            },
        }
        write_operational_log(
            event_type="xray_client_create_blocked",
            level="warning",
            message=preflight["message"],
            details={**payload, "requested_by": requested_by},
        )
        return payload

    result = _xray_adapter().create_client(alias=alias, email=email)
    client_payload = dict(result.details.get("client") or {})
    client_id = str(client_payload.get("client_id") or "")

    if not result.ok:
        subscription = {"ok": False, "subscription_uri": None}
    else:
        if client_id:
            _sync_xray_inventory(requested_by)
            if alias is not None:
                _set_local_alias(client_id, alias)
            _materialize_xray_runtime_bindings(requested_by=requested_by)

            link_token = str(email or "").strip().lower()
            if link_token:
                ensure_subscription_identity(link_token, display_name=alias)
                from fwrouter_api.services.xray_subscription_service import reconcile_xray_subscription_profile_nodes

                reconcile_xray_subscription_profile_nodes(
                    requested_by=requested_by,
                    token_or_slug=link_token,
                    materialize=True,
                )

        from fwrouter_api.services.xray_subscription_service import export_xray_subscription

        try:
            subscription = (
                export_xray_subscription(client_id)
                if result.details.get("client") and client_id
                else {"ok": False, "subscription_uri": None}
            )
        except Exception as exc:
            write_technical_log(
                component="xray",
                level="warning",
                event_type="xray_client_subscription_export_failed",
                message="Xray client was created, but compatibility subscription export failed.",
                details={"client_id": client_id, "error": str(exc), "requested_by": requested_by},
            )
            subscription = {"ok": False, "subscription_uri": None}

    payload = {
        "ok": result.ok,
        "status": "success" if result.ok else "failed",
        "stage": str(result.details.get("stage") or ("completed" if result.ok else "reload")),
        "client": (
            _serialize_client(
                XrayClient(
                    client_id=client_payload.get("client_id", client_id),
                    client_uuid=client_payload.get("client_uuid", client_id),
                    email=client_payload.get("email"),
                    alias=alias,
                    enabled=bool(client_payload.get("enabled", True)),
                    raw=dict(client_payload.get("raw") or {}),
                ),
                alias_override=alias,
            )
            if client_payload
            else None
        ),
        "subscription_uri": subscription.get("subscription_uri"),
        "subscription_url": f"/s/{str(email or '').strip().lower()}" if str(email or "").strip() else None,
        "result": {
            "message": result.message,
            "error_code": result.error_code,
            "details": _strip_raw_payload(result.details),
        },
    }

    if isinstance(payload.get("client"), dict):
        payload["client"].pop("raw", None)

    write_operational_log(
        event_type="xray_client_created" if result.ok else "xray_client_create_failed",
        level="info" if result.ok else "warning",
        subject_id=f"xray:{client_id}" if client_id else None,
        message=result.message,
        details=_strip_raw_payload(payload),
    )
    write_operational_log(
        event_type="external_client.created" if result.ok else "external_client.create_failed",
        level="info" if result.ok else "warning",
        subject_id=f"xray:{client_id}" if client_id else None,
        message="External client created." if result.ok else "External client create failed.",
        details={
            "client_id": client_id,
            "alias": alias,
            "email": client_payload.get("email") or email,
            "requested_by": requested_by,
            "result": "success" if result.ok else "failed",
            "xray_result": payload["result"],
        },
    )
    return payload


def submit_xray_client_create(
    *,
    alias: str | None = None,
    email: str | None = None,
    requested_by: str = "api",
    allow_blocked_egress: bool = False,
) -> dict[str, Any]:
    manager = get_default_job_manager()
    lock_key = f"xray-client-create:{_normalize_xray_create_identity(alias=alias, email=email)}"
    active_lease = get_active_lock_lease(lock_key)
    if active_lease is not None:
        active_job = get_job_without_cleanup(str(active_lease["owner_job_id"])) or active_lease
        return {
            "ok": True,
            "status": "accepted",
            "stage": "existing_job",
            "job": active_job,
            "result": {
                "message": "Xray client create job is already running.",
                "error_code": None,
                "details": {"lock_key": lock_key},
            },
        }

    existing = _existing_xray_client_by_email(email)
    if existing is not None:
        return {
            "ok": True,
            "status": "success",
            "stage": "existing",
            "client": existing,
            "subscription_uri": None,
            "subscription_url": f"/s/{str(email or '').strip().lower()}",
            "result": {
                "message": "Xray client already exists.",
                "error_code": None,
                "details": {"client": existing},
            },
        }

    input_data = {
        "alias": alias,
        "email": email,
        "requested_by": requested_by,
        "allow_blocked_egress": allow_blocked_egress,
    }
    try:
        job = manager.create(
            XRAY_CLIENT_CREATE_JOB_TYPE,
            lock_key=lock_key,
            requested_by=requested_by,
            input_data=input_data,
        )
    except JobLockConflictError as exc:
        return {
            "ok": True,
            "status": "accepted",
            "stage": "existing_job",
            "job": exc.active_job,
            "result": {
                "message": "Xray client create job is already running.",
                "error_code": None,
                "details": {"lock_key": lock_key},
            },
        }
    job = manager.start_job_and_wait(job["job_id"], timeout_seconds=1) or job
    return {
        "ok": True,
        "status": "accepted",
        "stage": "queued" if str(job.get("status")) == "queued" else "running",
        "job": job,
        "result": {
            "message": "Xray client create accepted.",
            "error_code": None,
            "details": {"lock_key": lock_key},
        },
    }


def run_xray_client_create_job(job: dict[str, Any]) -> dict[str, Any]:
    input_data = job.get("input") if isinstance(job.get("input"), dict) else {}
    payload = create_xray_client(
        alias=input_data.get("alias"),
        email=input_data.get("email"),
        requested_by=str(input_data.get("requested_by") or job.get("requested_by") or "job"),
        allow_blocked_egress=bool(input_data.get("allow_blocked_egress", False)),
    )
    if not payload.get("ok"):
        return {
            "job_status": "failed",
            "status": "failed",
            "error_code": payload.get("result", {}).get("error_code") or "XRAY_CREATE_FAILED",
            "error_message": payload.get("result", {}).get("message") or "Xray client create failed.",
            "xray_client": payload,
        }
    return {
        "job_status": "success",
        "status": "success",
        "xray_client": payload,
    }


def delete_xray_client(client_id: str, *, requested_by: str = "api") -> dict[str, Any]:
    blocked = _xray_managed_runtime_blocked("xray_client_delete")
    if blocked is not None:
        return {**blocked, "client_id": client_id}

    local_cleanup: dict[str, Any] | None = None
    try:
        result = _xray_adapter().delete_client(client_id)
    except XrayAdapterError as exc:
        if exc.code != "XRAY_CLIENT_NOT_FOUND":
            raise
        local_delete = _tombstone_local_xray_subject(client_id)
        if not local_delete["deleted"]:
            result = XrayApplyResult(
                ok=True,
                message="Xray client delete noop.",
                details={
                    "stage": "noop",
                    "client_id": client_id,
                    "adapter_error": {
                        "code": exc.code,
                        "message": exc.message,
                        "details": exc.details,
                    },
                },
            )
        else:
            result = XrayApplyResult(
                ok=True,
                message="Stale Xray client entry deleted from FWRouter inventory.",
                details={
                    "stage": "local_inventory",
                    "client": local_delete["client"],
                    "adapter_error": {
                        "code": exc.code,
                        "message": exc.message,
                        "details": exc.details,
                    },
                },
            )
            local_cleanup = local_delete.get("cleanup")

    cleanup = {
        "subject_ids": [],
        "subjects_deleted": 0,
        "server_overrides_deleted": 0,
        "user_overrides_deleted": 0,
    }
    if result.ok:
        _sync_xray_inventory(requested_by)
        cleanup = cleanup_xray_client_projection(client_id)
        if local_cleanup:
            cleanup = {
                "subject_ids": list(
                    dict.fromkeys(
                        [*local_cleanup.get("subject_ids", []), *cleanup.get("subject_ids", [])]
                    )
                ),
                "subjects_deleted": int(local_cleanup.get("subjects_deleted") or 0)
                + int(cleanup.get("subjects_deleted") or 0),
                "server_overrides_deleted": int(local_cleanup.get("server_overrides_deleted") or 0)
                + int(cleanup.get("server_overrides_deleted") or 0),
                "user_overrides_deleted": int(local_cleanup.get("user_overrides_deleted") or 0)
                + int(cleanup.get("user_overrides_deleted") or 0),
            }
        _materialize_xray_runtime_bindings(requested_by=requested_by)

    payload = {
        "ok": result.ok,
        "status": "success" if result.ok else "failed",
        "stage": str(result.details.get("stage") or ("completed" if result.ok else "reload")),
        "client_id": client_id,
        "requested_by": requested_by,
        "cleanup": cleanup,
        "result": {
            "message": result.message,
            "error_code": result.error_code,
            "details": _strip_raw_payload(result.details),
        },
    }
    write_operational_log(
        event_type="xray_client_deleted" if result.ok else "xray_client_delete_failed",
        level="info" if result.ok else "warning",
        subject_id=f"xray:{client_id}",
        message=result.message,
        details=payload,
    )
    noop = str(payload.get("stage") or "").lower() == "noop"
    write_operational_log(
        event_type="external_client.delete_noop" if result.ok and noop else "external_client.deleted" if result.ok else "external_client.delete_failed",
        level="info" if result.ok else "warning",
        subject_id=f"xray:{client_id}",
        message="External client delete noop." if result.ok and noop else "External client deleted." if result.ok else "External client delete failed.",
        details={
            "client_id": client_id,
            "alias": None,
            "requested_by": requested_by,
            "result": "success" if result.ok else "failed",
            "cleanup": cleanup,
            "xray_result": payload["result"],
        },
    )
    return payload


def submit_xray_client_delete(client_id: str, *, requested_by: str = "api") -> dict[str, Any]:
    normalized = str(client_id or "").strip()
    manager = get_default_job_manager()
    lock_key = f"xray-client-delete:{normalized}"
    try:
        job = manager.create(
            XRAY_CLIENT_DELETE_JOB_TYPE,
            lock_key=lock_key,
            requested_by=requested_by,
            input_data={"client_id": normalized, "requested_by": requested_by},
        )
    except JobLockConflictError as exc:
        return {
            "ok": True,
            "status": "accepted",
            "stage": "existing_job",
            "job": exc.active_job,
            "result": {
                "message": "Xray client delete job is already running.",
                "error_code": None,
                "details": {"lock_key": lock_key},
            },
        }
    job = manager.start_job_and_wait(job["job_id"], timeout_seconds=1) or job
    return {
        "ok": True,
        "status": "accepted",
        "stage": "queued" if str(job.get("status")) == "queued" else "running",
        "job": job,
        "result": {
            "message": "Xray client delete accepted.",
            "error_code": None,
            "details": {"lock_key": lock_key},
        },
    }


def run_xray_client_delete_job(job: dict[str, Any]) -> dict[str, Any]:
    input_data = job.get("input") if isinstance(job.get("input"), dict) else {}
    payload = delete_xray_client(
        str(input_data.get("client_id") or ""),
        requested_by=str(input_data.get("requested_by") or job.get("requested_by") or "job"),
    )
    if not payload.get("ok"):
        return {
            "job_status": "failed",
            "status": "failed",
            "error_code": payload.get("result", {}).get("error_code") or "XRAY_DELETE_FAILED",
            "error_message": payload.get("result", {}).get("message") or "Xray client delete failed.",
            "xray_client": payload,
        }
    return {
        "job_status": "success",
        "status": "success",
        "xray_client": payload,
    }


def update_xray_client_alias(
    client_id: str,
    *,
    alias: str | None,
    requested_by: str = "api",
) -> dict[str, Any]:
    blocked = _xray_managed_runtime_blocked("xray_client_alias_update")
    if blocked is not None:
        return {**blocked, "client_id": client_id}

    result = _xray_adapter().update_client_alias(client_id, alias)
    _set_local_alias(client_id, alias)

    payload = {
        "ok": result.ok,
        "status": "success" if result.ok else "failed",
        "client": result.details.get("client"),
        "result": {
            "message": result.message,
            "error_code": result.error_code,
            "details": _strip_raw_payload(result.details),
        },
    }
    write_operational_log(
        event_type="xray_client_alias_updated" if result.ok else "xray_client_alias_update_failed",
        level="info" if result.ok else "warning",
        subject_id=f"xray:{client_id}",
        message=result.message,
        details={**payload, "requested_by": requested_by},
    )
    return payload


def reload_xray(*, requested_by: str = "api") -> dict[str, Any]:
    blocked = _xray_managed_runtime_blocked("xray_reload")
    if blocked is not None:
        return blocked

    materialized = _materialize_xray_runtime_bindings(requested_by=requested_by, force_reload=True)
    if not materialized["ok"]:
        return materialized
    payload = {
        "ok": True,
        "status": "success",
        "result": materialized["result"],
        "bindings_state": materialized["bindings_state"],
    }
    write_operational_log(
        event_type="xray_reloaded",
        level="info",
        message=str(materialized["result"]["message"]),
        details={**payload, "requested_by": requested_by},
    )
    return payload


def sync_xray_subjects(*, requested_by: str = "api") -> dict[str, Any]:
    blocked = _xray_managed_runtime_blocked("xray_subject_sync")
    if blocked is not None:
        return blocked

    result = _sync_xray_inventory(requested_by)
    if result["ok"]:
        materialized = _materialize_xray_runtime_bindings(requested_by=requested_by)
    else:
        materialized = None
    payload = {
        "ok": result["ok"],
        "status": "success" if result["ok"] else "failed",
        "sync": result,
    }
    if materialized is not None:
        payload["materialize"] = materialized
    write_operational_log(
        event_type="xray_subjects_synced" if result["ok"] else "xray_subjects_sync_failed",
        level="info" if result["ok"] else "warning",
        message="Xray subject inventory synced." if result["ok"] else "Xray subject inventory sync failed.",
        details={**payload, "requested_by": requested_by},
    )
    write_technical_log(
        component="xray",
        event_type="xray_subjects_synced" if result["ok"] else "xray_subjects_sync_failed",
        level="info" if result["ok"] else "warning",
        message="Xray subject sync completed." if result["ok"] else "Xray subject sync failed.",
        details={**payload, "requested_by": requested_by},
    )
    return payload


def xray_service_call(fn: Any, *args: Any, **kwargs: Any) -> tuple[bool, dict[str, Any]]:
    try:
        return True, fn(*args, **kwargs)
    except XrayAdapterError as exc:
        payload = {
            "ok": False,
            "status": "failed",
            "error": {
                "code": exc.code,
                "message": exc.message,
            },
            "details": exc.details,
        }
        write_technical_log(
            component="xray",
            event_type="xray_service_error",
            level="warning",
            message=exc.message,
            details=payload,
        )
        return False, payload
