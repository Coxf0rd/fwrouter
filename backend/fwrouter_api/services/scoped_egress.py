from __future__ import annotations

import json
from ipaddress import ip_address
from pathlib import Path
from typing import Any

from fwrouter_api.core.config import get_settings
from fwrouter_api.services.subject_taxonomy import (
    CLIENT_PLANE_SUBJECT_TYPES,
    CONTROL_PLANE_DIRECT_SAFE_SUBJECT_TYPES,
    SYSTEM_SCOPED_SUBJECT_TYPES,
    TRANSPARENT_INGRESS_CLIENT_SUBJECT_TYPES,
)


SCOPED_EGRESS_ELIGIBLE_SUBJECT_TYPES = {
    *CLIENT_PLANE_SUBJECT_TYPES,
    *SYSTEM_SCOPED_SUBJECT_TYPES,
}
SCOPED_EGRESS_ELIGIBLE_SOURCES = {"subject_override", "global_fixed", "vpn_auto"}
SCOPED_PENDING_STATUSES = {
    "pending_not_vpn_path",
    "pending_core_bypass",
    "pending_missing_vpn_runtime",
    "pending_unresolved_subject_match",
    "pending_inactive_subject",
    "pending_no_selected_server",
}
SCOPED_RUNTIME_BLOCKER_TRANSPARENT_TCP_UNHEALTHY = (
    "selective_materialized_but_transparent_tcp_unhealthy"
)

SELECTIVE_SCOPED_RUNTIME_SUBJECT_TYPES = TRANSPARENT_INGRESS_CLIENT_SUBJECT_TYPES


def _normalized_detail(subject: dict[str, Any]) -> dict[str, Any]:
    detail = subject.get("detail")
    return detail if isinstance(detail, dict) else {}


def _xray_bindings_path() -> Path:
    return get_settings().paths.state_dir / "xray" / "fwrouter-bindings.json"


def _load_xray_bindings() -> dict[str, dict[str, Any]]:
    path = _xray_bindings_path()
    if not path.exists():
        return {}

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}

    bindings = payload.get("bindings") if isinstance(payload, dict) else []
    if not isinstance(bindings, list):
        return {}

    result: dict[str, dict[str, Any]] = {}
    for binding in bindings:
        if not isinstance(binding, dict):
            continue
        subject_id = str(binding.get("subject_id") or "").strip()
        if subject_id:
            result[subject_id] = binding
    return result


