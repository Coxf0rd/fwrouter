from __future__ import annotations

from typing import Any

from fwrouter_api.services import ui_state_common as _common
from fwrouter_api.services import ui_state_clients as _clients
from fwrouter_api.services import ui_state_inventory as _inventory
from fwrouter_api.services import ui_state_summary as _summary
from fwrouter_api.services.subject_policy import list_subjects_with_effective_state
from fwrouter_api.services.ui_state_common import (
    DEFAULT_TRAFFIC_PANEL_KEYS as DEFAULT_TRAFFIC_PANEL_KEYS,
    INVENTORY_ROLE_ALIASES as INVENTORY_ROLE_ALIASES,
    INVENTORY_ROLE_BY_KIND as INVENTORY_ROLE_BY_KIND,
    KINDS_BY_INVENTORY_ROLE as KINDS_BY_INVENTORY_ROLE,
    TRAFFIC_METRIC_KEYS as TRAFFIC_METRIC_KEYS,
    XRAY_INTERNAL_PREFIXES as XRAY_INTERNAL_PREFIXES,
    XRAY_SUBSCRIPTION_ACTIVE_WINDOW_SECONDS as XRAY_SUBSCRIPTION_ACTIVE_WINDOW_SECONDS,
    _load_traffic_maps,
    _month_key as _month_key,
)
from fwrouter_api.services.ui_state_logs import _summarize_log_event as _summarize_log_event
from fwrouter_api.services.ui_state_settings import (
    get_ui_display_settings as get_ui_display_settings,
    save_ui_display_settings as save_ui_display_settings,
)


def _sync_common_hooks() -> None:
    _common._load_traffic_maps = _load_traffic_maps
    _common.list_subjects_with_effective_state = list_subjects_with_effective_state


def list_ui_clients(*, display_settings: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    _sync_common_hooks()
    return _clients.list_ui_clients(display_settings=display_settings)


def filter_ui_clients(
    clients: list[dict[str, Any]],
    *,
    display_settings: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    _sync_common_hooks()
    return _clients.filter_ui_clients(clients, display_settings=display_settings)


def _ui_client_stats(
    clients: list[dict[str, Any]],
    *,
    display_settings: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    return _clients._ui_client_stats(clients, display_settings=display_settings)


def _list_ui_client_presence(display_settings: dict[str, Any]) -> list[dict[str, Any]]:
    _sync_common_hooks()
    return _clients._list_ui_client_presence(display_settings)


def _ui_workspace_counts(*, display_settings: dict[str, Any]) -> dict[str, int]:
    _sync_common_hooks()
    return _clients._ui_workspace_counts(display_settings=display_settings)


def list_ui_settings_inventory(
    *,
    role: str = "all",
    query: str = "",
    limit: int = 200,
    include_inactive: bool = False,
    live_observations: bool = True,
    display_settings: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    _sync_common_hooks()
    return _inventory.list_ui_settings_inventory(
        role=role,
        query=query,
        limit=limit,
        include_inactive=include_inactive,
        live_observations=live_observations,
        display_settings=display_settings,
    )


def get_router_self_subject() -> dict[str, Any] | None:
    return _summary.get_router_self_subject()


def _server_name_by_id(server_id: str | None) -> str | None:
    return _summary._server_name_by_id(server_id)


def get_ui_router_summary() -> dict[str, Any]:
    _sync_common_hooks()
    return _summary.get_ui_router_summary()


def _build_ui_router_summary() -> dict[str, Any]:
    _sync_common_hooks()
    return _summary._build_ui_router_summary()


def get_ui_settings_workspace() -> dict[str, Any]:
    _sync_common_hooks()
    return _summary.get_ui_settings_workspace()


def _build_ui_settings_workspace() -> dict[str, Any]:
    _sync_common_hooks()
    return _summary._build_ui_settings_workspace()
