from __future__ import annotations

import json
from typing import Any

from fwrouter_api.services.ui_text import _ui_text_title


UI_DISPLAY_SETTINGS_KEY = "ui.admin_client_display.v1"
UI_SYSTEM_VISIBILITY_DEFAULTS = {
    "lan": True,
    "external_network_source": True,
    "vless_client": True,
    "vpn_runtime": True,
    "docker": True,
    "host": True,
}
EXTERNAL_INTEGRATION_MODES = {"api_push", "http_poll", "command_probe", "file_read"}
EXTERNAL_REFRESH_MODES = {"on_change", "manual", "interval"}
DEFAULT_EXTERNAL_COLLECTOR_INTERVAL_SECONDS = 300
EXTERNAL_CONNECTION_TYPES = {"external_management", "external_vpn_module", "external_network_source", "display_only"}
EXTERNAL_LOCATIONS = {"docker", "host", "ip", "manual"}
EXTERNAL_COLLECTOR_BASE_KEYS = {"interval_seconds", "timeout_seconds", "apply_traffic", "trigger"}
EXTERNAL_COLLECTOR_MODE_KEYS = {
    "api_push": set(),
    "http_poll": {"url", "status_url", "data_url"},
    "command_probe": {"script_id", "extra_args"},
    "file_read": {"path"},
}
EXTERNAL_ENDPOINT_KEYS = {
    "controller_url",
    "http_proxy_url",
    "socks_proxy_url",
    "tcp_redir_port",
    "udp_tproxy_port",
    "full_tcp_redir_port",
    "full_udp_tproxy_port",
    "healthcheck_url",
    "selector_state_url",
    "selector_failover_url",
    "client_inventory_url",
    "subscription_base_url",
    "traffic_stats_url",
    "client_api_url",
    "reload_url",
    "interface_name",
    "client_cidr",
}
EXTERNAL_CAPABILITY_KEYS = {
    "supports_tcp",
    "supports_udp",
    "supports_transparent_proxy",
    "supports_http_proxy",
    "supports_socks_proxy",
    "supports_selector_api",
    "supports_client_inventory",
    "supports_client_api",
    "supports_subscription_api",
    "supports_traffic_stats",
    "supports_reload",
}
UI_DISPLAY_SYSTEMS = (
    {
        "system_id": "lan",
        "label_key": "lan",
        "kind": "core",
        "lifecycle_mode": "core",
        "module_name": "core",
        "count_key": "lan_client",
        "description_key": "lan",
        "custom": False,
        "always_show": True,
    },
    {
        "system_id": "external_network_source",
        "label_key": "external_network_source",
        "kind": "external",
        "lifecycle_mode": "external",
        "module_name": None,
        "count_key": "external_network_source",
        "description_key": "external_network_source",
        "custom": False,
        "show_in_connections": False,
    },
    {
        "system_id": "vless_client",
        "label_key": "vless_client",
        "kind": "managed",
        "lifecycle_mode": "managed",
        "module_name": "xray",
        "count_key": "vless_client",
        "description_key": "vless_client",
        "custom": False,
    },
    {
        "system_id": "vpn_runtime",
        "label_key": "vpn_runtime",
        "kind": "managed",
        "lifecycle_mode": "managed",
        "module_name": "vpn",
        "count_key": None,
        "description_key": "vpn_runtime",
        "custom": False,
    },
    {
        "system_id": "docker",
        "label_key": "docker",
        "kind": "inventory",
        "lifecycle_mode": "inventory",
        "module_name": None,
        "count_key": "docker",
        "description_key": "docker",
        "custom": False,
    },
    {
        "system_id": "host",
        "label_key": "host",
        "kind": "inventory",
        "lifecycle_mode": "inventory",
        "module_name": None,
        "count_key": "host",
        "description_key": "host",
        "custom": False,
    },
)


def _json_loads(value: str | None) -> dict[str, Any] | None:
    if not value:
        return None
    loaded = json.loads(value)
    return loaded if isinstance(loaded, dict) else None


