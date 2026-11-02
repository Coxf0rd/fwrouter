"""Xray subscription input and binding projections.

The subscription coordinator owns identity persistence and publication. This
module only reads the existing projections and builds prospective values.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from fwrouter_api.db.connection import db_session
from fwrouter_api.services.auto_eligibility import auto_eligible_sql
from fwrouter_api.services.custom_servers import (
    VIRTUAL_CUSTOM_HTTPS_PROXY_SERVER_NAME,
    VIRTUAL_XRAY_VPN_AUTO_SERVER_ID,
    VIRTUAL_XRAY_VPN_AUTO_SERVER_NAME,
)
from fwrouter_api.services.subject_inventory import (
    DEFAULT_DESIRED_MODE_BY_TYPE,
    EXPLICIT_EXTERNAL_CLIENT_SUBJECT_TYPE,
)
from fwrouter_api.services import subject_policy as subject_policy_service
from fwrouter_api.services.subscription_profiles import _stable_digest
from fwrouter_api.services.xray_bindings import (
    _annotate_bindings_with_handoff,
    _applied_handoff_assignments,
    _build_binding_for_subject,
    collect_xray_client_mode_directives,
    collect_xray_runtime_bindings,
)
from fwrouter_api.services.xray_client_state import _xray_subject_for_client
from fwrouter_api.services.xray_handoff import build_xray_handoff_assignments
from fwrouter_api.services.xray_runtime_state import _is_xray_supported_server_config


def read_vpn_auto_candidates() -> list[dict[str, Any]]:
    with db_session() as connection:
        vpn_auto_rows = connection.execute(
            f"""
            SELECT s.server_id, s.server_name, s.raw_json, ps.status AS ping_status, ps.last_ping_ms
            FROM servers AS s
            JOIN server_preferences AS p ON p.server_id = s.server_id
            LEFT JOIN server_ping_state AS ps ON ps.server_id = s.server_id
            WHERE {auto_eligible_sql(server_alias="s", preferences_alias="p", respect_exclusive=False)}
              AND s.server_id NOT IN (
                  SELECT server_id FROM server_custom_https_proxy
              )
            ORDER BY
              CASE WHEN ps.status = 'success' THEN 0 ELSE 1 END,
              ps.last_ping_ms,
              s.server_id
            """
        ).fetchall()
        proxy_rows = connection.execute(
            """
            SELECT s.server_id, s.server_name, s.raw_json, ps.status AS ping_status, ps.last_ping_ms
            FROM servers AS s
            JOIN server_preferences AS p ON p.server_id = s.server_id
            JOIN server_custom_https_proxy AS c ON c.server_id = s.server_id
            LEFT JOIN server_ping_state AS ps ON ps.server_id = s.server_id
            WHERE s.inventory_state = 'active'
              AND COALESCE(p.manually_deleted_at, '') = ''
            ORDER BY s.server_name, s.server_id
            """
        ).fetchall()

    normal_servers: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for row in vpn_auto_rows:
        try:
            raw = json.loads(row["raw_json"] or "{}")
        except json.JSONDecodeError:
            continue
        supported, reason = _is_xray_supported_server_config(raw if isinstance(raw, dict) else None)
        if not supported:
            continue
        normal_servers.append(
            {
                "server_id": row["server_id"],
                "server_name": row["server_name"],
                "raw": raw,
                "ping_status": row["ping_status"],
                "last_ping_ms": row["last_ping_ms"],
                "support_reason": reason,
            }
        )
        seen_ids.add(str(row["server_id"]))

    proxy_server: dict[str, Any] | None = None
    for row in proxy_rows:
        server_id = str(row["server_id"])
        if server_id in seen_ids:
            continue
        proxy_server = {
            "server_id": server_id,
            "server_name": VIRTUAL_CUSTOM_HTTPS_PROXY_SERVER_NAME,
            "raw": {"kind": "custom_https_proxy"},
            "ping_status": row["ping_status"],
            "last_ping_ms": row["last_ping_ms"],
            "support_reason": "custom_https_proxy",
        }
        seen_ids.add(server_id)

    result: list[dict[str, Any]] = [
        {
            "server_id": VIRTUAL_XRAY_VPN_AUTO_SERVER_ID,
            "server_name": VIRTUAL_XRAY_VPN_AUTO_SERVER_NAME,
            "raw": {"kind": "xray_vpn_auto"},
            "ping_status": "virtual",
            "last_ping_ms": None,
            "support_reason": "virtual_xray_vpn_auto",
        }
    ]
    if proxy_server is not None:
        result.append(proxy_server)
    result.extend(normal_servers)
    return result

def prepare_profile_bindings(
    nodes: list[dict[str, Any]],
    *,
    token_prefix: str,
    managed_email_prefixes: list[str] | None = None,
    preserve_existing_overrides: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Build candidate bindings and modes without creating runtime projections."""
    prefixes = [str(value).lower() for value in (managed_email_prefixes or [token_prefix]) if value]
    scoped_email = lambda item: any(str(item.get("client_email") or "").lower().startswith(prefix) for prefix in prefixes)
    bindings = [
        item for item in collect_xray_runtime_bindings(exclude_client_email_prefixes=prefixes)
        if not scoped_email(item)
    ]
    modes = [
        item for item in collect_xray_client_mode_directives(exclude_client_email_prefixes=prefixes)
        if not scoped_email(item)
    ]
    routing = subject_policy_service.get_routing_snapshot()
    enforcement = subject_policy_service.build_runtime_enforcement_state()
    bypass = subject_policy_service.get_core_bypass_state()

    for node in nodes:
        client_uuid = str(node.get("client_uuid") or "").strip()
        email = str(node.get("client_email") or "").strip()
        if not client_uuid or not email:
            continue
        # This planner already owns routing/enforcement/bypass snapshots.
        # Resolving a fully enriched subject here repeats native preflight per
        # client, then discards it below; retain only persisted identity intent.
        current = _xray_subject_for_client(client_uuid, include_effective_state=False)
        if isinstance(current, dict):
            subject_id = str(current.get("subject_id") or f"xray:{client_uuid}")
            subject = current
        else:
            subject_id = f"xray:{client_uuid}"
            subject = {
                "subject_id": subject_id,
                "subject_type": "explicit_external_client",
                "subject_role": "vless_client",
                "implementation_kind": "xray",
                "desired_mode": DEFAULT_DESIRED_MODE_BY_TYPE[EXPLICIT_EXTERNAL_CLIENT_SUBJECT_TYPE],
                "is_active": 1,
                "is_deleted": 0,
                "alias": str(node.get("xray_alias") or ""),
                "detail": {
                    "client_id": client_uuid,
                    "client_uuid": client_uuid,
                    "email": email,
                    "enabled": True,
                },
            }
        user_override = subject_policy_service._load_active_user_override(subject_id)
        server_override = subject_policy_service._load_active_server_override(subject_id)
        with db_session() as connection:
            persisted_server_override = connection.execute(
                "SELECT 1 FROM subject_server_overrides WHERE subject_id = ? LIMIT 1",
                (subject_id,),
            ).fetchone() is not None
            requested_server_exists = connection.execute(
                "SELECT 1 FROM servers WHERE server_id = ? LIMIT 1",
                (str(node.get("server_id") or ""),),
            ).fetchone() is not None
        if requested_server_exists and (
            not preserve_existing_overrides
            or (server_override is None and not persisted_server_override)
        ):
            server_override = {
                "subject_id": subject_id,
                "selected_server_id": str(node.get("server_id") or ""),
                "selected_until": "2099-12-31 23:59:59",
                "apply_state": "pending",
            }
        enriched = subject_policy_service.enrich_subject_with_effective_state(
            subject,
            routing=routing,
            user_override=user_override,
            server_override=server_override,
            runtime_enforcement=enforcement,
            bypass_state=bypass,
        )
        mode = str((enriched.get("effective_state") or {}).get("effective_mode") or "").lower()
        if mode in {"direct", "disabled", "selective"}:
            modes.append({
                "subject_id": subject_id,
                "client_id": client_uuid,
                "client_uuid": client_uuid,
                "client_email": email,
                "desired_mode": mode,
                "effective_mode": "unsupported_selective" if mode == "selective" else mode,
                "mode_support_state": "unsupported_legacy" if mode == "selective" else "supported",
            })
            continue
        binding = _build_binding_for_subject(enriched)
        if binding is not None:
            bindings.append(binding)

    old_assignments = _applied_handoff_assignments()
    bindings = _annotate_bindings_with_handoff(bindings)
    assignments = build_xray_handoff_assignments(bindings, preserve_assignments=old_assignments)
    return bindings, modes, assignments


