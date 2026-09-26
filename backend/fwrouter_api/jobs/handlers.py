from __future__ import annotations

from typing import Any

from fwrouter_api.jobs.manager import JobManager
from fwrouter_api.services.apply import ApplyMode, run_apply_pipeline
from fwrouter_api.services.apply_orchestrator import execute_apply_mutation
from fwrouter_api.services.jobs_retention import cleanup_jobs_retention
from fwrouter_api.services.runtime import get_runtime_summary
from fwrouter_api.services.server_ping import check_server_delay_sweep
from fwrouter_api.services.subscription_pipeline import prepare_subscription_refresh


def _redact_subscription_state(state: dict[str, Any] | None) -> dict[str, Any] | None:
    if state is None:
        return None

    public = dict(state)
    public["url_saved"] = bool(public.get("url"))
    public.pop("url", None)

    metadata = public.get("metadata")
    if isinstance(metadata, dict):
        metadata_public = dict(metadata)
        metadata_public.pop("url", None)
        public["metadata"] = metadata_public

    return public


def _redact_validation(validation: dict[str, Any] | None) -> dict[str, Any] | None:
    if validation is None:
        return None

    public = dict(validation)
    public["url_saved"] = bool(public.get("normalized_url"))
    public.pop("normalized_url", None)
    return public


def _redact_adapter_refresh(refresh: dict[str, Any] | None) -> dict[str, Any] | None:
    if refresh is None:
        return None

    public = dict(refresh)
    servers = public.pop("servers", []) or []
    public["servers_count"] = len(servers)

    metadata = public.get("metadata")
    if isinstance(metadata, dict):
        metadata_public = dict(metadata)
        metadata_public.pop("url", None)
        public["metadata"] = metadata_public

    return public


def _redact_subscription_prepare_result(result: dict[str, Any]) -> dict[str, Any]:
    public = dict(result)
    refresh = dict(public.get("refresh") or {})

    refresh["validation"] = _redact_validation(refresh.get("validation"))
    refresh["state"] = _redact_subscription_state(refresh.get("state"))
    refresh["refresh"] = _redact_adapter_refresh(refresh.get("refresh"))

    public["refresh"] = refresh
    candidate = public.get("candidate")
    if isinstance(candidate, dict):
        candidate = dict(candidate)
        candidate.pop("_candidate_config", None)
        candidate.pop("config", None)
        candidate.pop("rules", None)
        candidate.pop("handoff_assignments", None)
        public["candidate"] = candidate
    metadata = public.get("prepared_candidate_metadata")
    if isinstance(metadata, dict):
        public["prepared_candidate_metadata"] = {
            key: value for key, value in metadata.items() if not str(key).startswith("_")
        }
    return public


def noop_handler(job: dict[str, Any]) -> dict[str, Any]:
    """Safe no-op handler for wiring tests."""

    return {
        "handler": "noop",
        "job_id": job["job_id"],
        "job_type": job["job_type"],
        "message": "No-op job handler completed.",
    }


def runtime_probe_handler(job: dict[str, Any]) -> dict[str, Any]:
    """Collect runtime adapter summary without changing live system state."""

    return {
        "handler": "runtime_probe",
        "job_id": job["job_id"],
        "runtime": get_runtime_summary(),
    }


def apply_dry_run_handler(job: dict[str, Any]) -> dict[str, Any]:
    """Run safe apply pipeline dry-run.

    This writes job artifacts and operational log entries, but does not apply
    firewall/runtime changes.
    """

    return {
        "handler": "apply_dry_run",
        "job_id": job["job_id"],
        "apply": run_apply_pipeline(
            job_id=job["job_id"],
            reason="job_handler_apply_dry_run",
            mode=ApplyMode.DRY_RUN,
            input_data=job.get("input") or {},
        ),
    }


def subscription_refresh_prepare_handler(job: dict[str, Any]) -> dict[str, Any]:
    """Prepare subscription refresh without applying runtime changes.

    This refreshes provider inventory, syncs servers to SQLite, generates and
    validates a Mihomo candidate config. It does not promote active config and
    does not restart Mihomo.
    """

    subscription = prepare_subscription_refresh()

    return {
        "handler": "subscription_refresh_prepare",
        "job_id": job["job_id"],
        "subscription": _redact_subscription_prepare_result(subscription),
    }


def jobs_retention_cleanup_handler(job: dict[str, Any]) -> dict[str, Any]:
    """Clean old job rows/artifacts according to retention policy.

    By default this runs as dry-run unless input_data explicitly sets
    dry_run=false.
    """

    input_data = job.get("input") or {}
    dry_run = bool(input_data.get("dry_run", True))

    return {
        "handler": "jobs_retention_cleanup",
        "job_id": job["job_id"],
        "retention": cleanup_jobs_retention(dry_run=dry_run),
    }


def server_ping_sweep_handler(job: dict[str, Any]) -> dict[str, Any]:
    """Check latency for a bounded list of active servers.

    By default this runs as dry-run. Set input_data.update_state=true to write
    results into server_ping_state.
    """

    input_data = job.get("input") or {}

    result = check_server_delay_sweep(
        update_state=bool(input_data.get("update_state", False)),
        checked_by=str(input_data.get("checked_by") or "job_server_ping_sweep"),
        timeout_ms=int(input_data.get("timeout_ms") or 10000),
        limit=int(input_data.get("limit") or 10),
    )

    return {
        "handler": "server_ping_sweep",
        "job_id": job["job_id"],
        "ping": result,
    }


def global_fixed_server_apply_handler(job: dict[str, Any]) -> dict[str, Any]:
    from fwrouter_api.services.server_global_selection import run_global_fixed_server_apply_job

    return run_global_fixed_server_apply_job(job)


def xray_client_create_handler(job: dict[str, Any]) -> dict[str, Any]:
    from fwrouter_api.services.xray_clients import run_xray_client_create_job

    return run_xray_client_create_job(job)


def xray_client_delete_handler(job: dict[str, Any]) -> dict[str, Any]:
    from fwrouter_api.services.xray_clients import run_xray_client_delete_job

    return run_xray_client_delete_job(job)


def xray_subscription_profile_delete_handler(job: dict[str, Any]) -> dict[str, Any]:
    from fwrouter_api.services.xray_subscription_service import run_xray_subscription_profile_delete_job

    return run_xray_subscription_profile_delete_job(job)


def apply_mutation_handler(job: dict[str, Any]) -> dict[str, Any]:
    """Run one transactional mutation through the apply orchestrator."""

    return execute_apply_mutation(job)


def register_default_handlers(manager: JobManager) -> None:
    """Register built-in safe handlers."""

    manager.register_handler("noop", noop_handler)
    manager.register_handler("runtime_probe", runtime_probe_handler)
    manager.register_handler("apply_dry_run", apply_dry_run_handler)
    manager.register_handler(
        "subscription_refresh_prepare",
        subscription_refresh_prepare_handler,
    )
    manager.register_handler("jobs_retention_cleanup", jobs_retention_cleanup_handler)
    manager.register_handler("server_ping_sweep", server_ping_sweep_handler)
    manager.register_handler("global_fixed_server_apply", global_fixed_server_apply_handler)
    manager.register_handler("xray_client_create", xray_client_create_handler)
    manager.register_handler("xray_client_delete", xray_client_delete_handler)
    manager.register_handler("xray_subscription_profile_delete", xray_subscription_profile_delete_handler)
    manager.register_handler("apply_mutation", apply_mutation_handler)
