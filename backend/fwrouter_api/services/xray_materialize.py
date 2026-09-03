from __future__ import annotations

from typing import Any

from fwrouter_api.services.server_subject_overrides import sync_applied_runtime_binding_override_statuses
from fwrouter_api.services.xray_bindings import collect_xray_runtime_bindings
from fwrouter_api.services.xray_common import _strip_raw_payload, _xray_adapter, _xray_facade_attr, _xray_managed_runtime_blocked


def materialize_xray_runtime_bindings(
    *,
    requested_by: str = "api",
    prepare_mihomo_handoff: bool = True,
    force_reload: bool = False,
) -> dict[str, Any]:
    blocked = _xray_managed_runtime_blocked("xray_runtime_bindings_materialize")
    if blocked is not None:
        return blocked

    bindings = _xray_facade_attr("collect_xray_runtime_bindings")()

    mihomo_handoff_prepare: dict[str, Any] | None = None
    if prepare_mihomo_handoff:
        from fwrouter_api.services.mihomo_config import reconcile_mihomo_runtime

        mihomo_handoff_prepare = reconcile_mihomo_runtime()
        if not mihomo_handoff_prepare.get("ok"):
            payload = {
                "ok": False,
                "status": "failed",
                "stage": "mihomo_handoff_prepare",
                "bindings_count": len(bindings),
                "mihomo_handoff_prepare": mihomo_handoff_prepare,
            }
            _xray_facade_attr("write_technical_log")(
                component="xray",
                event_type="xray_binding_materialization_failed",
                level="warning",
                message="Failed to prepare Mihomo Xray handoff listeners.",
                details=payload,
            )
            _xray_facade_attr("write_operational_log")(
                event_type="xray_binding_materialization_failed",
                level="warning",
                message="Failed to prepare Mihomo handoff for Xray bindings.",
                details={**payload, "requested_by": requested_by},
            )
            return payload

    result = _xray_adapter().materialize_client_bindings(bindings, force_reload=force_reload)
    if not result.ok:
        payload = {
            "ok": False,
            "status": "failed",
            "error": {
                "code": result.error_code or "XRAY_BINDINGS_APPLY_FAILED",
                "message": result.message,
            },
            "bindings_count": len(bindings),
            "result": {
                "message": result.message,
                "error_code": result.error_code,
                "details": _strip_raw_payload(result.details),
            },
            "mihomo_handoff_prepare": mihomo_handoff_prepare,
        }
        _xray_facade_attr("write_technical_log")(
            component="xray",
            event_type="xray_binding_materialization_failed",
            level="warning",
            message=result.message,
            details=payload,
        )
        _xray_facade_attr("write_operational_log")(
            event_type="xray_binding_materialization_failed",
            level="warning",
            message=result.message,
            details={**payload, "requested_by": requested_by},
        )
        # Even on failure, we write the state but with 'pending' status
        _xray_facade_attr("_write_xray_bindings_state")(bindings, applied_ok=False)
        return payload

    state = _xray_facade_attr("_write_xray_bindings_state")(bindings, applied_ok=result.ok)
    override_status_sync = sync_applied_runtime_binding_override_statuses(state.get("bindings", []))
    payload = {
        "ok": True,
        "status": "success",
        "bindings_count": len(bindings),
        "bindings_state": state,
        "override_status_sync": override_status_sync,
        "mihomo_handoff_prepare": mihomo_handoff_prepare,
        "result": {
            "message": result.message,
            "error_code": result.error_code,
            "details": _strip_raw_payload(result.details),
        },
    }
    _xray_facade_attr("write_operational_log")(
        event_type="xray_binding_materialized",
        level="info",
        message="Xray runtime binding metadata materialized.",
        details={**payload, "requested_by": requested_by},
    )
    _xray_facade_attr("write_technical_log")(
        component="xray",
        event_type="xray_binding_materialized",
        level="info",
        message="Xray runtime binding metadata materialized.",
        details={**payload, "requested_by": requested_by},
    )
    return payload
