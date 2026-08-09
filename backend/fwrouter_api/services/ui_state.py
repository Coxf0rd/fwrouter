from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from fwrouter_api.db.connection import db_session
from fwrouter_api.services.jobs import get_active_lock_lease, get_job_without_cleanup
from fwrouter_api.services.live_probe_cache import clear_live_probe_cache, get_live_probe_cache
from fwrouter_api.services.logs import list_operational_logs, list_technical_logs
from fwrouter_api.services.modules import fetch_modules
from fwrouter_api.services.runtime_prewarm import prime_runtime_read_models_async
from fwrouter_api.services.servers import get_routing_global_state
from fwrouter_api.services.subject_policy import list_subjects_with_effective_state
from fwrouter_api.services.subjects import get_subject
from fwrouter_api.services.subscription import get_subscription_state
from fwrouter_api.services.subject_groups import XRAY_SUBSCRIPTION_GROUP_PREFIX, xray_subscription_group_from_row
from fwrouter_api.services.system_subjects import list_system_subjects
from fwrouter_api.services.traffic import get_traffic_accounting_state
from fwrouter_api.services.xray import get_xray_status
from fwrouter_api.services.ui_state_logs import _summarize_log_event


UI_DISPLAY_SETTINGS_KEY = "ui.admin_client_display.v1"
UI_SYSTEM_VISIBILITY_DEFAULTS = {
    "lan": True,
    "tailscale": True,
    "xray": True,
    "mihomo": True,
    "docker": True,
    "host": True,
}
UI_LEGACY_SYSTEM_VISIBILITY_KEYS = {
    "lan": "show_lan",
    "tailscale": "show_tailscale",
    "xray": "show_xray",
    "docker": "show_docker",
    "host": "show_host",
}
UI_DISPLAY_SYSTEMS = (
    {
        "system_id": "lan",
        "label": "LAN / Core",
        "kind": "core",
        "lifecycle_mode": "core",
        "module_name": "core",
        "count_key": "lan",
        "description": "Клиенты LAN и routing core FWRouter.",
        "custom": False,
    },
    {
        "system_id": "tailscale",
        "label": "Tailscale",
        "kind": "external",
        "lifecycle_mode": "external",
        "module_name": "tailscale",
        "count_key": "tailscale",
        "description": "Внешний transport/identity provider; FWRouter только учитывает клиентов.",
        "custom": False,
    },
    {
        "system_id": "xray",
        "label": "Xray",
        "kind": "managed",
        "lifecycle_mode": "managed",
        "module_name": "xray",
        "count_key": "xray",
        "description": "Managed runtime FWRouter для Xray/VLESS клиентов.",
        "custom": False,
    },
    {
        "system_id": "mihomo",
        "label": "Mihomo",
        "kind": "managed",
        "lifecycle_mode": "managed",
        "module_name": "vpn",
        "count_key": None,
        "description": "Managed VPN/dataplane adapter FWRouter.",
        "custom": False,
    },
    {
        "system_id": "docker",
        "label": "Docker",
        "kind": "inventory",
        "lifecycle_mode": "inventory",
        "module_name": None,
        "count_key": "docker",
        "description": "Inventory view for containers; not a managed runtime module.",
        "custom": False,
    },
    {
        "system_id": "host",
        "label": "Host services",
        "kind": "inventory",
        "lifecycle_mode": "inventory",
        "module_name": None,
        "count_key": "host",
        "description": "Inventory view for host/systemd services.",
        "custom": False,
    },
)
XRAY_INTERNAL_PREFIXES = ("sub-", "vpn-auto-")
XRAY_SUBSCRIPTION_ACTIVE_WINDOW_SECONDS = 24 * 60 * 60
TRAFFIC_METRIC_KEYS = (
    "direct_rx_bytes",
    "direct_tx_bytes",
    "vpn_rx_bytes",
    "vpn_tx_bytes",
)
DEFAULT_TRAFFIC_PANEL_KEYS = ["vpn_rx_bytes", "vpn_tx_bytes"]
TRAFFIC_METRIC_LABELS = {
    "direct_rx_bytes": "DIRECT вход",
    "direct_tx_bytes": "DIRECT выход",
    "vpn_rx_bytes": "VPN вход",
    "vpn_tx_bytes": "VPN выход",
}

def _default_display_settings() -> dict[str, Any]:
    return {
        "show_lan": True,
        "show_tailscale": True,
        "show_xray": True,
        "show_docker": True,
        "show_host": True,
        "system_visibility": dict(UI_SYSTEM_VISIBILITY_DEFAULTS),
        "custom_external_systems": [],
        "show_inactive": False,
        "show_internal_xray": False,
        "hidden_subject_ids": [],
        "subject_traffic_preferences": {},
    }


def _json_loads(value: str | None) -> dict[str, Any] | None:
    if not value:
        return None
    loaded = json.loads(value)
    return loaded if isinstance(loaded, dict) else None


