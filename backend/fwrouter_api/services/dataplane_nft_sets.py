from __future__ import annotations

from typing import Any, Callable

from fwrouter_api.core.config import get_settings
from fwrouter_api.services.dataplane_nft_constants import OWNED_TABLE


def _safe_set_suffix(value: str) -> str:
    return "".join(character.lower() if character.isalnum() else "_" for character in value).strip("_")

def _chunk_elements(elements: list[str], *, chunk_size: int = 512) -> list[list[str]]:
    return [elements[index : index + chunk_size] for index in range(0, len(elements), chunk_size)]

def _read_manifest_extra_ipv4_list(
    manifest: dict[str, Any] | None,
    key: str,
    *,
    default: tuple[str, ...] = (),
) -> list[str]:
    extra = manifest.get("extra") if isinstance(manifest, dict) else None
    values = extra.get(key) if isinstance(extra, dict) else None
    if not isinstance(values, list):
        return sorted(set(default))
    return sorted(
        {
            str(value).strip()
            for value in values
            if str(value).strip()
        }
    )

def _resolve_tproxy_handoff_ipv4(manifest: dict[str, Any] | None) -> str | None:
    global_preflight = manifest.get("global_preflight") if isinstance(manifest, dict) else None
    if not isinstance(global_preflight, dict):
        return None

    dnsmasq_status = global_preflight.get("dnsmasq_selective_status")
    if not isinstance(dnsmasq_status, dict):
        return None

    router_dns_ipv4 = dnsmasq_status.get("router_dns_ipv4")
    if isinstance(router_dns_ipv4, list):
        for candidate in router_dns_ipv4:
            value = str(candidate or "").strip()
            if value:
                return value

    dns_capture_status = dnsmasq_status.get("dns_capture_status")
    bindings = dns_capture_status.get("bindings") if isinstance(dns_capture_status, dict) else None
    if isinstance(bindings, list):
        for binding in bindings:
            if not isinstance(binding, dict):
                continue
            value = str(binding.get("address") or "").strip()
            if value:
                return value

    return None

def _resolve_lan_ingress_interfaces(manifest: dict[str, Any] | None) -> list[str]:
    global_preflight = manifest.get("global_preflight") if isinstance(manifest, dict) else None
    if not isinstance(global_preflight, dict):
        return []

    dnsmasq_status = global_preflight.get("dnsmasq_selective_status")
    if not isinstance(dnsmasq_status, dict):
        return []

    router_dns_interfaces = dnsmasq_status.get("router_dns_interfaces")
    if not isinstance(router_dns_interfaces, list):
        return []

    return sorted(
        {
            str(value).strip()
            for value in router_dns_interfaces
            if str(value).strip()
        }
    )

def _render_set(name: str, nft_type: str, elements: list[str]) -> tuple[list[str], list[str]]:
    if not elements:
        return [f"    set {name} {{ type {nft_type}; flags interval; auto-merge; }}"], []

    if len(elements) <= 256:
        lines = [
            f"    set {name} {{",
            f"        type {nft_type};",
            "        flags interval;",
            "        auto-merge;",
        ]
        lines.append("        elements = { " + ", ".join(elements) + " }")
        lines.append("    }")
        return lines, []

    lines = [
        f"    set {name} {{",
        f"        type {nft_type};",
        "        flags interval;",
        "        auto-merge;",
        "    }",
    ]
    add_commands = [f"flush set {OWNED_TABLE} {name}"]
    add_commands.extend(
        f"add element {OWNED_TABLE} {name} {{ " + ", ".join(chunk) + " }"
        for chunk in _chunk_elements(elements)
    )
    return lines, add_commands

def _render_dns_runtime_set(name: str, nft_type: str) -> list[str]:
    timeout_seconds = get_settings().dnsmasq_nftset_timeout_seconds
    return [
        f"    set {name} {{",
        f"        type {nft_type};",
        "        flags timeout;",
        f"        timeout {timeout_seconds}s;",
        "    }",
    ]

def _build_scoped_vpn_sets(manifest: dict[str, Any] | None) -> list[dict[str, Any]]:
    subjects = (manifest or {}).get("subjects") or []
    grouped: dict[str, dict[str, Any]] = {}

    for subject in subjects:
        if not isinstance(subject, dict):
            continue
        if subject.get("dataplane_path") != "vpn":
            continue
        scoped_runtime = subject.get("scoped_runtime")
        if not isinstance(scoped_runtime, dict):
            continue

        server_id = subject.get("selected_server_id") or "default"
        nft_type = str(scoped_runtime.get("nft_type") or "ipv4_addr")
        nft_expr = str(scoped_runtime.get("nft_expr") or "ip saddr")
        matcher_value = scoped_runtime.get("value")

        if not matcher_value:
            continue

        key = f"{server_id}:{nft_type}:{nft_expr}"
        if key not in grouped:
            grouped[key] = {
                "server_id": server_id,
                "nft_type": nft_type,
                "nft_expr": nft_expr,
                "values": [],
            }
        grouped[key]["values"].append(matcher_value)

    rendered: list[dict[str, Any]] = []
    for index, group in enumerate(grouped.values(), start=1):
        server_suffix = _safe_set_suffix(group["server_id"])[:24] or f"vpn_{index}"
        family_suffix = "v4" if group["nft_type"] == "ipv4_addr" else "v4" # We only do IPv4 for now
        rendered.append(
            {
                "set_name": f"scoped_{family_suffix}_{index}_{server_suffix}",
                "server_id": group["server_id"],
                "nft_type": group["nft_type"],
                "nft_expr": group["nft_expr"],
                "values": sorted(set(group["values"])),
            }
        )
    return rendered


def _resolve_rules_effective_artifact(
    manifest: dict[str, Any] | None,
    *,
    rules_effective_loader: Callable[[], dict[str, Any] | None],
) -> dict[str, Any] | None:
    if not isinstance(manifest, dict):
        return rules_effective_loader()

    extra = manifest.get("extra")
    manifest_rules = extra.get("rules_effective") if isinstance(extra, dict) else None
    if isinstance(manifest_rules, dict) and isinstance(manifest_rules.get("rules"), list):
        return manifest_rules

    return rules_effective_loader()
