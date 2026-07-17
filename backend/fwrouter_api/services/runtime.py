from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

from fwrouter_api.adapters.dataplane import (
    DEFAULT_DATAPLANE_ADAPTER,
    DataplaneOperation,
    DataplanePlan,
)
from fwrouter_api.adapters.mihomo import DEFAULT_MIHOMO_ADAPTER
from fwrouter_api.adapters.scripts import DEFAULT_SCRIPT_RUNNER
from fwrouter_api.adapters.xray import DEFAULT_XRAY_ADAPTER
from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import get_cached_schema_state
from fwrouter_api.db.schema_state import summarize_schema_state
from fwrouter_api.services.core_bypass import get_core_bypass_state
from fwrouter_api.services.dataplane_status import build_runtime_enforcement_state
from fwrouter_api.services.dataplane_status import read_live_dataplane_payload
from fwrouter_api.services.modules import fetch_modules
from fwrouter_api.services.server_layout import get_server_root_layout
from fwrouter_api.services.scoped_egress import (
    build_scoped_egress_diagnostics,
    build_scoped_egress_readiness,
)
from fwrouter_api.services.servers import ensure_routing_global_state
from fwrouter_api.services.scoped_egress import summarize_scoped_subjects
from fwrouter_api.services.subject_policy import list_subjects_effective_summaries
from fwrouter_api.services.subscription import get_subscription_state
from fwrouter_api.services.system_subjects import ensure_builtin_system_subjects, enrich_system_subject_summary
from fwrouter_api.services.tailscale import probe_tailscale_runtime
from fwrouter_api.services.traffic import get_traffic_accounting_state
from fwrouter_api.services.live_probe_cache import get_live_probe_cache


def _cached_mihomo_health():
    return get_live_probe_cache(
        "dataplane_global.mihomo_health",
        ttl_seconds=2.0,
        loader=DEFAULT_MIHOMO_ADAPTER.health,
    )


def _cached_xray_health():
    return get_live_probe_cache(
        "runtime.xray_health",
        ttl_seconds=2.0,
        loader=DEFAULT_XRAY_ADAPTER.health,
    )


def _project_module_runtime(
    modules: list[dict[str, Any]],
    *,
    runtime_enforcement: dict[str, Any],
    bypass: dict[str, Any],
    mihomo_health: Any,
    xray_health: Any,
) -> list[dict[str, Any]]:
    projected: list[dict[str, Any]] = []
    enforcement_level = str(runtime_enforcement.get("enforcement_level") or "")
    live_enforced = enforcement_level in {
        "global_direct_enforced",
        "global_selective_enforced",
        "global_vpn_enforced",
    }
    vpn_active = enforcement_level in {"global_selective_enforced", "global_vpn_enforced"}

    for module in modules:
        item = dict(module)
        item["state_source"] = "database"
        name = str(item.get("module_name") or "")
        runtime_state = str(item.get("runtime_state") or "")
        if name == "core" and (live_enforced or bool(bypass.get("enabled"))):
            item["runtime_state"] = "paused" if bool(bypass.get("enabled")) else "running"
            item["apply_state"] = "clean"
            item["status_text"] = (
                "FWRouter core bypass is active; host is in direct-safe contour."
                if bool(bypass.get("enabled"))
                else "FWRouter core runtime is projected from the live dataplane state."
            )
            item["state_source"] = "runtime_projection"
        elif name == "vpn" and (
            runtime_state == "not_configured"
            or (vpn_active and runtime_state not in {"running", "degraded"})
        ):
            item["runtime_state"] = (
                "running"
                if str(getattr(mihomo_health.runtime_state, "value", mihomo_health.runtime_state)) == "running"
                else runtime_state
            )
            item["apply_state"] = "clean" if item["runtime_state"] == "running" else item.get("apply_state")
            if item["runtime_state"] == "running":
                item["status_text"] = "VPN runtime is projected from live Mihomo/dataplane state."
                item["state_source"] = "runtime_projection"
        elif name == "xray" and runtime_state == "not_configured":
            xray_runtime_state = str(getattr(xray_health.runtime_state, "value", xray_health.runtime_state))
            if xray_runtime_state == "running":
                item["runtime_state"] = "running"
                item["apply_state"] = "clean"
                item["status_text"] = "Xray runtime is projected from the live adapter state."
                item["state_source"] = "runtime_projection"
        projected.append(item)
    return projected