def _matcher_from_subject(subject: dict[str, Any]) -> dict[str, Any]:
    subject_type = str(subject.get("subject_type") or "")
    detail = _normalized_detail(subject)

    if subject_type == "lan":
        candidate_ip = str(detail.get("ip_address") or "").strip()
        if candidate_ip:
            return _ip_matcher(candidate_ip, resolution_reason="subject_lan_ip")
        candidate_mac = str(detail.get("mac_address") or "").strip().lower()
        if candidate_mac:
            return {
                "resolved": False,
                "match_key": f"mac:{candidate_mac}",
                "resolution_reason": "subject_lan_mac_not_supported_in_v1",
            }
        return {
            "resolved": False,
            "match_key": None,
            "resolution_reason": "subject_lan_ip_missing",
        }

    if subject_type == "tailscale_node":
        candidate_ip = str(detail.get("tailscale_ip") or "").strip()
        if candidate_ip:
            return _ip_matcher(candidate_ip, resolution_reason="subject_tailscale_ip")
        candidate_node_id = str(detail.get("node_id") or "").strip()
        if candidate_node_id:
            return {
                "resolved": False,
                "match_key": f"node:{candidate_node_id}",
                "resolution_reason": "subject_tailscale_node_id_not_supported_in_v1",
            }
        return {
            "resolved": False,
            "match_key": None,
            "resolution_reason": "subject_tailscale_ip_missing",
        }

    if subject_type == "xray":
        client_uuid = str(detail.get("client_uuid") or "").strip()
        if client_uuid:
            return {
                "resolved": False,
                "match_key": f"xray-client-uuid:{client_uuid}",
                "resolution_reason": "subject_xray_client_uuid_runtime_binding_pending",
            }
        client_id = str(detail.get("client_id") or "").strip()
        if client_id:
            return {
                "resolved": False,
                "match_key": f"xray-client-id:{client_id}",
                "resolution_reason": "subject_xray_client_id_runtime_binding_pending",
            }
        return {
            "resolved": False,
            "match_key": None,
            "resolution_reason": "subject_xray_identity_missing",
        }

    if subject_type == "docker":
        candidate_ip = str(detail.get("ip_address") or "").strip()
        if candidate_ip:
            return _ip_matcher(candidate_ip, resolution_reason="subject_docker_ip")
        container_name = str(detail.get("container_name") or "").strip()
        if container_name:
            return {
                "resolved": False,
                "match_key": f"container:{container_name}",
                "resolution_reason": "subject_docker_ip_missing",
            }
        return {
            "resolved": False,
            "match_key": None,
            "resolution_reason": "subject_docker_identity_missing",
        }

    if subject_type == "host":
        systemd_unit = str(detail.get("systemd_unit") or "").strip()
        if systemd_unit:
            return {
                "resolved": False,
                "match_key": f"systemd:{systemd_unit}",
                "resolution_reason": "subject_host_process_only_match_not_supported_in_v1",
            }
        process_name = str(detail.get("process_name") or "").strip()
        if process_name:
            return {
                "resolved": False,
                "match_key": f"process:{process_name}",
                "resolution_reason": "subject_host_process_only_match_not_supported_in_v1",
            }
        return {
            "resolved": False,
            "match_key": None,
            "resolution_reason": "subject_host_identity_missing",
        }

    return {
        "resolved": False,
        "match_key": None,
        "resolution_reason": "subject_type_not_supported",
    }


def _ip_matcher(candidate_ip: str, *, resolution_reason: str) -> dict[str, Any]:
    parsed = ip_address(candidate_ip)
    if parsed.version == 4:
        return {
            "resolved": True,
            "match_key": f"ip:{candidate_ip}",
            "resolution_reason": resolution_reason,
            "matcher": {
                "family": "ipv4",
                "nft_type": "ipv4_addr",
                "nft_expr": "ip saddr",
                "value": candidate_ip,
            },
        }
    return {
        "resolved": True,
        "match_key": f"ip:{candidate_ip}",
        "resolution_reason": resolution_reason,
        "matcher": {
            "family": "ipv6",
            "nft_type": "ipv6_addr",
            "nft_expr": "ip6 saddr",
            "value": candidate_ip,
        },
    }


