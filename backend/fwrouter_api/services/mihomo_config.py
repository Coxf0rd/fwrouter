"""Public compatibility facade for Mihomo config and reconcile operations."""
from __future__ import annotations

from pathlib import Path as Path
from typing import Any as Any

import yaml as yaml

from fwrouter_api.services.artifacts import atomic_write_text as atomic_write_text
from fwrouter_api.services.custom_servers import (
    resolve_mihomo_runtime_proxy_rows as resolve_mihomo_runtime_proxy_rows,
    resolve_runtime_proxy_rows as resolve_runtime_proxy_rows,
)
from fwrouter_api.services.logs import (
    write_operational_log as write_operational_log,
    write_technical_log as write_technical_log,
)
from fwrouter_api.services.mihomo_runtime import restart_mihomo_container as restart_mihomo_container
from fwrouter_api.services.modules import managed_runtime_operation_blocked as managed_runtime_operation_blocked
from fwrouter_api.services.mihomo_serialization import dump_mihomo_config as dump_mihomo_config
from fwrouter_api.services.mihomo_config_paths import (
    APPLIED_MANIFEST_PATH as APPLIED_MANIFEST_PATH,
    BASE_CONFIG_PATH as BASE_CONFIG_PATH,
    DEFAULT_FULL_VPN_TCP_REDIR_PORT as DEFAULT_FULL_VPN_TCP_REDIR_PORT,
    DEFAULT_FULL_VPN_UDP_TPROXY_PORT as DEFAULT_FULL_VPN_UDP_TPROXY_PORT,
    DEFAULT_TRANSPARENT_TCP_REDIR_PORT as DEFAULT_TRANSPARENT_TCP_REDIR_PORT,
    DEFAULT_TRANSPARENT_UDP_TPROXY_PORT as DEFAULT_TRANSPARENT_UDP_TPROXY_PORT,
    EXPLICIT_MIXED_LISTENER_BIND as EXPLICIT_MIXED_LISTENER_BIND,
    EXPLICIT_MIXED_LISTENER_NAME as EXPLICIT_MIXED_LISTENER_NAME,
    EXPLICIT_MIXED_LISTENER_PORT as EXPLICIT_MIXED_LISTENER_PORT,
    FULL_VPN_REDIR_LISTENER_NAME as FULL_VPN_REDIR_LISTENER_NAME,
    FULL_VPN_RULE_NAME as FULL_VPN_RULE_NAME,
    FULL_VPN_TPROXY_LISTENER_NAME as FULL_VPN_TPROXY_LISTENER_NAME,
    LEGACY_INBOUND_KEYS as LEGACY_INBOUND_KEYS,
    MIHOMO_CANDIDATE_CONFIG_PATH as MIHOMO_CANDIDATE_CONFIG_PATH,
    MIHOMO_CONTROLLER_ADDRESS as MIHOMO_CONTROLLER_ADDRESS,
    SUBJECT_SELECTOR_PREFIX as SUBJECT_SELECTOR_PREFIX,
    TRANSPARENT_BIND_ADDRESS as TRANSPARENT_BIND_ADDRESS,
    TRANSPARENT_REDIR_LISTENER_NAME as TRANSPARENT_REDIR_LISTENER_NAME,
    TRANSPARENT_TPROXY_LISTENER_NAME as TRANSPARENT_TPROXY_LISTENER_NAME,
    TRANSPARENT_TPROXY_PROXY_NAME as TRANSPARENT_TPROXY_PROXY_NAME,
    TRANSPARENT_TPROXY_RULE_NAME as TRANSPARENT_TPROXY_RULE_NAME,
    XRAY_MIHOMO_LISTENER_PREFIX as XRAY_MIHOMO_LISTENER_PREFIX,
    _iso8601_mtime as _iso8601_mtime,
    _resolved_base_config_path as _resolved_base_config_path,
    _resolved_candidate_config_path as _resolved_candidate_config_path,
    _resolved_last_good_mihomo_dir as _resolved_last_good_mihomo_dir,
    _safe_load_yaml as _safe_load_yaml,
    subject_selector_name as subject_selector_name,
)
from fwrouter_api.services.mihomo_config_inbounds import (
    _build_explicit_mixed_listener as _build_explicit_mixed_listener,
    _build_managed_transparent_listeners as _build_managed_transparent_listeners,
    _build_xray_handoff_listeners as _build_xray_handoff_listeners,
    _collect_xray_handoff_assignments as _collect_xray_handoff_assignments,
    _ensure_fwrouter_sniffer as _ensure_fwrouter_sniffer,
    _load_base_config as _load_base_config,
    _managed_full_vpn_redir_port as _managed_full_vpn_redir_port,
    _managed_full_vpn_tproxy_port as _managed_full_vpn_tproxy_port,
    _managed_transparent_redir_port as _managed_transparent_redir_port,
    _managed_transparent_tproxy_port as _managed_transparent_tproxy_port,
    _resolve_transparent_bind_address as _resolve_transparent_bind_address,
    _sanitize_fwrouter_managed_inbounds as _sanitize_fwrouter_managed_inbounds,
)
from fwrouter_api.services import mihomo_config_builder as _builder
from fwrouter_api.services import mihomo_config_proxies as _proxies
from fwrouter_api.services import mihomo_config_validation as _validation
from fwrouter_api.services import mihomo_reconcile as _reconcile
from fwrouter_api.services.module_attribute_aliases import (
    AttributeAliases as _AttributeAliases,
    install_attribute_aliases as _install_attribute_aliases,
)
from fwrouter_api.services.mihomo_config_proxies import (
    _ensure_selector_groups as _ensure_selector_groups,
    _fixed_runtime_target as _fixed_runtime_target,
    _merge_runtime_proxies as _merge_runtime_proxies,
    _runtime_proxy_inventory_count as _runtime_proxy_inventory_count,
)
from fwrouter_api.services.mihomo_config_rules import (
    _build_fallback_rule as _build_fallback_rule,
    _build_mihomo_config_with_source as _build_mihomo_config_with_source,
    _build_transparent_fallback_rule as _build_transparent_fallback_rule,
    _resolved_selective_default as _resolved_selective_default,
)
from fwrouter_api.services.mihomo_config_status import (
    _configs_equal as _configs_equal,
    _summarize_candidate as _summarize_candidate,
    _summarize_config_status as _summarize_config_status,
    get_mihomo_config_status as get_mihomo_config_status,
    mihomo_runtime_satisfies_routing as mihomo_runtime_satisfies_routing,
)
from fwrouter_api.services.mihomo_config_validation import (
    _validate_candidate_structure as _validate_candidate_structure,
    _validate_candidate_with_binary as _validate_candidate_with_binary,
)
from fwrouter_api.services.mihomo_config_builder import (
    build_mihomo_config as build_mihomo_config,
    validate_mihomo_candidate_config as validate_mihomo_candidate_config,
    write_mihomo_candidate_config as write_mihomo_candidate_config,
)
from fwrouter_api.services.mihomo_reconcile import (
    _write_mihomo_reconcile_logs as _write_mihomo_reconcile_logs,
    promote_mihomo_candidate_config as promote_mihomo_candidate_config,
    reconcile_mihomo_runtime as reconcile_mihomo_runtime,
    reconcile_mihomo_selective_default_fast as reconcile_mihomo_selective_default_fast,
    validate_and_promote_mihomo_candidate_config as validate_and_promote_mihomo_candidate_config,
)

