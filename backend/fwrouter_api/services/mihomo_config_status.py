from __future__ import annotations

import json
import os
from typing import Any

from fwrouter_api.adapters.mihomo import DEFAULT_MIHOMO_ADAPTER, NON_SERVER_PROXY_TARGETS
from fwrouter_api.db.connection import db_session
from fwrouter_api.services.mihomo_config_paths import (
    FULL_VPN_RULE_NAME,
    TRANSPARENT_TPROXY_RULE_NAME,
    _count_top_level_yaml_sequence,
    _iso8601_mtime,
    _resolved_base_config_path,
    _resolved_candidate_config_path,
    _safe_load_yaml,
    _scan_fwrouter_config_metadata,
)
from fwrouter_api.services.mihomo_config_rules import (
    _build_mihomo_config_with_source,
    _build_fallback_rule,
    _build_transparent_fallback_rule,
    _resolved_selective_default,
)
from fwrouter_api.services.mihomo_config_proxies import (
    _ensure_selector_groups,
    _fixed_routing_server_id,
    _fixed_targetless_direct_compat,
    _logical_profile_groups,
)
from fwrouter_api.services.custom_servers import resolve_mihomo_runtime_proxy_rows


def _resolve_active_auto_runtime_target(
    logical_server_id: str,
    *,
    expected_targets: set[str],
) -> str | None:
    """Resolve one selected logical ID to its unique generated Mihomo target."""
    rows = resolve_mihomo_runtime_proxy_rows(inventory_state="active", limit=1000)
    matches: list[tuple[dict[str, Any], str]] = []
    for row in rows:
        if not isinstance(row, dict) or str(row.get("server_id") or "").strip() != logical_server_id:
            continue
        raw = row.get("raw")
        if not isinstance(raw, dict):
            continue
        runtime_target = str(raw.get("_fwrouter_runtime_name") or raw.get("name") or "").strip()
        if runtime_target:
            matches.append((raw, runtime_target))
    if len(matches) != 1:
        return None

    _raw, runtime_target = matches[0]
    if runtime_target in NON_SERVER_PROXY_TARGETS:
        return None
    if runtime_target not in expected_targets:
        return None
    aliases = 0
    for row in rows:
        raw = row.get("raw") if isinstance(row, dict) else None
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("_fwrouter_runtime_name") or raw.get("name") or "").strip()
        if name == runtime_target:
            aliases += 1
            if aliases > 1:
                return None
    return runtime_target


def _resolve_active_fixed_runtime_target(logical_server_id: str, *, expected_targets: set[str]) -> str | None:
    """Resolve the persisted fixed ID through the unique active runtime mapping."""
    rows = resolve_mihomo_runtime_proxy_rows(
        inventory_state="active",
        limit=1000,
        explicit_server_ids={logical_server_id},
    )
    matches: list[str] = []
    for row in rows:
        if not isinstance(row, dict) or str(row.get("server_id") or "").strip() != logical_server_id:
            continue
        raw = row.get("raw")
        if not isinstance(raw, dict):
            continue
        target = str(raw.get("_fwrouter_runtime_name") or raw.get("name") or "").strip()
        if target:
            matches.append(target)
    if len(matches) != 1:
        return None
    target = matches[0]
    if target in NON_SERVER_PROXY_TARGETS or target not in expected_targets:
        return None
    aliases = sum(
        1
        for row in rows
        if isinstance(row, dict)
        and isinstance(row.get("raw"), dict)
        and str(
            row["raw"].get("_fwrouter_runtime_name") or row["raw"].get("name") or ""
        ).strip() == target
    )
    return target if aliases == 1 else None


