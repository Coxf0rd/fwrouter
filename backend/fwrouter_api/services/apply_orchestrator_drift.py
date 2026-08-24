from __future__ import annotations

import json
from typing import Any

from fwrouter_api.adapters.mihomo import DEFAULT_MIHOMO_ADAPTER
from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import db_session
from fwrouter_api.services.apply import ApplyMode, run_apply_pipeline
from fwrouter_api.services.artifacts import write_job_json_artifact
from fwrouter_api.services.core_bypass import get_core_bypass_state
from fwrouter_api.services.custom_servers import VIRTUAL_XRAY_VPN_AUTO_SERVER_ID
from fwrouter_api.services.dataplane_global import (
    read_applied_manifest,
    read_effective_rules_artifact,
    validate_global_mode_request,
)
from fwrouter_api.services.dataplane_live import applied_nft_markers_match_live, probe_live_global_mode
from fwrouter_api.services.dataplane_status import build_runtime_enforcement_state, get_dataplane_capability
from fwrouter_api.services.external_vpn import external_vpn_mihomo_reconcile_skip
from fwrouter_api.services.global_mode_profiles import load_precompiled_global_mode_profile, materialize_precompiled_manifest
from fwrouter_api.services.jobs import get_job, touch_job_running
from fwrouter_api.services.logs import write_operational_log, write_technical_log
from fwrouter_api.services.mihomo_config import (
    mihomo_runtime_satisfies_routing,
    reconcile_mihomo_runtime,
    reconcile_mihomo_selective_default_fast,
    subject_selector_name,
)
from fwrouter_api.services.rules import (
    effective_rules_with_selective_default,
    finalize_manual_rules_apply,
    get_manual_rules_texts,
    mark_rules_job_failed,
    mark_rules_job_running,
    prepare_manual_rules_candidate,
    sync_active_selective_default,
)
from fwrouter_api.services.servers import (
    clear_subject_server_override,
    ensure_routing_global_state,
    get_routing_global_state,
    get_server,
    get_subject_server_override,
    set_subject_server_override,
    update_subject_server_override_apply_status,
)
from fwrouter_api.services.subject_policy import (
    ADMIN_MODES_BY_SUBJECT_TYPE,
    USER_MODES,
    USER_OVERRIDE_TTL_DAYS,
    enrich_subject_with_effective_state,
    get_routing_snapshot,
    get_subject_with_effective_state,
)
from fwrouter_api.services.subject_taxonomy import (
    SERVER_OVERRIDE_SUBJECT_TYPES,
    explicit_external_client_allows_virtual_vpn_auto,
    explicit_external_client_runtime_binding,
    is_explicit_external_client_subject_type,
    subject_follows_global_mode,
)
from fwrouter_api.services.subjects import get_subject, list_subjects
from fwrouter_api.services.xray import materialize_xray_runtime_bindings
from fwrouter_api.services.apply_orchestrator_constants import *


def _facade_attr(name: str) -> Any:
    from fwrouter_api.services import apply_orchestrator as orchestrator

    return getattr(orchestrator, name)


def _current_routing_drift(*, routing: dict[str, Any] | None = None) -> dict[str, Any]:
    resolved_routing = dict(routing or get_routing_snapshot() or {})
    live_probe = _facade_attr("probe_live_global_mode")()
    live_artifact_consistency = _facade_attr("_live_applied_nft_artifact_consistency")()
    expected_mode = str(
        resolved_routing.get("applied_mode") or resolved_routing.get("desired_mode") or "direct"
    ).strip().lower()
    expected_selective_default = str(
        resolved_routing.get("selective_default") or "direct"
    ).strip().lower()
    live_mode = str(live_probe.get("mode") or "unknown").strip().lower()
    live_selective_default = str(
        live_probe.get("selective_default") or "direct"
    ).strip().lower()
    matches_intent = bool(
        live_probe.get("ok")
        and live_mode == expected_mode
        and (
            expected_mode != "selective"
            or live_selective_default == expected_selective_default
        )
    )
    live_artifact_matches = not bool(live_artifact_consistency.get("detected"))
    drift_detected = not matches_intent or not live_artifact_matches
    drift_code = None
    if not matches_intent:
        drift_code = GLOBAL_ROUTING_DRIFT_CODE
    elif not live_artifact_matches:
        drift_code = LIVE_DATAPLANE_ARTIFACT_DRIFT_CODE
    return {
        "detected": drift_detected,
        "code": drift_code,
        "routing": resolved_routing,
        "expected_mode": expected_mode,
        "expected_selective_default": expected_selective_default,
        "live_probe": live_probe,
        "live_mode": live_mode,
        "live_selective_default": live_selective_default,
        "live_artifact_consistency": live_artifact_consistency,
    }


