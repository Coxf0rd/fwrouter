from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fwrouter_api.core.config import get_settings
from fwrouter_api.services.artifacts import atomic_copy_file, atomic_write_json
from fwrouter_api.services.dataplane_global import (
    build_dataplane_profile,
    build_global_preflight,
)
from fwrouter_api.services.dataplane_status import build_bypass_runtime_enforcement
from fwrouter_api.services.dataplane_status import DATAPLANE_CAPABILITY_NFT_OWNED_TABLE
from fwrouter_api.services.dataplane_status import ENFORCEMENT_LEVEL_OWNED_TABLE_READY
from fwrouter_api.services.dataplane_nft import (
    OWNED_TABLE,
    REQUIRED_CHAINS,
    utc_timestamp,
    write_candidate_artifacts,
)
from fwrouter_api.services.server_layout import SERVER_LAYOUT_CONTRACT_VERSION
from fwrouter_api.services.scoped_egress import summarize_scoped_subjects
from fwrouter_api.services.servers import ensure_routing_global_state
from fwrouter_api.services.subject_policy import (
    enrich_subject_with_effective_state,
    list_subjects_with_effective_state,
)


def _manifest_dir() -> Path:
    return get_settings().paths.generated_dir / "dataplane"


def _current_manifest_path() -> Path:
    return _manifest_dir() / "current-manifest.json"


def _job_manifest_path(job_id: str) -> Path:
    return get_settings().paths.jobs_dir / job_id / "dataplane-manifest.json"


def _json_size(value: Any) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False, sort_keys=True))
    except TypeError:
        return len(str(value))


def _summarize_mapping(value: dict[str, Any], *, label: str) -> dict[str, Any]:
    keys = sorted(str(key) for key in value.keys())

    return {
        "kind": label,
        "keys": keys[:50],
        "keys_count": len(keys),
        "approx_json_bytes": _json_size(value),
    }


def _summarize_sequence(value: list[Any], *, label: str) -> dict[str, Any]:
    return {
        "kind": label,
        "count": len(value),
        "approx_json_bytes": _json_size(value),
    }


