from __future__ import annotations

from typing import Any

from fwrouter_api.services.ui_display_settings_common import (
    _default_external_collector_config,
    _normalize_external_collector_config,
    _normalize_external_integration_mode,
    _normalize_external_refresh_mode,
    _normalize_replacement_target,
    external_connection_identity,
)
from fwrouter_api.services.ui_text import _ui_text_title


def _external_management_label(client_name: str) -> str:
    normalized = str(client_name or "").strip()
    if normalized.lower() in {"homeassistant", "home-assistant", "home_assistant", "ha"}:
        return "Home Assistant"
    return normalized.replace("_", " ").replace("-", " ").strip().title() or "External management"


def _external_management_api_guide(system: dict[str, Any]) -> dict[str, Any]:
    label = str(system.get("label") or system.get("system_id") or "external-client").strip()
    identity = external_connection_identity(system)
    client_slug = identity["external_system_id"]
    requested_by = identity["requested_by"]
    management_context = {
        "client_name": client_slug,
        "channel": "local_api",
        "action": "<action>",
        "actor": "<optional-actor>",
    }
    return {
        "connection_type": "external_management",
        "purpose": "External client controls FWRouter through the HTTP API.",
        "configure": {
            "base_url": "http://<fwrouter-host>:5500/api/v2",
            "requested_by": requested_by,
            "management_context": management_context,
        },
        "available_elements": {
            "requested_by": "external_client:<client-name>",
            "management_context.client_name": client_slug,
            "management_context.channel": "local_api|webhook|automation|manual",
            "management_context.action": "<action>",
            "management_context.actor": "<optional-actor>",
        },
        "collection": _external_collection_guide(system),
        "examples": [
            {
                "label": _ui_text_title("connection.api_example", "switch_vpn_auto_server") or "switch_vpn_auto_server",
                "method": "POST",
                "path": "/selector/vpn-auto/switch",
                "body": {
                    "confirm_switch": True,
                    "requested_by": requested_by,
                    "management_context": {
                        **management_context,
                        "action": "switch_best_vpn_auto_server",
                    },
                },
            },
            {
                "label": _ui_text_title("connection.api_example", "clear_fixed_global_server") or "clear_fixed_global_server",
                "method": "DELETE",
                "path": (
                    "/routing/global/fixed-server?confirm_switch=true"
                    f"&requested_by={requested_by}"
                    f"&management_client_name={client_slug}"
                    "&management_channel=local_api"
                    "&management_action=clear_global_fixed_server"
                ),
            },
        ],
    }


def _external_connection_guide(system: dict[str, Any]) -> dict[str, Any] | None:
    connection_type = str(system.get("connection_type") or "external_management")
    if connection_type == "external_management":
        return _external_management_api_guide(system)
    if connection_type == "external_vpn_module":
        return _external_vpn_module_guide(system)
    if connection_type == "external_network_source":
        return _external_network_source_guide(system)
    return None