def _json_dumps(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _load_setting(key: str) -> dict[str, Any] | None:
    with db_session() as connection:
        row = connection.execute(
            "SELECT value_json FROM settings WHERE key = ?",
            (key,),
        ).fetchone()
    return _json_loads(row["value_json"]) if row else None


def _save_setting(key: str, value: dict[str, Any]) -> None:
    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO settings (key, value_json, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET
                value_json = excluded.value_json,
                updated_at = excluded.updated_at
            """,
            (key, _json_dumps(value)),
        )


def _slugify_system_id(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    result = []
    previous_dash = False
    for char in normalized:
        if char.isalnum():
            result.append(char)
            previous_dash = False
        elif char in {"-", "_", ".", ":"} and not previous_dash:
            result.append("-")
            previous_dash = True
    return "".join(result).strip("-")[:64]


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
        raw_id = item.get("system_id") or item.get("id") or raw_label
        system_id = _slugify_system_id(raw_id)
        if not system_id or system_id in builtin_ids or system_id in seen:
            continue
        connection_type = str(item.get("connection_type") or "external_management").strip().lower()
        if connection_type not in {"external_management", "external_vpn_module", "external_network_source", "display_only"}:
            connection_type = "external_management"
        location = str(item.get("location") or "manual").strip().lower()
        if location not in {"docker", "host", "ip", "manual"}:
            location = "manual"
        address = str(item.get("address") or "").strip()[:160]
        runtime_type = str(item.get("runtime_type") or "").strip().lower()[:80]
        capabilities = _normalize_external_capabilities(item.get("capabilities"))
        endpoints = _normalize_external_endpoints(item.get("endpoints"))
        label = raw_label or system_id
        systems.append(
            {
                "system_id": system_id,
                "label": label[:80],
                "kind": "external",
                "lifecycle_mode": "external",
                "connection_type": connection_type,
                "location": location,
                "address": address,
                "runtime_type": runtime_type,
                "capabilities": capabilities,
                "endpoints": endpoints,
                "description": str(item.get("description") or _external_connection_description(connection_type)).strip()[:240],
                "custom": True,
            }
        )
        seen.add(system_id)
        if len(systems) >= 50:
            break
    return systems


def _normalize_system_visibility(saved: dict[str, Any]) -> dict[str, bool]:
    visibility = dict(UI_SYSTEM_VISIBILITY_DEFAULTS)
    incoming = saved.get("system_visibility")
    if isinstance(incoming, dict):
        for key, value in incoming.items():
            system_id = _slugify_system_id(key)
            if system_id:
                visibility[system_id] = bool(value)

    for system_id, legacy_key in UI_LEGACY_SYSTEM_VISIBILITY_KEYS.items():
        if legacy_key in saved:
            visibility[system_id] = bool(saved.get(legacy_key))
    return visibility


def _sync_legacy_display_keys(state: dict[str, Any]) -> None:
    visibility = state.get("system_visibility")
    if not isinstance(visibility, dict):
        visibility = dict(UI_SYSTEM_VISIBILITY_DEFAULTS)
        state["system_visibility"] = visibility
    for system_id, legacy_key in UI_LEGACY_SYSTEM_VISIBILITY_KEYS.items():
        state[legacy_key] = bool(visibility.get(system_id, UI_SYSTEM_VISIBILITY_DEFAULTS[system_id]))


def _system_visible(display_settings: dict[str, Any], system_id: str) -> bool:
    normalized = _slugify_system_id(system_id)
    visibility = display_settings.get("system_visibility")
    if isinstance(visibility, dict) and normalized in visibility:
        return bool(visibility.get(normalized))
    legacy_key = UI_LEGACY_SYSTEM_VISIBILITY_KEYS.get(normalized)
    if legacy_key and legacy_key in display_settings:
        return bool(display_settings.get(legacy_key))
    return bool(UI_SYSTEM_VISIBILITY_DEFAULTS.get(normalized, True))


def get_ui_display_settings() -> dict[str, Any]:
    state = _default_display_settings()
    saved = _load_setting(UI_DISPLAY_SETTINGS_KEY)
    if isinstance(saved, dict):
        state["system_visibility"] = _normalize_system_visibility(saved)
        state["custom_external_systems"] = _normalize_custom_external_systems(saved.get("custom_external_systems"))
        for key in ("show_inactive", "show_internal_xray"):
            if key in saved:
                state[key] = bool(saved.get(key))
        hidden_subject_ids = saved.get("hidden_subject_ids")
        if isinstance(hidden_subject_ids, list):
            state["hidden_subject_ids"] = [
                str(item).strip()
                for item in hidden_subject_ids
                if str(item).strip()
            ]
        traffic_preferences = saved.get("subject_traffic_preferences")
        if isinstance(traffic_preferences, dict):
            normalized_preferences: dict[str, list[str]] = {}
            for subject_id, metrics in traffic_preferences.items():
                normalized = _normalize_traffic_metric_keys(metrics)
                if normalized:
                    normalized_preferences[str(subject_id).strip()] = normalized
            state["subject_traffic_preferences"] = normalized_preferences
    _sync_legacy_display_keys(state)
    return state


def save_ui_display_settings(payload: dict[str, Any]) -> dict[str, Any]:
    state = _default_display_settings()
    state["system_visibility"] = _normalize_system_visibility(payload)
    state["custom_external_systems"] = _normalize_custom_external_systems(payload.get("custom_external_systems"))
    for custom_system in state["custom_external_systems"]:
        system_id = custom_system["system_id"]
        state["system_visibility"].setdefault(system_id, True)
    for key in ("show_inactive", "show_internal_xray"):
        if key in payload:
            state[key] = bool(payload.get(key))
    hidden_subject_ids = payload.get("hidden_subject_ids")
    if isinstance(hidden_subject_ids, list):
        state["hidden_subject_ids"] = [
            str(item).strip()
            for item in hidden_subject_ids
            if str(item).strip()
        ]
    traffic_preferences = payload.get("subject_traffic_preferences")
    if isinstance(traffic_preferences, dict):
        normalized_preferences: dict[str, list[str]] = {}
        for subject_id, metrics in traffic_preferences.items():
            normalized = _normalize_traffic_metric_keys(metrics)
            if normalized:
                normalized_preferences[str(subject_id).strip()] = normalized
        state["subject_traffic_preferences"] = normalized_preferences
    _sync_legacy_display_keys(state)
    _save_setting(UI_DISPLAY_SETTINGS_KEY, state)
    clear_live_probe_cache()
    prime_runtime_read_models_async(include_global_profiles=False)
    return state


def _display_systems(
    *,
    display_settings: dict[str, Any],
    counts: dict[str, int] | None = None,
    modules: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    module_map = {
        str(module.get("module_name") or ""): module
        for module in (modules if modules is not None else fetch_modules())
    }
    count_map = counts or {}
    systems: list[dict[str, Any]] = []

    for template in UI_DISPLAY_SYSTEMS:
        item = dict(template)
        base_kind = str(item.get("kind") or "")
        module_name = item.get("module_name")
        module = module_map.get(str(module_name or ""))
        if module:
            module_lifecycle_mode = str(module.get("lifecycle_mode") or item["lifecycle_mode"])
            if base_kind in {"managed", "external"}:
                item["lifecycle_mode"] = module_lifecycle_mode
                item["kind"] = module_lifecycle_mode if module_lifecycle_mode in {"managed", "external"} else base_kind
            item["desired_state"] = module.get("desired_state")
            item["runtime_state"] = module.get("runtime_state")
            item["apply_state"] = module.get("apply_state")
            item["status_text"] = module.get("status_text")
            item["installed"] = module.get("installed")
            item["manageable_actions"] = module.get("manageable_actions") or []
        count_key = item.get("count_key")
        item["count"] = int(count_map.get(str(count_key), 0)) if count_key else 0
        item["visible"] = _system_visible(display_settings, str(item["system_id"]))
        systems.append(item)

    for custom in _normalize_custom_external_systems(display_settings.get("custom_external_systems")):
        item = dict(custom)
        item["count"] = 0
        item["visible"] = _system_visible(display_settings, str(item["system_id"]))
        item["desired_state"] = None
        item["runtime_state"] = "external"
        item["apply_state"] = "clean"
        item["installed"] = True
        item["manageable_actions"] = []
        item["api_guide"] = _external_connection_guide(item)
        item["readiness"] = _external_connection_readiness(item)
        systems.append(item)
    existing_ids = {str(item.get("system_id") or "") for item in systems}
    systems.extend(
        item
        for item in _external_management_display_systems(display_settings=display_settings)
        if str(item.get("system_id") or "") not in existing_ids
    )
    return systems


def _external_management_display_systems(*, display_settings: dict[str, Any]) -> list[dict[str, Any]]:
    with db_session() as connection:
        rows = connection.execute(
            """
            SELECT details_json, created_at
            FROM operational_logs
            WHERE details_json LIKE '%external_client%'
               OR details_json LIKE '%management_attribution%'
            ORDER BY created_at DESC
            LIMIT 300
            """
        ).fetchall()

    clients: dict[str, dict[str, Any]] = {}
    for row in rows:
        details = _json_loads(row["details_json"])
        if not isinstance(details, dict):
            continue
        attribution = details.get("management_attribution")
        if not isinstance(attribution, dict):
            continue
        requested_by = str(attribution.get("requested_by") or details.get("requested_by") or "")
        source_type = str(attribution.get("source_type") or "").strip().lower()
        client_name = str(attribution.get("client_name") or "").strip()
        if not client_name and requested_by.startswith("external_client:"):
            client_name = requested_by.split(":", 1)[1].strip()
        if source_type != "external_client" and not requested_by.startswith("external_client:"):
            continue
        system_id = _slugify_system_id(f"external-management-{client_name}")
        if not system_id:
            continue
        item = clients.setdefault(
            system_id,
            {
                "system_id": system_id,
                "label": _external_management_label(client_name),
                "kind": "external",
                "lifecycle_mode": "external",
                "connection_type": "external_management",
                "location": "manual",
                "address": "",
                "description": _external_connection_description("external_management"),
                "custom": False,
                "count": 0,
                "visible": _system_visible(display_settings, system_id),
                "desired_state": None,
                "runtime_state": "external",
                "apply_state": "clean",
                "installed": True,
                "manageable_actions": [],
                "last_seen_at": row["created_at"],
                "last_action": attribution.get("action"),
                "channel": attribution.get("channel"),
                "api_guide": _external_connection_guide(
                    {"label": _external_management_label(client_name), "system_id": system_id}
                ),
                "readiness": {"state": "seen", "missing_fields": []},
            },
        )
        item["count"] = int(item["count"]) + 1
        if not item.get("last_action") and attribution.get("action"):
            item["last_action"] = attribution.get("action")
        if not item.get("channel") and attribution.get("channel"):
            item["channel"] = attribution.get("channel")

    return list(clients.values())


def _external_management_label(client_name: str) -> str:
    normalized = str(client_name or "").strip()
    if normalized.lower() in {"homeassistant", "home-assistant", "home_assistant", "ha"}:
        return "Home Assistant"
    return normalized.replace("_", " ").replace("-", " ").strip().title() or "External management"


def _external_connection_description(connection_type: str) -> str:
    if connection_type == "external_management":
        return "External management client: calls FWRouter API, not a routing target."
    if connection_type == "external_vpn_module":
        return "External VPN egress module: user-managed runtime that can be wired as a VPN provider after dataplane support is enabled."
    if connection_type == "external_network_source":
        return "External client source: user-managed ingress/network inventory provider."
    return "Display-only external system marker."


def _external_management_api_guide(system: dict[str, Any]) -> dict[str, Any]:
    label = str(system.get("label") or system.get("system_id") or "external-client").strip()
    client_slug = _slugify_system_id(label) or "external-client"
    requested_by = f"external_client:{client_slug}"
    management_context = {
        "client_name": client_slug,
        "channel": "local_api",
        "action": "<action>",
        "actor": "<optional-actor>",
    }
    return {
        "connection_type": "external_management",
        "purpose": "External client controls FWRouter through the HTTP API.",
        "configure": {
            "base_url": "http://<fwrouter-host>:5500/api/v2",
            "requested_by": requested_by,
            "management_context": management_context,
        },
        "available_elements": {
            "requested_by": "external_client:<client-name>",
            "management_context.client_name": client_slug,
            "management_context.channel": "local_api|webhook|automation|manual",
            "management_context.action": "<action>",
            "management_context.actor": "<optional-actor>",
        },
        "examples": [
            {
                "label": "Switch VPN-auto server",
                "method": "POST",
                "path": "/selector/switch",
                "body": {
                    "requested_by": requested_by,
                    "management_context": {
                        **management_context,
                        "action": "switch_best_vpn_auto_server",
                    },
                },
            },
            {
                "label": "Clear fixed global server",
                "method": "DELETE",
                "path": (
                    "/routing/global/fixed-server?confirm_switch=true"
                    f"&requested_by={requested_by}"
                    f"&management_client_name={client_slug}"
                    "&management_channel=local_api"
                    "&management_action=clear_global_fixed_server"
                ),
            },
        ],
    }


def _external_connection_guide(system: dict[str, Any]) -> dict[str, Any] | None:
    connection_type = str(system.get("connection_type") or "external_management")
    if connection_type == "external_management":
        return _external_management_api_guide(system)
    if connection_type == "external_vpn_module":
        return _external_vpn_module_guide(system)
    if connection_type == "external_network_source":
        return _external_network_source_guide(system)
    return None


def _external_vpn_module_guide(system: dict[str, Any]) -> dict[str, Any]:
    return {
        "connection_type": "external_vpn_module",
        "purpose": "User-managed runtime provides VPN egress endpoints; FWRouter does not own its lifecycle.",
        "routing_adapter": {
            "supported": "transparent_redir_tproxy",
            "required_for_dataplane": ["tcp_redir_port", "udp_tproxy_port"],
            "optional_full_vpn": ["full_tcp_redir_port", "full_udp_tproxy_port"],
            "note": "http_proxy_url and socks_proxy_url are metadata/explicit-proxy endpoints; nft transparent routing uses redir/tproxy ports.",
        },
        "configure": {
            "role": "vpn_egress",
            "runtime_type": system.get("runtime_type") or "<runtime>",
            "location": system.get("location") or "manual",
            "address": system.get("address") or "<host/container/ip>",
            "endpoints": system.get("endpoints") or {},
            "capabilities": system.get("capabilities") or {},
        },
        "available_elements": {
            "endpoints": [
                "http_proxy_url",
                "socks_proxy_url",
                "tcp_redir_port",
                "udp_tproxy_port",
                "controller_url",
                "healthcheck_url",
            ],
            "capabilities": [
                "supports_tcp",
                "supports_udp",
                "supports_http_proxy",
                "supports_socks_proxy",
                "supports_transparent_proxy",
                "supports_selector_api",
            ],
        },
        "probe": {
            "method": "GET",
            "url": "healthcheck_url or controller_url, if provided",
            "expected_response": {
                "status": "ok|degraded|down",
                "runtime_type": system.get("runtime_type") or "<runtime>",
                "selected_node": "<optional-current-node>",
                "version": "<optional-version>",
                "details": {},
            },
        },
        "example_config_line": (
            "role=vpn_egress,"
            f"runtime_type={system.get('runtime_type') or '<runtime>'},"
            f"location={system.get('location') or 'manual'},"
            f"address={system.get('address') or '<host/container/ip>'}"
        ),
    }


def _external_network_source_guide(system: dict[str, Any]) -> dict[str, Any]:
    return {
        "connection_type": "external_network_source",
        "purpose": "User-managed source provides client inventory, interface name, or client CIDR.",
        "configure": {
            "role": "client_source",
            "runtime_type": system.get("runtime_type") or "<source>",
            "location": system.get("location") or "manual",
            "address": system.get("address") or "<api/cli/interface>",
            "endpoints": system.get("endpoints") or {},
            "capabilities": system.get("capabilities") or {},
        },
        "available_elements": {
            "endpoints": [
                "client_inventory_url",
                "interface_name",
                "client_cidr",
                "healthcheck_url",
            ],
            "capabilities": [
                "supports_client_inventory",
            ],
        },
        "probe": {
            "method": "GET",
            "url": "client_inventory_url or healthcheck_url, if provided",
            "expected_response": {
                "status": "ok|degraded|down",
                "clients": [
                    {
                        "id": "<stable-client-id>",
                        "label": "<display-name>",
                        "address": "<ip-or-cidr>",
                        "metadata": {},
                    }
                ],
            },
        },
        "example_config_line": (
            "role=client_source,"
            f"runtime_type={system.get('runtime_type') or '<source>'},"
            f"location={system.get('location') or 'manual'},"
            f"address={system.get('address') or '<api/cli/interface>'}"
        ),
    }


def _normalize_external_capabilities(value: Any) -> dict[str, bool]:
    allowed = {
        "supports_tcp",
        "supports_udp",
        "supports_transparent_proxy",
        "supports_http_proxy",
        "supports_socks_proxy",
        "supports_selector_api",
        "supports_client_inventory",
    }
    if not isinstance(value, dict):
        return {}
    return {key: bool(value.get(key)) for key in sorted(allowed) if key in value}


def _normalize_external_endpoints(value: Any) -> dict[str, str]:
    allowed = {
        "controller_url",
        "http_proxy_url",
        "socks_proxy_url",
        "tcp_redir_port",
        "udp_tproxy_port",
        "full_tcp_redir_port",
        "full_udp_tproxy_port",
        "healthcheck_url",
        "client_inventory_url",
        "interface_name",
        "client_cidr",
    }
    if not isinstance(value, dict):
        return {}
    result: dict[str, str] = {}
    for key in sorted(allowed):
        raw = str(value.get(key) or "").strip()
        if raw:
            result[key] = raw[:180]
    return result


def _external_connection_readiness(system: dict[str, Any]) -> dict[str, Any]:
    connection_type = str(system.get("connection_type") or "")
    missing: list[str] = []
    if not str(system.get("label") or "").strip():
        missing.append("label")
    if connection_type in {"external_vpn_module", "external_network_source"} and not str(system.get("runtime_type") or "").strip():
        missing.append("runtime_type")
    if not str(system.get("location") or "").strip():
        missing.append("location")
    if connection_type == "external_vpn_module":
        endpoints = system.get("endpoints") if isinstance(system.get("endpoints"), dict) else {}
        capabilities = system.get("capabilities") if isinstance(system.get("capabilities"), dict) else {}
        has_proxy_endpoint = bool(endpoints.get("http_proxy_url") or endpoints.get("socks_proxy_url"))
        has_transparent_endpoint = bool(endpoints.get("tcp_redir_port") or endpoints.get("udp_tproxy_port"))
        if not has_proxy_endpoint and not has_transparent_endpoint:
            missing.append("proxy_or_transparent_endpoint")
        if not any(bool(value) for value in capabilities.values()):
            missing.append("capabilities")
    if connection_type == "external_network_source":
        endpoints = system.get("endpoints") if isinstance(system.get("endpoints"), dict) else {}
        if not (endpoints.get("client_inventory_url") or endpoints.get("interface_name") or endpoints.get("client_cidr")):
            missing.append("client_source")
    return {
        "state": "ready" if not missing else "incomplete",
        "missing_fields": missing,
    }


def _month_key(timestamp: datetime | None = None) -> str:
    current = timestamp or datetime.now(timezone.utc)
    return current.strftime("%Y-%m")


def _parse_ui_timestamp(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _subscription_group_token(group_subject_id: str) -> str:
    normalized = str(group_subject_id or "").strip().lower()
    if not normalized.startswith(XRAY_SUBSCRIPTION_GROUP_PREFIX):
        return ""
    return normalized[len(XRAY_SUBSCRIPTION_GROUP_PREFIX):].strip()


def _subscription_client_recent(subscription_client: dict[str, Any]) -> bool:
    if not subscription_client or not bool(subscription_client.get("enabled")):
        return False
    seen_at = _parse_ui_timestamp(subscription_client.get("last_seen_at"))
    if seen_at is None:
        return False
    age = datetime.now(timezone.utc) - seen_at
    return timedelta(seconds=0) <= age <= timedelta(seconds=XRAY_SUBSCRIPTION_ACTIVE_WINDOW_SECONDS)


def _activity_state(
    *,
    is_active: bool,
    last_seen_at: Any = None,
    last_traffic_at: Any = None,
    subscription_recent: bool = False,
    subscription_group: bool = False,
) -> dict[str, str]:
    if subscription_group and subscription_recent:
        return {
            "activity_reason": "profile_seen_24h",
            "activity_reason_label": "Профиль запрашивался за 24ч",
        }
    if is_active and last_traffic_at:
        return {
            "activity_reason": "traffic_seen",
            "activity_reason_label": "Был трафик",
        }
    if is_active:
        return {
            "activity_reason": "runtime_active",
            "activity_reason_label": "Runtime активен",
        }
    if last_seen_at or last_traffic_at:
        return {
            "activity_reason": "stale_seen",
            "activity_reason_label": "Нет свежей активности",
        }
    return {
        "activity_reason": "unknown",
        "activity_reason_label": "Нет данных активности",
    }


def _normalize_traffic_metric_keys(value: Any) -> list[str]:
    if not isinstance(value, list):
        return list(DEFAULT_TRAFFIC_PANEL_KEYS)
    normalized: list[str] = []
    for item in value:
        key = str(item or "").strip()
        if key not in TRAFFIC_METRIC_KEYS:
            continue
        if key in normalized:
            continue
        normalized.append(key)
        if len(normalized) >= 2:
            break
    if len(normalized) == 2:
        return normalized
    return list(DEFAULT_TRAFFIC_PANEL_KEYS)


def _subject_traffic_metric_keys(subject_id: str, display_settings: dict[str, Any]) -> list[str]:
    preferences = display_settings.get("subject_traffic_preferences")
    if isinstance(preferences, dict):
        keys = preferences.get(subject_id)
        return _normalize_traffic_metric_keys(keys)
    return list(DEFAULT_TRAFFIC_PANEL_KEYS)


def _panel_traffic_metrics(subject_id: str, month_breakdown: dict[str, int], display_settings: dict[str, Any]) -> list[dict[str, Any]]:
    metric_keys = _subject_traffic_metric_keys(subject_id, display_settings)
    return [
        {
            "key": key,
            "label": TRAFFIC_METRIC_LABELS[key],
            "bytes": int(month_breakdown.get(key, 0)),
        }
        for key in metric_keys
    ]


def _traffic_maps() -> tuple[dict[str, int], dict[str, int], dict[str, dict[str, int]]]:
    return get_live_probe_cache(
        "ui_state.traffic_maps",
        ttl_seconds=5.0,
        loader=_load_traffic_maps,
    )


def _load_traffic_maps() -> tuple[dict[str, int], dict[str, int], dict[str, dict[str, int]]]:
    current_month = _month_key()
    with db_session() as connection:
        rows = connection.execute(
            """
            SELECT
                subject_id,
                SUM(COALESCE(direct_rx_bytes, 0)) AS direct_rx_total_bytes,
                SUM(COALESCE(direct_tx_bytes, 0)) AS direct_tx_total_bytes,
                SUM(COALESCE(vpn_rx_bytes, 0)) AS vpn_rx_total_bytes,
                SUM(COALESCE(vpn_tx_bytes, 0)) AS vpn_tx_total_bytes,
                SUM(
                    COALESCE(direct_rx_bytes, 0) +
                    COALESCE(direct_tx_bytes, 0) +
                    COALESCE(vpn_rx_bytes, 0) +
                    COALESCE(vpn_tx_bytes, 0) +
                    COALESCE(blocked_rx_bytes, 0) +
                    COALESCE(blocked_tx_bytes, 0)
                ) AS total_bytes,
                SUM(CASE WHEN period_month = ? THEN COALESCE(direct_rx_bytes, 0) ELSE 0 END) AS direct_rx_month_bytes,
                SUM(CASE WHEN period_month = ? THEN COALESCE(direct_tx_bytes, 0) ELSE 0 END) AS direct_tx_month_bytes,
                SUM(CASE WHEN period_month = ? THEN COALESCE(vpn_rx_bytes, 0) ELSE 0 END) AS vpn_rx_month_bytes,
                SUM(CASE WHEN period_month = ? THEN COALESCE(vpn_tx_bytes, 0) ELSE 0 END) AS vpn_tx_month_bytes,
                SUM(
                    CASE WHEN period_month = ? THEN
                        COALESCE(direct_rx_bytes, 0) +
                        COALESCE(direct_tx_bytes, 0) +
                        COALESCE(vpn_rx_bytes, 0) +
                        COALESCE(vpn_tx_bytes, 0) +
                        COALESCE(blocked_rx_bytes, 0) +
                        COALESCE(blocked_tx_bytes, 0)
                    ELSE 0 END
                ) AS current_month_bytes
            FROM traffic_monthly
            GROUP BY subject_id
            """,
            (current_month, current_month, current_month, current_month, current_month),
        ).fetchall()

    total_map: dict[str, int] = {}
    month_map: dict[str, int] = {}
    month_breakdown_map: dict[str, dict[str, int]] = {}
    for row in rows:
        subject_id = str(row["subject_id"])
        total_map[subject_id] = int(row["total_bytes"] or 0)
        month_map[subject_id] = int(row["current_month_bytes"] or 0)
        month_breakdown_map[subject_id] = {
            "direct_rx_bytes": int(row["direct_rx_month_bytes"] or 0),
            "direct_tx_bytes": int(row["direct_tx_month_bytes"] or 0),
            "vpn_rx_bytes": int(row["vpn_rx_month_bytes"] or 0),
            "vpn_tx_bytes": int(row["vpn_tx_month_bytes"] or 0),
            "direct_rx_total_bytes": int(row["direct_rx_total_bytes"] or 0),
            "direct_tx_total_bytes": int(row["direct_tx_total_bytes"] or 0),
            "vpn_rx_total_bytes": int(row["vpn_rx_total_bytes"] or 0),
            "vpn_tx_total_bytes": int(row["vpn_tx_total_bytes"] or 0),
        }
    return total_map, month_map, month_breakdown_map


def _subscription_client_map() -> dict[str, dict[str, Any]]:
    return get_live_probe_cache(
        "ui_state.subscription_clients",
        ttl_seconds=5.0,
        loader=_load_subscription_client_map,
    )


def _load_subscription_client_map() -> dict[str, dict[str, Any]]:
    with db_session() as connection:
        rows = connection.execute(
            """
            SELECT client_id, token, app_type, enabled, display_name, last_seen_at, last_user_agent
            FROM subscription_clients
            """
        ).fetchall()

    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        token = str(row["token"] or "").strip().lower()
        if not token:
            continue
        result[token] = {
            "subscription_client_id": row["client_id"],
            "token": row["token"],
            "app_type": row["app_type"],
            "enabled": bool(row["enabled"]),
            "display_name": row["display_name"],
            "last_seen_at": row["last_seen_at"],
            "last_user_agent": row["last_user_agent"],
        }
    return result


def _list_effective_subjects_for_ui() -> list[dict[str, Any]]:
    return list_subjects_with_effective_state(include_deleted=False, limit=1000)


def _effective_state_by_subject_for_ui() -> dict[str, dict[str, Any]]:
    effective_subjects = get_live_probe_cache(
        "ui_state.effective_subjects",
        ttl_seconds=15.0,
        loader=_list_effective_subjects_for_ui,
    )
    return {
        str(item["subject_id"]): dict(item.get("effective_state") or {})
        for item in effective_subjects
    }


def _active_user_override_modes(subject_ids: list[str]) -> dict[str, str]:
    normalized = [str(subject_id).strip() for subject_id in subject_ids if str(subject_id).strip()]
    if not normalized:
        return {}
    placeholders = ", ".join("?" for _ in normalized)
    with db_session() as connection:
        rows = connection.execute(
            f"""
            SELECT subject_id, override_mode
            FROM subject_user_overrides
            WHERE subject_id IN ({placeholders})
              AND override_mode IS NOT NULL
              AND override_until > CURRENT_TIMESTAMP
            """,
            tuple(normalized),
        ).fetchall()
    return {
        str(row["subject_id"]): str(row["override_mode"])
        for row in rows
        if str(row["override_mode"] or "").strip()
    }


def _human_xray_email(email: str) -> bool:
    normalized = str(email or "").strip().lower()
    return bool(normalized) and not normalized.startswith(XRAY_INTERNAL_PREFIXES)


def _xray_internal(email: str) -> bool:
    normalized = str(email or "").strip().lower()
    return normalized.startswith(XRAY_INTERNAL_PREFIXES)


def _xray_service_subject(email: str) -> bool:
    normalized = str(email or "").strip().lower()
    return normalized.startswith("vpn-auto-")


def _xray_legacy_subscription_shadow(email: str, subscription_map: dict[str, dict[str, Any]]) -> bool:
    normalized = str(email or "").strip().lower()
    if not normalized or normalized.startswith(XRAY_INTERNAL_PREFIXES):
        return False
    return _localpart(normalized) in subscription_map


def _localpart(email: str) -> str:
    return str(email or "").split("@", 1)[0].strip().lower()


def _xray_subscription_group(row: Any) -> tuple[str, str] | None:
    return xray_subscription_group_from_row(row)


def _sum_month_breakdowns(subject_ids: list[str], month_breakdown_map: dict[str, dict[str, int]]) -> dict[str, int]:
    result = {key: 0 for key in TRAFFIC_METRIC_KEYS}
    for subject_id in subject_ids:
        breakdown = month_breakdown_map.get(subject_id, {})
        for key in TRAFFIC_METRIC_KEYS:
            result[key] += int(breakdown.get(key, 0))
    return result


def _latest_text(values: list[Any]) -> Any:
    present = [value for value in values if value]
    return max(present) if present else None


def _xray_group_mode(values: list[Any], default: str = "enabled") -> str:
    present = [str(value or default).strip().lower() for value in values if str(value or "").strip()]
    if not present:
        return default.upper()
    if len(set(present)) == 1:
        return present[0].upper()
    if "enabled" in present:
        return "ENABLED"
    return present[0].upper()


def _xray_opaque_subscription_label(label: Any) -> bool:
    return str(label or "").strip().lower().startswith("sub-")


def _row_bool(row: Any, key: str) -> bool:
    return bool(int(row[key] or 0))


def _active_job(lock_key: str) -> dict[str, Any] | None:
    lease = get_active_lock_lease(lock_key)
    if lease is None:
        return None
    job_id = str(lease.get("owner_job_id") or "").strip()
    if not job_id:
        return None
    return get_job_without_cleanup(job_id)


def _job_summary(job: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(job, dict):
        return None
    return {
        "job_id": job.get("job_id"),
        "job_type": job.get("job_type"),
        "status": job.get("status"),
        "requested_by": job.get("requested_by"),
        "created_at": job.get("created_at"),
        "started_at": job.get("started_at"),
        "error_code": job.get("error_code"),
        "error_message": job.get("error_message"),
    }


def _summarize_system_subject(subject: dict[str, Any]) -> dict[str, Any]:
    detail = subject.get("detail") if isinstance(subject.get("detail"), dict) else {}
    metadata = subject.get("metadata") if isinstance(subject.get("metadata"), dict) else {}
    return {
        "subject_id": subject.get("subject_id"),
        "subject_type": subject.get("subject_type"),
        "display_name": subject.get("display_name"),
        "alias": subject.get("alias"),
        "desired_mode": subject.get("desired_mode"),
        "applied_mode": subject.get("applied_mode"),
        "apply_state": subject.get("apply_state"),
        "runtime_state": subject.get("runtime_state"),
        "visibility": subject.get("visibility"),
        "is_active": bool(subject.get("is_active")),
        "is_deleted": bool(subject.get("is_deleted")),
        "can_delete": bool(subject.get("can_delete")),
        "last_seen_at": subject.get("last_seen_at"),
        "updated_at": subject.get("updated_at"),
        "detail": {
            "container_name": detail.get("container_name"),
            "project": detail.get("project"),
            "service": detail.get("service"),
            "host": detail.get("host"),
            "port": detail.get("port"),
            "protocol": detail.get("protocol"),
            "status": detail.get("status"),
        },
        "metadata": {
            "component_name": metadata.get("component_name"),
            "source": metadata.get("source"),
        },
    }


def _system_subject_counts() -> dict[str, int]:
    with db_session() as connection:
        rows = connection.execute(
            """
            SELECT subject_type, COUNT(*) AS count
            FROM subjects
            WHERE is_deleted = 0
              AND subject_type IN ('docker', 'host', 'fwrouter')
            GROUP BY subject_type
            """
        ).fetchall()

    counts = {"docker": 0, "host": 0, "fwrouter": 0}
    for row in rows:
        subject_type = str(row["subject_type"] or "")
        if subject_type in counts:
            counts[subject_type] = int(row["count"] or 0)
    return counts


def list_ui_clients() -> list[dict[str, Any]]:
    display_settings = get_ui_display_settings()
    total_map, month_map, month_breakdown_map = _traffic_maps()
    subscription_map = _subscription_client_map()
    effective_state_by_subject = _effective_state_by_subject_for_ui()

    with db_session() as connection:
        lan_rows = connection.execute(
            """
            SELECT
                s.subject_id,
                s.subject_type,
                s.display_name,
                s.alias,
                s.desired_mode,
                s.applied_mode,
                s.apply_state,
                s.runtime_state,
                s.is_active,
                s.last_seen_at,
                s.last_traffic_at,
                sl.ip_address,
                sl.mac_address,
                sl.hostname
            FROM subjects s
            JOIN subject_lan sl ON sl.subject_id = s.subject_id
            WHERE s.is_deleted = 0
            ORDER BY s.is_active DESC, COALESCE(s.last_seen_at, s.updated_at) DESC
            """
        ).fetchall()

        tailscale_rows = connection.execute(
            """
            SELECT
                s.subject_id,
                s.subject_type,
                s.display_name,
                s.alias,
                s.desired_mode,
                s.applied_mode,
                s.apply_state,
                s.runtime_state,
                s.is_active,
                s.last_seen_at,
                s.last_traffic_at,
                st.tailscale_ip,
                st.hostname,
                st.user_name,
                st.online
            FROM subjects s
            JOIN subject_tailscale st ON st.subject_id = s.subject_id
            WHERE s.is_deleted = 0
            ORDER BY s.is_active DESC, COALESCE(s.last_seen_at, s.updated_at) DESC
            """
        ).fetchall()

        xray_rows = connection.execute(
            """
            SELECT
                s.subject_id,
                s.subject_type,
                s.display_name,
                s.alias,
                s.desired_mode,
                s.applied_mode,
                s.apply_state,
                s.runtime_state,
                s.is_active,
                s.last_seen_at,
                s.last_traffic_at,
                sx.client_id,
                sx.client_uuid,
                sx.email,
                sx.subscription_path,
                sx.last_subscription_at,
                sx.enabled
            FROM subjects s
            JOIN subject_xray sx ON sx.subject_id = s.subject_id
            WHERE s.is_deleted = 0
            ORDER BY COALESCE(s.last_seen_at, s.updated_at) DESC
            """
        ).fetchall()

    clients: list[dict[str, Any]] = []

    for row in lan_rows:
        subject_id = str(row["subject_id"])
        effective_state = effective_state_by_subject.get(subject_id, {})
        month_breakdown = month_breakdown_map.get(subject_id, {})
        clients.append(
            {
                "subject_id": subject_id,
                "kind": "lan",
                "display_name": str(row["alias"] or row["display_name"] or row["hostname"] or row["ip_address"] or subject_id),
                "alias": row["alias"],
                "hostname": row["hostname"],
                "ip_address": row["ip_address"],
                "mac_address": row["mac_address"],
                "mode_source": str(effective_state.get("mode_source") or "global").upper(),
                "effective_mode": str(effective_state.get("effective_mode") or row["desired_mode"] or "global").upper(),
                "committed_desired_mode": str(row["desired_mode"] or "global").upper(),
                "desired_mode": str(row["desired_mode"] or "global").upper(),
                "applied_mode": str(row["applied_mode"] or row["desired_mode"] or "global").upper(),
                "apply_state": str(row["apply_state"] or "clean"),
                "runtime_state": row["runtime_state"],
                "is_active": _row_bool(row, "is_active"),
                "is_internal": False,
                "last_seen_at": row["last_seen_at"],
                "last_traffic_at": row["last_traffic_at"],
                "traffic_total_bytes": int(total_map.get(subject_id, 0)),
                "traffic_month_bytes": int(month_map.get(subject_id, 0)),
                "traffic_month": month_breakdown,
                "traffic_panel_metric_keys": _subject_traffic_metric_keys(subject_id, display_settings),
                "traffic_panel_metrics": _panel_traffic_metrics(subject_id, month_breakdown, display_settings),
            }
        )

    for row in tailscale_rows:
        subject_id = str(row["subject_id"])
        effective_state = effective_state_by_subject.get(subject_id, {})
        month_breakdown = month_breakdown_map.get(subject_id, {})
        clients.append(
            {
                "subject_id": subject_id,
                "kind": "tailscale",
                "display_name": str(row["alias"] or row["display_name"] or row["hostname"] or row["tailscale_ip"] or subject_id),
                "alias": row["alias"],
                "hostname": row["hostname"],
                "ip_address": row["tailscale_ip"],
                "mac_address": None,
                "user_name": row["user_name"],
                "online": _row_bool(row, "online"),
                "mode_source": str(effective_state.get("mode_source") or "global").upper(),
                "effective_mode": str(effective_state.get("effective_mode") or row["desired_mode"] or "global").upper(),
                "committed_desired_mode": str(row["desired_mode"] or "global").upper(),
                "desired_mode": str(row["desired_mode"] or "global").upper(),
                "applied_mode": str(row["applied_mode"] or row["desired_mode"] or "global").upper(),
                "apply_state": str(row["apply_state"] or "clean"),
                "runtime_state": row["runtime_state"],
                "is_active": _row_bool(row, "is_active"),
                "is_internal": False,
                "last_seen_at": row["last_seen_at"],
                "last_traffic_at": row["last_traffic_at"],
                "traffic_total_bytes": int(total_map.get(subject_id, 0)),
                "traffic_month_bytes": int(month_map.get(subject_id, 0)),
                "traffic_month": month_breakdown,
                "traffic_panel_metric_keys": _subject_traffic_metric_keys(subject_id, display_settings),
                "traffic_panel_metrics": _panel_traffic_metrics(subject_id, month_breakdown, display_settings),
            }
        )

    grouped_xray: dict[str, dict[str, Any]] = {}

    for row in xray_rows:
        subject_id = str(row["subject_id"])
        effective_state = effective_state_by_subject.get(subject_id, {})
        email = str(row["email"] or "")
        if _xray_service_subject(email):
            continue
        if _xray_legacy_subscription_shadow(email, subscription_map):
            continue
        token = _localpart(email)
        subscription_client: dict[str, Any] = {}
        alias = str(row["alias"] or "").strip() or None
        display_name = (
            alias
            or str(subscription_client.get("display_name") or "").strip()
            or str(row["display_name"] or "").strip()
            or token
            or str(row["client_id"] or subject_id)
        )
        last_seen_at = subscription_client.get("last_seen_at") or row["last_seen_at"]
        month_breakdown = month_breakdown_map.get(subject_id, {})
        group = _xray_subscription_group(row)
        if group is not None:
            group_subject_id, group_label = group
            subscription_client = subscription_map.get(_subscription_group_token(group_subject_id), {})
            subscription_recent = _subscription_client_recent(subscription_client)
            last_seen_at = subscription_client.get("last_seen_at") or row["last_seen_at"]
            bucket = grouped_xray.setdefault(
                group_subject_id,
                {
                    "subject_id": group_subject_id,
                    "subject_ids": [],
                    "kind": "xray",
                    "display_name": group_label,
                    "alias": group_label,
                    "email": email,
                    "client_id": None,
                    "client_uuid": None,
                    "subscription_path": None,
                    "subscription_client": subscription_client,
                    "mode_source_values": [],
                    "effective_mode_values": [],
                    "desired_mode_values": [],
                    "applied_mode_values": [],
                    "apply_state_values": [],
                    "runtime_state_values": [],
                    "is_active": False,
                    "is_internal": False,
                    "is_human": False,
                    "enabled": False,
                    "last_seen_values": [],
                    "last_traffic_values": [],
                    "traffic_total_bytes": 0,
                    "traffic_month_bytes": 0,
                    "traffic_month": {key: 0 for key in TRAFFIC_METRIC_KEYS},
                    "last_subscription_values": [],
                    "last_user_agent_values": [],
                    "member_count": 0,
                    "is_aggregate": True,
                    "aggregate_kind": "xray_subscription",
                    "can_delete": False,
                },
            )
            bucket["subject_ids"].append(subject_id)
            bucket["member_count"] += 1
            bucket["traffic_total_bytes"] += int(total_map.get(subject_id, 0))
            bucket["traffic_month_bytes"] += int(month_map.get(subject_id, 0))
            for key in TRAFFIC_METRIC_KEYS:
                bucket["traffic_month"][key] += int(month_breakdown.get(key, 0))
            bucket["mode_source_values"].append(effective_state.get("mode_source") or "enabled")
            bucket["effective_mode_values"].append(effective_state.get("effective_mode") or row["desired_mode"] or "enabled")
            bucket["desired_mode_values"].append(row["desired_mode"] or "enabled")
            bucket["applied_mode_values"].append(row["applied_mode"] or row["desired_mode"] or "enabled")
            bucket["apply_state_values"].append(row["apply_state"] or "clean")
            bucket["runtime_state_values"].append(row["runtime_state"])
            bucket["is_active"] = bool(bucket["is_active"]) or _row_bool(row, "is_active") or subscription_recent
            bucket["enabled"] = bool(bucket["enabled"]) or _row_bool(row, "enabled")
            if subscription_client and not bucket["subscription_client"]:
                bucket["subscription_client"] = subscription_client
            bucket["last_seen_values"].append(last_seen_at)
            bucket["last_traffic_values"].append(row["last_traffic_at"])
            bucket["last_subscription_values"].append(row["last_subscription_at"])
            bucket["last_user_agent_values"].append(subscription_client.get("last_user_agent"))
            continue

        clients.append(
            {
                "subject_id": subject_id,
                "kind": "xray",
                "display_name": display_name,
                "alias": alias,
                "email": email,
                "client_id": row["client_id"],
                "client_uuid": row["client_uuid"],
                "subscription_path": row["subscription_path"],
                "subscription_client": subscription_client,
                "mode_source": str(effective_state.get("mode_source") or "enabled").upper(),
                "effective_mode": str(effective_state.get("effective_mode") or row["desired_mode"] or "enabled").upper(),
                "committed_desired_mode": str(row["desired_mode"] or "enabled").upper(),
                "desired_mode": str(row["desired_mode"] or "enabled").upper(),
                "applied_mode": str(row["applied_mode"] or row["desired_mode"] or "enabled").upper(),
                "apply_state": str(row["apply_state"] or "clean"),
                "runtime_state": row["runtime_state"],
                "is_active": _row_bool(row, "is_active") or bool(subscription_client.get("last_seen_at")),
                **_activity_state(
                    is_active=_row_bool(row, "is_active"),
                    last_seen_at=last_seen_at,
                    last_traffic_at=row["last_traffic_at"],
                ),
                "is_internal": _xray_internal(email),
                "is_human": _human_xray_email(email),
                "enabled": _row_bool(row, "enabled"),
                "last_seen_at": last_seen_at,
                "last_traffic_at": row["last_traffic_at"],
                "traffic_total_bytes": int(total_map.get(subject_id, 0)),
                "traffic_month_bytes": int(month_map.get(subject_id, 0)),
                "traffic_month": month_breakdown,
                "traffic_panel_metric_keys": _subject_traffic_metric_keys(subject_id, display_settings),
                "traffic_panel_metrics": _panel_traffic_metrics(subject_id, month_breakdown, display_settings),
                "last_subscription_at": row["last_subscription_at"],
                "last_user_agent": subscription_client.get("last_user_agent"),
            }
        )

    for bucket in grouped_xray.values():
        if _xray_opaque_subscription_label(bucket["display_name"]):
            continue
        subject_id = str(bucket["subject_id"])
        month_breakdown = dict(bucket["traffic_month"])
        group_is_active = bool(bucket["is_active"])
        group_last_seen_at = _latest_text(bucket["last_seen_values"])
        group_last_traffic_at = _latest_text(bucket["last_traffic_values"])
        group_subscription_recent = _subscription_client_recent(bucket["subscription_client"])
        clients.append(
            {
                "subject_id": subject_id,
                "subject_ids": list(bucket["subject_ids"]),
                "kind": "xray",
                "display_name": bucket["display_name"],
                "alias": bucket["alias"],
                "email": bucket["email"],
                "client_id": bucket["client_id"],
                "client_uuid": bucket["client_uuid"],
                "subscription_path": bucket["subscription_path"],
                "subscription_client": bucket["subscription_client"],
                "mode_source": _xray_group_mode(bucket["mode_source_values"], "enabled"),
                "effective_mode": _xray_group_mode(bucket["effective_mode_values"], "enabled"),
                "committed_desired_mode": _xray_group_mode(bucket["desired_mode_values"], "enabled"),
                "desired_mode": _xray_group_mode(bucket["desired_mode_values"], "enabled"),
                "applied_mode": _xray_group_mode(bucket["applied_mode_values"], "enabled"),
                "apply_state": "failed" if "failed" in {str(item or "").lower() for item in bucket["apply_state_values"]} else "clean",
                "runtime_state": _latest_text(bucket["runtime_state_values"]),
                "is_active": group_is_active,
                **_activity_state(
                    is_active=group_is_active,
                    last_seen_at=group_last_seen_at,
                    last_traffic_at=group_last_traffic_at,
                    subscription_recent=group_subscription_recent,
                    subscription_group=True,
                ),
                "is_internal": False,
                "is_human": False,
                "enabled": bool(bucket["enabled"]),
                "last_seen_at": group_last_seen_at,
                "last_traffic_at": group_last_traffic_at,
                "traffic_total_bytes": int(bucket["traffic_total_bytes"]),
                "traffic_month_bytes": int(bucket["traffic_month_bytes"]),
                "traffic_month": month_breakdown,
                "traffic_panel_metric_keys": _subject_traffic_metric_keys(subject_id, display_settings),
                "traffic_panel_metrics": _panel_traffic_metrics(subject_id, month_breakdown, display_settings),
                "last_subscription_at": _latest_text(bucket["last_subscription_values"]),
                "last_user_agent": _latest_text(bucket["last_user_agent_values"]),
                "member_count": int(bucket["member_count"]),
                "is_aggregate": True,
                "aggregate_kind": "xray_subscription",
                "can_delete": False,
            }
        )

    kind_rank = {"lan": 0, "tailscale": 1, "xray": 2}
    clients.sort(
        key=lambda item: (
            kind_rank.get(str(item.get("kind")), 99),
            0 if bool(item.get("is_active")) else 1,
            str(item.get("display_name") or "").lower(),
        )
    )
    return clients


def filter_ui_clients(
    clients: list[dict[str, Any]],
    *,
    display_settings: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    settings = display_settings or get_ui_display_settings()
    hidden_subject_ids = {
        str(item).strip()
        for item in settings.get("hidden_subject_ids", [])
        if str(item).strip()
    }
    filtered: list[dict[str, Any]] = []
    for client in clients:
        if str(client.get("subject_id") or "").strip() in hidden_subject_ids:
            continue
        kind = str(client.get("kind") or "")
        if kind in {"lan", "tailscale", "xray"} and not _system_visible(settings, kind):
            continue
        if not settings["show_inactive"] and not bool(client.get("is_active")):
            continue
        if kind == "xray" and not settings["show_internal_xray"] and bool(client.get("is_internal")):
            continue
        filtered.append(client)
    return filtered


def _ui_client_stats(
    clients: list[dict[str, Any]],
    *,
    display_settings: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    hidden_subject_ids = {
        str(item).strip()
        for item in display_settings.get("hidden_subject_ids", [])
        if str(item).strip()
    }
    panel_clients: list[dict[str, Any]] = []
    counts = {
        "all": len(clients),
        "panel": 0,
        "lan": 0,
        "tailscale": 0,
        "xray": 0,
        "xray_internal": 0,
    }
    for client in clients:
        kind = str(client.get("kind") or "")
        if kind == "lan":
            counts["lan"] += 1
        elif kind == "tailscale":
            counts["tailscale"] += 1
        elif kind == "xray":
            if bool(client.get("is_internal")):
                counts["xray_internal"] += 1
            else:
                counts["xray"] += 1

        if str(client.get("subject_id") or "").strip() in hidden_subject_ids:
            continue
        if kind in {"lan", "tailscale", "xray"} and not _system_visible(display_settings, kind):
            continue
        if not display_settings["show_inactive"] and not bool(client.get("is_active")):
            continue
        if kind == "xray" and not display_settings["show_internal_xray"] and bool(client.get("is_internal")):
            continue
        panel_clients.append(client)

    counts["panel"] = len(panel_clients)
    return panel_clients, counts


def _list_ui_client_presence() -> list[dict[str, Any]]:
    subscription_map = _subscription_client_map()

    with db_session() as connection:
        basic_rows = connection.execute(
            """
            SELECT subject_id, subject_type, is_active
            FROM subjects
            WHERE is_deleted = 0 AND subject_type IN ('lan', 'tailscale')
            """
        ).fetchall()
        xray_rows = connection.execute(
            """
            SELECT s.subject_id, s.display_name, s.alias, s.is_active, sx.email
            FROM subjects AS s
            JOIN subject_xray AS sx ON sx.subject_id = s.subject_id
            WHERE s.is_deleted = 0
            """
        ).fetchall()

    items: list[dict[str, Any]] = []
    for row in basic_rows:
        items.append(
            {
                "subject_id": str(row["subject_id"]),
                "kind": str(row["subject_type"]),
                "is_active": _row_bool(row, "is_active"),
                "is_internal": False,
            }
        )

    grouped_xray: dict[str, dict[str, Any]] = {}
    for row in xray_rows:
        email = str(row["email"] or "")
        if _xray_service_subject(email):
            continue
        if _xray_legacy_subscription_shadow(email, subscription_map):
            continue
        group = _xray_subscription_group(row)
        if group is not None:
            group_subject_id, _group_label = group
            subscription_client = subscription_map.get(_subscription_group_token(group_subject_id), {})
            bucket = grouped_xray.setdefault(
                group_subject_id,
                {
                    "subject_id": group_subject_id,
                    "display_name": _group_label,
                    "kind": "xray",
                    "is_active": False,
                    "is_internal": False,
                },
            )
            bucket["is_active"] = bool(bucket["is_active"]) or _row_bool(row, "is_active") or _subscription_client_recent(subscription_client)
            continue
        subscription_client: dict[str, Any] = {}
        items.append(
            {
                "subject_id": str(row["subject_id"]),
                "kind": "xray",
                "is_active": _row_bool(row, "is_active") or bool(subscription_client.get("last_seen_at")),
                "is_internal": _xray_internal(email),
            }
        )

    items.extend(
        bucket
        for bucket in grouped_xray.values()
        if not _xray_opaque_subscription_label(bucket.get("display_name"))
    )
    return items


def _ui_workspace_counts(*, display_settings: dict[str, Any]) -> dict[str, int]:
    hidden_subject_ids = {
        str(item).strip()
        for item in display_settings.get("hidden_subject_ids", [])
        if str(item).strip()
    }
    counts = {
        "all": 0,
        "panel": 0,
        "lan": 0,
        "tailscale": 0,
        "xray": 0,
        "xray_internal": 0,
        "docker": 0,
        "host": 0,
        "fwrouter": 0,
    }

    for client in _list_ui_client_presence():
        counts["all"] += 1
        kind = str(client.get("kind") or "")
        if kind == "lan":
            counts["lan"] += 1
        elif kind == "tailscale":
            counts["tailscale"] += 1
        elif kind == "xray":
            if bool(client.get("is_internal")):
                counts["xray_internal"] += 1
            else:
                counts["xray"] += 1

        if str(client.get("subject_id") or "").strip() in hidden_subject_ids:
            continue
        if kind in {"lan", "tailscale", "xray"} and not _system_visible(display_settings, kind):
            continue
        if not display_settings["show_inactive"] and not bool(client.get("is_active")):
            continue
        if kind == "xray" and not display_settings["show_internal_xray"] and bool(client.get("is_internal")):
            continue
        counts["panel"] += 1

    return counts


def list_ui_settings_inventory(
    *,
    kind: str = "all",
    query: str = "",
    limit: int = 200,
) -> list[dict[str, Any]]:
    normalized_kind = str(kind or "all").strip().lower()
    normalized_query = str(query or "").strip().lower()
    display_settings = get_ui_display_settings()
    total_map, month_map, month_breakdown_map = _traffic_maps()
    subscription_map = _subscription_client_map()
    include_client_kinds = normalized_kind in {"all", "lan", "tailscale", "xray"}
    include_system_kinds = normalized_kind in {"all", "docker", "host"}
    routing = get_routing_global_state() or {}
    global_effective_mode = str(routing.get("desired_mode") or routing.get("applied_mode") or "direct").upper()

    def traffic_payload(subject_id: str) -> dict[str, Any]:
        month_breakdown = month_breakdown_map.get(subject_id, {})
        return {
            "traffic_total_bytes": int(total_map.get(subject_id, 0)),
            "traffic_month_bytes": int(month_map.get(subject_id, 0)),
            "traffic_month": month_breakdown,
            "traffic_panel_metric_keys": _subject_traffic_metric_keys(subject_id, display_settings),
            "traffic_panel_metrics": _panel_traffic_metrics(subject_id, month_breakdown, display_settings),
        }

    def mode_source_for(desired_mode: str | None) -> str:
        return "GLOBAL" if str(desired_mode or "").lower() == "global" else "ADMIN_LOCKED"

    def effective_mode_for(
        *,
        subject_id: str,
        desired_mode: str | None,
        applied_mode: str,
        user_override_modes: dict[str, str],
    ) -> str:
        if str(desired_mode or "").lower() != "global":
            return applied_mode
        override_mode = user_override_modes.get(subject_id)
        if override_mode:
            return str(override_mode).upper()
        return global_effective_mode

    def mode_source_with_user_override(
        *,
        subject_id: str,
        desired_mode: str | None,
        user_override_modes: dict[str, str],
    ) -> str:
        if str(desired_mode or "").lower() == "global" and subject_id in user_override_modes:
            return "USER_OVERRIDE"
        return mode_source_for(desired_mode)

    items: list[dict[str, Any]] = []
    with db_session() as connection:
        if include_client_kinds:
            if normalized_kind in {"all", "lan"}:
                rows = connection.execute(
                    """
                    SELECT
                        s.subject_id, s.display_name, s.alias, s.desired_mode, s.applied_mode,
                        s.apply_state, s.runtime_state, s.is_active, s.last_seen_at, s.last_traffic_at,
                        sl.ip_address, sl.mac_address, sl.hostname
                    FROM subjects AS s
                    JOIN subject_lan AS sl ON sl.subject_id = s.subject_id
                    WHERE s.is_deleted = 0
                    ORDER BY s.is_active DESC, COALESCE(s.last_seen_at, s.updated_at) DESC
                    LIMIT ?
                    """,
                    (max(limit * 2, limit),),
                ).fetchall()
                user_override_modes = _active_user_override_modes([str(row["subject_id"]) for row in rows])
                for row in rows:
                    subject_id = str(row["subject_id"])
                    desired = str(row["desired_mode"] or "global").upper()
                    applied = str(row["applied_mode"] or row["desired_mode"] or "global").upper()
                    items.append(
                        {
                            "subject_id": subject_id,
                            "kind": "lan",
                            "display_name": str(row["alias"] or row["display_name"] or row["hostname"] or row["ip_address"] or subject_id),
                            "alias": row["alias"],
                            "hostname": row["hostname"],
                            "ip_address": row["ip_address"],
                            "mac_address": row["mac_address"],
                            "mode_source": mode_source_with_user_override(
                                subject_id=subject_id,
                                desired_mode=row["desired_mode"],
                                user_override_modes=user_override_modes,
                            ),
                            "effective_mode": effective_mode_for(
                                subject_id=subject_id,
                                desired_mode=row["desired_mode"],
                                applied_mode=applied,
                                user_override_modes=user_override_modes,
                            ),
                            "committed_desired_mode": desired,
                            "desired_mode": desired,
                            "applied_mode": applied,
                            "apply_state": str(row["apply_state"] or "clean"),
                            "runtime_state": row["runtime_state"],
                            "is_active": _row_bool(row, "is_active"),
                            "is_internal": False,
                            "last_seen_at": row["last_seen_at"],
                            "last_traffic_at": row["last_traffic_at"],
                            **traffic_payload(subject_id),
                        }
                    )

            if normalized_kind in {"all", "tailscale"}:
                rows = connection.execute(
                    """
                    SELECT
                        s.subject_id, s.display_name, s.alias, s.desired_mode, s.applied_mode,
                        s.apply_state, s.runtime_state, s.is_active, s.last_seen_at, s.last_traffic_at,
                        st.tailscale_ip, st.hostname, st.user_name, st.online
                    FROM subjects AS s
                    JOIN subject_tailscale AS st ON st.subject_id = s.subject_id
                    WHERE s.is_deleted = 0
                    ORDER BY s.is_active DESC, COALESCE(s.last_seen_at, s.updated_at) DESC
                    LIMIT ?
                    """,
                    (max(limit * 2, limit),),
                ).fetchall()
                user_override_modes = _active_user_override_modes([str(row["subject_id"]) for row in rows])
                for row in rows:
                    subject_id = str(row["subject_id"])
                    desired = str(row["desired_mode"] or "global").upper()
                    applied = str(row["applied_mode"] or row["desired_mode"] or "global").upper()
                    items.append(
                        {
                            "subject_id": subject_id,
                            "kind": "tailscale",
                            "display_name": str(row["alias"] or row["display_name"] or row["hostname"] or row["tailscale_ip"] or subject_id),
                            "alias": row["alias"],
                            "hostname": row["hostname"],
                            "ip_address": row["tailscale_ip"],
                            "mac_address": None,
                            "user_name": row["user_name"],
                            "online": _row_bool(row, "online"),
                            "mode_source": mode_source_with_user_override(
                                subject_id=subject_id,
                                desired_mode=row["desired_mode"],
                                user_override_modes=user_override_modes,
                            ),
                            "effective_mode": effective_mode_for(
                                subject_id=subject_id,
                                desired_mode=row["desired_mode"],
                                applied_mode=applied,
                                user_override_modes=user_override_modes,
                            ),
                            "committed_desired_mode": desired,
                            "desired_mode": desired,
                            "applied_mode": applied,
                            "apply_state": str(row["apply_state"] or "clean"),
                            "runtime_state": row["runtime_state"],
                            "is_active": _row_bool(row, "is_active"),
                            "is_internal": False,
                            "last_seen_at": row["last_seen_at"],
                            "last_traffic_at": row["last_traffic_at"],
                            **traffic_payload(subject_id),
                        }
                    )

            if normalized_kind in {"all", "xray"}:
                rows = connection.execute(
                    """
                    SELECT
                        s.subject_id, s.display_name, s.alias, s.desired_mode, s.applied_mode,
                        s.apply_state, s.runtime_state, s.is_active, s.last_seen_at, s.last_traffic_at,
                        sx.client_id, sx.client_uuid, sx.email, sx.subscription_path,
                        sx.last_subscription_at, sx.enabled
                    FROM subjects AS s
                    JOIN subject_xray AS sx ON sx.subject_id = s.subject_id
                    WHERE s.is_deleted = 0
                    ORDER BY COALESCE(s.last_seen_at, s.updated_at) DESC
                    LIMIT ?
                    """,
                    (max(limit * 2, limit),),
                ).fetchall()
                grouped_xray: dict[str, dict[str, Any]] = {}
                for row in rows:
                    subject_id = str(row["subject_id"])
                    email = str(row["email"] or "")
                    if _xray_service_subject(email):
                        continue
                    if _xray_legacy_subscription_shadow(email, subscription_map):
                        continue
                    token = _localpart(email)
                    subscription_client: dict[str, Any] = {}
                    desired = str(row["desired_mode"] or "enabled").upper()
                    applied = str(row["applied_mode"] or row["desired_mode"] or "enabled").upper()
                    alias = str(row["alias"] or "").strip() or None
                    group = _xray_subscription_group(row)
                    if group is not None:
                        group_subject_id, group_label = group
                        subscription_client = subscription_map.get(_subscription_group_token(group_subject_id), {})
                        subscription_recent = _subscription_client_recent(subscription_client)
                        bucket = grouped_xray.setdefault(
                            group_subject_id,
                            {
                                "subject_id": group_subject_id,
                                "subject_ids": [],
                                "kind": "xray",
                                "display_name": group_label,
                                "alias": group_label,
                                "email": email,
                                "client_id": None,
                                "client_uuid": None,
                                "subscription_path": None,
                                "subscription_client": subscription_client,
                                "desired_values": [],
                                "applied_values": [],
                                "apply_state_values": [],
                                "runtime_state_values": [],
                                "is_active": False,
                                "is_internal": False,
                                "is_human": False,
                                "enabled": False,
                                "last_seen_values": [],
                                "last_traffic_values": [],
                                "last_subscription_values": [],
                                "last_user_agent_values": [],
                                "traffic_total_bytes": 0,
                                "traffic_month_bytes": 0,
                                "traffic_month": {key: 0 for key in TRAFFIC_METRIC_KEYS},
                                "member_count": 0,
                                "is_aggregate": True,
                                "aggregate_kind": "xray_subscription",
                                "can_delete": False,
                            },
                        )
                        month_breakdown = month_breakdown_map.get(subject_id, {})
                        bucket["subject_ids"].append(subject_id)
                        bucket["member_count"] += 1
                        bucket["desired_values"].append(row["desired_mode"] or "enabled")
                        bucket["applied_values"].append(row["applied_mode"] or row["desired_mode"] or "enabled")
                        bucket["apply_state_values"].append(row["apply_state"] or "clean")
                        bucket["runtime_state_values"].append(row["runtime_state"])
                        bucket["is_active"] = bool(bucket["is_active"]) or _row_bool(row, "is_active") or subscription_recent
                        bucket["enabled"] = bool(bucket["enabled"]) or _row_bool(row, "enabled")
                        if subscription_client and not bucket["subscription_client"]:
                            bucket["subscription_client"] = subscription_client
                        bucket["last_seen_values"].append(subscription_client.get("last_seen_at") or row["last_seen_at"])
                        bucket["last_traffic_values"].append(row["last_traffic_at"])
                        bucket["last_subscription_values"].append(row["last_subscription_at"])
                        bucket["last_user_agent_values"].append(subscription_client.get("last_user_agent"))
                        bucket["traffic_total_bytes"] += int(total_map.get(subject_id, 0))
                        bucket["traffic_month_bytes"] += int(month_map.get(subject_id, 0))
                        for key in TRAFFIC_METRIC_KEYS:
                            bucket["traffic_month"][key] += int(month_breakdown.get(key, 0))
                        continue
                    items.append(
                        {
                            "subject_id": subject_id,
                            "kind": "xray",
                            "display_name": alias or str(subscription_client.get("display_name") or "").strip() or str(row["display_name"] or "").strip() or token or str(row["client_id"] or subject_id),
                            "alias": alias,
                            "email": email,
                            "client_id": row["client_id"],
                            "client_uuid": row["client_uuid"],
                            "subscription_path": row["subscription_path"],
                            "subscription_client": subscription_client,
                            "mode_source": mode_source_for(row["desired_mode"]),
                            "effective_mode": applied,
                            "committed_desired_mode": desired,
                            "desired_mode": desired,
                            "applied_mode": applied,
                            "apply_state": str(row["apply_state"] or "clean"),
                            "runtime_state": row["runtime_state"],
                            "is_active": _row_bool(row, "is_active") or bool(subscription_client.get("last_seen_at")),
                            **_activity_state(
                                is_active=_row_bool(row, "is_active"),
                                last_seen_at=subscription_client.get("last_seen_at") or row["last_seen_at"],
                                last_traffic_at=row["last_traffic_at"],
                            ),
                            "is_internal": _xray_internal(email),
                            "is_human": _human_xray_email(email),
                            "enabled": _row_bool(row, "enabled"),
                            "last_seen_at": subscription_client.get("last_seen_at") or row["last_seen_at"],
                            "last_traffic_at": row["last_traffic_at"],
                            "last_subscription_at": row["last_subscription_at"],
                            "last_user_agent": subscription_client.get("last_user_agent"),
                            **traffic_payload(subject_id),
                        }
                    )
                for bucket in grouped_xray.values():
                    if _xray_opaque_subscription_label(bucket["display_name"]):
                        continue
                    subject_id = str(bucket["subject_id"])
                    month_breakdown = dict(bucket["traffic_month"])
                    group_is_active = bool(bucket["is_active"])
                    group_last_seen_at = _latest_text(bucket["last_seen_values"])
                    group_last_traffic_at = _latest_text(bucket["last_traffic_values"])
                    group_subscription_recent = _subscription_client_recent(bucket["subscription_client"])
                    items.append(
                        {
                            "subject_id": subject_id,
                            "subject_ids": list(bucket["subject_ids"]),
                            "kind": "xray",
                            "display_name": bucket["display_name"],
                            "alias": bucket["alias"],
                            "email": bucket["email"],
                            "client_id": bucket["client_id"],
                            "client_uuid": bucket["client_uuid"],
                            "subscription_path": bucket["subscription_path"],
                            "subscription_client": bucket["subscription_client"],
                            "mode_source": "ADMIN_LOCKED",
                            "effective_mode": _xray_group_mode(bucket["applied_values"], "enabled"),
                            "committed_desired_mode": _xray_group_mode(bucket["desired_values"], "enabled"),
                            "desired_mode": _xray_group_mode(bucket["desired_values"], "enabled"),
                            "applied_mode": _xray_group_mode(bucket["applied_values"], "enabled"),
                            "apply_state": "failed" if "failed" in {str(item or "").lower() for item in bucket["apply_state_values"]} else "clean",
                            "runtime_state": _latest_text(bucket["runtime_state_values"]),
                            "is_active": group_is_active,
                            **_activity_state(
                                is_active=group_is_active,
                                last_seen_at=group_last_seen_at,
                                last_traffic_at=group_last_traffic_at,
                                subscription_recent=group_subscription_recent,
                                subscription_group=True,
                            ),
                            "is_internal": False,
                            "is_human": False,
                            "enabled": bool(bucket["enabled"]),
                            "last_seen_at": group_last_seen_at,
                            "last_traffic_at": group_last_traffic_at,
                            "last_subscription_at": _latest_text(bucket["last_subscription_values"]),
                            "last_user_agent": _latest_text(bucket["last_user_agent_values"]),
                            "traffic_total_bytes": int(bucket["traffic_total_bytes"]),
                            "traffic_month_bytes": int(bucket["traffic_month_bytes"]),
                            "traffic_month": month_breakdown,
                            "traffic_panel_metric_keys": _subject_traffic_metric_keys(subject_id, display_settings),
                            "traffic_panel_metrics": _panel_traffic_metrics(subject_id, month_breakdown, display_settings),
                            "member_count": int(bucket["member_count"]),
                            "is_aggregate": True,
                            "aggregate_kind": "xray_subscription",
                            "can_delete": False,
                        }
                    )

        if include_system_kinds:
            if normalized_kind in {"all", "docker"}:
                rows = connection.execute(
                    """
                    SELECT
                        s.subject_id, s.display_name, s.alias, s.desired_mode, s.applied_mode,
                        s.apply_state, s.runtime_state, s.is_active, s.last_seen_at,
                        sd.container_name, sd.compose_project, sd.compose_service
                    FROM subjects AS s
                    JOIN subject_docker AS sd ON sd.subject_id = s.subject_id
                    WHERE s.is_deleted = 0
                    ORDER BY s.is_active DESC, COALESCE(s.last_seen_at, s.updated_at) DESC
                    LIMIT ?
                    """,
                    (max(limit * 2, limit),),
                ).fetchall()
                for row in rows:
                    subject_id = str(row["subject_id"])
                    desired = str(row["desired_mode"] or "direct").upper()
                    applied = str(row["applied_mode"] or row["desired_mode"] or "direct").upper()
                    items.append(
                        {
                            "kind": "docker",
                            "subject_id": subject_id,
                            "display_name": str(row["alias"] or row["display_name"] or row["container_name"] or subject_id),
                            "alias": str(row["alias"] or ""),
                            "ip_address": "",
                            "mac_address": "",
                            "email": "",
                            "hostname": str(row["container_name"] or row["display_name"] or subject_id),
                            "user_name": "",
                            "mode_source": "SYSTEM",
                            "effective_mode": applied,
                            "committed_desired_mode": desired,
                            "applied_mode": applied,
                            "desired_mode": desired,
                            "runtime_state": str(row["runtime_state"] or ""),
                            "is_active": _row_bool(row, "is_active"),
                            "is_internal": False,
                            "last_seen_at": str(row["last_seen_at"] or ""),
                            "visibility": "active" if _row_bool(row, "is_active") else "inactive",
                            "can_delete": not _row_bool(row, "is_active"),
                            "traffic_month_bytes": 0,
                            "traffic_total_bytes": 0,
                            "traffic_month": {},
                            "traffic_panel_metric_keys": list(DEFAULT_TRAFFIC_PANEL_KEYS),
                            "traffic_panel_metrics": [
                                {"key": key, "label": TRAFFIC_METRIC_LABELS[key], "bytes": 0}
                                for key in DEFAULT_TRAFFIC_PANEL_KEYS
                            ],
                        }
                    )

            if normalized_kind in {"all", "host"}:
                rows = connection.execute(
                    """
                    SELECT
                        s.subject_id, s.display_name, s.alias, s.desired_mode, s.applied_mode,
                        s.apply_state, s.runtime_state, s.is_active, s.last_seen_at,
                        sh.systemd_unit, sh.process_name
                    FROM subjects AS s
                    JOIN subject_host AS sh ON sh.subject_id = s.subject_id
                    WHERE s.is_deleted = 0
                    ORDER BY s.is_active DESC, COALESCE(s.last_seen_at, s.updated_at) DESC
                    LIMIT ?
                    """,
                    (max(limit * 2, limit),),
                ).fetchall()
                for row in rows:
                    subject_id = str(row["subject_id"])
                    desired = str(row["desired_mode"] or "direct").upper()
                    applied = str(row["applied_mode"] or row["desired_mode"] or "direct").upper()
                    name = str(row["alias"] or row["display_name"] or row["systemd_unit"] or row["process_name"] or subject_id)
                    items.append(
                        {
                            "kind": "host",
                            "subject_id": subject_id,
                            "display_name": name,
                            "alias": str(row["alias"] or ""),
                            "ip_address": "",
                            "mac_address": "",
                            "email": "",
                            "hostname": name,
                            "user_name": "",
                            "mode_source": "SYSTEM",
                            "effective_mode": applied,
                            "committed_desired_mode": desired,
                            "applied_mode": applied,
                            "desired_mode": desired,
                            "runtime_state": str(row["runtime_state"] or ""),
                            "is_active": _row_bool(row, "is_active"),
                            "is_internal": False,
                            "last_seen_at": str(row["last_seen_at"] or ""),
                            "visibility": "active" if _row_bool(row, "is_active") else "inactive",
                            "can_delete": not _row_bool(row, "is_active"),
                            "traffic_month_bytes": 0,
                            "traffic_total_bytes": 0,
                            "traffic_month": {},
                            "traffic_panel_metric_keys": list(DEFAULT_TRAFFIC_PANEL_KEYS),
                            "traffic_panel_metrics": [
                                {"key": key, "label": TRAFFIC_METRIC_LABELS[key], "bytes": 0}
                                for key in DEFAULT_TRAFFIC_PANEL_KEYS
                            ],
                        }
                    )

    filtered: list[dict[str, Any]] = []
    for item in items:
        item_kind = str(item.get("kind") or "").lower()
        if normalized_kind != "all" and item_kind != normalized_kind:
            continue
        if item_kind in {"lan", "tailscale", "xray", "docker", "host"} and not _system_visible(display_settings, item_kind):
            continue
        if item_kind == "xray" and not display_settings["show_internal_xray"] and bool(item.get("is_internal")):
            continue
        if not display_settings["show_inactive"] and not bool(item.get("is_active")):
            continue
        if normalized_query:
            haystack = "\n".join(
                [
                    str(item.get("display_name") or ""),
                    str(item.get("alias") or ""),
                    str(item.get("ip_address") or ""),
                    str(item.get("mac_address") or ""),
                    str(item.get("email") or ""),
                    str(item.get("hostname") or ""),
                    str(item.get("user_name") or ""),
                    str(item.get("subject_id") or ""),
                ]
            ).lower()
            if normalized_query not in haystack:
                continue
        filtered.append(item)
        if len(filtered) >= limit:
            break
    return filtered


def get_router_self_subject() -> dict[str, Any] | None:
    with db_session() as connection:
        row = connection.execute(
            """
            SELECT subject_id
            FROM subject_fwrouter
            WHERE component_name = 'global'
            ORDER BY updated_at DESC
            LIMIT 1
            """
        ).fetchone()
    if row is None:
        return None
    return get_subject(str(row["subject_id"]))


def _server_name_by_id(server_id: str | None) -> str | None:
    normalized = str(server_id or "").strip()
    if not normalized:
        return None
    with db_session() as connection:
        row = connection.execute(
            """
            SELECT server_name
            FROM servers
            WHERE server_id = ?
            LIMIT 1
            """,
            (normalized,),
        ).fetchone()
    return str(row["server_name"]).strip() if row and row["server_name"] else None


def get_ui_router_summary() -> dict[str, Any]:
    return get_live_probe_cache(
        "ui_state.router_summary",
        ttl_seconds=2.0,
        loader=_build_ui_router_summary,
    )


def _build_ui_router_summary() -> dict[str, Any]:
    routing = get_routing_global_state() or {}
    router_subject = get_router_self_subject()
    active_apply_job = _active_job("apply")
    fixed_server_id = str(
        routing.get("applied_fixed_server_id")
        or routing.get("desired_fixed_server_id")
        or ""
    ).strip()
    current_server_name = (
        _server_name_by_id(fixed_server_id)
        if fixed_server_id
        else str(routing.get("active_auto_server_id") or "").strip() or None
    )
    return {
        "global_mode": str(routing.get("applied_mode") or routing.get("desired_mode") or "direct").upper(),
        "global_mode_desired": str(routing.get("desired_mode") or "direct").upper(),
        "selective_default": str(routing.get("selective_default") or "direct").upper(),
        "router_self_mode": str((router_subject or {}).get("applied_mode") or (router_subject or {}).get("desired_mode") or "disabled").upper(),
        "router_self_mode_desired": str((router_subject or {}).get("desired_mode") or "disabled").upper(),
        "router_self_subject_id": (router_subject or {}).get("subject_id"),
        "router_self_display_name": (router_subject or {}).get("display_name"),
        "server_mode": str(routing.get("server_mode") or "auto").upper(),
        "active_auto_server_id": routing.get("active_auto_server_id"),
        "fixed_server_id": fixed_server_id or None,
        "current_server_name": current_server_name,
        "current_server_source": "manual" if fixed_server_id else "vpn-auto",
        "routing_apply_state": routing.get("apply_state"),
        "routing_error_code": routing.get("error_code"),
        "routing_error_message": routing.get("error_message"),
        "active_job": _job_summary(active_apply_job),
    }


def get_ui_settings_workspace() -> dict[str, Any]:
    return get_live_probe_cache(
        "ui_state.settings_workspace",
        ttl_seconds=2.0,
        loader=_build_ui_settings_workspace,
    )


def _build_ui_settings_workspace() -> dict[str, Any]:
    display_settings = get_ui_display_settings()
    counts = _ui_workspace_counts(display_settings=display_settings)
    modules = fetch_modules()
    subscription = dict(get_subscription_state() or {})
    subscription["url_saved"] = bool(subscription.get("url"))
    xray = get_xray_status()
    counts.update(_system_subject_counts())
    operational_logs = [
        _summarize_log_event(item)
        for item in list_operational_logs(limit=20)
    ]
    technical_logs = [
        _summarize_log_event(item, technical=True)
        for item in list_technical_logs(limit=20)
    ]
    return {
        "display_settings": display_settings,
        "display_systems": _display_systems(display_settings=display_settings, counts=counts, modules=modules),
        "modules": modules,
        "router": get_ui_router_summary(),
        "subscription": subscription,
        "traffic": get_traffic_accounting_state(),
        "xray": xray,
        "counts": counts,
        "logs": {
            "operational_recent": operational_logs,
            "technical_recent": technical_logs,
            "operational_count": len(operational_logs),
            "technical_count": len(technical_logs),
        },
    }
