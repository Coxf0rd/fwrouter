from __future__ import annotations

import json
from typing import Any

from fwrouter_api.db.connection import db_session
from fwrouter_api.services.custom_servers import (
    resolve_mihomo_runtime_proxy_rows,
    resolve_runtime_proxy_rows,
)
from fwrouter_api.services.auto_eligibility import auto_eligible_sql
from fwrouter_api.services.mihomo_config_inbounds import _normalize_proxy_list
from fwrouter_api.services.mihomo_config_rules import _load_subject_server_override_routes
from fwrouter_api.adapters.mihomo import NON_SERVER_PROXY_TARGETS


LOGICAL_PROFILE_GROUP_INTERVAL_SECONDS = 300


def _project_native_proxy(proxy: dict[str, Any]) -> dict[str, Any]:
    """Project FWRouter capability metadata into native Mihomo fields."""
    native = dict(proxy)
    if str(native.get("type") or "").strip().lower() == "vless":
        from fwrouter_api.adapters.protocols.vless import normalize_vless_udp_capability

        capability, attempt_udp = normalize_vless_udp_capability(native)
        if capability == "invalid":
            raise ValueError("invalid canonical VLESS UDP capability")
        native["_fwrouter_udp_capability"] = capability
        native["udp"] = attempt_udp
    # Internal provenance must never become unknown Mihomo schema fields.
    return {key: value for key, value in native.items() if not key.startswith("_fwrouter")}


def _logical_profile_members(raw: dict[str, Any], *, logical_runtime_name: str) -> list[dict[str, Any]]:
    logical_server_id = str(raw.get("_fwrouter_server_id") or "").strip()
    if logical_server_id:
        with db_session() as connection:
            rows = connection.execute(
                "SELECT member_id, member_runtime_name, member_config_json FROM logical_server_members WHERE logical_server_id = ? AND is_active = 1 ORDER BY member_order, member_id",
                (logical_server_id,),
            ).fetchall()
        relational_members: list[dict[str, Any]] = []
        for row in rows:
            try:
                member = json.loads(row["member_config_json"])
            except json.JSONDecodeError:
                continue
            if not isinstance(member, dict):
                continue
            member["name"] = row["member_runtime_name"]
            relational_members.append(member)
        if relational_members:
            return relational_members
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


def _logical_profile_groups(*, explicit_server_ids: set[str] | None = None) -> list[dict[str, Any]]:
    rows = resolve_mihomo_runtime_proxy_rows(
        inventory_state="active",
        limit=1000,
        explicit_server_ids=explicit_server_ids,
    )
    groups: list[dict[str, Any]] = []
    for row in rows:
        raw = row.get("raw") if isinstance(row, dict) else None
        if not isinstance(raw, dict):
            continue
        topology = raw.get("_fwrouter_topology")
        if not isinstance(topology, dict) or topology.get("kind") != "logical_profile":
            continue
        runtime_policy = str(topology.get("runtime_policy") or "fallback")
        if runtime_policy != "fallback":
            continue
        runtime_name = str(raw.get("_fwrouter_runtime_name") or raw.get("name") or "").strip()
        if not runtime_name:
            continue
        members = _logical_profile_members(raw, logical_runtime_name=runtime_name)
        if not members:
            continue
        groups.append(_generated_logical_profile_group(runtime_name, members, runtime_policy))
    return groups


def _generated_logical_profile_group(
    runtime_name: str,
    members: list[dict[str, Any]],
    runtime_policy: str = "fallback",
) -> dict[str, Any]:
    return {
        "name": runtime_name,
        "type": runtime_policy,
        "proxies": [str(member["name"]) for member in members],
        "url": "https://www.gstatic.com/generate_204",
        "interval": LOGICAL_PROFILE_GROUP_INTERVAL_SECONDS,
    }


