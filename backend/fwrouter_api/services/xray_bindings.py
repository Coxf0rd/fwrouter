from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fwrouter_api.db.connection import db_session
from fwrouter_api.services.artifacts import atomic_write_json
from fwrouter_api.services.custom_servers import (
    VIRTUAL_XRAY_VPN_AUTO_SERVER_ID,
    VIRTUAL_XRAY_VPN_AUTO_SERVER_NAME,
)
from fwrouter_api.services.subjects import get_subject
import fwrouter_api.services.subject_policy as subject_policy_service
from fwrouter_api.services.xray_handoff import build_xray_handoff_assignments
from fwrouter_api.services.xray_runtime_state import (
    _load_server_config_for_xray_binding,
    _xray_bindings_path,
)


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_binding_for_state(binding: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in binding.items()
        if key not in {"server_config"}
    }


def _bindings_for_state(bindings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [_safe_binding_for_state(binding) for binding in bindings]


def _annotate_bindings_with_handoff(bindings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    handoff_by_server = {
        str(assignment["selected_server_id"]): assignment
        for assignment in build_xray_handoff_assignments(bindings)
    }
    annotated: list[dict[str, Any]] = []
    for binding in bindings:
        updated = dict(binding)
        selected_server_id = str(binding.get("selected_server_id") or "").strip()
        handoff = handoff_by_server.get(selected_server_id)
        if handoff is not None:
            updated["handoff"] = {
                "listener_name": handoff["listener_name"],
                "listen": handoff["listen"],
                "port": handoff["port"],
                "outbound_tag": handoff["tag"],
            }
        annotated.append(updated)
    return annotated


def collect_xray_runtime_bindings() -> list[dict[str, Any]]:
    with db_session() as connection:
        rows = connection.execute(
            """
            SELECT s.subject_id
            FROM subjects AS s
            WHERE s.implementation_kind = 'xray'
              AND COALESCE(json_extract(s.metadata_json, '$.detail.enabled'), 1) = 1
              AND s.is_active = 1
              AND s.is_deleted = 0
            ORDER BY s.subject_id
            """
        ).fetchall()

    routing = subject_policy_service.get_routing_snapshot()
    runtime_enforcement = subject_policy_service.build_runtime_enforcement_state()
    bypass_state = subject_policy_service.get_core_bypass_state()
    bindings: list[dict[str, Any]] = []
    for row in rows:
        subject = get_subject(str(row["subject_id"]))
        if not isinstance(subject, dict):
            continue
        subject = subject_policy_service.enrich_subject_with_effective_state(
            subject,
            routing=routing,
            runtime_enforcement=runtime_enforcement,
            bypass_state=bypass_state,
        )
        binding = _build_binding_for_subject(subject)
        if binding is not None:
            bindings.append(binding)

    return _annotate_bindings_with_handoff(bindings)


def get_xray_handoff_listeners(bindings: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    active_bindings = bindings if bindings is not None else collect_xray_runtime_bindings()
    return build_xray_handoff_assignments(active_bindings)


def _build_binding_for_subject(subject: dict[str, Any]) -> dict[str, Any] | None:
    detail = subject.get("detail") if isinstance(subject.get("detail"), dict) else {}
    effective_state = subject.get("effective_state") if isinstance(subject.get("effective_state"), dict) else {}
    scoped_runtime = (
        effective_state.get("scoped_runtime")
        if isinstance(effective_state.get("scoped_runtime"), dict)
        else {}
    )
    client_uuid = str(detail.get("client_uuid") or "").strip()
    client_id = str(detail.get("client_id") or "").strip()
    selected_server_id = effective_state.get("selected_server_id")
    selected_server_source = effective_state.get("selected_server_source")
    if not client_uuid and not client_id:
        return None
    if str(effective_state.get("dataplane_path") or "") != "vpn":
        return None

    target_server_id_for_handoff = selected_server_id
    target_server_name = None
    target_server_raw_config = None

    if selected_server_source == "vpn_auto" or str(selected_server_id) == VIRTUAL_XRAY_VPN_AUTO_SERVER_ID:
        target_server_id_for_handoff = "vpn-global"
        target_server_name = VIRTUAL_XRAY_VPN_AUTO_SERVER_NAME
        server_config = None
    else:
        if selected_server_id is None:
            return None
        server_config = _load_server_config_for_xray_binding(
            str(selected_server_id) if selected_server_id is not None else None,
        )
        if server_config is None:
            return None
        target_server_name = server_config.get("server_name")
        target_server_raw_config = server_config.get("raw")

    return {
        "subject_id": subject.get("subject_id"),
        "client_id": client_id or client_uuid,
        "client_uuid": client_uuid or client_id,
        "client_email": detail.get("email"),
        "selected_server_id": target_server_id_for_handoff,
        "selected_server_source": effective_state.get("selected_server_source"),
        "handoff_proxy_name": (
            target_server_id_for_handoff
            if target_server_id_for_handoff == "vpn-global"
            else str(target_server_name or target_server_id_for_handoff)
        ),
        "server_name": target_server_name,
        "server_config": target_server_raw_config,
        "match_key": scoped_runtime.get("match_key"),
        "status": "pending",
        "applied_at": _utc_timestamp(),
    }


def _write_xray_bindings_state(bindings: list[dict[str, Any]], *, applied_ok: bool = False) -> dict[str, Any]:
    safe_bindings = _bindings_for_state(bindings)
    if applied_ok:
        for binding in safe_bindings:
            binding["status"] = "applied"

    handoff_listeners = get_xray_handoff_listeners(bindings)
    payload = {
        "bindings_version": 1,
        "generated_at": _utc_timestamp(),
        "bindings_count": len(safe_bindings),
        "applied_count": len([binding for binding in safe_bindings if binding.get("status") == "applied"]),
        "bindings": safe_bindings,
        "handoff_count": len(handoff_listeners),
        "handoff_listeners": handoff_listeners,
    }
    atomic_write_json(_xray_bindings_path(), payload)
    return payload