def get_runtime_summary() -> dict[str, Any]:
    return get_live_probe_cache(
        "runtime.summary",
        ttl_seconds=2.0,
        loader=_build_runtime_summary,
    )


def _build_startup_automation_status(
    *,
    startup_recovery_enabled: bool,
    runtime_enforcement: dict[str, Any],
) -> dict[str, Any]:
    live_recovery_pending = startup_recovery_enabled and not bool(
        runtime_enforcement.get("traffic_enforcement_guaranteed")
    )
    startup_phase = "startup_live_recovery_pending" if live_recovery_pending else "steady_state"
    legacy_bootstrap_mode = (
        "temporary_direct_safe_bootstrap" if live_recovery_pending else "steady_state"
    )
    phase_message = (
        "Startup live recovery is enabled; until live enforcement is restored the host may still be in a temporary direct-safe contour."
        if live_recovery_pending
        else "Runtime appears to be in steady state."
    )
    recovery_status = "enabled" if startup_recovery_enabled else "disabled_by_config"

    live_recovery = {
        "enabled": startup_recovery_enabled,
        "status": recovery_status,
        "phase": startup_phase,
        "message": phase_message,
        "pending": live_recovery_pending,
    }
    apply_reconcile = {
        "enabled": startup_recovery_enabled,
        "status": recovery_status,
        "phase": "startup_apply_reconcile_available" if startup_recovery_enabled else "disabled_by_config",
        "message": (
            "Startup may run persisted-intent apply/reconcile steps when live state drifts."
            if startup_recovery_enabled
            else "Startup apply/reconcile steps are disabled by config."
        ),
    }
    dnsmasq_reconcile = {
        "enabled": True,
        "status": "enabled",
        "phase": "startup_dnsmasq_reconcile",
        "message": "Startup keeps dnsmasq reconcile as a separate runtime convergence step.",
    }

    return {
        "startup_foundation": {
            "phase": "safe_startup_foundation",
            "message": "Startup foundation covers directories, database, and basic persistent state preparation.",
        },
        "startup_live_recovery": live_recovery,
        "startup_apply_reconcile": apply_reconcile,
        "startup_dnsmasq_reconcile": dnsmasq_reconcile,
        # Legacy compatibility shape retained for callers/tests that still read
        # the older aggregated startup_recovery bucket.
        "startup_recovery": {
            "enabled": startup_recovery_enabled,
            "status": recovery_status,
            "bootstrap_mode": legacy_bootstrap_mode,
            "phase": startup_phase,
            "message": (
                "Startup recovery is enabled; until live recovery and persisted-intent reconcile complete, the host may remain in a temporary direct-safe contour."
                if live_recovery_pending
                else "Startup recovery status reflects the steady-state runtime."
            ),
        },
    }


