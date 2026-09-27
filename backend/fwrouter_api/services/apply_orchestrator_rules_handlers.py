from __future__ import annotations

import hashlib
from typing import Any

from fwrouter_api.services import apply_orchestrator as orchestrator


def _execute_apply_manual_rules(job: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    del payload
    requested_by = str(job.get("requested_by") or "api")
    orchestrator.mark_rules_job_running(job_id=str(job["job_id"]), update_type="manual_apply")
    previous_active_text = str(orchestrator.get_manual_rules_texts().get("active_text") or "")
    candidate = orchestrator.prepare_manual_rules_candidate(job_id=str(job["job_id"]))
    validation = candidate["manual_validation"]

    if not validation["valid"]:
        result = orchestrator._build_failure_result(
            intent=orchestrator.INTENT_APPLY_MANUAL_RULES,
            job_id=str(job["job_id"]),
            requested_by=requested_by,
            stage="validate",
            code="RULES_VALIDATION_FAILED",
            message="Manual rules validation failed.",
            details={"validation": validation},
        )
        orchestrator._persist_rules_error(
            job_id=str(job["job_id"]),
            code=result["code"],
            message=result["message"],
        )
        return result

    effective_artifact = candidate["effective_artifact"]
    routing = orchestrator.get_routing_snapshot()
    subjects = orchestrator._load_subjects_with_overrides(
        routing=routing,
        user_overrides=orchestrator._load_user_override_map(),
        server_overrides=orchestrator._load_server_override_map(),
    )
    apply_result = orchestrator._run_pipeline_for_state(
        job_id=str(job["job_id"]),
        reason=orchestrator.INTENT_APPLY_MANUAL_RULES,
        input_data={"intent": orchestrator.INTENT_APPLY_MANUAL_RULES},
        routing=routing,
        subjects=subjects,
        extra={"rules_effective": effective_artifact},
    )

    if not apply_result["ok"]:
        result = orchestrator._build_failure_result(
            intent=orchestrator.INTENT_APPLY_MANUAL_RULES,
            job_id=str(job["job_id"]),
            requested_by=requested_by,
            stage=str(apply_result.get("stage") or "apply"),
            code=apply_result["dataplane"]["error_code"] or "RULES_APPLY_FAILED",
            message=apply_result["dataplane"]["error_message"] or apply_result["dataplane"]["message"],
            apply_id=apply_result["apply_id"],
            details={"apply": apply_result, "validation": validation},
        )
        orchestrator._persist_rules_error(
            job_id=str(job["job_id"]),
            code=result["code"],
            message=result["message"],
            effective_artifact=effective_artifact,
        )
        return result

    committed = orchestrator._commit_manual_rules_apply(
        job_id=str(job["job_id"]),
        draft_text=validation["normalized_text"],
        effective_artifact=effective_artifact,
        runtime_enforcement={
            "dataplane_capability": apply_result["dataplane_capability"],
            "capability": apply_result["dataplane_capability"],
            "enforcement_level": apply_result["enforcement_level"],
            "traffic_enforcement_guaranteed": apply_result["traffic_enforcement_guaranteed"],
            "supported_modes": dict(apply_result.get("supported_modes") or {}),
            "missing_runtime_requirements": list(apply_result.get("missing_runtime_requirements") or []),
        },
        audit_change={
            "changed": previous_active_text != validation["normalized_text"],
            "requested_by": requested_by,
            "apply_id": apply_result.get("apply_id"),
            "previous_value": {
                "sha256": hashlib.sha256(previous_active_text.encode("utf-8")).hexdigest(),
                "rule_count": len(orchestrator.validate_manual_rules(previous_active_text).get("rules") or []),
            },
            "new_value": {
                "sha256": hashlib.sha256(validation["normalized_text"].encode("utf-8")).hexdigest(),
                "rule_count": len(validation.get("rules") or []),
            },
        },
    )
    orchestrator._sync_subject_server_override_statuses(subjects)
    return orchestrator._build_success_result(
        intent=orchestrator.INTENT_APPLY_MANUAL_RULES,
        job_id=str(job["job_id"]),
        requested_by=requested_by,
        stage="commit",
        apply_result=apply_result,
        details={
            "rules": {
                "state": committed["state"],
                "active_text": committed["active_text"],
                "effective_counts": committed.get("effective_counts"),
                "source_counts": committed.get("source_counts"),
            }
        },
    )