def _known_inactive_logical_profile_groups(
    *,
    active_group_names: set[str] | None = None,
    candidate_names: set[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Return exact generated groups whose persisted profile is not active.

    This includes profiles made inactive by inventory reconciliation and
    profiles intentionally excluded from the runtime proxy inventory. It does
    not infer ownership from names: the source row must retain logical-profile
    topology and the group must match the generator's complete shape later.
    """
    if active_group_names is None:
        active_group_names = {str(group["name"]) for group in _logical_profile_groups()}
    with db_session() as connection:
        rows = connection.execute(
            "SELECT server_name, raw_json FROM servers"
        ).fetchall()

    inactive: dict[str, dict[str, Any]] = {}
    ambiguous_names: set[str] = set()
    for row in rows:
        try:
            raw = json.loads(row["raw_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(raw, dict):
            continue
        topology = raw.get("_fwrouter_topology")
        if not isinstance(topology, dict) or topology.get("kind") != "logical_profile":
            continue
        if str(topology.get("runtime_policy") or "fallback") != "fallback":
            continue
        runtime_name = str(
            raw.get("_fwrouter_runtime_name") or raw.get("name") or row["server_name"] or ""
        ).strip()
        if not runtime_name or runtime_name in active_group_names:
            continue
        if candidate_names is not None and runtime_name not in candidate_names:
            continue
        members = _logical_profile_members(raw, logical_runtime_name=runtime_name)
        if not members:
            continue
        generated = _generated_logical_profile_group(runtime_name, members)
        previous = inactive.get(runtime_name)
        if runtime_name in ambiguous_names:
            continue
        if previous is not None:
            inactive.pop(runtime_name, None)
            ambiguous_names.add(runtime_name)
            continue
        inactive[runtime_name] = generated
    return inactive


def _inactive_logical_profile_members(
    runtime_name: str, expected_group: dict[str, Any],
) -> list[dict[str, Any]] | None:
    """Return members only when one persisted inactive profile owns the exact group."""
    with db_session() as connection:
        rows = connection.execute(
            "SELECT inventory_state, server_name, raw_json FROM servers "
            "WHERE CASE WHEN json_valid(raw_json) THEN COALESCE("
            "json_extract(raw_json, '$._fwrouter_runtime_name'), "
            "json_extract(raw_json, '$.name'), server_name) ELSE server_name END=? LIMIT 3",
            (runtime_name,),
        ).fetchall()
    # The bounded read is also the uniqueness proof: a saturated result is
    # ambiguous even if only one of the returned rows parses as a candidate.
    if len(rows) >= 3:
        return None
    matches: list[list[dict[str, Any]]] = []
    for row in rows:
        try:
            raw = json.loads(row["raw_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(raw, dict):
            continue
        topology = raw.get("_fwrouter_topology")
        if not isinstance(topology, dict) or topology.get("kind") != "logical_profile":
            continue
        name = str(raw.get("_fwrouter_runtime_name") or raw.get("name") or row["server_name"] or "").strip()
        if name != runtime_name or str(row["inventory_state"] or "") == "active":
            continue
        members = _logical_profile_members(raw, logical_runtime_name=runtime_name)
        if _generated_logical_profile_group(runtime_name, members) == expected_group:
            matches.append(members)
    return matches[0] if len(matches) == 1 else None


def _proxy_group_is_referenced(config: dict[str, Any], group_name: str) -> bool:
    for group in config.get("proxy-groups") or []:
        if not isinstance(group, dict) or str(group.get("name") or "").strip() == group_name:
            continue
        targets = group.get("proxies")
        if isinstance(targets, list) and group_name in targets:
            return True
    for listener in config.get("listeners") or []:
        if isinstance(listener, dict) and str(listener.get("proxy") or "").strip() == group_name:
            return True
    rule_sets = [config.get("rules")]
    sub_rules = config.get("sub-rules")
    if isinstance(sub_rules, dict):
        rule_sets.extend(sub_rules.values())
    for rules in rule_sets:
        if not isinstance(rules, list):
            continue
        for rule in rules:
            if not isinstance(rule, str):
                continue
            parts = [part.strip() for part in rule.split(",")]
            if parts and parts[-1].lower() == "no-resolve":
                parts.pop()
            if parts and parts[-1] == group_name:
                return True
    return False


def _stale_generated_logical_profile_group_names(
    base_config: dict[str, Any],
    *,
    logical_profile_groups: list[dict[str, Any]] | None = None,
) -> set[str]:
    """Identify unchanged FWRouter groups for profiles no longer in inventory.

    The base Mihomo file is also the input to the next generated candidate, so
    groups emitted by a previous generation can outlive their source row. Only
    prune a group when its persisted source row proves it was a logical profile,
    its name and full generated fallback shape still match that source, and none
    of its member proxies exist in the candidate. Modified or ambiguous groups
    are left for the normal native validator to accept or reject.
    """
    proxies = base_config.get("proxies") if isinstance(base_config.get("proxies"), list) else []
    proxy_names = {
        str(proxy.get("name") or "").strip()
        for proxy in proxies
        if isinstance(proxy, dict) and str(proxy.get("name") or "").strip()
    }
    groups = base_config.get("proxy-groups")
    if not isinstance(groups, list) or not groups:
        return set()

    stale_names: set[str] = set()
    active_groups = logical_profile_groups if logical_profile_groups is not None else _logical_profile_groups()
    active_group_names = {str(group["name"]) for group in active_groups}
    for runtime_name, expected in _known_inactive_logical_profile_groups(
        active_group_names=active_group_names,
        candidate_names={
            str(group.get("name") or "").strip()
            for group in groups
            if isinstance(group, dict) and str(group.get("name") or "").strip()
        },
    ).items():
        member_names = [str(name).strip() for name in expected.get("proxies", [])]
        # Do not remove a group whose old members still form any part of the
        # candidate. It may still be referenced by an independent selector or
        # be part of an in-flight last-good transition.
        if any(name in proxy_names for name in member_names):
            continue
        for group in groups:
            if not isinstance(group, dict) or group != expected:
                continue
            stale_names.add(runtime_name)
            break
    return stale_names


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
    routing: dict[str, Any] | None = None,
    required_last_good_names: set[str] | None = None,
    required_last_good_server_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    fixed_server_id = _fixed_routing_server_id(routing)
    explicit_server_ids = {fixed_server_id} if fixed_server_id else set()
    runtime_proxy_rows = resolve_mihomo_runtime_proxy_rows(
        inventory_state="active",
        limit=1000,
        explicit_server_ids=explicit_server_ids,
    )
    active_logical_group_names = {
        str(group["name"])
        for group in _logical_profile_groups(explicit_server_ids=explicit_server_ids)
    }
    inactive_logical_groups = _known_inactive_logical_profile_groups(
        active_group_names=active_logical_group_names,
        candidate_names=set(required_last_good_names or set()),
    )
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
            proxy = _project_native_proxy(proxy)
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
            topology = raw.get("_fwrouter_topology")
            if isinstance(topology, dict) and topology.get("kind") == "logical_profile":
                # Logical profiles are represented by generated proxy-groups,
                # not by a single native proxy. Their last-good handoff is
                # retained below from the group's owned member definitions.
                # Normalizing the profile row here would create a proxy whose
                # name collides with that group and makes native validation
                # reject the candidate configuration.
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
            proxy = _project_native_proxy(proxy)
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

    # Keep the old, already-applied logical-profile path intact while an Xray
    # handoff still targets that group. Once Xray converges, its name is no
    # longer in required_last_good_names and the stale group and its proxies
    # can be pruned by the normal candidate generation path.
    required_names = required_last_good_names or set()
    old_proxy_rows = {
        str(item.get("name") or "").strip(): item
        for item in (base_config.get("proxies") or [])
        if isinstance(item, dict) and str(item.get("name") or "").strip()
    }
    old_groups = {
        str(item.get("name") or "").strip(): item
        for item in (base_config.get("proxy-groups") or [])
        if isinstance(item, dict) and str(item.get("name") or "").strip()
    }
    for group_name, group in inactive_logical_groups.items():
        if group_name not in required_names:
            continue

        # The Xray binding is the authority for whether an old logical group
        # still has a live consumer. Its source row and ordered member
        # definitions are the authority for rebuilding that group while the
        # new profile generation is being validated. The active Mihomo file
        # may already have lost the old group, so requiring it to be present
        # there would make an otherwise recoverable handoff impossible.
        existing_group = old_groups.get(group_name)
        if existing_group is not None and existing_group != group:
            # A same-name operator or modified group is ambiguous ownership;
            # leave it intact and refuse to build a candidate that would bind
            # the retained Xray listener to an unproven target.
            raise ValueError("MIHOMO_XRAY_HANDOFF_TARGET_OWNERSHIP_AMBIGUOUS")
        source_members = _inactive_logical_profile_members(group_name, group)
        if source_members is None:
            continue
        if existing_group is None:
            base_config.setdefault("proxy-groups", []).append(group)
            old_groups[group_name] = group

        source_by_name = {str(member.get("name") or "").strip(): member for member in source_members}
        present_by_name = dict(old_proxy_rows)
        present_by_name.update({
            str(item.get("name") or "").strip(): item
            for item in runtime_proxies
            if isinstance(item, dict) and str(item.get("name") or "").strip()
        })
        for member_name in group.get("proxies") or []:
            name = str(member_name or "").strip()
            if not name:
                continue
            old_proxy = old_proxy_rows.get(name)
            source_proxy = source_by_name.get(name)
            if name in seen_names:
                present_proxy = present_by_name.get(name)
                expected_proxy = source_proxy
                if not isinstance(present_proxy, dict) or not isinstance(expected_proxy, dict):
                    raise ValueError("MIHOMO_XRAY_HANDOFF_MEMBER_OWNERSHIP_AMBIGUOUS")
                present_normalized = _normalize_proxy_list({
                    "proxies": [_project_native_proxy(dict(present_proxy))],
                }).get("proxies")
                expected_normalized = _normalize_proxy_list({
                    "proxies": [_project_native_proxy(dict(expected_proxy))],
                }).get("proxies")
                if present_normalized != expected_normalized:
                    raise ValueError("MIHOMO_XRAY_HANDOFF_MEMBER_OWNERSHIP_AMBIGUOUS")
                continue
            if isinstance(old_proxy, dict) and isinstance(source_proxy, dict):
                # Prefer the currently installed definition only when it is
                # the same persisted member. This retains its native form
                # without silently changing an owned handoff target.
                old_normalized = _normalize_proxy_list({
                    "proxies": [_project_native_proxy(dict(old_proxy))],
                }).get("proxies")
                source_normalized = _normalize_proxy_list({
                    "proxies": [_project_native_proxy(dict(source_proxy))],
                }).get("proxies")
                if old_normalized != source_normalized:
                    raise ValueError("MIHOMO_XRAY_HANDOFF_MEMBER_OWNERSHIP_AMBIGUOUS")
            proxy_source = old_proxy if isinstance(old_proxy, dict) else source_proxy
            if not isinstance(proxy_source, dict):
                continue
            normalized = _normalize_proxy_list({"proxies": [dict(proxy_source)]}).get("proxies")
            if not isinstance(normalized, list) or not normalized:
                continue
            proxy = normalized[0]
            proxy_type = str(proxy.get("type") or "").strip().lower()
            if not proxy_type or (proxy_type != "http" and not str(proxy.get("server") or "").strip()):
                continue
            proxy = _project_native_proxy(proxy)
            runtime_proxies.append(proxy)
            seen_names.add(name)
            present_by_name[name] = proxy

    return runtime_proxies


def _load_vpn_auto_proxy_names() -> list[str]:
    with db_session() as connection:
        rows = connection.execute(
            f"""
            SELECT COALESCE(
                json_extract(s.raw_json, '$._fwrouter_runtime_name'),
                json_extract(s.raw_json, '$.name'),
                s.server_name
            ) AS runtime_name
            FROM servers AS s
            JOIN server_preferences AS p ON p.server_id = s.server_id
            WHERE {auto_eligible_sql(server_alias="s", preferences_alias="p")}
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


def _fixed_routing_server_id(routing: dict[str, Any] | None) -> str:
    if not isinstance(routing, dict) or str(routing.get("server_mode") or "auto").strip().lower() != "fixed":
        return ""
    return str(routing.get("desired_fixed_server_id") or "").strip()


def _fixed_targetless_direct_compat(routing: dict[str, Any] | None) -> bool:
    """Retain the legacy direct-only startup shape without fixed proof."""
    if not isinstance(routing, dict):
        return False
    return bool(
        str(routing.get("server_mode") or "auto").strip().lower() == "fixed"
        and not _fixed_routing_server_id(routing)
        and str(routing.get("desired_mode") or "").strip().lower() == "direct"
        and str(routing.get("applied_mode") or "").strip().lower() == "direct"
    )


def _fixed_runtime_target(routing: dict[str, Any] | None) -> str | None:
    server_id = _fixed_routing_server_id(routing)
    if not server_id:
        return None
    rows = resolve_mihomo_runtime_proxy_rows(
        inventory_state="active",
        limit=1000,
        explicit_server_ids={server_id},
    )
    matches = [
        row for row in rows
        if isinstance(row, dict) and str(row.get("server_id") or "").strip() == server_id
    ]
    if len(matches) != 1:
        raise ValueError("FIXED_GLOBAL_SERVER_TARGET_UNRESOLVED")
    raw = matches[0].get("raw")
    if not isinstance(raw, dict):
        raise ValueError("FIXED_GLOBAL_SERVER_TARGET_UNRESOLVED")
    target = str(raw.get("_fwrouter_runtime_name") or raw.get("name") or "").strip()
    if not target or target in NON_SERVER_PROXY_TARGETS:
        raise ValueError("FIXED_GLOBAL_SERVER_TARGET_UNRESOLVED")
    aliases = 0
    for row in rows:
        candidate = row.get("raw") if isinstance(row, dict) else None
        if isinstance(candidate, dict) and str(
            candidate.get("_fwrouter_runtime_name") or candidate.get("name") or ""
        ).strip() == target:
            aliases += 1
            if aliases > 1:
                raise ValueError("FIXED_GLOBAL_SERVER_TARGET_AMBIGUOUS")
    return target


def _ensure_selector_groups(
    base_config: dict[str, Any],
    *,
    routing: dict[str, Any] | None = None,
    fixed_runtime_target: str | None = None,
) -> list[dict[str, Any]]:
    if fixed_runtime_target is None:
        fixed_runtime_target = _fixed_runtime_target(routing)
    fixed_server_id = _fixed_routing_server_id(routing)
    explicit_server_ids = {fixed_server_id} if fixed_server_id else set()
    existing_groups = base_config.get("proxy-groups") if isinstance(base_config.get("proxy-groups"), list) else []
    logical_profile_groups = _logical_profile_groups(explicit_server_ids=explicit_server_ids)
    stale_generated_groups = _stale_generated_logical_profile_group_names(
        base_config,
        logical_profile_groups=logical_profile_groups,
    )
    groups_by_name: dict[str, dict[str, Any]] = {}
    for group in existing_groups:
        if not isinstance(group, dict):
            continue
        name = str(group.get("name") or "").strip()
        if not name or name in stale_generated_groups:
            continue
        groups_by_name[name] = dict(group)

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
        # DIRECT is never an implicit member of a required-VPN selector.
        # REJECT gives Mihomo a valid terminal member when the pool is empty.
        "proxies": [*vpn_auto_names, "REJECT"],
    }

    fixed_mode = str((routing or {}).get("server_mode") or "auto").strip().lower() == "fixed"
    targetless_direct_compat = _fixed_targetless_direct_compat(routing)
    if fixed_server_id:
        # A pending fixed intent must not inherit a cached vpn-auto or global
        # target while the explicit owner is materializing and proving it.
        # REJECT is the safe startup/default member; only the canonical fixed
        # target is available to the later fenced selection owner.
        vpn_global_proxies = ["REJECT"]
        if fixed_runtime_target:
            if fixed_runtime_target not in proxy_name_set:
                raise ValueError("FIXED_GLOBAL_SERVER_TARGET_NOT_GENERATED")
            vpn_global_proxies.append(fixed_runtime_target)
    elif fixed_mode and not targetless_direct_compat:
        # Keep malformed fixed intent fail-closed while structural validation
        # rejects it before any native promotion or restart.
        vpn_global_proxies = ["REJECT"]
    else:
        vpn_global_proxies = ["vpn-auto"]
        for name in global_list_proxy_names:
            if name not in vpn_global_proxies:
                vpn_global_proxies.append(name)
        vpn_global_proxies.append("REJECT")
    groups_by_name["vpn-global"] = {
        "name": "vpn-global",
        "type": "select",
        "proxies": vpn_global_proxies,
    }

    subject_selector_targets = ["vpn-global", "vpn-auto"]
    for name in global_list_proxy_names:
        if name not in subject_selector_targets:
            subject_selector_targets.append(name)
    subject_selector_targets.append("REJECT")
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

    # Evaluate ownership references after FWRouter-owned selectors have been
    # rebuilt. A stale name present only in the old vpn-auto/vpn-global
    # projection is not a live external reference. User groups, rules and
    # listeners remain unchanged and protect their referenced group; the
    # native validator then decides whether that retained config is usable.
    reference_config = dict(base_config)
    reference_config["proxy-groups"] = list(groups_by_name.values())
    for name in stale_generated_groups:
        if _proxy_group_is_referenced(reference_config, name):
            previous = next(
                (
                    group
                    for group in existing_groups
                    if isinstance(group, dict) and str(group.get("name") or "").strip() == name
                ),
                None,
            )
            if isinstance(previous, dict):
                groups_by_name[name] = dict(previous)

    ordered_names = ["vpn-auto", "vpn-global"]
    ordered_names.extend(
        name for name in groups_by_name.keys()
        if name not in {"vpn-auto", "vpn-global"}
    )
    return [groups_by_name[name] for name in ordered_names]
