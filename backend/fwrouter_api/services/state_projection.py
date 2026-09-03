from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fwrouter_api.adapters import mihomo as mihomo_adapter_module
from fwrouter_api.adapters import xray as xray_adapter_module
from fwrouter_api.db.connection import db_session
from fwrouter_api.services.core_bypass import get_core_bypass_state
from fwrouter_api.services.dataplane_global import read_applied_manifest
from fwrouter_api.services.dataplane_status import build_runtime_enforcement_state, read_live_dataplane_payload
from fwrouter_api.services.modules import fetch_modules
from fwrouter_api.services.rules_state_metadata import list_rules_metadata
from fwrouter_api.services.rules_state_store import get_rules_state
from fwrouter_api.services.scoped_egress import summarize_scoped_subjects
from fwrouter_api.services.server_state import get_routing_global_state
from fwrouter_api.services.subject_policy import list_subjects_with_effective_state
from fwrouter_api.services.subjects import get_subject, list_subjects
from fwrouter_api.services.watchdog_status import load_watchdog_module
from fwrouter_api.services.xray_runtime_state import _load_xray_bindings_state
from fwrouter_api.services.state_projection_types import (
    EntityStateProjectionDTO,
    StateExecutionDTO,
    StateIntentDTO,
    StateObservationDTO,
    StateProjectionDTO,
    StateReconcileDTO,
)


ERROR_OBSERVATION_STATES = {"failed"}
WARNING_OBSERVATION_STATES = {"degraded", "stale", "unknown", "not_configured", "missing"}
RUNNING_OBSERVATION_STATES = {"running", "active"}
INACTIVE_OBSERVATION_STATES = {"inactive", "stopped", "paused"}
ACTIVE_EXECUTION_STATES = {"pending", "running", "applying"}


def _dump(dto: EntityStateProjectionDTO) -> dict[str, Any]:
    return dto.model_dump(mode="json")


def _safe_health(adapter: Any) -> dict[str, Any]:
    try:
        health = adapter.health()
    except Exception as exc:
        return {
            "runtime_state": "failed",
            "message": str(exc),
            "error_code": "RUNTIME_HEALTH_PROBE_FAILED",
            "details": {},
        }
    runtime_state = getattr(health, "runtime_state", "unknown")
    return {
        "runtime_state": str(getattr(runtime_state, "value", runtime_state)),
        "active_server_id": getattr(health, "active_server_id", None),
        "message": getattr(health, "message", None),
        "details": getattr(health, "details", {}) if isinstance(getattr(health, "details", {}), dict) else {},
    }


def _execution_state(legacy_apply_state: str | None, *, error_code: str | None = None) -> str:
    state = str(legacy_apply_state or "").strip().lower()
    if state in {"pending", "applying", "running"}:
        return "running" if state == "applying" else "pending"
    if state == "failed" or error_code:
        return "failed"
    if state in {"clean", "success", "idle", "active"}:
        return "idle"
    return "unknown"


def _basic_projection(
    *,
    execution: StateExecutionDTO,
    observation: StateObservationDTO,
    reconcile: StateReconcileDTO,
    inactive: bool = False,
    disabled: bool = False,
) -> StateProjectionDTO:
    if disabled:
        return StateProjectionDTO(state="disabled", severity="none", message_key="state.disabled")
    if inactive:
        return StateProjectionDTO(state="inactive", severity="info", message_key="state.inactive")
    if execution.state == "failed" or observation.state in ERROR_OBSERVATION_STATES:
        return StateProjectionDTO(state="error", severity="error", message_key="state.error")
    if reconcile.state == "runtime_drift":
        return StateProjectionDTO(
            state="error",
            severity="error",
            message_key="state.runtime_drift",
            recommended_actions=["reconcile"],
        )
    if (
        execution.state in ACTIVE_EXECUTION_STATES
        or observation.stale
        or observation.state in WARNING_OBSERVATION_STATES
        or reconcile.state not in {"in_sync", "not_applicable"}
    ):
        return StateProjectionDTO(state="warning", severity="warning", message_key="state.warning")
    return StateProjectionDTO(state="healthy", severity="none", message_key="state.healthy")