def _bounded_subject_runtime(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    status = value.get("status")
    legacy_state = value.get("state")
    if legacy_state is None and status == "not_applicable":
        legacy_state = "disabled"
    return {
        "state": legacy_state,
        "status": status,
        "eligible": value.get("eligible"),
        "applied": value.get("applied"),
        "reason": value.get("reason"),
        "required_capability": value.get("required_capability"),
        "matcher": value.get("matcher"),
    }


def _bounded_enforcement(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {
        "capability": value.get("capability") or value.get("dataplane_capability"),
        "enforcement_level": value.get("enforcement_level"),
        "traffic_enforcement_guaranteed": value.get("traffic_enforcement_guaranteed"),
        "bypass_active": value.get("bypass_active"),
        "supported_modes": value.get("supported_modes"),
        "missing_runtime_requirements": value.get("missing_runtime_requirements"),
    }


def _bounded_extra(extra: dict[str, Any]) -> dict[str, Any]:
    bounded: dict[str, Any] = {}
    for key, value in extra.items():
        if key == "rules_effective" and isinstance(value, dict):
            bounded[key] = value
            bounded["rules_effective_summary"] = {
                "selective_default": value.get("selective_default"),
                "source_counts": value.get("source_counts"),
                "effective_counts": value.get("effective_counts"),
                "runtime_enforcement": _bounded_enforcement(value.get("runtime_enforcement")),
            }
        elif key == "core_bypass" and isinstance(value, dict):
            bounded[key] = {
                "enabled": bool(value.get("enabled")),
                "reason": value.get("reason"),
                "status": value.get("status"),
            }
        elif isinstance(value, dict):
            bounded[key] = _summarize_mapping(value, label=f"{key}_summary")
        elif isinstance(value, list):
            bounded[key] = _summarize_sequence(value, label=f"{key}_summary")
        else:
            bounded[key] = value
    return bounded


def _bounded_routing_summary(routing: dict[str, Any]) -> dict[str, Any]:
    return {
        "desired_mode": routing.get("desired_mode"),
        "applied_mode": routing.get("applied_mode"),
        "selective_default": routing.get("selective_default"),
        "server_mode": routing.get("server_mode"),
        "active_auto_server_id": routing.get("active_auto_server_id"),
        "desired_fixed_server_id": routing.get("desired_fixed_server_id"),
        "applied_fixed_server_id": routing.get("applied_fixed_server_id"),
        "updated_at": routing.get("updated_at"),
    }


def _requires_vpn_policy_routing(
    *,
    routing: dict[str, Any],
    subjects: list[dict[str, Any]],
    global_preflight: dict[str, Any],
    core_bypass_enabled: bool,
) -> bool:
    if core_bypass_enabled:
        return False

    routing_mode = str(routing.get("desired_mode") or routing.get("applied_mode") or "direct").strip().lower()
    selective_default = str(routing.get("selective_default") or "direct").strip().lower()
    selective_rules = global_preflight.get("selective_rules")
    selective_vpn_ready = bool(global_preflight.get("selective_vpn_ready"))
    selective_requires_vpn_runtime = bool(
        isinstance(selective_rules, dict) and selective_rules.get("requires_vpn_runtime")
    )
    selective_reaches_vpn = selective_vpn_ready and (
        selective_requires_vpn_runtime or selective_default == "vpn"
    )

    if routing_mode == "vpn":
        return True
    if routing_mode == "selective" and selective_reaches_vpn:
        return True

    for subject in subjects:
        if not bool(subject.get("is_active")):
            continue
        # Xray forced-VPN subjects use the explicit Xray runtime contour and must
        # not keep LAN transparent nft/policy-routing contracts alive on their own.
        if str(subject.get("subject_type") or "").strip().lower() == "xray":
            continue
        effective_state = subject.get("effective_state")
        if not isinstance(effective_state, dict):
            continue
        dataplane_path = str(
            effective_state.get("dataplane_path")
            or subject.get("dataplane_path")
            or ""
        ).strip().lower()
        if dataplane_path == "vpn":
            return True
        if dataplane_path == "selective" and selective_reaches_vpn:
            return True

    return False


def build_dataplane_manifest_from_state(
    *,
    plan_id: str,
    reason: str,
    routing: dict[str, Any],
    subjects: list[dict[str, Any]],
    input_data: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    global_preflight = build_global_preflight(
        routing=routing,
        effective_rules_artifact=(extra or {}).get("rules_effective")
        if isinstance((extra or {}).get("rules_effective"), dict)
        else None,
        require_runtime_verify=False,
    )
    extra_data = _bounded_extra(extra or {})
    core_bypass = (
        extra_data.get("core_bypass")
        if isinstance(extra_data.get("core_bypass"), dict)
        else None
    )
    if core_bypass and core_bypass.get("enabled"):
        runtime_enforcement = build_bypass_runtime_enforcement(
            preflight=global_preflight,
        )
    else:
        runtime_enforcement = {
            "dataplane_capability": DATAPLANE_CAPABILITY_NFT_OWNED_TABLE,
            "capability": DATAPLANE_CAPABILITY_NFT_OWNED_TABLE,
            "enforcement_level": ENFORCEMENT_LEVEL_OWNED_TABLE_READY,
            "traffic_enforcement_guaranteed": False,
            "supported_modes": {
                "direct": bool(global_preflight["can_enforce_global_direct"]),
                "selective": bool(global_preflight["can_enforce_global_selective"]),
                "vpn": bool(global_preflight["can_enforce_global_vpn"]),
            },
            "missing_runtime_requirements": list(global_preflight["missing"]),
            "profile": global_preflight["profile"],
            "bypass_active": False,
        }
    normalized_subjects = [
        enrich_subject_with_effective_state(
            subject,
            routing=routing,
            runtime_enforcement=runtime_enforcement,
            bypass_state=core_bypass if isinstance(core_bypass, dict) else {"enabled": False},
        )
        for subject in subjects
    ]
    manifest_subjects = [
        {
            "subject_id": subject["subject_id"],
            "subject_type": subject["subject_type"],
            "display_name": subject["display_name"],
            "desired_mode": subject["desired_mode"],
            "applied_mode": subject.get("applied_mode"),
            "runtime_state": subject["runtime_state"],
            "is_active": subject["is_active"],
            "effective_mode": subject["effective_state"]["effective_mode"],
            "mode_source": subject["effective_state"]["mode_source"],
            "dataplane_path": subject["effective_state"]["dataplane_path"],
            "selected_server_id": subject["effective_state"]["selected_server_id"],
            "selected_server_source": subject["effective_state"]["selected_server_source"],
            "enforcement": _bounded_enforcement(
                subject["effective_state"].get("runtime_enforcement", {})
            ),
            "scoped_runtime": _bounded_subject_runtime(
                subject["effective_state"].get("scoped_runtime", {})
            ),
        }
        for subject in normalized_subjects
    ]

    path_counts: dict[str, int] = {}
    for subject in manifest_subjects:
        dataplane_path = str(subject["dataplane_path"])
        path_counts[dataplane_path] = path_counts.get(dataplane_path, 0) + 1

    scoped_egress = summarize_scoped_subjects(normalized_subjects)
    routing_mode = str(routing.get("desired_mode") or routing.get("applied_mode") or "direct")
    vpn_contour = global_preflight.get("vpn_contour")
    requires_vpn_policy_routing = _requires_vpn_policy_routing(
        routing=routing,
        subjects=normalized_subjects,
        global_preflight=global_preflight,
        core_bypass_enabled=bool(core_bypass and core_bypass.get("enabled")),
    )
    global_preflight["vpn_policy_required"] = requires_vpn_policy_routing
    resolved_vpn_contour = None
    if isinstance(vpn_contour, dict) and (
        routing_mode in {"vpn", "selective"} or requires_vpn_policy_routing
    ):
        resolved_vpn_contour = dict(vpn_contour)
        resolved_vpn_contour["required"] = requires_vpn_policy_routing

    return {
        "contract_version": SERVER_LAYOUT_CONTRACT_VERSION,
        "plan_id": plan_id,
        "reason": reason,
        "generated_at": utc_timestamp(),
        "owned_table": OWNED_TABLE,
        "required_chains": list(REQUIRED_CHAINS),
        "dataplane_profile": global_preflight["profile"],
        "input": input_data or {},
        "routing_global_state": routing,
        "runtime_enforcement": runtime_enforcement,
        "global_preflight": global_preflight,
        "vpn_contour": resolved_vpn_contour,
        "core_bypass": core_bypass,
        "scoped_egress": scoped_egress,
        "subjects": manifest_subjects,
        "extra": extra_data,
        "summary": {
            "subjects_count": len(manifest_subjects),
            "active_subjects_count": sum(1 for subject in manifest_subjects if subject["is_active"]),
            "path_counts": path_counts,
            "global_mode": routing_mode,
            "selective_default": str(routing.get("selective_default") or "direct"),
            "routing": _bounded_routing_summary(routing),
            "core_bypass_enabled": bool(core_bypass and core_bypass.get("enabled")),
            "requires_vpn_policy_routing": requires_vpn_policy_routing,
            "scoped_egress_state": scoped_egress["state"],
            "scoped_egress_eligible_count": scoped_egress["eligible_count"],
            "scoped_egress_applied_count": scoped_egress["applied_count"],
            "extra_keys": sorted(extra_data.keys()),
        },
    }


def build_dataplane_manifest(
    *,
    plan_id: str,
    reason: str,
    input_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    routing = ensure_routing_global_state()
    subjects = list_subjects_with_effective_state(include_deleted=False, limit=1000)

    return build_dataplane_manifest_from_state(
        plan_id=plan_id,
        reason=reason,
        routing=routing,
        subjects=subjects,
        input_data=input_data,
    )


def write_dataplane_manifest(
    *,
    job_id: str,
    plan_id: str,
    manifest: dict[str, Any],
) -> dict[str, str]:
    artifact_paths = write_candidate_artifacts(
        job_id=job_id,
        apply_id=plan_id,
        manifest=manifest,
    )

    current_path = _current_manifest_path()
    versioned_path = _manifest_dir() / f"{plan_id}.json"
    job_path = _job_manifest_path(job_id)
    candidate_path = Path(artifact_paths["candidate_manifest_path"])

    atomic_copy_file(candidate_path, versioned_path)
    atomic_write_json(
        job_path,
        {
            "kind": "dataplane_manifest_summary",
            "plan_id": plan_id,
            "candidate_manifest_path": str(candidate_path),
            "versioned_manifest_path": str(versioned_path),
            "summary": manifest.get("summary") if isinstance(manifest.get("summary"), dict) else {},
        },
    )

    return {
        **artifact_paths,
        "current_manifest_path": str(current_path),
        "versioned_manifest_path": str(versioned_path),
        "job_manifest_path": str(job_path),
    }
