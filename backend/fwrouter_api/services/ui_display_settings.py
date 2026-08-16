from __future__ import annotations

import json
from typing import Any

from fwrouter_api.db.connection import db_session
from fwrouter_api.services.live_probe_cache import clear_live_probe_cache
from fwrouter_api.services.modules import fetch_modules


def _json_loads(value: str | None) -> dict[str, Any] | None:
    if not value:
        return None
    loaded = json.loads(value)
    return loaded if isinstance(loaded, dict) else None


def _json_dumps(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


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
        "show_in_connections": False,
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


def _builtin_external_connection_by_id(system_id: str, display_settings: dict[str, Any] | None = None) -> dict[str, Any] | None:
    normalized = _slugify_system_id(system_id)
    if not normalized:
        return None
    settings = display_settings if isinstance(display_settings, dict) else _load_display_settings_raw()
    builtin_candidates = [
        *_external_management_display_systems(display_settings=settings),
        *_external_network_source_display_systems(display_settings=settings),
    ]
    return next(
        (
            dict(candidate)
            for candidate in builtin_candidates
            if str(candidate.get("system_id") or "") == normalized
        ),
        None,
    )


def preview_custom_external_connection(payload: dict[str, Any], *, system_id: str | None = None) -> dict[str, Any]:
    item = _normalize_external_connection_input(payload, system_id=system_id, existing=None, partial=False)
    return _external_connection_response(item)


def upsert_custom_external_connection(system_id: str, payload: dict[str, Any], *, partial: bool = False) -> dict[str, Any]:
    normalized_id = _slugify_system_id(system_id)
    if not normalized_id:
        raise ExternalConnectionValidationError(
            "INVALID_EXTERNAL_CONNECTION_ID",
            "External connection id is required.",
            {"system_id": "required"},
        )

    saved = _load_display_settings_raw()
    current = _normalize_custom_external_systems(saved.get("custom_external_systems"))
    existing = next((item for item in current if str(item.get("system_id") or "") == normalized_id), None)
    if existing is None:
        existing = _builtin_external_connection_by_id(normalized_id, display_settings=saved)
    if partial and existing is None:
        raise ExternalConnectionValidationError(
            "EXTERNAL_CONNECTION_NOT_FOUND",
            "External connection is not registered in UI display settings.",
            {"system_id": "not_found"},
        )
    item = _normalize_external_connection_input(
        payload,
        system_id=normalized_id,
        existing=existing,
        partial=partial,
    )
    next_systems = [entry for entry in current if str(entry.get("system_id") or "") != normalized_id]
    next_systems.append(item)
    saved["custom_external_systems"] = _normalize_custom_external_systems(next_systems)
    visibility = saved.get("system_visibility") if isinstance(saved.get("system_visibility"), dict) else {}
    visibility = dict(visibility)
    visibility.setdefault(normalized_id, True)
    saved["system_visibility"] = visibility
    _save_display_settings_raw(saved)
    stored = custom_external_system_by_id(normalized_id) or item
    return {
        "external_connection": _external_connection_response(stored)["external_connection"],
        "display_settings": _normalized_display_settings_for_response(saved),
    }


def delete_custom_external_connection(system_id: str) -> dict[str, Any]:
    normalized_id = _slugify_system_id(system_id)
    if not normalized_id:
        raise ExternalConnectionValidationError(
            "INVALID_EXTERNAL_CONNECTION_ID",
            "External connection id is required.",
            {"system_id": "required"},
        )
    saved = _load_display_settings_raw()
    current = _normalize_custom_external_systems(saved.get("custom_external_systems"))
    next_systems = [entry for entry in current if str(entry.get("system_id") or "") != normalized_id]
    if len(next_systems) == len(current):
        raise ExternalConnectionValidationError(
            "EXTERNAL_CONNECTION_NOT_FOUND",
            "Only custom external connections can be deleted here.",
            {"system_id": "not_found"},
        )
    saved["custom_external_systems"] = next_systems
    visibility = saved.get("system_visibility")
    if isinstance(visibility, dict):
        visibility = dict(visibility)
        visibility.pop(normalized_id, None)
        saved["system_visibility"] = visibility
    _save_display_settings_raw(saved)
    return {"display_settings": _normalized_display_settings_for_response(saved)}


def _load_display_settings_raw() -> dict[str, Any]:
    with db_session() as connection:
        row = connection.execute(
            "SELECT value_json FROM settings WHERE key = ?",
            (UI_DISPLAY_SETTINGS_KEY,),
        ).fetchone()
    loaded = _json_loads(row["value_json"]) if row else {}
    return loaded if isinstance(loaded, dict) else {}


def _save_display_settings_raw(value: dict[str, Any]) -> None:
    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO settings (key, value_json, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET
                value_json = excluded.value_json,
                updated_at = excluded.updated_at
            """,
            (UI_DISPLAY_SETTINGS_KEY, _json_dumps(value)),
        )
    clear_live_probe_cache()


def _normalized_display_settings_for_response(saved: dict[str, Any]) -> dict[str, Any]:
    custom_systems = _normalize_custom_external_systems(saved.get("custom_external_systems"))
    state: dict[str, Any] = {
        "system_visibility": _normalize_system_visibility(
            saved,
            {str(system.get("system_id") or "") for system in custom_systems},
        ),
        "custom_external_systems": custom_systems,
        "show_inactive": bool(saved.get("show_inactive", False)),
        "show_internal_vless": bool(saved.get("show_internal_vless", False)),
        "hidden_subject_ids": [
            str(item).strip()
            for item in (saved.get("hidden_subject_ids") if isinstance(saved.get("hidden_subject_ids"), list) else [])
            if str(item).strip()
        ],
        "subject_traffic_preferences": (
            saved.get("subject_traffic_preferences")
            if isinstance(saved.get("subject_traffic_preferences"), dict)
            else {}
        ),
    }
    for custom_system in custom_systems:
        state["system_visibility"].setdefault(str(custom_system["system_id"]), True)
    return state


def _external_connection_response(item: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(item)
    identity = external_connection_identity(enriched)
    enriched["identity"] = identity
    enriched["external_system_id"] = identity["external_system_id"]
    enriched["requested_by"] = identity["requested_by"]
    enriched["collector"] = identity["collector"]
    enriched["api_guide"] = _external_connection_guide(enriched)
    enriched["readiness"] = _external_connection_readiness(enriched)
    return {
        "external_connection": enriched,
        "contract": enriched.get("api_guide"),
        "validation": {
            "ok": enriched["readiness"].get("state") in {"ready", "seen", "active"},
            "readiness": enriched["readiness"],
        },
    }


def _normalize_external_connection_input(
    payload: dict[str, Any],
    *,
    system_id: str | None,
    existing: dict[str, Any] | None,
    partial: bool,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ExternalConnectionValidationError(
            "INVALID_EXTERNAL_CONNECTION_PAYLOAD",
            "External connection payload must be a JSON object.",
            {"payload": "object_required"},
        )
    source = {**existing, **payload} if existing else dict(payload)
    field_errors: dict[str, str] = {}

    raw_connection_type = source.get("connection_type") or (existing or {}).get("connection_type") or "external_vpn_module"
    connection_type = str(raw_connection_type or "").strip().lower()
    if connection_type not in EXTERNAL_CONNECTION_TYPES:
        field_errors["connection_type"] = "unsupported"

    raw_label = str(source.get("label") or source.get("name") or "").strip()
    default_id = f"{_external_connection_prefix(connection_type)}-{raw_label}" if raw_label else ""
    raw_id = source.get("system_id") or source.get("id") or system_id or default_id
    normalized_id = _slugify_system_id(raw_id)
    if system_id:
        path_id = _slugify_system_id(system_id)
        if normalized_id and normalized_id != path_id:
            field_errors["system_id"] = "immutable"
        normalized_id = path_id
    if not normalized_id:
        field_errors["system_id"] = "required"
    if not raw_label:
        field_errors["label"] = "required"

    if existing:
        if "connection_type" in payload and connection_type != str(existing.get("connection_type") or ""):
            field_errors["connection_type"] = "immutable"
        if "replacement_target" in payload:
            next_replacement = _normalize_replacement_target(payload.get("replacement_target"))
            if next_replacement != str(existing.get("replacement_target") or ""):
                field_errors["replacement_target"] = "immutable"

    location = str(source.get("location") or "manual").strip().lower()
    if location not in EXTERNAL_LOCATIONS:
        field_errors["location"] = "unsupported"

    integration_mode = str(source.get("integration_mode") or "api_push").strip().lower()
    if integration_mode not in EXTERNAL_INTEGRATION_MODES:
        field_errors["integration_mode"] = "unsupported"
    refresh_mode = str(source.get("refresh_mode") or "").strip().lower()
    if integration_mode == "api_push":
        refresh_mode = "on_change"
    elif refresh_mode not in {"manual", "interval"}:
        field_errors["refresh_mode"] = "unsupported_for_integration"

    endpoints = _strict_external_endpoints(source.get("endpoints"), field_errors)
    capabilities = _strict_external_capabilities(source.get("capabilities"), field_errors)
    collector_config = _strict_external_collector_config(
        source.get("collector_config") or source.get("collector"),
        integration_mode=integration_mode,
        refresh_mode=refresh_mode,
        field_errors=field_errors,
    )

    if field_errors:
        raise ExternalConnectionValidationError(
            "INVALID_EXTERNAL_CONNECTION",
            "External connection payload failed validation.",
            field_errors,
        )

    replacement_target = _normalize_replacement_target(source.get("replacement_target") or source.get("replaces"))
    return {
        "system_id": normalized_id,
        "label": raw_label[:80],
        "kind": "external",
        "lifecycle_mode": "external",
        "connection_type": connection_type,
        "location": location,
        "address": str(source.get("address") or "").strip()[:160],
        "runtime_type": str(source.get("runtime_type") or "").strip().lower()[:80],
        "replacement_target": replacement_target,
        "capabilities": capabilities,
        "endpoints": endpoints,
        "integration_mode": integration_mode,
        "refresh_mode": refresh_mode,
        "collector_config": collector_config,
        "description": str(source.get("description") or _external_connection_description(connection_type)).strip()[:240],
        "custom": True,
    }


def _strict_external_endpoints(value: Any, field_errors: dict[str, str]) -> dict[str, str]:
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        field_errors["endpoints"] = "object_required"
        return {}
    unknown = sorted(str(key) for key in value if str(key) not in EXTERNAL_ENDPOINT_KEYS)
    if unknown:
        field_errors["endpoints"] = f"unsupported_keys:{','.join(unknown[:8])}"
    return _normalize_external_endpoints(value)


def _strict_external_capabilities(value: Any, field_errors: dict[str, str]) -> dict[str, bool]:
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        field_errors["capabilities"] = "object_required"
        return {}
    unknown = sorted(str(key) for key in value if str(key) not in EXTERNAL_CAPABILITY_KEYS)
    if unknown:
        field_errors["capabilities"] = f"unsupported_keys:{','.join(unknown[:8])}"
    return _normalize_external_capabilities(value)


def _strict_external_collector_config(
    value: Any,
    *,
    integration_mode: str,
    refresh_mode: str,
    field_errors: dict[str, str],
) -> dict[str, Any]:
    if value in (None, ""):
        source: dict[str, Any] = {}
    elif isinstance(value, dict):
        source = value
    else:
        field_errors["collector_config"] = "object_required"
        source = {}
    allowed = EXTERNAL_COLLECTOR_BASE_KEYS | EXTERNAL_COLLECTOR_MODE_KEYS.get(integration_mode, set())
    unknown = sorted(str(key) for key in source if str(key) not in allowed)
    if unknown:
        field_errors["collector_config"] = f"unsupported_keys:{','.join(unknown[:8])}"
    result = _normalize_external_collector_config(
        source,
        integration_mode=integration_mode,
        refresh_mode=refresh_mode,
    )
    if integration_mode == "http_poll" and not result.get("url"):
        field_errors["collector_config.url"] = "required"
    if integration_mode == "command_probe" and not result.get("script_id"):
        field_errors["collector_config.script_id"] = "required"
    if integration_mode == "file_read" and not result.get("path"):
        field_errors["collector_config.path"] = "required"
    return result


def external_connection_contract(system_id: str) -> dict[str, Any] | None:
    normalized = _slugify_system_id(system_id)
    if not normalized:
        return None
    system = custom_external_system_by_id(system_id)
    if system:
        item = dict(system)
    else:
        item = _builtin_external_connection_by_id(normalized)
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
            if (
                system_id in allowed_system_ids
                or system_id.startswith("external-management-")
                or system_id.startswith("external-network-")
            ):
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
    existing_ids = {str(item.get("system_id") or "") for item in systems}
    systems.extend(
        item
        for item in _external_network_source_display_systems(display_settings=display_settings)
        if str(item.get("system_id") or "") not in existing_ids
    )
    return systems


def _external_network_source_display_systems(*, display_settings: dict[str, Any]) -> list[dict[str, Any]]:
    with db_session() as connection:
        rows = connection.execute(
            """
            SELECT
                subject_type,
                COUNT(*) AS total_count,
                SUM(CASE WHEN runtime_state = 'active' THEN 1 ELSE 0 END) AS active_count,
                MAX(updated_at) AS last_seen_at
            FROM subjects
            WHERE subject_role = 'external_network_source'
            GROUP BY subject_type
            ORDER BY subject_type
            """
        ).fetchall()

    systems: list[dict[str, Any]] = []
    for row in rows:
        subject_type = str(row["subject_type"] or "").strip().lower()
        if subject_type in {"tailscale", "tailscale_node"}:
            system_id = "external-network-tailscale"
            label = "Tailscale"
            runtime_type = "tailscale"
            description = "External network source discovered from Tailscale inventory."
            location = "host"
        else:
            system_id = _slugify_system_id(f"external-network-{subject_type}")
            label = subject_type.replace("_", " ").replace("-", " ").strip().title() or "External network"
            runtime_type = subject_type
            description = "External network source discovered from subject inventory."
            location = "manual"
        if not system_id:
            continue
        count = int(row["total_count"] or 0)
        active_count = int(row["active_count"] or 0)
        if count <= 0:
            continue
        item = {
            "system_id": system_id,
            "label": label,
            "kind": "external",
            "lifecycle_mode": "external",
            "connection_type": "external_network_source",
            "location": location,
            "address": "",
            "runtime_type": runtime_type,
            "replacement_target": "",
            "capabilities": {"supports_client_inventory": True},
            "endpoints": {},
            "integration_mode": "command_probe" if runtime_type == "tailscale" else "api_push",
            "refresh_mode": "interval" if runtime_type == "tailscale" else "on_change",
            "collector_config": {
                "script_id": "tailscale_status",
                "interval_seconds": 3600,
                "timeout_seconds": 20,
                "apply_traffic": False,
            } if runtime_type == "tailscale" else _default_external_collector_config("api_push", "on_change"),
            "description": description,
            "custom": False,
            "customizable": True,
            "count": count,
            "active_count": active_count,
            "visible": _system_visible(display_settings, system_id),
            "desired_state": None,
            "runtime_state": "external",
            "apply_state": "clean",
            "installed": True,
            "manageable_actions": [],
            "last_seen_at": row["last_seen_at"],
        }
        identity = external_connection_identity(item)
        item["identity"] = identity
        item["external_system_id"] = identity["external_system_id"]
        item["requested_by"] = identity["requested_by"]
        item["collector"] = identity["collector"]
        item["api_guide"] = _external_connection_guide(item)
        item["readiness"] = {"state": "seen", "missing_fields": []}
        systems.append(item)
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
        "collection": _external_collection_guide(system),
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
            "integration_mode": system.get("integration_mode") or "api_push",
            "refresh_mode": system.get("refresh_mode") or "on_change",
            "collector_config": system.get("collector_config") or {},
        },
        "collection": _external_collection_guide(system),
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
            "integration_mode": system.get("integration_mode") or "api_push",
            "refresh_mode": system.get("refresh_mode") or "on_change",
            "collector_config": system.get("collector_config") or {},
        },
        "collection": _external_collection_guide(system),
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


def _external_collection_guide(system: dict[str, Any]) -> dict[str, Any]:
    integration_mode = _normalize_external_integration_mode(
        system.get("integration_mode"),
        str(system.get("connection_type") or ""),
    )
    refresh_mode = _normalize_external_refresh_mode(system.get("refresh_mode"), integration_mode)
    collector_config = _normalize_external_collector_config(
        system.get("collector_config"),
        integration_mode=integration_mode,
        refresh_mode=refresh_mode,
    )
    return {
        "integration_mode": integration_mode,
        "refresh_mode": refresh_mode,
        "collector_config": collector_config,
        "manual_refresh": {
            "method": "POST",
            "path": f"/ui/external-connections/{system.get('system_id')}/collect",
            "body": {"dry_run": True},
        },
        "accepted_payload": {
            "status": "ok|ready|running|degraded|down",
            "details": {},
            "clients": [
                {
                    "id": "<stable-client-id>",
                    "label": "<display-name>",
                    "address": "<ip-or-cidr>",
                    "metadata": {},
                }
            ],
            "traffic_samples": [
                {
                    "counter_key": "<stable-counter>",
                    "subject_id": "<existing-fwrouter-subject-id>",
                    "path": "vpn|direct",
                    "rx_bytes": 0,
                    "tx_bytes": 0,
                    "metadata": {},
                }
            ],
        },
        "notes": [
            "api_push does not poll; the external system sends updates when its state changes.",
            "manual refresh runs only when called from UI/API.",
            "interval refresh is optional and should be used only when the external system cannot push changes.",
        ],
    }


def _external_connection_readiness(system: dict[str, Any]) -> dict[str, Any]:
    connection_type = str(system.get("connection_type") or "")
    missing: list[str] = []
    details: dict[str, Any] = {}
    integration_mode = _normalize_external_integration_mode(system.get("integration_mode"), connection_type)
    refresh_mode = _normalize_external_refresh_mode(system.get("refresh_mode"), integration_mode)
    collector_config = _normalize_external_collector_config(
        system.get("collector_config"),
        integration_mode=integration_mode,
        refresh_mode=refresh_mode,
    )
    details["integration_mode"] = integration_mode
    details["refresh_mode"] = refresh_mode
    if not str(system.get("label") or "").strip():
        missing.append("label")
    if connection_type in {"external_vpn_module", "external_network_source"} and not str(system.get("runtime_type") or "").strip():
        missing.append("runtime_type")
    if not str(system.get("location") or "").strip():
        missing.append("location")
    if integration_mode == "http_poll" and not collector_config.get("url"):
        missing.append("collector_url")
    if integration_mode == "command_probe" and not collector_config.get("script_id"):
        missing.append("collector_script_id")
    if integration_mode == "file_read" and not collector_config.get("path"):
        missing.append("collector_path")
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
            from fwrouter_api.services.runtime_adapters import active_runtime_adapter_for_replacement_target

            runtime_adapter = active_runtime_adapter_for_replacement_target(replacement_target)
        except Exception:
            runtime_adapter = None
        adapter_source = runtime_adapter.get("source") if isinstance(runtime_adapter, dict) else {}
        adapter_source = adapter_source if isinstance(adapter_source, dict) else {}
        active_system_id = str(adapter_source.get("system_id") or "")
        active_as_runtime_adapter = bool(active_system_id and active_system_id == str(system.get("system_id") or ""))
        details["active_as_runtime_adapter"] = active_as_runtime_adapter
        details["runtime_adapter_role"] = (runtime_adapter or {}).get("role") if isinstance(runtime_adapter, dict) else None
        if active_as_runtime_adapter:
            details["active_adapter"] = {
                "role": (runtime_adapter or {}).get("role"),
                "adapter_id": (runtime_adapter or {}).get("adapter_id"),
                "system_id": active_system_id,
                "runtime_type": adapter_source.get("runtime_type"),
                "redir_port": adapter_source.get("redir_port"),
                "tproxy_port": adapter_source.get("tproxy_port"),
            }
    if connection_type == "external_network_source":
        endpoints = system.get("endpoints") if isinstance(system.get("endpoints"), dict) else {}
        discovered_count = int(system.get("count") or 0)
        builtin_discovered = not bool(system.get("custom")) and discovered_count > 0
        if (
            not builtin_discovered
            and not (endpoints.get("client_inventory_url") or endpoints.get("interface_name") or endpoints.get("client_cidr"))
        ):
            missing.append("client_source")
    return {
        "state": "active" if details.get("active_as_runtime_adapter") else ("ready" if not missing else "incomplete"),
        "missing_fields": missing,
        "details": details,
    }
