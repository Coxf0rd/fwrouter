from __future__ import annotations

from typing import Any


def auto_priority(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def is_auto_eligible(
    *,
    vpn_auto: Any,
    vpn_auto_priority: Any,
    inventory_state: Any = "active",
    manually_deleted_at: Any = "",
    provider_managed_legacy: Any = False,
    provider_internal_member: Any = False,
) -> bool:
    return (
        not provider_managed_legacy
        and not provider_internal_member
        and bool(vpn_auto)
        and auto_priority(vpn_auto_priority) >= 0
        and str(inventory_state or "").strip() == "active"
        and str(manually_deleted_at or "").strip() == ""
    )


def auto_eligible_sql(
    *,
    server_alias: str = "s",
    preferences_alias: str = "p",
) -> str:
    return (
        f"NOT ({provider_managed_legacy_sql(server_alias)} OR {provider_internal_member_sql(server_alias)}) "
        f"AND {server_alias}.inventory_state = 'active' "
        f"AND COALESCE({preferences_alias}.vpn_auto, 0) = 1 "
        f"AND COALESCE({preferences_alias}.vpn_auto_priority, 0) >= 0 "
        f"AND COALESCE({preferences_alias}.manually_deleted_at, '') = ''"
    )


def provider_managed_legacy_sql(server_alias: str = "s") -> str:
    """Source intent masks retained entries, without disabling active shared owners."""
    return f"""EXISTS (
        SELECT 1 FROM subscription_server_memberships pm
        JOIN provider_bindings pb ON pb.source_ref=pm.source_id AND pb.enabled=1
        WHERE pm.server_id={server_alias}.server_id AND pb.logical_server_id<>{server_alias}.server_id
    ) AND NOT EXISTS (
        SELECT 1 FROM subscription_server_memberships om
        LEFT JOIN provider_bindings ob ON ob.source_ref=om.source_id
        WHERE om.server_id={server_alias}.server_id AND om.is_active=1 AND COALESCE(ob.enabled,0)=0
    ) AND NOT EXISTS (SELECT 1 FROM server_custom_https_proxy cp WHERE cp.server_id={server_alias}.server_id)
      AND NOT EXISTS (SELECT 1 FROM provider_bindings root WHERE root.enabled=1 AND root.logical_server_id={server_alias}.server_id)"""


def provider_internal_member_sql(server_alias: str = "s") -> str:
    """Canonical member/runtime identity never constitutes a top-level target."""
    return f"""EXISTS (
        SELECT 1 FROM logical_server_members lm
        JOIN provider_bindings pb ON pb.logical_server_id=lm.logical_server_id AND pb.enabled=1
        WHERE {server_alias}.server_id<>pb.logical_server_id AND
          (lm.member_id={server_alias}.server_id OR lm.member_runtime_name={server_alias}.server_name
           OR lm.member_runtime_name=json_extract({server_alias}.raw_json,'$._fwrouter_runtime_name'))
    )"""
