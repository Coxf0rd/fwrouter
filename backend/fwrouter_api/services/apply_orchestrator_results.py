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


def _base_result(
    *,
    intent: str,
    job_id: str,
    stage: str,
    ok: bool,
    requested_by: str,
    apply_id: str | None = None,
    code: str | None = None,
    message: str | None = None,
    runtime_state_unchanged: bool = True,
) -> dict[str, Any]:
    try:
        runtime_enforcement = build_runtime_enforcement_state()
        capability = get_dataplane_capability()
    except Exception as exc:
        write_technical_log(
            component="apply-orchestrator",
            level="warning",
            event_type="runtime_enforcement_probe_failed",
            message="Failed to collect runtime enforcement diagnostics while building mutation result.",
            details={
                "intent": intent,
                "job_id": job_id,
                "stage": stage,
                "error": str(exc),
            },
        )
        runtime_enforcement = {
            "dataplane_capability": "unknown",
            "capability": "unknown",
            "enforcement_level": "runtime_probe_failed",
            "traffic_enforcement_guaranteed": False,
        }
        capability = "unknown"
    return {
        "ok": ok,
        "intent": intent,
        "job_id": job_id,
        "apply_id": apply_id,
        "requested_by": requested_by,
        "stage": stage,
        "code": code,
        "message": message,
        "runtime_state_unchanged": runtime_state_unchanged,
        "enforcement_level": runtime_enforcement["enforcement_level"],
        "dataplane_capability": capability,
        "traffic_enforcement_guaranteed": runtime_enforcement["traffic_enforcement_guaranteed"],
    }


def _scoped_runtime_message(status: str) -> str:
    if status == "applied":
        return "Applied"
    if status == "pending_inactive_subject":
        return "Pending (subject inactive)"
    if status == "pending_not_vpn_path":
        return "Pending (not in VPN path)"
    if status == "pending_unresolved_server":
        return "Pending (server unresolved)"
    return f"Pending ({status})"


def _scoped_runtime_error_code(status: str) -> str | None:
    if status == "applied":
        return None
    return f"SCOPED_RUNTIME_{status.upper()}"


def _log_mutation_result(result: dict[str, Any]) -> None:
    dedupe_key = None
    cooldown_seconds = None
    if result.get("ok") and result.get("intent") == INTENT_SET_GLOBAL_MODE:
        routing = result.get("routing") if isinstance(result.get("routing"), dict) else {}
        dedupe_key = json.dumps(
            {
                "intent": result.get("intent"),
                "mode": routing.get("applied_mode") or routing.get("desired_mode"),
                "selective_default": routing.get("selective_default"),
                "server_id": routing.get("active_server_id") or routing.get("server_id"),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        cooldown_seconds = 20

    write_operational_log(
        event_type=f"mutation_{result['intent']}_{'success' if result['ok'] else 'failed'}",
        level="info" if result["ok"] else "error",
        message=result["message"] or f"Mutation {result['intent']} successful." if result["ok"] else result["message"] or f"Mutation {result['intent']} failed.",
        details=result,
        dedupe_key=dedupe_key,
        cooldown_seconds=cooldown_seconds,
    )


def _build_failure_result(
    *,
    intent: str,
    job_id: str,
    requested_by: str,
    stage: str,
    code: str,
    message: str,
    apply_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return _base_result(
        intent=intent,
        job_id=job_id,
        stage=stage,
        ok=False,
        requested_by=requested_by,
        apply_id=apply_id,
        code=code,
        message=message,
        runtime_state_unchanged=True,
    )


def _build_success_result(
    *,
    intent: str,
    job_id: str,
    requested_by: str,
    stage: str,
    apply_result: dict[str, Any],
    details: dict[str, Any] | None = None,
    runtime_state_unchanged: bool = False,
) -> dict[str, Any]:
    result = _base_result(
        intent=intent,
        job_id=job_id,
        stage=stage,
        ok=True,
        requested_by=requested_by,
        apply_id=apply_result.get("apply_id"),
        runtime_state_unchanged=runtime_state_unchanged,
    )
    if details:
        result.update(details)
    return result

