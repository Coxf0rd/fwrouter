from __future__ import annotations

import json
import sqlite3
from typing import Any

from fwrouter_api.db.connection import db_session
from fwrouter_api.services.external_vpn import (
    active_external_vpn_module,
    build_external_vpn_contour,
)
from fwrouter_api.services.live_probe_cache import get_live_probe_cache
from fwrouter_api.services.modules import get_module_state


UI_DISPLAY_SETTINGS_KEY = "ui.admin_client_display.v1"
RUNTIME_ROLE_VPN_DATAPLANE = "vpn_dataplane"
RUNTIME_ROLE_EXPLICIT_CLIENT = "explicit_client_runtime"


def _json_loads(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        loaded = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _load_display_settings() -> dict[str, Any]:
    try:
        with db_session() as connection:
            row = connection.execute(
                "SELECT value_json FROM settings WHERE key = ?",
                (UI_DISPLAY_SETTINGS_KEY,),
            ).fetchone()
    except sqlite3.OperationalError:
        return {}
    return _json_loads(row["value_json"]) if row else {}


def _slugify_system_id(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    result: list[str] = []
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


def _visible(settings: dict[str, Any], system_id: str) -> bool:
    visibility = settings.get("system_visibility")
    if isinstance(visibility, dict) and system_id in visibility:
        return bool(visibility[system_id])
    return True


def _module_state(module_name: str) -> dict[str, Any] | None:
    try:
        return get_module_state(module_name)
    except sqlite3.OperationalError:
        return None


def _runtime_adapter(
    *,
    role: str,
    adapter_id: str,
    lifecycle_mode: str,
    ready: bool,
    source: dict[str, Any],
    contour: dict[str, Any] | None = None,
    reason: str = "",
) -> dict[str, Any]:
    return {
        "role": role,
        "adapter_id": adapter_id,
        "lifecycle_mode": lifecycle_mode,
        "ready": bool(ready),
        "source": source,
        "contour": contour,
        "reason": reason,
    }


def active_vpn_dataplane_adapter() -> dict[str, Any]:
    module = active_external_vpn_module()
    if module is not None:
        contour = build_external_vpn_contour(module)
        return _runtime_adapter(
            role=RUNTIME_ROLE_VPN_DATAPLANE,
            adapter_id="external_vpn_module",
            lifecycle_mode="external",
            ready=True,
            source=contour,
            contour=contour,
            reason="external_vpn_module_ready",
        )

    vpn_module = _module_state("vpn")
    return _runtime_adapter(
        role=RUNTIME_ROLE_VPN_DATAPLANE,
        adapter_id="mihomo",
        lifecycle_mode=str((vpn_module or {}).get("lifecycle_mode") or "managed"),
        ready=bool(vpn_module and vpn_module.get("desired_state") == "enabled"),
        source={"kind": "managed", "module": "mihomo"},
        contour=None,
        reason="managed_mihomo_default",
    )


def _active_external_explicit_client_runtime_uncached() -> dict[str, Any] | None:
    settings = _load_display_settings()
    systems = settings.get("custom_external_systems")
    if not isinstance(systems, list):
        return None
    for item in systems:
        if not isinstance(item, dict):
            continue
        if str(item.get("connection_type") or "").strip().lower() != "external_vpn_module":
            continue
        if str(item.get("replacement_target") or "").strip().lower() != "xray":
            continue
        system_id = _slugify_system_id(item.get("system_id") or item.get("label"))
        if not system_id or not _visible(settings, system_id):
            continue
        endpoints = item.get("endpoints") if isinstance(item.get("endpoints"), dict) else {}
        if not (endpoints.get("controller_url") or endpoints.get("healthcheck_url")):
            continue
        return {
            "system_id": system_id,
            "label": str(item.get("label") or system_id).strip(),
            "runtime_type": str(item.get("runtime_type") or "generic").strip(),
            "location": str(item.get("location") or "manual").strip(),
            "address": str(item.get("address") or "").strip(),
            "endpoints": dict(endpoints),
        }
    return None


def active_external_explicit_client_runtime() -> dict[str, Any] | None:
    return get_live_probe_cache(
        "runtime_adapters.external_explicit_client_runtime",
        ttl_seconds=2.0,
        loader=_active_external_explicit_client_runtime_uncached,
    )


def active_explicit_client_runtime_adapter() -> dict[str, Any]:
    module = active_external_explicit_client_runtime()
    if module is not None:
        return _runtime_adapter(
            role=RUNTIME_ROLE_EXPLICIT_CLIENT,
            adapter_id="external_explicit_client_runtime",
            lifecycle_mode="external",
            ready=True,
            source={
                "kind": "external",
                "system_id": module["system_id"],
                "label": module["label"],
                "runtime_type": module["runtime_type"],
                "location": module["location"],
                "address": module["address"],
            },
            contour=None,
            reason="external_explicit_client_runtime_configured",
        )

    xray_module = _module_state("xray")
    return _runtime_adapter(
        role=RUNTIME_ROLE_EXPLICIT_CLIENT,
        adapter_id="xray",
        lifecycle_mode=str((xray_module or {}).get("lifecycle_mode") or "managed"),
        ready=bool(
            xray_module
            and xray_module.get("desired_state") == "enabled"
            and xray_module.get("runtime_state") in {"running", "degraded"}
        ),
        source={"kind": "managed", "module": "xray"},
        contour=None,
        reason="managed_xray_default",
    )


def active_runtime_adapter_for_replacement_target(target: str) -> dict[str, Any] | None:
    normalized = str(target or "").strip().lower()
    if normalized == "mihomo":
        return active_vpn_dataplane_adapter()
    if normalized == "xray":
        return active_explicit_client_runtime_adapter()
    return None
