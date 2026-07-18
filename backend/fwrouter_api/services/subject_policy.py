from __future__ import annotations

from typing import Any

from fwrouter_api.services.core_bypass import get_core_bypass_state
from fwrouter_api.services.dataplane_global import build_global_preflight
from fwrouter_api.services.dataplane_status import (
    DATAPLANE_CAPABILITY_NFT_OWNED_TABLE,
    ENFORCEMENT_LEVEL_OWNED_TABLE_READY,
    build_bypass_runtime_enforcement,
    build_runtime_enforcement_state,
)
from fwrouter_api.db.connection import db_session
from fwrouter_api.services.logs import write_operational_log
from fwrouter_api.services.scoped_egress import build_scoped_subject_runtime
from fwrouter_api.services.servers import get_routing_global_state
from fwrouter_api.services.subjects import get_subject, list_subjects


USER_OVERRIDE_TTL_DAYS = 7
ADMIN_MODES_BY_SUBJECT_TYPE = {
    "lan": {"global", "direct", "selective", "vpn", "disabled"},
    "tailscale": {"global", "direct", "selective", "vpn", "disabled"},
    "tailscale_node": {"global", "direct", "selective", "vpn", "disabled"},
    "xray": {"enabled", "direct", "selective", "vpn", "disabled"},
    "host": {"direct", "vpn", "disabled"},
    "docker": {"direct", "vpn", "disabled"},
    "fwrouter": {"direct"},
}
USER_MODES = {"direct", "selective", "vpn"}
UI_ACTIVE_SUBJECT_TYPES = {"lan", "tailscale", "tailscale_node", "xray"}
_OVERRIDE_NOT_PROVIDED = object()


