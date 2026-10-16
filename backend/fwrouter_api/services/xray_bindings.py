from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import fwrouter_api.services.subject_policy as subject_policy_service
from fwrouter_api.db.connection import db_session
from fwrouter_api.services.artifacts import atomic_write_json
from fwrouter_api.services.custom_servers import (
    VIRTUAL_XRAY_VPN_AUTO_SERVER_ID,
    VIRTUAL_XRAY_VPN_AUTO_SERVER_NAME,
)
from fwrouter_api.services.subjects import _load_subject_details, _row_to_subject
from fwrouter_api.services.xray_handoff import build_xray_handoff_assignments
from fwrouter_api.services.xray_runtime_state import (
    _load_server_config_for_xray_binding,
    _xray_bindings_path,
)

_XRAY_PROJECTION_BATCH_SIZE = 500


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _applied_handoff_assignments() -> list[dict[str, Any]]:
    """Read applied listener tuples so routine renders retain live ports."""
    from fwrouter_api.services.xray_runtime_state import _load_xray_bindings_state

    state = _load_xray_bindings_state()
    assignments = state.get("handoff_listeners") if isinstance(state, dict) else []
    return [item for item in assignments if isinstance(item, dict)] if isinstance(assignments, list) else []


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
        for assignment in build_xray_handoff_assignments(
            bindings,
            preserve_assignments=_applied_handoff_assignments(),
        )
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


def _email_matches_prefix(email: Any, prefixes: tuple[str, ...]) -> bool:
    return isinstance(email, str) and any(email.lower().startswith(prefix) for prefix in prefixes)


def _load_xray_subject_projections(rows: list[Any]) -> tuple[
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
]:
    """Project selected subject rows and active overrides with bounded DB reads."""
    projected = [_row_to_subject(row) for row in rows]
    user_overrides: dict[str, dict[str, Any]] = {}
    server_overrides: dict[str, dict[str, Any]] = {}
    for offset in range(0, len(projected), _XRAY_PROJECTION_BATCH_SIZE):
        batch = projected[offset : offset + _XRAY_PROJECTION_BATCH_SIZE]
        details = _load_subject_details(batch)
        for subject in batch:
            subject["detail"] = details.get(str(subject["subject_id"]))

        subject_ids = [str(subject["subject_id"]) for subject in batch]
        user_overrides.update(subject_policy_service._load_active_user_overrides(subject_ids))
        server_overrides.update(subject_policy_service._load_active_server_overrides(subject_ids))
    return (
        {str(subject["subject_id"]): subject for subject in projected},
        user_overrides,
        server_overrides,
    )


def collect_xray_runtime_bindings(
    *, exclude_client_email_prefixes: tuple[str, ...] | list[str] | None = None,
) -> list[dict[str, Any]]:
    prefixes = tuple(str(prefix).lower() for prefix in (exclude_client_email_prefixes or ()) if prefix)
    email_projection = (
        "CASE WHEN json_valid(s.metadata_json) "
        "THEN json_extract(s.metadata_json, '$.detail.email') END"
        if prefixes else "NULL"
    )
    with db_session() as connection:
        rows = connection.execute(
            f"""
            SELECT s.*, {email_projection} AS client_email
            FROM subjects AS s
            WHERE s.implementation_kind = 'xray'
              AND COALESCE(json_extract(s.metadata_json, '$.detail.enabled'), 1) = 1
              AND s.is_active = 1
              AND s.is_deleted = 0
            ORDER BY s.subject_id
            """
        ).fetchall()

    # Missing or non-string email metadata remains in the conservative path.
    # Only an explicit matching managed prefix is excluded before enrichment.
    if prefixes:
        rows = [row for row in rows if not _email_matches_prefix(row["client_email"], prefixes)]
    if not rows and prefixes:
        return []

    routing = subject_policy_service.get_routing_snapshot()
    runtime_enforcement = subject_policy_service.build_runtime_enforcement_state()
    bypass_state = subject_policy_service.get_core_bypass_state()
    subjects_by_id, user_overrides, server_overrides = _load_xray_subject_projections(rows)
    bindings: list[dict[str, Any]] = []
    for row in rows:
        subject_id = str(row["subject_id"])
        subject = subjects_by_id.get(subject_id)
        if not isinstance(subject, dict):
            continue
        subject = subject_policy_service.enrich_subject_with_effective_state(
            subject,
            routing=routing,
            user_override=user_overrides.get(subject_id),
            server_override=server_overrides.get(subject_id),
            runtime_enforcement=runtime_enforcement,
            bypass_state=bypass_state,
        )
        binding = _build_binding_for_subject(subject)
        if binding is not None:
            bindings.append(binding)

    return _annotate_bindings_with_handoff(bindings)