def _external_vpn_module_guide(system: dict[str, Any]) -> dict[str, Any]:
    identity = external_connection_identity(system)
    replacement_target = _normalize_replacement_target(system.get("replacement_target"))
    target = replacement_target or "mihomo"
    explicit_client_runtime = {
        "supported": "external_explicit_client_runtime_contract",
        "required_for_contract": ["controller_url or healthcheck_url"],
        "optional_endpoints": [
            "client_inventory_url",
            "subscription_base_url",
            "traffic_stats_url",
            "reload_url",
        ],
        "note": (
            "This marks the connection as a user-managed explicit-client runtime. "
            "FWRouter exposes identity, traffic collection, and status contract; "
            "runtime-specific client create/delete/proxy logic still belongs to an adapter."
        ),
    } if target == "xray" else None
    return {
        "connection_type": "external_vpn_module",
        "purpose": "User-managed runtime provides VPN egress endpoints; FWRouter does not own its lifecycle.",
        "identity": identity,
        "replacement_target": target,
        "routing_adapter": {
            "supported": "transparent_redir_tproxy",
            "required_for_dataplane": ["tcp_redir_port", "udp_tproxy_port"],
            "optional_full_vpn": ["full_tcp_redir_port", "full_udp_tproxy_port"],
            "note": "http_proxy_url and socks_proxy_url are metadata/explicit-proxy endpoints; nft transparent routing uses redir/tproxy ports.",
        },
        "explicit_client_runtime": explicit_client_runtime,
        "configure": {
            "role": "vpn_egress",
            "runtime_type": system.get("runtime_type") or "<runtime>",
            "location": system.get("location") or "manual",
            "address": system.get("address") or "<host/container/ip>",
            "endpoints": system.get("endpoints") or {},
            "capabilities": system.get("capabilities") or {},
            "integration_mode": system.get("integration_mode") or "api_push",
            "refresh_mode": system.get("refresh_mode") or "on_change",
            "collector_config": system.get("collector_config") or {},
        },
        "collection": _external_collection_guide(system),
        "available_elements": {
            "endpoints": [
                "http_proxy_url",
                "socks_proxy_url",
                "tcp_redir_port",
                "udp_tproxy_port",
                "controller_url",
                "healthcheck_url",
                "selector_state_url",
                "selector_failover_url",
                "client_inventory_url",
                "subscription_base_url",
                "traffic_stats_url",
                "reload_url",
            ],
            "capabilities": [
                "supports_tcp",
                "supports_udp",
                "supports_http_proxy",
                "supports_socks_proxy",
                "supports_transparent_proxy",
                "supports_selector_api",
                "supports_client_api",
                "supports_subscription_api",
                "supports_traffic_stats",
                "supports_reload",
            ],
        },
        "probe": {
            "method": "GET",
            "url": "healthcheck_url or controller_url, if provided",
            "expected_response": {
                "status": "ok|degraded|down",
                "runtime_type": system.get("runtime_type") or "<runtime>",
                "selected_node": "<optional-current-node>",
                "version": "<optional-version>",
                "details": {},
            },
        },
        "traffic_accounting": {
            "method": "POST",
            "path": "/traffic/collect",
            "body": {
                "requested_by": identity["requested_by"],
                "collector": identity["collector"],
                "samples": [
                    {
                        "counter_key": f"{identity['external_system_id']}:vpn",
                        "subject_id": "<existing-fwrouter-subject-id>",
                        "path": "vpn",
                        "rx_bytes": 0,
                        "tx_bytes": 0,
                        "metadata": {
                            "connection_id": identity["external_system_id"],
                            "connection_type": "external_vpn_module",
                            "source": "external_runtime_api",
                        },
                    }
                ],
            },
            "watchdog_note": (
                "Only external_vpn_module samples may declare metadata.watchdog_signal="
                "adapter_response when they report real response traffic."
            ),
        },
        "example_config_line": (
            "role=vpn_egress,"
            f"runtime_type={system.get('runtime_type') or '<runtime>'},"
            f"location={system.get('location') or 'manual'},"
            f"address={system.get('address') or '<host/container/ip>'}"
        ),
    }


