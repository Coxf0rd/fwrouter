from __future__ import annotations

import json
from typing import Any

from fwrouter_api.db.connection import db_session
from fwrouter_api.services.modules import fetch_modules


def _json_loads(value: str | None) -> dict[str, Any] | None:
    if not value:
        return None
    loaded = json.loads(value)
    return loaded if isinstance(loaded, dict) else None


UI_DISPLAY_SETTINGS_KEY = "ui.admin_client_display.v1"
UI_SYSTEM_VISIBILITY_DEFAULTS = {
    "lan": True,
    "external_network_source": True,
    "vless_client": True,
    "vpn_runtime": True,
    "docker": True,
    "host": True,
}
UI_DISPLAY_SYSTEMS = (
    {
        "system_id": "lan",
        "label": "Lan / Core",
        "kind": "core",
        "lifecycle_mode": "core",
        "module_name": "core",
        "count_key": "lan_client",
        "description": "Клиенты LAN и routing core FWRouter.",
        "custom": False,
        "always_show": True,
    },
    {
        "system_id": "external_network_source",
        "label": "Внешняя сеть",
        "kind": "external",
        "lifecycle_mode": "external",
        "module_name": None,
        "count_key": "external_network_source",
        "description": "Внешний источник клиентов; FWRouter показывает его только когда есть реальные discovered clients.",
        "custom": False,
    },
    {
        "system_id": "vless_client",
        "label": "Vless",
        "kind": "managed",
        "lifecycle_mode": "managed",
        "module_name": "xray",
        "count_key": "vless_client",
        "description": "Клиентское ядро Vless; конкретная реализация хранится отдельно.",
        "custom": False,
    },
    {
        "system_id": "vpn_runtime",
        "label": "VPN runtime",
        "kind": "managed",
        "lifecycle_mode": "managed",
        "module_name": "vpn",
        "count_key": None,
        "description": "VPN/dataplane adapter FWRouter; конкретная реализация хранится отдельно.",
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
        elif char in {"-", ".", ":"} and not previous_dash:
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
        replacement_target = _normalize_replacement_target(item.get("replacement_target") or item.get("replaces"))
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
                "replacement_target": replacement_target,
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


def external_connection_identity(system: dict[str, Any]) -> dict[str, str]:
    system_id = _slugify_system_id(system.get("system_id") or system.get("label"))
    label = str(system.get("label") or system_id or "external-client").strip()
    client_slug = system_id or _slugify_system_id(label) or "external-client"
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


def custom_external_system_by_id(system_id: str) -> dict[str, Any] | None:
    normalized = _slugify_system_id(system_id)
    if not normalized:
        return None
    with db_session() as connection:
        row = connection.execute(
            "SELECT value_json FROM settings WHERE key = ?",
            (UI_DISPLAY_SETTINGS_KEY,),
        ).fetchone()
    settings = _json_loads(row["value_json"]) if row else {}
    settings = settings if isinstance(settings, dict) else {}
    for system in _normalize_custom_external_systems(settings.get("custom_external_systems")):
        if str(system.get("system_id") or "") == normalized:
            return system
    return None


def external_connection_contract(system_id: str) -> dict[str, Any] | None:
    normalized = _slugify_system_id(system_id)
    if not normalized:
        return None
    system = custom_external_system_by_id(system_id)
    if system:
        item = dict(system)
    else:
        with db_session() as connection:
            row = connection.execute(
                "SELECT value_json FROM settings WHERE key = ?",
                (UI_DISPLAY_SETTINGS_KEY,),
            ).fetchone()
        display_settings = _json_loads(row["value_json"]) if row else {}
        display_settings = display_settings if isinstance(display_settings, dict) else {}
        item = next(
            (
                dict(candidate)
                for candidate in _external_management_display_systems(display_settings=display_settings)
                if str(candidate.get("system_id") or "") == normalized
            ),
            None,
        )
    if not item:
        return None
    identity = external_connection_identity(item)
    item["identity"] = identity
    item["external_system_id"] = identity["external_system_id"]
    item["requested_by"] = identity["requested_by"]
    item["collector"] = identity["collector"]
    item["api_guide"] = _external_connection_guide(item)
    item["readiness"] = _external_connection_readiness(item)
    return item


def _normalize_system_visibility(saved: dict[str, Any], extra_system_ids: set[str] | None = None) -> dict[str, bool]:
    visibility = dict(UI_SYSTEM_VISIBILITY_DEFAULTS)
    allowed_system_ids = set(UI_SYSTEM_VISIBILITY_DEFAULTS)
    if extra_system_ids:
        allowed_system_ids.update(
            system_id
            for system_id in (_slugify_system_id(item) for item in extra_system_ids)
            if system_id
        )
    incoming = saved.get("system_visibility")
    if isinstance(incoming, dict):
        for key, value in incoming.items():
            system_id = _slugify_system_id(key)
            if system_id in allowed_system_ids or system_id.startswith("external-management-"):
                visibility[system_id] = bool(value)
    return visibility


def _system_visible(display_settings: dict[str, Any], system_id: str) -> bool:
    normalized = _slugify_system_id(system_id)
    visibility = display_settings.get("system_visibility")
    if isinstance(visibility, dict) and normalized in visibility:
        return bool(visibility.get(normalized))
    return bool(UI_SYSTEM_VISIBILITY_DEFAULTS.get(normalized, True))


def _module_has_real_runtime(module: dict[str, Any] | None) -> bool:
    if not module:
        return False
    lifecycle_mode = str(module.get("lifecycle_mode") or "none")
    if lifecycle_mode == "none":
        return False
    if bool(module.get("installed")):
        return True
    runtime_state = str(module.get("runtime_state") or "").strip().lower()
    return runtime_state in {"running", "active", "degraded"}


def _display_system_has_data(item: dict[str, Any], module: dict[str, Any] | None, count: int) -> bool:
    if bool(item.get("always_show")):
        return True
    system_id = str(item.get("system_id") or "")
    if system_id in {"external_network_source", "docker", "host"}:
        return count > 0
    if system_id in {"vless_client", "vpn_runtime"}:
        return count > 0 or _module_has_real_runtime(module)
    return count > 0 or _module_has_real_runtime(module)


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
        count_key = item.get("count_key")
        count = int(count_map.get(str(count_key), 0)) if count_key else 0
        if not _display_system_has_data(item, module, count):
            continue
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
        item["count"] = count
        item["visible"] = _system_visible(display_settings, str(item["system_id"]))
        systems.append(item)

    for custom in _normalize_custom_external_systems(display_settings.get("custom_external_systems")):
        item = dict(custom)
        identity = external_connection_identity(item)
        item["identity"] = identity
        item["external_system_id"] = identity["external_system_id"]
        item["requested_by"] = identity["requested_by"]
        item["collector"] = identity["collector"]
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
    identity = external_connection_identity(system)
    client_slug = identity["external_system_id"]
    requested_by = identity["requested_by"]
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
                "path": "/selector/vpn-auto/switch",
                "body": {
                    "confirm_switch": True,
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
    identity = external_connection_identity(system)
    replacement_target = _normalize_replacement_target(system.get("replacement_target"))
    target = replacement_target or "mihomo"
    explicit_client_runtime = {
        "supported": "external_explicit_client_runtime_contract",
        "required_for_contract": ["controller_url or healthcheck_url"],
        "optional_endpoints": [
            "client_inventory_url",
            "subscription_base_url",
            "traffic_stats_url",
            "reload_url",
        ],
        "note": (
            "This marks the connection as a user-managed explicit-client runtime. "
            "FWRouter exposes identity, traffic collection, and status contract; "
            "runtime-specific client create/delete/proxy logic still belongs to an adapter."
        ),
    } if target == "xray" else None
    return {
        "connection_type": "external_vpn_module",
        "purpose": "User-managed runtime provides VPN egress endpoints; FWRouter does not own its lifecycle.",
        "identity": identity,
        "replacement_target": target,
        "routing_adapter": {
            "supported": "transparent_redir_tproxy",
            "required_for_dataplane": ["tcp_redir_port", "udp_tproxy_port"],
            "optional_full_vpn": ["full_tcp_redir_port", "full_udp_tproxy_port"],
            "note": "http_proxy_url and socks_proxy_url are metadata/explicit-proxy endpoints; nft transparent routing uses redir/tproxy ports.",
        },
        "explicit_client_runtime": explicit_client_runtime,
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
                "client_inventory_url",
                "subscription_base_url",
                "traffic_stats_url",
                "reload_url",
            ],
            "capabilities": [
                "supports_tcp",
                "supports_udp",
                "supports_http_proxy",
                "supports_socks_proxy",
                "supports_transparent_proxy",
                "supports_selector_api",
                "supports_client_api",
                "supports_subscription_api",
                "supports_traffic_stats",
                "supports_reload",
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
        "traffic_accounting": {
            "method": "POST",
            "path": "/traffic/collect",
            "body": {
                "requested_by": identity["requested_by"],
                "collector": identity["collector"],
                "samples": [
                    {
                        "counter_key": f"{identity['external_system_id']}:vpn",
                        "subject_id": "<existing-fwrouter-subject-id>",
                        "path": "vpn",
                        "rx_bytes": 0,
                        "tx_bytes": 0,
                        "metadata": {
                            "external_system_id": identity["external_system_id"],
                            "connection_type": "external_vpn_module",
                            "source": "external_runtime_api",
                        },
                    }
                ],
            },
            "watchdog_note": (
                "Only external_vpn_module samples may declare metadata.watchdog_signal="
                "adapter_response when they report real response traffic."
            ),
        },
        "example_config_line": (
            "role=vpn_egress,"
            f"runtime_type={system.get('runtime_type') or '<runtime>'},"
            f"location={system.get('location') or 'manual'},"
            f"address={system.get('address') or '<host/container/ip>'}"
        ),
    }


def _external_network_source_guide(system: dict[str, Any]) -> dict[str, Any]:
    identity = external_connection_identity(system)
    return {
        "connection_type": "external_network_source",
        "purpose": "User-managed source provides client inventory, interface name, or client CIDR.",
        "identity": identity,
        "replacement_target": _normalize_replacement_target(system.get("replacement_target")),
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
        "traffic_accounting": {
            "method": "POST",
            "path": "/traffic/collect",
            "body": {
                "requested_by": identity["requested_by"],
                "collector": identity["collector"],
                "samples": [
                    {
                        "counter_key": f"{identity['external_system_id']}:<client-id>:vpn",
                        "subject_id": "<existing-fwrouter-subject-id>",
                        "path": "vpn",
                        "rx_bytes": 0,
                        "tx_bytes": 0,
                        "metadata": {
                            "external_system_id": identity["external_system_id"],
                            "connection_type": "external_network_source",
                            "source": "external_inventory_api",
                        },
                    }
                ],
            },
            "note": "Registration/display is built in; automatic subject import still needs a provider adapter.",
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
        "supports_client_api",
        "supports_subscription_api",
        "supports_traffic_stats",
        "supports_reload",
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
        "subscription_base_url",
        "traffic_stats_url",
        "client_api_url",
        "reload_url",
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
    details: dict[str, Any] = {}
    if not str(system.get("label") or "").strip():
        missing.append("label")
    if connection_type in {"external_vpn_module", "external_network_source"} and not str(system.get("runtime_type") or "").strip():
        missing.append("runtime_type")
    if not str(system.get("location") or "").strip():
        missing.append("location")
    if connection_type == "external_vpn_module":
        endpoints = system.get("endpoints") if isinstance(system.get("endpoints"), dict) else {}
        capabilities = system.get("capabilities") if isinstance(system.get("capabilities"), dict) else {}
        replacement_target = _normalize_replacement_target(system.get("replacement_target")) or "mihomo"
        details["replacement_target"] = replacement_target
        details["tcp_redir_port_present"] = bool(endpoints.get("tcp_redir_port"))
        details["udp_tproxy_port_present"] = bool(endpoints.get("udp_tproxy_port"))
        details["healthcheck_configured"] = bool(endpoints.get("healthcheck_url"))
        has_proxy_endpoint = bool(endpoints.get("http_proxy_url") or endpoints.get("socks_proxy_url"))
        has_transparent_endpoint = bool(endpoints.get("tcp_redir_port") or endpoints.get("udp_tproxy_port"))
        if not has_proxy_endpoint and not has_transparent_endpoint:
            missing.append("proxy_or_transparent_endpoint")
        if replacement_target == "mihomo":
            if not endpoints.get("tcp_redir_port"):
                missing.append("tcp_redir_port")
            if not endpoints.get("udp_tproxy_port"):
                missing.append("udp_tproxy_port")
        if replacement_target == "xray":
            details["replacement_support"] = "explicit_client_runtime_contract"
            if not (endpoints.get("controller_url") or endpoints.get("healthcheck_url")):
                missing.append("controller_or_healthcheck_url")
        if not any(bool(value) for value in capabilities.values()):
            missing.append("capabilities")
        try:
            from fwrouter_api.services.external_vpn import active_external_vpn_module

            active_module = active_external_vpn_module()
        except Exception:
            active_module = None
        active_system_id = str((active_module or {}).get("system_id") or "")
        details["active_as_vpn_adapter"] = bool(active_system_id and active_system_id == str(system.get("system_id") or ""))
        if details["active_as_vpn_adapter"]:
            details["active_adapter"] = {
                "system_id": active_system_id,
                "runtime_type": (active_module or {}).get("runtime_type"),
                "redir_port": (active_module or {}).get("redir_port"),
                "tproxy_port": (active_module or {}).get("tproxy_port"),
            }
    if connection_type == "external_network_source":
        endpoints = system.get("endpoints") if isinstance(system.get("endpoints"), dict) else {}
        if not (endpoints.get("client_inventory_url") or endpoints.get("interface_name") or endpoints.get("client_cidr")):
            missing.append("client_source")
    return {
        "state": "active" if details.get("active_as_vpn_adapter") else ("ready" if not missing else "incomplete"),
        "missing_fields": missing,
        "details": details,
    }
