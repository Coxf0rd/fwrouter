from __future__ import annotations

import json
from typing import Any

from fwrouter_api.core.config import get_settings
from fwrouter_api.db.schema_state import summarize_schema_state
from fwrouter_api.services.core_bypass import get_core_bypass_state
from fwrouter_api.services.dataplane_status import build_runtime_enforcement_state
from fwrouter_api.services.live_probe_cache import get_live_probe_cache
from fwrouter_api.services.modules import fetch_modules, find_module
from fwrouter_api.services.runtime import get_scoped_egress_runtime_summary
from fwrouter_api.services.system_subjects import ensure_builtin_system_subjects, list_system_subjects


def _backend_runtime_status(
    *,
    modules: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
    schema_ok: bool,
) -> tuple[str, str]:
    runtime_states = {str(module.get("runtime_state") or "") for module in modules}
    warning_codes = {str(item.get("code") or "") for item in warnings}

    if not modules:
        return "bootstrapping", "FWRouter backend is installed but module inventory is not available yet."
    if not schema_ok:
        return "degraded", "FWRouter backend is running, but SQLite schema rebuild is required."
    if "failed" in runtime_states:
        return "degraded", "FWRouter backend is running with failed runtime modules."
    if "degraded" in runtime_states or warning_codes:
        return "degraded", "FWRouter backend is running with partial runtime readiness."
    if runtime_states.issubset({"running", "paused", "not_configured", "stopped", "clean"}):
        return "ready", "FWRouter backend is running and reporting runtime state."
    return "active", "FWRouter backend is running."


def build_system_summary(*, schema_state: dict[str, Any] | None = None) -> dict[str, Any]:
    schema_summary = (
        summarize_schema_state(schema_state)
        if isinstance(schema_state, dict)
        else None
    )
    cache_suffix = (
        json.dumps(schema_summary, ensure_ascii=False, sort_keys=True)
        if isinstance(schema_summary, dict)
        else "none"
    )
    return get_live_probe_cache(
        f"system_summary:{cache_suffix}",
        ttl_seconds=5.0,
        loader=lambda: _build_system_summary_uncached(schema_summary=schema_summary),
    )


def _build_system_summary_uncached(
    *,
    schema_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the system summary DTO for /api/v2/system/summary."""

    settings = get_settings()
    modules = fetch_modules()
    core_module = find_module(modules, "core")
    tailscale_module = find_module(modules, "tailscale")
    bypass = get_core_bypass_state()
    scoped_egress = get_scoped_egress_runtime_summary()
    ensure_builtin_system_subjects()
    system_subjects = list_system_subjects(limit=200)
    resolved_schema_summary = schema_summary or {
        "ok": True,
        "status": "ok",
        "expected_schema_version": None,
        "actual_schema_version": None,
        "rebuild_required": False,
        "problem_count": 0,
        "drifted_tables": [],
    }
    scoped_egress_readiness = (
        scoped_egress.get("readiness")
        if isinstance(scoped_egress.get("readiness"), dict)
        else {}
    )
    warnings: list[dict[str, Any]] = []
    if bypass["enabled"]:
        warnings.append(
            {
                "code": "FWROUTER_CORE_BYPASS_ACTIVE",
                "severity": "warning",
                "message": (
                    "FWRouter core bypass is active. Dataplane enforcement is intentionally "
                    "direct-safe and dependent runtime modules are paused."
                ),
            }
        )
    if str(scoped_egress_readiness.get("state") or "") == "degraded":
        warnings.append(
            {
                "code": "FWROUTER_SCOPED_EGRESS_DEGRADED",
                "severity": "warning",
                "message": (
                    "Scoped egress is partially configured, but some tracked subjects are "
                    "still pending or unresolved."
                ),
            }
        )
    if str(scoped_egress_readiness.get("state") or "") == "blocked":
        warnings.append(
            {
                "code": "FWROUTER_SCOPED_EGRESS_BLOCKED",
                "severity": "warning",
                "message": (
                    "Scoped egress is blocked by core bypass or missing VPN runtime prerequisites."
                ),
            }
        )
    runtime_enforcement = build_runtime_enforcement_state()
    if not bool(runtime_enforcement.get("active_mode_matches_intent", True)):
        warnings.append(
            {
                "code": "FWROUTER_ACTIVE_DATAPLANE_MODE_MISMATCH",
                "severity": "warning",
                "message": (
                    "Persisted global routing state does not match the live nftables dataplane mode."
                ),
            }
        )
    if tailscale_module and str(tailscale_module.get("runtime_state") or "") in {"degraded", "failed"}:
        warnings.append(
            {
                "code": "FWROUTER_TAILSCALE_DEGRADED",
                "severity": "warning",
                "message": (
                    "Tailscale module is enabled in control plane, but host status probe or "
                    "tailscale-node inventory sync is degraded."
                ),
            }
        )
    if not bool(resolved_schema_summary.get("ok")):
        warnings.append(
            {
                "code": "FWROUTER_DATABASE_SCHEMA_MISMATCH",
                "severity": "warning",
                "message": "SQLite schema drift detected. Rebuild the control-plane database from snapshot.",
            }
        )
    backend_status, backend_message = _backend_runtime_status(
        modules=modules,
        warnings=warnings,
        schema_ok=bool(resolved_schema_summary.get("ok")),
    )

    return {
        "core": {
            "desired_state": (
                core_module["desired_state"] if core_module else "not_configured"
            ),
            "runtime_state": (
                core_module["runtime_state"] if core_module else "not_configured"
            ),
            "apply_state": core_module["apply_state"] if core_module else "pending",
            "status_text": (
                core_module["status_text"]
                if core_module
                else "FWRouter core is not initialized yet."
            ),
            "bypass": bypass,
        },
        "subject_taxonomy": {
            "client_subjects": ["lan", "tailscale_node", "xray"],
            "system_subjects": ["docker", "host", "fwrouter"],
            "notes": [
                "`tailscale` is a module/service concept, not a client subject type.",
                "Legacy subject rows may still be stored as `tailscale`, but backend normalizes them to `tailscale_node`.",
            ],
        },
        "backend": {
            "status": backend_status,
            "version": settings.app_version,
            "environment": settings.environment,
            "message": backend_message,
            "database": resolved_schema_summary,
            "runtime_enforcement": runtime_enforcement,
            "readiness": {
                "scoped_egress": scoped_egress_readiness,
            },
            "system_subjects": {
                "endpoint": "/api/v2/system-subjects",
                "total_count": len(system_subjects),
                "active_count": sum(1 for item in system_subjects if item["is_active"]),
                "builtin_fwrouter_subject": "fwrouter:global",
            },
        },
        "paths": {
            "etc_dir": str(settings.paths.etc_dir),
            "state_dir": str(settings.paths.state_dir),
            "log_dir": str(settings.paths.log_dir),
            "run_dir": str(settings.paths.run_dir),
            "db_path": str(settings.paths.db_path),
        },
        "modules": modules,
        "warnings": warnings,
    }
