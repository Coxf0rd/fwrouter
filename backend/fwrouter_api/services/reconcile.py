from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
import json
from typing import Any, Literal

from pydantic import BaseModel, Field

from fwrouter_api.adapters import mihomo as mihomo_adapter_module
from fwrouter_api.adapters import xray as xray_adapter_module
from fwrouter_api.db.connection import db_session
from fwrouter_api.services.dataplane_status import (
    build_runtime_enforcement_state,
    read_live_dataplane_payload,
)
from fwrouter_api.services.modules import fetch_modules
from fwrouter_api.services.state_projection import (
    build_module_state_projection,
    build_routing_state_projection,
    build_subject_state_projection,
    build_vpn_state_projection,
    build_watchdog_state_projection,
    build_xray_state_projection,
)
from fwrouter_api.services.xray_runtime_state import _load_xray_bindings_state


ReconcileState = Literal["in_sync", "drift", "stale", "failed", "unknown"]
ACTIVE_OBSERVED_STATES = {"active", "running", "paused"}
ACTIVE_INTENT_MODES = {"enabled", "vpn", "selective", "forced_vpn"}
STALE_AFTER_SECONDS = 300


class ReconcileResult(BaseModel):
    entity_type: str
    entity_id: str
    intent_state: dict[str, Any] = Field(default_factory=dict)
    execution_state: dict[str, Any] = Field(default_factory=dict)
    observed_state: dict[str, Any] = Field(default_factory=dict)
    projection_state: dict[str, Any] = Field(default_factory=dict)
    reconcile_state: ReconcileState
    reason: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class ReconcileResponse(BaseModel):
    entities: list[ReconcileResult]
    summary: dict[str, int]


class Reconciler(ABC):
    @abstractmethod
    def check(self, entity: Any = None) -> ReconcileResult:
        raise NotImplementedError


def _json_loads(value: Any) -> Any:
    if not value:
        return None
    try:
        return json.loads(str(value))
    except json.JSONDecodeError:
        return None


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
        "details": (
            getattr(health, "details", {})
            if isinstance(getattr(health, "details", {}), dict)
            else {}
        ),
    }


def _parse_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    for parser in (datetime.fromisoformat,):
        try:
            parsed = parser(text)
            break
        except ValueError:
            parsed = None
    if parsed is None:
        try:
            parsed = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _is_stale(value: Any, *, stale_after_seconds: int = STALE_AFTER_SECONDS) -> bool:
    observed = _parse_timestamp(value)
    if observed is None:
        return False
    return datetime.now(UTC) > observed + timedelta(seconds=stale_after_seconds)


def _summarize(results: Iterable[ReconcileResult]) -> dict[str, int]:
    summary = {"healthy": 0, "drift": 0, "stale": 0, "failed": 0}
    for result in results:
        if result.reconcile_state == "in_sync":
            summary["healthy"] += 1
        elif result.reconcile_state == "drift":
            summary["drift"] += 1
        elif result.reconcile_state == "stale":
            summary["stale"] += 1
        elif result.reconcile_state == "failed":
            summary["failed"] += 1
    return summary


def _state_from_projection(
    reconcile_state: str | None,
    projection_state: str | None = None,
) -> ReconcileState:
    if reconcile_state == "in_sync":
        return "in_sync"
    if reconcile_state in {"runtime_drift", "legacy_ambiguous"}:
        return "drift"
    if reconcile_state in {"observation_stale", "intent_newer_than_runtime"}:
        return "stale"
    if projection_state == "error":
        return "failed"
    if reconcile_state in {"not_applicable"}:
        return "in_sync"
    return "unknown"


def _read_routing_state() -> dict[str, Any] | None:
    with db_session() as connection:
        row = connection.execute(
            """
            SELECT id, desired_mode, applied_mode, selective_default, server_mode,
                   desired_fixed_server_id, applied_fixed_server_id, fixed_server_until,
                   active_auto_server_id, apply_state, error_code, error_message, updated_at
            FROM routing_global_state
            WHERE id = 1
            """
        ).fetchone()
    return dict(row) if row is not None else None


