from __future__ import annotations

import json
import os
import ipaddress
from pathlib import Path
from typing import Any

import yaml

from fwrouter_api.adapters.mihomo import DEFAULT_MIHOMO_ADAPTER
from fwrouter_api.db.connection import db_session
from fwrouter_api.services.artifacts import atomic_write_text
from fwrouter_api.services.custom_servers import (
    resolve_mihomo_runtime_proxy_rows,
    resolve_runtime_proxy_rows,
)
from fwrouter_api.services.logs import write_operational_log, write_technical_log
from fwrouter_api.services.mihomo_runtime import restart_mihomo_container
from fwrouter_api.services.modules import managed_runtime_operation_blocked

from fwrouter_api.services.mihomo_config_paths import (
    APPLIED_MANIFEST_PATH,
    BASE_CONFIG_PATH,
    DEFAULT_FULL_VPN_TCP_REDIR_PORT,
    DEFAULT_FULL_VPN_UDP_TPROXY_PORT,
    DEFAULT_TRANSPARENT_TCP_REDIR_PORT,
    DEFAULT_TRANSPARENT_UDP_TPROXY_PORT,
    EXPLICIT_MIXED_LISTENER_BIND,
    EXPLICIT_MIXED_LISTENER_NAME,
    EXPLICIT_MIXED_LISTENER_PORT,
    FULL_VPN_REDIR_LISTENER_NAME,
    FULL_VPN_RULE_NAME,
    FULL_VPN_TPROXY_LISTENER_NAME,
    LEGACY_INBOUND_KEYS,
    MAX_BASE_CONFIG_BYTES,
    MIHOMO_CANDIDATE_CONFIG_PATH,
    MIHOMO_CONTROLLER_ADDRESS,
    SUBJECT_SELECTOR_PREFIX,
    TRANSPARENT_BIND_ADDRESS,
    TRANSPARENT_REDIR_LISTENER_NAME,
    TRANSPARENT_TPROXY_LISTENER_NAME,
    TRANSPARENT_TPROXY_PROXY_NAME,
    TRANSPARENT_TPROXY_RULE_NAME,
    XRAY_MIHOMO_LISTENER_PREFIX,
    _count_top_level_yaml_sequence,
    _iso8601_mtime,
    _resolved_applied_manifest_path,
    _resolved_base_config_path,
    _resolved_candidate_config_path,
    _resolved_contours_path,
    _resolved_debug_dir,
    _resolved_last_good_mihomo_dir,
    _resolve_proxy_bypass_mark_value,
    _safe_load_yaml,
    _scan_fwrouter_config_metadata,
    _uses_state_override,
    subject_selector_name,
)
from fwrouter_api.services.mihomo_config_inbounds import (
    _build_explicit_mixed_listener,
    _build_managed_transparent_listeners,
    _build_xray_handoff_listeners,
    _collect_xray_handoff_assignments,
    _ensure_fwrouter_sniffer,
    _load_base_config,
    _load_contours,
    _managed_full_vpn_redir_port,
    _managed_full_vpn_tproxy_port,
    _managed_transparent_redir_port,
    _managed_transparent_tproxy_port,
    _normalize_proxy_list,
    _resolve_transparent_bind_address,
    _resolve_transparent_redir_port,
    _resolve_transparent_tproxy_port,
    _sanitize_fwrouter_managed_inbounds,
    _transparent_bind_address_valid,
)


def _write_mihomo_reconcile_logs(
    *,
    ok: bool,
    event_type: str,
    message: str,
    details: dict[str, Any],
    operational_level: str = "info",
    technical_level: str = "info",
) -> None:
    if operational_level != "debug":
        write_operational_log(
            event_type=event_type,
            level=operational_level,
            message=message,
            details=details,
        )
    write_technical_log(
        component="mihomo",
        event_type=event_type,
        level=technical_level,
        message=message,
        details=details,
    )


