from __future__ import annotations

from typing import Any


NATIVE_INGRESS_SUBJECT_TYPES = frozenset({"lan"})
LEGACY_TRANSPARENT_INGRESS_SUBJECT_ALIASES = {
    "tailscale": "tailscale_node",
}

MANAGED_EXTERNAL_INGRESS_PROVIDERS: dict[str, dict[str, Any]] = {
    "tailscale": {
        "provider": "tailscale",
        "module_concept": "tailscale",
        "subject_type": "tailscale_node",
        "subject_id_prefix": "tailscale-node:",
        "identity_kind": "tailscale_ip",
        "ingress_interface": "tailscale0",
        "payload_source_cidr": "100.64.0.0/10",
        "service_traffic_policy": "direct_immune",
    },
}

MANAGED_EXTERNAL_INGRESS_SUBJECT_TYPES = frozenset(
    str(provider["subject_type"])
    for provider in MANAGED_EXTERNAL_INGRESS_PROVIDERS.values()
)

TRANSPARENT_INGRESS_CLIENT_SUBJECT_TYPES = frozenset(
    {*NATIVE_INGRESS_SUBJECT_TYPES, *MANAGED_EXTERNAL_INGRESS_SUBJECT_TYPES}
)

EXPLICIT_EXTERNAL_CLIENT_PROVIDERS: dict[str, dict[str, Any]] = {
    "xray": {
        "provider": "xray",
        "subject_type": "xray",
        "runtime_binding": "xray_runtime_bindings",
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

UI_ACTIVE_SUBJECT_TYPES = frozenset({*CLIENT_PLANE_SUBJECT_TYPES, "tailscale"})
SERVER_OVERRIDE_SUBJECT_TYPES = frozenset(
    {
        *TRANSPARENT_INGRESS_CLIENT_SUBJECT_TYPES,
        *LEGACY_TRANSPARENT_INGRESS_SUBJECT_TYPES,
        *EXPLICIT_EXTERNAL_CLIENT_SUBJECT_TYPES,
        *SYSTEM_SCOPED_SUBJECT_TYPES,
    }
)


def managed_external_ingress_contracts() -> list[dict[str, Any]]:
    return [dict(provider) for provider in MANAGED_EXTERNAL_INGRESS_PROVIDERS.values()]


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
    for provider in MANAGED_EXTERNAL_INGRESS_PROVIDERS.values():
        if str(provider["subject_type"]) == normalized:
            return dict(provider)
    return None


def normalize_subject_type(subject_type: str | None) -> str:
    normalized = str(subject_type or "").strip().lower()
    return LEGACY_TRANSPARENT_INGRESS_SUBJECT_ALIASES.get(normalized, normalized)


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
