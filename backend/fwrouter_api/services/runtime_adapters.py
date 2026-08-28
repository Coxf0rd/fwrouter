from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fwrouter_api.adapters.mihomo import DEFAULT_MIHOMO_ADAPTER
from fwrouter_api.db.connection import db_session
from fwrouter_api.services.external_vpn import (
    active_external_vpn_module_for_replacement_target,
    build_external_vpn_contour,
)
from fwrouter_api.services.live_probe_cache import get_live_probe_cache
from fwrouter_api.services.modules import get_module_state


UI_DISPLAY_SETTINGS_KEY = "ui.admin_client_display.v1"
RUNTIME_ROLE_VPN_DATAPLANE = "vpn_dataplane"
RUNTIME_ROLE_EXPLICIT_CLIENT = "explicit_client_runtime"
RUNTIME_CAPABILITY_HEALTH = "health"
RUNTIME_CAPABILITY_LIST_SERVERS = "list_servers"
RUNTIME_CAPABILITY_APPLY_SERVER = "apply_server"
RUNTIME_CAPABILITY_APPLY_SELECTOR = "apply_selector"
RUNTIME_CAPABILITY_TRANSPARENT_PROXY = "transparent_proxy"
RUNTIME_CAPABILITY_EXPLICIT_CLIENTS = "explicit_clients"


@dataclass(frozen=True)
class RuntimeAdapterRegistration:
    role: str
    adapter_id: str
    capabilities: frozenset[str]
    priority: int
    replacement_targets: frozenset[str]
    resolver: Callable[[], dict[str, Any] | None]
    operations_factory: Callable[[dict[str, Any]], Any | None] | None = None


_RUNTIME_ADAPTER_REGISTRY: list[RuntimeAdapterRegistration] = []


def _normalize_token(value: Any) -> str:
    return str(value or "").strip().lower()


def register_runtime_adapter(registration: RuntimeAdapterRegistration) -> None:
    role = _normalize_token(registration.role)
    adapter_id = _normalize_token(registration.adapter_id)
    if not role or not adapter_id:
        raise ValueError("Runtime adapter registration requires role and adapter_id.")
    _RUNTIME_ADAPTER_REGISTRY[:] = [
        item
        for item in _RUNTIME_ADAPTER_REGISTRY
        if not (item.role == role and item.adapter_id == adapter_id)
    ]
    _RUNTIME_ADAPTER_REGISTRY.append(
        RuntimeAdapterRegistration(
            role=role,
            adapter_id=adapter_id,
            capabilities=frozenset(_normalize_token(item) for item in registration.capabilities if item),
            priority=int(registration.priority),
            replacement_targets=frozenset(
                _normalize_token(item) for item in registration.replacement_targets if item
            ),
            resolver=registration.resolver,
            operations_factory=registration.operations_factory,
        )
    )


def registered_runtime_adapters(role: str | None = None) -> list[RuntimeAdapterRegistration]:
    normalized_role = _normalize_token(role)
    registrations = [
        item
        for item in _RUNTIME_ADAPTER_REGISTRY
        if not normalized_role or item.role == normalized_role
    ]
    return sorted(registrations, key=lambda item: (-item.priority, item.adapter_id))


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


def _managed_mihomo_vpn_dataplane_adapter() -> dict[str, Any] | None:
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


def _external_vpn_module_adapter_for_target(replacement_target: str) -> dict[str, Any] | None:
    module = active_external_vpn_module_for_replacement_target(replacement_target)
    if module is None:
        return None
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


def _external_vpn_dataplane_adapter() -> dict[str, Any] | None:
    return _external_vpn_module_adapter_for_target("mihomo")


def _external_explicit_client_runtime_adapter() -> dict[str, Any] | None:
    module = active_external_explicit_client_runtime()
    if module is None:
        return None
    return _runtime_adapter(
        role=RUNTIME_ROLE_EXPLICIT_CLIENT,
        adapter_id="external_explicit_client_runtime",
        lifecycle_mode="external",
        ready=True,
        source={
            "kind": "external",
            "connection_id": module["connection_id"],
            "system_id": module["system_id"],
            "label": module["label"],
            "runtime_type": module["runtime_type"],
            "location": module["location"],
            "address": module["address"],
        },
        contour=None,
        reason="external_explicit_client_runtime_configured",
    )


def _managed_xray_explicit_client_runtime_adapter() -> dict[str, Any] | None:
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


def active_runtime_adapter(role: str) -> dict[str, Any]:
    normalized_role = _normalize_token(role)
    for registration in registered_runtime_adapters(normalized_role):
        adapter = registration.resolver()
        if adapter is None:
            continue
        resolved = dict(adapter)
        resolved["role"] = normalized_role
        resolved["adapter_id"] = registration.adapter_id
        resolved["capabilities"] = sorted(registration.capabilities)
        resolved["registry_priority"] = registration.priority
        return resolved
    return _runtime_adapter(
        role=normalized_role,
        adapter_id="none",
        lifecycle_mode="none",
        ready=False,
        source={"kind": "none"},
        reason="runtime_adapter_not_registered",
    )


