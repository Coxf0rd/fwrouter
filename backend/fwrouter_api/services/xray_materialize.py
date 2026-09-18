from __future__ import annotations

import json
import time
from typing import Any

from fwrouter_api.services.server_subject_overrides import sync_applied_runtime_binding_override_statuses
from fwrouter_api.services.xray_bindings import collect_xray_runtime_bindings
from fwrouter_api.services.xray_common import _strip_raw_payload, _xray_adapter, _xray_facade_attr, _xray_managed_runtime_blocked


def _load_active_config_payload() -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    adapter = _xray_adapter()
    config_path = getattr(adapter, "config_path", None)
    if config_path is None:
        return None, {
            "ok": True,
            "status": "skipped",
            "reason": "adapter_config_path_unavailable",
        }

    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return None, {
            "ok": False,
            "status": "failed",
            "reason": "active_config_unreadable",
            "error": str(exc),
        }

    if not isinstance(payload, dict):
        return None, {
            "ok": False,
            "status": "failed",
            "reason": "active_config_invalid",
        }
    return payload, None


def _xray_config_sections(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], set[str], list[dict[str, Any]], list[dict[str, Any]]]:
    inbounds = [
        inbound
        for inbound in (payload.get("inbounds") if isinstance(payload.get("inbounds"), list) else [])
        if isinstance(inbound, dict)
    ]
    outbounds = [
        outbound
        for outbound in (payload.get("outbounds") if isinstance(payload.get("outbounds"), list) else [])
        if isinstance(outbound, dict)
    ]
    outbound_tags = {str(outbound.get("tag") or "") for outbound in outbounds}
    routing = payload.get("routing") if isinstance(payload.get("routing"), dict) else {}
    rules = [
        rule
        for rule in (routing.get("rules") if isinstance(routing.get("rules"), list) else [])
        if isinstance(rule, dict)
    ]
    return inbounds, outbound_tags, outbounds, rules


def _client_present_in_vless_inbound(
    inbounds: list[dict[str, Any]],
    *,
    client_id: str | None,
    email: str | None,
) -> bool:
    wanted_id = str(client_id or "").strip()
    wanted_email = str(email or "").strip()
    for inbound in inbounds:
        if str(inbound.get("tag") or "") != "vless-ws":
            continue
        settings = inbound.get("settings") if isinstance(inbound.get("settings"), dict) else {}
        clients = settings.get("clients") if isinstance(settings.get("clients"), list) else []
        for client in clients:
            if not isinstance(client, dict):
                continue
            if wanted_id and str(client.get("id") or "").strip() == wanted_id:
                return True
            if wanted_email and str(client.get("email") or "").strip() == wanted_email:
                return True
    return False