def _json_dumps(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class ExternalConnectionValidationError(ValueError):
    def __init__(self, code: str, message: str, field_errors: dict[str, str] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.field_errors = field_errors or {}


def _slugify_system_id(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    result = []
    previous_dash = False
    for char in normalized:
        if char.isalnum():
            result.append(char)
            previous_dash = False
        elif char == "_":
            result.append("_")
            previous_dash = False
        elif not previous_dash:
            result.append("-")
            previous_dash = True
    return "".join(result).strip("-")[:64]


def _external_connection_prefix(connection_type: str) -> str:
    if connection_type == "external_vpn_module":
        return "external-vpn"
    if connection_type == "external_network_source":
        return "external-network"
    if connection_type == "display_only":
        return "external-display"
    return "external-management"


def external_connection_identity(system: dict[str, Any]) -> dict[str, str]:
    connection_id = _slugify_system_id(system.get("connection_id"))
    client_slug = connection_id or "external-client"
    return {
        "external_system_id": client_slug,
        "requested_by": f"external_client:{client_slug}",
        "collector": f"external_connection:{client_slug}",
    }


def _normalize_replacement_target(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"mihomo", "xray"}:
        return normalized
    return ""


def _external_connection_description(connection_type: str) -> str:
    normalized = str(connection_type or "display_only").strip().lower()
    return _ui_text_title("connection.description", normalized) or normalized


def _normalize_external_capabilities(value: Any) -> dict[str, bool]:
    if not isinstance(value, dict):
        return {}
    return {key: bool(value.get(key)) for key in sorted(EXTERNAL_CAPABILITY_KEYS) if key in value}


def _normalize_external_endpoints(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, str] = {}
    for key in sorted(EXTERNAL_ENDPOINT_KEYS):
        raw = str(value.get(key) or "").strip()
        if raw:
            result[key] = raw[:180]
    return result


def _normalize_external_integration_mode(value: Any, connection_type: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in EXTERNAL_INTEGRATION_MODES:
        return normalized
    return "api_push"


def _normalize_external_refresh_mode(value: Any, integration_mode: str) -> str:
    normalized = str(value or "").strip().lower()
    if integration_mode == "api_push":
        return "on_change"
    if normalized in {"manual", "interval"}:
        return normalized
    return "manual"


def _normalize_interval_seconds(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = DEFAULT_EXTERNAL_COLLECTOR_INTERVAL_SECONDS
    return max(30, min(parsed, 86400))


def _default_external_collector_config(integration_mode: str, refresh_mode: str) -> dict[str, Any]:
    config: dict[str, Any] = {
        "interval_seconds": DEFAULT_EXTERNAL_COLLECTOR_INTERVAL_SECONDS,
        "timeout_seconds": 5,
        "apply_traffic": False,
    }
    if integration_mode == "api_push":
        config["trigger"] = "external_system_pushes_on_change"
    elif refresh_mode == "manual":
        config["trigger"] = "manual_refresh"
    else:
        config["trigger"] = "poll_interval"
    return config


def _normalize_external_collector_config(
    value: Any,
    *,
    integration_mode: str,
    refresh_mode: str,
) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    result = _default_external_collector_config(integration_mode, refresh_mode)
    result["interval_seconds"] = _normalize_interval_seconds(source.get("interval_seconds"))
    try:
        timeout = int(source.get("timeout_seconds", result["timeout_seconds"]))
    except (TypeError, ValueError):
        timeout = result["timeout_seconds"]
    result["timeout_seconds"] = max(1, min(timeout, 60))
    result["apply_traffic"] = bool(source.get("apply_traffic", False))

    if integration_mode == "http_poll":
        url = str(source.get("url") or source.get("status_url") or source.get("data_url") or "").strip()
        if url:
            result["url"] = url[:300]
    elif integration_mode == "command_probe":
        script_id = str(source.get("script_id") or "").strip()
        if script_id:
            result["script_id"] = script_id[:80]
        extra_args = source.get("extra_args")
        if isinstance(extra_args, list):
            result["extra_args"] = [str(item)[:120] for item in extra_args[:20]]
    elif integration_mode == "file_read":
        path = str(source.get("path") or "").strip()
        if path:
            result["path"] = path[:300]
    return result


def _normalize_custom_external_systems(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    builtin_ids = set(UI_SYSTEM_VISIBILITY_DEFAULTS)
    seen: set[str] = set()
    systems: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        raw_label = str(item.get("label") or item.get("name") or "").strip()
        connection_id = _slugify_system_id(item.get("connection_id") or item.get("system_id") or item.get("id"))
        system_id = _slugify_system_id(item.get("system_id") or connection_id)
        if not connection_id or not system_id or system_id in builtin_ids or connection_id in seen:
            continue
        connection_type = str(item.get("connection_type") or "external_management").strip().lower()
        if connection_type not in {"external_management", "external_vpn_module", "external_network_source", "display_only"}:
            connection_type = "external_management"
        location = str(item.get("location") or "manual").strip().lower()
        if location not in {"docker", "host", "ip", "manual"}:
            location = "manual"
        address = str(item.get("address") or "").strip()[:160]
        runtime_type = str(item.get("runtime_type") or "").strip().lower()[:80]
        replacement_target = _normalize_replacement_target(item.get("replacement_target") or item.get("replaces"))
        capabilities = _normalize_external_capabilities(item.get("capabilities"))
        endpoints = _normalize_external_endpoints(item.get("endpoints"))
        integration_mode = _normalize_external_integration_mode(item.get("integration_mode"), connection_type)
        refresh_mode = _normalize_external_refresh_mode(item.get("refresh_mode"), integration_mode)
        collector_config = _normalize_external_collector_config(
            item.get("collector_config") or item.get("collector"),
            integration_mode=integration_mode,
            refresh_mode=refresh_mode,
        )
        label = raw_label or system_id
        systems.append(
            {
                "connection_id": connection_id,
                "system_id": system_id,
                "label": label[:80],
                "kind": "external",
                "lifecycle_mode": "external",
                "connection_type": connection_type,
                "location": location,
                "address": address,
                "runtime_type": runtime_type,
                "replacement_target": replacement_target,
                "capabilities": capabilities,
                "endpoints": endpoints,
                "integration_mode": integration_mode,
                "refresh_mode": refresh_mode,
                "collector_config": collector_config,
                "description": str(item.get("description") or _external_connection_description(connection_type)).strip()[:240],
                "custom": True,
            }
        )
        seen.add(connection_id)
        if len(systems) >= 50:
            break
    return systems
