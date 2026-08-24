from __future__ import annotations

from typing import Any

from fwrouter_api.services import apply_orchestrator as orchestrator
from fwrouter_api.services.apply_orchestrator_handler_common import (
    _reconcile_vpn_runtime_for_apply,
    _selective_default_artifact_drift_is_ignorable_for_global_direct,
)


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
            mihomo_reconcile = _reconcile_vpn_runtime_for_apply(
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

    mihomo_reconcile = _reconcile_vpn_runtime_for_apply(
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
        rules_state = orchestrator.sync_active_selective_default(
            selective_default=selective_default,
            job_id=str(job["job_id"]),
        )
        return orchestrator._build_success_result(
            intent=orchestrator.INTENT_SET_SELECTIVE_DEFAULT,
            job_id=str(job["job_id"]),
            requested_by=requested_by,
            stage="commit",
            apply_result={"ok": True, "message": "Selective default saved; global direct runtime unchanged."},
            details={"routing": committed, "rules_state": rules_state, "runtime_state_unchanged": True},
            runtime_state_unchanged=True,
        )

    future_routing = dict(routing)
    future_routing["selective_default"] = selective_default
    future_routing["apply_state"] = "applying"
    effective_rules = orchestrator.effective_rules_with_selective_default(
        orchestrator.read_effective_rules_artifact(),
        selective_default=selective_default,
    )

    orchestrator.touch_job_running(str(job["job_id"]))
    mihomo_reconcile = orchestrator.mihomo_runtime_satisfies_routing(future_routing)
    if mihomo_reconcile.get("ok"):
        mihomo_reconcile = {
            **mihomo_reconcile,
            "skipped": True,
            "reconcile_action": "none",
            "reconcile_reason": "active_runtime_already_matches_routing",
        }
    else:
        mihomo_reconcile = orchestrator.reconcile_mihomo_selective_default_fast(
            routing=future_routing,
            job_id=str(job["job_id"]),
        )
        if not mihomo_reconcile.get("ok"):
            mihomo_reconcile = _reconcile_vpn_runtime_for_apply(
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
        extra={"rules_effective": effective_rules},
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
    rules_state = orchestrator.sync_active_selective_default(
        selective_default=selective_default,
        job_id=str(job["job_id"]),
        effective_artifact=effective_rules,
    )
    orchestrator._sync_subject_server_override_statuses(subjects)
    return orchestrator._build_success_result(
        intent=orchestrator.INTENT_SET_SELECTIVE_DEFAULT,
        job_id=str(job["job_id"]),
        requested_by=requested_by,
        stage="commit",
        apply_result=apply_result,
        details={"routing": committed, "rules_state": rules_state},
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

