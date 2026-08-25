from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

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
from fwrouter_api.services.mihomo_config_proxies import (
    _ensure_selector_groups,
    _load_custom_proxy_names,
    _load_vpn_auto_proxy_names,
    _merge_runtime_proxies,
    _runtime_proxy_inventory_count,
)
from fwrouter_api.services.mihomo_config_rules import (
    _build_effective_rules,
    _build_fallback_rule,
    _build_mihomo_config_with_source,
    _build_source_scoped_vpn_rule,
    _build_transparent_fallback_rule,
    _format_source_ip_cidr_rule_value,
    _load_subject_server_override_routes,
    _resolved_selective_default,
    _split_mihomo_rule_target,
)
from fwrouter_api.services.mihomo_config_status import (
    _config_structural_fingerprint,
    _configs_equal,
    _summarize_candidate,
    _summarize_config_status,
    get_mihomo_config_status,
    mihomo_runtime_satisfies_routing,
)


def _runtime_proxy_inventory_count() -> int:
    rows = resolve_mihomo_runtime_proxy_rows(inventory_state="active", limit=1000)
    return sum(
        1
        for row in rows
        if isinstance(row, dict)
        and isinstance(row.get("raw"), dict)
        and str((row.get("raw") or {}).get("name") or "").strip()
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


from fwrouter_api.services.mihomo_reconcile import (  # noqa: E402
    _build_config_status_summary,
    promote_mihomo_candidate_config,
    reconcile_mihomo_runtime,
    reconcile_mihomo_selective_default_fast,
    validate_and_promote_mihomo_candidate_config,
)
