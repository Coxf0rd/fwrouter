from __future__ import annotations

from fwrouter_api.jobs.manager import JobManager
from fwrouter_api.services.apply import ApplyMode, run_apply_pipeline
from fwrouter_api.services.core_bypass import core_bypass_handler
from fwrouter_api.services.maintenance import run_control_plane_maintenance
from fwrouter_api.services.rules import run_rules_full_update
from fwrouter_api.services.subject_inventory import sync_subject_inventory
from fwrouter_api.services.subject_policy import expire_subject_overrides
from fwrouter_api.services.traffic import collect_traffic_from_script, record_traffic_samples


def _compact_script_result(script_result: object) -> dict[str, object] | None:
    if not isinstance(script_result, dict):
        return None

    compact: dict[str, object] = {
        "script_id": script_result.get("script_id"),
        "returncode": script_result.get("returncode"),
        "duration_seconds": script_result.get("duration_seconds"),
        "ok": script_result.get("ok"),
    }
    stderr = str(script_result.get("stderr") or "").strip()
    if stderr:
        compact["stderr"] = stderr[:1000]
    return compact


def _compact_traffic_result(traffic: object) -> dict[str, object]:
    if not isinstance(traffic, dict):
        return {}

    compact_keys = [
        "ok",
        "dry_run",
        "collector",
        "collected_at",
        "period_month",
        "received_count",
        "valid_count",
        "processed_count",
        "invalid_count",
        "updated_count",
        "seeded_count",
        "total_rx_delta",
        "total_tx_delta",
        "script_id",
        "error_code",
        "error_message",
    ]
    compact = {
        key: traffic.get(key)
        for key in compact_keys
        if key in traffic
    }

    state = traffic.get("state")
    if isinstance(state, dict):
        compact["state"] = {
            key: state.get(key)
            for key in [
                "enabled",
                "last_collected_at",
                "last_collected_age_seconds",
                "signal_authoritative",
                "safe_for_watchdog_auto",
                "snapshots_count",
                "monthly_rows_count",
                "monthly_subjects_count",
            ]
            if key in state
        }

    script_result = _compact_script_result(traffic.get("script_result"))
    if script_result is not None:
        compact["script_result"] = script_result

    invalid_samples = traffic.get("invalid_samples")
    if isinstance(invalid_samples, list) and invalid_samples:
        compact["invalid_sample_count"] = len(invalid_samples)
        compact["invalid_sample_preview"] = invalid_samples[:5]

    return compact


def apply_control_plane_dry_run_handler(job: dict[str, object]) -> dict[str, object]:
    return {
        "handler": "apply_control_plane_dry_run",
        "job_id": job["job_id"],
        "apply": run_apply_pipeline(
            job_id=str(job["job_id"]),
            reason="job_handler_apply_control_plane_dry_run",
            mode=ApplyMode.DRY_RUN,
            input_data=job.get("input") if isinstance(job.get("input"), dict) else {},
        ),
    }


def maintenance_cleanup_handler(job: dict[str, object]) -> dict[str, object]:
    input_data = job.get("input") if isinstance(job.get("input"), dict) else {}
    dry_run = bool(input_data.get("dry_run", True))

    return {
        "handler": "maintenance_cleanup",
        "job_id": job["job_id"],
        "maintenance": run_control_plane_maintenance(dry_run=dry_run),
    }


def expire_subject_overrides_handler(job: dict[str, object]) -> dict[str, object]:
    input_data = job.get("input") if isinstance(job.get("input"), dict) else {}
    dry_run = bool(input_data.get("dry_run", True))

    return {
        "handler": "expire_subject_overrides",
        "job_id": job["job_id"],
        "overrides": expire_subject_overrides(dry_run=dry_run),
    }


def traffic_accounting_collect_handler(job: dict[str, object]) -> dict[str, object]:
    input_data = job.get("input") if isinstance(job.get("input"), dict) else {}
    dry_run = bool(input_data.get("dry_run", True))
    collector = str(input_data.get("collector") or "job")

    if bool(input_data.get("use_script", False)):
        raw_extra_args = input_data.get("extra_args", [])
        extra_args = [
            str(item)
            for item in raw_extra_args
            if isinstance(raw_extra_args, list) and isinstance(item, (str, int, float))
        ]
        traffic = collect_traffic_from_script(
            script_id=str(input_data.get("script_id") or "traffic_collect"),
            dry_run=dry_run,
            collector=collector,
            extra_args=extra_args,
        )
    else:
        raw_samples = input_data.get("samples")
        samples = raw_samples if isinstance(raw_samples, list) else []
        traffic = record_traffic_samples(samples, collector=collector, dry_run=dry_run)

    return {
        "handler": "traffic_accounting_collect",
        "job_id": job["job_id"],
        "traffic": _compact_traffic_result(traffic),
    }


def subject_inventory_sync_handler(job: dict[str, object]) -> dict[str, object]:
    input_data = job.get("input") if isinstance(job.get("input"), dict) else {}
    return {
        "handler": "subject_inventory_sync",
        "job_id": job["job_id"],
        "subjects": sync_subject_inventory(
            requested_by=str(job.get("requested_by") or "job"),
            discover_docker=bool(input_data.get("discover_docker", True)),
            discover_host=bool(input_data.get("discover_host", False)),
            discover_tailscale=bool(input_data.get("discover_tailscale", False)),
            discover_external_ingress_providers=(
                input_data.get("discover_external_ingress_providers")
                if isinstance(input_data.get("discover_external_ingress_providers"), list)
                else []
            ),
            discover_xray=bool(input_data.get("discover_xray", False)),
            include_all_external_ingress_peers=bool(
                input_data.get("include_all_external_ingress_peers", False)
            ),
            include_all_tailscale_peers=bool(input_data.get("include_all_tailscale_peers", False)),
            lan_clients=input_data.get("lan_clients") if isinstance(input_data.get("lan_clients"), list) else [],
            tailscale_nodes=input_data.get("tailscale_nodes") if isinstance(input_data.get("tailscale_nodes"), list) else [],
            host_services=input_data.get("host_services") if isinstance(input_data.get("host_services"), list) else [],
        ),
    }


def rules_full_update_handler(job: dict[str, object]) -> dict[str, object]:
    return run_rules_full_update(job)


def register_extended_handlers(manager: JobManager) -> None:
    manager.register_handler("apply_control_plane_dry_run", apply_control_plane_dry_run_handler)
    manager.register_handler("core_bypass", core_bypass_handler)
    manager.register_handler("maintenance_cleanup", maintenance_cleanup_handler)
    manager.register_handler("expire_subject_overrides", expire_subject_overrides_handler)
    manager.register_handler("traffic_accounting_collect", traffic_accounting_collect_handler)
    manager.register_handler("subject_inventory_sync", subject_inventory_sync_handler)
    manager.register_handler("rules_full_update", rules_full_update_handler)