def _module_observation(module: dict[str, Any]) -> StateObservationDTO:
    name = str(module.get("module_name") or "")
    state_source = str(module.get("state_source") or "database")
    return StateObservationDTO(
        state=str(module.get("runtime_state") or "unknown"),
        source="live_projection" if state_source == "runtime_projection" else "database",
        observed_at=module.get("updated_at"),
        evidence={
            "lifecycle_mode": module.get("lifecycle_mode"),
            "installed": bool(module.get("installed")),
            "state_source": state_source,
            "module_name": name,
        },
    )


def _project_module(module: dict[str, Any]) -> EntityStateProjectionDTO:
    desired_state = str(module.get("desired_state") or "disabled")
    execution = StateExecutionDTO(
        state=_execution_state(module.get("apply_state"), error_code=module.get("error_code")),
        legacy_apply_state=module.get("apply_state"),
        error_code=module.get("error_code"),
        error_message=module.get("error_message"),
        updated_at=module.get("updated_at"),
    )
    observation = _module_observation(module)
    disabled = desired_state == "disabled" and observation.state not in RUNNING_OBSERVATION_STATES
    if desired_state == "enabled" and observation.state in RUNNING_OBSERVATION_STATES and execution.state != "failed":
        reconcile = StateReconcileDTO(state="in_sync")
    elif desired_state == "disabled" and observation.state in RUNNING_OBSERVATION_STATES:
        reconcile = StateReconcileDTO(
            state="legacy_ambiguous",
            reason_code="MODULE_DISABLED_BUT_RUNTIME_RUNNING",
        )
    elif execution.state in ACTIVE_EXECUTION_STATES:
        reconcile = StateReconcileDTO(state="intent_newer_than_runtime")
    elif disabled:
        reconcile = StateReconcileDTO(state="not_applicable")
    else:
        reconcile = StateReconcileDTO(state="observation_stale", reason_code="MODULE_RUNTIME_NOT_CONFIRMED")
    return EntityStateProjectionDTO(
        entity={
            "type": "module",
            "id": module.get("module_name"),
            "role": module.get("module_name"),
            "label": module.get("module_name"),
        },
        intent=StateIntentDTO(
            state=desired_state,
            source="database",
            updated_at=module.get("updated_at"),
            details={"lifecycle_mode": module.get("lifecycle_mode")},
        ),
        execution=execution,
        observation=observation,
        reconcile=reconcile,
        projection=_basic_projection(
            execution=execution,
            observation=observation,
            reconcile=reconcile,
            disabled=disabled,
        ),
        legacy={"raw": module},
    )


def build_module_state_projection() -> dict[str, Any]:
    items = [_dump(_project_module(module)) for module in fetch_modules()]
    return {"items": items, "summary": _summary(items)}


def _subject_observation(subject: dict[str, Any], scoped_runtime: dict[str, Any] | None) -> StateObservationDTO:
    runtime_state = str(subject.get("runtime_state") or "unknown")
    is_active = bool(subject.get("is_active"))
    state = "active" if is_active and runtime_state in {"active", "running"} else runtime_state
    evidence = {"is_active": is_active, "is_deleted": bool(subject.get("is_deleted"))}
    if scoped_runtime:
        evidence["scoped_runtime"] = scoped_runtime
    return StateObservationDTO(
        state=state,
        source="database+scoped_runtime" if scoped_runtime else "database",
        observed_at=subject.get("last_seen_at") or subject.get("updated_at"),
        evidence=evidence,
    )


