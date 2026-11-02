from __future__ import annotations

import hashlib
from typing import Any

from fwrouter_api.services.apply_orchestrator_constants import (
    INTENT_APPLY_MANUAL_RULES as INTENT_APPLY_MANUAL_RULES,
)
from fwrouter_api.services.apply_orchestrator_pipeline import (
    _commit_manual_rules_apply as _commit_manual_rules_apply,
    _run_pipeline_for_state as _run_pipeline_for_state,
)
from fwrouter_api.services.apply_orchestrator_results import (
    _build_failure_result as _build_failure_result,
    _build_success_result as _build_success_result,
)
from fwrouter_api.services.apply_orchestrator_state import (
    _load_server_override_map as _load_server_override_map,
    _load_subjects_with_overrides as _load_subjects_with_overrides,
    _load_user_override_map as _load_user_override_map,
    _persist_rules_error as _persist_rules_error,
    _sync_subject_server_override_statuses as _sync_subject_server_override_statuses,
)
from fwrouter_api.services.rules import (
    get_manual_rules_texts as get_manual_rules_texts,
    mark_rules_job_running as mark_rules_job_running,
    prepare_manual_rules_candidate as prepare_manual_rules_candidate,
    validate_manual_rules as validate_manual_rules,
)
from fwrouter_api.services.subject_policy import (
    get_routing_snapshot as get_routing_snapshot,
)





def _execute_apply_manual_rules(job: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    job_id = str(job["job_id"])
    mark_rules_job_running(job_id=job_id, update_type="manual_apply")
    try:
        return _execute_apply_manual_rules_running(job, payload)
    except Exception as exc:
        # The job manager owns the terminal job failure. Persist its matching
        # rules-domain failure here, before allowing that original exception
        # to reach the manager; never convert render/write failures to success.
        try:
            _persist_rules_error(
                job_id=job_id,
                code=str(getattr(exc, "error_code", "RULES_APPLY_FAILED")),
                message=str(exc),
            )
        except Exception:
            # Keep the original apply exception as the job's observable cause.
            pass
        raise


def _execute_apply_manual_rules_running(job: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    del payload
    requested_by = str(job.get("requested_by") or "api")
    job_id = str(job["job_id"])
    previous_active_text = str(get_manual_rules_texts().get("active_text") or "")
    candidate = prepare_manual_rules_candidate(job_id=job_id)
    validation = candidate["manual_validation"]

    if not validation["valid"]:
        result = _build_failure_result(
            intent=INTENT_APPLY_MANUAL_RULES,
            job_id=str(job["job_id"]),
            requested_by=requested_by,
            stage="validate",
            code="RULES_VALIDATION_FAILED",
            message="Manual rules validation failed.",
            details={"validation": validation},
        )
        _persist_rules_error(
            job_id=str(job["job_id"]),
            code=result["code"],
            message=result["message"],
        )
        return result

    effective_artifact = candidate["effective_artifact"]
    routing = get_routing_snapshot()
    subjects = _load_subjects_with_overrides(
        routing=routing,
        user_overrides=_load_user_override_map(),
        server_overrides=_load_server_override_map(),
    )
    apply_result = _run_pipeline_for_state(
        job_id=str(job["job_id"]),
        reason=INTENT_APPLY_MANUAL_RULES,
        input_data={"intent": INTENT_APPLY_MANUAL_RULES},
        routing=routing,
        subjects=subjects,
        extra={"rules_effective": effective_artifact},
    )

    if not apply_result["ok"]:
        result = _build_failure_result(
            intent=INTENT_APPLY_MANUAL_RULES,
            job_id=str(job["job_id"]),
            requested_by=requested_by,
            stage=str(apply_result.get("stage") or "apply"),
            code=apply_result["dataplane"]["error_code"] or "RULES_APPLY_FAILED",
            message=apply_result["dataplane"]["error_message"] or apply_result["dataplane"]["message"],
            apply_id=apply_result["apply_id"],
            details={"apply": apply_result, "validation": validation},
        )
        _persist_rules_error(
            job_id=str(job["job_id"]),
            code=result["code"],
            message=result["message"],
            effective_artifact=effective_artifact,
        )
        return result

    committed = _commit_manual_rules_apply(
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
                "rule_count": len(validate_manual_rules(previous_active_text).get("rules") or []),
            },
            "new_value": {
                "sha256": hashlib.sha256(validation["normalized_text"].encode("utf-8")).hexdigest(),
                "rule_count": len(validation.get("rules") or []),
            },
        },
    )
    _sync_subject_server_override_statuses(subjects)
    return _build_success_result(
        intent=INTENT_APPLY_MANUAL_RULES,
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