def build_scoped_subject_runtime(
    subject: dict[str, Any],
    *,
    dataplane_path: str,
    selected_server_id: str | None,
    selected_server_source: str | None,
    vpn_target_id: str | None = None,
    vpn_target_source: str | None = None,
    server_override: dict[str, Any] | None,
    vpn_supported: bool,
    bypass_enabled: bool,
) -> dict[str, Any]:
    subject_id = str(subject.get("subject_id") or "")
    subject_type = str(subject.get("subject_type") or "")
    xray_bindings = _load_xray_bindings() if subject_type == "xray" else {}
    xray_binding = xray_bindings.get(subject_id)
    tracked = _is_tracked_subject(
        subject_type=subject_type,
        dataplane_path=dataplane_path,
        server_override=server_override,
    )
    subject_role = (
        "client_plane"
        if subject_type in CLIENT_PLANE_SUBJECT_TYPES
        else "system_control"
        if subject_type in CONTROL_PLANE_DIRECT_SAFE_SUBJECT_TYPES
        else "unknown"
    )
    matcher_state = _matcher_from_subject(subject)
    inventory_classification = _inventory_classification(
        subject_type=subject_type,
        dataplane_path=dataplane_path,
        server_override=server_override,
    )

    result = {
        "tracked": tracked,
        "eligible": False,
        "applied": False,
        "status": "not_applicable",
        "subject_id": subject_id,
        "subject_type": subject_type,
        "dataplane_path": dataplane_path,
        "selected_server_id": selected_server_id,
        "selected_server_source": selected_server_source,
        "vpn_target_id": vpn_target_id,
        "vpn_target_source": vpn_target_source,
        "match_key": matcher_state.get("match_key"),
        "resolution_reason": matcher_state.get("resolution_reason"),
        "matcher": matcher_state.get("matcher"),
        "vpn_supported": vpn_supported,
        "bypass_active": bypass_enabled,
        "materialized_by": None,
        "subject_role": subject_role,
        "inventory_classification": inventory_classification,
        "control_plane_direct_safe": subject_type in CONTROL_PLANE_DIRECT_SAFE_SUBJECT_TYPES,
    }

    resolved_vpn_target_id = vpn_target_id if vpn_target_id is not None else selected_server_id
    resolved_vpn_target_source = (
        vpn_target_source if vpn_target_source is not None else selected_server_source
    )
    result["selected_server_id"] = resolved_vpn_target_id
    result["selected_server_source"] = resolved_vpn_target_source

    if (
        subject_type == "xray"
        and dataplane_path == "vpn"
        and resolved_vpn_target_source == "vpn_auto"
        and resolved_vpn_target_id is None
    ):
        resolved_vpn_target_id = "vpn-global"
        result["vpn_target_id"] = resolved_vpn_target_id
        result["selected_server_id"] = resolved_vpn_target_id

    if subject_type not in SCOPED_EGRESS_ELIGIBLE_SUBJECT_TYPES:
        result["resolution_reason"] = "subject_type_not_supported"
        return result

    if server_override is not None and dataplane_path != "vpn":
        result["status"] = "pending_not_vpn_path"
        result["resolution_reason"] = "subject_override_saved_but_subject_not_in_vpn_path"
        return result

    if not _uses_scoped_runtime_path(subject_type=subject_type, dataplane_path=dataplane_path):
        result["status"] = "not_applicable"
        result["resolution_reason"] = "subject_not_in_vpn_path"
        return result

    selective_subject_runtime = (
        dataplane_path == "selective"
        and subject_type in SELECTIVE_SCOPED_RUNTIME_SUBJECT_TYPES
    )

    if selective_subject_runtime:
        if not bool(subject.get("is_active")):
            result["eligible"] = False
            result["status"] = "pending_inactive_subject"
            result["resolution_reason"] = "subject_inactive"
            return result

        result["eligible"] = True

        if not vpn_supported:
            result["status"] = "pending_missing_vpn_runtime"
            result["resolution_reason"] = "vpn_runtime_not_ready"
            return result

        if not matcher_state.get("resolved", False):
            result["status"] = "pending_unresolved_subject_match"
            return result

        if bypass_enabled:
            result["status"] = "pending_core_bypass"
            result["resolution_reason"] = "core_bypass_active"
            return result

        result["applied"] = True
        result["status"] = "applied"
        result["resolution_reason"] = "subject_selective_runtime_materialized"
        result["materialized_by"] = "nft_subject_classify"
        return result

    if resolved_vpn_target_id is None or resolved_vpn_target_source not in SCOPED_EGRESS_ELIGIBLE_SOURCES:
        result["status"] = "pending_no_selected_server"
        result["resolution_reason"] = "vpn_path_has_no_selected_server"
        return result

    if not bool(subject.get("is_active")):
        # Inactive subjects are tracked for inventory visibility, but they do
        # not require a live/materialized scoped egress binding and therefore
        # must not degrade scoped-egress readiness.
        result["eligible"] = False
        result["status"] = "pending_inactive_subject"
        result["resolution_reason"] = "subject_inactive"
        return result

    result["eligible"] = True

    if subject_type == "xray":
        expected_server_id = (
            str(resolved_vpn_target_id) if resolved_vpn_target_id is not None else None
        )
        # Xray vpn_auto is materialized through Mihomo's stable transparent
        # selector handoff, not through the current concrete active_auto_server_id.
        # The concrete auto target may change inside Mihomo while Xray keeps using
        # the vpn-global selector listener.
        if resolved_vpn_target_source == "vpn_auto":
            expected_server_id = "vpn-global"
        materialized_server_id = (
            str(xray_binding.get("selected_server_id"))
            if isinstance(xray_binding, dict) and xray_binding.get("selected_server_id") is not None
            else None
        )
        binding_status = str((xray_binding or {}).get("status") or "")
        if binding_status == "applied" and expected_server_id and materialized_server_id == expected_server_id:
            result["applied"] = True
            result["status"] = "applied"
            result["resolution_reason"] = "subject_xray_binding_materialized"
            result["materialized_by"] = "xray_runtime_metadata"
            return result
        result["status"] = "pending_unresolved_subject_match"
        result["resolution_reason"] = "subject_xray_runtime_binding_missing"
        return result

    if not vpn_supported:
        result["status"] = "pending_missing_vpn_runtime"
        result["resolution_reason"] = "vpn_runtime_not_ready"
        return result

    if not matcher_state.get("resolved", False):
        result["status"] = "pending_unresolved_subject_match"
        return result

    if bypass_enabled:
        result["status"] = "pending_core_bypass"
        result["resolution_reason"] = "core_bypass_active"
        return result

    result["applied"] = True
    result["status"] = "applied"
    return result