# Retain the exact facade alias surface while routing assignments only to the
# consumer-local bindings that historically read these names.
_ALIASES: _AttributeAliases = {
    "_build_mihomo_config_with_source": ((_builder.__dict__, "_build_mihomo_config_with_source"), (_validation.__dict__, "_build_mihomo_config_with_source")),
    "_build_fallback_rule": ((_validation.__dict__, "_build_fallback_rule"),),
    "_build_transparent_fallback_rule": ((_validation.__dict__, "_build_transparent_fallback_rule"),),
    "_collect_xray_handoff_assignments": ((_builder.__dict__, "_collect_xray_handoff_assignments"),),
    "_configs_equal": _reconcile._RECONCILE_COMPAT_ALIAS_TARGETS["_configs_equal"],
    "_ensure_selector_groups": ((_builder.__dict__, "_ensure_selector_groups"),),
    "_iso8601_mtime": _reconcile._RECONCILE_COMPAT_ALIAS_TARGETS["_iso8601_mtime"],
    "_merge_runtime_proxies": ((_builder.__dict__, "_merge_runtime_proxies"),),
    "_resolve_transparent_bind_address": ((_builder.__dict__, "_resolve_transparent_bind_address"),),
    "_resolved_base_config_path": _reconcile._RECONCILE_COMPAT_ALIAS_TARGETS["_resolved_base_config_path"],
    "_resolved_candidate_config_path": _reconcile._RECONCILE_COMPAT_ALIAS_TARGETS["_resolved_candidate_config_path"],
    "_resolved_last_good_mihomo_dir": _reconcile._RECONCILE_COMPAT_ALIAS_TARGETS["_resolved_last_good_mihomo_dir"],
    "_resolved_selective_default": ((_builder.__dict__, "_resolved_selective_default"),),
    "_runtime_proxy_inventory_count": ((_validation.__dict__, "_runtime_proxy_inventory_count"),),
    "_safe_load_yaml": ((_builder.__dict__, "_safe_load_yaml"),),
    "_summarize_candidate": _reconcile._RECONCILE_COMPAT_ALIAS_TARGETS["_summarize_candidate"],
    "_summarize_config_status": _reconcile._RECONCILE_COMPAT_ALIAS_TARGETS["_summarize_config_status"],
    "_validate_candidate_structure": ((_builder.__dict__, "_validate_candidate_structure"),),
    "_validate_candidate_with_binary": ((_builder.__dict__, "_validate_candidate_with_binary"),),
    "_write_mihomo_reconcile_logs": _reconcile._RECONCILE_COMPAT_ALIAS_TARGETS["_write_mihomo_reconcile_logs"],
    "build_mihomo_config": ((_builder.__dict__, "build_mihomo_config"),),
    "get_mihomo_config_status": _reconcile._RECONCILE_COMPAT_ALIAS_TARGETS["get_mihomo_config_status"],
    "managed_runtime_operation_blocked": _reconcile._RECONCILE_COMPAT_ALIAS_TARGETS["managed_runtime_operation_blocked"],
    "resolve_mihomo_runtime_proxy_rows": ((_proxies.__dict__, "_resolve_config_inventory_rows"),),
    "restart_mihomo_container": _reconcile._RECONCILE_COMPAT_ALIAS_TARGETS["restart_mihomo_container"],
    "validate_mihomo_candidate_config": _reconcile._RECONCILE_COMPAT_ALIAS_TARGETS["validate_mihomo_candidate_config"],
    "write_mihomo_candidate_config": _reconcile._RECONCILE_COMPAT_ALIAS_TARGETS["write_mihomo_candidate_config"],
    "write_operational_log": _reconcile._RECONCILE_COMPAT_ALIAS_TARGETS["write_operational_log"],
    "write_technical_log": _reconcile._RECONCILE_COMPAT_ALIAS_TARGETS["write_technical_log"],
}

_install_attribute_aliases(__name__, _ALIASES)
