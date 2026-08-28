from __future__ import annotations

from typing import Any


NATIVE_INGRESS_SUBJECT_TYPES = frozenset({"lan"})
EXTERNAL_NETWORK_CLIENT_SUBJECT_TYPE = "external_network_client"
EXPLICIT_EXTERNAL_CLIENT_SUBJECT_TYPE = "explicit_external_client"
LEGACY_TRANSPARENT_INGRESS_SUBJECT_ALIASES = {
    "tailscale": EXTERNAL_NETWORK_CLIENT_SUBJECT_TYPE,
    "tailscale_node": EXTERNAL_NETWORK_CLIENT_SUBJECT_TYPE,
}
LEGACY_EXPLICIT_EXTERNAL_CLIENT_SUBJECT_ALIASES = {
    "xray": EXPLICIT_EXTERNAL_CLIENT_SUBJECT_TYPE,
}

EXTERNAL_INGRESS_PROVIDERS: dict[str, dict[str, Any]] = {
    "tailscale": {
        "provider": "tailscale",
        "module_concept": "tailscale",
        "subject_type": EXTERNAL_NETWORK_CLIENT_SUBJECT_TYPE,
        "implementation_kind": "tailscale",
        "display_label": "Tailscale",
        "subject_id_prefix": "tailscale-node:",
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

EXTERNAL_INGRESS_SUBJECT_TYPES = frozenset(
    str(provider["subject_type"])
    for provider in EXTERNAL_INGRESS_PROVIDERS.values()
)

TRANSPARENT_INGRESS_CLIENT_SUBJECT_TYPES = frozenset(
    {*NATIVE_INGRESS_SUBJECT_TYPES, *EXTERNAL_INGRESS_SUBJECT_TYPES}
)

EXPLICIT_EXTERNAL_CLIENT_PROVIDERS: dict[str, dict[str, Any]] = {
    "xray": {
        "provider": "xray",
        "subject_type": EXPLICIT_EXTERNAL_CLIENT_SUBJECT_TYPE,
        "implementation_kind": "xray",
        "runtime_binding": "xray_runtime_bindings",
        "identity_match_prefix": "xray-client",
        "traffic_source": "runtime_api",
        "transparent_dataplane_policy": False,
        "virtual_vpn_auto_override": True,
    },
}

EXPLICIT_EXTERNAL_CLIENT_SUBJECT_TYPES = frozenset(
    str(provider["subject_type"])
    for provider in EXPLICIT_EXTERNAL_CLIENT_PROVIDERS.values()
)

CLIENT_PLANE_SUBJECT_TYPES = frozenset(
    {*TRANSPARENT_INGRESS_CLIENT_SUBJECT_TYPES, *EXPLICIT_EXTERNAL_CLIENT_SUBJECT_TYPES}
)

SYSTEM_SCOPED_SUBJECT_TYPES = frozenset({"host", "docker"})
CONTROL_PLANE_DIRECT_SAFE_SUBJECT_TYPES = frozenset({"host", "docker", "fwrouter"})
LEGACY_TRANSPARENT_INGRESS_SUBJECT_TYPES = frozenset(LEGACY_TRANSPARENT_INGRESS_SUBJECT_ALIASES)
LEGACY_EXPLICIT_EXTERNAL_CLIENT_SUBJECT_TYPES = frozenset(
    LEGACY_EXPLICIT_EXTERNAL_CLIENT_SUBJECT_ALIASES
)

UI_ACTIVE_SUBJECT_TYPES = frozenset(
    {
        *CLIENT_PLANE_SUBJECT_TYPES,
        *LEGACY_TRANSPARENT_INGRESS_SUBJECT_TYPES,
        *LEGACY_EXPLICIT_EXTERNAL_CLIENT_SUBJECT_TYPES,
    }
)
SERVER_OVERRIDE_SUBJECT_TYPES = frozenset(
    {
        *TRANSPARENT_INGRESS_CLIENT_SUBJECT_TYPES,
        *LEGACY_TRANSPARENT_INGRESS_SUBJECT_TYPES,
        *EXPLICIT_EXTERNAL_CLIENT_SUBJECT_TYPES,
        *SYSTEM_SCOPED_SUBJECT_TYPES,
    }
)


def external_ingress_contracts() -> list[dict[str, Any]]:
    return [dict(provider) for provider in EXTERNAL_INGRESS_PROVIDERS.values()]


def external_ingress_contract(provider: str | None) -> dict[str, Any] | None:
    normalized = str(provider or "").strip().lower()
    contract = EXTERNAL_INGRESS_PROVIDERS.get(normalized)
    return dict(contract) if contract else None


def external_ingress_contract_by_module(module_concept: str | None) -> dict[str, Any] | None:
    normalized = str(module_concept or "").strip().lower()
    for contract in EXTERNAL_INGRESS_PROVIDERS.values():
        contract_module = str(
            contract.get("module_concept") or contract.get("provider") or ""
        ).strip().lower()
        if contract_module == normalized:
            return dict(contract)
    return None


def explicit_external_client_contracts() -> list[dict[str, Any]]:
    return [dict(provider) for provider in EXPLICIT_EXTERNAL_CLIENT_PROVIDERS.values()]


def transparent_ingress_contract(subject_type: str | None) -> dict[str, Any] | None:
    normalized = normalize_subject_type(subject_type)
    if normalized in NATIVE_INGRESS_SUBJECT_TYPES:
        return {
            "provider": "native_lan",
            "subject_type": normalized,
            "identity_kind": "ip_address",
        }
    for provider in EXTERNAL_INGRESS_PROVIDERS.values():
        if str(provider["subject_type"]) == normalized:
            return dict(provider)
    return None


def transparent_ingress_contract_for_subject(
    subject_type: str | None,
    implementation_kind: str | None,
) -> dict[str, Any] | None:
    normalized = normalize_subject_type(subject_type)
    if normalized in NATIVE_INGRESS_SUBJECT_TYPES:
        return transparent_ingress_contract(normalized)
    provider = str(implementation_kind or "").strip().lower()
    return external_ingress_contract(provider)


def external_network_source_display_contract(subject_type: str | None) -> dict[str, Any] | None:
    contract = transparent_ingress_contract(subject_type)
    if not contract or not contract.get("module_concept"):
        return None
    provider = str(contract.get("provider") or "").strip().lower()
    module_concept = str(contract.get("module_concept") or provider).strip().lower()
    if not provider or not module_concept:
        return None
    label = str(contract.get("display_label") or provider.replace("_", " ").title()).strip()
    return {
        "system_id": f"external-network-{module_concept}",
        "label": label,
        "runtime_type": provider,
        "description": str(
            contract.get("description")
            or f"External network source discovered from {label} inventory."
        ),
        "location": str(contract.get("location") or "host"),
        "integration_mode": str(contract.get("integration_mode") or "command_probe"),
        "refresh_mode": str(contract.get("refresh_mode") or "interval"),
        "collector_config": dict(contract.get("collector_config") or {}),
    }


def normalize_subject_type(subject_type: str | None) -> str:
    normalized = str(subject_type or "").strip().lower()
    return LEGACY_EXPLICIT_EXTERNAL_CLIENT_SUBJECT_ALIASES.get(
        normalized,
        LEGACY_TRANSPARENT_INGRESS_SUBJECT_ALIASES.get(normalized, normalized),
    )


def is_transparent_ingress_subject_type(
    subject_type: str | None,
    *,
    include_legacy: bool = True,
) -> bool:
    normalized = str(subject_type or "").strip().lower()
    if normalized in TRANSPARENT_INGRESS_CLIENT_SUBJECT_TYPES:
        return True
    return include_legacy and normalized in LEGACY_TRANSPARENT_INGRESS_SUBJECT_TYPES


def is_explicit_external_client_subject_type(subject_type: str | None) -> bool:
    return normalize_subject_type(subject_type) in EXPLICIT_EXTERNAL_CLIENT_SUBJECT_TYPES


def is_system_scoped_subject_type(subject_type: str | None) -> bool:
    return normalize_subject_type(subject_type) in SYSTEM_SCOPED_SUBJECT_TYPES


def subject_follows_global_mode(subject_type: str | None) -> bool:
    return is_transparent_ingress_subject_type(subject_type, include_legacy=True)


def explicit_external_client_contract(subject_type: str | None) -> dict[str, Any] | None:
    normalized = normalize_subject_type(subject_type)
    for provider in EXPLICIT_EXTERNAL_CLIENT_PROVIDERS.values():
        if str(provider["subject_type"]) == normalized:
            return dict(provider)
    return None


def explicit_external_client_contract_for_subject(
    subject_type: str | None,
    implementation_kind: str | None,
) -> dict[str, Any] | None:
    normalized = normalize_subject_type(subject_type)
    provider = str(implementation_kind or "").strip().lower()
    for contract in EXPLICIT_EXTERNAL_CLIENT_PROVIDERS.values():
        if (
            str(contract["subject_type"]) == normalized
            and str(contract.get("implementation_kind") or contract.get("provider") or "").strip().lower() == provider
        ):
            return dict(contract)
    return explicit_external_client_contract(normalized)


def explicit_external_client_runtime_binding(subject_type: str | None) -> str | None:
    contract = explicit_external_client_contract(subject_type)
    if contract is None:
        return None
    return str(contract.get("runtime_binding") or "").strip() or None


def explicit_external_client_allows_virtual_vpn_auto(subject_type: str | None) -> bool:
    contract = explicit_external_client_contract(subject_type)
    return bool(contract and contract.get("virtual_vpn_auto_override"))


def explicit_external_client_uses_transparent_policy(subject_type: str | None) -> bool:
    contract = explicit_external_client_contract(subject_type)
    return bool(contract and contract.get("transparent_dataplane_policy"))


def subject_needs_transparent_policy(subject_type: str | None) -> bool:
    if is_explicit_external_client_subject_type(subject_type):
        return explicit_external_client_uses_transparent_policy(subject_type)
    return True


def watchdog_nft_subject_counter_prefixes() -> tuple[str, ...]:
    subject_types = sorted(
        {
            *TRANSPARENT_INGRESS_CLIENT_SUBJECT_TYPES,
            *SYSTEM_SCOPED_SUBJECT_TYPES,
        }
    )
    return tuple(f"{subject_type}_" for subject_type in subject_types) + ("fwrouter_global_",)