def _project_subject(subject: dict[str, Any]) -> EntityStateProjectionDTO:
    effective = subject.get("effective_state") if isinstance(subject.get("effective_state"), dict) else {}
    scoped_runtime = effective.get("scoped_runtime") if isinstance(effective.get("scoped_runtime"), dict) else None
    desired_mode = str(subject.get("desired_mode") or "global")
    apply_state = str(subject.get("apply_state") or "clean")
    execution = StateExecutionDTO(
        state=_execution_state(apply_state),
        legacy_apply_state=apply_state,
        applied_mode=subject.get("applied_mode"),
        updated_at=subject.get("updated_at"),
        details={
            "effective_mode": effective.get("effective_mode"),
            "dataplane_path": effective.get("dataplane_path"),
        },
    )
    observation = _subject_observation(subject, scoped_runtime)
    inactive = (
        bool(subject.get("is_deleted"))
        or not bool(subject.get("is_active"))
        or observation.state in {"inactive", "missing"}
    )
    scoped_status = str((scoped_runtime or {}).get("status") or "")
    if inactive:
        reconcile = StateReconcileDTO(
            state="not_applicable",
            reason_code="SUBJECT_INACTIVE",
            details={"scoped_runtime_status": scoped_status or None},
        )
    elif scoped_status == "applied":
        reconcile = StateReconcileDTO(state="in_sync")
    elif scoped_status.startswith("pending_"):
        reconcile = StateReconcileDTO(
            state="intent_newer_than_runtime",
            reason_code=scoped_status.upper(),
        )
    elif execution.state in ACTIVE_EXECUTION_STATES:
        reconcile = StateReconcileDTO(state="intent_newer_than_runtime")
    elif subject.get("applied_mode") is None and desired_mode not in {"enabled", "direct"}:
        reconcile = StateReconcileDTO(
            state="legacy_ambiguous",
            reason_code="SUBJECT_APPLIED_MODE_MISSING",
        )
    else:
        reconcile = StateReconcileDTO(state="in_sync")
    return EntityStateProjectionDTO(
        entity={
            "type": "subject",
            "id": subject.get("subject_id"),
            "role": subject.get("subject_role"),
            "label": subject.get("alias") or subject.get("display_name") or subject.get("subject_id"),
        },
        intent=StateIntentDTO(
            state="deleted" if bool(subject.get("is_deleted")) else "configured",
            mode=desired_mode,
            source="database",
            updated_at=subject.get("updated_at"),
            details={
                "subject_type": subject.get("subject_type"),
                "implementation_kind": subject.get("implementation_kind"),
                "effective_mode": effective.get("effective_mode"),
            },
        ),
        execution=execution,
        observation=observation,
        reconcile=reconcile,
        projection=_basic_projection(
            execution=execution,
            observation=observation,
            reconcile=reconcile,
            inactive=inactive,
            disabled=desired_mode == "disabled",
        ),
        legacy={"raw": subject},
    )


def build_subject_state_projection(
    *,
    subject_id: str | None = None,
    include_deleted: bool = False,
    limit: int = 500,
) -> dict[str, Any]:
    if subject_id:
        raw = get_subject(subject_id)
        if raw is None or (bool(raw.get("is_deleted")) and not include_deleted):
            return {"subject": None}
        subjects = [raw]
    else:
        subjects = list_subjects(include_deleted=include_deleted, limit=limit)

    routing = get_routing_global_state()
    runtime_enforcement = build_runtime_enforcement_state()
    bypass = get_core_bypass_state()
    enriched = list_subjects_with_effective_state(
        include_deleted=include_deleted,
        limit=limit,
        runtime_enforcement=runtime_enforcement,
        bypass_state=bypass,
    )
    enriched_by_id = {str(item.get("subject_id")): item for item in enriched}
    items = [_dump(_project_subject(enriched_by_id.get(str(item["subject_id"]), item))) for item in subjects]
    if subject_id:
        return {"subject": items[0] if items else None}
    return {"items": items, "summary": _summary(items)}


def build_routing_state_projection() -> dict[str, Any]:
    routing = get_routing_global_state()
    live_payload = read_live_dataplane_payload()
    runtime = build_runtime_enforcement_state(live_payload=live_payload)
    applied_manifest = read_applied_manifest()
    desired_mode = str((routing or {}).get("desired_mode") or "direct")
    applied_mode = (routing or {}).get("applied_mode")
    apply_state = (routing or {}).get("apply_state")
    execution = StateExecutionDTO(
        state=_execution_state(apply_state, error_code=(routing or {}).get("error_code")),
        legacy_apply_state=apply_state,
        applied_mode=applied_mode,
        error_code=(routing or {}).get("error_code"),
        error_message=(routing or {}).get("error_message"),
        updated_at=(routing or {}).get("updated_at"),
        details={
            "server_mode": (routing or {}).get("server_mode"),
            "active_auto_server_id": (routing or {}).get("active_auto_server_id"),
        },
    )
    live_mode = runtime.get("live_global_mode")
    observation_state = (
        "running"
        if runtime.get("traffic_enforcement_guaranteed")
        else "degraded"
        if runtime.get("enforcement_level") != "owned_table_missing"
        else "missing"
    )
    observation = StateObservationDTO(
        state=observation_state,
        source="live_dataplane",
        observed_at=(live_payload or {}).get("checked_at") or (routing or {}).get("updated_at"),
        evidence={
            "enforcement_level": runtime.get("enforcement_level"),
            "live_global_mode": live_mode,
            "live_selective_default": runtime.get("live_selective_default"),
            "traffic_enforcement_guaranteed": runtime.get("traffic_enforcement_guaranteed"),
            "applied_manifest_present": isinstance(applied_manifest, dict),
        },
    )
    if not routing:
        reconcile = StateReconcileDTO(state="unknown", reason_code="ROUTING_STATE_MISSING")
    elif runtime.get("active_mode_matches_intent"):
        reconcile = StateReconcileDTO(state="in_sync")
    elif execution.state in ACTIVE_EXECUTION_STATES:
        reconcile = StateReconcileDTO(state="intent_newer_than_runtime")
    else:
        reconcile = StateReconcileDTO(
            state="runtime_drift",
            reason_code="ROUTING_LIVE_MODE_MISMATCH",
            details={"desired_mode": desired_mode, "applied_mode": applied_mode, "live_mode": live_mode},
        )
    item = EntityStateProjectionDTO(
        entity={"type": "routing", "id": "global", "role": "routing_core", "label": "Global routing"},
        intent=StateIntentDTO(
            state="configured",
            mode=desired_mode,
            target_id=(routing or {}).get("desired_fixed_server_id") or (routing or {}).get("active_auto_server_id"),
            source="database",
            updated_at=(routing or {}).get("updated_at"),
            details={
                "selective_default": (routing or {}).get("selective_default"),
                "server_mode": (routing or {}).get("server_mode"),
            },
        ),
        execution=execution,
        observation=observation,
        reconcile=reconcile,
        projection=_basic_projection(execution=execution, observation=observation, reconcile=reconcile),
        legacy={"raw": routing or {}, "runtime_enforcement": runtime},
    )
    return {"routing": _dump(item)}