def _fixed_intent_is_clean_and_unexpired(routing: dict[str, Any]) -> bool:
    fixed_id = _fixed_routing_server_id(routing)
    if (
        not fixed_id
        or str(routing.get("applied_fixed_server_id") or "").strip() != fixed_id
        or str(routing.get("apply_state") or "").strip().lower() != "clean"
        or routing.get("error_code")
        or routing.get("error_message")
    ):
        return False
    with db_session() as connection:
        row = connection.execute(
            """SELECT server_mode, desired_fixed_server_id, applied_fixed_server_id,
                      fixed_server_until, apply_state, error_code, error_message,
                      datetime(fixed_server_until) IS NOT NULL
                        AND datetime(fixed_server_until) > CURRENT_TIMESTAMP AS ttl_live
               FROM routing_global_state WHERE id=1"""
        ).fetchone()
    if row is None:
        return False
    persisted = {
        "server_mode": str(row["server_mode"] or "auto"),
        "desired_fixed_server_id": str(row["desired_fixed_server_id"] or ""),
        "applied_fixed_server_id": str(row["applied_fixed_server_id"] or ""),
        "fixed_server_until": row["fixed_server_until"],
        "apply_state": str(row["apply_state"] or ""),
        "error_code": row["error_code"],
        "error_message": row["error_message"],
    }
    if any(routing.get(key) != value for key, value in persisted.items()):
        return False
    return bool(row["ttl_live"])