def _build_runtime_summary() -> dict[str, Any]:
    """Return runtime adapter status without changing live system state."""

    schema_state = get_cached_schema_state()
    database_summary = summarize_schema_state(schema_state)
    settings = get_settings()
    with ThreadPoolExecutor(max_workers=6, thread_name_prefix="fwrouter-runtime") as pool:
        future_mihomo_health = pool.submit(_cached_mihomo_health)
        future_xray_health = pool.submit(_cached_xray_health)
        future_tailscale_probe = pool.submit(probe_tailscale_runtime)
        future_subscription_state = pool.submit(get_subscription_state)
        future_modules = pool.submit(fetch_modules)
        future_live_dataplane_payload = pool.submit(read_live_dataplane_payload)

        mihomo_health = future_mihomo_health.result()
        xray_health = future_xray_health.result()
        tailscale_probe = future_tailscale_probe.result()
        subscription_state = future_subscription_state.result()
        modules = future_modules.result()
        live_dataplane_payload = future_live_dataplane_payload.result()

    applied_manifest_path = settings.paths.generated_dir / "dataplane" / "applied-manifest.json"
    candidate_manifest_path = settings.paths.generated_dir / "dataplane" / "candidate-manifest.json"
    last_good_nft_path = settings.paths.state_dir / "last-good" / "dataplane" / "last-good.nft"
    candidate_nft_path = settings.paths.generated_dir / "dataplane" / "candidate.nft"

    if applied_manifest_path.exists() and last_good_nft_path.exists():
        dataplane_generated_path = str(last_good_nft_path)
        dataplane_manifest_path = str(applied_manifest_path)
    else:
        dataplane_generated_path = str(candidate_nft_path)
        dataplane_manifest_path = str(candidate_manifest_path)

    subscription_status = str(subscription_state.get("status") or "not_configured")
    subscription_error_code = subscription_state.get("error_code")
    subscription_error_message = subscription_state.get("error_message")
    subscription_message = (
        subscription_error_message
        or (
            "Subscription URL is configured."
            if subscription_state.get("url")
            else "Subscription URL is not configured."
        )
    )
    routing = ensure_routing_global_state()
    runtime_enforcement = build_runtime_enforcement_state(
        live_payload=live_dataplane_payload,
        mihomo_health=mihomo_health,
    )
    bypass = get_core_bypass_state()
    modules = _project_module_runtime(
        modules,
        runtime_enforcement=runtime_enforcement,
        bypass=bypass,
        mihomo_health=mihomo_health,
        xray_health=xray_health,
    )
    ensure_builtin_system_subjects()
    subjects = list_subjects_effective_summaries(
        limit=500,
        runtime_enforcement=runtime_enforcement,
        bypass_state=bypass,
    )
    system_subjects = [
        enrich_system_subject_summary(subject)
        for subject in subjects
        if str(subject.get("subject_type") or "") in {"docker", "host", "fwrouter"}
    ][:200]
    scoped_egress = summarize_scoped_subjects(subjects)
    scoped_egress_diagnostics = build_scoped_egress_diagnostics(
        summary=scoped_egress,
        runtime_enforcement=runtime_enforcement,
        bypass=bypass,
    )
    scoped_egress_readiness = build_scoped_egress_readiness(
        diagnostics=scoped_egress_diagnostics,
        runtime_enforcement=runtime_enforcement,
        bypass=bypass,
    )
    routing_drift = {
        "detected": not bool(runtime_enforcement.get("active_mode_matches_intent", True)),
        "code": (
            "ACTIVE_DATAPLANE_MODE_MISMATCH"
            if not bool(runtime_enforcement.get("active_mode_matches_intent", True))
            else None
        ),
        "expected_global_mode": str(
            routing.get("applied_mode") or routing.get("desired_mode") or "direct"
        ).lower(),
        "expected_selective_default": str(
            routing.get("selective_default") or "direct"
        ).lower(),
        "live_global_mode": runtime_enforcement.get("live_global_mode"),
        "live_selective_default": runtime_enforcement.get("live_selective_default"),
        "message": (
            "Persisted routing state does not match live dataplane mode."
            if not bool(runtime_enforcement.get("active_mode_matches_intent", True))
            else None
        ),
    }
    startup_automation = _build_startup_automation_status(
        startup_recovery_enabled=bool(settings.startup_recovery_enabled),
        runtime_enforcement=runtime_enforcement,
    )

    subject_counts_by_type: dict[str, int] = {}
    active_subject_count = 0
    subject_sample: list[dict[str, Any]] = []
    for subject in subjects:
        subject_type = str(subject["subject_type"])
        subject_counts_by_type[subject_type] = subject_counts_by_type.get(subject_type, 0) + 1
        if subject["is_active"]:
            active_subject_count += 1
        if len(subject_sample) < 10:
            subject_sample.append(
                {
                    "subject_id": subject["subject_id"],
                    "subject_type": subject["subject_type"],
                    "effective_mode": subject["effective_state"]["effective_mode"],
                    "dataplane_path": subject["effective_state"]["dataplane_path"],
                    "scoped_runtime": subject["effective_state"].get("scoped_runtime"),
                }
            )

    system_subject_counts = {"docker": 0, "host": 0, "fwrouter": 0}
    system_subject_active_count = 0
    system_subject_sample: list[dict[str, Any]] = []
    for subject in system_subjects:
        subject_type = str(subject["subject_type"])
        if subject_type in system_subject_counts:
            system_subject_counts[subject_type] += 1
        if subject["is_active"]:
            system_subject_active_count += 1
        if len(system_subject_sample) < 10:
            system_subject_sample.append(
                {
                    "subject_id": subject["subject_id"],
                    "subject_type": subject["subject_type"],
                    "visibility": subject.get("visibility"),
                    "effective_mode": subject["effective_state"]["effective_mode"],
                    "dataplane_path": subject["effective_state"]["dataplane_path"],
                }
            )

    return {
        "backend": {
            "layout": get_server_root_layout(),
            "database": database_summary,
        },
        "core_bypass": bypass,
        "modules": modules,
        "routing": routing,
        "dataplane": {
            "adapter": "nft-owned-table",
            "check_ok": bool((live_dataplane_payload or {}).get("ok")),
            "state": runtime_enforcement["enforcement_level"],
            "message": str((live_dataplane_payload or {}).get("message") or "Dataplane runtime summary unavailable."),
            "details": live_dataplane_payload or {},
            "drift": routing_drift,
            "bypass": bypass,
            "scoped_egress": scoped_egress_diagnostics,
            "scoped_egress_readiness": scoped_egress_readiness,
            **runtime_enforcement,
        },
        "mihomo": {
            "adapter": mihomo_health.details.get("adapter", "unknown"),
            "runtime_state": mihomo_health.runtime_state.value,
            "active_server_id": mihomo_health.active_server_id,
            "message": mihomo_health.message,
            "details": mihomo_health.details,
        },
        "xray": {
            "adapter": xray_health.details.get("adapter", "xray"),
            "runtime_state": xray_health.runtime_state.value,
            "message": xray_health.message,
            "forced_vpn_ready": bool(xray_health.details.get("forced_vpn_ready", False)),
            "traffic_available": bool(xray_health.details.get("traffic_available", False)),
            "details": xray_health.details,
        },
        "tailscale": {
            "adapter": tailscale_probe.get("adapter", "allowlist"),
            "runtime_state": tailscale_probe.get("runtime_state", "not_configured"),
            "message": tailscale_probe.get("message"),
            "error_code": tailscale_probe.get("error_code"),
            "error_message": tailscale_probe.get("error_message"),
            "details": tailscale_probe.get("details", {}),
        },
        "subscription": {
            "adapter": "http" if subscription_state.get("url") else "noop",
            "refresh_available": bool(subscription_state.get("url")),
            "status": subscription_status,
            "error_code": subscription_error_code,
            "message": subscription_message,
            "state": {
                **subscription_state,
                "url_saved": bool(subscription_state.get("url")),
            },
        },
        "automation": {
            **startup_automation,
            "maintenance_scheduler": {
                "enabled": bool(settings.maintenance_scheduler_enabled),
                "status": "enabled" if settings.maintenance_scheduler_enabled else "disabled_by_config",
            },
            "runtime_convergence_scheduler": {
                "enabled": bool(settings.runtime_convergence_scheduler_enabled),
                "status": (
                    "enabled"
                    if settings.runtime_convergence_scheduler_enabled
                    else "disabled_by_config"
                ),
                "interval_seconds": settings.runtime_convergence_interval_seconds,
            },
            "watchdog_scheduler": {
                "enabled": bool(settings.watchdog_scheduler_enabled),
                "status": "enabled" if settings.watchdog_scheduler_enabled else "disabled_by_config",
            },
        },
        "scripts": {
            "adapter": "allowlist",
            "allowlist": DEFAULT_SCRIPT_RUNNER.list_specs(),
        },
        "subjects": {
            "total_count": len(subjects),
            "active_count": active_subject_count,
            "by_type": subject_counts_by_type,
            "sample": subject_sample,
        },
        "system_subjects": {
            "total_count": len(system_subjects),
            "active_count": system_subject_active_count,
            "by_type": system_subject_counts,
            "sample": system_subject_sample,
        },
        "traffic_accounting": get_traffic_accounting_state(),
    }


def get_scoped_egress_runtime_summary() -> dict[str, Any]:
    runtime = get_runtime_summary()
    dataplane = runtime.get("dataplane") if isinstance(runtime.get("dataplane"), dict) else {}
    return {
        "diagnostics": dataplane.get("scoped_egress", {}),
        "readiness": dataplane.get("scoped_egress_readiness", {}),
        "core_bypass": runtime.get("core_bypass", {}),
        "routing": runtime.get("routing", {}),
    }
