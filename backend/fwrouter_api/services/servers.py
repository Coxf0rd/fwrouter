from __future__ import annotations

from typing import Any

from fwrouter_api.services.server_global_selection import (
    apply_global_auto_server as apply_global_auto_server,
    apply_global_fixed_server as apply_global_fixed_server,
    clear_global_fixed_server as clear_global_fixed_server,
    set_global_fixed_server as set_global_fixed_server,
    submit_global_fixed_server_apply_job as submit_global_fixed_server_apply_job,
)
from fwrouter_api.services.server_inventory import (
    get_server as get_server,
    list_servers as list_servers,
    sync_servers_from_mihomo as sync_servers_from_mihomo,
)
from fwrouter_api.services.server_preferences import (
    _reconcile_mihomo_after_server_preferences,
    replace_vpn_auto_servers as _replace_vpn_auto_servers,
    update_server_preferences as _update_server_preferences,
)
from fwrouter_api.services.server_state import (
    ensure_routing_global_state as ensure_routing_global_state,
    expire_global_fixed_server as expire_global_fixed_server,
    get_routing_global_state as get_routing_global_state,
    reconcile_current_routing_if_drift as reconcile_current_routing_if_drift,
    set_global_mode as set_global_mode,
    set_selective_default as set_selective_default,
)
from fwrouter_api.services.server_subject_overrides import (
    clear_subject_server_override as clear_subject_server_override,
    get_subject_server_override as get_subject_server_override,
    set_subject_server_override as set_subject_server_override,
    update_subject_server_override_apply_status as update_subject_server_override_apply_status,
)


VIRTUAL_XRAY_VPN_AUTO_SERVER_ID = "virtual:xray:vpn-auto"
MANUAL_SERVER_TTL_HOURS = 24
GLOBAL_FIXED_SERVER_TTL_HOURS = 24


def update_server_preferences(
    server_id: str,
    *,
    vpn_auto: bool | None = None,
    vpn_auto_priority: int | None = None,
    global_list: bool | None = None,
    reconcile_mihomo: bool = True,
    requested_by: str = "admin",
) -> dict[str, Any]:
    return _update_server_preferences(
        server_id,
        vpn_auto=vpn_auto,
        vpn_auto_priority=vpn_auto_priority,
        global_list=global_list,
        reconcile_mihomo=reconcile_mihomo,
        requested_by=requested_by,
        reconcile_after_preferences=_reconcile_mihomo_after_server_preferences,
    )


def replace_vpn_auto_servers(
    server_ids: list[str],
    *,
    reconcile_mihomo: bool = True,
    requested_by: str = "admin",
) -> dict[str, Any]:
    return _replace_vpn_auto_servers(
        server_ids,
        reconcile_mihomo=reconcile_mihomo,
        requested_by=requested_by,
        reconcile_after_preferences=_reconcile_mihomo_after_server_preferences,
    )
