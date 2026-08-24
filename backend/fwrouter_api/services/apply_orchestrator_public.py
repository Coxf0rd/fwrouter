from __future__ import annotations

from typing import Any

from fwrouter_api.services.apply_orchestrator_constants import (
    INTENT_APPLY_MANUAL_RULES,
    INTENT_CLEAR_SUBJECT_USER_MODE,
    INTENT_REPAIR_GLOBAL_DIRECT_RUNTIME,
    INTENT_SET_GLOBAL_MODE,
    INTENT_SET_SELECTIVE_DEFAULT,
    INTENT_SET_SUBJECT_ADMIN_MODE,
    INTENT_SET_SUBJECT_USER_MODE,
    JOB_TYPE_APPLY_MUTATION,
)
from fwrouter_api.services.apply_orchestrator_jobs import _lock_for_intent
from fwrouter_api.services.jobs import JobLockConflictError


def _job_manager() -> Any:
    from fwrouter_api.services import apply_orchestrator as orchestrator

    return orchestrator.get_default_job_manager()


from fwrouter_api.services.apply_orchestrator import (  # noqa: E402
    _build_failure_result,
    _current_routing_drift,
    ensure_routing_global_state,
    get_routing_snapshot,
)


class ApplyOrchestrator:
    """Thin facade around Wave 1 transactional mutation orchestration."""

    @staticmethod
    def submit(
        *,
        intent: str,
        payload: dict[str, Any],
        requested_by: str = "api",
        run_now: bool = True,
    ) -> dict[str, Any]:
        return submit_apply_mutation(
            intent=intent,
            payload=payload,
            requested_by=requested_by,
            run_now=run_now,
        )

    @staticmethod
    def run(
        *,
        intent: str,
        payload: dict[str, Any],
        requested_by: str = "api",
    ) -> dict[str, Any]:
        return run_apply_mutation(
            intent=intent,
            payload=payload,
            requested_by=requested_by,
        )


def submit_apply_mutation(
    *,
    intent: str,
    payload: dict[str, Any],
    requested_by: str = "api",
    run_now: bool = True,
) -> dict[str, Any]:
    manager = _job_manager()
    try:
        job = manager.create(
            JOB_TYPE_APPLY_MUTATION,
            lock_key=_lock_for_intent(intent),
            requested_by=requested_by,
            input_data={
                "intent": intent,
                "payload": payload,
            },
        )
    except JobLockConflictError:
        raise

    if run_now:
        job = manager.start_job_and_wait(job["job_id"]) or job
    else:
        job = manager.start_job(job["job_id"]) or job
    return job


def run_apply_mutation(
    *,
    intent: str,
    payload: dict[str, Any],
    requested_by: str = "api",
) -> dict[str, Any]:
    job = submit_apply_mutation(
        intent=intent,
        payload=payload,
        requested_by=requested_by,
        run_now=True,
    )
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    mutation = result.get("mutation") if isinstance(result, dict) else None
    if isinstance(mutation, dict):
        return mutation
    if job.get("status") == "running":
        return _build_failure_result(
            intent=intent,
            job_id=str(job["job_id"]),
            requested_by=requested_by,
            stage="job",
            code="JOB_RUNNING",
            message="Mutation job is still running; poll job status for completion.",
        )

    return _build_failure_result(
        intent=intent,
        job_id=str(job["job_id"]),
        requested_by=requested_by,
        stage="job",
        code=job.get("error_code") or "JOB_FAILED",
        message=job.get("error_message") or "Mutation job failed.",
    )


def set_subject_mode(
    subject_id: str,
    mode: str,
    *,
    actor_scope: str = "admin",
    requested_by: str = "api",
) -> dict[str, Any]:
    intent = (
        INTENT_SET_SUBJECT_USER_MODE
        if actor_scope == "user"
        else INTENT_SET_SUBJECT_ADMIN_MODE
    )
    return run_apply_mutation(
        intent=intent,
        payload={"subject_id": subject_id, "mode": mode},
        requested_by=requested_by,
    )