def _resolved_selective_default(routing: dict[str, Any] | None = None) -> str:
    if isinstance(routing, dict):
        candidate = str(routing.get("selective_default") or "").strip().lower()
        if candidate in {"direct", "vpn"}:
            return candidate
    from fwrouter_api.services.rules import get_rules_state

    rules_state = get_rules_state()
    candidate = str(rules_state.get("selective_default") or "").strip().lower()
    return candidate if candidate in {"direct", "vpn"} else "direct"


def _build_fallback_rule(routing: dict[str, Any] | None = None) -> str:
    return "MATCH,DIRECT"


def _build_transparent_fallback_rule(routing: dict[str, Any] | None = None) -> str:
    """Return the fallback rule for FWRouter-managed transparent ingress.

    nft/dnsmasq remain the first decision layer, but IP sets can contain shared
    CDN addresses. The transparent listener must therefore re-apply domain
    rules after sniffing SNI/Host and only use the configured fallback when no
    domain rule matches.
    """

    mode = str((routing or {}).get("desired_mode") or (routing or {}).get("applied_mode") or "").strip().lower()
    if mode == "vpn" or _resolved_selective_default(routing) == "vpn":
        return "MATCH,vpn-global"
    return "MATCH,DIRECT"


def _build_effective_rules(routing: dict[str, Any] | None = None) -> tuple[list[str], dict[str, Any]]:
    from fwrouter_api.services.dataplane_global import read_effective_rules_artifact

    artifact = read_effective_rules_artifact()
    if not isinstance(artifact, dict):
        return [], {"rendered_rules_count": 0, "path_kind": "ip_only"}

    artifact_rules = artifact.get("rules")
    if not isinstance(artifact_rules, list):
        return [], {"rendered_rules_count": 0, "path_kind": "ip_only"}

    rendered_rules: list[str] = []
    domain_rule_count = 0
    for item in artifact_rules:
        if not isinstance(item, dict):
            continue

        action = str(item.get("action") or "").strip().upper()
        kind = str(item.get("kind") or "").strip().lower()
        raw_value = str(item.get("value") or "").strip()
        if not action or not kind or not raw_value:
            continue

        target = "vpn-global" if action == "VPN" else "DIRECT" if action == "DIRECT" else None
        if target is None:
            continue

        if kind == "domain":
            rendered_rules.append(f"DOMAIN,{raw_value.lower().rstrip('.')},{target}")
            domain_rule_count += 1
        elif kind == "domain_suffix":
            rendered_rules.append(f"DOMAIN-SUFFIX,{raw_value.lower().strip('.')},{target}")
            domain_rule_count += 1
        elif kind == "cidr":
            if ":" in raw_value:
                rendered_rules.append(f"IP-CIDR6,{raw_value},{target}")
            else:
                rendered_rules.append(f"IP-CIDR,{raw_value},{target}")

    return rendered_rules, {
        "rendered_rules_count": len(rendered_rules),
        "path_kind": "domain_aware" if domain_rule_count > 0 else "ip_only",
    }


def _format_source_ip_cidr_rule_value(value: str) -> str | None:
    """Return Mihomo-compatible source CIDR rule value for a host address."""

    raw_value = str(value or "").strip()
    if not raw_value:
        return None
    try:
        address = ipaddress.ip_address(raw_value)
    except ValueError:
        return None
    return f"{address}/{address.max_prefixlen}"


def _split_mihomo_rule_target(rule: str) -> tuple[str, str] | None:
    parts = [part.strip() for part in str(rule or "").split(",")]
    if len(parts) < 3:
        return None
    condition = ",".join(parts[:-1])
    target = parts[-1]
    if not condition or not target:
        return None
    return condition, target


def _build_source_scoped_vpn_rule(rule: str, *, source_cidr: str, target: str) -> str | None:
    parsed = _split_mihomo_rule_target(rule)
    if parsed is None:
        return None
    condition, original_target = parsed
    if original_target != "vpn-global":
        return None
    return f"AND,((SRC-IP-CIDR,{source_cidr}),({condition})),{target}"


