from __future__ import annotations

import shutil
import subprocess
from typing import Any

from fwrouter_api.services.mihomo_config_inbounds import (
    _load_contours,
    _transparent_bind_address_valid,
)
from fwrouter_api.services.mihomo_config_paths import (
    EXPLICIT_MIXED_LISTENER_BIND,
    EXPLICIT_MIXED_LISTENER_NAME,
    EXPLICIT_MIXED_LISTENER_PORT,
    LEGACY_INBOUND_KEYS,
    TRANSPARENT_REDIR_LISTENER_NAME,
    TRANSPARENT_TPROXY_LISTENER_NAME,
    TRANSPARENT_TPROXY_PROXY_NAME,
    TRANSPARENT_TPROXY_RULE_NAME,
    XRAY_MIHOMO_LISTENER_PREFIX,
    _resolve_proxy_bypass_mark_value,
)


def _candidate_runtime_proxies(candidate_config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    proxies = candidate_config.get("proxies")
    if not isinstance(proxies, list):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for proxy in proxies:
        if not isinstance(proxy, dict):
            continue
        name = str(proxy.get("name") or "").strip()
        if not name:
            continue
        result[name] = proxy
    return result


def _candidate_group_names(candidate_config: dict[str, Any]) -> set[str]:
    groups = candidate_config.get("proxy-groups")
    if not isinstance(groups, list):
        return set()
    names: set[str] = set()
    for group in groups:
        if not isinstance(group, dict):
            continue
        name = str(group.get("name") or "").strip()
        if name:
            names.add(name)
    return names


def _validate_candidate_with_binary(candidate_path: str) -> dict[str, Any]:
    for binary_name in ("mihomo", "clash-meta", "clash"):
        binary_path = shutil.which(binary_name)
        if not binary_path:
            continue
        completed = subprocess.run(
            [binary_path, "-t", "-f", candidate_path],
            capture_output=True,
            text=True,
            check=False,
        )
        stdout_tail = (completed.stdout or "")[-4000:]
        stderr_tail = (completed.stderr or "")[-4000:]
        return {
            "available": True,
            "binary": binary_path,
            "ok": completed.returncode == 0,
            "returncode": completed.returncode,
            "stdout_tail": stdout_tail,
            "stderr_tail": stderr_tail,
        }

    return {
        "available": False,
        "binary": None,
        "ok": True,
        "returncode": 0,
        "stdout_tail": "",
        "stderr_tail": "",
    }


def _validate_candidate_structure(
    candidate_config: dict[str, Any],
    *,
    routing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from fwrouter_api.services.mihomo_config import (
        _build_fallback_rule,
        _build_transparent_fallback_rule,
        _runtime_proxy_inventory_count,
    )

    rules = candidate_config.get("rules") if isinstance(candidate_config.get("rules"), list) else []
    proxies = candidate_config.get("proxies") if isinstance(candidate_config.get("proxies"), list) else []
    groups = candidate_config.get("proxy-groups") if isinstance(candidate_config.get("proxy-groups"), list) else []
    listeners = candidate_config.get("listeners") if isinstance(candidate_config.get("listeners"), list) else []
    sub_rules = candidate_config.get("sub-rules") if isinstance(candidate_config.get("sub-rules"), dict) else {}

    final_match_rule = str(rules[-1]) if rules else ""
    expected_final_match_rule = _build_fallback_rule(routing)
    state_consistency_ok = final_match_rule == expected_final_match_rule

    runtime_proxy_count = _runtime_proxy_inventory_count()
    proxy_inventory_ok = not (runtime_proxy_count > 0 and len(proxies) == 0)
    allow_lan_enabled = bool(candidate_config.get("allow-lan"))
    routing_mark_value = int(candidate_config.get("routing-mark") or 0)
    expected_routing_mark_value = _resolve_proxy_bypass_mark_value()
    legacy_inbound_keys_present = [key for key in LEGACY_INBOUND_KEYS if key in candidate_config]
    managed_redir_port = candidate_config.get("redir-port")
    if isinstance(managed_redir_port, str) and managed_redir_port.isdigit():
        managed_redir_port = int(managed_redir_port)
    if not isinstance(managed_redir_port, int):
        managed_redir_port = None

    managed_tproxy_port = candidate_config.get("tproxy-port")
    if isinstance(managed_tproxy_port, str) and managed_tproxy_port.isdigit():
        managed_tproxy_port = int(managed_tproxy_port)
    if not isinstance(managed_tproxy_port, int):
        managed_tproxy_port = None
    top_level_bind_address = str(candidate_config.get("bind-address") or "").strip() or None

    proxy_names = set(_candidate_runtime_proxies(candidate_config).keys())
    group_names = _candidate_group_names(candidate_config)
    vpn_auto_present = "vpn-auto" in group_names
    vpn_global_present = "vpn-global" in group_names

    vpn_global_has_vpn_auto = False
    for group in groups:
        if not isinstance(group, dict):
            continue
        if str(group.get("name") or "").strip() != "vpn-global":
            continue
        targets = group.get("proxies")
        if isinstance(targets, list):
            vpn_global_has_vpn_auto = "vpn-auto" in [str(item) for item in targets]
        break

    mixed_listeners = [
        listener
        for listener in listeners
        if isinstance(listener, dict)
        and str(listener.get("name") or "").strip() == EXPLICIT_MIXED_LISTENER_NAME
    ]
    mixed_listener = mixed_listeners[0] if mixed_listeners else None
    mixed_listener_proxy = (
        str(mixed_listener.get("proxy") or "").strip()
        if isinstance(mixed_listener, dict)
        else None
    )
    mixed_listener_bind = (
        str(mixed_listener.get("listen") or "").strip()
        if isinstance(mixed_listener, dict)
        else None
    )
    mixed_listener_port = (
        int(mixed_listener.get("port") or 0)
        if isinstance(mixed_listener, dict)
        else None
    )

    transparent_redir_listener = next(
        (
            listener
            for listener in listeners
            if isinstance(listener, dict)
            and str(listener.get("name") or "").strip() == TRANSPARENT_REDIR_LISTENER_NAME
            and str(listener.get("type") or "").strip().lower() == "redir"
        ),
        None,
    )
    transparent_tproxy_listener = next(
        (
            listener
            for listener in listeners
            if isinstance(listener, dict)
            and str(listener.get("name") or "").strip() == TRANSPARENT_TPROXY_LISTENER_NAME
            and str(listener.get("type") or "").strip().lower() == "tproxy"
        ),
        None,
    )
    transparent_rule_name = TRANSPARENT_TPROXY_RULE_NAME
    transparent_listener_proxy = TRANSPARENT_TPROXY_PROXY_NAME
    transparent_listener_bind = None
    if isinstance(transparent_redir_listener, dict):
        transparent_listener_bind = str(transparent_redir_listener.get("listen") or "").strip() or None
        if not isinstance(managed_redir_port, int):
            port_value = transparent_redir_listener.get("port")
            if isinstance(port_value, int):
                managed_redir_port = port_value
    if isinstance(transparent_tproxy_listener, dict):
        if not transparent_listener_bind:
            transparent_listener_bind = str(transparent_tproxy_listener.get("listen") or "").strip() or None
        if not isinstance(managed_tproxy_port, int):
            port_value = transparent_tproxy_listener.get("port")
            if isinstance(port_value, int):
                managed_tproxy_port = port_value
    if not transparent_listener_bind:
        transparent_listener_bind = top_level_bind_address
    transparent_listener_bind_valid = _transparent_bind_address_valid(transparent_listener_bind)
    transparent_subrules = sub_rules.get(transparent_rule_name) if transparent_rule_name else None
    transparent_subrules_ok = isinstance(transparent_subrules, list) and bool(transparent_subrules)
    transparent_final_match_rule = (
        str(transparent_subrules[-1])
        if isinstance(transparent_subrules, list) and transparent_subrules
        else ""
    )
    expected_transparent_final_match_rule = _build_transparent_fallback_rule(routing)
    transparent_direct_proxy_ok = (
        isinstance(transparent_redir_listener, dict)
        and isinstance(transparent_tproxy_listener, dict)
        and str(transparent_redir_listener.get("proxy") or "").strip() == TRANSPARENT_TPROXY_PROXY_NAME
        and str(transparent_tproxy_listener.get("proxy") or "").strip() == TRANSPARENT_TPROXY_PROXY_NAME
    )
    transparent_listener_rule_ok = (
        isinstance(transparent_redir_listener, dict)
        and isinstance(transparent_tproxy_listener, dict)
        and str(transparent_redir_listener.get("rule") or "").strip() == transparent_rule_name
        and str(transparent_tproxy_listener.get("rule") or "").strip() == transparent_rule_name
    )
    transparent_inbound_rule_ok = transparent_direct_proxy_ok or transparent_listener_rule_ok
    transparent_state_consistency_ok = (
        transparent_direct_proxy_ok
        or transparent_final_match_rule == expected_transparent_final_match_rule
    )
    contours = _load_contours()
    transparent_contour = contours.get("transparent_vpn") if isinstance(contours, dict) else None
    transparent_required = bool(
        isinstance(transparent_contour, dict)
        and transparent_contour.get("ready")
        and transparent_contour.get("tproxy_port")
        and transparent_contour.get("redir_port")
    )
    desired_mode = str((routing or {}).get("desired_mode") or "").strip().lower()
    if desired_mode in {"selective", "vpn"}:
        transparent_required = True

    handoff_targets_missing: list[str] = []
    for listener in listeners:
        if not isinstance(listener, dict):
            continue
        name = str(listener.get("name") or "").strip()
        if not name.startswith(XRAY_MIHOMO_LISTENER_PREFIX):
            continue
        proxy_target = str(listener.get("proxy") or "").strip()
        if proxy_target and proxy_target not in proxy_names and proxy_target not in group_names and proxy_target != "DIRECT":
            handoff_targets_missing.append(name)

    return {
        "final_match_rule": final_match_rule,
        "expected_final_match_rule": expected_final_match_rule,
        "state_consistency_ok": state_consistency_ok,
        "runtime_proxy_inventory_count": runtime_proxy_count,
        "candidate_proxies_count": len(proxies),
        "proxy_inventory_ok": proxy_inventory_ok,
        "allow_lan_enabled": allow_lan_enabled,
        "routing_mark_value": routing_mark_value,
        "expected_routing_mark_value": expected_routing_mark_value,
        "legacy_inbound_keys_present": legacy_inbound_keys_present,
        "vpn_auto_present": vpn_auto_present,
        "vpn_global_present": vpn_global_present,
        "vpn_global_has_vpn_auto": vpn_global_has_vpn_auto,
        "mixed_listener_count": len(mixed_listeners),
        "mixed_listener_bind": mixed_listener_bind,
        "mixed_listener_port": mixed_listener_port,
        "mixed_listener_proxy": mixed_listener_proxy,
        "transparent_required": transparent_required,
        "transparent_listener_present": isinstance(transparent_redir_listener, dict) and isinstance(transparent_tproxy_listener, dict),
        "transparent_listener_count": int(isinstance(transparent_redir_listener, dict)) + int(isinstance(transparent_tproxy_listener, dict)),
        "transparent_listener_bind": transparent_listener_bind,
        "transparent_redir_port": managed_redir_port,
        "transparent_listener_port": managed_tproxy_port,
        "transparent_listener_bind_valid": transparent_listener_bind_valid,
        "transparent_listener_proxy": transparent_listener_proxy or None,
        "transparent_rule_name": transparent_rule_name or None,
        "transparent_direct_proxy_ok": transparent_direct_proxy_ok,
        "transparent_listener_rule_ok": transparent_listener_rule_ok,
        "transparent_inbound_rule": None,
        "transparent_inbound_rules": [],
        "transparent_inbound_rule_ok": transparent_inbound_rule_ok,
        "transparent_subrules_ok": transparent_subrules_ok,
        "transparent_final_match_rule": transparent_final_match_rule,
        "expected_transparent_final_match_rule": expected_transparent_final_match_rule,
        "transparent_state_consistency_ok": transparent_state_consistency_ok,
        "xray_handoff_targets_missing": handoff_targets_missing,
    }