def _is_tracked_subject(
    *,
    subject_type: str,
    dataplane_path: str,
    server_override: dict[str, Any] | None,
) -> bool:
    if subject_type == "xray":
        return dataplane_path == "vpn" or server_override is not None
    return subject_type in SCOPED_EGRESS_ELIGIBLE_SUBJECT_TYPES and (
        _uses_scoped_runtime_path(subject_type=subject_type, dataplane_path=dataplane_path)
        or server_override is not None
    )


def _inventory_classification(
    *,
    subject_type: str,
    dataplane_path: str,
    server_override: dict[str, Any] | None,
) -> str:
    if subject_type == "fwrouter":
        return "control_plane_direct_safe"
    if subject_type in SYSTEM_SCOPED_SUBJECT_TYPES and dataplane_path != "vpn" and server_override is None:
        return "control_plane_direct_safe"
    if subject_type in SCOPED_EGRESS_ELIGIBLE_SUBJECT_TYPES and (
        _uses_scoped_runtime_path(subject_type=subject_type, dataplane_path=dataplane_path)
        or server_override is not None
    ):
        return "eligible_for_scoped_vpn"
    return "tracked_only"


def _uses_scoped_runtime_path(*, subject_type: str, dataplane_path: str) -> bool:
    if dataplane_path == "vpn":
        return True
    return (
        dataplane_path == "selective"
        and subject_type in SELECTIVE_SCOPED_RUNTIME_SUBJECT_TYPES
    )


def summarize_scoped_subjects(subjects: list[dict[str, Any]]) -> dict[str, Any]:
    bindings: list[dict[str, Any]] = []
    eligible_count = 0
    applied_count = 0
    pending_count = 0
    unresolved_count = 0
    status_counts: dict[str, int] = {}
    source_counts: dict[str, int] = {}
    inventory_counts = {
        "eligible_for_scoped_vpn": 0,
        "tracked_only": 0,
        "control_plane_direct_safe": 0,
    }

    for subject in subjects:
        effective_state = subject.get("effective_state")
        if not isinstance(effective_state, dict):
            continue
        scoped_runtime = effective_state.get("scoped_runtime")
        if not isinstance(scoped_runtime, dict):
            continue
        inventory_classification = str(scoped_runtime.get("inventory_classification") or "tracked_only")
        inventory_counts[inventory_classification] = inventory_counts.get(inventory_classification, 0) + 1
        if not bool(scoped_runtime.get("tracked")):
            continue

        binding = {
            "subject_id": subject["subject_id"],
            "subject_type": subject["subject_type"],
            "display_name": subject["display_name"],
            "eligible": bool(scoped_runtime.get("eligible")),
            "applied": bool(scoped_runtime.get("applied")),
            "status": str(scoped_runtime.get("status") or "not_applicable"),
            "selected_server_id": scoped_runtime.get("selected_server_id"),
            "selected_server_source": scoped_runtime.get("selected_server_source"),
            "match_key": scoped_runtime.get("match_key"),
            "resolution_reason": scoped_runtime.get("resolution_reason"),
            "matcher": scoped_runtime.get("matcher"),
            "subject_role": scoped_runtime.get("subject_role"),
            "inventory_classification": inventory_classification,
        }
        bindings.append(binding)
        status_counts[binding["status"]] = status_counts.get(binding["status"], 0) + 1
        source = str(binding["selected_server_source"] or "none")
        source_counts[source] = source_counts.get(source, 0) + 1

        if binding["eligible"]:
            eligible_count += 1
        if binding["applied"]:
            applied_count += 1
        if binding["eligible"] and not binding["applied"] and binding["status"].startswith("pending_"):
            pending_count += 1
        if binding["status"] == "pending_unresolved_subject_match":
            unresolved_count += 1

    if eligible_count == 0:
        state = "disabled"
    elif applied_count == eligible_count:
        state = "active"
    else:
        state = "degraded"

    return {
        "state": state,
        "eligible_count": eligible_count,
        "applied_count": applied_count,
        "pending_count": pending_count,
        "unresolved_count": unresolved_count,
        "status_counts": status_counts,
        "source_counts": source_counts,
        "inventory_counts": inventory_counts,
        "bindings": bindings,
    }


