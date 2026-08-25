from __future__ import annotations

from typing import Any

from fwrouter_api.db.connection import db_session
from fwrouter_api.services.custom_servers import (
    resolve_mihomo_runtime_proxy_rows,
    resolve_runtime_proxy_rows,
)
from fwrouter_api.services.mihomo_config_inbounds import _normalize_proxy_list
from fwrouter_api.services.mihomo_config_rules import _load_subject_server_override_routes


def _runtime_proxy_inventory_count() -> int:
    rows = resolve_mihomo_runtime_proxy_rows(inventory_state="active", limit=1000)
    return sum(
        1
        for row in rows
        if isinstance(row, dict)
        and isinstance(row.get("raw"), dict)
        and str((row.get("raw") or {}).get("name") or "").strip()
    )


def _merge_runtime_proxies(base_config: dict[str, Any]) -> list[dict[str, Any]]:
    runtime_proxy_rows = resolve_mihomo_runtime_proxy_rows(inventory_state="active", limit=1000)
    runtime_proxies: list[dict[str, Any]] = []
    seen_names: set[str] = set()

    for item in runtime_proxy_rows:
        if not isinstance(item, dict) or not isinstance(item.get("raw"), dict):
            continue
        proxy = _normalize_proxy_list({"proxies": [dict(item["raw"])]}).get("proxies")[0]
        name = str(proxy.get("name") or "").strip()
        proxy_type = str(proxy.get("type") or "").strip().lower()
        if not name or not proxy_type:
            continue
        if proxy_type != "http" and not str(proxy.get("server") or "").strip():
            continue
        if name in seen_names:
            continue
        runtime_proxies.append(proxy)
        seen_names.add(name)

    return runtime_proxies


def _load_vpn_auto_proxy_names() -> list[str]:
    with db_session() as connection:
        rows = connection.execute(
            """
            SELECT s.server_name
            FROM servers AS s
            JOIN server_preferences AS p ON p.server_id = s.server_id
            WHERE s.inventory_state = 'active'
              AND COALESCE(p.vpn_auto, 0) = 1
              AND COALESCE(p.manually_deleted_at, '') = ''
            ORDER BY s.server_name, s.server_id
            """
        ).fetchall()
    return [str(row["server_name"]) for row in rows if str(row["server_name"] or "").strip()]


def _load_custom_proxy_names() -> set[str]:
    with db_session() as connection:
        rows = connection.execute(
            """
            SELECT s.server_name
            FROM servers AS s
            JOIN server_custom_https_proxy AS c ON c.server_id = s.server_id
            WHERE s.inventory_state = 'active'
            """
        ).fetchall()
    return {str(row["server_name"]) for row in rows if str(row["server_name"] or "").strip()}


def _ensure_selector_groups(base_config: dict[str, Any]) -> list[dict[str, Any]]:
    existing_groups = base_config.get("proxy-groups") if isinstance(base_config.get("proxy-groups"), list) else []
    groups_by_name: dict[str, dict[str, Any]] = {}
    for group in existing_groups:
        if not isinstance(group, dict):
            continue
        name = str(group.get("name") or "").strip()
        if not name:
            continue
        groups_by_name[name] = dict(group)

    proxy_names = [
        str(proxy.get("name"))
        for proxy in (base_config.get("proxies") or [])
        if isinstance(proxy, dict) and str(proxy.get("name") or "").strip()
    ]
    proxy_name_set = set(proxy_names)
    vpn_auto_names = [
        name
        for name in _load_vpn_auto_proxy_names()
        if name in proxy_name_set
    ]
    global_list_proxy_names = [
        str((row.get("raw") or {}).get("name") or row.get("server_name") or "").strip()
        for row in resolve_runtime_proxy_rows(inventory_state="active", global_list=True, limit=1000)
        if isinstance(row, dict)
    ]
    global_list_proxy_names = [
        name
        for name in global_list_proxy_names
        if name and name in proxy_name_set
    ]

    groups_by_name["vpn-auto"] = {
        "name": "vpn-auto",
        "type": "select",
        "proxies": [*vpn_auto_names, "DIRECT"],
    }

    vpn_global_proxies = ["vpn-auto"]
    for name in global_list_proxy_names:
        if name not in vpn_global_proxies:
            vpn_global_proxies.append(name)
    vpn_global_proxies.append("DIRECT")
    groups_by_name["vpn-global"] = {
        "name": "vpn-global",
        "type": "select",
        "proxies": vpn_global_proxies,
    }

    subject_selector_targets = ["vpn-global", "vpn-auto"]
    for name in global_list_proxy_names:
        if name not in subject_selector_targets:
            subject_selector_targets.append(name)
    subject_selector_targets.append("DIRECT")
    for route in _load_subject_server_override_routes():
        selector_name = str(route.get("selector_name") or "").strip()
        if not selector_name:
            continue
        selected_server_name = str(route.get("server_name") or "").strip()
        proxies = list(subject_selector_targets)
        if selected_server_name and selected_server_name not in proxies:
            proxies.insert(0, selected_server_name)
        groups_by_name[selector_name] = {
            "name": selector_name,
            "type": "select",
            "proxies": proxies,
        }

    ordered_names = ["vpn-auto", "vpn-global"]
    ordered_names.extend(
        name for name in groups_by_name.keys()
        if name not in {"vpn-auto", "vpn-global"}
    )
    return [groups_by_name[name] for name in ordered_names]