def mihomo_runtime_fixed_target_materialized(
    routing: dict[str, Any], *, require_selected: bool = False,
) -> dict[str, Any]:
    """Prove generated/native membership and optionally exact global selection."""
    fixed_id = _fixed_routing_server_id(routing)
    if not fixed_id:
        return {"ok": False, "reason": "fixed_target_missing"}
    base_path = _resolved_base_config_path()
    config = _safe_load_yaml(base_path)
    if not isinstance(config, dict):
        return {"ok": False, "reason": "active_config_unavailable"}
    fixed_logical_groups = _logical_profile_groups(explicit_server_ids={fixed_id})
    fixed_logical_groups_by_name = {
        str(group.get("name") or ""): group
        for group in fixed_logical_groups
        if isinstance(group, dict) and str(group.get("name") or "").strip()
    }
    target = _resolve_active_fixed_runtime_target(
        fixed_id,
        expected_targets={
            str(name).strip()
            for proxy in (config.get("proxies") or [])
            if isinstance(proxy, dict)
            for name in [proxy.get("name")]
            if str(name or "").strip()
        } | set(fixed_logical_groups_by_name),
    )
    if not target:
        return {"ok": False, "reason": "fixed_target_mapping_unresolved"}
    expected_groups = {
        str(group.get("name") or ""): list(group.get("proxies") or [])
        for group in _ensure_selector_groups(config, routing=routing)
        if isinstance(group, dict)
        and str(group.get("name") or "") in {"vpn-auto", "vpn-global"}
    }
    actual_groups = {
        str(group.get("name") or ""): list(group.get("proxies") or [])
        for group in (config.get("proxy-groups") or [])
        if isinstance(group, dict)
    }
    logical_group = fixed_logical_groups_by_name.get(target)
    logical_group_native_membership_ok = True
    if logical_group is not None:
        try:
            group_readbacks = DEFAULT_MIHOMO_ADAPTER.get_logical_groups_state([target])
        except Exception:
            group_readbacks = []
        if not isinstance(group_readbacks, list) or len(group_readbacks) != 1:
            logical_group_native_membership_ok = False
        else:
            group_readback = group_readbacks[0]
            members = group_readback.get("members") if isinstance(group_readback, dict) else None
            native_member_names = [
                str(member.get("runtime_identity") or "").strip()
                for member in members or []
                if isinstance(member, dict) and str(member.get("runtime_identity") or "").strip()
            ] if isinstance(members, list) else []
            expected_member_names = [str(name).strip() for name in logical_group.get("proxies") or []]
            logical_group_native_membership_ok = bool(
                group_readback.get("ok") is True
                and group_readback.get("logical_runtime_target") == target
                and native_member_names == expected_member_names
                and group_readback.get("effective_member_runtime_identity") in expected_member_names
            )
    config_membership_ok = bool(
        target in set(expected_groups.get("vpn-global", []))
        and all(actual_groups.get(name) == members for name, members in expected_groups.items())
        and (
            logical_group is None
            or actual_groups.get(target) == logical_group.get("proxies")
            and all(
                str(member_name) in {
                    str(proxy.get("name") or "").strip()
                    for proxy in (config.get("proxies") or [])
                    if isinstance(proxy, dict)
                }
                for member_name in logical_group.get("proxies") or []
            )
        )
    )
    if not config_membership_ok:
        return {"ok": False, "reason": "fixed_target_config_membership_mismatch"}
    try:
        health = DEFAULT_MIHOMO_ADAPTER.health()
    except Exception:
        return {"ok": False, "reason": "mihomo_health_unavailable"}
    runtime_state = str(getattr(health.runtime_state, "value", health.runtime_state))
    details = health.details if isinstance(health.details, dict) else {}
    selectors = details.get("selectors") if isinstance(details.get("selectors"), dict) else {}
    native_global = set(selectors.get("vpn_global_targets") or [])
    native_auto = set(selectors.get("vpn_auto_targets") or [])
    # Active server identity intentionally filters REJECT. Use fresh raw
    # selector readback, preserving fail-closed REJECT as positive evidence.
    current_global = str(selectors.get("vpn_global_selected_target") or "").strip()
    fixed_mode = bool(_fixed_routing_server_id(routing))
    native_membership_ok = bool(
        selectors.get("vpn_global_exists")
        and (
            selectors.get("vpn_global_has_vpn_auto") is False
            if fixed_mode
            else selectors.get("vpn_global_has_vpn_auto") is True
        )
        and
        native_global == set(expected_groups.get("vpn-global", []))
        and native_auto == set(expected_groups.get("vpn-auto", []))
        and target in native_global
        and "REJECT" in native_global
        and "DIRECT" not in native_global
        and (
            native_global == {"REJECT", target}
            if fixed_mode and target in native_global
            else not fixed_mode
        )
        and "REJECT" in native_auto
        and "DIRECT" not in native_auto
        and current_global in native_global
        and (
            current_global == target
            or current_global == "REJECT"
        )
    )
    selected_ok = current_global == target
    selection_requirement_ok = not require_selected or selected_ok
    return {
        "ok": runtime_state == "running" and native_membership_ok and selection_requirement_ok and logical_group_native_membership_ok,
        "reason": None if runtime_state == "running" and native_membership_ok and selection_requirement_ok and logical_group_native_membership_ok else "fixed_target_native_membership_unverified",
        "runtime_state": runtime_state,
        "config_membership_ok": config_membership_ok,
        "native_membership_ok": native_membership_ok,
        "logical_group_native_membership_ok": logical_group_native_membership_ok,
        "fixed_runtime_target": target,
        "vpn_global_now": current_global,
        "selected_fixed_target": selected_ok,
    }


def _config_structural_fingerprint(config: dict[str, Any]) -> dict[str, Any]:
    listeners = config.get("listeners") if isinstance(config.get("listeners"), list) else []
    normalized_listeners = [
        {
            "name": str(listener.get("name") or ""),
            "type": str(listener.get("type") or ""),
            "listen": str(listener.get("listen") or ""),
            "port": int(listener.get("port") or 0),
            "proxy": str(listener.get("proxy") or ""),
            "rule": str(listener.get("rule") or ""),
        }
        for listener in listeners
        if isinstance(listener, dict)
    ]
    return {
        "mixed-port": int(config.get("mixed-port") or 0),
        "routing-mark": int(config.get("routing-mark") or 0),
        "allow-lan": bool(config.get("allow-lan", False)),
        "mode": str(config.get("mode") or ""),
        "listeners": normalized_listeners,
        "tun_enabled": bool((config.get("tun") or {}).get("enable")) if isinstance(config.get("tun"), dict) else False,
    }


