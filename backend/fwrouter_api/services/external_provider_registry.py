from __future__ import annotations

from typing import Any


EXTERNAL_INGRESS_PROVIDERS: dict[str, dict[str, Any]] = {
    "tailscale": {
        "provider": "tailscale",
        "module_concept": "tailscale",
        "subject_type": "external_network_client",
        "implementation_kind": "tailscale",
        "display_label": "Tailscale",
        "identity_kind": "tailscale_ip",
        "ingress_interface": "tailscale0",
        "payload_source_cidr": "100.64.0.0/10",
        "service_traffic_policy": "direct_immune",
        "location": "host",
        "integration_mode": "command_probe",
        "refresh_mode": "interval",
        "runtime_available_message": (
            "External ingress status is available through the allowlisted host probe."
        ),
        "collector_config": {
            "script_id": "tailscale_status",
            "interval_seconds": 3600,
            "timeout_seconds": 20,
            "apply_traffic": False,
        },
        "runtime_probe": {
            "script_id": "tailscale_status",
            "cache_key": "external_ingress.runtime.tailscale",
            "ttl_seconds": 5.0,
        },
        "status_mapping": {
            "self_field": "Self",
            "self_hostname_fields": ("HostName", "DNSName", "Name"),
            "self_address_fields": ("TailscaleIPs", "Addresses"),
            "self_online_field": "Online",
            "self_state_field": "BackendState",
            "peer_collection_fields": ("Peer", "Peers"),
            "peer_identity_fields": ("ID", "NodeID", "IDShort"),
            "peer_name_fields": ("HostName", "DNSName", "Name"),
            "peer_address_fields": ("TailscaleIPs", "Addresses"),
            "peer_user_fields": ("User", "UserName"),
            "peer_online_field": "Online",
            "peer_routing_hint_fields": (
                "through_fwrouter",
                "fwrouter_routed",
                "routed_via_server",
                "UsesExitNode",
                "ExitNode",
                "UsesThisServerAsExit",
            ),
        },
    },
}


EXPLICIT_EXTERNAL_CLIENT_PROVIDERS: dict[str, dict[str, Any]] = {
    "xray": {
        "provider": "xray",
        "subject_type": "explicit_external_client",
        "implementation_kind": "xray",
        "runtime_binding": "xray_runtime_bindings",
        "identity_match_prefix": "xray-client",
        "traffic_source": "runtime_api",
        "transparent_dataplane_policy": False,
        "virtual_vpn_auto_override": True,
    },
}


def external_ingress_provider_contracts() -> list[dict[str, Any]]:
    return [dict(provider) for provider in EXTERNAL_INGRESS_PROVIDERS.values()]


def external_ingress_provider_contract(provider: str | None) -> dict[str, Any] | None:
    normalized = str(provider or "").strip().lower()
    contract = EXTERNAL_INGRESS_PROVIDERS.get(normalized)
    return dict(contract) if contract else None


def explicit_external_client_provider_contracts() -> list[dict[str, Any]]:
    return [dict(provider) for provider in EXPLICIT_EXTERNAL_CLIENT_PROVIDERS.values()]
