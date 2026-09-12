from __future__ import annotations

import json
from typing import Any

from fwrouter_api.services.server_subject_overrides import sync_applied_runtime_binding_override_statuses
from fwrouter_api.services.xray_bindings import collect_xray_runtime_bindings
from fwrouter_api.services.xray_common import _strip_raw_payload, _xray_adapter, _xray_facade_attr, _xray_managed_runtime_blocked


def _verify_active_config_bindings(bindings: list[dict[str, Any]]) -> dict[str, Any]:
    adapter = _xray_adapter()
    config_path = getattr(adapter, "config_path", None)
    if config_path is None:
        return {
            "ok": True,
            "status": "skipped",
            "reason": "adapter_config_path_unavailable",
        }

    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {
            "ok": False,
            "status": "failed",
            "reason": "active_config_unreadable",
            "error": str(exc),
        }

    outbounds = payload.get("outbounds") if isinstance(payload.get("outbounds"), list) else []
    outbound_tags = {
        str(outbound.get("tag") or "")
        for outbound in outbounds
        if isinstance(outbound, dict)
    }
    routing = payload.get("routing") if isinstance(payload.get("routing"), dict) else {}
    rules = routing.get("rules") if isinstance(routing.get("rules"), list) else []

    missing_outbounds: list[dict[str, Any]] = []
    missing_rules: list[dict[str, Any]] = []
    wrong_api_rules: list[dict[str, Any]] = []
    verified = 0
    for binding in bindings:
        email = str(binding.get("client_email") or "").strip()
        handoff = binding.get("handoff") if isinstance(binding.get("handoff"), dict) else {}
        expected_outbound = str(handoff.get("outbound_tag") or "").strip()
        if not email or not expected_outbound:
            continue
        verified += 1
        if expected_outbound not in outbound_tags:
            missing_outbounds.append({"email": email, "outbound": expected_outbound})
            continue
        has_expected_rule = False
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            users = rule.get("user") or []
            if isinstance(users, str):
                users = [users]
            inbound_tags = rule.get("inboundTag") or []
            if isinstance(inbound_tags, str):
                inbound_tags = [inbound_tags]
            if email not in {str(item) for item in users}:
                continue
            if "vless-ws" not in {str(item) for item in inbound_tags}:
                continue
            outbound = str(rule.get("outboundTag") or "")
            if outbound == expected_outbound:
                has_expected_rule = True
            if outbound == "fwrouter-api":
                wrong_api_rules.append({"email": email, "rule": rule})
        if not has_expected_rule:
            missing_rules.append({"email": email, "outbound": expected_outbound})

    ok = not missing_outbounds and not missing_rules and not wrong_api_rules
    return {
        "ok": ok,
        "status": "verified" if ok else "failed",
        "verified_bindings_count": verified,
        "missing_outbounds": missing_outbounds,
        "missing_rules": missing_rules,
        "wrong_api_rules": wrong_api_rules,
    }


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

    convergence = _verify_active_config_bindings(bindings)
    if not convergence.get("ok"):
        payload = {
            "ok": False,
            "status": "failed",
            "stage": "runtime_convergence",
            "bindings_count": len(bindings),
            "convergence": convergence,
            "result": {
                "message": "Xray active config did not converge to expected scoped egress bindings.",
                "error_code": "XRAY_BINDINGS_CONVERGENCE_FAILED",
                "details": convergence,
            },
            "mihomo_handoff_prepare": mihomo_handoff_prepare,
        }
        _xray_facade_attr("write_operational_log")(
            event_type="xray_binding_materialization_failed",
            level="warning",
            message=payload["result"]["message"],
            details={**payload, "requested_by": requested_by},
        )
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
        "convergence": convergence,
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
