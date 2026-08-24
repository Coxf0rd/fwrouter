from __future__ import annotations

from typing import Any

from fwrouter_api.services import apply_orchestrator as orchestrator
from fwrouter_api.services.apply_orchestrator_handler_common import _subject_needs_mihomo_selector_from_committed
from fwrouter_api.services.subject_taxonomy import subject_follows_global_mode


def _execute_set_subject_admin_mode(job: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    requested_by = str(job.get("requested_by") or "api")
    subject_id = str(payload.get("subject_id") or "").strip()
    payload_subject_ids = payload.get("subject_ids")
    subject_ids = [
        str(item or "").strip()
        for item in (payload_subject_ids if isinstance(payload_subject_ids, list) else [subject_id])
        if str(item or "").strip()
    ]
    subject_ids = list(dict.fromkeys(subject_ids))
    mode = str(payload.get("mode") or "").strip().lower()
    subjects_by_id = {
        current_subject_id: orchestrator.get_subject(current_subject_id)
        for current_subject_id in subject_ids
    }
    missing_subject_ids = [
        current_subject_id
        for current_subject_id, subject in subjects_by_id.items()
        if subject is None
    ]
    if not subject_ids or missing_subject_ids:
        return orchestrator._build_failure_result(
            intent=orchestrator.INTENT_SET_SUBJECT_ADMIN_MODE,
            job_id=str(job["job_id"]),
            requested_by=requested_by,
            stage="validate",
            code="SUBJECT_NOT_FOUND",
            message=f"Subject not found: {', '.join(missing_subject_ids or [subject_id])}",
        )

    validation_failures: list[dict[str, Any]] = []
    for current_subject_id in subject_ids:
        subject = subjects_by_id[current_subject_id]
        validation = orchestrator._validate_subject_admin_mode(subject, mode)  # type: ignore[arg-type]
        if validation is not None:
            validation_failures.append({"subject_id": current_subject_id, **validation})

    if validation_failures:
        first_failure = validation_failures[0]
        result = orchestrator._build_failure_result(
            intent=orchestrator.INTENT_SET_SUBJECT_ADMIN_MODE,
            job_id=str(job["job_id"]),
            requested_by=requested_by,
            stage="validate",
            code=str(first_failure["code"]),
            message=str(first_failure["message"]),
        )
        for failure in validation_failures:
            orchestrator._persist_subject_failure(str(failure["subject_id"]))
        return result

    for current_subject_id in subject_ids:
        orchestrator._stage_subject_admin_mode(subject_id=current_subject_id, mode=mode)
    subject = subjects_by_id[subject_ids[0]]
    subject_type = str((subject or {}).get("subject_type") or "").strip().lower()
    routing = orchestrator.get_routing_snapshot()

    user_overrides = orchestrator._load_user_override_map()
    if mode != "global":
        for current_subject_id in subject_ids:
            user_overrides.pop(current_subject_id, None)
    server_overrides = orchestrator._load_server_override_map()

    runtime_enforcement = orchestrator.build_runtime_enforcement_state()
    bypass_state = orchestrator.get_core_bypass_state()
    target_subject_ids = set(subject_ids)
    current_subjects = orchestrator.list_subjects(include_deleted=False, limit=1000)
    future_subjects = [
        orchestrator.enrich_subject_with_effective_state(
            (
                {**dict(current_subject), "desired_mode": mode}
                if str(current_subject["subject_id"]) in target_subject_ids
                else dict(current_subject)
            ),
            routing=routing,
            user_override=user_overrides.get(str(current_subject["subject_id"])),
            server_override=server_overrides.get(str(current_subject["subject_id"])),
            runtime_enforcement=runtime_enforcement,
            bypass_state=bypass_state,
        )
        for current_subject in current_subjects
    ]

    apply_result = orchestrator._run_pipeline_for_state(
        job_id=str(job["job_id"]),
        reason=orchestrator.INTENT_SET_SUBJECT_ADMIN_MODE,
        input_data={
            "intent": orchestrator.INTENT_SET_SUBJECT_ADMIN_MODE,
            "subject_id": subject_id,
            "subject_ids": subject_ids,
            "mode": mode,
            "fast_subject_apply": {
                "enabled": len(subject_ids) == 1 and subject_follows_global_mode(subject_type) and mode in {"direct", "selective", "vpn"},
                "subject_id": subject_id,
                "subject_type": subject_type,
                "target_mode": mode,
            },
        },
        routing=routing,
        subjects=future_subjects,
    )

    if not apply_result["ok"]:
        result = orchestrator._build_failure_result(
            intent=orchestrator.INTENT_SET_SUBJECT_ADMIN_MODE,
            job_id=str(job["job_id"]),
            requested_by=requested_by,
            stage=str(apply_result.get("stage") or "apply"),
            code=apply_result["dataplane"]["error_code"] or "SUBJECT_ADMIN_MODE_APPLY_FAILED",
            message=apply_result["dataplane"]["error_message"] or apply_result["dataplane"]["message"],
            apply_id=apply_result["apply_id"],
            details={"apply": apply_result, "subject_id": subject_id, "subject_ids": subject_ids},
        )
        for current_subject_id in subject_ids:
            orchestrator._persist_subject_failure(current_subject_id)
        return result

    for current_subject_id in subject_ids:
        orchestrator._commit_subject_admin_mode(subject_id=current_subject_id, mode=mode)
    effective_subjects = [
        orchestrator.enrich_subject_with_effective_state(
            orchestrator.get_subject(current_subject_id) or subjects_by_id[current_subject_id],
            routing=routing,
        )
        for current_subject_id in subject_ids
    ]
    effective = effective_subjects[0]
    effective_by_id = {str(item["subject_id"]): item for item in effective_subjects}
    future_subjects = [
        effective_by_id[str(item["subject_id"])] if str(item["subject_id"]) in effective_by_id else item
        for item in future_subjects
    ]
    sync_subjects = (
        effective_subjects
        if len(subject_ids) == 1 and subject_follows_global_mode(subject_type)
        else future_subjects
    )
    orchestrator._sync_subject_server_override_statuses(sync_subjects)
    return orchestrator._build_success_result(
        intent=orchestrator.INTENT_SET_SUBJECT_ADMIN_MODE,
        job_id=str(job["job_id"]),
        requested_by=requested_by,
        stage="commit",
        apply_result=apply_result,
        details={"subject": effective, "subjects": effective_subjects, "subject_ids": subject_ids},
    )


def _execute_set_subject_user_mode(job: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    requested_by = str(job.get("requested_by") or "api")
    subject_id = str(payload.get("subject_id") or "").strip()
    mode = str(payload.get("mode") or "").strip().lower()
    subject = orchestrator.get_subject(subject_id)
    if subject is None:
        return orchestrator._build_failure_result(
            intent=orchestrator.INTENT_SET_SUBJECT_USER_MODE,
            job_id=str(job["job_id"]),
            requested_by=requested_by,
            stage="validate",
            code="SUBJECT_NOT_FOUND",
            message=f"Subject not found: {subject_id}",
        )

    validation = orchestrator._validate_subject_user_mode(subject, mode)
    if validation is not None:
        result = orchestrator._build_failure_result(
            intent=orchestrator.INTENT_SET_SUBJECT_USER_MODE,
            job_id=str(job["job_id"]),
            requested_by=requested_by,
            stage="validate",
            code=validation["code"],
            message=validation["message"],
        )
        orchestrator._persist_subject_failure(subject_id)
        return result

    subject_type = str(subject.get("subject_type") or "").strip().lower()
    routing = orchestrator.get_routing_snapshot()
    user_overrides = orchestrator._load_user_override_map()
    user_overrides[subject_id] = {
        "subject_id": subject_id,
        "override_mode": mode,
        "override_until": "pending_commit",
        "created_by": requested_by,
    }
    server_overrides = orchestrator._load_server_override_map()

    runtime_enforcement = orchestrator.build_runtime_enforcement_state()
    bypass_state = orchestrator.get_core_bypass_state()
    current_subjects = orchestrator.list_subjects(include_deleted=False, limit=1000)
    future_subjects = [
        orchestrator.enrich_subject_with_effective_state(
            dict(current_subject),
            routing=routing,
            user_override=user_overrides.get(str(current_subject["subject_id"])),
            server_override=server_overrides.get(str(current_subject["subject_id"])),
            runtime_enforcement=runtime_enforcement,
            bypass_state=bypass_state,
        )
        for current_subject in current_subjects
    ]

    apply_result = orchestrator._run_pipeline_for_state(
        job_id=str(job["job_id"]),
        reason=orchestrator.INTENT_SET_SUBJECT_USER_MODE,
        input_data={
            "intent": orchestrator.INTENT_SET_SUBJECT_USER_MODE,
            "subject_id": subject_id,
            "mode": mode,
            "fast_subject_apply": {
                "enabled": subject_follows_global_mode(subject_type) and mode in {"direct", "selective", "vpn"},
                "subject_id": subject_id,
                "subject_type": subject_type,
                "target_mode": mode,
            },
        },
        routing=routing,
        subjects=future_subjects,
    )

    if not apply_result["ok"]:
        result = orchestrator._build_failure_result(
            intent=orchestrator.INTENT_SET_SUBJECT_USER_MODE,
            job_id=str(job["job_id"]),
            requested_by=requested_by,
            stage=str(apply_result.get("stage") or "apply"),
            code=apply_result["dataplane"]["error_code"] or "SUBJECT_USER_MODE_APPLY_FAILED",
            message=apply_result["dataplane"]["error_message"] or apply_result["dataplane"]["message"],
            apply_id=apply_result["apply_id"],
            details={"apply": apply_result, "subject_id": subject_id},
        )
        orchestrator._persist_subject_failure(subject_id)
        return result

    orchestrator._commit_subject_user_mode(subject_id=subject_id, mode=mode, requested_by=requested_by)
    committed_subject = orchestrator.enrich_subject_with_effective_state(orchestrator.get_subject(subject_id) or subject, routing=routing)
    future_subjects = [
        committed_subject if str(item["subject_id"]) == subject_id else item
        for item in future_subjects
    ]
    orchestrator._sync_subject_server_override_statuses([committed_subject])
    return orchestrator._build_success_result(
        intent=orchestrator.INTENT_SET_SUBJECT_USER_MODE,
        job_id=str(job["job_id"]),
        requested_by=requested_by,
        stage="commit",
        apply_result=apply_result,
        details={"subject": committed_subject},
    )


def _execute_clear_subject_user_mode(job: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    requested_by = str(job.get("requested_by") or "api")
    subject_id = str(payload.get("subject_id") or "").strip()
    subject = orchestrator.get_subject(subject_id)
    if subject is None:
        return orchestrator._build_failure_result(
            intent=orchestrator.INTENT_CLEAR_SUBJECT_USER_MODE,
            job_id=str(job["job_id"]),
            requested_by=requested_by,
            stage="validate",
            code="SUBJECT_NOT_FOUND",
            message=f"Subject not found: {subject_id}",
        )

    user_overrides = orchestrator._load_user_override_map()
    existing_override = user_overrides.pop(subject_id, None)
    if existing_override is None:
        return orchestrator._build_success_result(
            intent=orchestrator.INTENT_CLEAR_SUBJECT_USER_MODE,
            job_id=str(job["job_id"]),
            requested_by=requested_by,
            stage="commit",
            apply_result={"apply_id": None, "ok": True},
            details={"subject": orchestrator.get_subject_with_effective_state(subject_id)},
            runtime_state_unchanged=True,
        )

    subject_type = str(subject.get("subject_type") or "").strip().lower()
    if subject_follows_global_mode(subject_type) and str(subject.get("desired_mode") or "") != "global":
        return orchestrator._build_failure_result(
            intent=orchestrator.INTENT_CLEAR_SUBJECT_USER_MODE,
            job_id=str(job["job_id"]),
            requested_by=requested_by,
            stage="validate",
            code="SUBJECT_MODE_ADMIN_LOCKED",
            message="User override can be cleared only while admin mode is global.",
        )

    routing = orchestrator.get_routing_snapshot()
    server_overrides = orchestrator._load_server_override_map()
    runtime_enforcement = orchestrator.build_runtime_enforcement_state()
    bypass_state = orchestrator.get_core_bypass_state()
    current_subjects = orchestrator.list_subjects(include_deleted=False, limit=1000)
    future_subjects = [
        orchestrator.enrich_subject_with_effective_state(
            dict(current_subject),
            routing=routing,
            user_override=user_overrides.get(str(current_subject["subject_id"])),
            server_override=server_overrides.get(str(current_subject["subject_id"])),
            runtime_enforcement=runtime_enforcement,
            bypass_state=bypass_state,
        )
        for current_subject in current_subjects
    ]

    apply_result = orchestrator._run_pipeline_for_state(
        job_id=str(job["job_id"]),
        reason=orchestrator.INTENT_CLEAR_SUBJECT_USER_MODE,
        input_data={
            "intent": orchestrator.INTENT_CLEAR_SUBJECT_USER_MODE,
            "subject_id": subject_id,
            "fast_subject_apply": {
                "enabled": subject_follows_global_mode(subject_type),
                "subject_id": subject_id,
                "subject_type": subject_type,
                "target_mode": "global",
            },
        },
        routing=routing,
        subjects=future_subjects,
    )

    if not apply_result["ok"]:
        result = orchestrator._build_failure_result(
            intent=orchestrator.INTENT_CLEAR_SUBJECT_USER_MODE,
            job_id=str(job["job_id"]),
            requested_by=requested_by,
            stage=str(apply_result.get("stage") or "apply"),
            code=apply_result["dataplane"]["error_code"] or "SUBJECT_USER_MODE_CLEAR_FAILED",
            message=apply_result["dataplane"]["error_message"] or apply_result["dataplane"]["message"],
            apply_id=apply_result["apply_id"],
            details={"apply": apply_result, "subject_id": subject_id},
        )
        orchestrator._persist_subject_failure(subject_id)
        return result

    orchestrator._clear_subject_user_mode(subject_id=subject_id)
    committed_subject = orchestrator.enrich_subject_with_effective_state(orchestrator.get_subject(subject_id) or subject, routing=routing)
    orchestrator._sync_subject_server_override_statuses([committed_subject])
    return orchestrator._build_success_result(
        intent=orchestrator.INTENT_CLEAR_SUBJECT_USER_MODE,
        job_id=str(job["job_id"]),
        requested_by=requested_by,
        stage="commit",
        apply_result=apply_result,
        details={"subject": committed_subject},
    )
