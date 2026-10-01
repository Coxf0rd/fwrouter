from __future__ import annotations

from typing import Any


VPN_AUTO_EXCLUSIVE_SETTING_KEY = "vpn_auto_exclusive_source_ref"


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
    respect_exclusive: bool = True,
) -> str:
    base = (
        f"NOT ({provider_managed_legacy_sql(server_alias)} OR {provider_internal_member_sql(server_alias)}) "
        f"AND {server_alias}.inventory_state = 'active' "
        f"AND COALESCE({preferences_alias}.vpn_auto, 0) = 1 "
        f"AND COALESCE({preferences_alias}.vpn_auto_priority, 0) >= 0 "
        f"AND COALESCE({preferences_alias}.manually_deleted_at, '') = ''"
    )
    if not respect_exclusive:
        return base
    return f"({base}) AND ({exclusive_pool_sql(server_alias)})"


def exclusive_source_ref_sql() -> str:
    return (
        "SELECT CASE WHEN json_valid(value_json) THEN json_extract(value_json, '$.source_ref') ELSE NULL END FROM settings "
        f"WHERE key = '{VPN_AUTO_EXCLUSIVE_SETTING_KEY}' LIMIT 1"
    )


def exclusive_pool_sql(server_alias: str = "s") -> str:
    """Restrict automatic candidates to the selected source's logical pool.

    Provider-managed sources contribute only their configured logical root.
    Ordinary sources use the canonical direct/member ownership projection.
    Disabled bindings use ordinary ownership; a missing enabled root yields no candidates.
    """
    source_ref = f"({exclusive_source_ref_sql()})"
    return f"""(
        {source_ref} IS NULL OR TRIM(CAST({source_ref} AS TEXT)) = ''
        OR (
            EXISTS (SELECT 1 FROM provider_bindings b WHERE b.source_ref={source_ref} AND b.enabled=1)
            AND EXISTS (
                SELECT 1 FROM provider_bindings b
                WHERE b.source_ref={source_ref} AND b.enabled=1
                  AND b.logical_server_id={server_alias}.server_id
            )
        )
        OR (
            NOT EXISTS (SELECT 1 FROM provider_bindings b WHERE b.source_ref={source_ref} AND b.enabled=1)
            AND EXISTS (
                SELECT 1 FROM ({source_memberships_sql(server_alias)}) membership
                WHERE membership.source_id={source_ref} AND membership.is_active=1
            )
        )
    )"""


def server_is_in_exclusive_pool(connection: Any, server_id: str, source_ref: str | None = None) -> bool:
    """Read one server's exclusive-pool membership using canonical ownership."""
    selected = str(source_ref or "").strip()
    if not selected:
        row = connection.execute(exclusive_source_ref_sql()).fetchone()
        selected = str(row[0] or "").strip() if row else ""
    if not selected:
        return True
    binding = connection.execute(
        "SELECT logical_server_id FROM provider_bindings WHERE source_ref=? AND enabled=1 LIMIT 1",
        (selected,),
    ).fetchone()
    if binding is not None:
        return str(binding["logical_server_id"] or "") == str(server_id)
    row = connection.execute(
        f"""SELECT 1 FROM servers s
             WHERE s.server_id=? AND EXISTS (
                 SELECT 1 FROM ({source_memberships_sql('s')}) membership
                 WHERE membership.source_id=? AND membership.is_active=1
             ) LIMIT 1""",
        (str(server_id), selected),
    ).fetchone()
    return row is not None


def source_memberships_sql(server_alias: str = "s") -> str:
    """Direct entry ownership plus ownership inherited from logical members.

    Resolve member identities once per server, rather than once per membership.
    Source intent is never inferred from a display name or provider protocol.
    """
    return f"""WITH identity AS MATERIALIZED (
        SELECT {server_alias}.server_id id, {server_alias}.server_name name,
               json_extract({server_alias}.raw_json,'$._fwrouter_runtime_name') runtime_name
    ) SELECT direct.source_id, direct.is_active
        FROM subscription_server_memberships direct WHERE direct.server_id={server_alias}.server_id
        UNION SELECT membership.source_id, membership.is_active
        FROM identity CROSS JOIN logical_server_members owned
        JOIN subscription_server_memberships membership ON membership.server_id=owned.logical_server_id
        WHERE owned.member_id=identity.id OR owned.member_runtime_name=identity.name
           OR owned.member_runtime_name=identity.runtime_name"""


def source_refs_sql(server_alias: str = "s") -> str:
    return f"""SELECT json_group_array(DISTINCT membership.source_id)
        FROM ({source_memberships_sql(server_alias)}) membership"""


def provider_managed_legacy_sql(server_alias: str = "s") -> str:
    """Source intent masks retained entries, without disabling active shared owners."""
    return f"""EXISTS (
        SELECT 1 FROM ({source_memberships_sql(server_alias)}) pm
        JOIN provider_bindings pb ON pb.source_ref=pm.source_id AND pb.enabled=1
        WHERE pb.logical_server_id<>{server_alias}.server_id
    ) AND NOT EXISTS (
        SELECT 1 FROM ({source_memberships_sql(server_alias)}) om
        LEFT JOIN provider_bindings ob ON ob.source_ref=om.source_id
        WHERE om.is_active=1 AND COALESCE(ob.enabled,0)=0
    ) AND NOT EXISTS (SELECT 1 FROM server_custom_https_proxy cp WHERE cp.server_id={server_alias}.server_id)
      AND NOT EXISTS (SELECT 1 FROM provider_bindings root WHERE root.enabled=1 AND root.logical_server_id={server_alias}.server_id)
      AND NOT ({provider_internal_member_sql(server_alias)})"""


def provider_internal_member_sql(server_alias: str = "s") -> str:
    """Canonical member/runtime identity never constitutes a top-level target."""
    return f"""EXISTS (
        SELECT 1 FROM logical_server_members lm
        JOIN provider_bindings pb ON pb.logical_server_id=lm.logical_server_id AND pb.enabled=1
        WHERE {server_alias}.server_id<>pb.logical_server_id AND
          (lm.member_id={server_alias}.server_id OR lm.member_runtime_name={server_alias}.server_name
           OR lm.member_runtime_name=json_extract({server_alias}.raw_json,'$._fwrouter_runtime_name'))
    )"""