def build_watchdog_state_projection() -> dict[str, Any]:
    module = load_watchdog_module() or {}
    runtime = _read_watchdog_runtime_state()
    desired_state = str(module.get("desired_state") or "disabled")
    execution = StateExecutionDTO(
        state=_execution_state(module.get("apply_state"), error_code=module.get("error_code")),
        legacy_apply_state=module.get("apply_state"),
        error_code=module.get("error_code"),
        error_message=module.get("error_message"),
        updated_at=module.get("updated_at"),
    )
    observation = StateObservationDTO(
        state=str(module.get("runtime_state") or "unknown"),
        source="database+watchdog_state",
        observed_at=runtime.get("updated_at") or module.get("updated_at"),
        evidence=runtime,
    )
    if desired_state == "disabled":
        reconcile = StateReconcileDTO(state="not_applicable")
    elif observation.state in {"running", "paused"} and execution.state != "failed":
        reconcile = StateReconcileDTO(state="in_sync")
    elif observation.state == "degraded":
        reconcile = StateReconcileDTO(state="runtime_drift", reason_code=module.get("error_code"))
    else:
        reconcile = StateReconcileDTO(state="observation_stale", reason_code="WATCHDOG_RUNTIME_NOT_CONFIRMED")
    item = EntityStateProjectionDTO(
        entity={"type": "watchdog", "id": "watchdog", "role": "automation", "label": "Watchdog"},
        intent=StateIntentDTO(state=desired_state, source="database", updated_at=module.get("updated_at")),
        execution=execution,
        observation=observation,
        reconcile=reconcile,
        projection=_basic_projection(
            execution=execution,
            observation=observation,
            reconcile=reconcile,
            disabled=desired_state == "disabled",
        ),
        legacy={"raw": {"module": module, "watchdog_state": runtime}},
    )
    return {"watchdog": _dump(item)}


def _read_watchdog_runtime_state() -> dict[str, Any]:
    with db_session() as connection:
        row = connection.execute(
            """
            SELECT
                id,
                path_key,
                failure_candidate_json,
                last_processed_decision_id,
                last_successful_failover_at,
                failover_path_key,
                previous_target_id,
                selected_target_id,
                cooldown_until,
                updated_at
            FROM watchdog_state
            WHERE id = 1
            """
        ).fetchone()
    if row is None:
        return {
            "id": 1,
            "path_key": None,
            "failure_candidate": None,
            "last_processed_decision_id": None,
            "last_successful_failover_at": None,
            "failover_path_key": None,
            "previous_target_id": None,
            "selected_target_id": None,
            "cooldown_until": None,
            "updated_at": None,
            "present": False,
        }
    try:
        candidate = json.loads(row["failure_candidate_json"]) if row["failure_candidate_json"] else None
    except json.JSONDecodeError:
        candidate = None
    return {
        "id": row["id"],
        "path_key": row["path_key"],
        "failure_candidate": candidate if isinstance(candidate, dict) else None,
        "last_processed_decision_id": row["last_processed_decision_id"],
        "last_successful_failover_at": row["last_successful_failover_at"],
        "failover_path_key": row["failover_path_key"],
        "previous_target_id": row["previous_target_id"],
        "selected_target_id": row["selected_target_id"],
        "cooldown_until": row["cooldown_until"],
        "updated_at": row["updated_at"],
        "present": True,
    }


