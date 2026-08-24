from __future__ import annotations

from typing import Any

from fwrouter_api.services import apply_orchestrator as orchestrator
from fwrouter_api.services.apply_orchestrator_handler_common import (
    _subject_needs_mihomo_selector_from_committed,
    _switch_subject_mihomo_selector,
)
from fwrouter_api.services.subject_taxonomy import is_explicit_external_client_subject_type


def _execute_set_subject_server_override(job: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    requested_by = str(job.get("requested_by") or "api")
    subject_id = str(payload.get("subject_id") or "").strip()
    server_id = str(payload.get("server_id") or "").strip()
    actor_scope = str(payload.get("actor_scope") or "user").strip().lower()
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

    desired_mode = str(subject.get("desired_mode") or "").strip().lower()
    if actor_scope == "user" and desired_mode in {"direct", "disabled"}:
        return orchestrator._build_failure_result(
            intent=orchestrator.INTENT_SET_SUBJECT_SERVER_OVERRIDE,
            job_id=str(job["job_id"]),
            requested_by=requested_by,
            stage="validate",
            code="SUBJECT_SERVER_OVERRIDE_ADMIN_LOCKED",
            message="Subject server override is locked by the admin-selected mode.",
            details={"subject_id": subject_id, "admin_mode": desired_mode},
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

    if is_explicit_external_client_subject_type(str(subject.get("subject_type") or "")):
        materialized = orchestrator.materialize_explicit_external_client_runtime_bindings(
            str(subject.get("subject_type") or ""),
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
                    "explicit_client_materialization": materialized,
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
            apply_result={
                "ok": True,
                "apply_id": None,
                "message": "Explicit external client runtime metadata materialized.",
            },
            details={
                "subject": orchestrator.get_subject_with_effective_state(subject_id),
                "server_override": orchestrator.get_subject_server_override(subject_id),
            },
            runtime_state_unchanged=True,
        )

    routing = orchestrator.get_routing_snapshot()
    needs_selector = _subject_needs_mihomo_selector_from_committed(subject, routing=routing)
    mihomo_selector_switch: dict[str, Any] | None = None
    mihomo_reconcile: dict[str, Any] | None = None
    if needs_selector:
        mihomo_selector_switch = _switch_subject_mihomo_selector(subject_id, server_id)
        if not mihomo_selector_switch["ok"] and mihomo_selector_switch.get("error_code") == "MIHOMO_SELECTOR_NOT_FOUND":
            mihomo_reconcile = _reconcile_vpn_runtime_for_apply(
                routing=routing,
                job_id=str(job["job_id"]),
            )
            if mihomo_reconcile["ok"]:
                mihomo_selector_switch = _switch_subject_mihomo_selector(subject_id, server_id)
        if not mihomo_selector_switch["ok"]:
            orchestrator.update_subject_server_override_apply_status(
                subject_id,
                apply_state="failed",
                error_code=str(mihomo_selector_switch.get("error_code") or "MIHOMO_SUBJECT_SELECTOR_SWITCH_FAILED"),
                error_message=str(mihomo_selector_switch.get("error_message") or "Failed to switch subject Mihomo selector."),
            )
            return orchestrator._build_failure_result(
                intent=orchestrator.INTENT_SET_SUBJECT_SERVER_OVERRIDE,
                job_id=str(job["job_id"]),
                requested_by=requested_by,
                stage="mihomo_selector_switch",
                code=str(mihomo_selector_switch.get("error_code") or "MIHOMO_SUBJECT_SELECTOR_SWITCH_FAILED"),
                message=str(mihomo_selector_switch.get("error_message") or "Failed to switch subject Mihomo selector."),
                details={
                    "mihomo_selector_switch": mihomo_selector_switch,
                    "mihomo_reconcile": mihomo_reconcile,
                    "subject": orchestrator.get_subject_with_effective_state(subject_id),
                    "server_override": orchestrator.get_subject_server_override(subject_id),
                },
            )

        orchestrator.update_subject_server_override_apply_status(subject_id, apply_state="clean")
    else:
        orchestrator.update_subject_server_override_apply_status(
            subject_id,
            apply_state="pending",
            error_code=orchestrator._scoped_runtime_error_code("pending_not_vpn_path"),
            error_message=orchestrator._scoped_runtime_message("pending_not_vpn_path"),
        )
    override_state = orchestrator.get_subject_server_override(subject_id)
    return orchestrator._build_success_result(
        intent=orchestrator.INTENT_SET_SUBJECT_SERVER_OVERRIDE,
        job_id=str(job["job_id"]),
        requested_by=requested_by,
        stage="commit",
        apply_result={
            "ok": True,
            "apply_id": None,
            "message": "Subject server selector switched; dataplane unchanged.",
        },
        details={
            "subject": orchestrator.get_subject(subject_id),
            "server_override": override_state,
            "mihomo_selector_switch": mihomo_selector_switch,
            "mihomo_reconcile": mihomo_reconcile,
        },
        runtime_state_unchanged=True,
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

    if is_explicit_external_client_subject_type(str(subject.get("subject_type") or "")):
        cleared = orchestrator.clear_subject_server_override(subject_id, requested_by=requested_by)
        materialized = orchestrator.materialize_explicit_external_client_runtime_bindings(
            str(subject.get("subject_type") or ""),
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
            apply_result={
                "ok": True,
                "apply_id": None,
                "message": "Explicit external client runtime metadata materialized.",
            },
            details={
                "subject": orchestrator.get_subject_with_effective_state(subject_id),
                "server_override": orchestrator.get_subject_server_override(subject_id),
            },
            runtime_state_unchanged=True,
        )

    mihomo_selector_switch = _switch_subject_mihomo_selector(subject_id, "vpn-global")
    if (
        not mihomo_selector_switch["ok"]
        and mihomo_selector_switch.get("error_code") != "MIHOMO_SELECTOR_NOT_FOUND"
    ):
        return orchestrator._build_failure_result(
            intent=orchestrator.INTENT_CLEAR_SUBJECT_SERVER_OVERRIDE,
            job_id=str(job["job_id"]),
            requested_by=requested_by,
            stage="mihomo_selector_switch",
            code=str(mihomo_selector_switch.get("error_code") or "MIHOMO_SUBJECT_SELECTOR_SWITCH_FAILED"),
            message=str(mihomo_selector_switch.get("error_message") or "Failed to reset subject Mihomo selector."),
            details={
                "mihomo_selector_switch": mihomo_selector_switch,
                "server_override": existing_override,
            },
        )
    cleared = orchestrator.clear_subject_server_override(subject_id, requested_by=requested_by)
    return orchestrator._build_success_result(
        intent=orchestrator.INTENT_CLEAR_SUBJECT_SERVER_OVERRIDE,
        job_id=str(job["job_id"]),
        requested_by=requested_by,
        stage="commit",
        apply_result={
            "ok": True,
            "apply_id": None,
            "message": "Subject server selector reset; dataplane unchanged.",
        },
        details={
            "subject": orchestrator.get_subject(subject_id),
            "server_override": cleared,
            "mihomo_selector_switch": mihomo_selector_switch,
        },
        runtime_state_unchanged=True,
    )