def build_scoped_egress_diagnostics(
    *,
    summary: dict[str, Any],
    runtime_enforcement: dict[str, Any],
    bypass: dict[str, Any],
) -> dict[str, Any]:
    bindings = summary.get("bindings") if isinstance(summary.get("bindings"), list) else []
    pending_bindings = [
        binding
        for binding in bindings
        if str(binding.get("status") or "") in SCOPED_PENDING_STATUSES
    ]
    unresolved_bindings = [
        binding
        for binding in bindings
        if str(binding.get("status") or "") == "pending_unresolved_subject_match"
    ]
    applied_bindings = [
        binding
        for binding in bindings
        if bool(binding.get("applied"))
    ]

    blockers: list[dict[str, Any]] = []
    active_mode_matches_intent = bool(runtime_enforcement.get("active_mode_matches_intent", True))
    if bool(bypass.get("enabled")):
        blockers.append(
            {
                "code": "CORE_BYPASS_ACTIVE",
                "severity": "warning",
                "message": "Core bypass suppresses scoped egress enforcement.",
            }
        )
    if not active_mode_matches_intent:
        blockers.append(
            {
                "code": "LIVE_DATAPLANE_MODE_MISMATCH",
                "severity": "warning",
                "message": "Live nftables dataplane does not match the intended FWRouter mode.",
                "live_global_mode": runtime_enforcement.get("live_global_mode"),
            }
        )
    if not bool((runtime_enforcement.get("supported_modes") or {}).get("vpn", False)):
        blockers.append(
            {
                "code": "VPN_RUNTIME_NOT_READY",
                "severity": "warning",
                "message": "Global VPN runtime prerequisites are not ready for scoped egress.",
            }
        )
    dataplane_profile = runtime_enforcement.get("profile") if isinstance(runtime_enforcement.get("profile"), dict) else {}
    mihomo_profile = dataplane_profile.get("mihomo") if isinstance(dataplane_profile.get("mihomo"), dict) else {}
    mihomo_contours = mihomo_profile.get("contours") if isinstance(mihomo_profile.get("contours"), dict) else {}
    selective_bindings = [
        binding
        for binding in applied_bindings
        if str(binding.get("subject_type") or "") in SELECTIVE_SCOPED_RUNTIME_SUBJECT_TYPES
    ]
    transparent_tcp_ready = bool(mihomo_contours.get("transparent_tcp_ready"))
    if selective_bindings and not transparent_tcp_ready:
        blockers.append(
            {
                "code": "SELECTIVE_TRANSPARENT_TCP_UNHEALTHY",
                "severity": "warning",
                "status": SCOPED_RUNTIME_BLOCKER_TRANSPARENT_TCP_UNHEALTHY,
                "message": "Selective LAN/Tailscale subjects are materialized, but transparent TCP ingress is not healthy.",
                "count": len(selective_bindings),
            }
        )
    if unresolved_bindings:
        blockers.append(
            {
                "code": "UNRESOLVED_SUBJECT_MATCHES",
                "severity": "warning",
                "message": "Some scoped subjects do not have a supported runtime match key yet.",
                "count": len(unresolved_bindings),
            }
        )

    recommendations: list[str] = []
    if unresolved_bindings:
        recommendations.append(
            "Resolve LAN/Tailscale match keys or add Xray runtime binding support before relying on scoped subject-specific egress."
        )
    if not bool((runtime_enforcement.get("supported_modes") or {}).get("vpn", False)):
        recommendations.append(
            "Validate Mihomo TProxy and global VPN runtime prerequisites on the Linux server."
        )
    if selective_bindings and not transparent_tcp_ready:
        recommendations.append(
            "Verify the fwrouter-redir TCP listener and a materialized Mihomo transparent TCP session before trusting selective LAN/Tailscale VPN rules."
        )
    if bool(bypass.get("enabled")):
        recommendations.append(
            "Disable core bypass before expecting scoped egress bindings to materialize."
        )

    return {
        **summary,
        "applied_bindings_sample": applied_bindings[:10],
        "pending_bindings_sample": pending_bindings[:10],
        "unresolved_bindings_sample": unresolved_bindings[:10],
        "blockers": blockers,
        "recommendations": recommendations,
    }


