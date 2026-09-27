from __future__ import annotations

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
from fwrouter_api.services.event_contract import current_event_context
from fwrouter_api.services.events import safe_actor_identifier
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


def _find_mapping_containing_key(value: Any, key: str) -> dict[str, Any]:
    if isinstance(value, dict):
        if key in value:
            return value
        for nested in value.values():
            found = _find_mapping_containing_key(nested, key)
            if found:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _find_mapping_containing_key(nested, key)
            if found:
                return found
    return {}


def _find_nested_value(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        if key in value:
            return value[key]
        for nested in value.values():
            found = _find_nested_value(nested, key)
            if found is not None:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _find_nested_value(nested, key)
            if found is not None:
                return found
    return None


def _safe_rule_counts(values: dict[str, Any]) -> dict[str, int]:
    allowed = {"manual", "static_direct", "big_direct", "big_vpn", "protected", "direct", "vpn", "total"}
    return {
        str(key): int(value)
        for key, value in values.items()
        if str(key) in allowed and isinstance(value, int) and not isinstance(value, bool)
    }


def _log_mutation_result(result: dict[str, Any]) -> None:
    intent = str(result.get("intent") or "")
    audit_codes = {
        "set_subject_admin_mode": "client.mode_changed",
        "set_subject_server_override": "server.assignment_changed",
        "clear_subject_server_override": "server.assignment_changed",
    }
    failure_codes = {
        "set_subject_admin_mode": "client.mode_apply_failed",
        "set_subject_server_override": "server.assignment_apply_failed",
        "clear_subject_server_override": "server.assignment_apply_failed",
    }
    audit_context = result.get("audit_context")
    if intent == INTENT_APPLY_MANUAL_RULES:
        import hashlib

        payload = result.get("details") if isinstance(result.get("details"), dict) else {}
        rules = _find_mapping_containing_key(payload, "active_text")
        active_text = rules.get("active_text") if isinstance(rules.get("active_text"), str) else ""
        validation_value = _find_nested_value(payload, "validation")
        validation = validation_value if isinstance(validation_value, dict) else {}
        validation_errors = validation.get("errors") if isinstance(validation.get("errors"), list) else []
        source_counts = rules.get("source_counts") if isinstance(rules.get("source_counts"), dict) else {}
        effective_counts = rules.get("effective_counts") if isinstance(rules.get("effective_counts"), dict) else {}
        success = bool(result.get("ok"))
        event_code = "manual_rules_apply_completed" if success else "manual_rules_apply_failed"
        job_id = str(result.get("job_id") or "") or None
        apply_id = str(result.get("apply_id") or "") or None
        context = current_event_context()
        write_operational_log(
            event_type=event_code,
            level="info" if success else "error",
            message="Manual routing rules apply completed." if success else "Manual routing rules apply failed.",
            event_category="operational",
            event_code=event_code,
            component="fwrouter-api",
            operation="apply_manual_rules",
            outcome="success" if success else "failed",
            details={
                "actor": safe_actor_identifier(result.get("requested_by")),
                "actor_attribution": "caller_supplied",
                "action": "apply_manual_rules",
                "outcome": "success" if success else "failed",
                "job_id": job_id,
                "apply_id": apply_id,
                "request_id": context.get("request_id"),
                "stage": str(result.get("stage") or "") or None,
                "error_code": str(result.get("code") or "") or None,
                "manual_rule_set": {
                    "sha256": hashlib.sha256(active_text.encode("utf-8")).hexdigest() if active_text else None,
                    "rule_count": sum(1 for line in active_text.splitlines() if line.strip() and not line.lstrip().startswith("#")),
                },
                "source_counts": _safe_rule_counts(source_counts),
                "effective_counts": _safe_rule_counts(effective_counts),
                "validation": {
                    "valid": bool(validation.get("valid")) if validation else None,
                    "error_count": len(validation_errors),
                    "error_codes": sorted({str(item.get("code")) for item in validation_errors if isinstance(item, dict) and item.get("code")}),
                },
            },
        )
        return
    if intent in audit_codes:
        job_id = str(result.get("job_id") or "")
        audit_already_written = False
        if job_id:
            with db_session() as connection:
                audit_already_written = connection.execute(
                    """
                    SELECT 1
                    FROM operational_logs
                    WHERE details_json LIKE ?
                      AND details_json LIKE ?
                    LIMIT 1
                    """,
                    (
                        f'%"job_id": "{job_id}"%',
                        f'%"event_code": "{audit_codes[intent]}"%',
                    ),
                ).fetchone() is not None
        if audit_already_written:
            if result.get("ok"):
                return
            write_operational_log(
                event_type=f"mutation_{intent}_failed",
                level="error",
                message=f"Administrative {intent.replace('_', ' ')} runtime apply failed.",
                event_category="operational",
                event_code=failure_codes[intent],
                component="fwrouter-api",
                details={
                    "actor": safe_actor_identifier(result.get("requested_by")),
                    "actor_attribution": "caller_supplied",
                    "action": intent,
                    "outcome": "runtime_apply_failed_after_intent_commit",
                    "job_id": job_id,
                    "apply_id": result.get("apply_id"),
                    "request_id": current_event_context().get("request_id"),
                    "stage": result.get("stage"),
                    "error_code": result.get("code"),
                },
            )
            return
        if isinstance(audit_context, dict):
            write_operational_log(
                event_type="admin_audit_event_missing",
                level="error",
                message="Committed administrative intent has no matching audit row.",
                event_category="operational",
                event_code="admin.audit_event_missing",
                component="fwrouter-api",
                details={
                    "actor": safe_actor_identifier(result.get("requested_by")),
                    "actor_attribution": "caller_supplied",
                    "action": audit_context.get("action") or intent,
                    "outcome": "committed_intent_missing_audit",
                    "job_id": job_id or result.get("job_id"),
                    "apply_id": result.get("apply_id"),
                    "request_id": current_event_context().get("request_id"),
                },
            )
            if not result.get("ok"):
                write_operational_log(
                    event_type=f"mutation_{intent}_failed",
                    level="error",
                    message=f"Administrative {intent.replace('_', ' ')} runtime apply failed.",
                    event_category="operational",
                    event_code=failure_codes[intent],
                    component="fwrouter-api",
                    details={
                        "actor": safe_actor_identifier(result.get("requested_by")),
                        "actor_attribution": "caller_supplied",
                        "action": intent,
                        "outcome": "runtime_apply_failed_after_intent_commit",
                        "job_id": result.get("job_id"),
                        "apply_id": result.get("apply_id"),
                        "request_id": current_event_context().get("request_id"),
                        "stage": result.get("stage"),
                        "error_code": result.get("code"),
                    },
                )
            return
        if result.get("ok"):
            # A successful no-op has no committed administrative change to audit.
            return
        write_operational_log(
            event_type=f"mutation_{intent}_failed",
            level="error",
            message=f"Administrative {intent.replace('_', ' ')} failed.",
            event_category="operational",
            event_code=failure_codes[intent],
            component="fwrouter-api",
            details={
                "actor": safe_actor_identifier(result.get("requested_by")),
                "actor_attribution": "caller_supplied",
                "action": intent,
                "outcome": "not_committed",
                "job_id": result.get("job_id"),
                "apply_id": result.get("apply_id"),
                "request_id": current_event_context().get("request_id"),
                "stage": result.get("stage"),
                "error_code": result.get("code"),
            },
        )
        return

    write_operational_log(
        event_type=f"mutation_{result['intent']}_{'success' if result['ok'] else 'failed'}",
        level="info" if result["ok"] else "error",
        message=result["message"] or f"Mutation {result['intent']} successful." if result["ok"] else result["message"] or f"Mutation {result['intent']} failed.",
        details=result,
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