def _read_active_subjects() -> list[dict[str, Any]]:
    with db_session() as connection:
        rows = connection.execute(
            """
            SELECT subject_id, subject_type, subject_role, implementation_kind, stable_key,
                   display_name, alias, desired_mode, applied_mode, apply_state,
                   runtime_state, is_active, is_deleted, last_seen_at, last_traffic_at,
                   metadata_json, updated_at
            FROM subjects
            WHERE is_deleted = 0
            ORDER BY subject_id
            """
        ).fetchall()
    return [dict(row) for row in rows]


def _read_subject_server_overrides(subject_ids: list[str]) -> dict[str, dict[str, Any]]:
    if not subject_ids:
        return {}
    placeholders = ", ".join("?" for _ in subject_ids)
    with db_session() as connection:
        rows = connection.execute(
            f"""
            SELECT subject_id, selected_server_id, selected_until, apply_state,
                   error_code, error_message, updated_at
            FROM subject_server_overrides
            WHERE subject_id IN ({placeholders})
            """,
            tuple(subject_ids),
        ).fetchall()
    return {str(row["subject_id"]): dict(row) for row in rows}


def _read_watchdog_state() -> dict[str, Any] | None:
    with db_session() as connection:
        row = connection.execute(
            """
            SELECT id, path_key, failure_candidate_json, last_processed_decision_id,
                   last_successful_failover_at, failover_path_key, previous_target_id,
                   selected_target_id, cooldown_until, updated_at
            FROM watchdog_state
            WHERE id = 1
            """
        ).fetchone()
    if row is None:
        return None
    state = dict(row)
    state["failure_candidate"] = _json_loads(state.pop("failure_candidate_json", None))
    return state


class ModuleReconciler(Reconciler):
    def __init__(
        self,
        *,
        projection_loader: Callable[[], dict[str, Any]] = build_module_state_projection,
    ) -> None:
        self._projection_loader = projection_loader

    def check(self, entity: Any = None) -> ReconcileResult:
        module = dict(entity or {})
        module_id = str(module.get("module_name") or "unknown")
        projections = {
            str(item.get("entity", {}).get("id")): item
            for item in self._projection_loader().get("items", [])
            if isinstance(item, dict)
        }
        projection = projections.get(module_id, {})
        projection_reconcile = (projection.get("reconcile") or {}).get("state")
        projection_state = (projection.get("projection") or {}).get("state")
        desired = str(module.get("desired_state") or "disabled")
        runtime = str(module.get("runtime_state") or "not_configured")
        apply_state = str(module.get("apply_state") or "clean")
        error_code = module.get("error_code")
        state = _state_from_projection(projection_reconcile, projection_state)
        reason = (projection.get("reason") or {}).get("code")
        if error_code or apply_state == "failed" or runtime == "failed":
            state = "failed"
            reason = str(error_code or "MODULE_FAILED")
        elif desired == "enabled" and runtime not in ACTIVE_OBSERVED_STATES and state == "in_sync":
            state = "drift"
            reason = "module_runtime_missing"
        return ReconcileResult(
            entity_type="module",
            entity_id=module_id,
            intent_state={"source": "database.modules", "desired_state": desired},
            execution_state={"source": "database.modules", "apply_state": apply_state},
            observed_state={"source": "runtime_probe+database.modules", "runtime_state": runtime},
            projection_state=projection,
            reconcile_state=state,
            reason=reason,
            details={"lifecycle_mode": module.get("lifecycle_mode")},
        )