def _verify_active_config_bindings(bindings: list[dict[str, Any]]) -> dict[str, Any]:
    payload, load_error = _load_active_config_payload()
    if load_error is not None:
        if load_error.get("reason") == "adapter_config_path_unavailable":
            return load_error
        return load_error
    if payload is None:
        return {
            "ok": False,
            "status": "failed",
            "reason": "active_config_unavailable",
        }

    inbounds, outbound_tags, outbounds, rules = _xray_config_sections(payload)

    missing_clients: list[dict[str, Any]] = []
    missing_outbounds: list[dict[str, Any]] = []
    missing_rules: list[dict[str, Any]] = []
    invalid_handoffs: list[dict[str, Any]] = []
    wrong_api_rules: list[dict[str, Any]] = []
    verified = 0
    for binding in bindings:
        email = str(binding.get("client_email") or "").strip()
        client_id = str(binding.get("client_uuid") or binding.get("client_id") or "").strip()
        handoff = binding.get("handoff") if isinstance(binding.get("handoff"), dict) else {}
        expected_outbound = str(handoff.get("outbound_tag") or "").strip()
        if not email or not expected_outbound:
            continue
        verified += 1
        if not _client_present_in_vless_inbound(inbounds, client_id=client_id, email=email):
            missing_clients.append({"email": email, "client_id": client_id})
        if expected_outbound not in outbound_tags:
            missing_outbounds.append({"email": email, "outbound": expected_outbound})
        outbound_payload = next(
            (outbound for outbound in outbounds if str(outbound.get("tag") or "") == expected_outbound),
            None,
        )
        servers = []
        if isinstance(outbound_payload, dict):
            settings = outbound_payload.get("settings") if isinstance(outbound_payload.get("settings"), dict) else {}
            servers = settings.get("servers") if isinstance(settings.get("servers"), list) else []
        expected_port = int(handoff.get("port") or 0)
        expected_address = str(handoff.get("listen") or "").strip()
        if (
            not isinstance(outbound_payload, dict)
            or str(outbound_payload.get("protocol") or "") != "socks"
            or not any(
                isinstance(server, dict)
                and str(server.get("address") or "") == expected_address
                and int(server.get("port") or 0) == expected_port
                for server in servers
            )
        ):
            invalid_handoffs.append(
                {
                    "email": email,
                    "outbound": expected_outbound,
                    "expected_address": expected_address,
                    "expected_port": expected_port,
                }
            )
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

    ok = (
        not missing_clients
        and not missing_outbounds
        and not invalid_handoffs
        and not missing_rules
        and not wrong_api_rules
    )
    return {
        "ok": ok,
        "status": "verified" if ok else "failed",
        "verified_bindings_count": verified,
        "missing_clients": missing_clients,
        "missing_outbounds": missing_outbounds,
        "invalid_handoffs": invalid_handoffs,
        "missing_rules": missing_rules,
        "wrong_api_rules": wrong_api_rules,
    }


def verify_xray_client_runtime_convergence(
    *,
    client_id: str | None,
    email: str | None = None,
    expect_present: bool,
    timeout_seconds: float = 3.0,
    poll_interval_seconds: float = 0.1,
) -> dict[str, Any]:
    """Bounded verification for one VLESS client's effective generated runtime."""

    deadline = time.monotonic() + max(timeout_seconds, 0.0)
    last: dict[str, Any] | None = None
    while True:
        payload, load_error = _load_active_config_payload()
        if load_error is not None and load_error.get("reason") != "adapter_config_path_unavailable":
            last = load_error
        elif load_error is not None:
            last = {
                "ok": True,
                "status": "skipped",
                "reason": "adapter_config_path_unavailable",
            }
        elif payload is not None:
            inbounds, _, _, rules = _xray_config_sections(payload)
            present = _client_present_in_vless_inbound(
                inbounds,
                client_id=client_id,
                email=email,
            )
            stale_api_rules = []
            wanted_email = str(email or "").strip()
            if wanted_email:
                for rule in rules:
                    users = rule.get("user") or []
                    if isinstance(users, str):
                        users = [users]
                    if wanted_email not in {str(item) for item in users}:
                        continue
                    if str(rule.get("outboundTag") or "") == "fwrouter-api":
                        stale_api_rules.append(rule)

            ok = present is expect_present and not stale_api_rules
            last = {
                "ok": ok,
                "status": "verified" if ok else "failed",
                "client_id": client_id,
                "email": email,
                "expect_present": expect_present,
                "present": present,
                "stale_api_rules": stale_api_rules,
            }
            if ok:
                return last

        if time.monotonic() >= deadline:
            break
        time.sleep(max(poll_interval_seconds, 0.01))

    return last or {
        "ok": False,
        "status": "failed",
        "reason": "runtime_convergence_timeout",
        "client_id": client_id,
        "email": email,
        "expect_present": expect_present,
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
        return payload

    convergence = _verify_active_config_bindings(bindings)
    if not convergence.get("ok"):
        rollback = None
        restore_last_good = getattr(_xray_adapter(), "restore_last_good_config", None)
        if callable(restore_last_good):
            restored = restore_last_good()
            rollback = {
                "ok": bool(restored.ok),
                "error_code": restored.error_code,
                "message": restored.message,
                "details": _strip_raw_payload(restored.details),
            }
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
            "rollback": rollback,
        }
        _xray_facade_attr("write_operational_log")(
            event_type="xray_binding_materialization_failed",
            level="warning",
            message=payload["result"]["message"],
            details={**payload, "requested_by": requested_by},
        )
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
