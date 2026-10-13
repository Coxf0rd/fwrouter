from __future__ import annotations

import json
import os
from typing import Any

from fwrouter_api.adapters.mihomo import DEFAULT_MIHOMO_ADAPTER
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
from fwrouter_api.services.mihomo_config_proxies import _ensure_selector_groups


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

    metadata_ok = (
        metadata.get("resolved_selective_default") == expected_selective_default
        and metadata.get("final_match_rule") == expected_final_match_rule
        and metadata.get("transparent_final_match_rule") == expected_transparent_final_match_rule
        and transparent_udp_policy_ok
        and full_vpn_udp_policy_ok
    )
    if not metadata_ok:
        return {
            "ok": False,
            "reason": "fwrouter_metadata_mismatch",
            "metadata": metadata,
            "expected": {
                "resolved_selective_default": expected_selective_default,
                "final_match_rule": expected_final_match_rule,
                "transparent_final_match_rule": expected_transparent_final_match_rule,
                "transparent_udp_policy_ok": transparent_udp_policy_ok,
                "full_vpn_udp_policy_ok": full_vpn_udp_policy_ok,
            },
        }

    expected_groups = {
        str(group.get("name") or ""): group.get("proxies")
        for group in _ensure_selector_groups(active_config if isinstance(active_config, dict) else {})
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
    selector_ok = bool(
        selectors.get("vpn_global_exists")
        and selectors.get("vpn_global_has_vpn_auto")
        and str(selectors.get("vpn_global_now") or "").strip() == "vpn-auto"
        and "REJECT" in set(selectors.get("vpn_auto_targets") or [])
        and "DIRECT" not in set(selectors.get("vpn_auto_targets") or [])
        and "REJECT" in set(selectors.get("vpn_global_targets") or [])
        and "DIRECT" not in set(selectors.get("vpn_global_targets") or [])
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
    if server_mode == "auto" and expected_native_members.get("vpn-auto") == {"REJECT"}:
        # Persisted automatic-selection intent/provenance can outlive an empty
        # eligible pool. The exact terminal native selection proves a valid
        # fail-closed policy without erasing that intent or claiming egress.
        selector_ok = selector_ok and (
            str(selectors.get("vpn_auto_now") or "").strip() == "REJECT"
            or str(selectors.get("vpn_auto_selected_target") or "").strip() == "REJECT"
        )
    elif server_mode == "auto" and expected_auto_server:
        selector_ok = selector_ok and str(selectors.get("vpn_auto_now") or "").strip() == expected_auto_server
    elif server_mode == "auto":
        # An empty owned pool is a valid installed fail-closed policy only
        # when the selector explicitly resolves to its terminal REJECT target.
        selector_ok = selector_ok and (
            str(selectors.get("vpn_auto_now") or "").strip() == "REJECT"
            or str(selectors.get("vpn_auto_selected_target") or "").strip() == "REJECT"
        )
    else:
        selector_ok = False
    vpn_egress_ready = bool(
        selector_ok
        and health.active_server_id
        and str(selectors.get("vpn_auto_selected_target") or selectors.get("vpn_auto_now") or "")
        not in {"DIRECT", "REJECT", "REJECT-DROP", "PASS", "PASS-RULE", "COMPATIBLE"}
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