def runtime_adapter_operations(adapter: dict[str, Any]) -> Any | None:
    role = _normalize_token(adapter.get("role"))
    adapter_id = _normalize_token(adapter.get("adapter_id"))
    for registration in registered_runtime_adapters(role):
        if registration.adapter_id != adapter_id:
            continue
        if registration.operations_factory is None:
            return None
        return registration.operations_factory(adapter)
    return None


def runtime_role_for_replacement_target(target: str) -> str | None:
    normalized = _normalize_token(target)
    if not normalized:
        return None
    for registration in registered_runtime_adapters():
        if normalized in registration.replacement_targets:
            return registration.role
    return None


def active_vpn_dataplane_adapter() -> dict[str, Any]:
    return active_runtime_adapter(RUNTIME_ROLE_VPN_DATAPLANE)


def _active_external_explicit_client_runtime_uncached() -> dict[str, Any] | None:
    from fwrouter_api.services.external_connections_registry import list_external_connections

    settings = _load_display_settings()
    for item in list_external_connections(enabled_only=True):
        if not isinstance(item, dict):
            continue
        if str(item.get("connection_type") or "").strip().lower() != "external_vpn_module":
            continue
        if str(item.get("replacement_target") or "").strip().lower() != "xray":
            continue
        connection_id = str(item.get("connection_id") or "").strip()
        if not connection_id:
            continue
        system_id = _slugify_system_id(item.get("system_id") or item.get("connection_id"))
        connection_id = _slugify_system_id(item.get("connection_id"))
        if not connection_id or not _visible(settings, connection_id):
            continue
        endpoints = item.get("endpoints") if isinstance(item.get("endpoints"), dict) else {}
        if not (endpoints.get("controller_url") or endpoints.get("healthcheck_url")):
            continue
        return {
            "connection_id": connection_id,
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
    return active_runtime_adapter(RUNTIME_ROLE_EXPLICIT_CLIENT)


def active_runtime_adapter_for_replacement_target(target: str) -> dict[str, Any] | None:
    role = runtime_role_for_replacement_target(target)
    return active_runtime_adapter(role) if role else None


def _managed_mihomo_operations(adapter: dict[str, Any]) -> Any:
    return DEFAULT_MIHOMO_ADAPTER


def _no_runtime_operations(adapter: dict[str, Any]) -> None:
    return None


register_runtime_adapter(
    RuntimeAdapterRegistration(
        role=RUNTIME_ROLE_VPN_DATAPLANE,
        adapter_id="external_vpn_module",
        capabilities=frozenset({RUNTIME_CAPABILITY_TRANSPARENT_PROXY}),
        priority=100,
        replacement_targets=frozenset({"mihomo"}),
        resolver=_external_vpn_dataplane_adapter,
        operations_factory=_no_runtime_operations,
    )
)
register_runtime_adapter(
    RuntimeAdapterRegistration(
        role=RUNTIME_ROLE_VPN_DATAPLANE,
        adapter_id="mihomo",
        capabilities=frozenset(
            {
                RUNTIME_CAPABILITY_HEALTH,
                RUNTIME_CAPABILITY_LIST_SERVERS,
                RUNTIME_CAPABILITY_APPLY_SERVER,
                RUNTIME_CAPABILITY_APPLY_SELECTOR,
                RUNTIME_CAPABILITY_TRANSPARENT_PROXY,
            }
        ),
        priority=0,
        replacement_targets=frozenset({"mihomo"}),
        resolver=_managed_mihomo_vpn_dataplane_adapter,
        operations_factory=_managed_mihomo_operations,
    )
)
register_runtime_adapter(
    RuntimeAdapterRegistration(
        role=RUNTIME_ROLE_EXPLICIT_CLIENT,
        adapter_id="external_explicit_client_runtime",
        capabilities=frozenset({RUNTIME_CAPABILITY_EXPLICIT_CLIENTS}),
        priority=100,
        replacement_targets=frozenset({"xray"}),
        resolver=_external_explicit_client_runtime_adapter,
        operations_factory=_no_runtime_operations,
    )
)
register_runtime_adapter(
    RuntimeAdapterRegistration(
        role=RUNTIME_ROLE_EXPLICIT_CLIENT,
        adapter_id="xray",
        capabilities=frozenset({RUNTIME_CAPABILITY_EXPLICIT_CLIENTS}),
        priority=0,
        replacement_targets=frozenset({"xray"}),
        resolver=_managed_xray_explicit_client_runtime_adapter,
        operations_factory=_no_runtime_operations,
    )
)