def build_rules_state_projection() -> dict[str, Any]:
    state = get_rules_state()
    metadata = list_rules_metadata()
    paths = {
        key: value
        for key, value in state.items()
        if key.endswith("_path") and isinstance(value, str) and value
    }
    missing = [key for key, value in paths.items() if not Path(value).exists()]
    error_code = state.get("error_code")
    execution = StateExecutionDTO(
        state=_execution_state(state.get("status"), error_code=error_code),
        legacy_apply_state=state.get("status"),
        job_id=state.get("last_apply_job_id") or state.get("last_update_job_id"),
        error_code=error_code,
        error_message=state.get("error_message"),
        updated_at=state.get("updated_at"),
    )
    observation_state = "running" if state.get("status") in {"clean", "success", "idle"} and not missing else "missing" if missing else "unknown"
    observation = StateObservationDTO(
        state=observation_state,
        source="database+artifact_paths",
        observed_at=state.get("updated_at"),
        evidence={"missing_paths": missing, "metadata_count": len(metadata)},
    )
    if error_code:
        reconcile = StateReconcileDTO(state="runtime_drift", reason_code=error_code)
    elif missing:
        reconcile = StateReconcileDTO(state="observation_stale", reason_code="RULES_ARTIFACTS_MISSING")
    elif state.get("status") in {"clean", "success", "idle"}:
        reconcile = StateReconcileDTO(state="in_sync")
    else:
        reconcile = StateReconcileDTO(state="intent_newer_than_runtime")
    item = EntityStateProjectionDTO(
        entity={"type": "rules", "id": "rules", "role": "routing_rules", "label": "Routing rules"},
        intent=StateIntentDTO(
            state="configured" if state.get("status") != "not_configured" else "not_configured",
            mode=state.get("selective_default"),
            source="database+artifacts",
            updated_at=state.get("updated_at"),
        ),
        execution=execution,
        observation=observation,
        reconcile=reconcile,
        projection=_basic_projection(execution=execution, observation=observation, reconcile=reconcile),
        legacy={"raw": {"state": state, "metadata": metadata}},
    )
    return {"rules": _dump(item)}


def build_xray_state_projection() -> dict[str, Any]:
    bindings = _load_xray_bindings_state()
    health = _safe_health(xray_adapter_module.DEFAULT_XRAY_ADAPTER)
    module = next((item for item in fetch_modules() if item.get("module_name") == "xray"), {})
    execution = StateExecutionDTO(
        state=_execution_state(module.get("apply_state"), error_code=module.get("error_code")),
        legacy_apply_state=module.get("apply_state"),
        error_code=module.get("error_code") or bindings.get("error_code"),
        error_message=module.get("error_message") or health.get("message"),
        updated_at=module.get("updated_at"),
    )
    runtime_state = str(health.get("runtime_state") or "unknown")
    observation = StateObservationDTO(
        state=runtime_state,
        source="xray_adapter+xray_bindings",
        observed_at=bindings.get("generated_at") or module.get("updated_at"),
        evidence={
            "bindings_count": bindings.get("bindings_count", 0),
            "applied_count": bindings.get("applied_count", 0),
            "health": health,
        },
    )
    if runtime_state == "running" and not execution.error_code:
        reconcile = StateReconcileDTO(state="in_sync")
    elif runtime_state == "failed":
        reconcile = StateReconcileDTO(state="runtime_drift", reason_code="XRAY_RUNTIME_FAILED")
    else:
        reconcile = StateReconcileDTO(state="observation_stale", reason_code="XRAY_RUNTIME_NOT_CONFIRMED")
    item = EntityStateProjectionDTO(
        entity={"type": "xray", "id": "xray", "role": "explicit_client_runtime", "label": "Xray"},
        intent=StateIntentDTO(
            state=str(module.get("desired_state") or "disabled"),
            source="database",
            updated_at=module.get("updated_at"),
        ),
        execution=execution,
        observation=observation,
        reconcile=reconcile,
        projection=_basic_projection(execution=execution, observation=observation, reconcile=reconcile),
        legacy={"raw": {"module": module, "bindings": bindings}},
    )
    return {"xray": _dump(item)}