def set_subject_admin_mode(
    subject_id: str,
    mode: str,
    *,
    requested_by: str = "api",
) -> dict[str, Any]:
    return run_apply_mutation(
        intent=INTENT_SET_SUBJECT_ADMIN_MODE,
        payload={"subject_id": subject_id, "mode": mode},
        requested_by=requested_by,
    )


def set_subject_user_mode(
    subject_id: str,
    mode: str,
    *,
    requested_by: str = "api",
) -> dict[str, Any]:
    return run_apply_mutation(
        intent=INTENT_SET_SUBJECT_USER_MODE,
        payload={"subject_id": subject_id, "mode": mode},
        requested_by=requested_by,
    )


def clear_subject_user_mode(
    subject_id: str,
    *,
    requested_by: str = "api",
) -> dict[str, Any]:
    return run_apply_mutation(
        intent=INTENT_CLEAR_SUBJECT_USER_MODE,
        payload={"subject_id": subject_id},
        requested_by=requested_by,
    )


def set_global_mode(
    mode: str,
    *,
    requested_by: str = "api",
) -> dict[str, Any]:
    return run_apply_mutation(
        intent=INTENT_SET_GLOBAL_MODE,
        payload={"mode": mode},
        requested_by=requested_by,
    )


def reconcile_current_routing_if_drift(
    *,
    requested_by: str = "api",
) -> dict[str, Any]:
    """Reapply the persisted routing intent only when live dataplane drift exists."""

    routing = get_routing_snapshot() or ensure_routing_global_state()
    drift = _current_routing_drift(routing=routing)
    if not drift.get("detected"):
        return {
            "ok": True,
            "action": "none",
            "drift_detected": False,
            "drift": drift,
            "routing": routing,
            "message": "Live dataplane matches persisted routing intent.",
        }

    mode = str(
        (routing or {}).get("desired_mode")
        or (routing or {}).get("applied_mode")
        or "direct"
    ).strip().lower()
    mutation = set_global_mode(mode, requested_by=requested_by)
    return {
        "ok": bool(mutation.get("ok")),
        "action": "reapply_global_mode",
        "drift_detected": True,
        "drift": drift,
        "routing": routing,
        "mutation": mutation,
        "message": (
            "Live dataplane drift detected; persisted routing intent was reapplied."
            if mutation.get("ok")
            else "Live dataplane drift detected, but reapply failed."
        ),
        "error_code": None if mutation.get("ok") else mutation.get("code"),
        "error_message": None if mutation.get("ok") else mutation.get("message"),
    }


def apply_global_mode_immediately(
    mode: str,
    *,
    requested_by: str = "api",
) -> dict[str, Any]:
    """Apply global mode synchronously for startup/bootstrap recovery.

    Startup recovery must use the same job lifecycle as normal API mutations:
    queued -> running -> success/failed. Otherwise the apply can complete while
    the SQLite jobs row remains queued and keeps the apply lock stale.
    """

    return run_apply_mutation(
        intent=INTENT_SET_GLOBAL_MODE,
        payload={"mode": mode},
        requested_by=requested_by,
    )


def set_selective_default(
    selective_default: str,
    *,
    requested_by: str = "api",
) -> dict[str, Any]:
    return run_apply_mutation(
        intent=INTENT_SET_SELECTIVE_DEFAULT,
        payload={"selective_default": selective_default},
        requested_by=requested_by,
    )


def apply_manual_rules(
    *,
    requested_by: str = "api",
) -> dict[str, Any]:
    return run_apply_mutation(
        intent=INTENT_APPLY_MANUAL_RULES,
        payload={},
        requested_by=requested_by,
    )


def repair_global_direct_runtime(
    *,
    requested_by: str = "api",
    run_now: bool = True,
) -> dict[str, Any]:
    return submit_apply_mutation(
        intent=INTENT_REPAIR_GLOBAL_DIRECT_RUNTIME,
        payload={},
        requested_by=requested_by,
        run_now=run_now,
    )


def repair_global_direct_runtime_sync(
    *,
    requested_by: str = "api",
) -> dict[str, Any]:
    return submit_apply_mutation(
        intent=INTENT_REPAIR_GLOBAL_DIRECT_RUNTIME,
        payload={},
        requested_by=requested_by,
        run_now=True,
    )