def _external_network_source_guide(system: dict[str, Any]) -> dict[str, Any]:
    identity = external_connection_identity(system)
    return {
        "connection_type": "external_network_source",
        "purpose": "User-managed source provides client inventory, interface name, or client CIDR.",
        "identity": identity,
        "replacement_target": _normalize_replacement_target(system.get("replacement_target")),
        "configure": {
            "role": "client_source",
            "runtime_type": system.get("runtime_type") or "<source>",
            "location": system.get("location") or "manual",
            "address": system.get("address") or "<api/cli/interface>",
            "endpoints": system.get("endpoints") or {},
            "capabilities": system.get("capabilities") or {},
            "integration_mode": system.get("integration_mode") or "api_push",
            "refresh_mode": system.get("refresh_mode") or "on_change",
            "collector_config": system.get("collector_config") or {},
        },
        "collection": _external_collection_guide(system),
        "available_elements": {
            "endpoints": [
                "client_inventory_url",
                "interface_name",
                "client_cidr",
                "healthcheck_url",
            ],
            "capabilities": [
                "supports_client_inventory",
            ],
        },
        "probe": {
            "method": "GET",
            "url": "client_inventory_url or healthcheck_url, if provided",
            "expected_response": {
                "status": "ok|degraded|down",
                "clients": [
                    {
                        "id": "<stable-client-id>",
                        "label": "<display-name>",
                        "address": "<ip-or-cidr>",
                        "metadata": {},
                    }
                ],
            },
        },
        "traffic_accounting": {
            "method": "POST",
            "path": "/traffic/collect",
            "body": {
                "requested_by": identity["requested_by"],
                "collector": identity["collector"],
                "samples": [
                    {
                        "counter_key": f"{identity['external_system_id']}:<client-id>:vpn",
                        "subject_id": "<existing-fwrouter-subject-id>",
                        "path": "vpn",
                        "rx_bytes": 0,
                        "tx_bytes": 0,
                        "metadata": {
                            "connection_id": identity["external_system_id"],
                            "connection_type": "external_network_source",
                            "source": "external_inventory_api",
                        },
                    }
                ],
            },
            "note": "Registration/display is built in; automatic subject import still needs a provider adapter.",
        },
        "example_config_line": (
            "role=client_source,"
            f"runtime_type={system.get('runtime_type') or '<source>'},"
            f"location={system.get('location') or 'manual'},"
            f"address={system.get('address') or '<api/cli/interface>'}"
        ),
    }


def _external_collection_guide(system: dict[str, Any]) -> dict[str, Any]:
    integration_mode = _normalize_external_integration_mode(
        system.get("integration_mode"),
        str(system.get("connection_type") or ""),
    )
    refresh_mode = _normalize_external_refresh_mode(system.get("refresh_mode"), integration_mode)
    collector_config = _normalize_external_collector_config(
        system.get("collector_config"),
        integration_mode=integration_mode,
        refresh_mode=refresh_mode,
    )
    return {
        "integration_mode": integration_mode,
        "refresh_mode": refresh_mode,
        "collector_config": collector_config,
        "manual_refresh": {
            "method": "POST",
            "path": f"/ui/external-connections/{system.get('connection_id')}/collect",
            "body": {"dry_run": True},
        },
        "accepted_payload": {
            "status": "ok|ready|running|degraded|down",
            "details": {},
            "clients": [
                {
                    "id": "<stable-client-id>",
                    "label": "<display-name>",
                    "address": "<ip-or-cidr>",
                    "metadata": {},
                }
            ],
            "traffic_samples": [
                {
                    "counter_key": "<stable-counter>",
                    "subject_id": "<existing-fwrouter-subject-id>",
                    "path": "vpn|direct",
                    "rx_bytes": 0,
                    "tx_bytes": 0,
                    "metadata": {},
                }
            ],
        },
        "notes": [
            "api_push does not poll; the external system sends updates when its state changes.",
            "manual refresh runs only when called from UI/API.",
            "interval refresh is optional and should be used only when the external system cannot push changes.",
        ],
    }


