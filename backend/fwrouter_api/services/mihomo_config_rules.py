from __future__ import annotations

import ipaddress
from typing import Any

from fwrouter_api.db.connection import db_session
from fwrouter_api.services.mihomo_config_paths import (
    FULL_VPN_RULE_NAME,
    TRANSPARENT_TPROXY_RULE_NAME,
    subject_selector_name,
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
                coalesce(
                    l.ip_address,
                    json_extract(s.metadata_json, '$.detail.provider_ip'),
                    json_extract(s.metadata_json, '$.detail.ip_address'),
                    json_extract(s.metadata_json, '$.detail.tailscale_ip'),
                    d.ip_address
                ) as ip,
                srv.server_name
            FROM subject_server_overrides o
            JOIN subjects s ON o.subject_id = s.subject_id
            JOIN servers srv ON o.selected_server_id = srv.server_id
            LEFT JOIN subject_lan l ON s.subject_id = l.subject_id
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
        "transparent_rule_name": TRANSPARENT_TPROXY_RULE_NAME,
        "full_vpn_rule_name": FULL_VPN_RULE_NAME,
    }
