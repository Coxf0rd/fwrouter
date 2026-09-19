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
) -> bool:
    return (
        bool(vpn_auto)
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
        f"{server_alias}.inventory_state = 'active' "
        f"AND COALESCE({preferences_alias}.vpn_auto, 0) = 1 "
        f"AND COALESCE({preferences_alias}.vpn_auto_priority, 0) >= 0 "
        f"AND COALESCE({preferences_alias}.manually_deleted_at, '') = ''"
    )
