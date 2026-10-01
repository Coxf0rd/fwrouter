from __future__ import annotations
from typing import Any

PROVIDER_OPERATION = "subscription_provider_operation"


def run_provider_job(job: dict[str, Any]) -> dict[str, Any]:
    from fwrouter_api.services.provider_managed import execute_provider_operation
    from fwrouter_api.services.events import write_operational_event
    from fwrouter_api.services.live_probe_cache import clear_live_probe_cache
    payload = job.get("input") or {}
    try:
        result = execute_provider_operation(
            payload["source_ref"], payload["action"], member_id=payload.get("member_id"),
            protocol=payload.get("protocol"), location_id=payload.get("location_id"), auto=payload.get("auto"), priority=payload.get("priority"),
            expected_revision=payload.get("expected_revision"),
        )
    except Exception:
        result = {"ok": False, "outcome": "unconfirmed", "error_code": "PROVIDER_OPERATION_FAILED", "last_good_retained": True}
    clear_live_probe_cache()
    code = "provider_operation_verified" if result.get("ok") else "provider_operation_unconfirmed"
    write_operational_event(event_type=code, event_code=code, message="Provider operation completed.",
                            severity="info" if result.get("ok") else "warning", entity_type="subscription",
                            entity_id=payload.get("source_ref"), job_id=job["job_id"],
                            details={"source_ref": payload.get("source_ref"), "operation": payload.get("action"), **result})
    return {**result, "job_status": "success" if result.get("ok") else "failed",
            "error_message": result.get("error_code"), "operation": PROVIDER_OPERATION}


def register_provider_handler(manager: Any) -> None:
    manager.register_handler(PROVIDER_OPERATION, run_provider_job)
