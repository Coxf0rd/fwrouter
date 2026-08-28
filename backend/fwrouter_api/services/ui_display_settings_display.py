from __future__ import annotations

import json
from typing import Any

from fwrouter_api.db.connection import db_session
from fwrouter_api.services.modules import fetch_modules
from fwrouter_api.services.subject_taxonomy import external_network_source_display_contract
from fwrouter_api.services.ui_display_settings_common import (
    UI_DISPLAY_SYSTEMS,
    _default_external_collector_config,
    _external_connection_description,
    _json_loads,
    _slugify_system_id,
    external_connection_identity,
)
from fwrouter_api.services.ui_display_settings_guides import (
    _external_connection_guide,
    _external_connection_readiness,
    _external_management_label,
)
from fwrouter_api.services.ui_display_settings_store import (
    _load_display_settings_raw,
    _system_visible,
)
from fwrouter_api.services.ui_text import _ui_text_title


def _builtin_external_connection_by_id(
    system_id: str,
    display_settings: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    normalized = _slugify_system_id(system_id)
    if not normalized:
        return None
    settings = (
        display_settings
        if isinstance(display_settings, dict)
        else _load_display_settings_raw()
    )
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


def _display_system_has_data(
    item: dict[str, Any],
    module: dict[str, Any] | None,
    count: int,
) -> bool:
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
        label_key = str(item.pop("label_key", "") or item.get("system_id") or "").strip()
        description_key = str(item.pop("description_key", "") or item.get("system_id") or "").strip()
        item["label"] = (
            _ui_text_title("display.system.title", label_key)
            or label_key
            or str(item.get("system_id") or "")
        )
        item["description"] = _ui_text_title("display.system.description", description_key) or ""
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
                item["kind"] = (
                    module_lifecycle_mode
                    if module_lifecycle_mode in {"managed", "external"}
                    else base_kind
                )
            item["desired_state"] = module.get("desired_state")
            item["runtime_state"] = module.get("runtime_state")
            item["apply_state"] = module.get("apply_state")
            item["status_text"] = module.get("status_text")
            item["installed"] = module.get("installed")
            item["manageable_actions"] = module.get("manageable_actions") or []
        item["count"] = count
        item["visible"] = _system_visible(display_settings, str(item["system_id"]))
        systems.append(item)

    from fwrouter_api.services.external_connections_registry import list_external_connections

    for custom in list_external_connections():
        item = dict(custom)
        identity = external_connection_identity(item)
        item["identity"] = identity
        item["external_system_id"] = identity["external_system_id"]
        item["requested_by"] = identity["requested_by"]
        item["collector"] = identity["collector"]
        item["count"] = 0
        item["visible"] = _system_visible(display_settings, str(item["connection_id"]))
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
        and bool(item.get("custom"))
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
        contract = external_network_source_display_contract(subject_type)
        if contract:
            system_id = str(contract["system_id"])
            label = str(contract["label"])
            runtime_type = str(contract["runtime_type"])
            description = str(contract["description"])
            location = str(contract["location"])
            integration_mode = str(contract["integration_mode"])
            refresh_mode = str(contract["refresh_mode"])
            collector_config = dict(contract["collector_config"])
        else:
            system_id = _slugify_system_id(f"external-network-{subject_type}")
            label = subject_type.replace("_", " ").replace("-", " ").strip().title() or "External network"
            runtime_type = subject_type
            description = _ui_text_title("display.system.description", "external_network_discovered") or ""
            location = "manual"
            integration_mode = "api_push"
            refresh_mode = "on_change"
            collector_config = _default_external_collector_config("api_push", "on_change")
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
            "integration_mode": integration_mode,
            "refresh_mode": refresh_mode,
            "collector_config": collector_config,
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