def build_vpn_state_projection() -> dict[str, Any]:
    health = _safe_health(mihomo_adapter_module.DEFAULT_MIHOMO_ADAPTER)
    module = next((item for item in fetch_modules() if item.get("module_name") == "vpn"), {})
    routing = get_routing_global_state() or {}
    runtime_state = str(health.get("runtime_state") or "unknown")
    execution = StateExecutionDTO(
        state=_execution_state(module.get("apply_state"), error_code=module.get("error_code")),
        legacy_apply_state=module.get("apply_state"),
        error_code=module.get("error_code"),
        error_message=module.get("error_message") or health.get("message"),
        updated_at=module.get("updated_at"),
        details={"active_server_id": health.get("active_server_id")},
    )
    observation = StateObservationDTO(
        state=runtime_state,
        source="mihomo_adapter",
        observed_at=module.get("updated_at"),
        evidence={"health": health, "routing": routing},
    )
    routing_mode = str(routing.get("desired_mode") or "direct")
    if routing_mode in {"selective", "vpn"} and runtime_state != "running":
        reconcile = StateReconcileDTO(state="runtime_drift", reason_code="VPN_ADAPTER_UNAVAILABLE")
    elif runtime_state == "running":
        reconcile = StateReconcileDTO(state="in_sync")
    else:
        reconcile = StateReconcileDTO(state="not_applicable")
    item = EntityStateProjectionDTO(
        entity={"type": "vpn", "id": "vpn", "role": "vpn_dataplane", "label": "VPN dataplane"},
        intent=StateIntentDTO(
            state=str(module.get("desired_state") or "disabled"),
            mode=routing_mode,
            target_id=routing.get("active_auto_server_id") or routing.get("desired_fixed_server_id"),
            source="database",
            updated_at=module.get("updated_at"),
        ),
        execution=execution,
        observation=observation,
        reconcile=reconcile,
        projection=_basic_projection(execution=execution, observation=observation, reconcile=reconcile),
        legacy={"raw": {"module": module, "routing": routing}},
    )
    return {"vpn": _dump(item)}


def _summary(items: list[dict[str, Any]]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    reconcile_counts: dict[str, int] = {}
    for item in items:
        state = str(((item.get("projection") or {}).get("state")) or "unknown")
        counts[state] = counts.get(state, 0) + 1
        reconcile_state = str(((item.get("reconcile") or {}).get("state")) or "unknown")
        reconcile_counts[reconcile_state] = reconcile_counts.get(reconcile_state, 0) + 1
    return {
        "total_count": len(items),
        "projection_counts": counts,
        "reconcile_counts": reconcile_counts,
        "healthy_count": counts.get("healthy", 0),
        "warning_count": counts.get("warning", 0),
        "error_count": counts.get("error", 0),
        "inactive_count": counts.get("inactive", 0),
        "drift_count": reconcile_counts.get("runtime_drift", 0),
    }


def _read_db_table_counts() -> dict[str, int]:
    with db_session() as connection:
        rows = connection.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
              AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        ).fetchall()
        result: dict[str, int] = {}
        for row in rows:
            table = str(row["name"])
            count_row = connection.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()
            result[table] = int(count_row["count"] if count_row else 0)
    return result


def build_system_state_projection() -> dict[str, Any]:
    modules = build_module_state_projection()
    subjects = build_subject_state_projection()
    routing = build_routing_state_projection()
    watchdog = build_watchdog_state_projection()
    rules = build_rules_state_projection()
    xray = build_xray_state_projection()
    vpn = build_vpn_state_projection()
    items = [
        *modules["items"],
        *subjects["items"],
        routing["routing"],
        watchdog["watchdog"],
        rules["rules"],
        xray["xray"],
        vpn["vpn"],
    ]
    return {
        "summary": _summary(items),
        "modules": modules,
        "subjects": subjects,
        "routing": routing["routing"],
        "watchdog": watchdog["watchdog"],
        "rules": rules["rules"],
        "xray": xray["xray"],
        "vpn": vpn["vpn"],
        "diagnostics": {
            "db_table_counts": _read_db_table_counts(),
            "contract": "read_only_state_projection_v1",
        },
    }