def _configs_equal(left: dict[str, Any] | None, right: dict[str, Any] | None) -> bool:
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    return json.dumps(left, ensure_ascii=False, sort_keys=True) == json.dumps(right, ensure_ascii=False, sort_keys=True)


def _summarize_candidate(candidate: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(candidate, dict):
        return {}
    summary = dict(candidate)
    summary.pop("config", None)
    summary.pop("_config", None)
    summary.pop("_candidate_config", None)
    rules = summary.pop("rules", None)
    handoff_assignments = summary.pop("handoff_assignments", None)
    summary["rules_count"] = int(summary.get("rules_count") or len(rules or []))
    summary["handoff_assignments_count"] = int(
        summary.get("handoff_assignments_count") or len(handoff_assignments or [])
    )
    return summary


def _summarize_config_status(status: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(status, dict):
        return {}
    summary = dict(status)
    summary.pop("base_config", None)
    summary.pop("candidate_config", None)
    return summary


def get_mihomo_config_status(*, include_config: bool = False) -> dict[str, Any]:
    base_path = _resolved_base_config_path()
    candidate_path = _resolved_candidate_config_path()
    current_config = _safe_load_yaml(base_path) if include_config else None
    candidate_config = _safe_load_yaml(candidate_path) if include_config else None

    status = {
        "base_path": base_path,
        "candidate_path": candidate_path,
        "base_exists": os.path.exists(base_path),
        "candidate_exists": os.path.exists(candidate_path),
        "base_updated_at": _iso8601_mtime(base_path),
        "candidate_updated_at": _iso8601_mtime(candidate_path),
        "base_rules_count": (
            len((current_config or {}).get("rules") or [])
            if include_config
            else _count_top_level_yaml_sequence(base_path, "rules")
        ),
        "candidate_rules_count": (
            len((candidate_config or {}).get("rules") or [])
            if include_config
            else _count_top_level_yaml_sequence(candidate_path, "rules")
        ),
    }
    if include_config:
        status["base_config"] = current_config
        status["candidate_config"] = candidate_config
    return status


def mihomo_runtime_satisfies_routing(routing: dict[str, Any] | None = None) -> dict[str, Any]:
    routing_dict = routing if isinstance(routing, dict) else {}
    base_path = _resolved_base_config_path()
    metadata = _scan_fwrouter_config_metadata(base_path)
    expected_final_match_rule = _build_fallback_rule(routing_dict)
    expected_transparent_final_match_rule = _build_transparent_fallback_rule(routing_dict)
    expected_selective_default = _resolved_selective_default(routing_dict)
    expected_metadata = {
        "resolved_selective_default": expected_selective_default,
        "final_match_rule": expected_final_match_rule,
        "transparent_final_match_rule": expected_transparent_final_match_rule,
    }
    if any(metadata.get(key) != value for key, value in expected_metadata.items()):
        # Missing or stale scalar metadata already proves that this config
        # cannot satisfy FWRouter's current routing contract. Avoid parsing a
        # potentially very large rules document and rebuilding its policy just
        # to reach the same fail-closed result. UDP policy checks are deliberately
        # left unknown here; they have not been computed on this fast-fail path.
        return {
            "ok": False,
            "reason": "fwrouter_metadata_mismatch",
            "metadata": metadata,
            "expected": {
                **expected_metadata,
                "transparent_udp_policy_ok": None,
                "full_vpn_udp_policy_ok": None,
            },
        }

    active_config = _safe_load_yaml(base_path)
    active_subrules = active_config.get("sub-rules") if isinstance(active_config, dict) else None
    active_subrules = active_subrules if isinstance(active_subrules, dict) else {}
    _expected_rules, expected_policy = _build_mihomo_config_with_source(routing_dict)
    transparent_udp_policy_ok = (
        active_subrules.get(TRANSPARENT_TPROXY_RULE_NAME)
        == expected_policy.get("transparent_rules")
    )
    full_vpn_udp_policy_ok = (
        active_subrules.get(FULL_VPN_RULE_NAME)
        == expected_policy.get("full_vpn_rules")
    )

    metadata_ok = transparent_udp_policy_ok and full_vpn_udp_policy_ok
    if not metadata_ok:
        return {
            "ok": False,
            "reason": "fwrouter_metadata_mismatch",
            "metadata": metadata,
            "expected": {
                **expected_metadata,
                "transparent_udp_policy_ok": transparent_udp_policy_ok,
                "full_vpn_udp_policy_ok": full_vpn_udp_policy_ok,
            },
        }

    expected_groups = {
        str(group.get("name") or ""): group.get("proxies")
        for group in _ensure_selector_groups(
            active_config if isinstance(active_config, dict) else {},
            routing=routing_dict,
        )
        if isinstance(group, dict)
        and (str(group.get("name") or "") in {"vpn-auto", "vpn-global"}
             or str(group.get("name") or "").startswith("fwrouter-subject-"))
    }
    actual_groups = {
        str(group.get("name") or ""): group.get("proxies")
        for group in (active_config.get("proxy-groups") or [])
        if isinstance(group, dict)
    } if isinstance(active_config, dict) else {}
    selector_config_ok = all(actual_groups.get(name) == targets for name, targets in expected_groups.items())
    if not selector_config_ok:
        return {
            "ok": False,
            "reason": "vpn_selector_config_mismatch",
            "expected_groups": expected_groups,
            "actual_groups": {name: actual_groups.get(name) for name in expected_groups},
        }

    try:
        health = DEFAULT_MIHOMO_ADAPTER.health()
    except Exception as exc:
        return {"ok": False, "reason": "mihomo_health_failed", "error": str(exc)}

    runtime_state = str(getattr(health.runtime_state, "value", health.runtime_state))
    details = health.details if isinstance(health.details, dict) else {}
    selectors = details.get("selectors") if isinstance(details.get("selectors"), dict) else {}
    config = details.get("config") if isinstance(details.get("config"), dict) else {}
    contours = config.get("fwrouter_contours") if isinstance(config.get("fwrouter_contours"), dict) else {}
    transparent_vpn = contours.get("transparent_vpn") if isinstance(contours.get("transparent_vpn"), dict) else {}

    server_mode = str(routing_dict.get("server_mode") or "auto").strip().lower()
    expected_auto_server = str(routing_dict.get("active_auto_server_id") or "").strip()
    fixed_id = _fixed_routing_server_id(routing_dict)
    legacy_targetless_direct = _fixed_targetless_direct_compat(routing_dict)
    fixed_mode = server_mode == "fixed" and bool(fixed_id)
    auto_projection = server_mode == "auto" or legacy_targetless_direct
    global_selector_structure_ok = bool(
        selectors.get("vpn_global_exists")
        and (
            selectors.get("vpn_global_has_vpn_auto") is False
            if fixed_mode
            else selectors.get("vpn_global_has_vpn_auto") is True
        )
        and "REJECT" in set(selectors.get("vpn_global_targets") or [])
        and "DIRECT" not in set(selectors.get("vpn_global_targets") or [])
    )
    auto_selector_policy_ok = bool(
        "REJECT" in set(selectors.get("vpn_auto_targets") or [])
        and "DIRECT" not in set(selectors.get("vpn_auto_targets") or [])
    )
    expected_native_members = {name: set(targets or []) for name, targets in expected_groups.items()}
    runtime_native_members = {
        "vpn-auto": set(selectors.get("vpn_auto_targets") or []),
        "vpn-global": set(selectors.get("vpn_global_targets") or []),
    }
    runtime_native_members.update({
        str(name): set(targets or [])
        for name, targets in (selectors.get("subject_selector_targets") or {}).items()
    })
    selector_native_membership_ok = runtime_native_members == expected_native_members
    selector_ok = global_selector_structure_ok and auto_selector_policy_ok
    if auto_projection and expected_native_members.get("vpn-auto") == {"REJECT"}:
        # Persisted automatic-selection intent/provenance can outlive an empty
        # eligible pool. The exact terminal native selection proves a valid
        # fail-closed policy without erasing that intent or claiming egress.
        selector_ok = selector_ok and str(selectors.get("vpn_global_now") or "").strip() == "vpn-auto" and (
            str(selectors.get("vpn_auto_now") or "").strip() == "REJECT"
            or str(selectors.get("vpn_auto_selected_target") or "").strip() == "REJECT"
        )
    elif auto_projection and expected_auto_server:
        expected_runtime_target = _resolve_active_auto_runtime_target(
            expected_auto_server,
            expected_targets=expected_native_members.get("vpn-auto", set()),
        )
        selector_ok = selector_ok and str(selectors.get("vpn_global_now") or "").strip() == "vpn-auto" and expected_runtime_target is not None and (
            str(selectors.get("vpn_auto_now") or "").strip() == expected_runtime_target
        )
    elif auto_projection:
        # An empty owned pool is a valid installed fail-closed policy only
        # when the selector explicitly resolves to its terminal REJECT target.
        selector_ok = selector_ok and str(selectors.get("vpn_global_now") or "").strip() == "vpn-auto" and (
            str(selectors.get("vpn_auto_now") or "").strip() == "REJECT"
            or str(selectors.get("vpn_auto_selected_target") or "").strip() == "REJECT"
        )
    elif fixed_mode:
        expected_fixed_target = _resolve_active_fixed_runtime_target(
            fixed_id,
            expected_targets=expected_native_members.get("vpn-global", set()),
        ) if fixed_id else None
        selector_ok = bool(
            selector_ok
            and _fixed_intent_is_clean_and_unexpired(routing_dict)
            and expected_fixed_target is not None
            and expected_native_members.get("vpn-global") == {"REJECT", expected_fixed_target}
            and str(selectors.get("vpn_global_now") or "").strip() == expected_fixed_target
            and str(selectors.get("vpn_global_selected_target") or "").strip() == expected_fixed_target
        )
    else:
        selector_ok = False
    active_global_target = (
        selectors.get("vpn_global_selected_target") or selectors.get("vpn_global_now")
        if fixed_mode
        else selectors.get("vpn_auto_selected_target") or selectors.get("vpn_auto_now")
    )
    vpn_egress_ready = bool(
        selector_ok
        and str(active_global_target or "") not in NON_SERVER_PROXY_TARGETS
    )

    transparent_ok = bool(
        transparent_vpn.get("transparent_tcp_ready")
        and transparent_vpn.get("transparent_udp_ready")
        and transparent_vpn.get("transparent_tcp_listener_socket_present")
        and transparent_vpn.get("transparent_udp_listener_socket_present")
    )
    ok = runtime_state == "running" and selector_ok and selector_native_membership_ok and transparent_ok
    return {
        "ok": ok,
        "reason": "active_mihomo_runtime_matches_routing" if ok else "active_mihomo_runtime_mismatch",
        "runtime_state": runtime_state,
        "selector_ok": selector_ok,
        "selector_native_membership_ok": selector_native_membership_ok,
        "transparent_ok": transparent_ok,
        "metadata_ok": metadata_ok,
        "active_server_id": health.active_server_id,
        "vpn_auto_now": selectors.get("vpn_auto_now"),
        "vpn_global_now": selectors.get("vpn_global_now"),
        "vpn_auto_selected_target": selectors.get("vpn_auto_selected_target"),
        "vpn_global_selected_target": selectors.get("vpn_global_selected_target"),
        "vpn_egress_ready": vpn_egress_ready,
    }