def _load_subject_server_override_routes() -> list[dict[str, str]]:
    with db_session() as connection:
        cursor = connection.execute("""
            SELECT
                s.subject_id,
                coalesce(l.ip_address, t.tailscale_ip, d.ip_address) as ip,
                srv.server_name
            FROM subject_server_overrides o
            JOIN subjects s ON o.subject_id = s.subject_id
            JOIN servers srv ON o.selected_server_id = srv.server_id
            LEFT JOIN subject_lan l ON s.subject_id = l.subject_id
            LEFT JOIN subject_tailscale t ON s.subject_id = t.subject_id
            LEFT JOIN subject_docker d ON s.subject_id = d.subject_id
            WHERE s.is_active = 1
              AND s.is_deleted = 0
              AND o.selected_server_id IS NOT NULL
              AND ip IS NOT NULL
        """)
        rows = cursor.fetchall()

    routes: list[dict[str, str]] = []
    for row in rows:
        source_cidr = _format_source_ip_cidr_rule_value(str(row["ip"] or ""))
        subject_id = str(row["subject_id"] or "").strip()
        server_name = str(row["server_name"] or "").strip()
        if subject_id and source_cidr and server_name:
            routes.append(
                {
                    "subject_id": subject_id,
                    "source_cidr": source_cidr,
                    "server_name": server_name,
                    "selector_name": subject_selector_name(subject_id),
                }
            )
    return routes


def _runtime_proxy_inventory_count() -> int:
    rows = resolve_mihomo_runtime_proxy_rows(inventory_state="active", limit=1000)
    return sum(
        1
        for row in rows
        if isinstance(row, dict)
        and isinstance(row.get("raw"), dict)
        and str((row.get("raw") or {}).get("name") or "").strip()
    )


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
    summary["rules_count"] = len(candidate.get("rules") or [])
    return summary