def _live_applied_nft_artifact_consistency() -> dict[str, Any]:
    settings = get_settings()
    dataplane_dir = settings.paths.generated_dir / "dataplane"
    applied_manifest_path = dataplane_dir / "applied-manifest.json"
    applied_nft_path = dataplane_dir / "applied.nft"
    if not applied_manifest_path.exists() or not applied_nft_path.exists():
        return {
            "detected": False,
            "checked": False,
            "reason": "applied_artifacts_missing",
            "applied_manifest_path": str(applied_manifest_path),
            "applied_nft_path": str(applied_nft_path),
        }

    consistency = _facade_attr("applied_nft_markers_match_live")(applied_nft_path)
    return {
        "detected": not bool(consistency.get("ok", True)),
        "checked": True,
        "applied_manifest_path": str(applied_manifest_path),
        "applied_nft_path": str(applied_nft_path),
        **consistency,
    }


def _applied_manifest_routing_drift(*, routing: dict[str, Any] | None = None) -> dict[str, Any]:
    resolved_routing = dict(routing or get_routing_snapshot() or {})
    applied_manifest = _facade_attr("read_applied_manifest")()
    if not (
        isinstance(applied_manifest, dict)
        and isinstance(applied_manifest.get("routing_global_state"), dict)
    ):
        return {
            "detected": False,
            "code": None,
            "routing": resolved_routing,
            "applied_manifest_routing": None,
            "mismatches": {},
        }

    manifest_routing = dict(applied_manifest.get("routing_global_state"))

    routing_keys = (
        "desired_mode",
        "applied_mode",
        "selective_default",
        "server_mode",
        "desired_fixed_server_id",
        "applied_fixed_server_id",
    )
    mismatches = {
        key: {
            "routing": resolved_routing.get(key),
            "applied_manifest": manifest_routing.get(key),
        }
        for key in routing_keys
        if resolved_routing.get(key) != manifest_routing.get(key)
    }

    return {
        "detected": bool(mismatches),
        "code": GLOBAL_ARTIFACT_DRIFT_CODE if mismatches else None,
        "routing": resolved_routing,
        "applied_manifest_routing": manifest_routing,
        "mismatches": mismatches,
    }


def _log_routing_drift(*, intent: str, requested_by: str, drift: dict[str, Any]) -> None:
    if not drift.get("detected"):
        return
    details = {
        "intent": intent,
        "requested_by": requested_by,
        "code": drift.get("code") or GLOBAL_ROUTING_DRIFT_CODE,
        "routing": drift.get("routing"),
        "expected_mode": drift.get("expected_mode"),
        "expected_selective_default": drift.get("expected_selective_default"),
        "live_mode": drift.get("live_mode"),
        "live_selective_default": drift.get("live_selective_default"),
        "live_probe": drift.get("live_probe"),
        "live_artifact_consistency": drift.get("live_artifact_consistency"),
    }
    _facade_attr("write_operational_log")(
        event_type="routing_live_drift_detected",
        level="warning",
        message="Persisted global routing state does not match live dataplane mode.",
        details=details,
    )
    _facade_attr("write_technical_log")(
        component="apply-orchestrator",
        level="warning",
        event_type="routing_live_drift_detected",
        message="Persisted global routing state does not match live dataplane mode.",
        details=details,
    )


def _log_artifact_drift(*, intent: str, requested_by: str, drift: dict[str, Any]) -> None:
    if not drift.get("detected"):
        return
    details = {
        "intent": intent,
        "requested_by": requested_by,
        "code": drift.get("code") or GLOBAL_ARTIFACT_DRIFT_CODE,
        "routing": drift.get("routing"),
        "applied_manifest_routing": drift.get("applied_manifest_routing"),
        "mismatches": drift.get("mismatches"),
    }
    _facade_attr("write_operational_log")(
        event_type="routing_artifact_drift_detected",
        level="warning",
        message="Applied routing manifest does not match persisted global routing state.",
        details=details,
    )
    _facade_attr("write_technical_log")(
        component="apply-orchestrator",
        level="warning",
        event_type="routing_artifact_drift_detected",
        message="Applied routing manifest does not match persisted global routing state.",
        details=details,
    )