class SubjectReconciler(Reconciler):
    def __init__(
        self,
        *,
        projection_loader: Callable[..., dict[str, Any]] = build_subject_state_projection,
    ) -> None:
        self._projection_loader = projection_loader

    def check(self, entity: Any = None) -> ReconcileResult:
        subject = dict(entity or {})
        subject_id = str(subject.get("subject_id") or "unknown")
        projection = (
            self._projection_loader(subject_id=subject_id, include_deleted=False).get("subject")
            or {}
        )
        projection_reconcile = (projection.get("reconcile") or {}).get("state")
        projection_state = (projection.get("projection") or {}).get("state")
        desired_mode = str(subject.get("desired_mode") or "global")
        runtime_state = str(subject.get("runtime_state") or "not_configured")
        apply_state = str(subject.get("apply_state") or "clean")
        is_active = bool(subject.get("is_active"))
        state = _state_from_projection(projection_reconcile, projection_state)
        reason = (projection.get("reason") or {}).get("code")
        if apply_state == "failed" or runtime_state == "failed":
            state = "failed"
            reason = "subject_failed"
        elif (
            is_active
            and desired_mode in ACTIVE_INTENT_MODES
            and runtime_state not in ACTIVE_OBSERVED_STATES
        ):
            state = "drift"
            reason = "runtime_missing"
        elif is_active and _is_stale(subject.get("last_seen_at") or subject.get("updated_at")):
            state = "stale"
            reason = "observation_stale"
        return ReconcileResult(
            entity_type="subject",
            entity_id=subject_id,
            intent_state={
                "source": "database.subjects",
                "desired_mode": desired_mode,
                "is_active": is_active,
            },
            execution_state={
                "source": "database.subjects",
                "apply_state": apply_state,
                "applied_mode": subject.get("applied_mode"),
            },
            observed_state={
                "source": "database.subjects+runtime_activity",
                "runtime_state": runtime_state,
                "last_seen_at": subject.get("last_seen_at"),
            },
            projection_state=projection,
            reconcile_state=state,
            reason=reason,
            details={"implementation_kind": subject.get("implementation_kind")},
        )


