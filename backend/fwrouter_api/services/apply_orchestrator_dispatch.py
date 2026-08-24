from __future__ import annotations

from typing import Any

from fwrouter_api.services import apply_orchestrator as orchestrator
from fwrouter_api.services.apply_orchestrator_global_handlers import (
    _execute_repair_global_direct_runtime,
    _execute_set_global_mode,
    _execute_set_global_server_mode,
    _execute_set_selective_default,
)
from fwrouter_api.services.apply_orchestrator_rules_handlers import _execute_apply_manual_rules
from fwrouter_api.services.apply_orchestrator_server_handlers import (
    _execute_clear_subject_server_override,
    _execute_set_subject_server_override,
)
from fwrouter_api.services.apply_orchestrator_subject_handlers import (
    _execute_clear_subject_user_mode,
    _execute_set_subject_admin_mode,
    _execute_set_subject_user_mode,
)


def execute_apply_mutation(job: dict[str, Any]) -> dict[str, Any]:
    input_data = job.get("input") if isinstance(job.get("input"), dict) else {}
    intent = str(input_data.get("intent") or "").strip()
    payload = input_data.get("payload") if isinstance(input_data.get("payload"), dict) else {}

    if intent == orchestrator.INTENT_SET_GLOBAL_MODE:
        result = _execute_set_global_mode(job, payload)
    elif intent == orchestrator.INTENT_SET_GLOBAL_SERVER_MODE:
        result = _execute_set_global_server_mode(job, payload)
    elif intent == orchestrator.INTENT_SET_SELECTIVE_DEFAULT:
        result = _execute_set_selective_default(job, payload)
    elif intent == orchestrator.INTENT_SET_SUBJECT_ADMIN_MODE:
        result = _execute_set_subject_admin_mode(job, payload)
    elif intent == orchestrator.INTENT_SET_SUBJECT_USER_MODE:
        result = _execute_set_subject_user_mode(job, payload)
    elif intent == orchestrator.INTENT_CLEAR_SUBJECT_USER_MODE:
        result = _execute_clear_subject_user_mode(job, payload)
    elif intent == orchestrator.INTENT_SET_SUBJECT_SERVER_OVERRIDE:
        result = _execute_set_subject_server_override(job, payload)
    elif intent == orchestrator.INTENT_CLEAR_SUBJECT_SERVER_OVERRIDE:
        result = _execute_clear_subject_server_override(job, payload)
    elif intent == orchestrator.INTENT_APPLY_MANUAL_RULES:
        result = _execute_apply_manual_rules(job, payload)
    elif intent == orchestrator.INTENT_REPAIR_GLOBAL_DIRECT_RUNTIME:
        result = _execute_repair_global_direct_runtime(job, payload)
    else:
        result = orchestrator._build_failure_result(
            intent=intent or "unknown",
            job_id=str(job["job_id"]),
            requested_by=str(job.get("requested_by") or "api"),
            stage="validate",
            code="APPLY_INTENT_UNKNOWN",
            message=f"Unsupported apply mutation intent: {intent}",
        )

    orchestrator._log_mutation_result(result)
    return {
        "job_status": "success" if result["ok"] else "failed",
        "error_code": None if result["ok"] else result["code"],
        "error_message": None if result["ok"] else result["message"],
        "mutation": result,
    }