def _summarize_config_status(status: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(status, dict):
        return {}
    summary = dict(status)
    summary.pop("base_config", None)
    summary.pop("candidate_config", None)
    return summary


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


def _build_mihomo_config_with_source(routing: dict[str, Any] | None = None) -> tuple[list[str], dict[str, Any]]:
    effective_rules, effective_metadata = _build_effective_rules(routing)
    subject_routes = _load_subject_server_override_routes()
    transparent_rules = []
    scoped_vpn_source_rules_count = 0
    for route in subject_routes:
        for rule in effective_rules:
            scoped_rule = _build_source_scoped_vpn_rule(
                rule,
                source_cidr=route["source_cidr"],
                target=route["selector_name"],
            )
            if scoped_rule:
                transparent_rules.append(scoped_rule)
                scoped_vpn_source_rules_count += 1

    final_match_rule = _build_fallback_rule(routing)
    transparent_final_match_rule = _build_transparent_fallback_rule(routing)
    base_rules = [*effective_rules, final_match_rule]
    transparent_rules.extend(effective_rules)
    transparent_rules.append(transparent_final_match_rule)
    full_vpn_rules = [
        f"SRC-IP-CIDR,{route['source_cidr']},{route['selector_name']}"
        for route in subject_routes
    ]
    full_vpn_rules.append("MATCH,vpn-global")
    return base_rules, {
        "rules": base_rules,
        "transparent_rules": transparent_rules,
        "full_vpn_rules": full_vpn_rules,
        "scoped_vpn_source_rules_count": scoped_vpn_source_rules_count,
        "resolved_selective_default": _resolved_selective_default(routing),
        "final_match_rule": final_match_rule,
        "transparent_final_match_rule": transparent_final_match_rule,
        "rendered_rules_count": int(effective_metadata.get("rendered_rules_count") or 0),
        "path_kind": str(effective_metadata.get("path_kind") or "core_routed"),
    }


def build_mihomo_config(routing: dict[str, Any] | None = None) -> dict[str, Any]:
    rules, metadata = _build_mihomo_config_with_source(routing)

    base_config = _load_base_config()
    base_config, sanitized_inbounds = _sanitize_fwrouter_managed_inbounds(base_config)
    base_config["rules"] = list(rules)
    base_config["proxies"] = _merge_runtime_proxies(base_config)
    base_config["proxy-groups"] = _ensure_selector_groups(base_config)
    sub_rules = base_config.get("sub-rules")
    if not isinstance(sub_rules, dict):
        sub_rules = {}
    else:
        sub_rules = dict(sub_rules)
    sub_rules[TRANSPARENT_TPROXY_RULE_NAME] = list(metadata["transparent_rules"])
    sub_rules[FULL_VPN_RULE_NAME] = list(metadata["full_vpn_rules"])
    base_config["sub-rules"] = sub_rules
    handoff_assignments = _collect_xray_handoff_assignments()
    transparent_bind_address = _resolve_transparent_bind_address()
    managed_transparent_listeners = _build_managed_transparent_listeners(transparent_bind_address)
    base_config["listeners"] = (
        [_build_explicit_mixed_listener()]
        + managed_transparent_listeners
        + list(base_config.get("listeners") or [])
        + _build_xray_handoff_listeners(handoff_assignments)
    )
    transparent_redir_port = _managed_transparent_redir_port()
    transparent_tproxy_port = _managed_transparent_tproxy_port()
    full_vpn_redir_port = _managed_full_vpn_redir_port()
    full_vpn_tproxy_port = _managed_full_vpn_tproxy_port()
    if "redir-port" in base_config:
        del base_config["redir-port"]
    if "tproxy-port" in base_config:
        del base_config["tproxy-port"]
    base_config["bind-address"] = transparent_bind_address
    base_config["external-controller"] = MIHOMO_CONTROLLER_ADDRESS
    base_config["allow-lan"] = True
    # FWRouter transparent ingress is currently IPv4-only. Leaving Mihomo
    # IPv6 enabled here can make the tproxy listener bind as IPv6-only
    # (`[::]:5202`), which breaks LAN transparent interception for IPv4
    # clients even though the YAML still says `listen: 0.0.0.0`.
    base_config["ipv6"] = False
    base_config.setdefault("mode", "rule")
    _ensure_fwrouter_sniffer(base_config)
    base_config.setdefault("fwrouter", {})
    if isinstance(base_config["fwrouter"], dict):
        base_config["fwrouter"].update(
            {
                "resolved_selective_default": metadata["resolved_selective_default"],
                "final_match_rule": metadata["final_match_rule"],
                "transparent_final_match_rule": metadata["transparent_final_match_rule"],
                "rendered_rules_count": metadata["rendered_rules_count"],
                "scoped_vpn_source_rules_count": metadata["scoped_vpn_source_rules_count"],
                "transparent_rule_name": TRANSPARENT_TPROXY_RULE_NAME,
                "path_kind": metadata["path_kind"],
                "state_consistency_ok": True,
                "transparent_mechanism": "split_redir_tproxy_ports",
                "mixed_listener_name": EXPLICIT_MIXED_LISTENER_NAME,
                "mixed_listener_bind": EXPLICIT_MIXED_LISTENER_BIND,
                "mixed_listener_port": EXPLICIT_MIXED_LISTENER_PORT,
                "transparent_listener_name": TRANSPARENT_TPROXY_LISTENER_NAME,
                "transparent_listener_bind": transparent_bind_address,
                "transparent_redir_port": transparent_redir_port,
                "transparent_tproxy_port": transparent_tproxy_port,
                "full_vpn_redir_port": full_vpn_redir_port,
                "full_vpn_tproxy_port": full_vpn_tproxy_port,
                "transparent_listener_port": transparent_tproxy_port,
                "transparent_inbound_rules": [],
                "sanitized_legacy_inbound_keys": list(sanitized_inbounds["removed_top_level_keys"]),
                "sanitized_managed_listeners": list(sanitized_inbounds["removed_listener_names"]),
            }
        )
    return base_config


from fwrouter_api.services.mihomo_config_validation import (
    _candidate_group_names,
    _candidate_runtime_proxies,
    _validate_candidate_structure,
    _validate_candidate_with_binary,
)

def write_mihomo_candidate_config(routing: dict[str, Any] | None = None) -> dict[str, Any]:
    """Generate and write Mihomo config."""
    base_config = build_mihomo_config(routing)
    rules = list(base_config.get("rules") or [])
    handoff_assignments = _collect_xray_handoff_assignments()

    candidate_path = Path(_resolved_candidate_config_path())
    atomic_write_text(candidate_path, yaml.dump(base_config, sort_keys=False))

    fwrouter_meta = base_config.get("fwrouter") if isinstance(base_config.get("fwrouter"), dict) else {}
    result = {
        "candidate_path": str(candidate_path),
        "rules": rules,
        "handoff_assignments": handoff_assignments,
        "resolved_selective_default": fwrouter_meta.get("resolved_selective_default"),
        "final_match_rule": fwrouter_meta.get("final_match_rule"),
        "transparent_final_match_rule": fwrouter_meta.get("transparent_final_match_rule"),
        "config": base_config,
    }
    write_technical_log(
        component="mihomo",
        event_type="mihomo_candidate_config_written",
        level="info",
        message="Mihomo candidate config generated.",
        details={
            "candidate_path": result["candidate_path"],
            "rules_count": len(result["rules"]),
            "resolved_selective_default": result["resolved_selective_default"],
            "final_match_rule": result["final_match_rule"],
            "transparent_final_match_rule": result["transparent_final_match_rule"],
            "handoff_assignments_count": len(result["handoff_assignments"]),
        },
    )
    return result


def validate_mihomo_candidate_config(routing: dict[str, Any] | None = None) -> dict[str, Any]:
    candidate_path = _resolved_candidate_config_path()
    candidate_config = _safe_load_yaml(candidate_path)
    if not isinstance(candidate_config, dict):
        return {
            "ok": False,
            "returncode": 1,
            "stdout_tail": "",
            "stderr_tail": "Candidate config is missing or invalid.",
            "error_code": "MIHOMO_CANDIDATE_INVALID",
        }

    selective_default = _resolved_selective_default(routing)
    structural = _validate_candidate_structure(candidate_config, routing=routing)
    binary_validation = _validate_candidate_with_binary(candidate_path)

    error_code = None
    stderr_tail = ""
    if not structural["allow_lan_enabled"]:
        error_code = "MIHOMO_ALLOW_LAN_REQUIRED"
        stderr_tail = "FWRouter Mihomo contour requires allow-lan=true."
    elif structural["routing_mark_value"] != structural["expected_routing_mark_value"]:
        error_code = "MIHOMO_ROUTING_MARK_MISMATCH"
        stderr_tail = (
            "Mihomo routing-mark does not match the FWRouter bypass mark contract."
        )
    elif structural["legacy_inbound_keys_present"]:
        error_code = "MIHOMO_LEGACY_INBOUND_CONFLICT"
        stderr_tail = (
            "Candidate Mihomo config still contains legacy top-level inbound keys: "
            + ", ".join(structural["legacy_inbound_keys_present"])
        )
    elif structural["mixed_listener_count"] != 1:
        error_code = "MIHOMO_MIXED_LISTENER_CONFLICT"
        stderr_tail = "FWRouter explicit mixed listener must exist exactly once."
    elif (
        structural["mixed_listener_bind"] != EXPLICIT_MIXED_LISTENER_BIND
        or structural["mixed_listener_port"] != EXPLICIT_MIXED_LISTENER_PORT
        or structural["mixed_listener_proxy"] != "vpn-global"
    ):
        error_code = "MIHOMO_MIXED_LISTENER_INVALID"
        stderr_tail = "FWRouter explicit mixed listener does not match the expected 127.0.0.1:5201 -> vpn-global contract."
    elif not structural["proxy_inventory_ok"]:
        error_code = "MIHOMO_PROXYSET_EMPTY"
        stderr_tail = "Candidate Mihomo config has no proxies while runtime VPN inventory is non-empty."
    elif not structural["vpn_auto_present"]:
        error_code = "MIHOMO_VPN_AUTO_MISSING"
        stderr_tail = "Candidate Mihomo config is missing vpn-auto selector."
    elif not structural["vpn_global_present"]:
        error_code = "MIHOMO_VPN_GLOBAL_MISSING"
        stderr_tail = "Candidate Mihomo config is missing vpn-global selector."
    elif not structural["vpn_global_has_vpn_auto"]:
        error_code = "MIHOMO_VPN_GLOBAL_MISWIRED"
        stderr_tail = "vpn-global selector does not include vpn-auto."
    elif structural["transparent_required"] and structural["transparent_listener_count"] != 2:
        error_code = "MIHOMO_TRANSPARENT_LISTENER_CONFLICT"
        stderr_tail = "FWRouter transparent ingress must expose exactly one REDIR port and one TPROXY port."
    elif structural["transparent_required"] and not structural["transparent_listener_present"]:
        error_code = "MIHOMO_TRANSPARENT_LISTENER_MISSING"
        stderr_tail = "Transparent REDIR/TPROXY ports are missing from candidate config."
    elif structural["transparent_required"] and not structural["transparent_listener_bind_valid"]:
        error_code = "MIHOMO_TRANSPARENT_LISTENER_BIND_INVALID"
        stderr_tail = (
            "Transparent TPROXY ingress must bind to the wildcard address or a real router IPv4, "
            f"got {structural['transparent_listener_bind'] or 'missing'}."
        )
    elif structural["transparent_required"] and not structural["transparent_inbound_rule_ok"]:
        error_code = "MIHOMO_TRANSPARENT_TARGET_MISSING"
        stderr_tail = "Transparent REDIR/TPROXY listeners must target vpn-global directly."
    elif (
        structural["transparent_required"]
        and not structural["transparent_direct_proxy_ok"]
        and not structural["transparent_subrules_ok"]
    ):
        error_code = "MIHOMO_TRANSPARENT_TARGET_MISSING"
        stderr_tail = "Transparent REDIR/TPROXY ingress must route to vpn-global directly or reference valid fwrouter sub-rules."
    elif not structural["state_consistency_ok"]:
        error_code = "MIHOMO_SELECTIVE_DEFAULT_MISMATCH"
        stderr_tail = "Resolved selective_default does not match candidate fallback rule."
    elif structural["transparent_required"] and not structural["transparent_state_consistency_ok"]:
        error_code = "MIHOMO_TRANSPARENT_FALLBACK_MISMATCH"
        stderr_tail = "Transparent listener sub-rules fallback does not match the resolved selective_default."
    elif structural["xray_handoff_targets_missing"]:
        error_code = "MIHOMO_XRAY_HANDOFF_TARGET_MISSING"
        stderr_tail = "One or more Xray handoff listeners reference missing proxy or selector targets."
    elif not binary_validation["ok"]:
        error_code = "MIHOMO_BINARY_VALIDATION_FAILED"
        stderr_tail = binary_validation["stderr_tail"] or "Mihomo binary validation failed."
    result = {
        "ok": error_code is None,
        "returncode": 0 if error_code is None else 1,
        "stdout_tail": binary_validation["stdout_tail"],
        "stderr_tail": stderr_tail,
        "resolved_selective_default": selective_default,
        "final_match_rule": structural["final_match_rule"],
        "expected_final_match_rule": structural["expected_final_match_rule"],
        "state_consistency_ok": structural["state_consistency_ok"],
        "runtime_proxy_inventory_count": structural["runtime_proxy_inventory_count"],
        "candidate_proxies_count": structural["candidate_proxies_count"],
        "proxy_inventory_ok": structural["proxy_inventory_ok"],
        "allow_lan_enabled": structural["allow_lan_enabled"],
        "routing_mark_value": structural["routing_mark_value"],
        "expected_routing_mark_value": structural["expected_routing_mark_value"],
        "legacy_inbound_keys_present": structural["legacy_inbound_keys_present"],
        "vpn_auto_present": structural["vpn_auto_present"],
        "vpn_global_present": structural["vpn_global_present"],
        "vpn_global_has_vpn_auto": structural["vpn_global_has_vpn_auto"],
        "mixed_listener_count": structural["mixed_listener_count"],
        "mixed_listener_bind": structural["mixed_listener_bind"],
        "mixed_listener_port": structural["mixed_listener_port"],
        "mixed_listener_proxy": structural["mixed_listener_proxy"],
        "transparent_required": structural["transparent_required"],
        "transparent_listener_present": structural["transparent_listener_present"],
        "transparent_listener_count": structural["transparent_listener_count"],
        "transparent_listener_bind": structural["transparent_listener_bind"],
        "transparent_redir_port": structural["transparent_redir_port"],
        "transparent_listener_port": structural["transparent_listener_port"],
        "transparent_listener_bind_valid": structural["transparent_listener_bind_valid"],
        "transparent_listener_proxy": structural["transparent_listener_proxy"],
        "transparent_rule_name": structural["transparent_rule_name"],
        "transparent_direct_proxy_ok": structural["transparent_direct_proxy_ok"],
        "transparent_inbound_rule": structural["transparent_inbound_rule"],
        "transparent_inbound_rules": structural["transparent_inbound_rules"],
        "transparent_inbound_rule_ok": structural["transparent_inbound_rule_ok"],
        "transparent_subrules_ok": structural["transparent_subrules_ok"],
        "transparent_final_match_rule": structural["transparent_final_match_rule"],
        "expected_transparent_final_match_rule": structural["expected_transparent_final_match_rule"],
        "transparent_state_consistency_ok": structural["transparent_state_consistency_ok"],
        "xray_handoff_targets_missing": structural["xray_handoff_targets_missing"],
        "binary_validation": binary_validation,
        "error_code": error_code,
    }
    write_technical_log(
        component="mihomo",
        event_type="mihomo_candidate_config_validated",
        level="info" if result["ok"] else "warning",
        message="Mihomo candidate config validation completed." if result["ok"] else "Mihomo candidate config validation failed.",
        details=result,
    )
    return result


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
    """Cheaply verify that live Mihomo already satisfies routing-owned config.

    This intentionally avoids generating and validating the full 100k+ rule
    candidate YAML. Any uncertainty returns ok=False so callers can fall back to
    the full reconcile path.
    """

    routing_dict = routing if isinstance(routing, dict) else {}
    base_path = _resolved_base_config_path()
    metadata = _scan_fwrouter_config_metadata(base_path)
    expected_final_match_rule = _build_fallback_rule(routing_dict)
    expected_transparent_final_match_rule = _build_transparent_fallback_rule(routing_dict)
    expected_selective_default = _resolved_selective_default(routing_dict)

    metadata_ok = (
        metadata.get("resolved_selective_default") == expected_selective_default
        and metadata.get("final_match_rule") == expected_final_match_rule
        and metadata.get("transparent_final_match_rule") == expected_transparent_final_match_rule
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
            },
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
    )
    if server_mode == "auto" and expected_auto_server:
        selector_ok = selector_ok and str(selectors.get("vpn_auto_now") or "").strip() == expected_auto_server
    else:
        selector_ok = False

    transparent_ok = bool(
        transparent_vpn.get("transparent_tcp_ready")
        and transparent_vpn.get("transparent_udp_ready")
        and transparent_vpn.get("transparent_tcp_listener_socket_present")
        and transparent_vpn.get("transparent_udp_listener_socket_present")
    )
    ok = runtime_state == "running" and selector_ok and transparent_ok
    return {
        "ok": ok,
        "reason": "active_mihomo_runtime_matches_routing" if ok else "active_mihomo_runtime_mismatch",
        "runtime_state": runtime_state,
        "selector_ok": selector_ok,
        "transparent_ok": transparent_ok,
        "metadata_ok": metadata_ok,
        "active_server_id": health.active_server_id,
        "vpn_auto_now": selectors.get("vpn_auto_now"),
        "vpn_global_now": selectors.get("vpn_global_now"),
    }


from fwrouter_api.services.mihomo_reconcile import (  # noqa: E402
    _build_config_status_summary,
    promote_mihomo_candidate_config,
    reconcile_mihomo_runtime,
    reconcile_mihomo_selective_default_fast,
    validate_and_promote_mihomo_candidate_config,
)
