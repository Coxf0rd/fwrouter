from __future__ import annotations

from typing import Any

from fwrouter_api.services.apply import ApplyMode
from fwrouter_api.services.core_bypass import get_core_bypass_state
from fwrouter_api.services.subject_taxonomy import (
    explicit_external_client_runtime_binding,
)
from fwrouter_api.services.xray import materialize_xray_runtime_bindings


def _facade_attr(name: str) -> Any:
    from fwrouter_api.services import apply_orchestrator as orchestrator

    return getattr(orchestrator, name)


def _run_pipeline_for_state(
    *,
    job_id: str,
    reason: str,
    input_data: dict[str, Any],
    routing: dict[str, Any],
    subjects: list[dict[str, Any]],
    extra: dict[str, Any] | None = None,
    core_intent_revision: int | None = None,
) -> dict[str, Any]:
    extra_payload = dict(extra or {})
    if not isinstance(extra_payload.get("core_bypass"), dict):
        extra_payload["core_bypass"] = get_core_bypass_state()

    return _facade_attr("run_apply_pipeline")(
        job_id=job_id,
        reason=reason,
        mode=ApplyMode.APPLY,
        input_data=input_data,
        manifest_state={
            "routing_global_state": routing,
            "subjects": subjects,
            "extra": extra_payload,
        },
        core_intent_revision=core_intent_revision,
    )


def _run_pipeline_for_manifest(
    *,
    job_id: str,
    reason: str,
    input_data: dict[str, Any],
    manifest: dict[str, Any],
    core_intent_revision: int | None = None,
) -> dict[str, Any]:
    return _facade_attr("run_apply_pipeline")(
        job_id=job_id,
        reason=reason,
        mode=ApplyMode.APPLY,
        input_data=input_data,
        prebuilt_manifest=manifest,
        core_intent_revision=core_intent_revision,
    )


def materialize_explicit_external_client_runtime_bindings(
    subject_type: str,
    *,
    requested_by: str,
    prepare_mihomo_handoff: bool,
) -> dict[str, Any]:
    runtime_binding = explicit_external_client_runtime_binding(subject_type)
    if runtime_binding == "xray_runtime_bindings":
        return materialize_xray_runtime_bindings(
            requested_by=requested_by,
            prepare_mihomo_handoff=prepare_mihomo_handoff,
        )
    return {
        "ok": False,
        "error_code": "EXPLICIT_CLIENT_RUNTIME_BINDING_UNSUPPORTED",
        "error_message": f"Explicit external client runtime binding is not supported for subject type: {subject_type}.",
    }


def _commit_manual_rules_apply(
    *,
    job_id: str,
    draft_text: str,
    effective_artifact: dict[str, Any],
    runtime_enforcement: dict[str, Any],
    audit_change: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return _facade_attr("finalize_manual_rules_apply")(
        job_id=job_id,
        manual_active_text=draft_text,
        effective_artifact=effective_artifact,
        runtime_enforcement=runtime_enforcement,
        audit_change=audit_change,
    )
