from __future__ import annotations

import json
from typing import Any

from fwrouter_api.db.connection import db_session
from fwrouter_api.services.custom_servers import (
    resolve_mihomo_runtime_proxy_rows,
    resolve_runtime_proxy_rows,
)
from fwrouter_api.services.mihomo_config_inbounds import _normalize_proxy_list
from fwrouter_api.services.mihomo_config_rules import _load_subject_server_override_routes


LOGICAL_PROFILE_GROUP_INTERVAL_SECONDS = 300


def _logical_profile_members(raw: dict[str, Any], *, logical_runtime_name: str) -> list[dict[str, Any]]:
    """Expand a persisted JSON profile into private, concrete Mihomo proxies.

    A JSON subscription profile is one user-visible server, but its topology
    records several VLESS outbounds.  The profile runtime name is deliberately
    reserved for the group; assigning it to the first outbound silently turns
    a structured profile into an arbitrary concrete node.
    """

    topology = raw.get("_fwrouter_topology") if isinstance(raw, dict) else None
    endpoints = topology.get("endpoints") if isinstance(topology, dict) else None
    if not isinstance(endpoints, list):
        return []

    members: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for index, endpoint in enumerate(endpoints, start=1):
        if not isinstance(endpoint, dict):
            continue
        runtime = endpoint.get("runtime")
        if not isinstance(runtime, dict):
            continue
        identity = str(endpoint.get("identity") or "").removeprefix("sub:")
        suffix = identity[:12] or str(index)
        member_name = f"{logical_runtime_name} :: {suffix}"
        if member_name in seen_names:
            continue
        member = dict(runtime)
        member["name"] = member_name
        member["_fwrouter_logical_runtime_name"] = logical_runtime_name
        member["_fwrouter_logical_member_identity"] = str(endpoint.get("identity") or suffix)
        if not str(member.get("type") or "").strip() or not str(member.get("server") or "").strip():
            continue
        members.append(member)
        seen_names.add(member_name)
    return members


def _logical_profile_groups() -> list[dict[str, Any]]:
    """Return one fallback group per structured subscription profile.

    The source gives a single logical profile identity and an explicit member
    set.  Mihomo has no equivalent for Xray's ``leastLoad`` balancer, so the
    safe common behavior is failover within that exact set; it never mixes
    members between profiles or exposes them to vpn-auto directly.
    """

    rows = resolve_mihomo_runtime_proxy_rows(inventory_state="active", limit=1000)
    groups: list[dict[str, Any]] = []
    for row in rows:
        raw = row.get("raw") if isinstance(row, dict) else None
        if not isinstance(raw, dict):
            continue
        topology = raw.get("_fwrouter_topology")
        if not isinstance(topology, dict) or topology.get("kind") != "logical_profile":
            continue
        runtime_name = str(raw.get("_fwrouter_runtime_name") or raw.get("name") or "").strip()
        if not runtime_name:
            continue
        members = _logical_profile_members(raw, logical_runtime_name=runtime_name)
        if not members:
            continue
        groups.append(
            {
                "name": runtime_name,
                "type": "fallback",
                "proxies": [str(member["name"]) for member in members],
                "url": "https://www.gstatic.com/generate_204",
                "interval": LOGICAL_PROFILE_GROUP_INTERVAL_SECONDS,
            }
        )
    return groups


def _runtime_proxy_inventory_count() -> int:
    rows = resolve_mihomo_runtime_proxy_rows(inventory_state="active", limit=1000)
    return sum(
        1
        for row in rows
        if isinstance(row, dict)
        and isinstance(row.get("raw"), dict)
        and str((row.get("raw") or {}).get("name") or "").strip()
    )