@dataclass(frozen=True)
class GenerationInputs:
    """Shallow-frozen generation projection, before any native or durable mutation."""

    desired_by_email: dict[str, dict[str, Any]]
    token_prefix: str
    desired_clients: list[dict[str, Any]]
    managed_email_prefixes: list[str]


def compile_generation_inputs(
    desired_nodes: list[dict[str, Any]],
    *,
    token_or_slug: str | None,
    include_vpn_auto: bool,
) -> GenerationInputs:
    """Compile the existing node list into the native-generation input shape.

    This intentionally preserves the coordinator's direct indexing and
    insertion-order behavior. Validation and error classification remain with
    the caller that owns generation admission.
    """
    desired_by_email = {
        str(node["client_email"]): node
        for node in desired_nodes
        if str(node.get("client_email") or "").strip()
    }
    token_prefix = ""
    if token_or_slug:
        token_prefix = f"sub-{_stable_digest(str(token_or_slug).strip().lower(), length=10)}-"
    desired_clients = [
        {
            "client_uuid": node["client_uuid"],
            "client_id": node["client_uuid"],
            "email": node["client_email"],
            "alias": node["xray_alias"],
        }
        for node in desired_nodes
    ]
    managed_prefixes = [token_prefix] if token_prefix else ["sub-"]
    if include_vpn_auto and token_or_slug is None:
        managed_prefixes.append("vpn-auto-")
    return GenerationInputs(
        desired_by_email=desired_by_email,
        token_prefix=token_prefix,
        desired_clients=desired_clients,
        managed_email_prefixes=managed_prefixes,
    )


__all__ = [
    "GenerationInputs",
    "compile_generation_inputs",
    "prepare_profile_bindings",
    "read_vpn_auto_candidates",
]

# These aliases let existing implementation bodies retain their established
# private helper lookups while the coordinator binds compatibility attributes.