def _routing_snapshot() -> dict[str, Any]:
    return get_routing_global_state() or {
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


def get_routing_snapshot() -> dict[str, Any]:
    """Return current committed routing snapshot for effective-state builders."""

    return _routing_snapshot()


def _default_subject_runtime_enforcement(
    *,
    routing: dict[str, Any],
    bypass_state: dict[str, Any],
) -> dict[str, Any]:
    preflight = build_global_preflight(
        routing=routing,
        require_runtime_verify=False,
    )
    if bool(bypass_state.get("enabled")):
        return build_bypass_runtime_enforcement(preflight=preflight)
    return {
        "dataplane_capability": DATAPLANE_CAPABILITY_NFT_OWNED_TABLE,
        "capability": DATAPLANE_CAPABILITY_NFT_OWNED_TABLE,
        "enforcement_level": ENFORCEMENT_LEVEL_OWNED_TABLE_READY,
        "traffic_enforcement_guaranteed": False,
        "supported_modes": {
            "direct": bool(preflight["can_enforce_global_direct"]),
            "selective": bool(preflight["can_enforce_global_selective"]),
            "vpn": bool(preflight["can_enforce_global_vpn"]),
        },
        "missing_runtime_requirements": list(preflight["missing"]),
        "profile": preflight["profile"],
        "bypass_active": False,
    }


def resolve_selective_default(routing: dict[str, Any]) -> str:
    """Return the capture fallback for selective mode.

    This is a capture-decision fallback only. It does not select a VPN server.
    """

    return str(routing.get("selective_default") or "direct")


def _load_active_user_override(subject_id: str) -> dict[str, Any] | None:
    with db_session() as connection:
        row = connection.execute(
            """
            SELECT
                subject_id,
                override_mode,
                override_until,
                created_by,
                updated_at
            FROM subject_user_overrides
            WHERE subject_id = ?
              AND override_mode IS NOT NULL
              AND override_until > CURRENT_TIMESTAMP
            """,
            (subject_id,),
        ).fetchone()

    return dict(row) if row else None


def _load_active_user_overrides(subject_ids: list[str]) -> dict[str, dict[str, Any]]:
    normalized = [str(subject_id).strip() for subject_id in subject_ids if str(subject_id).strip()]
    if not normalized:
        return {}
    placeholders = ", ".join("?" for _ in normalized)
    with db_session() as connection:
        rows = connection.execute(
            f"""
            SELECT
                subject_id,
                override_mode,
                override_until,
                created_by,
                updated_at
            FROM subject_user_overrides
            WHERE subject_id IN ({placeholders})
              AND override_mode IS NOT NULL
              AND override_until > CURRENT_TIMESTAMP
            """,
            tuple(normalized),
        ).fetchall()
    return {str(row["subject_id"]): dict(row) for row in rows}


def _load_active_server_override(subject_id: str) -> dict[str, Any] | None:
    with db_session() as connection:
        row = connection.execute(
            """
            SELECT
                subject_id,
                selected_server_id,
                selected_until,
                apply_state,
                error_code,
                error_message,
                updated_at
            FROM subject_server_overrides
            WHERE subject_id = ?
              AND selected_server_id IS NOT NULL
              AND selected_until > CURRENT_TIMESTAMP
            """,
            (subject_id,),
        ).fetchone()

    return dict(row) if row else None


def _load_active_server_overrides(subject_ids: list[str]) -> dict[str, dict[str, Any]]:
    normalized = [str(subject_id).strip() for subject_id in subject_ids if str(subject_id).strip()]
    if not normalized:
        return {}
    placeholders = ", ".join("?" for _ in normalized)
    with db_session() as connection:
        rows = connection.execute(
            f"""
            SELECT
                subject_id,
                selected_server_id,
                selected_until,
                apply_state,
                error_code,
                error_message,
                updated_at
            FROM subject_server_overrides
            WHERE subject_id IN ({placeholders})
              AND selected_server_id IS NOT NULL
              AND selected_until > CURRENT_TIMESTAMP
            """,
            tuple(normalized),
        ).fetchall()
    return {str(row["subject_id"]): dict(row) for row in rows}


def _xray_effective_mode(
    desired_mode: str,
    *,
    user_override: dict[str, Any] | None,
) -> tuple[str, str]:
    if desired_mode == "disabled":
        return "disabled", "subject"

    if desired_mode == "enabled":
        if user_override is not None:
            return str(user_override["override_mode"]), "user_override"
        return "forced_vpn", "subject_default"

    if desired_mode == "forced_vpn":
        return "forced_vpn", "subject"

    if desired_mode in {"direct", "selective", "vpn"}:
        return desired_mode, "admin_locked"

    return "disabled", "subject"


def _user_override_gate(subject: dict[str, Any]) -> tuple[bool, str]:
    subject_type = str(subject["subject_type"])
    desired_mode = str(subject["desired_mode"])

    if subject_type in {"lan", "tailscale", "tailscale_node"}:
        return desired_mode == "global", "User override is allowed only while admin mode is global."

    if subject_type == "xray":
        return False, "User mode changes are not allowed for Xray subjects."

    return False, "User mode changes are allowed only for LAN and Tailscale-node subjects."


def resolve_effective_capture_mode(
    subject: dict[str, Any],
    routing: dict[str, Any],
    *,
    user_override: dict[str, Any] | None | object = _OVERRIDE_NOT_PROVIDED,
) -> tuple[str, str]:
    subject_type = str(subject["subject_type"])
    desired_mode = str(subject["desired_mode"])
    resolved_user_override = (
        user_override
        if user_override is not _OVERRIDE_NOT_PROVIDED
        else _load_active_user_override(subject["subject_id"])
    )

    if subject_type in {"lan", "tailscale", "tailscale_node"}:
        if desired_mode == "global":
            if resolved_user_override is not None:
                return str(resolved_user_override["override_mode"]), "user_override"
            return str(routing["desired_mode"]), "global"
        return desired_mode, "admin_locked"

    if subject_type == "xray":
        return _xray_effective_mode(desired_mode, user_override=resolved_user_override)

    return desired_mode, "subject"


def _effective_mode_with_override(
    subject: dict[str, Any],
    routing: dict[str, Any],
    *,
    user_override: dict[str, Any] | None | object,
) -> tuple[str, str]:
    subject_type = str(subject["subject_type"])
    if subject_type == "fwrouter":
        return "direct", "architectural_invariant"
    return resolve_effective_capture_mode(subject, routing, user_override=user_override)


def resolve_effective_vpn_target(
    effective_mode: str,
    *,
    subject_type: str | None = None,
    subject_server_override: dict[str, Any] | None,
    routing: dict[str, Any],
    runtime_enforcement: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if runtime_enforcement is None:
        runtime_enforcement = build_runtime_enforcement_state()

    if str(subject_type or "") == "fwrouter":
        return {
            "vpn_target_id": None,
            "vpn_target_source": None,
            "selected_server_id": None,
            "selected_server_source": "fwrouter_direct_safe",
        }

    if effective_mode == "disabled":
        return {
            "vpn_target_id": None,
            "vpn_target_source": None,
            "selected_server_id": None,
            "selected_server_source": "disabled",
        }

    if effective_mode == "direct":
        return {
            "vpn_target_id": None,
            "vpn_target_source": None,
            "selected_server_id": None,
            "selected_server_source": "direct",
        }

    if effective_mode == "selective":
        # Legacy compatibility field: the capture fallback is still reported
        # here, but the real VPN target remains unset until traffic is VPN-bound.
        return {
            "vpn_target_id": None,
            "vpn_target_source": None,
            "selected_server_id": None,
            "selected_server_source": resolve_selective_default(routing),
            "selective_default": resolve_selective_default(routing),
        }

    if effective_mode not in {"vpn", "forced_vpn"}:
        return {
            "vpn_target_id": None,
            "vpn_target_source": None,
            "selected_server_id": None,
            "selected_server_source": "unknown",
        }

    if subject_server_override is not None:
        return {
            "vpn_target_id": str(subject_server_override["selected_server_id"]),
            "vpn_target_source": "subject_override",
            "selected_server_id": subject_server_override["selected_server_id"],
            "selected_server_source": "subject_override",
        }

    fixed_server_id = routing["applied_fixed_server_id"] or routing["desired_fixed_server_id"]
    if routing["server_mode"] == "fixed" and fixed_server_id:
        return {
            "vpn_target_id": str(fixed_server_id),
            "vpn_target_source": "global_fixed",
            "selected_server_id": fixed_server_id,
            "selected_server_source": "global_fixed",
        }

    return {
        "vpn_target_id": "vpn-global",
        "vpn_target_source": "vpn_auto",
        "selected_server_id": routing["active_auto_server_id"],
        "selected_server_source": "vpn_auto",
    }


def _effective_binding(
    effective_mode: str,
    *,
    subject_type: str | None = None,
    subject_server_override: dict[str, Any] | None,
    routing: dict[str, Any],
    runtime_enforcement: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if runtime_enforcement is None:
        runtime_enforcement = build_runtime_enforcement_state()

    if str(subject_type or "") == "fwrouter":
        return {
            "dataplane_path": "direct",
            **resolve_effective_vpn_target(
                effective_mode,
                subject_type=subject_type,
                subject_server_override=subject_server_override,
                routing=routing,
                runtime_enforcement=runtime_enforcement,
            ),
        }

    if effective_mode == "disabled":
        return {
            "dataplane_path": "blocked",
            **resolve_effective_vpn_target(
                effective_mode,
                subject_type=subject_type,
                subject_server_override=subject_server_override,
                routing=routing,
                runtime_enforcement=runtime_enforcement,
            ),
        }

    if effective_mode == "direct":
        return {
            "dataplane_path": "direct",
            **resolve_effective_vpn_target(
                effective_mode,
                subject_type=subject_type,
                subject_server_override=subject_server_override,
                routing=routing,
                runtime_enforcement=runtime_enforcement,
            ),
        }

    if effective_mode == "selective":
        return {
            "dataplane_path": "selective",
            **resolve_effective_vpn_target(
                effective_mode,
                subject_type=subject_type,
                subject_server_override=subject_server_override,
                routing=routing,
                runtime_enforcement=runtime_enforcement,
            ),
        }

    if effective_mode not in {"vpn", "forced_vpn"}:
        return {
            "dataplane_path": effective_mode,
            **resolve_effective_vpn_target(
                effective_mode,
                subject_type=subject_type,
                subject_server_override=subject_server_override,
                routing=routing,
                runtime_enforcement=runtime_enforcement,
            ),
        }

    return {
        "dataplane_path": "vpn",
        **resolve_effective_vpn_target(
            effective_mode,
            subject_type=subject_type,
            subject_server_override=subject_server_override,
            routing=routing,
            runtime_enforcement=runtime_enforcement,
        ),
    }


def enrich_subject_with_effective_state(
    subject: dict[str, Any],
    *,
    routing: dict[str, Any] | None = None,
    user_override: dict[str, Any] | None | object = _OVERRIDE_NOT_PROVIDED,
    server_override: dict[str, Any] | None | object = _OVERRIDE_NOT_PROVIDED,
    runtime_enforcement: dict[str, Any] | None = None,
    bypass_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    routing_snapshot = routing or _routing_snapshot()
    resolved_user_override = (
        user_override
        if user_override is not _OVERRIDE_NOT_PROVIDED
        else _load_active_user_override(subject["subject_id"])
    )
    resolved_server_override = (
        server_override
        if server_override is not _OVERRIDE_NOT_PROVIDED
        else _load_active_server_override(subject["subject_id"])
    )
    resolved_bypass_state = bypass_state or get_core_bypass_state()
    resolved_runtime_enforcement = (
        runtime_enforcement
        if runtime_enforcement is not None
        else _default_subject_runtime_enforcement(
            routing=routing_snapshot,
            bypass_state=resolved_bypass_state,
        )
    )
    effective_mode, mode_source = _effective_mode_with_override(
        subject,
        routing_snapshot,
        user_override=resolved_user_override if isinstance(resolved_user_override, dict) else None,
    )
    binding = _effective_binding(
        effective_mode,
        subject_type=str(subject.get("subject_type") or ""),
        subject_server_override=resolved_server_override if isinstance(resolved_server_override, dict) else None,
        routing=routing_snapshot,
        runtime_enforcement=resolved_runtime_enforcement,
    )
    scoped_runtime = build_scoped_subject_runtime(
        subject,
        dataplane_path=str(binding["dataplane_path"]),
        selected_server_id=(
            str(binding["selected_server_id"])
            if binding.get("selected_server_id") is not None
            else None
        ),
        selected_server_source=(
            str(binding["selected_server_source"])
            if binding.get("selected_server_source") is not None
            else None
        ),
        vpn_target_id=(
            str(binding["vpn_target_id"])
            if binding.get("vpn_target_id") is not None
            else None
        ),
        vpn_target_source=(
            str(binding["vpn_target_source"])
            if binding.get("vpn_target_source") is not None
            else None
        ),
        server_override=resolved_server_override if isinstance(resolved_server_override, dict) else None,
        vpn_supported=bool(
            (resolved_runtime_enforcement.get("supported_modes") or {}).get("vpn", False)
        ),
        bypass_enabled=bool(resolved_bypass_state.get("enabled")),
    )

    enriched = dict(subject)
    enriched["lifecycle"] = {
        "visible_in_ui": bool(subject["is_active"]) and subject["subject_type"] in UI_ACTIVE_SUBJECT_TYPES,
        "known_only": not bool(subject["is_active"]),
    }
    enriched["effective_state"] = {
        "committed_desired_mode": subject["desired_mode"],
        "applied_control_plane_mode": effective_mode,
        "effective_mode": effective_mode,
        "mode_source": mode_source,
        "capture_mode": effective_mode,
        "capture_mode_source": mode_source,
        "dataplane_path": binding["dataplane_path"],
        "selected_server_id": binding.get("selected_server_id"),
        "selected_server_source": binding.get("selected_server_source"),
        "vpn_target_id": binding.get("vpn_target_id"),
        "vpn_target_source": binding.get("vpn_target_source"),
        "selective_default": binding.get("selective_default"),
        "global_routing": routing_snapshot,
        "user_override": resolved_user_override,
        "server_override": resolved_server_override,
        "runtime_enforcement": resolved_runtime_enforcement,
        "scoped_runtime": scoped_runtime,
    }
    return enriched


def _subject_effective_state_summary(
    subject: dict[str, Any],
    *,
    routing: dict[str, Any],
    runtime_enforcement: dict[str, Any],
    bypass_state: dict[str, Any],
    user_override: dict[str, Any] | None | object = _OVERRIDE_NOT_PROVIDED,
    server_override: dict[str, Any] | None | object = _OVERRIDE_NOT_PROVIDED,
) -> dict[str, Any]:
    resolved_user_override = (
        user_override
        if user_override is not _OVERRIDE_NOT_PROVIDED
        else _load_active_user_override(subject["subject_id"])
    )
    resolved_server_override = (
        server_override
        if server_override is not _OVERRIDE_NOT_PROVIDED
        else _load_active_server_override(subject["subject_id"])
    )
    effective_mode, mode_source = _effective_mode_with_override(
        subject,
        routing,
        user_override=resolved_user_override if isinstance(resolved_user_override, dict) else None,
    )
    binding = _effective_binding(
        effective_mode,
        subject_type=str(subject.get("subject_type") or ""),
        subject_server_override=resolved_server_override if isinstance(resolved_server_override, dict) else None,
        routing=routing,
        runtime_enforcement=runtime_enforcement,
    )
    scoped_runtime = build_scoped_subject_runtime(
        subject,
        dataplane_path=str(binding["dataplane_path"]),
        selected_server_id=(
            str(binding["selected_server_id"])
            if binding.get("selected_server_id") is not None
            else None
        ),
        selected_server_source=(
            str(binding["selected_server_source"])
            if binding.get("selected_server_source") is not None
            else None
        ),
        vpn_target_id=(
            str(binding["vpn_target_id"])
            if binding.get("vpn_target_id") is not None
            else None
        ),
        vpn_target_source=(
            str(binding["vpn_target_source"])
            if binding.get("vpn_target_source") is not None
            else None
        ),
        server_override=resolved_server_override if isinstance(resolved_server_override, dict) else None,
        vpn_supported=bool(
            (runtime_enforcement.get("supported_modes") or {}).get("vpn", False)
        ),
        bypass_enabled=bool(bypass_state.get("enabled")),
    )
    return {
        "subject_id": subject["subject_id"],
        "subject_type": subject["subject_type"],
        "display_name": subject.get("display_name"),
        "is_active": bool(subject.get("is_active")),
        "is_deleted": bool(subject.get("is_deleted")),
        "runtime_state": subject.get("runtime_state"),
        "visibility": subject.get("visibility"),
        "effective_state": {
            "effective_mode": effective_mode,
            "mode_source": mode_source,
            "capture_mode": effective_mode,
            "capture_mode_source": mode_source,
            "dataplane_path": binding["dataplane_path"],
            "selected_server_id": binding.get("selected_server_id"),
            "selected_server_source": binding.get("selected_server_source"),
            "vpn_target_id": binding.get("vpn_target_id"),
            "vpn_target_source": binding.get("vpn_target_source"),
            "scoped_runtime": scoped_runtime,
        },
    }


def list_subjects_with_effective_state(
    *,
    subject_type: str | None = None,
    is_active: bool | None = None,
    include_deleted: bool = False,
    limit: int = 100,
    runtime_enforcement: dict[str, Any] | None = None,
    bypass_state: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    routing = _routing_snapshot()
    resolved_bypass_state = (
        bypass_state
        if bypass_state is not None
        else get_core_bypass_state()
    )
    resolved_runtime_enforcement = (
        runtime_enforcement
        if runtime_enforcement is not None
        else _default_subject_runtime_enforcement(
            routing=routing,
            bypass_state=resolved_bypass_state,
        )
    )
    subjects = list_subjects(
        subject_type=subject_type,
        is_active=is_active,
        include_deleted=include_deleted,
        include_detail=True,
        limit=limit,
    )
    subject_ids = [str(subject["subject_id"]) for subject in subjects]
    user_overrides = _load_active_user_overrides(subject_ids)
    server_overrides = _load_active_server_overrides(subject_ids)
    return [
        enrich_subject_with_effective_state(
            subject,
            routing=routing,
            user_override=user_overrides.get(str(subject["subject_id"])),
            server_override=server_overrides.get(str(subject["subject_id"])),
            runtime_enforcement=resolved_runtime_enforcement,
            bypass_state=resolved_bypass_state,
        )
        for subject in subjects
    ]


def list_subjects_effective_summaries(
    *,
    subject_type: str | None = None,
    is_active: bool | None = None,
    include_deleted: bool = False,
    limit: int = 100,
    runtime_enforcement: dict[str, Any] | None = None,
    bypass_state: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    routing = _routing_snapshot()
    resolved_bypass_state = (
        bypass_state
        if bypass_state is not None
        else get_core_bypass_state()
    )
    resolved_runtime_enforcement = (
        runtime_enforcement
        if runtime_enforcement is not None
        else _default_subject_runtime_enforcement(
            routing=routing,
            bypass_state=resolved_bypass_state,
        )
    )
    subjects = list_subjects(
        subject_type=subject_type,
        is_active=is_active,
        include_deleted=include_deleted,
        include_detail=True,
        limit=limit,
    )
    subject_ids = [str(subject["subject_id"]) for subject in subjects]
    user_overrides = _load_active_user_overrides(subject_ids)
    server_overrides = _load_active_server_overrides(subject_ids)
    return [
        _subject_effective_state_summary(
            subject,
            routing=routing,
            runtime_enforcement=resolved_runtime_enforcement,
            bypass_state=resolved_bypass_state,
            user_override=user_overrides.get(str(subject["subject_id"])),
            server_override=server_overrides.get(str(subject["subject_id"])),
        )
        for subject in subjects
    ]


def get_subject_with_effective_state(subject_id: str) -> dict[str, Any] | None:
    subject = get_subject(subject_id)
    if subject is None:
        return None
    routing = _routing_snapshot()
    bypass_state = get_core_bypass_state()
    return enrich_subject_with_effective_state(
        subject,
        routing=routing,
        runtime_enforcement=_default_subject_runtime_enforcement(
            routing=routing,
            bypass_state=bypass_state,
        ),
        bypass_state=bypass_state,
    )


def _validate_subject_mode(
    subject: dict[str, Any],
    *,
    mode: str,
    actor_scope: str,
) -> dict[str, str] | None:
    subject_type = str(subject["subject_type"])

    if actor_scope == "user":
        if subject_type not in {"lan", "tailscale", "tailscale_node"}:
            return {
                "code": "SUBJECT_MODE_FORBIDDEN",
                "message": "User mode changes are allowed only for LAN and Tailscale-node subjects.",
            }
        override_allowed, locked_message = _user_override_gate(subject)
        if not override_allowed:
            return {
                "code": "SUBJECT_MODE_ADMIN_LOCKED",
                "message": locked_message,
            }
        if mode not in USER_MODES:
            return {
                "code": "SUBJECT_MODE_INVALID",
                "message": f"User mode must be one of: {', '.join(sorted(USER_MODES))}.",
            }
        return None

    allowed_modes = ADMIN_MODES_BY_SUBJECT_TYPE.get(subject_type, set())
    if mode not in allowed_modes:
        return {
            "code": "SUBJECT_MODE_INVALID",
            "message": (
                f"Mode {mode!r} is not allowed for subject type {subject_type}. "
                f"Allowed: {', '.join(sorted(allowed_modes))}."
            ),
        }
    return None


def set_subject_mode(
    subject_id: str,
    mode: str,
    *,
    actor_scope: str = "admin",
    requested_by: str = "api",
) -> dict[str, Any]:
    from fwrouter_api.services.apply_orchestrator import set_subject_mode as run_subject_mode_transaction

    return run_subject_mode_transaction(
        subject_id,
        mode,
        actor_scope=actor_scope,
        requested_by=requested_by,
    )


def expire_subject_overrides(*, dry_run: bool = True) -> dict[str, Any]:
    with db_session() as connection:
        expired_user_rows = connection.execute(
            """
            SELECT subject_id, override_mode, override_until
            FROM subject_user_overrides
            WHERE override_until IS NOT NULL
              AND override_until <= CURRENT_TIMESTAMP
            ORDER BY override_until
            """
        ).fetchall()
        expired_server_rows = connection.execute(
            """
            SELECT subject_id, selected_server_id, selected_until
            FROM subject_server_overrides
            WHERE selected_until IS NOT NULL
              AND selected_until <= CURRENT_TIMESTAMP
            ORDER BY selected_until
            """
        ).fetchall()

        expired_user_overrides = [dict(row) for row in expired_user_rows]
        expired_server_overrides = [dict(row) for row in expired_server_rows]

        if not dry_run:
            if expired_user_overrides:
                connection.execute(
                    """
                    DELETE FROM subject_user_overrides
                    WHERE override_until IS NOT NULL
                      AND override_until <= CURRENT_TIMESTAMP
                    """
                )
            if expired_server_overrides:
                connection.execute(
                    """
                    DELETE FROM subject_server_overrides
                    WHERE selected_until IS NOT NULL
                      AND selected_until <= CURRENT_TIMESTAMP
                    """
                )

    if not dry_run:
        for override in expired_user_overrides:
            write_operational_log(
                event_type="subject_user_override_expired",
                subject_id=str(override["subject_id"]),
                message="User override expired and was cleared.",
                details=override,
            )
        for override in expired_server_overrides:
            write_operational_log(
                event_type="subject_server_override_expired",
                subject_id=str(override["subject_id"]),
                message="Subject server override expired and was cleared.",
                details=override,
            )

    return {
        "dry_run": dry_run,
        "expired_user_overrides_count": len(expired_user_overrides),
        "expired_user_overrides": expired_user_overrides,
        "expired_server_overrides_count": len(expired_server_overrides),
        "expired_server_overrides": expired_server_overrides,
    }
