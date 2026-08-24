from __future__ import annotations

from typing import Any

from fwrouter_api.adapters.xray import XrayAdapterError, XrayApplyResult, XrayClient
from fwrouter_api.services.logs import write_operational_log, write_technical_log
from fwrouter_api.services.xray_client_state import (
    _client_alias_map,
    _serialize_client,
    _set_local_alias,
    _sync_xray_inventory,
    _tombstone_local_xray_subject,
)
from fwrouter_api.services.xray_common import (
    _materialize_xray_runtime_bindings,
    _strip_raw_payload,
    _xray_adapter,
    _xray_client_create_preflight,
    _xray_managed_runtime_blocked,
)


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

        from fwrouter_api.services.xray_subscription_service import export_xray_subscription

        subscription = (
            export_xray_subscription(client_id)
            if result.details.get("client") and client_id
            else {"ok": False, "subscription_uri": None}
        )

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
    return payload


def delete_xray_client(client_id: str, *, requested_by: str = "api") -> dict[str, Any]:
    blocked = _xray_managed_runtime_blocked("xray_client_delete")
    if blocked is not None:
        return {**blocked, "client_id": client_id}

    try:
        result = _xray_adapter().delete_client(client_id)
    except XrayAdapterError as exc:
        if exc.code != "XRAY_CLIENT_NOT_FOUND":
            raise
        local_delete = _tombstone_local_xray_subject(client_id)
        if not local_delete["deleted"]:
            raise
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

    _sync_xray_inventory(requested_by)
    _materialize_xray_runtime_bindings(requested_by=requested_by)

    payload = {
        "ok": result.ok,
        "status": "success" if result.ok else "failed",
        "stage": str(result.details.get("stage") or ("completed" if result.ok else "reload")),
        "client_id": client_id,
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
    return payload


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
