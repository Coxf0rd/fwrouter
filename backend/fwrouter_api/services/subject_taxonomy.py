from __future__ import annotations

from typing import Any


NATIVE_INGRESS_SUBJECT_TYPES = frozenset({"lan"})

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

EXPLICIT_EXTERNAL_CLIENT_SUBJECT_TYPES = frozenset({"xray"})

CLIENT_PLANE_SUBJECT_TYPES = frozenset(
    {*TRANSPARENT_INGRESS_CLIENT_SUBJECT_TYPES, *EXPLICIT_EXTERNAL_CLIENT_SUBJECT_TYPES}
)

SYSTEM_SCOPED_SUBJECT_TYPES = frozenset({"host", "docker"})
CONTROL_PLANE_DIRECT_SAFE_SUBJECT_TYPES = frozenset({"host", "docker", "fwrouter"})

UI_ACTIVE_SUBJECT_TYPES = frozenset({*CLIENT_PLANE_SUBJECT_TYPES, "tailscale"})


def managed_external_ingress_contracts() -> list[dict[str, Any]]:
    return [dict(provider) for provider in MANAGED_EXTERNAL_INGRESS_PROVIDERS.values()]