class XrayReconciler(Reconciler):
    def __init__(
        self,
        *,
        bindings_loader: Callable[[], dict[str, Any]] = _load_xray_bindings_state,
        projection_loader: Callable[[], dict[str, Any]] = build_xray_state_projection,
        health_loader: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self._bindings_loader = bindings_loader
        self._projection_loader = projection_loader
        self._health_loader = health_loader or (
            lambda: _safe_health(xray_adapter_module.DEFAULT_XRAY_ADAPTER)
        )

    def check(self, entity: Any = None) -> ReconcileResult:
        subjects = [
            subject
            for subject in _read_active_subjects()
            if bool(subject.get("is_active"))
            and str(subject.get("implementation_kind") or "") == "xray"
        ]
        subject_ids = [str(subject["subject_id"]) for subject in subjects]
        overrides = _read_subject_server_overrides(subject_ids)
        bindings = self._bindings_loader()
        binding_items = (
            bindings.get("bindings")
            if isinstance(bindings.get("bindings"), list)
            else []
        )
        applied_subject_ids = {
            str(binding.get("subject_id"))
            for binding in binding_items
            if isinstance(binding, dict) and str(binding.get("status") or "") == "applied"
        }
        binding_subject_ids = {
            str(binding.get("subject_id"))
            for binding in binding_items
            if isinstance(binding, dict) and binding.get("subject_id") is not None
        }
        pending_subject_ids = sorted(
            subject_id
            for subject_id, override in overrides.items()
            if str(override.get("apply_state") or "") in {"pending", "applying"}
        )
        missing_subject_ids = sorted(set(subject_ids) - applied_subject_ids)
        stale_subject_ids = sorted(binding_subject_ids - set(subject_ids))
        health = self._health_loader()
        runtime_state = str(health.get("runtime_state") or "unknown")
        projection = self._projection_loader().get("xray") or {}
        if bindings.get("error_code"):
            state: ReconcileState = "failed"
            reason = str(bindings.get("error_code"))
        elif runtime_state == "failed":
            state = "failed"
            reason = "runtime_unavailable"
        elif missing_subject_ids:
            state = "drift"
            reason = "binding_missing"
        elif stale_subject_ids:
            state = "stale"
            reason = "stale_binding"
        elif pending_subject_ids and set(pending_subject_ids).issubset(applied_subject_ids):
            state = "in_sync"
            reason = "runtime_confirmed"
        else:
            state = "in_sync"
            reason = None
        return ReconcileResult(
            entity_type="xray",
            entity_id="xray",
            intent_state={"source": "database.subjects", "active_subject_ids": subject_ids},
            execution_state={
                "source": "database.subject_server_overrides",
                "pending_subject_ids": pending_subject_ids,
            },
            observed_state={
                "source": "fwrouter-bindings.json+xray_adapter",
                "applied_subject_ids": sorted(applied_subject_ids),
                "runtime_state": runtime_state,
            },
            projection_state=projection,
            reconcile_state=state,
            reason=reason,
            details={
                "missing_subject_ids": missing_subject_ids,
                "stale_subject_ids": stale_subject_ids,
            },
        )


class RoutingReconciler(Reconciler):
    def __init__(
        self,
        *,
        runtime_loader: Callable[[], dict[str, Any]] = build_runtime_enforcement_state,
        live_payload_loader: Callable[[], dict[str, Any] | None] = read_live_dataplane_payload,
        projection_loader: Callable[[], dict[str, Any]] = build_routing_state_projection,
    ) -> None:
        self._runtime_loader = runtime_loader
        self._live_payload_loader = live_payload_loader
        self._projection_loader = projection_loader

    def check(self, entity: Any = None) -> ReconcileResult:
        routing = _read_routing_state() or {}
        live_payload = self._live_payload_loader()
        runtime = self._runtime_loader()
        projection = self._projection_loader().get("routing") or {}
        desired_mode = str(routing.get("desired_mode") or "direct")
        apply_state = str(routing.get("apply_state") or "unknown")
        if (
            routing.get("error_code")
            or apply_state == "failed"
            or (isinstance(live_payload, dict) and live_payload.get("ok") is False)
        ):
            state: ReconcileState = "failed"
            reason = str(
                routing.get("error_code")
                or (live_payload or {}).get("error_code")
                or "routing_probe_failed"
            )
        elif not routing:
            state = "unknown"
            reason = "routing_state_missing"
        elif not bool(runtime.get("traffic_enforcement_guaranteed")):
            state = "drift"
            reason = "dataplane_not_enforced"
        elif not bool(runtime.get("active_mode_matches_intent")):
            state = "drift"
            reason = "mode_mismatch"
        elif apply_state in {"pending", "applying"}:
            state = "stale"
            reason = "execution_pending"
        else:
            state = "in_sync"
            reason = None
        return ReconcileResult(
            entity_type="routing",
            entity_id="global",
            intent_state={
                "source": "database.routing_global_state",
                "desired_mode": desired_mode,
                "selective_default": routing.get("selective_default"),
            },
            execution_state={
                "source": "database.routing_global_state",
                "apply_state": apply_state,
                "applied_mode": routing.get("applied_mode"),
            },
            observed_state={
                "source": "dataplane observation",
                "live_global_mode": runtime.get("live_global_mode"),
                "traffic_enforcement_guaranteed": runtime.get(
                    "traffic_enforcement_guaranteed"
                ),
            },
            projection_state=projection,
            reconcile_state=state,
            reason=reason,
            details={"enforcement_level": runtime.get("enforcement_level")},
        )


class VpnReconciler(Reconciler):
    def __init__(
        self,
        *,
        health_loader: Callable[[], dict[str, Any]] | None = None,
        projection_loader: Callable[[], dict[str, Any]] = build_vpn_state_projection,
    ) -> None:
        self._health_loader = health_loader or (
            lambda: _safe_health(mihomo_adapter_module.DEFAULT_MIHOMO_ADAPTER)
        )
        self._projection_loader = projection_loader

    def check(self, entity: Any = None) -> ReconcileResult:
        module = next((item for item in fetch_modules() if item.get("module_name") == "vpn"), {})
        routing = _read_routing_state() or {}
        health = self._health_loader()
        projection = self._projection_loader().get("vpn") or {}
        desired_mode = str(routing.get("desired_mode") or "direct")
        runtime_state = str(health.get("runtime_state") or "unknown")
        selected_server_id = routing.get("desired_fixed_server_id") or routing.get(
            "active_auto_server_id"
        )
        active_server_id = health.get("active_server_id")
        if (
            module.get("error_code")
            or module.get("apply_state") == "failed"
            or runtime_state == "failed"
        ):
            state: ReconcileState = "failed"
            reason = str(
                module.get("error_code")
                or health.get("error_code")
                or "vpn_runtime_failed"
            )
        elif desired_mode in {"vpn", "selective"} and runtime_state != "running":
            state = "drift"
            reason = "adapter_unavailable"
        elif selected_server_id and active_server_id and selected_server_id != active_server_id:
            state = "drift"
            reason = "selected_server_mismatch"
        else:
            state = "in_sync"
            reason = None
        return ReconcileResult(
            entity_type="vpn",
            entity_id="vpn",
            intent_state={
                "source": "database.routing_global_state",
                "desired_mode": desired_mode,
                "selected_server_id": selected_server_id,
            },
            execution_state={
                "source": "database.modules",
                "apply_state": module.get("apply_state"),
                "module_runtime_state": module.get("runtime_state"),
            },
            observed_state={
                "source": "vpn adapter health",
                "runtime_state": runtime_state,
                "active_server_id": active_server_id,
            },
            projection_state=projection,
            reconcile_state=state,
            reason=reason,
            details={"server_mode": routing.get("server_mode")},
        )


class WatchdogReconciler(Reconciler):
    def __init__(
        self,
        *,
        projection_loader: Callable[[], dict[str, Any]] = build_watchdog_state_projection,
    ) -> None:
        self._projection_loader = projection_loader

    def check(self, entity: Any = None) -> ReconcileResult:
        module = next(
            (item for item in fetch_modules() if item.get("module_name") == "watchdog"),
            {},
        )
        runtime = _read_watchdog_state()
        projection = self._projection_loader().get("watchdog") or {}
        desired_state = str(module.get("desired_state") or "disabled")
        apply_state = str(module.get("apply_state") or "unknown")
        module_runtime_state = str(module.get("runtime_state") or "unknown")
        if module.get("error_code") or apply_state == "failed" or module_runtime_state == "failed":
            state: ReconcileState = "failed"
            reason = str(module.get("error_code") or "watchdog_failed")
        elif desired_state == "enabled" and runtime is None:
            state = "stale"
            reason = "last_successful_check_missing"
        elif desired_state == "enabled" and module_runtime_state not in ACTIVE_OBSERVED_STATES:
            state = "drift"
            reason = "module_not_running"
        elif runtime is not None and _is_stale(runtime.get("updated_at")):
            state = "stale"
            reason = "last_check_stale"
        else:
            state = "in_sync"
            reason = None
        return ReconcileResult(
            entity_type="watchdog",
            entity_id="watchdog",
            intent_state={"source": "database.modules", "desired_state": desired_state},
            execution_state={"source": "database.modules", "apply_state": apply_state},
            observed_state={
                "source": "database.watchdog_state",
                "module_runtime_state": module_runtime_state,
                "last_successful_check": (runtime or {}).get("updated_at"),
            },
            projection_state=projection,
            reconcile_state=state,
            reason=reason,
            details={"decision_state": runtime or {}},
        )


def build_reconcile_response() -> ReconcileResponse:
    results: list[ReconcileResult] = []
    module_reconciler = ModuleReconciler()
    subject_reconciler = SubjectReconciler()
    for module in fetch_modules():
        results.append(module_reconciler.check(module))
    for subject in _read_active_subjects():
        results.append(subject_reconciler.check(subject))
    for reconciler in (
        XrayReconciler(),
        RoutingReconciler(),
        VpnReconciler(),
        WatchdogReconciler(),
    ):
        results.append(reconciler.check())
    return ReconcileResponse(entities=results, summary=_summarize(results))