def _external_connection_readiness(system: dict[str, Any]) -> dict[str, Any]:
    connection_type = str(system.get("connection_type") or "")
    missing: list[str] = []
    details: dict[str, Any] = {}
    integration_mode = _normalize_external_integration_mode(system.get("integration_mode"), connection_type)
    refresh_mode = _normalize_external_refresh_mode(system.get("refresh_mode"), integration_mode)
    collector_config = _normalize_external_collector_config(
        system.get("collector_config"),
        integration_mode=integration_mode,
        refresh_mode=refresh_mode,
    )
    details["integration_mode"] = integration_mode
    details["refresh_mode"] = refresh_mode
    if not str(system.get("label") or "").strip():
        missing.append("label")
    if connection_type in {"external_vpn_module", "external_network_source"} and not str(system.get("runtime_type") or "").strip():
        missing.append("runtime_type")
    if not str(system.get("location") or "").strip():
        missing.append("location")
    if integration_mode == "http_poll" and not collector_config.get("url"):
        missing.append("collector_url")
    if integration_mode == "command_probe" and not collector_config.get("script_id"):
        missing.append("collector_script_id")
    if integration_mode == "file_read" and not collector_config.get("path"):
        missing.append("collector_path")
    if connection_type == "external_vpn_module":
        endpoints = system.get("endpoints") if isinstance(system.get("endpoints"), dict) else {}
        capabilities = system.get("capabilities") if isinstance(system.get("capabilities"), dict) else {}
        replacement_target = _normalize_replacement_target(system.get("replacement_target")) or "mihomo"
        details["replacement_target"] = replacement_target
        details["tcp_redir_port_present"] = bool(endpoints.get("tcp_redir_port"))
        details["udp_tproxy_port_present"] = bool(endpoints.get("udp_tproxy_port"))
        details["healthcheck_configured"] = bool(endpoints.get("healthcheck_url"))
        has_proxy_endpoint = bool(endpoints.get("http_proxy_url") or endpoints.get("socks_proxy_url"))
        has_transparent_endpoint = bool(endpoints.get("tcp_redir_port") or endpoints.get("udp_tproxy_port"))
        if not has_proxy_endpoint and not has_transparent_endpoint:
            missing.append("proxy_or_transparent_endpoint")
        if replacement_target == "mihomo":
            if not endpoints.get("tcp_redir_port"):
                missing.append("tcp_redir_port")
            if not endpoints.get("udp_tproxy_port"):
                missing.append("udp_tproxy_port")
        if replacement_target == "xray":
            details["replacement_support"] = "explicit_client_runtime_contract"
            if not (endpoints.get("controller_url") or endpoints.get("healthcheck_url")):
                missing.append("controller_or_healthcheck_url")
        if not any(bool(value) for value in capabilities.values()):
            missing.append("capabilities")
        try:
            from fwrouter_api.services.runtime_adapters import active_runtime_adapter_for_replacement_target

            runtime_adapter = active_runtime_adapter_for_replacement_target(replacement_target)
        except Exception:
            runtime_adapter = None
        adapter_source = runtime_adapter.get("source") if isinstance(runtime_adapter, dict) else {}
        adapter_source = adapter_source if isinstance(adapter_source, dict) else {}
        active_connection_id = str(adapter_source.get("connection_id") or "")
        active_as_runtime_adapter = bool(
            active_connection_id
            and active_connection_id == str(system.get("connection_id") or "")
        )
        details["active_as_runtime_adapter"] = active_as_runtime_adapter
        details["runtime_adapter_role"] = (runtime_adapter or {}).get("role") if isinstance(runtime_adapter, dict) else None
        if active_as_runtime_adapter:
            details["active_adapter"] = {
                "role": (runtime_adapter or {}).get("role"),
                "adapter_id": (runtime_adapter or {}).get("adapter_id"),
                "connection_id": active_connection_id,
                "system_id": adapter_source.get("system_id"),
                "runtime_type": adapter_source.get("runtime_type"),
                "redir_port": adapter_source.get("redir_port"),
                "tproxy_port": adapter_source.get("tproxy_port"),
            }
    if connection_type == "external_network_source":
        endpoints = system.get("endpoints") if isinstance(system.get("endpoints"), dict) else {}
        discovered_count = int(system.get("count") or 0)
        builtin_discovered = not bool(system.get("custom")) and discovered_count > 0
        if (
            not builtin_discovered
            and not (endpoints.get("client_inventory_url") or endpoints.get("interface_name") or endpoints.get("client_cidr"))
        ):
            missing.append("client_source")
    return {
        "state": "active" if details.get("active_as_runtime_adapter") else ("ready" if not missing else "incomplete"),
        "missing_fields": missing,
        "details": details,
    }