def build_scoped_egress_readiness(
    *,
    diagnostics: dict[str, Any],
    runtime_enforcement: dict[str, Any],
    bypass: dict[str, Any],
) -> dict[str, Any]:
    eligible_count = int(diagnostics.get("eligible_count") or 0)
    applied_count = int(diagnostics.get("applied_count") or 0)
    unresolved_count = int(diagnostics.get("unresolved_count") or 0)
    pending_count = int(diagnostics.get("pending_count") or 0)
    vpn_supported = bool((runtime_enforcement.get("supported_modes") or {}).get("vpn", False))
    bypass_enabled = bool(bypass.get("enabled"))
    active_mode_matches_intent = bool(runtime_enforcement.get("active_mode_matches_intent", True))

    checks = [
        {
            "name": "vpn_runtime_supported",
            "ok": vpn_supported,
            "message": (
                "Scoped egress can use VPN runtime prerequisites."
                if vpn_supported
                else "Scoped egress still depends on missing VPN runtime prerequisites."
            ),
        },
        {
            "name": "core_bypass_inactive",
            "ok": not bypass_enabled,
            "message": (
                "Core bypass is inactive."
                if not bypass_enabled
                else "Core bypass is active and suppresses scoped enforcement."
            ),
        },
        {
            "name": "live_dataplane_matches_intent",
            "ok": active_mode_matches_intent,
            "message": (
                "Live dataplane matches the intended FWRouter mode."
                if active_mode_matches_intent
                else "Live dataplane is stale or in an unintended bypass/mode."
            ),
        },
        {
            "name": "subject_matches_resolved",
            "ok": unresolved_count == 0,
            "message": (
                "All tracked scoped subjects have supported match keys."
                if unresolved_count == 0
                else f"{unresolved_count} tracked scoped subjects still have unresolved match keys."
            ),
        },
        {
            "name": "eligible_bindings_materialized",
            "ok": eligible_count == 0 or applied_count == eligible_count,
            "message": (
                "All eligible scoped bindings are materialized."
                if eligible_count == 0 or applied_count == eligible_count
                else f"{applied_count}/{eligible_count} eligible scoped bindings are materialized."
            ),
        },
    ]

    if bypass_enabled or not vpn_supported or not active_mode_matches_intent:
        state = "blocked"
    elif eligible_count == 0 and pending_count == 0:
        state = "ready"
    elif unresolved_count > 0 or applied_count < eligible_count:
        state = "degraded"
    else:
        state = "ready"

    return {
        "state": state,
        "ready_for_server_rollout": state == "ready",
        "checks": checks,
        "tracked_subjects_count": len(diagnostics.get("bindings") or []),
        "eligible_count": eligible_count,
        "applied_count": applied_count,
        "pending_count": pending_count,
        "unresolved_count": unresolved_count,
    }