def collect_xray_client_mode_directives(
    *, exclude_client_email_prefixes: tuple[str, ...] | list[str] | None = None,
) -> list[dict[str, Any]]:
    """Return explicit non-VPN Xray policies that require a per-user rule."""
    prefixes = tuple(str(prefix).lower() for prefix in (exclude_client_email_prefixes or ()) if prefix)
    with db_session() as connection:
        rows = connection.execute(
            """
            SELECT *,
                   json_extract(metadata_json, '$.detail.client_id') AS client_id,
                   json_extract(metadata_json, '$.detail.client_uuid') AS client_uuid,
                   json_extract(metadata_json, '$.detail.email') AS client_email
            FROM subjects
            WHERE implementation_kind = 'xray'
              AND subject_type = 'explicit_external_client'
              AND subject_role = 'vless_client'
              AND is_active = 1 AND is_deleted = 0
              AND COALESCE(json_extract(metadata_json, '$.detail.enabled'), 1) = 1
            ORDER BY subject_id
            """
        ).fetchall()
    if prefixes:
        rows = [row for row in rows if not _email_matches_prefix(row["client_email"], prefixes)]
    if not rows:
        return []

    routing = subject_policy_service.get_routing_snapshot()
    bypass_state = subject_policy_service.get_core_bypass_state()
    runtime_enforcement = subject_policy_service._default_subject_runtime_enforcement(
        routing=routing,
        bypass_state=bypass_state,
    )
    subjects_by_id, user_overrides, server_overrides = _load_xray_subject_projections(rows)
    directives = []
    for row in rows:
        mode = str(row["desired_mode"] or "enabled").strip().lower()
        subject_id = str(row["subject_id"])
        subject = subjects_by_id.get(subject_id)
        projected = (
            subject_policy_service.enrich_subject_with_effective_state(
                subject,
                routing=routing,
                user_override=user_overrides.get(subject_id),
                server_override=server_overrides.get(subject_id),
                runtime_enforcement=runtime_enforcement,
                bypass_state=bypass_state,
            )
            if isinstance(subject, dict)
            else None
        )
        effective_mode = str(((projected or {}).get("effective_state") or {}).get("effective_mode") or mode)
        if effective_mode == "direct":
            mode = "direct"
        if mode not in {"direct", "disabled", "selective"}:
            continue
        directives.append({
            "subject_id": str(row["subject_id"]),
            "client_id": str(row["client_id"] or ""),
            "client_uuid": str(row["client_uuid"] or ""),
            "client_email": str(row["client_email"] or ""),
            "desired_mode": str(row["desired_mode"] or "enabled"),
            "effective_mode": "unsupported_selective" if mode == "selective" else mode,
            "mode_support_state": "unsupported_legacy" if mode == "selective" else "legacy_supported_direct" if mode == "direct" else "supported",
        })
    return directives


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
    target_server_runtime_name = None
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
        target_server_runtime_name = server_config.get("runtime_name") or target_server_name
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
            else str(target_server_runtime_name or target_server_name or target_server_id_for_handoff)
        ),
        "server_name": target_server_name,
        "server_runtime_name": target_server_runtime_name,
        "server_config": target_server_raw_config,
        "match_key": scoped_runtime.get("match_key"),
        "status": "pending",
        "applied_at": _utc_timestamp(),
    }


def _write_xray_bindings_state(
    bindings: list[dict[str, Any]],
    *,
    applied_ok: bool = False,
    client_modes: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
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
        "client_modes_count": len(client_modes or []),
        "client_modes": [
            {
                **{key: item.get(key) for key in ("subject_id", "client_id", "client_uuid", "client_email", "desired_mode", "effective_mode", "mode_support_state")},
                "status": "applied" if applied_ok else "pending",
            }
            for item in (client_modes or [])
        ],
    }
    atomic_write_json(_xray_bindings_path(), payload)
    return payload
