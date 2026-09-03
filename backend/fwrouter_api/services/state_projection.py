from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
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
from fwrouter_api.services.subject_policy import enrich_subject_with_effective_state
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
DEFAULT_STALE_AFTER_SECONDS = 300
LIVE_PROBE_STALE_AFTER_SECONDS = 30


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


def _parse_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _format_timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def compute_staleness(
    observed_at: Any,
    *,
    stale_after_seconds: int | None = DEFAULT_STALE_AFTER_SECONDS,
    now: datetime | None = None,
) -> dict[str, Any]:
    observed = _parse_timestamp(observed_at)
    if observed is None or stale_after_seconds is None:
        return {"stale": False, "stale_after": None, "age_seconds": None}
    stale_after = observed + timedelta(seconds=stale_after_seconds)
    current = (now or datetime.now(UTC)).astimezone(UTC)
    return {
        "stale": current > stale_after,
        "stale_after": _format_timestamp(stale_after),
        "age_seconds": max(0, int((current - observed).total_seconds())),
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


def compute_reconcile_state(
    *,
    intent_state: str | None = None,
    execution_state: str | None = None,
    observation_state: str | None = None,
    intent_mode: str | None = None,
    live_mode: str | None = None,
    runtime_error: str | None = None,
    inactive: bool = False,
    disabled: bool = False,
    stale: bool = False,
    active_required: bool = False,
    scoped_status: str | None = None,
    legacy_ambiguity: str | None = None,
    drift_reason: str | None = None,
    details: dict[str, Any] | None = None,
) -> StateReconcileDTO:
    resolved_details = dict(details or {})
    if inactive:
        return StateReconcileDTO(state="not_applicable", reason_code="ENTITY_INACTIVE", details=resolved_details)
    if disabled and observation_state not in RUNNING_OBSERVATION_STATES:
        return StateReconcileDTO(state="not_applicable", reason_code="INTENT_DISABLED", details=resolved_details)
    if runtime_error:
        return StateReconcileDTO(state="runtime_drift", reason_code=runtime_error, details=resolved_details)
    if legacy_ambiguity:
        return StateReconcileDTO(state="legacy_ambiguous", reason_code=legacy_ambiguity, details=resolved_details)
    if scoped_status == "applied":
        return StateReconcileDTO(state="in_sync", details=resolved_details)
    if scoped_status and scoped_status.startswith("pending_"):
        return StateReconcileDTO(
            state="intent_newer_than_runtime",
            reason_code=scoped_status.upper(),
            details=resolved_details,
        )
    if execution_state in ACTIVE_EXECUTION_STATES:
        return StateReconcileDTO(state="intent_newer_than_runtime", details=resolved_details)
    if intent_mode and live_mode and intent_mode != live_mode:
        return StateReconcileDTO(
            state="runtime_drift",
            reason_code=drift_reason or "INTENT_RUNTIME_MODE_MISMATCH",
            details=resolved_details,
        )
    if stale:
        return StateReconcileDTO(state="observation_stale", reason_code="OBSERVATION_STALE", details=resolved_details)
    if active_required and observation_state not in RUNNING_OBSERVATION_STATES:
        return StateReconcileDTO(
            state="observation_stale",
            reason_code="RUNTIME_NOT_CONFIRMED",
            details=resolved_details,
        )
    return StateReconcileDTO(state="in_sync", details=resolved_details)


def compute_health_level(
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


def _basic_projection(
    *,
    execution: StateExecutionDTO,
    observation: StateObservationDTO,
    reconcile: StateReconcileDTO,
    inactive: bool = False,
    disabled: bool = False,
) -> StateProjectionDTO:
    return compute_health_level(
        execution=execution,
        observation=observation,
        reconcile=reconcile,
        inactive=inactive,
        disabled=disabled,
    )


def _read_routing_global_state_readonly() -> dict[str, Any] | None:
    with db_session() as connection:
        row = connection.execute(
            """
            SELECT
                id,
                desired_mode,
                applied_mode,
                selective_default,
                server_mode,
                desired_fixed_server_id,
                applied_fixed_server_id,
                fixed_server_until,
                active_auto_server_id,
                apply_state,
                error_code,
                error_message,
                updated_at
            FROM routing_global_state
            WHERE id = 1
            """
        ).fetchone()
    return dict(row) if row is not None else None


def _routing_snapshot_readonly() -> dict[str, Any]:
    return _read_routing_global_state_readonly() or {
        "desired_mode": "direct",
        "applied_mode": None,
        "selective_default": "direct",
        "server_mode": "auto",
        "desired_fixed_server_id": None,
        "applied_fixed_server_id": None,
        "fixed_server_until": None,
        "active_auto_server_id": None,
        "apply_state": "pending",
        "error_code": None,
        "error_message": None,
        "updated_at": None,
    }


def _read_active_user_overrides_readonly(subject_ids: list[str]) -> dict[str, dict[str, Any]]:
    normalized = [str(subject_id).strip() for subject_id in subject_ids if str(subject_id).strip()]
    if not normalized:
        return {}
    placeholders = ", ".join("?" for _ in normalized)
    with db_session() as connection:
        rows = connection.execute(
            f"""
            SELECT subject_id, override_mode, override_until, created_by, updated_at
            FROM subject_user_overrides
            WHERE subject_id IN ({placeholders})
              AND override_mode IS NOT NULL
              AND override_until > CURRENT_TIMESTAMP
            """,
            tuple(normalized),
        ).fetchall()
    return {str(row["subject_id"]): dict(row) for row in rows}


def _read_active_server_overrides_readonly(subject_ids: list[str]) -> dict[str, dict[str, Any]]:
    normalized = [str(subject_id).strip() for subject_id in subject_ids if str(subject_id).strip()]
    if not normalized:
        return {}
    placeholders = ", ".join("?" for _ in normalized)
    with db_session() as connection:
        rows = connection.execute(
            f"""
            SELECT subject_id, selected_server_id, selected_until, apply_state, error_code, error_message, updated_at
            FROM subject_server_overrides
            WHERE subject_id IN ({placeholders})
              AND selected_server_id IS NOT NULL
              AND selected_until > CURRENT_TIMESTAMP
            """,
            tuple(normalized),
        ).fetchall()
    return {str(row["subject_id"]): dict(row) for row in rows}


def _read_server_runtime_summary_readonly(server_id: str | None) -> dict[str, Any] | None:
    if not server_id:
        return None
    with db_session() as connection:
        row = connection.execute(
            """
            SELECT
                s.server_id,
                s.server_name,
                s.inventory_state,
                s.updated_at,
                p.vpn_auto,
                p.vpn_auto_priority,
                p.global_list,
                ping.status AS health_status,
                ping.last_ping_ms,
                ping.checked_at,
                ping.error_code AS health_error_code,
                ping.error_message AS health_error_message
            FROM servers s
            LEFT JOIN server_preferences p ON p.server_id = s.server_id
            LEFT JOIN server_ping_state ping ON ping.server_id = s.server_id
            WHERE s.server_id = ?
            """,
            (server_id,),
        ).fetchone()
    return dict(row) if row is not None else None


def _module_observation(module: dict[str, Any], runtime_context: dict[str, Any] | None = None) -> StateObservationDTO:
    name = str(module.get("module_name") or "")
    state_source = str(module.get("state_source") or "database")
    runtime = (runtime_context or {}).get(name) if isinstance(runtime_context, dict) else None
    if isinstance(runtime, dict):
        observed_at = runtime.get("observed_at") or module.get("updated_at")
        staleness = compute_staleness(
            observed_at,
            stale_after_seconds=runtime.get("stale_after_seconds", LIVE_PROBE_STALE_AFTER_SECONDS),
        )
        return StateObservationDTO(
            state=str(runtime.get("observed_state") or module.get("runtime_state") or "unknown"),
            source=str(runtime.get("source") or "runtime_probe"),
            observed_at=observed_at,
            stale_after=staleness["stale_after"],
            stale=bool(staleness["stale"]),
            evidence={
                "lifecycle_mode": module.get("lifecycle_mode"),
                "installed": bool(module.get("installed")),
                "state_source": state_source,
                "module_name": name,
                "intent_state": module.get("desired_state"),
                "observed_state": runtime.get("observed_state"),
                "stale_after": staleness["stale_after"],
                "age_seconds": staleness["age_seconds"],
                **(runtime.get("evidence") if isinstance(runtime.get("evidence"), dict) else {}),
            },
        )
    staleness = compute_staleness(module.get("updated_at"))
    return StateObservationDTO(
        state=str(module.get("runtime_state") or "unknown"),
        source="live_projection" if state_source == "runtime_projection" else "database",
        observed_at=module.get("updated_at"),
        stale_after=staleness["stale_after"],
        stale=bool(staleness["stale"]),
        evidence={
            "lifecycle_mode": module.get("lifecycle_mode"),
            "installed": bool(module.get("installed")),
            "state_source": state_source,
            "module_name": name,
            "intent_state": module.get("desired_state"),
            "observed_state": module.get("runtime_state"),
            "stale_after": staleness["stale_after"],
            "age_seconds": staleness["age_seconds"],
        },
    )


def _project_module(module: dict[str, Any], runtime_context: dict[str, Any] | None = None) -> EntityStateProjectionDTO:
    desired_state = str(module.get("desired_state") or "disabled")
    execution = StateExecutionDTO(
        state=_execution_state(module.get("apply_state"), error_code=module.get("error_code")),
        legacy_apply_state=module.get("apply_state"),
        error_code=module.get("error_code"),
        error_message=module.get("error_message"),
        updated_at=module.get("updated_at"),
    )
    observation = _module_observation(module, runtime_context)
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
        identity={
            "module_name": module.get("module_name"),
            "stable_key": module.get("module_name"),
            "display_name": module.get("module_name"),
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
        effective={
            "intent_state": desired_state,
            "observed_state": observation.state,
            "reconcile_state": reconcile.state,
            "stale_after": observation.stale_after,
        },
        reason={"code": reconcile.reason_code, "source": observation.source},
        legacy={"raw": module},
    )


def _module_runtime_context() -> dict[str, dict[str, Any]]:
    runtime_enforcement = build_runtime_enforcement_state()
    bypass = get_core_bypass_state()
    mihomo_health = _safe_health(mihomo_adapter_module.DEFAULT_MIHOMO_ADAPTER)
    xray_health = _safe_health(xray_adapter_module.DEFAULT_XRAY_ADAPTER)
    watchdog_runtime = _read_watchdog_runtime_state()
    core_state = (
        "paused"
        if bool(bypass.get("enabled"))
        else "running"
        if bool(runtime_enforcement.get("traffic_enforcement_guaranteed"))
        else "degraded"
        if runtime_enforcement.get("enforcement_level") != "owned_table_missing"
        else "stopped"
    )
    return {
        "core": {
            "observed_state": core_state,
            "source": "dataplane_probe",
            "observed_at": runtime_enforcement.get("checked_at"),
            "evidence": {"runtime_enforcement": runtime_enforcement, "bypass": bypass},
        },
        "vpn": {
            "observed_state": str(mihomo_health.get("runtime_state") or "unknown"),
            "source": "mihomo_adapter",
            "evidence": {"health": mihomo_health},
        },
        "xray": {
            "observed_state": str(xray_health.get("runtime_state") or "unknown"),
            "source": "xray_adapter",
            "evidence": {"health": xray_health},
        },
        "watchdog": {
            "observed_state": "running" if bool(watchdog_runtime.get("present")) else "unknown",
            "source": "watchdog_state",
            "observed_at": watchdog_runtime.get("updated_at"),
            "evidence": watchdog_runtime,
        },
    }


def build_module_state_projection() -> dict[str, Any]:
    runtime_context = _module_runtime_context()
    items = [_dump(_project_module(module, runtime_context)) for module in fetch_modules()]
    return {"items": items, "summary": _summary(items)}


def _subject_observation(subject: dict[str, Any], scoped_runtime: dict[str, Any] | None) -> StateObservationDTO:
    runtime_state = str(subject.get("runtime_state") or "unknown")
    is_active = bool(subject.get("is_active"))
    state = "active" if is_active and runtime_state in {"active", "running"} else runtime_state
    evidence = {"is_active": is_active, "is_deleted": bool(subject.get("is_deleted"))}
    if scoped_runtime:
        evidence["scoped_runtime"] = scoped_runtime
    staleness = compute_staleness(subject.get("last_seen_at") or subject.get("updated_at"))
    return StateObservationDTO(
        state=state,
        source="database+scoped_runtime" if scoped_runtime else "database",
        observed_at=subject.get("last_seen_at") or subject.get("updated_at"),
        stale_after=staleness["stale_after"],
        stale=bool(staleness["stale"]) if is_active else False,
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
            "server_override": effective.get("server_override"),
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
        reconcile = compute_reconcile_state(
            scoped_status=scoped_status,
            details={"scoped_runtime_status": scoped_status or None},
        )
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
        identity={
            "subject_id": subject.get("subject_id"),
            "stable_key": subject.get("stable_key"),
            "display_name": subject.get("alias") or subject.get("display_name") or subject.get("subject_id"),
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
        effective={
            "mode": effective.get("effective_mode"),
            "mode_source": effective.get("mode_source"),
            "dataplane_path": effective.get("dataplane_path"),
            "selected_server_id": effective.get("selected_server_id"),
            "selected_server_source": effective.get("selected_server_source"),
            "scoped_runtime_status": scoped_status or None,
            "control_plane_direct_safe": (scoped_runtime or {}).get("control_plane_direct_safe"),
            "inventory_classification": (scoped_runtime or {}).get("inventory_classification"),
        },
        reason={
            "code": (scoped_runtime or {}).get("resolution_reason") or reconcile.reason_code,
            "mode_source": effective.get("mode_source"),
            "source": observation.source,
        },
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

    routing = _routing_snapshot_readonly()
    runtime_enforcement = build_runtime_enforcement_state()
    bypass = get_core_bypass_state()
    subject_ids = [str(subject["subject_id"]) for subject in subjects]
    user_overrides = _read_active_user_overrides_readonly(subject_ids)
    server_overrides = _read_active_server_overrides_readonly(subject_ids)
    enriched = [
        enrich_subject_with_effective_state(
            subject,
            routing=routing,
            user_override=user_overrides.get(str(subject["subject_id"])),
            server_override=server_overrides.get(str(subject["subject_id"])),
            runtime_enforcement=runtime_enforcement,
            bypass_state=bypass,
        )
        for subject in subjects
    ]
    enriched_by_id = {str(item.get("subject_id")): item for item in enriched}
    items = [_dump(_project_subject(enriched_by_id.get(str(item["subject_id"]), item))) for item in subjects]
    if subject_id:
        return {"subject": items[0] if items else None}
    return {"items": items, "summary": _summary(items)}


def build_routing_state_projection() -> dict[str, Any]:
    routing = _read_routing_global_state_readonly()
    live_payload = read_live_dataplane_payload()
    runtime = build_runtime_enforcement_state(live_payload=live_payload)
    applied_manifest = read_applied_manifest()
    rules_state = get_rules_state()
    rules_metadata = list_rules_metadata()
    profile = runtime.get("profile") if isinstance(runtime.get("profile"), dict) else {}
    selective_rules = runtime.get("selective_rules")
    if not isinstance(selective_rules, dict):
        selective_rules = {}
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
            "selected_server_id": (routing or {}).get("desired_fixed_server_id") or (routing or {}).get("active_auto_server_id"),
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
            "dataplane_status": {
                "supported_modes": runtime.get("supported_modes"),
                "missing_runtime_requirements": runtime.get("missing_runtime_requirements"),
                "bypass_active": runtime.get("bypass_active"),
            },
            "selective_rules": selective_rules,
            "direct_exceptions": {
                "selective_default": (routing or {}).get("selective_default"),
                "protected_ipv4_count": len(profile.get("protected_ipv4") or []),
                "protected_ipv6_count": len(profile.get("protected_ipv6") or []),
            },
            "forced_vpn_bindings": {
                "subject_scoped_count": selective_rules.get("subject_scoped_count"),
                "xray_forced_vpn": True,
            },
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
                "global_mode": desired_mode,
                "selective_rules": {
                    "status": rules_state.get("status"),
                    "metadata_count": len(rules_metadata),
                    "counts": selective_rules,
                },
                "direct_exceptions": {
                    "source": "dataplane_profile",
                    "selective_default": (routing or {}).get("selective_default"),
                },
                "forced_vpn_bindings": {"xray_clients": "runtime_binding_required"},
            },
        ),
        execution=execution,
        observation=observation,
        reconcile=reconcile,
        projection=_basic_projection(execution=execution, observation=observation, reconcile=reconcile),
        effective={
            "global_mode": live_mode,
            "desired_global_mode": desired_mode,
            "selective_default": runtime.get("live_selective_default") or (routing or {}).get("selective_default"),
            "dataplane_status": observation.state,
            "rules_runtime_confirmed": bool(runtime.get("traffic_enforcement_guaranteed")),
        },
        reason={"code": reconcile.reason_code, "source": observation.source},
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
    runtime_enforcement = build_runtime_enforcement_state()
    bypass = get_core_bypass_state()
    routing = _routing_snapshot_readonly()
    xray_subjects = [
        subject
        for subject in list_subjects(include_deleted=False, limit=1000)
        if bool(subject.get("is_active"))
        and str(subject.get("implementation_kind") or "") == "xray"
    ]
    server_overrides = _read_active_server_overrides_readonly(
        [str(subject["subject_id"]) for subject in xray_subjects]
    )
    active_xray_subjects = [
        enrich_subject_with_effective_state(
            subject,
            routing=routing,
            user_override=None,
            server_override=server_overrides.get(str(subject["subject_id"])),
            runtime_enforcement=runtime_enforcement,
            bypass_state=bypass,
        )
        for subject in xray_subjects
    ]
    binding_items = bindings.get("bindings") if isinstance(bindings.get("bindings"), list) else []
    binding_subject_ids = {
        str(binding.get("subject_id"))
        for binding in binding_items
        if isinstance(binding, dict) and binding.get("subject_id") is not None
    }
    active_subject_ids = {str(subject.get("subject_id")) for subject in active_xray_subjects}
    applied_binding_subject_ids = {
        str(binding.get("subject_id"))
        for binding in binding_items
        if isinstance(binding, dict) and str(binding.get("status") or "") == "applied"
    }
    pending_subject_ids = [
        str(subject.get("subject_id"))
        for subject in active_xray_subjects
        if str((((subject.get("effective_state") or {}).get("scoped_runtime") or {}).get("status") or "")).startswith("pending_")
    ]
    failed_binding_ids = [
        str(binding.get("subject_id"))
        for binding in binding_items
        if isinstance(binding, dict) and str(binding.get("status") or "") == "failed"
    ]
    stale_binding_ids = sorted(binding_subject_ids - active_subject_ids)
    missing_binding_ids = sorted(active_subject_ids - applied_binding_subject_ids)
    active_bound_count = len(active_subject_ids & applied_binding_subject_ids)
    execution = StateExecutionDTO(
        state=_execution_state(module.get("apply_state"), error_code=module.get("error_code")),
        legacy_apply_state=module.get("apply_state"),
        error_code=module.get("error_code") or bindings.get("error_code"),
        error_message=module.get("error_message") or health.get("message"),
        updated_at=module.get("updated_at"),
        details={
            "pending_subject_ids": pending_subject_ids,
            "failed_binding_ids": failed_binding_ids,
        },
    )
    runtime_state = str(health.get("runtime_state") or "unknown")
    observed_at = bindings.get("generated_at") or module.get("updated_at")
    staleness = compute_staleness(observed_at)
    observation = StateObservationDTO(
        state=runtime_state,
        source="xray_adapter+xray_bindings",
        observed_at=observed_at,
        stale_after=staleness["stale_after"],
        stale=bool(staleness["stale"]),
        evidence={
            "bindings_count": bindings.get("bindings_count", 0),
            "applied_count": bindings.get("applied_count", 0),
            "active_clients_count": len(active_xray_subjects),
            "active_bound_count": active_bound_count,
            "missing_binding_ids": missing_binding_ids,
            "stale_binding_ids": stale_binding_ids,
            "pending_subject_ids": pending_subject_ids,
            "failed_binding_ids": failed_binding_ids,
            "health": health,
        },
    )
    if execution.error_code:
        reconcile = StateReconcileDTO(state="runtime_drift", reason_code=execution.error_code)
    elif runtime_state == "running" and not missing_binding_ids and not failed_binding_ids:
        reconcile = StateReconcileDTO(state="in_sync")
    elif runtime_state == "failed":
        reconcile = StateReconcileDTO(state="runtime_drift", reason_code="XRAY_RUNTIME_FAILED")
    elif missing_binding_ids:
        reconcile = StateReconcileDTO(
            state="intent_newer_than_runtime",
            reason_code="XRAY_ACTIVE_CLIENT_BINDING_MISSING",
            details={"missing_binding_ids": missing_binding_ids},
        )
    else:
        reconcile = StateReconcileDTO(state="observation_stale", reason_code="XRAY_RUNTIME_NOT_CONFIRMED")
    item = EntityStateProjectionDTO(
        entity={"type": "xray", "id": "xray", "role": "explicit_client_runtime", "label": "Xray"},
        identity={"stable_key": "module:xray", "display_name": "Xray"},
        intent=StateIntentDTO(
            state=str(module.get("desired_state") or "disabled"),
            source="database",
            updated_at=module.get("updated_at"),
            details={"active_clients_count": len(active_xray_subjects), "forced_vpn": True},
        ),
        execution=execution,
        observation=observation,
        reconcile=reconcile,
        projection=_basic_projection(execution=execution, observation=observation, reconcile=reconcile),
        effective={
            "active_clients_count": len(active_xray_subjects),
            "runtime_bindings_count": len(binding_subject_ids),
            "applied_bindings_count": len(applied_binding_subject_ids),
            "stale_bindings_count": len(stale_binding_ids),
            "pending_apply_count": len(pending_subject_ids),
            "failed_apply_count": len(failed_binding_ids),
        },
        reason={"code": reconcile.reason_code, "source": observation.source},
        legacy={"raw": {"module": module, "bindings": bindings}},
    )
    return {"xray": _dump(item)}


def build_vpn_state_projection() -> dict[str, Any]:
    health = _safe_health(mihomo_adapter_module.DEFAULT_MIHOMO_ADAPTER)
    module = next((item for item in fetch_modules() if item.get("module_name") == "vpn"), {})
    routing = _routing_snapshot_readonly()
    runtime_state = str(health.get("runtime_state") or "unknown")
    selected_server_id = routing.get("desired_fixed_server_id") or routing.get("active_auto_server_id")
    active_server_id = health.get("active_server_id")
    selected_server = _read_server_runtime_summary_readonly(selected_server_id)
    active_server = _read_server_runtime_summary_readonly(active_server_id)
    server_health = {
        "selected": selected_server,
        "active": active_server,
        "active_matches_selected": bool(active_server_id and selected_server_id and active_server_id == selected_server_id),
    }
    execution = StateExecutionDTO(
        state=_execution_state(module.get("apply_state"), error_code=module.get("error_code")),
        legacy_apply_state=module.get("apply_state"),
        error_code=module.get("error_code"),
        error_message=module.get("error_message") or health.get("message"),
        updated_at=module.get("updated_at"),
        details={
            "active_server_id": active_server_id,
            "selected_server_id": selected_server_id,
            "server_mode": routing.get("server_mode"),
            "fallback_state": {
                "auto_server_id": routing.get("active_auto_server_id"),
                "fixed_server_until": routing.get("fixed_server_until"),
            },
        },
    )
    observation = StateObservationDTO(
        state=runtime_state,
        source="mihomo_adapter",
        observed_at=module.get("updated_at"),
        evidence={"health": health, "routing": routing, "server_health": server_health},
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
        identity={"stable_key": "module:vpn", "display_name": "VPN dataplane"},
        intent=StateIntentDTO(
            state=str(module.get("desired_state") or "disabled"),
            mode=routing_mode,
            target_id=routing.get("active_auto_server_id") or routing.get("desired_fixed_server_id"),
            source="database",
            updated_at=module.get("updated_at"),
            details={
                "selected_server_id": selected_server_id,
                "server_mode": routing.get("server_mode"),
                "policy_owner": "fwrouter",
                "adapter_role": "egress_adapter",
            },
        ),
        execution=execution,
        observation=observation,
        reconcile=reconcile,
        projection=_basic_projection(execution=execution, observation=observation, reconcile=reconcile),
        effective={
            "adapter_state": runtime_state,
            "active_server_id": active_server_id,
            "selected_server_id": selected_server_id,
            "server_health": server_health,
            "fallback_state": execution.details["fallback_state"],
        },
        reason={"code": reconcile.reason_code, "source": observation.source},
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