def _merge_runtime_proxies(
    base_config: dict[str, Any],
    *,
    required_last_good_names: set[str] | None = None,
    required_last_good_server_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    runtime_proxy_rows = resolve_mihomo_runtime_proxy_rows(inventory_state="active", limit=1000)
    active_logical_group_names = {
        str(group["name"])
        for group in _logical_profile_groups()
    }
    runtime_proxies: list[dict[str, Any]] = []
    seen_names: set[str] = set()

    for item in runtime_proxy_rows:
        if not isinstance(item, dict) or not isinstance(item.get("raw"), dict):
            continue
        raw = dict(item["raw"])
        logical_runtime_name = str(raw.get("_fwrouter_runtime_name") or raw.get("name") or "").strip()
        topology = raw.get("_fwrouter_topology")
        candidates = (
            _logical_profile_members(raw, logical_runtime_name=logical_runtime_name)
            if isinstance(topology, dict) and topology.get("kind") == "logical_profile"
            else [raw]
        )
        for candidate in candidates:
            proxy = _normalize_proxy_list({"proxies": [candidate]}).get("proxies")[0]
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

    # A previous refresh can already have removed the proxy from the active
    # Mihomo config while Xray still has its applied binding. Its persistent
    # raw definition is enough to retain that one last-good dataplane target;
    # this does not reactivate the server in inventory or selectors.
    server_ids = sorted(server_id for server_id in (required_last_good_server_ids or set()) if server_id)
    if server_ids:
        placeholders = ", ".join("?" for _ in server_ids)
        with db_session() as connection:
            rows = connection.execute(
                f"SELECT server_id, raw_json FROM servers WHERE server_id IN ({placeholders})",
                tuple(server_ids),
            ).fetchall()
        for row in rows:
            try:
                raw = json.loads(row["raw_json"] or "{}")
            except json.JSONDecodeError:
                continue
            if not isinstance(raw, dict):
                continue
            proxy = _normalize_proxy_list({"proxies": [dict(raw)]}).get("proxies")[0]
            name = str(proxy.get("name") or "").strip()
            proxy_type = str(proxy.get("type") or "").strip().lower()
            if (
                not name
                or name in seen_names
                or name in active_logical_group_names
                or name not in (required_last_good_names or set())
                or not proxy_type
                or (proxy_type != "http" and not str(proxy.get("server") or "").strip())
            ):
                continue
            runtime_proxies.append(proxy)
            seen_names.add(name)

    # Xray can still be using an applied handoff while subscription inventory is
    # being prepared. Keep only the corresponding definitions from the active
    # Mihomo config so that its candidate remains a valid downstream for that
    # last-good Xray runtime. The caller performs a final reconcile after Xray
    # convergence, which prunes definitions that are no longer required.
    for item in base_config.get("proxies") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if (
            not name
            or name in seen_names
            or name in active_logical_group_names
            or name not in (required_last_good_names or set())
        ):
            continue
        proxy = _normalize_proxy_list({"proxies": [dict(item)]}).get("proxies")[0]
        proxy_type = str(proxy.get("type") or "").strip().lower()
        if not proxy_type or (proxy_type != "http" and not str(proxy.get("server") or "").strip()):
            continue
        runtime_proxies.append(proxy)
        seen_names.add(name)

    return runtime_proxies


def _load_vpn_auto_proxy_names() -> list[str]:
    with db_session() as connection:
        rows = connection.execute(
            """
            SELECT COALESCE(
                json_extract(s.raw_json, '$._fwrouter_runtime_name'),
                json_extract(s.raw_json, '$.name'),
                s.server_name
            ) AS runtime_name
            FROM servers AS s
            JOIN server_preferences AS p ON p.server_id = s.server_id
            WHERE s.inventory_state = 'active'
              AND COALESCE(p.vpn_auto, 0) = 1
              AND COALESCE(p.manually_deleted_at, '') = ''
            ORDER BY s.server_name, s.server_id
            """
        ).fetchall()
    return [str(row["runtime_name"]) for row in rows if str(row["runtime_name"] or "").strip()]


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

    logical_profile_groups = _logical_profile_groups()
    logical_group_names = {str(group["name"]) for group in logical_profile_groups}
    for group in logical_profile_groups:
        groups_by_name[str(group["name"])] = group

    proxy_names = [
        str(proxy.get("name"))
        for proxy in (base_config.get("proxies") or [])
        if isinstance(proxy, dict) and str(proxy.get("name") or "").strip()
    ]
    proxy_name_set = set(proxy_names) | logical_group_names
    vpn_auto_names = [
        name
        for name in _load_vpn_auto_proxy_names()
        if name in proxy_name_set
    ]
    global_list_proxy_names = [
        str(
            (row.get("raw") or {}).get("_fwrouter_runtime_name")
            or (row.get("raw") or {}).get("name")
            or row.get("server_name")
            or ""
        ).strip()
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
