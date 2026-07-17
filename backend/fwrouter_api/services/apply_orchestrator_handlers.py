from __future__ import annotations

from typing import Any

from fwrouter_api.services import apply_orchestrator as orchestrator


def _selective_default_artifact_drift_is_ignorable_for_global_direct(
    *,
    routing: dict[str, Any],
    artifact_drift: dict[str, Any],
) -> bool:
    if not artifact_drift.get("detected"):
        return True
    mode = str(routing.get("applied_mode") or routing.get("desired_mode") or "").strip().lower()
    if mode != "direct":
        return False
    mismatches = artifact_drift.get("mismatches")
    if not isinstance(mismatches, dict):
        return False
    return set(mismatches.keys()) == {"selective_default"}


def _execute_set_global_mode(job: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    requested_by = str(job.get("requested_by") or "api")
    mode = str(payload.get("mode") or "").strip().lower()
    if mode not in {"direct", "selective", "vpn"}:
        return orchestrator._build_failure_result(
            intent=orchestrator.INTENT_SET_GLOBAL_MODE,
            job_id=str(job["job_id"]),
            requested_by=requested_by,
            stage="validate",
            code="GLOBAL_MODE_INVALID",
            message="Global mode must be one of: direct, selective, vpn.",
        )

    routing = orchestrator.get_routing_snapshot()
    drift = orchestrator._current_routing_drift(routing=routing)
    artifact_drift = orchestrator._applied_manifest_routing_drift(routing=routing)
    if (
        str(routing.get("applied_mode")) == mode
        and str(routing.get("apply_state")) == "clean"
        and not drift["detected"]
        and not artifact_drift["detected"]
    ):
        return orchestrator._build_success_result(
            intent=orchestrator.INTENT_SET_GLOBAL_MODE,
            job_id=str(job["job_id"]),
            requested_by=requested_by,
            stage="validate",
            apply_result={"ok": True, "message": "Global mode is already correct."},
            details={"routing": routing, "runtime_state_unchanged": True},
            runtime_state_unchanged=True,
        )
    if drift["detected"]:
        orchestrator._log_routing_drift(
            intent=orchestrator.INTENT_SET_GLOBAL_MODE,
            requested_by=requested_by,
            drift=drift,
        )
    if artifact_drift["detected"]:
        orchestrator._log_artifact_drift(
            intent=orchestrator.INTENT_SET_GLOBAL_MODE,
            requested_by=requested_by,
            drift=artifact_drift,
        )

    future_routing = dict(routing)
    future_routing["desired_mode"] = mode
    future_routing["apply_state"] = "applying"
    future_routing["error_code"] = None
    future_routing["error_message"] = None

    mode_validation = orchestrator.validate_global_mode_request(mode, routing=future_routing)
    if not mode_validation["ok"]:
        result = orchestrator._build_failure_result(
            intent=orchestrator.INTENT_SET_GLOBAL_MODE,
            job_id=str(job["job_id"]),
            requested_by=requested_by,
            stage=str(mode_validation["stage"]),
            code=str(mode_validation["code"]),
            message=str(mode_validation["message"]),
        )
        orchestrator._persist_global_error(code=result["code"], message=result["message"])
        return result

    orchestrator.touch_job_running(str(job["job_id"]))

    if mode == "direct":
        mihomo_reconcile: dict[str, Any] = {
            "ok": True,
            "skipped": True,
            "reason": "global_direct_does_not_change_mihomo_config",
        }
    else:
        mihomo_reconcile = orchestrator.mihomo_runtime_satisfies_routing(future_routing)
        if mihomo_reconcile.get("ok"):
            mihomo_reconcile = {
                **mihomo_reconcile,
                "skipped": True,
                "reconcile_action": "none",
                "reconcile_reason": "active_runtime_already_matches_routing",
            }
        else:
            mihomo_reconcile = orchestrator.reconcile_mihomo_runtime(
                routing=future_routing,
                job_id=str(job["job_id"]),
            )
        if not mihomo_reconcile["ok"]:
            result = orchestrator._build_failure_result(
                intent=orchestrator.INTENT_SET_GLOBAL_MODE,
                job_id=str(job["job_id"]),
                requested_by=requested_by,
                stage=str(mihomo_reconcile.get("stage") or "mihomo_reconcile"),
                code="MIHOMO_RECONCILE_FAILED",
                message="Failed to reconcile Mihomo runtime before global mode apply.",
                details={"mihomo_reconcile": mihomo_reconcile},
            )
            orchestrator._persist_global_error(code=result["code"], message=result["message"])
            return result

    precompiled_profile = orchestrator.load_precompiled_global_mode_profile(mode, routing=routing)
    affected_subjects: list[str]
    subjects: list[dict[str, Any]]
    if precompiled_profile is not None:
        affected_subjects = [
            str(subject_id)
            for subject_id in (precompiled_profile.get("affected_subject_ids") or [])
            if str(subject_id).strip()
        ]
        subjects = list(precompiled_profile.get("subject_runtime_statuses") or [])
        apply_result = orchestrator._run_pipeline_for_manifest(
            job_id=str(job["job_id"]),
            reason=orchestrator.INTENT_SET_GLOBAL_MODE,
            input_data={"intent": orchestrator.INTENT_SET_GLOBAL_MODE, "mode": mode},
            manifest=orchestrator.materialize_precompiled_manifest(
                precompiled_profile,
                plan_id="unused",
                reason=orchestrator.INTENT_SET_GLOBAL_MODE,
                input_data={"intent": orchestrator.INTENT_SET_GLOBAL_MODE, "mode": mode},
            ),
        )
    else:
        user_overrides = orchestrator._load_user_override_map()
        server_overrides = orchestrator._load_server_override_map()
        subjects = orchestrator._load_subjects_with_overrides(
            routing=future_routing,
            user_overrides=user_overrides,
            server_overrides=server_overrides,
        )

        affected_subjects = [
            str(subject["subject_id"])
            for subject in subjects
            if orchestrator._subject_follows_global(subject)
            and str(subject["desired_mode"]) == "global"
            and subject["effective_state"]["mode_source"] == "global"
        ]

        apply_result = orchestrator._run_pipeline_for_state(
            job_id=str(job["job_id"]),
            reason=orchestrator.INTENT_SET_GLOBAL_MODE,
            input_data={"intent": orchestrator.INTENT_SET_GLOBAL_MODE, "mode": mode},
            routing=future_routing,
            subjects=subjects,
            extra={"affected_subject_ids": affected_subjects},
        )

    if not apply_result["ok"]:
        result = orchestrator._build_failure_result(
            intent=orchestrator.INTENT_SET_GLOBAL_MODE,
            job_id=str(job["job_id"]),
            requested_by=requested_by,
            stage=str(apply_result.get("stage") or "apply"),
            code=apply_result["dataplane"]["error_code"] or "GLOBAL_MODE_APPLY_FAILED",
            message=apply_result["dataplane"]["error_message"] or apply_result["dataplane"]["message"],
            apply_id=apply_result["apply_id"],
            details={"apply": apply_result, "affected_subject_ids": affected_subjects},
        )
        orchestrator._persist_global_error(code=result["code"], message=result["message"])
        return result

    orchestrator.touch_job_running(str(job["job_id"]))
    committed = orchestrator._commit_global_mode(mode=mode)
    orchestrator._sync_subject_server_override_statuses(subjects)
    return orchestrator._build_success_result(
        intent=orchestrator.INTENT_SET_GLOBAL_MODE,
        job_id=str(job["job_id"]),
        requested_by=requested_by,
        stage="commit",
        apply_result=apply_result,
        details={"routing": committed, "affected_subject_ids": affected_subjects},
    )


def _execute_set_global_server_mode(job: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    requested_by = str(job.get("requested_by") or "api")
    server_mode = str(payload.get("server_mode") or "").strip().lower()
    if server_mode not in {"auto", "fixed"}:
        return orchestrator._build_failure_result(
            intent=orchestrator.INTENT_SET_GLOBAL_SERVER_MODE,
            job_id=str(job["job_id"]),
            requested_by=requested_by,
            stage="validate",
            code="SERVER_MODE_INVALID",
            message="Server mode must be one of: auto, fixed.",
        )

    routing = orchestrator.get_routing_snapshot()
    drift = orchestrator._current_routing_drift(routing=routing)
    artifact_drift = orchestrator._applied_manifest_routing_drift(routing=routing)
    if str(routing.get("server_mode")) == server_mode and not drift["detected"] and not artifact_drift["detected"]:
        return orchestrator._build_success_result(
            intent=orchestrator.INTENT_SET_GLOBAL_SERVER_MODE,
            job_id=str(job["job_id"]),
            requested_by=requested_by,
            stage="validate",
            apply_result={"ok": True, "message": "Server mode is already correct."},
            details={"routing": routing, "runtime_state_unchanged": True},
            runtime_state_unchanged=True,
        )
    if drift["detected"]:
        orchestrator._log_routing_drift(
            intent=orchestrator.INTENT_SET_GLOBAL_SERVER_MODE,
            requested_by=requested_by,
            drift=drift,
        )
    if artifact_drift["detected"]:
        orchestrator._log_artifact_drift(
            intent=orchestrator.INTENT_SET_GLOBAL_SERVER_MODE,
            requested_by=requested_by,
            drift=artifact_drift,
        )

    future_routing = dict(routing)
    future_routing["server_mode"] = server_mode
    future_routing["apply_state"] = "applying"
    future_routing["error_code"] = None
    future_routing["error_message"] = None

    current_mode = orchestrator.get_routing_snapshot().get("applied_mode") or orchestrator.get_routing_snapshot().get("desired_mode") or "direct"
    mode_validation = orchestrator.validate_global_mode_request(current_mode, routing=future_routing)
    if not mode_validation["ok"]:
        result = orchestrator._build_failure_result(
            intent=orchestrator.INTENT_SET_GLOBAL_SERVER_MODE,
            job_id=str(job["job_id"]),
            requested_by=requested_by,
            stage=str(mode_validation["stage"]),
            code=str(mode_validation["code"]),
            message=str(mode_validation["message"]),
        )
        orchestrator._persist_global_error(code=result["code"], message=result["message"])
        return result

    orchestrator.touch_job_running(str(job["job_id"]))

    mihomo_reconcile = orchestrator.reconcile_mihomo_runtime(
        routing=future_routing,
        job_id=str(job["job_id"]),
    )
    if not mihomo_reconcile["ok"]:
        result = orchestrator._build_failure_result(
            intent=orchestrator.INTENT_SET_GLOBAL_SERVER_MODE,
            job_id=str(job["job_id"]),
            requested_by=requested_by,
            stage=str(mihomo_reconcile.get("stage") or "mihomo_reconcile"),
            code="MIHOMO_RECONCILE_FAILED",
            message="Failed to reconcile Mihomo runtime before server mode apply.",
            details={"mihomo_reconcile": mihomo_reconcile},
        )
        orchestrator._persist_global_error(code=result["code"], message=result["message"])
        return result

    user_overrides = orchestrator._load_user_override_map()
    server_overrides = orchestrator._load_server_override_map()
    subjects = orchestrator._load_subjects_with_overrides(
        routing=future_routing,
        user_overrides=user_overrides,
        server_overrides=server_overrides,
    )

    affected_subjects = [
        str(subject["subject_id"])
        for subject in subjects
        if orchestrator._subject_follows_global(subject)
        and str(subject["desired_mode"]) == "global"
        and subject["effective_state"]["mode_source"] == "global"
    ]

    apply_result = orchestrator._run_pipeline_for_state(
        job_id=str(job["job_id"]),
        reason=orchestrator.INTENT_SET_GLOBAL_SERVER_MODE,
        input_data={"intent": orchestrator.INTENT_SET_GLOBAL_SERVER_MODE, "server_mode": server_mode},
        routing=future_routing,
        subjects=subjects,
        extra={"affected_subject_ids": affected_subjects},
    )

    if not apply_result["ok"]:
        result = orchestrator._build_failure_result(
            intent=orchestrator.INTENT_SET_GLOBAL_SERVER_MODE,
            job_id=str(job["job_id"]),
            requested_by=requested_by,
            stage=str(apply_result.get("stage") or "apply"),
            code=apply_result["dataplane"]["error_code"] or "SERVER_MODE_APPLY_FAILED",
            message=apply_result["dataplane"]["error_message"] or apply_result["dataplane"]["message"],
            apply_id=apply_result["apply_id"],
            details={"apply": apply_result, "affected_subject_ids": affected_subjects},
        )
        orchestrator._persist_global_error(code=result["code"], message=result["message"])
        return result

    orchestrator.touch_job_running(str(job["job_id"]))
    committed = orchestrator._commit_global_server_mode(server_mode=server_mode)
    orchestrator._sync_subject_server_override_statuses(subjects)
    return orchestrator._build_success_result(
        intent=orchestrator.INTENT_SET_GLOBAL_SERVER_MODE,
        job_id=str(job["job_id"]),
        requested_by=requested_by,
        stage="commit",
        apply_result=apply_result,
        details={"routing": committed, "affected_subject_ids": affected_subjects},
    )


def _execute_set_selective_default(job: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    requested_by = str(job.get("requested_by") or "api")
    selective_default = str(payload.get("selective_default") or "").strip().lower()
    if selective_default not in {"direct", "vpn"}:
        return orchestrator._build_failure_result(
            intent=orchestrator.INTENT_SET_SELECTIVE_DEFAULT,
            job_id=str(job["job_id"]),
            requested_by=requested_by,
            stage="validate",
            code="SELECTIVE_DEFAULT_INVALID",
            message="Selective default must be one of: direct, vpn.",
        )

    routing = orchestrator.get_routing_snapshot()
    drift = orchestrator._current_routing_drift(routing=routing)
    artifact_drift = orchestrator._applied_manifest_routing_drift(routing=routing)
    if str(routing.get("selective_default")) == selective_default and not drift["detected"] and not artifact_drift["detected"]:
        return orchestrator._build_success_result(
            intent=orchestrator.INTENT_SET_SELECTIVE_DEFAULT,
            job_id=str(job["job_id"]),
            requested_by=requested_by,
            stage="validate",
            apply_result={"ok": True, "message": "Selective default is already correct."},
            details={"routing": routing, "runtime_state_unchanged": True},
            runtime_state_unchanged=True,
        )
    if drift["detected"]:
        orchestrator._log_routing_drift(
            intent=orchestrator.INTENT_SET_SELECTIVE_DEFAULT,
            requested_by=requested_by,
            drift=drift,
        )
    selective_default_artifact_drift_ignorable = (
        _selective_default_artifact_drift_is_ignorable_for_global_direct(
            routing=routing,
            artifact_drift=artifact_drift,
        )
    )
    if artifact_drift["detected"] and not selective_default_artifact_drift_ignorable:
        orchestrator._log_artifact_drift(
            intent=orchestrator.INTENT_SET_SELECTIVE_DEFAULT,
            requested_by=requested_by,
            drift=artifact_drift,
        )

    if (
        str(routing.get("applied_mode") or routing.get("desired_mode") or "").strip().lower() == "direct"
        and not drift["detected"]
        and selective_default_artifact_drift_ignorable
    ):
        committed = orchestrator._commit_selective_default(selective_default=selective_default)
        return orchestrator._build_success_result(
            intent=orchestrator.INTENT_SET_SELECTIVE_DEFAULT,
            job_id=str(job["job_id"]),
            requested_by=requested_by,
            stage="commit",
            apply_result={"ok": True, "message": "Selective default saved; global direct runtime unchanged."},
            details={"routing": committed, "runtime_state_unchanged": True},
            runtime_state_unchanged=True,
        )

    future_routing = dict(routing)
    future_routing["selective_default"] = selective_default
    future_routing["apply_state"] = "applying"

    orchestrator.touch_job_running(str(job["job_id"]))
    mihomo_reconcile = orchestrator.reconcile_mihomo_runtime(
        routing=future_routing,
        job_id=str(job["job_id"]),
    )
    if not mihomo_reconcile["ok"]:
        result = orchestrator._build_failure_result(
            intent=orchestrator.INTENT_SET_SELECTIVE_DEFAULT,
            job_id=str(job["job_id"]),
            requested_by=requested_by,
            stage=str(mihomo_reconcile.get("stage") or "mihomo_reconcile"),
            code="MIHOMO_RECONCILE_FAILED",
            message="Failed to reconcile Mihomo runtime before selective default apply.",
        )
        orchestrator._persist_global_error(code=result["code"], message=result["message"])
        return result

    user_overrides = orchestrator._load_user_override_map()
    server_overrides = orchestrator._load_server_override_map()
    subjects = orchestrator._load_subjects_with_overrides(
        routing=future_routing,
        user_overrides=user_overrides,
        server_overrides=server_overrides,
    )

    apply_result = orchestrator._run_pipeline_for_state(
        job_id=str(job["job_id"]),
        reason=orchestrator.INTENT_SET_SELECTIVE_DEFAULT,
        input_data={"intent": orchestrator.INTENT_SET_SELECTIVE_DEFAULT, "selective_default": selective_default},
        routing=future_routing,
        subjects=subjects,
    )

    if not apply_result["ok"]:
        result = orchestrator._build_failure_result(
            intent=orchestrator.INTENT_SET_SELECTIVE_DEFAULT,
            job_id=str(job["job_id"]),
            requested_by=requested_by,
            stage=str(apply_result.get("stage") or "apply"),
            code=apply_result["dataplane"]["error_code"] or "SELECTIVE_DEFAULT_APPLY_FAILED",
            message=apply_result["dataplane"]["error_message"] or apply_result["dataplane"]["message"],
            apply_id=apply_result["apply_id"],
            details={"apply": apply_result},
        )
        orchestrator._persist_global_error(code=result["code"], message=result["message"])
        return result

    orchestrator.touch_job_running(str(job["job_id"]))
    committed = orchestrator._commit_selective_default(selective_default=selective_default)
    orchestrator._sync_subject_server_override_statuses(subjects)
    return orchestrator._build_success_result(
        intent=orchestrator.INTENT_SET_SELECTIVE_DEFAULT,
        job_id=str(job["job_id"]),
        requested_by=requested_by,
        stage="commit",
        apply_result=apply_result,
        details={"routing": committed},
    )


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
    future_subjects = [
        orchestrator.enrich_subject_with_effective_state(
            (
                {**dict(orchestrator.get_subject(str(current_subject["subject_id"])) or current_subject), "desired_mode": mode}
                if str(current_subject["subject_id"]) in target_subject_ids
                else dict(orchestrator.get_subject(str(current_subject["subject_id"])) or current_subject)
            ),
            routing=routing,
            user_override=user_overrides.get(str(current_subject["subject_id"])),
            server_override=server_overrides.get(str(current_subject["subject_id"])),
            runtime_enforcement=runtime_enforcement,
            bypass_state=bypass_state,
        )
        for current_subject in orchestrator.list_subjects(include_deleted=False, limit=1000)
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
                "enabled": len(subject_ids) == 1 and subject_type in {"lan", "tailscale", "tailscale_node"} and mode in {"direct", "selective", "vpn"},
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
    orchestrator._sync_subject_server_override_statuses(future_subjects)
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
    future_subjects = [
        orchestrator.enrich_subject_with_effective_state(
            dict(orchestrator.get_subject(str(current_subject["subject_id"])) or current_subject),
            routing=routing,
            user_override=user_overrides.get(str(current_subject["subject_id"])),
            server_override=server_overrides.get(str(current_subject["subject_id"])),
            runtime_enforcement=runtime_enforcement,
            bypass_state=bypass_state,
        )
        for current_subject in orchestrator.list_subjects(include_deleted=False, limit=1000)
    ]

    apply_result = orchestrator._run_pipeline_for_state(
        job_id=str(job["job_id"]),
        reason=orchestrator.INTENT_SET_SUBJECT_USER_MODE,
        input_data={
            "intent": orchestrator.INTENT_SET_SUBJECT_USER_MODE,
            "subject_id": subject_id,
            "mode": mode,
            "fast_subject_apply": {
                "enabled": subject_type in {"lan", "tailscale", "tailscale_node"} and mode in {"direct", "selective", "vpn"},
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
    orchestrator._sync_subject_server_override_statuses(future_subjects)
    return orchestrator._build_success_result(
        intent=orchestrator.INTENT_SET_SUBJECT_USER_MODE,
        job_id=str(job["job_id"]),
        requested_by=requested_by,
        stage="commit",
        apply_result=apply_result,
        details={"subject": committed_subject},
    )


def _execute_set_subject_server_override(job: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    requested_by = str(job.get("requested_by") or "api")
    subject_id = str(payload.get("subject_id") or "").strip()
    server_id = str(payload.get("server_id") or "").strip()
    subject = orchestrator.get_subject(subject_id)
    if subject is None:
        return orchestrator._build_failure_result(
            intent=orchestrator.INTENT_SET_SUBJECT_SERVER_OVERRIDE,
            job_id=str(job["job_id"]),
            requested_by=requested_by,
            stage="validate",
            code="SUBJECT_NOT_FOUND",
            message=f"Subject not found: {subject_id}",
        )

    validation = orchestrator._validate_subject_server_override_request(subject, server_id)
    if validation is not None:
        return orchestrator._build_failure_result(
            intent=orchestrator.INTENT_SET_SUBJECT_SERVER_OVERRIDE,
            job_id=str(job["job_id"]),
            requested_by=requested_by,
            stage="validate",
            code=validation["code"],
            message=validation["message"],
            details={"server": validation.get("server")},
        )

    persisted = orchestrator.set_subject_server_override(
        subject_id,
        server_id,
        requested_by=requested_by,
    )
    if not persisted["ok"]:
        return orchestrator._build_failure_result(
            intent=orchestrator.INTENT_SET_SUBJECT_SERVER_OVERRIDE,
            job_id=str(job["job_id"]),
            requested_by=requested_by,
            stage="persist",
            code=str(persisted.get("error_code") or "SUBJECT_SERVER_OVERRIDE_PERSIST_FAILED"),
            message=str(persisted.get("error_message") or "Failed to save subject server override."),
            details=persisted,
        )

    if str(subject.get("subject_type") or "") == "xray":
        materialized = orchestrator.materialize_xray_runtime_bindings(
            requested_by=requested_by,
            prepare_mihomo_handoff=False,
        )
        if not materialized["ok"]:
            materialize_code = str(
                materialized.get("error", {}).get("code")
                or materialized.get("error_code")
                or "XRAY_RUNTIME_MATERIALIZE_FAILED"
            )
            materialize_message = str(
                materialized.get("error", {}).get("message")
                or materialized.get("error_message")
                or "Failed to materialize Xray runtime bindings."
            )
            orchestrator.update_subject_server_override_apply_status(
                subject_id,
                apply_state="failed",
                error_code=materialize_code,
                error_message=materialize_message,
            )
            return orchestrator._build_failure_result(
                intent=orchestrator.INTENT_SET_SUBJECT_SERVER_OVERRIDE,
                job_id=str(job["job_id"]),
                requested_by=requested_by,
                stage="runtime_materialize",
                code=materialize_code,
                message=materialize_message,
                details={
                    "subject": orchestrator.get_subject_with_effective_state(subject_id),
                    "server_override": orchestrator.get_subject_server_override(subject_id),
                    "xray_materialization": materialized,
                },
            )

        future_subjects = orchestrator._load_subjects_with_overrides(
            routing=orchestrator.get_routing_snapshot(),
            user_overrides=orchestrator._load_user_override_map(),
            server_overrides=orchestrator._load_server_override_map(),
        )
        orchestrator._sync_subject_server_override_statuses(future_subjects)
        return orchestrator._build_success_result(
            intent=orchestrator.INTENT_SET_SUBJECT_SERVER_OVERRIDE,
            job_id=str(job["job_id"]),
            requested_by=requested_by,
            stage="commit",
            apply_result={"ok": True, "apply_id": None, "message": "Xray runtime metadata materialized."},
            details={
                "subject": orchestrator.get_subject_with_effective_state(subject_id),
                "server_override": orchestrator.get_subject_server_override(subject_id),
            },
            runtime_state_unchanged=True,
        )

    routing = orchestrator.get_routing_snapshot()
    future_subjects = orchestrator._load_subjects_with_overrides(
        routing=routing,
        user_overrides=orchestrator._load_user_override_map(),
        server_overrides=orchestrator._load_server_override_map(),
    )
    apply_result = orchestrator._run_pipeline_for_state(
        job_id=str(job["job_id"]),
        reason=orchestrator.INTENT_SET_SUBJECT_SERVER_OVERRIDE,
        input_data={
            "intent": orchestrator.INTENT_SET_SUBJECT_SERVER_OVERRIDE,
            "subject_id": subject_id,
            "server_id": server_id,
        },
        routing=routing,
        subjects=future_subjects,
    )

    if str(subject.get("subject_type") or "") == "xray":
        materialized = orchestrator.materialize_xray_runtime_bindings(
            requested_by=requested_by,
            prepare_mihomo_handoff=False,
        )
        if not materialized["ok"]:
            materialize_code = str(
                materialized.get("error", {}).get("code")
                or materialized.get("error_code")
                or "XRAY_RUNTIME_MATERIALIZE_FAILED"
            )
            materialize_message = str(
                materialized.get("error", {}).get("message")
                or materialized.get("error_message")
                or "Failed to materialize Xray runtime bindings."
            )
            orchestrator.update_subject_server_override_apply_status(
                subject_id,
                apply_state="failed",
                error_code=materialize_code,
                error_message=materialize_message,
            )
            return orchestrator._build_failure_result(
                intent=orchestrator.INTENT_SET_SUBJECT_SERVER_OVERRIDE,
                job_id=str(job["job_id"]),
                requested_by=requested_by,
                stage="runtime_materialize",
                code=materialize_code,
                message=materialize_message,
                apply_id=apply_result["apply_id"],
                details={
                    "apply": apply_result,
                    "subject": orchestrator.get_subject_with_effective_state(subject_id),
                    "server_override": orchestrator.get_subject_server_override(subject_id),
                    "xray_materialization": materialized,
                },
            )

    future_subjects = orchestrator._load_subjects_with_overrides(
        routing=routing,
        user_overrides=orchestrator._load_user_override_map(),
        server_overrides=orchestrator._load_server_override_map(),
    )
    orchestrator._sync_subject_server_override_statuses(future_subjects)
    effective = orchestrator.get_subject_with_effective_state(subject_id)
    override_state = orchestrator.get_subject_server_override(subject_id)

    if not apply_result["ok"]:
        return orchestrator._build_failure_result(
            intent=orchestrator.INTENT_SET_SUBJECT_SERVER_OVERRIDE,
            job_id=str(job["job_id"]),
            requested_by=requested_by,
            stage=str(apply_result.get("stage") or "apply"),
            code=apply_result["dataplane"]["error_code"] or "SUBJECT_SERVER_OVERRIDE_APPLY_FAILED",
            message=apply_result["dataplane"]["error_message"] or apply_result["dataplane"]["message"],
            apply_id=apply_result["apply_id"],
            details={
                "apply": apply_result,
                "subject": effective,
                "server_override": override_state,
            },
        )

    return orchestrator._build_success_result(
        intent=orchestrator.INTENT_SET_SUBJECT_SERVER_OVERRIDE,
        job_id=str(job["job_id"]),
        requested_by=requested_by,
        stage="commit",
        apply_result=apply_result,
        details={
            "subject": effective,
            "server_override": override_state,
        },
    )


def _execute_clear_subject_server_override(job: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    requested_by = str(job.get("requested_by") or "api")
    subject_id = str(payload.get("subject_id") or "").strip()
    subject = orchestrator.get_subject(subject_id)
    if subject is None:
        return orchestrator._build_failure_result(
            intent=orchestrator.INTENT_CLEAR_SUBJECT_SERVER_OVERRIDE,
            job_id=str(job["job_id"]),
            requested_by=requested_by,
            stage="validate",
            code="SUBJECT_NOT_FOUND",
            message=f"Subject not found: {subject_id}",
        )

    existing_override = orchestrator.get_subject_server_override(subject_id)
    if existing_override is None:
        return orchestrator._build_success_result(
            intent=orchestrator.INTENT_CLEAR_SUBJECT_SERVER_OVERRIDE,
            job_id=str(job["job_id"]),
            requested_by=requested_by,
            stage="commit",
            apply_result={"apply_id": None, "ok": True},
            details={"subject": orchestrator.get_subject_with_effective_state(subject_id), "server_override": None},
        )

    if str(subject.get("subject_type") or "") == "xray":
        cleared = orchestrator.clear_subject_server_override(subject_id, requested_by=requested_by)
        materialized = orchestrator.materialize_xray_runtime_bindings(
            requested_by=requested_by,
            prepare_mihomo_handoff=False,
        )
        if not materialized["ok"]:
            materialize_code = str(
                materialized.get("error", {}).get("code")
                or materialized.get("error_code")
                or "XRAY_RUNTIME_MATERIALIZE_FAILED"
            )
            materialize_message = str(
                materialized.get("error", {}).get("message")
                or materialized.get("error_message")
                or "Failed to materialize Xray runtime bindings."
            )
            return orchestrator._build_failure_result(
                intent=orchestrator.INTENT_CLEAR_SUBJECT_SERVER_OVERRIDE,
                job_id=str(job["job_id"]),
                requested_by=requested_by,
                stage="runtime_materialize",
                code=materialize_code,
                message=materialize_message,
                details={"server_override": existing_override, "cleared": cleared},
            )

        future_subjects = orchestrator._load_subjects_with_overrides(
            routing=orchestrator.get_routing_snapshot(),
            user_overrides=orchestrator._load_user_override_map(),
            server_overrides=orchestrator._load_server_override_map(),
        )
        orchestrator._sync_subject_server_override_statuses(future_subjects)
        return orchestrator._build_success_result(
            intent=orchestrator.INTENT_CLEAR_SUBJECT_SERVER_OVERRIDE,
            job_id=str(job["job_id"]),
            requested_by=requested_by,
            stage="commit",
            apply_result={"ok": True, "apply_id": None, "message": "Xray runtime metadata materialized."},
            details={
                "subject": orchestrator.get_subject_with_effective_state(subject_id),
                "server_override": orchestrator.get_subject_server_override(subject_id),
            },
            runtime_state_unchanged=True,
        )

    routing = orchestrator.get_routing_snapshot()
    server_overrides = orchestrator._load_server_override_map()
    server_overrides.pop(subject_id, None)
    future_subjects = orchestrator._load_subjects_with_overrides(
        routing=routing,
        user_overrides=orchestrator._load_user_override_map(),
        server_overrides=server_overrides,
    )
    apply_result = orchestrator._run_pipeline_for_state(
        job_id=str(job["job_id"]),
        reason=orchestrator.INTENT_CLEAR_SUBJECT_SERVER_OVERRIDE,
        input_data={"intent": orchestrator.INTENT_CLEAR_SUBJECT_SERVER_OVERRIDE, "subject_id": subject_id},
        routing=routing,
        subjects=future_subjects,
    )

    if not apply_result["ok"]:
        orchestrator.update_subject_server_override_apply_status(
            subject_id,
            apply_state="failed",
            error_code=apply_result["dataplane"]["error_code"] or "SUBJECT_SERVER_OVERRIDE_CLEAR_FAILED",
            error_message=apply_result["dataplane"]["error_message"] or apply_result["dataplane"]["message"],
        )
        return orchestrator._build_failure_result(
            intent=orchestrator.INTENT_CLEAR_SUBJECT_SERVER_OVERRIDE,
            job_id=str(job["job_id"]),
            requested_by=requested_by,
            stage=str(apply_result.get("stage") or "apply"),
            code=apply_result["dataplane"]["error_code"] or "SUBJECT_SERVER_OVERRIDE_CLEAR_FAILED",
            message=apply_result["dataplane"]["error_message"] or apply_result["dataplane"]["message"],
            apply_id=apply_result["apply_id"],
            details={"apply": apply_result, "server_override": existing_override},
        )

    cleared = orchestrator.clear_subject_server_override(subject_id, requested_by=requested_by)
    if str(subject.get("subject_type") or "") == "xray":
        materialized = orchestrator.materialize_xray_runtime_bindings(
            requested_by=requested_by,
            prepare_mihomo_handoff=False,
        )
        if not materialized["ok"]:
            materialize_code = str(
                materialized.get("error", {}).get("code")
                or materialized.get("error_code")
                or "XRAY_RUNTIME_MATERIALIZE_FAILED"
            )
            materialize_message = str(
                materialized.get("error", {}).get("message")
                or materialized.get("error_message")
                or "Failed to materialize Xray runtime bindings."
            )
            return orchestrator._build_failure_result(
                intent=orchestrator.INTENT_CLEAR_SUBJECT_SERVER_OVERRIDE,
                job_id=str(job["job_id"]),
                requested_by=requested_by,
                stage="runtime_materialize",
                code=materialize_code,
                message=materialize_message,
                apply_id=apply_result["apply_id"],
                details={
                    "apply": apply_result,
                    "server_override": existing_override,
                    "xray_materialization": materialized,
                },
            )
    orchestrator._update_subject_apply_state(subject_id, "clean")
    return orchestrator._build_success_result(
        intent=orchestrator.INTENT_CLEAR_SUBJECT_SERVER_OVERRIDE,
        job_id=str(job["job_id"]),
        requested_by=requested_by,
        stage="commit",
        apply_result=apply_result,
        details={
            "subject": orchestrator.get_subject_with_effective_state(subject_id),
            "server_override": cleared,
        },
    )


def _execute_apply_manual_rules(job: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    del payload
    requested_by = str(job.get("requested_by") or "api")
    orchestrator.mark_rules_job_running(job_id=str(job["job_id"]), update_type="manual_apply")
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


def _execute_repair_global_direct_runtime(job: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    del payload
    requested_by = str(job.get("requested_by") or "api")
    routing = orchestrator.get_routing_snapshot()

    subjects = orchestrator._load_subjects_with_overrides(
        routing=routing,
        user_overrides=orchestrator._load_user_override_map(),
        server_overrides=orchestrator._load_server_override_map(),
    )

    apply_result = orchestrator._run_pipeline_for_state(
        job_id=str(job["job_id"]),
        reason=orchestrator.INTENT_REPAIR_GLOBAL_DIRECT_RUNTIME,
        input_data={"intent": orchestrator.INTENT_REPAIR_GLOBAL_DIRECT_RUNTIME},
        routing=routing,
        subjects=subjects,
    )

    if not apply_result["ok"]:
        return orchestrator._build_failure_result(
            intent=orchestrator.INTENT_REPAIR_GLOBAL_DIRECT_RUNTIME,
            job_id=str(job["job_id"]),
            requested_by=requested_by,
            stage=str(apply_result.get("stage") or "apply"),
            code=apply_result["dataplane"]["error_code"] or "REPAIR_APPLY_FAILED",
            message=apply_result["dataplane"]["error_message"] or apply_result["dataplane"]["message"],
            apply_id=apply_result["apply_id"],
            details={"apply": apply_result},
        )

    committed = orchestrator._commit_repaired_global_runtime()
    orchestrator._sync_subject_server_override_statuses(subjects)
    return orchestrator._build_success_result(
        intent=orchestrator.INTENT_REPAIR_GLOBAL_DIRECT_RUNTIME,
        job_id=str(job["job_id"]),
        requested_by=requested_by,
        stage="commit",
        apply_result=apply_result,
        details={"routing": committed},
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
