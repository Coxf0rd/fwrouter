from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

from fwrouter_api.db.connection import db_session
from fwrouter_api.services.logs import write_operational_log
from fwrouter_api.services.events import safe_human_label
from fwrouter_api.services.management_attribution import (
    build_incomplete_attribution_error,
    build_management_attribution,
)
from fwrouter_api.services.runtime_adapters import (
    RUNTIME_CAPABILITY_APPLY_SERVER,
    RUNTIME_CAPABILITY_APPLY_SELECTOR,
    RUNTIME_CAPABILITY_HEALTH,
    RUNTIME_CAPABILITY_LIST_SERVERS,
    RUNTIME_ROLE_VPN_DATAPLANE,
    active_runtime_adapter,
    runtime_adapter_operations,
)
from fwrouter_api.services.server_ping import check_server_delay, check_server_delays
from fwrouter_api.services.auto_eligibility import auto_eligible_sql, is_auto_eligible
from fwrouter_api.adapters.xray_common import xray_writer_guard
from fwrouter_api.adapters.xray_common import xray_writer_guard_is_held
from fwrouter_api.services.vpn_auto_selection_state import (
    commit_active_selection,
    read_selection_fence,
    selection_pool_signature,
)


DEFAULT_ON_DEMAND_LIMIT = 10
DEFAULT_ON_DEMAND_TIMEOUT_MS = 10000
MIN_VPN_AUTO_PRIORITY = -1
MAX_VPN_AUTO_PRIORITY = 5
TRAFFIC_COLLECT_TIMER_PATH = Path("/etc/systemd/system/fwrouter-traffic-collect.timer")
TRAFFIC_COLLECT_SERVICE_PATH = Path("/etc/systemd/system/fwrouter-traffic-collect.service")
TRAFFIC_COLLECT_SCRIPT_PATH = Path("/usr/local/libexec/fwrouter/traffic-collect-api.sh")


def _server_event_label(server_id: str | None) -> str | None:
    if not server_id:
        return None
    with db_session() as connection:
        row = connection.execute("SELECT server_name FROM servers WHERE server_id = ?", (server_id,)).fetchone()
    return safe_human_label(row["server_name"], entity_id=server_id) if row else None


def _selector_reason_code(reason: Any, *, origin: Any = "unknown") -> str:
    value = str(reason or "")
    normalized_origin = _safe_selection_origin(origin)
    if normalized_origin == "api":
        return "api_controlled_switch"
    if normalized_origin == "watchdog" and value.startswith("watchdog_failover:"):
        return "watchdog_failover"
    if normalized_origin == "watchdog" and value.startswith("watchdog_initial_select:"):
        return "watchdog_initial_select"
    if normalized_origin == "watchdog":
        return "automatic_selection"
    return value if value in {
        "manual", "scheduler_watchdog_check", "subscription_refresh_auto_select",
        "vpn_auto_membership_changed", "server_preferences_vpn_auto",
        "api_controlled_switch",
    } else "automatic_selection"


def _runtime_target_identity(
    runtime_target: Any, candidates: list[dict[str, Any]]
) -> tuple[str | None, str | None, str]:
    """Resolve a runtime selector value only when the candidate mapping is unique."""
    target = str(runtime_target or "").strip()
    if not target:
        return None, None, "missing"
    matches = [
        item for item in candidates
        if str(item.get("runtime_target") or item.get("server_id") or "").strip() == target
    ]
    if len(matches) != 1:
        return None, None, "unconfirmed" if matches else "unmapped"
    item = matches[0]
    logical_id = str(item["server_id"])
    return logical_id, safe_human_label(item.get("server_name"), entity_id=logical_id), "confirmed"


def _load_runtime_target_inventory() -> list[dict[str, Any]]:
    with db_session() as connection:
        rows = connection.execute(
            """
            SELECT s.server_id, s.server_name,
                CASE WHEN c.server_id IS NOT NULL THEN s.server_name
                     ELSE COALESCE(json_extract(s.raw_json, '$._fwrouter_runtime_name'),
                                   json_extract(s.raw_json, '$.name'), s.server_name)
                END AS runtime_target
            FROM servers s
            LEFT JOIN server_custom_https_proxy c ON c.server_id = s.server_id
            WHERE COALESCE(s.inventory_state, 'active') = 'active'
            """
        ).fetchall()
    return [dict(row) for row in rows]


def _candidate_fence(candidates: list[dict[str, Any]]) -> str:
    rows = []
    for item in candidates:
        rows.append((
            str(item.get("server_id") or ""),
            str(item.get("runtime_target") or item.get("server_id") or ""),
            int(item.get("vpn_auto_priority") or 0),
            bool(item.get("vpn_auto")),
            str(item.get("inventory_state") or ""),
            str(item.get("source_ref") or ""),
            str(item.get("execution_capability") or ""),
        ))
    payload = json.dumps(sorted(rows), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _runtime_generation_identity(
    health: Any,
    operations: Any | None = None,
    *,
    require_incarnation: bool = False,
    expected_vpn_auto_target: str | None = None,
) -> str:
    details = health.details if isinstance(getattr(health, "details", None), dict) else {}
    selectors = details.get("selectors") if isinstance(details.get("selectors"), dict) else {}
    version = details.get("version") if isinstance(details.get("version"), dict) else {}
    config_hash = None
    try:
        from fwrouter_api.services.mihomo_config_paths import _resolved_base_config_path

        path = _resolved_base_config_path()
        if os.path.isfile(path):
            digest = hashlib.sha256()
            with open(path, "rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            config_hash = digest.hexdigest()
    except OSError:
        config_hash = None
    incarnation = None
    identity_reader = getattr(operations, "runtime_incarnation", None)
    if callable(identity_reader):
        try:
            incarnation = identity_reader()
        except Exception as exc:
            raise RuntimeError("Unable to read vpn runtime incarnation.") from exc
    if require_incarnation and not incarnation:
        from fwrouter_api.core.config import get_settings

        if get_settings().environment.strip().lower() != "test":
            raise RuntimeError("VPN runtime incarnation is unavailable; refusing stale-plan apply.")
    identity = {
        "config_sha256": config_hash,
        "runtime_version": version.get("version"),
        "runtime_commit": version.get("commit"),
        "runtime_incarnation": incarnation,
        "vpn_auto_targets": sorted(str(value) for value in selectors.get("vpn_auto_targets", []) if value),
        "vpn_auto_now": expected_vpn_auto_target if expected_vpn_auto_target is not None
        else str(selectors.get("vpn_auto_now") or "").strip() or None,
    }
    return hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _selection_fence_error(
    *, expected_fence: dict[str, Any], candidate_fingerprint: str,
    pool_signature: str, runtime_generation: str, runtime_inventory_targets: set[str], selected_server_id: str,
    expected_runtime_target: str | None,
) -> str | None:
    try:
        with db_session() as connection:
            current = read_selection_fence(connection)
        if current != expected_fence:
            return "VPN_AUTO_SELECTION_STALE_STATE"
        with db_session() as connection:
            if selection_pool_signature(connection) != pool_signature:
                return "VPN_AUTO_SELECTION_CANDIDATES_CHANGED"
    except (ValueError, KeyError, TypeError):
        return "VPN_AUTO_SELECTION_FENCE_INVALID"
    fresh_candidates = [
        item for item in _load_selector_candidates()
        if str(item.get("runtime_target") or item.get("server_id") or "") in runtime_inventory_targets
    ]
    fresh = {str(item.get("server_id") or ""): item for item in fresh_candidates}
    candidate = fresh.get(selected_server_id)
    if candidate is None or not is_auto_eligible(
        vpn_auto=candidate.get("vpn_auto"),
        vpn_auto_priority=candidate.get("vpn_auto_priority"),
        inventory_state=candidate.get("inventory_state"),
        manually_deleted_at=candidate.get("manually_deleted_at"),
        provider_managed_legacy=candidate.get("provider_managed_legacy", False),
        provider_internal_member=candidate.get("provider_internal_member", False),
    ):
        return "VPN_AUTO_SELECTION_CANDIDATE_NO_LONGER_ELIGIBLE"
    if _candidate_fence(fresh_candidates) != candidate_fingerprint:
        return "VPN_AUTO_SELECTION_CANDIDATES_CHANGED"
    runtime_adapter, runtime_operations = _active_selector_runtime()
    health, _error = _runtime_health_or_error(runtime_adapter, runtime_operations)
    try:
        fresh_generation = _runtime_generation_identity(
            health,
            runtime_operations,
            require_incarnation=str(runtime_adapter.get("adapter_id") or "") == "mihomo",
        ) if health is not None else None
    except RuntimeError:
        fresh_generation = None
    if health is None or fresh_generation != runtime_generation:
        return "VPN_AUTO_SELECTION_RUNTIME_GENERATION_CHANGED"
    details = health.details if isinstance(getattr(health, "details", None), dict) else {}
    selectors = details.get("selectors") if isinstance(details.get("selectors"), dict) else {}
    current_runtime_target = selectors.get("vpn_auto_now")
    if "vpn_auto_now" not in selectors:
        current_runtime_target = getattr(health, "active_server_id", None)
    current_runtime_target = str(current_runtime_target or "").strip() or None
    if current_runtime_target != expected_runtime_target:
        return "VPN_AUTO_SELECTION_RUNTIME_TARGET_CHANGED"
    return None


def restore_auto_selection_snapshot(
    *, before: dict[str, Any], after: dict[str, Any], operation_id: str | None = None,
) -> dict[str, Any]:
    """Core-owned conditional Auto restore after the runtime generation is restored."""
    from fwrouter_api.adapters.xray_common import xray_writer_guard
    before_routing = before.get("routing") if isinstance(before, dict) else None
    after_routing = after.get("routing") if isinstance(after, dict) else None
    if not isinstance(before_routing, dict) or not isinstance(after_routing, dict):
        return {"ok": False, "error_code": "VPN_AUTO_ROLLBACK_CONTEXT_MISSING"}
    target_id = str(before_routing.get("active_auto_server_id") or "").strip()
    expected_id = str(after_routing.get("active_auto_server_id") or "").strip() or None
    expected_revision = after.get("selection_revision")
    provenance_row = after.get("provenance") if isinstance(after.get("provenance"), dict) else {}
    try:
        expected_provenance = json.loads(provenance_row.get("value_json") or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {"ok": False, "error_code": "VPN_AUTO_ROLLBACK_PROVENANCE_INVALID"}
    expected_decision = str(expected_provenance.get("decision_id") or "").strip() or None if isinstance(expected_provenance, dict) else None
    if type(expected_revision) is not int or not target_id:
        # A generation restore can legitimately have no logical Auto target
        # before or after. If Core state is still exactly the checkpoint's
        # nullable state, the restored last-good generation needs no selector
        # PUT and no new revision; this confirms only the unchanged nullable
        # state, never an effective VPN server selection.
        if not target_id and not expected_id and type(expected_revision) is int:
            try:
                with xray_writer_guard(timeout_seconds=5.0):
                    with db_session() as connection:
                        current = read_selection_fence(connection)
                    if (
                        current.get("revision") == expected_revision
                        and current.get("active_server_id") is None
                        and current.get("decision_id") == expected_decision
                    ):
                        return {"ok": True, "changed": False, "selection_revision": expected_revision,
                                "selection_confirmed": False, "skip_reason": "nullable_selection_unchanged"}
            except TimeoutError as exc:
                return {"ok": False, "error_code": "VPN_AUTO_SELECTION_BUSY", "error_message": str(exc), "retryable": True}
        return {"ok": False, "error_code": "VPN_AUTO_ROLLBACK_TARGET_UNAVAILABLE"}
    runtime_adapter, runtime_operations = _active_selector_runtime()
    if runtime_operations is None or RUNTIME_CAPABILITY_APPLY_SERVER not in _runtime_capabilities(runtime_adapter):
        return {"ok": False, "error_code": "VPN_AUTO_RUNTIME_APPLY_UNAVAILABLE"}
    try:
        health, health_error = _runtime_health_or_error(runtime_adapter, runtime_operations)
        if health is None:
            return {"ok": False, "error_code": str((health_error or {}).get("error_code") or "VPN_RUNTIME_UNREACHABLE")}
        runtime_generation = _runtime_generation_identity(
            health, runtime_operations,
            require_incarnation=str(runtime_adapter.get("adapter_id") or "") == "mihomo",
        )
        candidates = _load_selector_candidates()
        candidate_fingerprint = _candidate_fence(candidates)
        with db_session() as connection:
            pool_signature = selection_pool_signature(connection)
        candidate = next((item for item in candidates if str(item.get("server_id") or "") == target_id), None)
        if candidate is None or not is_auto_eligible(
            vpn_auto=candidate.get("vpn_auto"),
            vpn_auto_priority=candidate.get("vpn_auto_priority"),
            inventory_state=candidate.get("inventory_state"),
            manually_deleted_at=candidate.get("manually_deleted_at"),
            provider_managed_legacy=candidate.get("provider_managed_legacy", False),
            provider_internal_member=candidate.get("provider_internal_member", False),
        ):
            return {"ok": False, "error_code": "VPN_AUTO_ROLLBACK_TARGET_INELIGIBLE"}
        target = str(candidate.get("runtime_target") or target_id)
        with xray_writer_guard(timeout_seconds=5.0):
            with db_session() as connection:
                current = read_selection_fence(connection)
            if (
                current.get("revision") != expected_revision
                or current.get("active_server_id") != expected_id
                or current.get("decision_id") != expected_decision
                or _candidate_fence(_load_selector_candidates()) != candidate_fingerprint
            ):
                return {"ok": False, "error_code": "VPN_AUTO_ROLLBACK_STALE", "retryable": True}
            with db_session() as connection:
                if selection_pool_signature(connection) != pool_signature:
                    return {"ok": False, "error_code": "VPN_AUTO_ROLLBACK_STALE", "retryable": True}
            current_health, current_error = _runtime_health_or_error(runtime_adapter, runtime_operations)
            if current_health is None or _runtime_generation_identity(
                current_health, runtime_operations,
                require_incarnation=str(runtime_adapter.get("adapter_id") or "") == "mihomo",
            ) != runtime_generation:
                return {"ok": False, "error_code": "VPN_AUTO_ROLLBACK_RUNTIME_CHANGED", "retryable": True}
            details = current_health.details if isinstance(getattr(current_health, "details", None), dict) else {}
            selectors = details.get("selectors") if isinstance(details.get("selectors"), dict) else {}
            observed_before = str(selectors.get("vpn_auto_now") or "").strip() or None
            if target_id == expected_id:
                if observed_before != target:
                    return {"ok": False, "error_code": "VPN_AUTO_ROLLBACK_READBACK_UNCONFIRMED",
                            "runtime_observed": observed_before}
                return {"ok": True, "changed": False, "selection_revision": expected_revision,
                        "runtime_target": target}
            applied = runtime_operations.apply_server(target)
            details = getattr(applied, "details", None)
            observed = str((details or {}).get("selector_after") or "").strip() or None if isinstance(details, dict) else None
            if not bool(getattr(applied, "ok", False)) or observed != target:
                return {"ok": False, "error_code": "VPN_AUTO_ROLLBACK_READBACK_UNCONFIRMED", "runtime_observed": observed}
            decision_id = str(uuid4())
            restored_provenance = {
                "decision_id": decision_id,
                "operation_id": str(operation_id or expected_provenance.get("operation_id") or uuid4()),
                "selected_server_id": target_id,
                "selected_server_label": safe_human_label(candidate.get("server_name"), entity_id=target_id),
                "reason_code": "generation_rollback",
                "origin": "subscription",
                "actor_attribution": "core_reconcile",
                "selected_at": datetime.now(timezone.utc).isoformat(),
            }
            with db_session() as connection:
                next_revision = commit_active_selection(
                    connection,
                    expected_revision=expected_revision,
                    expected_active_server_id=expected_id,
                    expected_provenance_decision_id=expected_decision,
                    server_id=target_id,
                    provenance=restored_provenance,
                )
            if next_revision is None:
                return {"ok": False, "error_code": "VPN_AUTO_ROLLBACK_PERSISTENCE_CAS_FAILED", "retryable": True}
            return {"ok": True, "changed": True, "selection_revision": next_revision,
                    "operation_id": restored_provenance["operation_id"], "runtime_target": target}
    except TimeoutError as exc:
        return {"ok": False, "error_code": "VPN_AUTO_SELECTION_BUSY", "error_message": str(exc), "retryable": True}


def _safe_selection_origin(origin: Any) -> str:
    value = str(origin or "").strip().lower()
    return value if value in {"api", "ui", "watchdog", "subscription", "server_preferences"} else "unknown"


def _unconfirmed_selector_outcome(*, reason: Any, origin: Any, apply: bool) -> dict[str, Any]:
    safe_origin = _safe_selection_origin(origin)
    return {
        "selection_outcome": "failed",
        "auto_transition": {
            "active_before_id": None,
            "active_before_name": None,
            "active_before_runtime_target": None,
            "selected_server_id": None,
            "selected_server_name": None,
            "selected_runtime_target": None,
            "active_after_id": None,
            "active_after_name": None,
            "active_after_runtime_target": None,
            "outcome": "failed",
            "changed": None,
            "reason_code": _selector_reason_code(reason, origin=safe_origin),
            "origin": safe_origin,
            "actor_attribution": "caller_supplied_unverified",
            "correlation_id": None,
            "selector_readback": "unconfirmed",
        },
        "effective_route": {
            "server_mode": None,
            "fixed_server_id": None,
            "runtime_target_before": None,
            "runtime_target_after": None,
            "global_selector_before": None,
            "changed": False if not apply else None,
            "traffic_impact_confirmed": False,
        },
        "post_check": {
            "enabled": bool(apply),
            "status": "not_run",
            "result": None,
            "failed_no_rollback": False,
        },
    }


def _deferred_selector_outcome(*, reason: Any, origin: Any, apply: bool, **details: Any) -> dict[str, Any]:
    base = _unconfirmed_selector_outcome(reason=reason, origin=origin, apply=apply)
    transition = dict(base.get("auto_transition") or {})
    transition.update({"outcome": "deferred", "changed": False})
    return {
        **base,
        **details,
        "selection_outcome": "deferred",
        "auto_transition": transition,
    }


def _watchdog_enabled() -> bool:
    with db_session() as connection:
        row = connection.execute(
            """
            SELECT desired_state
            FROM modules
            WHERE module_name = 'watchdog'
            """
        ).fetchone()
    return row is not None and str(row["desired_state"] or "").strip().lower() == "enabled"


def _traffic_collector_installed() -> bool:
    return (
        TRAFFIC_COLLECT_TIMER_PATH.exists()
        and TRAFFIC_COLLECT_SERVICE_PATH.exists()
        and TRAFFIC_COLLECT_SCRIPT_PATH.exists()
    )


def _auto_selectable_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        candidate
        for candidate in candidates
        if is_auto_eligible(
            vpn_auto=candidate.get("vpn_auto"),
            vpn_auto_priority=candidate.get("vpn_auto_priority"),
            inventory_state=candidate.get("inventory_state"),
            manually_deleted_at=candidate.get("manually_deleted_at"),
            provider_managed_legacy=candidate.get("provider_managed_legacy", False),
            provider_internal_member=candidate.get("provider_internal_member", False),
        )
    ]


def _runtime_capabilities(adapter: dict[str, Any]) -> set[str]:
    return {str(item) for item in adapter.get("capabilities") or []}


def _active_selector_runtime() -> tuple[dict[str, Any], Any | None]:
    adapter = active_runtime_adapter(RUNTIME_ROLE_VPN_DATAPLANE)
    return adapter, runtime_adapter_operations(adapter)


def _runtime_health_or_error(
    adapter: dict[str, Any],
    operations: Any | None,
) -> tuple[Any | None, dict[str, str] | None]:
    adapter_id = str(adapter.get("adapter_id") or "unknown")
    if operations is None or RUNTIME_CAPABILITY_HEALTH not in _runtime_capabilities(adapter):
        return None, {
            "error_code": "VPN_RUNTIME_HEALTH_UNAVAILABLE",
            "error_message": f"Active VPN runtime adapter does not expose health: {adapter_id}.",
        }
    try:
        return operations.health(), None
    except Exception as exc:
        return None, {
            "error_code": "VPN_RUNTIME_CONTROLLER_UNREACHABLE",
            "error_message": str(exc),
        }


def get_vpn_auto_state(*, read_only: bool = False) -> dict[str, Any]:
    from fwrouter_api.services.servers import ensure_routing_global_state, get_routing_global_state
    from fwrouter_api.services.traffic import get_traffic_accounting_state

    routing = dict(
        (
            get_routing_global_state(expire_ttl=False)
            if read_only
            else ensure_routing_global_state()
        )
        or {}
    )
    candidates = _load_selector_candidates()
    auto_selectable_candidates = _auto_selectable_candidates(candidates)
    enabled_candidate_ids = [str(candidate["server_id"]) for candidate in candidates]
    enabled_candidate_names = [str(candidate["server_name"]) for candidate in candidates]
    enabled_candidate_target_names = [
        str(candidate.get("runtime_target") or candidate.get("server_name") or candidate.get("server_id") or "")
        for candidate in candidates
        if str(candidate.get("runtime_target") or candidate.get("server_name") or candidate.get("server_id") or "").strip()
    ]
    auto_selectable_candidate_ids = [
        str(candidate["server_id"]) for candidate in auto_selectable_candidates
    ]
    auto_selectable_candidate_names = [
        str(candidate["server_name"]) for candidate in auto_selectable_candidates
    ]
    auto_selectable_candidate_target_names = [
        str(candidate.get("runtime_target") or candidate.get("server_name") or candidate.get("server_id") or "")
        for candidate in auto_selectable_candidates
        if str(candidate.get("runtime_target") or candidate.get("server_name") or candidate.get("server_id") or "").strip()
    ]

    runtime_adapter, runtime_operations = _active_selector_runtime()
    runtime_capabilities = _runtime_capabilities(runtime_adapter)
    health, health_error = _runtime_health_or_error(runtime_adapter, runtime_operations)
    runtime_state = "failed" if health is None else getattr(health.runtime_state, "value", str(health.runtime_state))
    details = health.details if health is not None and isinstance(health.details, dict) else {}
    selectors = details.get("selectors") if isinstance(details.get("selectors"), dict) else {}
    runtime_vpn_auto_targets = [
        str(target)
        for target in (selectors.get("vpn_auto_targets") or [])
        if str(target or "").strip()
    ]
    runtime_vpn_global_targets = [
        str(target)
        for target in (selectors.get("vpn_global_targets") or [])
        if str(target or "").strip()
    ]
    runtime_vpn_auto_server_targets = [
        target for target in runtime_vpn_auto_targets if target != "DIRECT"
    ]
    fallback_active_server_id = str(getattr(health, "active_server_id", "") or "").strip() if health is not None else ""
    if not runtime_vpn_auto_server_targets and not runtime_vpn_auto_targets and fallback_active_server_id:
        runtime_vpn_auto_server_targets = [fallback_active_server_id]
        runtime_vpn_auto_targets = [fallback_active_server_id]

    active_auto_server_id = str(routing.get("active_auto_server_id") or "").strip() or None
    active_auto_candidate = next(
        (
            candidate
            for candidate in auto_selectable_candidates
            if str(candidate.get("server_id") or "") == active_auto_server_id
        ),
        None,
    )
    # Keep target structure separate from health. A manual Ping can replace the
    # current latency evidence with an unknown/manual lane without removing the
    # selected logical server from the configured vpn-auto group.
    active_auto_target_valid = bool(
        active_auto_server_id
        and active_auto_server_id in auto_selectable_candidate_ids
        and (
            active_auto_server_id in runtime_vpn_auto_server_targets
            or any(
                candidate_id == active_auto_server_id and candidate_name in runtime_vpn_auto_server_targets
                for candidate_id, candidate_name in zip(
                    auto_selectable_candidate_ids,
                    auto_selectable_candidate_target_names,
                )
            )
        )
    )
    active_auto_server_valid = bool(
        active_auto_target_valid and _candidate_has_auto_health(active_auto_candidate)
    )
    config_consistent = set(auto_selectable_candidate_target_names).issubset(
        set(runtime_vpn_auto_server_targets)
    )
    traffic_state = get_traffic_accounting_state()
    traffic_signal_fresh = bool(
        traffic_state.get("safe_for_watchdog_auto")
        or traffic_state.get("signal_authoritative")
        or traffic_state.get("signal_fresh")
    )

    problem_code: str | None = None
    recommended_action: str | None = None
    if runtime_state != "running":
        problem_code = (
            "mihomo_controller_unreachable"
            if runtime_adapter.get("adapter_id") == "mihomo"
            else "vpn_runtime_controller_unreachable"
        )
        recommended_action = (
            "restore_mihomo_runtime"
            if runtime_adapter.get("adapter_id") == "mihomo"
            else "restore_vpn_runtime"
        )
    elif RUNTIME_CAPABILITY_LIST_SERVERS not in runtime_capabilities:
        problem_code = "vpn_runtime_inventory_unavailable"
        recommended_action = "configure_vpn_runtime_selector_adapter"
    elif not enabled_candidate_ids:
        problem_code = "vpn_auto_no_candidates"
        recommended_action = "assign_vpn_auto_candidates"
    elif not auto_selectable_candidate_ids:
        problem_code = "vpn_auto_no_auto_selectable_candidates"
        recommended_action = "set_nonnegative_priority_for_vpn_auto_candidate"
    elif not config_consistent:
        problem_code = (
            "vpn_auto_candidates_not_in_mihomo_config"
            if runtime_adapter.get("adapter_id") == "mihomo"
            else "vpn_auto_candidates_not_in_runtime_inventory"
        )
        recommended_action = "rebuild_config_and_run_selector"
    elif active_auto_server_id and not active_auto_server_valid:
        problem_code = "active_auto_server_invalid"
        recommended_action = "run_selector_reselect"
    elif routing.get("server_mode") == "auto" and active_auto_server_id is None:
        problem_code = "needs_initial_auto_selection"
        recommended_action = "run_selector_initial_select"
    elif not traffic_signal_fresh and not _traffic_collector_installed():
        problem_code = "traffic_collector_timer_missing"
        recommended_action = "install_and_enable_traffic_collector_timer"
    elif not traffic_signal_fresh:
        problem_code = "traffic_signal_unavailable"
        recommended_action = "collect_traffic_and_wait_for_fresh_signal"

    return {
        "enabled_candidates_count": len(enabled_candidate_ids),
        "enabled_candidate_ids": enabled_candidate_ids,
        "enabled_candidate_names": enabled_candidate_names,
        "enabled_candidate_target_names": enabled_candidate_target_names,
        "auto_selectable_candidates_count": len(auto_selectable_candidate_ids),
        "auto_selectable_candidate_ids": auto_selectable_candidate_ids,
        "auto_selectable_candidate_names": auto_selectable_candidate_names,
        "auto_selectable_candidate_target_names": auto_selectable_candidate_target_names,
        "candidate_scores": _candidate_scores(auto_selectable_candidates),
        "runtime_adapter": runtime_adapter,
        "runtime_adapter_id": runtime_adapter.get("adapter_id"),
        "runtime_vpn_auto_targets_count": len(runtime_vpn_auto_targets),
        "runtime_vpn_auto_targets": runtime_vpn_auto_targets,
        "runtime_vpn_global_targets_count": len(runtime_vpn_global_targets),
        "runtime_vpn_global_targets": runtime_vpn_global_targets,
        "mihomo_vpn_auto_targets_count": len(runtime_vpn_auto_targets),
        "mihomo_vpn_auto_targets": runtime_vpn_auto_targets,
        "mihomo_vpn_global_targets_count": len(runtime_vpn_global_targets),
        "mihomo_vpn_global_targets": runtime_vpn_global_targets,
        "active_auto_server_id": active_auto_server_id,
        "active_auto_target_valid": active_auto_target_valid,
        "active_auto_server_valid": active_auto_server_valid,
        "server_mode": str(routing.get("server_mode") or "auto"),
        "global_mode": str(routing.get("desired_mode") or routing.get("applied_mode") or "direct"),
        "watchdog_enabled": _watchdog_enabled(),
        "traffic_signal_fresh": traffic_signal_fresh,
        "traffic_last_collected_at": traffic_state.get("last_collected_at"),
        "traffic_collector_installed": _traffic_collector_installed(),
        "config_consistent": config_consistent,
        "problem_code": problem_code,
        "recommended_action": recommended_action,
        "runtime_state": runtime_state,
        "runtime_error": health_error,
        "mihomo_runtime_state": runtime_state,
        "mihomo_error": health_error,
        "selector_runtime": selectors,
    }


def restore_mihomo_selector_state(
    *,
    routing: dict[str, Any] | None = None,
    requested_by: str = "runtime_restore",
) -> dict[str, Any]:
    """Restore live Mihomo selectors from persisted routing state.

    Restores both:
    - concrete `vpn-auto` selection from `active_auto_server_id` when available;
    - top-level `vpn-global` selector target based on `server_mode`.
    """

    from fwrouter_api.services.servers import ensure_routing_global_state

    resolved_routing = dict(routing or ensure_routing_global_state() or {})
    server_mode = str(resolved_routing.get("server_mode") or "auto").strip().lower()
    fixed_server_id = str(
        resolved_routing.get("applied_fixed_server_id")
        or resolved_routing.get("desired_fixed_server_id")
        or ""
    ).strip()
    active_auto_server_id = str(resolved_routing.get("active_auto_server_id") or "").strip()

    runtime_adapter, runtime_operations = _active_selector_runtime()
    health, health_error = _runtime_health_or_error(runtime_adapter, runtime_operations)
    runtime_state = (
        "failed"
        if health is None
        else getattr(health.runtime_state, "value", str(health.runtime_state))
    )
    result: dict[str, Any] = {
        "ok": False,
        "requested_by": requested_by,
        "runtime_adapter": runtime_adapter,
        "runtime_adapter_id": runtime_adapter.get("adapter_id"),
        "runtime_state": runtime_state,
        "routing": resolved_routing,
        "server_mode": server_mode,
        "active_auto_server_id": active_auto_server_id or None,
        "requested_vpn_auto_target": active_auto_server_id or None,
        "requested_vpn_global_target": fixed_server_id if server_mode == "fixed" and fixed_server_id else "vpn-auto",
        "vpn_auto_restore": None,
        "vpn_global_restore": None,
        "skipped": False,
        "skip_reason": None,
    }

    if health is None or runtime_state != "running":
        result["skipped"] = True
        result["skip_reason"] = "vpn_runtime_controller_unreachable"
        result["runtime_error"] = health_error
        result["mihomo_runtime_state"] = runtime_state
        return result
    if (
        runtime_operations is None
        or RUNTIME_CAPABILITY_LIST_SERVERS not in _runtime_capabilities(runtime_adapter)
        or RUNTIME_CAPABILITY_APPLY_SELECTOR not in _runtime_capabilities(runtime_adapter)
    ):
        result["skipped"] = True
        result["skip_reason"] = "vpn_runtime_selector_unsupported"
        result["mihomo_runtime_state"] = runtime_state
        return result

    eligible_candidates = _load_selector_candidates()
    eligible_ids = {str(item.get("server_id") or "") for item in eligible_candidates}
    eligible_runtime_targets = {
        str(item.get("runtime_target") or item.get("server_id") or "")
        for item in eligible_candidates
    }
    inventory_ids = {server.server_id for server in runtime_operations.list_servers()}
    vpn_auto_restore_required = bool(
        server_mode == "auto"
        and active_auto_server_id
        and active_auto_server_id in eligible_ids
        and active_auto_server_id in inventory_ids
    )
    if server_mode == "auto" and active_auto_server_id and active_auto_server_id not in inventory_ids:
        result["vpn_auto_restore"] = {
            "ok": True,
            "skipped": True,
            "skip_reason": "active_auto_server_not_in_runtime_inventory",
            "requested_server_id": active_auto_server_id,
        }
    elif server_mode == "auto" and active_auto_server_id and active_auto_server_id not in eligible_ids:
        result["vpn_auto_restore"] = {
            "ok": True,
            "skipped": True,
            "skip_reason": "active_auto_server_outside_current_pool",
            "requested_server_id": active_auto_server_id,
        }
    elif vpn_auto_restore_required:
        target = next(
            (str(item.get("runtime_target") or active_auto_server_id) for item in eligible_candidates
             if str(item.get("server_id") or "") == active_auto_server_id),
            active_auto_server_id,
        )
        if target not in eligible_runtime_targets:
            result["vpn_auto_restore"] = {
                "ok": True,
                "skipped": True,
                "skip_reason": "active_auto_target_outside_current_pool",
                "requested_server_id": active_auto_server_id,
            }
            vpn_auto_restore_required = False
        else:
            result["vpn_auto_restore"] = restore_core_vpn_auto_runtime_target(
                active_auto_server_id, requested_by=requested_by,
            )
    if not vpn_auto_restore_required and result["vpn_auto_restore"] is None:
        result["vpn_auto_restore"] = {
            "ok": True,
            "skipped": True,
            "skip_reason": "vpn_auto_restore_not_required",
            "requested_server_id": active_auto_server_id or None,
        }

    vpn_global_target = result["requested_vpn_global_target"]
    apply_global = runtime_operations.apply_server_to_selector(
        "vpn-global",
        str(vpn_global_target),
    )
    result["vpn_global_restore"] = apply_global.to_dict()
    result["ok"] = bool(result["vpn_auto_restore"]["ok"]) and bool(apply_global.ok)
    return result


def restore_core_vpn_auto_runtime_target(server_id: str, *, requested_by: str = "runtime_restore") -> dict[str, Any]:
    """Restore only the current Core-owned persisted Auto target after runtime restart."""
    from fwrouter_api.adapters.xray_common import xray_writer_guard
    from fwrouter_api.services.vpn_auto_selection_state import advance_selection_revision, read_selection_fence

    wanted = str(server_id or "").strip()
    if not wanted:
        return {"ok": False, "error_code": "VPN_AUTO_SELECTION_TARGET_MISSING"}
    try:
        with xray_writer_guard(timeout_seconds=5.0):
            with db_session() as connection:
                fence = read_selection_fence(connection)
            if fence.get("active_server_id") != wanted:
                return {"ok": False, "deferred": True, "error_code": "VPN_AUTO_SELECTION_STALE_RESTORE"}
            candidates = _load_selector_candidates()
            candidate = next((item for item in candidates if str(item.get("server_id") or "") == wanted), None)
            if candidate is None:
                return {"ok": False, "error_code": "VPN_AUTO_SELECTION_TARGET_INELIGIBLE"}
            adapter, operations = _active_selector_runtime()
            if operations is None or RUNTIME_CAPABILITY_APPLY_SELECTOR not in _runtime_capabilities(adapter):
                return {"ok": False, "error_code": "VPN_RUNTIME_SELECTOR_UNSUPPORTED"}
            before_health, error = _runtime_health_or_error(adapter, operations)
            if before_health is None:
                return {"ok": False, "error_code": "VPN_RUNTIME_UNREACHABLE", "details": error}
            targets = {str(item.get("runtime_target") or item.get("server_id") or "") for item in candidates}
            target = str(candidate.get("runtime_target") or wanted)
            if target not in targets:
                return {"ok": False, "error_code": "VPN_AUTO_SELECTION_TARGET_INELIGIBLE"}
            generation = _runtime_generation_identity(
                before_health, operations, require_incarnation=str(adapter.get("adapter_id")) == "mihomo",
                expected_vpn_auto_target=target,
            )
            selectors = (before_health.details or {}).get("selectors") if isinstance(before_health.details, dict) else {}
            active = str((selectors or {}).get("vpn_auto_now") or "").strip()
            if active == target:
                return {"ok": True, "changed": False, "server_id": wanted, "runtime_target": target}
            with db_session() as connection:
                next_revision = advance_selection_revision(
                    connection, expected_revision=int(fence["revision"]),
                )
            if next_revision is None:
                return {"ok": False, "deferred": True, "error_code": "VPN_AUTO_SELECTION_STALE_STATE"}
            fence = {**fence, "revision": next_revision}
            apply_result = operations.apply_server_to_selector("vpn-auto", target)
            if not apply_result.ok:
                return {**apply_result.to_dict(), "selection_revision": next_revision}
            after_health, error = _runtime_health_or_error(adapter, operations)
            if after_health is None:
                return {"ok": False, "error_code": "VPN_AUTO_SELECTION_READBACK_UNCONFIRMED",
                        "details": error, "selection_revision": next_revision}
            after_selectors = (after_health.details or {}).get("selectors") if isinstance(after_health.details, dict) else {}
            after_target = str((after_selectors or {}).get("vpn_auto_now") or "").strip()
            after_generation = _runtime_generation_identity(
                after_health, operations, require_incarnation=str(adapter.get("adapter_id")) == "mihomo",
                expected_vpn_auto_target=target,
            )
            with db_session() as connection:
                after_fence = read_selection_fence(connection)
            if after_fence != fence or after_target != target or after_generation != generation:
                return {"ok": False, "error_code": "VPN_AUTO_SELECTION_READBACK_UNCONFIRMED",
                        "runtime_target": after_target or None, "selection_revision": next_revision}
            return {"ok": True, "changed": True, "server_id": wanted,
                    "runtime_target": target, "apply": apply_result.to_dict(),
                    "selection_revision": next_revision}
    except TimeoutError:
        return {"ok": False, "deferred": True, "error_code": "VPN_AUTO_SELECTION_WRITER_BUSY"}
    except (ValueError, RuntimeError, KeyError, TypeError) as exc:
        return {"ok": False, "deferred": True, "error_code": "VPN_AUTO_SELECTION_RESTORE_UNCONFIRMED",
                "error_message": str(exc)}


def _load_selector_candidates() -> list[dict[str, Any]]:
    """Load active server candidates from SQLite.

    The default dry-run selector uses stored server_ping_state and does not
    perform live delay checks. Candidates are loaded from the explicit SQLite
    vpn_auto list. On-demand checks are only performed by
    select_vpn_auto_server(..., check_on_demand=True) or apply=True.
    """

    with db_session() as connection:
        rows = connection.execute(
            f"""
            SELECT
                s.server_id,
                s.server_name,
                s.provider_name,
                s.inventory_state,
                sp.vpn_auto,
                sp.vpn_auto_priority,
                sp.global_list,
                sp.manually_deleted_at,
                ping.status AS ping_status,
                ping.last_ping_ms,
                ping.checked_at,
                ping.checked_by,
                ping.error_code,
                ping.error_message,
                json_extract(ping.metadata_json, '$.source') AS ping_source,
                CASE
                    WHEN c.server_id IS NOT NULL THEN s.server_name
                    ELSE COALESCE(
                        json_extract(s.raw_json, '$._fwrouter_runtime_name'),
                        json_extract(s.raw_json, '$.name'),
                        s.server_name
                    )
                END AS runtime_target
            FROM servers s
            LEFT JOIN server_preferences sp ON sp.server_id = s.server_id
            LEFT JOIN server_ping_state ping ON ping.server_id = s.server_id
            LEFT JOIN server_custom_https_proxy c ON c.server_id = s.server_id
            WHERE {auto_eligible_sql(server_alias="s", preferences_alias="sp")}
            ORDER BY
                CASE ping.status WHEN 'success' THEN 0 ELSE 1 END,
                CASE WHEN ping.last_ping_ms IS NULL THEN 1 ELSE 0 END,
                ping.last_ping_ms ASC,
                s.server_name ASC
            """
        ).fetchall()

    candidates = [
        {
            "server_id": row["server_id"],
            "server_name": row["server_name"],
            "provider_name": row["provider_name"],
            "inventory_state": row["inventory_state"],
            "runtime_target": row["runtime_target"] or row["server_id"],
            "mihomo_target": row["runtime_target"] or row["server_id"],
            "vpn_auto": bool(row["vpn_auto"]) if row["vpn_auto"] is not None else False,
            "manually_deleted_at": row["manually_deleted_at"],
            "vpn_auto_priority": max(
                MIN_VPN_AUTO_PRIORITY,
                min(MAX_VPN_AUTO_PRIORITY, int(row["vpn_auto_priority"] or 0)),
            ),
            "global_list": bool(row["global_list"]) if row["global_list"] is not None else True,
            "ping": {
                "status": row["ping_status"] or "unknown",
                "last_ping_ms": row["last_ping_ms"],
                "checked_at": row["checked_at"],
                "checked_by": row["checked_by"],
                "source": row["ping_source"],
                "error_code": row["error_code"],
                "error_message": row["error_message"],
            },
        }
        for row in rows
    ]
    for candidate in candidates:
        ping = candidate["ping"]
        if _is_manual_ping(ping):
            ping["status"] = "unknown"
            ping["last_ping_ms"] = None
    # Canonical logical member evidence is the primary ranking projection.
    # Legacy server_ping_state remains the compatibility source for servers
    # only when canonical topology/evidence is absent. Manual evidence is never eligible.
    from fwrouter_api.services.logical_topology import get_logical_topologies

    topologies = get_logical_topologies([str(item["server_id"]) for item in candidates])
    for candidate in candidates:
        topology = topologies.get(str(candidate["server_id"]))
        if not topology:
            continue
        effective = next(
            (member for member in topology["members"] if member.get("is_effective_active")),
            None,
        )
        # A manual refresh is diagnostic only and cannot qualify an automatic
        # candidate; when it is the latest canonical evidence, rank as unknown.
        if not effective or not effective.get("checked_at"):
            continue
        if effective.get("probe_lane") == "manual":
            candidate["ping"] = {**candidate["ping"], "status": "unknown", "last_ping_ms": None, "source": "manual_only"}
            continue
        status = str(effective.get("status") or "unknown")
        candidate["ping"] = {
            "status": "success" if status == "healthy" and effective.get("fresh") else ("failed" if status == "failed" and effective.get("fresh") else "unknown"),
            "last_ping_ms": effective.get("latency_ms") if status == "healthy" and effective.get("fresh") else None,
            "checked_at": effective.get("checked_at"),
            "checked_by": "canonical_health",
            "source": "canonical_health",
            "error_code": effective.get("error_code"),
            "error_message": effective.get("error_message"),
            "canonical_health_status": topology["health"]["status"],
        }
    return candidates


def _candidate_with_on_demand_ping(
    candidate: dict[str, Any],
    *,
    checked_by: str,
    update_ping_state: bool,
    timeout_ms: int,
) -> dict[str, Any]:
    ping = check_server_delay(
        candidate["server_id"],
        update_state=update_ping_state,
        checked_by=checked_by,
        source="selector",
        timeout_ms=timeout_ms,
    )

    updated = dict(candidate)
    updated["ping"] = {
        "status": ping["status"],
        "last_ping_ms": ping["last_ping_ms"],
        "checked_at": None,
        "error_code": ping["error_code"],
        "error_message": ping["error_message"],
        "latency_label": ping["latency_label"],
        "updated_state": ping["updated_state"],
    }
    updated["on_demand_ping"] = ping
    return updated


def _parse_checked_at(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized:
        return None
    try:
        return datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError:
        return None


def _ping_status_rank(candidate: dict[str, Any]) -> int:
    status = str(((candidate.get("ping") or {}).get("status")) or "unknown").lower()
    if status == "success":
        return 0
    if status == "unknown":
        return 1
    return 2


def _latency_sort_value(candidate: dict[str, Any]) -> int:
    value = ((candidate.get("ping") or {}).get("last_ping_ms"))
    if isinstance(value, bool) or value is None:
        return 10**9
    try:
        return int(value)
    except (TypeError, ValueError):
        return 10**9


def _candidate_priority(candidate: dict[str, Any]) -> int:
    return max(
        MIN_VPN_AUTO_PRIORITY,
        min(MAX_VPN_AUTO_PRIORITY, int(candidate.get("vpn_auto_priority") or 0)),
    )


def _candidate_effective_ping(candidate: dict[str, Any]) -> float | None:
    if str(((candidate.get("ping") or {}).get("status")) or "").lower() != "success":
        return None
    latency = (candidate.get("ping") or {}).get("last_ping_ms")
    if isinstance(latency, bool) or latency is None:
        return None
    try:
        latency_value = float(latency)
    except (TypeError, ValueError):
        return None
    priority = _candidate_priority(candidate)
    if priority < 0:
        return None
    weight = priority if priority > 0 else 1
    return latency_value / float(weight)


def _candidate_has_auto_health(candidate: dict[str, Any] | None) -> bool:
    if not candidate:
        return False
    ping = candidate.get("ping") if isinstance(candidate.get("ping"), dict) else {}
    if str(ping.get("status") or "").strip().lower() != "success":
        return False
    source = str(ping.get("source") or "").strip().lower()
    if source == "manual" or _is_manual_ping(ping):
        return False
    return True


def _is_manual_ping(ping: dict[str, Any]) -> bool:
    source = str(ping.get("source") or "").strip().lower()
    checked_by = str(ping.get("checked_by") or "").strip().lower()
    return source == "manual" or checked_by in {"manual", "ui", "admin_ui"} or checked_by.startswith(
        ("manual_", "ui_", "admin_ui")
    ) or ":manual" in checked_by


def _candidate_score(candidate: dict[str, Any]) -> dict[str, Any]:
    ping = candidate.get("ping") if isinstance(candidate.get("ping"), dict) else {}
    return {
        "server_id": candidate.get("server_id"),
        "server_name": candidate.get("server_name"),
        "runtime_target": candidate.get("runtime_target") or candidate.get("server_id"),
        "priority": _candidate_priority(candidate),
        "real_ping_ms": ping.get("last_ping_ms"),
        "ping_status": ping.get("status") or "unknown",
        "effective_ping": _candidate_effective_ping(candidate),
    }


def _candidate_scores(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [_candidate_score(candidate) for candidate in candidates]


def _build_on_demand_shortlist(
    candidates: list[dict[str, Any]],
    *,
    active_before: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    if limit <= 0 or not candidates:
        return []

    priority_order = sorted(
        candidates,
        key=lambda item: (
            0 if is_auto_eligible(vpn_auto=item.get("vpn_auto"), vpn_auto_priority=item.get("vpn_auto_priority"), inventory_state=item.get("inventory_state"), manually_deleted_at=item.get("manually_deleted_at"), provider_managed_legacy=item.get("provider_managed_legacy", False), provider_internal_member=item.get("provider_internal_member", False)) else 1,
            -int(item.get("vpn_auto_priority") or 0),
            _ping_status_rank(item),
            _latency_sort_value(item),
            str(item.get("server_name") or item.get("server_id") or ""),
        ),
    )
    latency_order = sorted(
        candidates,
        key=lambda item: (
            _ping_status_rank(item),
            _latency_sort_value(item),
            -int(item.get("vpn_auto_priority") or 0),
            str(item.get("server_name") or item.get("server_id") or ""),
        ),
    )
    refresh_order = sorted(
        candidates,
        key=lambda item: (
            0 if _parse_checked_at((item.get("ping") or {}).get("checked_at")) is None else 1,
            _parse_checked_at((item.get("ping") or {}).get("checked_at")) or datetime.min,
            _ping_status_rank(item),
            -int(item.get("vpn_auto_priority") or 0),
            str(item.get("server_name") or item.get("server_id") or ""),
        ),
    )

    merged: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _append(candidate: dict[str, Any]) -> None:
        server_id = str(candidate.get("server_id") or "")
        if not server_id or server_id in seen or len(merged) >= limit:
            return
        seen.add(server_id)
        merged.append(candidate)

    if active_before:
        for candidate in candidates:
            if str(candidate.get("runtime_target") or candidate.get("server_id") or "") == active_before:
                _append(candidate)
                break

    for ordered in (priority_order, latency_order, refresh_order, candidates):
        for candidate in ordered:
            _append(candidate)
            if len(merged) >= limit:
                return merged

    return merged


def _select_best_successful_candidate(
    candidates: list[dict[str, Any]],
) -> dict[str, Any] | None:
    successful = [
        candidate
        for candidate in candidates
        if candidate["ping"]["status"] == "success"
        and candidate["ping"]["last_ping_ms"] is not None
    ]

    if not successful:
        return None

    return sorted(
        successful,
        key=lambda item: (
            item["ping"]["last_ping_ms"],
            item["server_name"],
        ),
    )[0]


def _select_candidate_with_priority(
    candidates: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    auto_selectable = [
        candidate
        for candidate in candidates
        if is_auto_eligible(vpn_auto=candidate.get("vpn_auto"), vpn_auto_priority=candidate.get("vpn_auto_priority"), inventory_state=candidate.get("inventory_state"), manually_deleted_at=candidate.get("manually_deleted_at"), provider_managed_legacy=candidate.get("provider_managed_legacy", False), provider_internal_member=candidate.get("provider_internal_member", False))
    ]
    best_by_ping = _select_best_successful_candidate(auto_selectable)
    if best_by_ping is None:
        executable = [c for c in auto_selectable if c.get("execution_capability") == "provider_switch" and c.get("runtime_evidence") == "not_observed" and c.get("provider_eligible") is True]
        selected = sorted(executable, key=lambda c: (-_candidate_priority(c), str(c["server_id"]), str(c.get("member_id") or "")))[0] if executable else None
        return selected, None

    scored = [
        candidate
        for candidate in auto_selectable
        if _candidate_effective_ping(candidate) is not None
    ]
    if not scored:
        return best_by_ping, None

    selected = sorted(
        scored,
        key=lambda item: (
            _candidate_effective_ping(item),
            int(item["ping"]["last_ping_ms"]),
            item["server_name"],
            item["server_id"],
        ),
    )[0]
    if selected["server_id"] == best_by_ping["server_id"]:
        return selected, None
    return selected, best_by_ping


def select_vpn_auto_server(
    *,
    apply: bool = False,
    reason: str = "manual",
    requested_by: str = "api",
    management_context: dict[str, Any] | None = None,
    check_on_demand: bool = False,
    update_ping_state: bool = True,
    on_demand_limit: int = DEFAULT_ON_DEMAND_LIMIT,
    timeout_ms: int = DEFAULT_ON_DEMAND_TIMEOUT_MS,
    exclude_active: bool = False,
    post_check: bool = True,
    origin: str = "unknown",
    candidate_server_id: str | None = None,
    allow_provider_fallback: bool = True,
    expected_selection_revision: int | None = None,
    expected_active_server_id: str | None = None,
    expected_runtime_target: str | None = None,
    expected_runtime_target_valid: bool | None = None,
    operation_id: str | None = None,
) -> dict[str, Any]:
    """Select a vpn-auto server.

    Default dry-run does not ping servers and uses stored server_ping_state.

    When check_on_demand=True or apply=True, selector checks a bounded list of
    candidates now, chooses the lowest successful latency, and optionally applies
    it through the active VPN runtime adapter. This is the mode for "need to change server".

    If apply=True and post_check=True, selector checks delay for the selected
    server after switching. A failed post-check does not rollback the switch.
    """

    attribution = build_management_attribution(
        requested_by=requested_by,
        context=management_context,
    )
    attribution_error = build_incomplete_attribution_error(attribution)
    selection_operation_id = str(operation_id or uuid4())
    if apply and attribution_error is not None:
        return {
            "ok": False,
            "reason": reason,
            "requested_by": requested_by,
            "management_attribution": attribution,
            "apply": apply,
            "applied": False,
            "error_code": attribution_error["code"],
            "error_message": attribution_error["message"],
            "error": attribution_error,
            **_unconfirmed_selector_outcome(reason=reason, origin=origin, apply=apply),
        }

    runtime_adapter, runtime_operations = _active_selector_runtime()
    runtime_capabilities = _runtime_capabilities(runtime_adapter)
    runtime_adapter_id = str(runtime_adapter.get("adapter_id") or "unknown")
    health, health_error = _runtime_health_or_error(runtime_adapter, runtime_operations)
    if health is None:
        error_code = str((health_error or {}).get("error_code") or "VPN_RUNTIME_UNREACHABLE")
        if runtime_adapter_id == "mihomo":
            error_code = "MIHOMO_CONTROLLER_UNREACHABLE"
        return {
            "ok": False,
            "reason": reason,
            "requested_by": requested_by,
            "management_attribution": attribution,
            "apply": apply,
            "applied": False,
            "runtime_adapter": runtime_adapter,
            "runtime_adapter_id": runtime_adapter_id,
            "error_code": error_code,
            "error_message": str((health_error or {}).get("error_message") or "VPN runtime is unreachable."),
            "active_before": None,
            "active_after": None,
            "exclude_active": exclude_active,
            "selected_server_id": None,
            "selected_server_name": None,
            "candidates_count": 0,
            "runtime_servers_count": 0,
            "mihomo_servers_count": 0,
            "selection_basis": "vpn_runtime_controller_unreachable",
            "fail_open_direct_recommended": True,
            **_unconfirmed_selector_outcome(reason=reason, origin=origin, apply=apply),
        }
    health_details = health.details if isinstance(getattr(health, "details", None), dict) else {}
    health_selectors = health_details.get("selectors") if isinstance(health_details.get("selectors"), dict) else {}
    # health.active_server_id is effective routing and can describe vpn-global
    # while this operation only changes vpn-auto. Keep those identities separate.
    active_runtime_value = health_selectors.get("vpn_auto_now")
    if "vpn_auto_now" not in health_selectors:
        active_runtime_value = getattr(health, "active_server_id", None)
    active_runtime_target = str(active_runtime_value or "").strip() or None
    effective_runtime_target_before = str(getattr(health, "active_server_id", "") or "").strip() or None
    global_selector_before = str(health_selectors.get("vpn_global_now") or "").strip() or None
    if expected_runtime_target_valid is False:
        return {
            "ok": False,
            "reason": reason,
            "apply": apply,
            "applied": False,
            "error_code": "VPN_AUTO_SELECTION_RUNTIME_EVIDENCE_UNAVAILABLE",
            "error_message": "Watchdog runtime selector evidence could not be captured before its probes.",
            "retryable": True,
            "operation_id": selection_operation_id,
            **_deferred_selector_outcome(reason=reason, origin=origin, apply=apply),
        }
    if expected_runtime_target_valid is True and active_runtime_target != expected_runtime_target:
        return {
            "ok": False,
            "reason": reason,
            "apply": apply,
            "applied": False,
            "error_code": "VPN_AUTO_SELECTION_STALE_RUNTIME_EVIDENCE",
            "error_message": "The vpn-auto runtime target changed after watchdog evidence was captured.",
            "retryable": True,
            "operation_id": selection_operation_id,
            **_deferred_selector_outcome(reason=reason, origin=origin, apply=apply),
        }
    try:
        with db_session() as connection:
            selection_fence = read_selection_fence(connection)
    except (ValueError, KeyError, TypeError) as exc:
        return {
            "ok": False,
            "reason": reason,
            "apply": apply,
            "applied": False,
            "error_code": "VPN_AUTO_SELECTION_FENCE_INVALID",
            "error_message": str(exc),
            "retryable": False,
            "operation_id": selection_operation_id,
            **_unconfirmed_selector_outcome(reason=reason, origin=origin, apply=apply),
        }
    if (
        expected_selection_revision is not None
        and selection_fence["revision"] != int(expected_selection_revision)
    ) or (
        expected_active_server_id is not None
        and selection_fence["active_server_id"] != str(expected_active_server_id)
    ):
        return {
            "ok": False,
            "reason": reason,
            "apply": apply,
            "applied": False,
            "error_code": "VPN_AUTO_SELECTION_STALE_EVIDENCE",
            "error_message": "Watchdog selection evidence no longer matches the current vpn-auto state.",
            "retryable": True,
            "operation_id": selection_operation_id,
            **_deferred_selector_outcome(reason=reason, origin=origin, apply=apply),
        }
    with db_session() as connection:
        routing_row = connection.execute(
            "SELECT server_mode, applied_fixed_server_id, desired_fixed_server_id FROM routing_global_state WHERE id = 1"
        ).fetchone()
    effective_mode = str(routing_row["server_mode"] or "auto").strip().lower() if routing_row else "auto"
    effective_fixed_id = (
        str(routing_row["applied_fixed_server_id"] or routing_row["desired_fixed_server_id"] or "").strip() or None
        if routing_row and effective_mode == "fixed" else None
    )
    if (
        runtime_operations is None
        or RUNTIME_CAPABILITY_LIST_SERVERS not in runtime_capabilities
        or RUNTIME_CAPABILITY_APPLY_SERVER not in runtime_capabilities
    ):
        return {
            "ok": False,
            "reason": reason,
            "requested_by": requested_by,
            "management_attribution": attribution,
            "apply": apply,
            "applied": False,
            "runtime_adapter": runtime_adapter,
            "runtime_adapter_id": runtime_adapter_id,
            "error_code": "VPN_RUNTIME_SELECTOR_UNSUPPORTED",
            "error_message": (
                f"Active VPN runtime adapter does not expose selector operations: {runtime_adapter_id}."
            ),
            "active_before": None,
            "active_after": None,
            "exclude_active": exclude_active,
            "selected_server_id": None,
            "selected_server_name": None,
            "candidates_count": 0,
            "runtime_servers_count": 0,
            "mihomo_servers_count": 0,
            "selection_basis": "vpn_runtime_selector_unsupported",
            "fail_open_direct_recommended": True,
            **_unconfirmed_selector_outcome(reason=reason, origin=origin, apply=apply),
        }
    try:
        runtime_servers = runtime_operations.list_servers()
    except Exception as exc:
        error_code = "VPN_RUNTIME_INVENTORY_UNAVAILABLE"
        if runtime_adapter_id == "mihomo":
            error_code = "MIHOMO_INVENTORY_UNAVAILABLE"
        return {
            "ok": False,
            "reason": reason,
            "requested_by": requested_by,
            "management_attribution": attribution,
            "apply": apply,
            "applied": False,
            "runtime_adapter": runtime_adapter,
            "runtime_adapter_id": runtime_adapter_id,
            "error_code": error_code,
            "error_message": str(exc),
            "active_before": None,
            "active_after": None,
            "exclude_active": exclude_active,
            "selected_server_id": None,
            "selected_server_name": None,
            "candidates_count": 0,
            "runtime_servers_count": 0,
            "mihomo_servers_count": 0,
            "selection_basis": "vpn_runtime_inventory_unavailable",
            "fail_open_direct_recommended": True,
            **_unconfirmed_selector_outcome(reason=reason, origin=origin, apply=apply),
        }
    runtime_server_ids = {server.server_id for server in runtime_servers}
    runtime_selector_targets = {
        str(target)
        for target in (health_selectors.get("vpn_auto_targets") or [])
        if str(target or "").strip() and str(target) != "DIRECT"
    }
    runtime_inventory_targets = runtime_server_ids | runtime_selector_targets

    all_candidates = _load_runtime_target_inventory()
    ranked_candidates = _load_selector_candidates()
    runtime_candidates = [
        candidate for candidate in ranked_candidates
        if str(candidate.get("runtime_target") or candidate["server_id"]) in runtime_inventory_targets
    ]
    candidate_fingerprint = _candidate_fence(runtime_candidates)
    with db_session() as connection:
        candidate_pool_signature = selection_pool_signature(connection)
    candidates = runtime_candidates
    try:
        with db_session() as connection:
            loaded_fence = read_selection_fence(connection)
        if loaded_fence != selection_fence:
            return {
                "ok": False, "reason": reason, "apply": apply, "applied": False,
                "error_code": "VPN_AUTO_SELECTION_STALE_SNAPSHOT",
                "error_message": "vpn-auto state changed while the candidate snapshot was loaded.",
                "retryable": True,
                "operation_id": selection_operation_id,
                **_deferred_selector_outcome(reason=reason, origin=origin, apply=apply),
            }
    except (ValueError, KeyError, TypeError) as exc:
        return {
            "ok": False, "reason": reason, "apply": apply, "applied": False,
            "error_code": "VPN_AUTO_SELECTION_FENCE_INVALID", "error_message": str(exc),
            "retryable": False, "operation_id": selection_operation_id,
            **_unconfirmed_selector_outcome(reason=reason, origin=origin, apply=apply),
        }
    try:
        runtime_generation = _runtime_generation_identity(
            health,
            runtime_operations,
            require_incarnation=runtime_adapter_id == "mihomo",
        ) if apply else ""
    except RuntimeError as exc:
        return {
            "ok": False, "reason": reason, "apply": apply, "applied": False,
            "error_code": "VPN_AUTO_SELECTION_RUNTIME_IDENTITY_UNAVAILABLE",
            "error_message": str(exc), "retryable": True,
            "operation_id": selection_operation_id,
            **_deferred_selector_outcome(reason=reason, origin=origin, apply=apply),
        }

    if candidate_server_id is not None:
        candidates = [c for c in candidates if c["server_id"] == candidate_server_id]

    active_before, active_before_name, active_identity_status = _runtime_target_identity(
        active_runtime_target, all_candidates
    )

    if exclude_active and active_runtime_target:
        candidates = [
            candidate
            for candidate in candidates
            if str(candidate.get("runtime_target") or candidate["server_id"]) != active_runtime_target
        ]

    should_check_on_demand = check_on_demand or apply
    if apply and should_check_on_demand and xray_writer_guard_is_held():
        return {
            "ok": False, "reason": reason, "apply": True, "applied": False,
            "error_code": "VPN_AUTO_SELECTION_OUTER_GUARD_HELD",
            "error_message": "Selection probes cannot run while a caller holds the shared writer guard.",
            "retryable": True,
            "operation_id": selection_operation_id,
            "selection_revision": selection_fence["revision"],
            **_deferred_selector_outcome(reason=reason, origin=origin, apply=True),
        }
    on_demand_results: list[dict[str, Any]] = []
    checked_count = 0
    success_count = 0
    failed_count = 0

    if should_check_on_demand:
        safe_limit = max(1, min(on_demand_limit, 20))
        checked_by = f"selector:{reason}"
        shortlist = _build_on_demand_shortlist(
            candidates,
            active_before=active_runtime_target,
            limit=safe_limit,
        )

        pings = check_server_delays(
            [candidate["server_id"] for candidate in shortlist],
            update_state=update_ping_state,
            checked_by=checked_by,
            source="selector",
            timeout_ms=timeout_ms,
            fallback_check=check_server_delay,
        )
        on_demand_results = []
        for candidate, ping in zip(shortlist, pings):
            updated = dict(candidate)
            updated["ping"] = {
                "status": ping["status"],
                "last_ping_ms": ping["last_ping_ms"],
                "checked_at": None,
                "error_code": ping["error_code"],
                "error_message": ping["error_message"],
                "latency_label": ping["latency_label"],
                "updated_state": ping["updated_state"],
            }
            updated["on_demand_ping"] = ping
            on_demand_results.append(updated)

        checked_count = len(on_demand_results)
        success_count = sum(
            1 for candidate in on_demand_results if candidate["ping"]["status"] == "success"
        )
        failed_count = sum(
            1 for candidate in on_demand_results if candidate["ping"]["status"] == "failed"
        )
        selected, best_latency_candidate = _select_candidate_with_priority(on_demand_results)
        selection_basis = (
            "on-demand successful latency check with vpn-auto priority"
            if selected and best_latency_candidate is not None
            else "on-demand successful latency check"
        )
    else:
        selected, best_latency_candidate = _select_candidate_with_priority(candidates)
        selected_ping = selected.get("ping") if selected and isinstance(selected.get("ping"), dict) else {}
        if selected and selected_ping.get("source") == "canonical_health":
            selection_basis = (
                "canonical effective-member health and latency with vpn-auto priority"
                if best_latency_candidate is not None
                else "canonical effective-member health and latency"
            )
        else:
            selection_basis = (
                "stored server_ping_state with vpn-auto priority"
                if selected and best_latency_candidate is not None
                else (
                    "stored server_ping_state, then known latency, then server name"
                    if selected
                    else "no active SQLite vpn_auto servers matched runtime inventory"
                )
            )

    if selected is None and candidate_server_id is None and allow_provider_fallback:
        from fwrouter_api.services.provider_managed import provider_candidates
        provider_fallback = provider_candidates(exclude_active=True)
        from fwrouter_api.services.vpn_auto_exclusive import get_vpn_auto_exclusive_source_ref
        exclusive_source_ref = get_vpn_auto_exclusive_source_ref()
        if exclusive_source_ref:
            provider_fallback = [
                item for item in provider_fallback
                if str(item.get("source_ref") or "") == exclusive_source_ref
            ]
        selected, best_latency_candidate = _select_candidate_with_priority(provider_fallback)
    if selected and selected.get("execution_capability") == "provider_switch":
        base = {"ok": True, "selected_server_id": selected["server_id"], "selected_server_name": selected["server_name"],
                "selected_member_id": selected["member_id"], "selection_basis": "provider_priority_stable_identity",
                "selected_ping": selected["ping"], "applied": False, "active_before": active_before,
                "active_after": active_before, "runtime_evidence": "not_observed", "apply": apply}
        if not apply:
            return {**base, "selection_outcome": "candidate"}
        from fwrouter_api.services.provider_managed import execute_provider_operation
        execution = execute_provider_operation(selected["source_ref"], "switch", member_id=selected["member_id"], _select_logical=True)
        changed = bool(execution.get("runtime_verified") and (execution.get("changed") or active_before != selected["server_id"]))
        return {**base, "ok": bool(execution.get("ok")), "applied": changed,
                "selected_member_id": execution.get("actual_member_id") or selected["member_id"],
                "selection_outcome": ("selected" if changed else "noop") if execution.get("runtime_verified") else "unconfirmed",
                "active_after": selected["server_id"] if execution.get("runtime_verified") else active_before,
                "provider_operation": execution, "error_code": execution.get("error_code")}

    result: dict[str, Any] = {
        "ok": selected is not None,
        "reason": reason,
        "requested_by": requested_by,
        "management_attribution": attribution,
        "apply": apply,
        "check_on_demand": should_check_on_demand,
        "update_ping_state": update_ping_state if should_check_on_demand else False,
        "active_before": active_before,
        "active_after": active_before,
        "active_before_runtime_target": active_runtime_target,
        "active_before_name": active_before_name,
        "active_before_identity_status": active_identity_status,
        "effective_route": {
            "server_mode": effective_mode,
            "fixed_server_id": effective_fixed_id,
            "runtime_target_before": effective_runtime_target_before,
            "runtime_target_after": effective_runtime_target_before,
            "global_selector_before": global_selector_before,
            "changed": None,
            "traffic_impact_confirmed": False,
        },
        "exclude_active": exclude_active,
        "runtime_adapter": runtime_adapter,
        "runtime_adapter_id": runtime_adapter_id,
        "selected_server_id": selected["server_id"] if selected else None,
        "selected_server_name": selected["server_name"] if selected else None,
        "candidates_count": len(candidates),
        "auto_selectable_candidates_count": sum(
            1 for candidate in candidates if is_auto_eligible(vpn_auto=candidate.get("vpn_auto"), vpn_auto_priority=candidate.get("vpn_auto_priority"), inventory_state=candidate.get("inventory_state"), manually_deleted_at=candidate.get("manually_deleted_at"), provider_managed_legacy=candidate.get("provider_managed_legacy", False), provider_internal_member=candidate.get("provider_internal_member", False))
        ),
        "runtime_servers_count": len(runtime_servers),
        "mihomo_servers_count": len(runtime_servers),
        "selection_basis": selection_basis,
        "selected_ping": selected["ping"] if selected else None,
        "selected_vpn_auto_priority": int(selected.get("vpn_auto_priority") or 0) if selected else 0,
        "candidate_scores": _candidate_scores(_auto_selectable_candidates(candidates)),
        "on_demand": {
            "limit": max(1, min(on_demand_limit, 20)),
            "timeout_ms": timeout_ms,
            "checked_count": checked_count,
            "success_count": success_count,
            "failed_count": failed_count,
            "candidate_shortlist": [
                candidate["server_id"]
                for candidate in (shortlist if should_check_on_demand else [])
            ],
            "results": [
                candidate["on_demand_ping"]
                for candidate in on_demand_results
            ],
        },
        "priority_override": (
            {
                "selected_server_id": selected["server_id"],
                "selected_vpn_auto_priority": int(selected.get("vpn_auto_priority") or 0),
                "best_latency_server_id": best_latency_candidate["server_id"],
                "best_latency_ping_ms": best_latency_candidate["ping"]["last_ping_ms"],
                "best_latency_effective_ping": _candidate_effective_ping(best_latency_candidate),
                "selected_ping_ms": selected["ping"]["last_ping_ms"],
                "selected_effective_ping": _candidate_effective_ping(selected),
            }
            if should_check_on_demand and selected and 'best_latency_candidate' in locals() and best_latency_candidate is not None
            else None
        ),
        "fail_open_direct_recommended": should_check_on_demand and selected is None,
        "applied": False,
        "apply_result": None,
        "post_check_enabled": post_check if apply else False,
        "post_switch_check": None,
        "post_check_failed_no_rollback": False,
        "selection_outcome": "not_applied",
        "operation_id": selection_operation_id,
        "selection_revision": selection_fence["revision"],
        "auto_transition": {
            "active_before_id": active_before,
            "active_before_name": active_before_name,
            "active_before_runtime_target": active_runtime_target,
            "selected_server_id": selected["server_id"] if selected else None,
            "selected_server_name": safe_human_label(selected.get("server_name"), entity_id=selected.get("server_id")) if selected else None,
            "selected_runtime_target": (selected.get("runtime_target") or selected["server_id"]) if selected else None,
            "active_after_id": active_before,
            "active_after_name": active_before_name,
            "active_after_runtime_target": active_runtime_target,
            "outcome": "dry_run" if not apply else "not_applied",
            "changed": False if not apply else None,
            "reason_code": _selector_reason_code(reason, origin=origin),
            "origin": _safe_selection_origin(origin),
            "actor_attribution": "caller_supplied_unverified",
            "correlation_id": None,
            "selector_readback": "not_checked",
        },
        "post_check": {
            "enabled": post_check if apply else False,
            "status": "not_run",
            "result": None,
            "failed_no_rollback": False,
        },
    }

    if not selected:
        return result

    selected_runtime_target = str(selected.get("runtime_target") or selected["server_id"])
    if active_runtime_target and selected_runtime_target == active_runtime_target:
        repaired_revision: int | None = None
        if apply:
            try:
                with xray_writer_guard(timeout_seconds=5.0):
                    stale_reason = _selection_fence_error(
                        expected_fence=selection_fence,
                        candidate_fingerprint=candidate_fingerprint,
                        pool_signature=candidate_pool_signature,
                        runtime_generation=runtime_generation,
                        runtime_inventory_targets=runtime_inventory_targets,
                        selected_server_id=str(selected["server_id"]),
                        expected_runtime_target=(expected_runtime_target if expected_runtime_target_valid is True else active_runtime_target),
                    )
                    if not stale_reason and (
                        selection_fence.get("active_server_id") != str(selected["server_id"])
                        or selection_fence.get("provenance_server_id") != str(selected["server_id"])
                    ):
                        decision_id = str(uuid4())
                        repaired_at = datetime.now(timezone.utc).isoformat()
                        repaired_provenance = {
                            "decision_id": decision_id,
                            "operation_id": selection_operation_id,
                            "selected_server_id": str(selected["server_id"]),
                            "selected_server_label": safe_human_label(selected.get("server_name"), entity_id=selected.get("server_id")),
                            "reason_code": "runtime_readback_state_repair",
                            "origin": _safe_selection_origin(origin),
                            "actor_attribution": "core_reconcile",
                            "selected_at": repaired_at,
                        }
                        with db_session() as connection:
                            repaired_revision = commit_active_selection(
                                connection,
                                expected_revision=int(selection_fence["revision"]),
                                expected_active_server_id=selection_fence["active_server_id"],
                                expected_provenance_decision_id=selection_fence["decision_id"],
                                server_id=str(selected["server_id"]),
                                provenance=repaired_provenance,
                            )
                        if repaired_revision is None:
                            stale_reason = "VPN_AUTO_SELECTION_PERSISTENCE_CAS_FAILED"
            except TimeoutError as exc:
                return {
                    "ok": False, "reason": reason, "apply": True, "applied": False,
                    "error_code": "VPN_AUTO_SELECTION_BUSY", "error_message": str(exc),
                    "retryable": True,
                    "operation_id": selection_operation_id,
                    **_deferred_selector_outcome(reason=reason, origin=origin, apply=True),
                }
            if stale_reason:
                return {
                    "ok": False, "reason": reason, "apply": True, "applied": False,
                    "error_code": stale_reason,
                    "error_message": "vpn-auto state changed during the probe phase; no runtime apply was attempted.",
                    "retryable": True,
                    "operation_id": selection_operation_id,
                    **_deferred_selector_outcome(reason=reason, origin=origin, apply=True),
                }
        result["ok"] = True
        result["applied"] = False
        result["active_after"] = active_before
        result["active_after_runtime_target"] = active_runtime_target
        result["effective_route"]["changed"] = False
        result["selection_basis"] = "selected server already active"
        result["noop"] = True
        result["noop_reason"] = "selected_server_already_active"
        result["selection_outcome"] = "noop"
        result["canonical_state_repaired"] = repaired_revision is not None
        if repaired_revision is not None:
            result["selection_revision"] = repaired_revision
            result["selection_provenance"] = {
                "operation_id": selection_operation_id,
                "selected_server_id": str(selected["server_id"]),
                "reason_code": "runtime_readback_state_repair",
                "origin": _safe_selection_origin(origin),
                "selected_at": repaired_at,
            }
        result["auto_transition"]["outcome"] = "noop"
        result["auto_transition"]["changed"] = False
        result["auto_transition"]["selector_readback"] = "matched_before"
        # Explicit API requests need an auditable no-op outcome; routine
        # selector reads and watchdog polls remain event-free.
        if apply and _safe_selection_origin(origin) == "api":
            write_operational_log(
                event_type="server_selection_noop",
                message="VPN-auto server selection was already effective.",
                details={
                    "requested_by": requested_by,
                    "reason": reason,
                    "reason_code": _selector_reason_code(reason, origin=origin),
                    "source": _safe_selection_origin(origin),
                    "result": "no_op",
                    "outcome": "no_op",
                    "changed": False,
                    "selected_server_name": safe_human_label(selected.get("server_name"), entity_id=selected.get("server_id")),
                },
            )
        return result

    if apply:
        try:
            with xray_writer_guard(timeout_seconds=5.0):
                stale_reason = _selection_fence_error(
                    expected_fence=selection_fence,
                    candidate_fingerprint=candidate_fingerprint,
                    pool_signature=candidate_pool_signature,
                    runtime_generation=runtime_generation,
                    runtime_inventory_targets=runtime_inventory_targets,
                    selected_server_id=str(selected["server_id"]),
                    expected_runtime_target=(expected_runtime_target if expected_runtime_target_valid is True else active_runtime_target),
                )
                if stale_reason:
                    result.update({
                        "ok": False,
                        "applied": False,
                        "error_code": stale_reason,
                        "error_message": "vpn-auto state changed during the probe phase; no runtime apply was attempted.",
                        "retryable": True,
                        "selection_outcome": "deferred",
                        "auto_transition": {**result["auto_transition"], "outcome": "deferred"},
                    })
                    return result
                apply_result = runtime_operations.apply_server(selected_runtime_target)
                result["applied"] = apply_result.ok
                result["apply_result"] = apply_result.to_dict()
                apply_details = getattr(apply_result, "details", None)
                apply_details = apply_details if isinstance(apply_details, dict) else {}
                observed_target = str(apply_details.get("selector_after") or "").strip() or None
                observed_id, observed_name, observed_identity_status = _runtime_target_identity(observed_target, all_candidates)
                result["active_after"] = observed_id
                result["active_after_runtime_target"] = observed_target
                result["active_after_name"] = observed_name
                result["active_after_identity_status"] = observed_identity_status
                selector_readback_matches = observed_target == selected_runtime_target
                result["selector_readback_matches"] = selector_readback_matches
                result["selection_outcome"] = (
                    "selected" if apply_result.ok and selector_readback_matches and observed_identity_status == "confirmed"
                    else "unconfirmed" if apply_result.ok else "failed"
                )
                result["ok"] = result["selection_outcome"] == "selected"
                result["auto_transition"].update({
                    "active_after_id": observed_id,
                    "active_after_name": observed_name,
                    "active_after_runtime_target": observed_target,
                    "outcome": result["selection_outcome"],
                    "changed": active_runtime_target != observed_target if active_runtime_target is not None and observed_target is not None else None,
                    "selector_readback": "matched_selected" if selector_readback_matches else ("unconfirmed" if observed_target is None else "different_target"),
                })
                effective_after = str(apply_details.get("active_after") or "").strip() or None
                if effective_after:
                    result["effective_route"]["runtime_target_after"] = effective_after
                if effective_runtime_target_before and effective_after:
                    result["effective_route"]["changed"] = effective_runtime_target_before != effective_after
                if apply_result.ok and selector_readback_matches and observed_id == selected["server_id"]:
                    decision_id = str(uuid4())
                    reason_code = _selector_reason_code(reason, origin=origin)
                    safe_origin = _safe_selection_origin(origin)
                    selected_label = safe_human_label(selected.get("server_name"), entity_id=selected.get("server_id"))
                    selected_at = datetime.now(timezone.utc).isoformat()
                    provenance = {
                        "decision_id": decision_id,
                        "operation_id": selection_operation_id,
                        "selected_server_id": str(selected["server_id"]),
                        "selected_server_label": selected_label,
                        "reason_code": reason_code,
                        "origin": safe_origin,
                        "actor_attribution": "caller_supplied_unverified",
                        "selected_at": selected_at,
                    }
                    with db_session() as connection:
                        next_revision = commit_active_selection(
                            connection,
                            expected_revision=int(selection_fence["revision"]),
                            expected_active_server_id=selection_fence["active_server_id"],
                            expected_provenance_decision_id=selection_fence["decision_id"],
                            server_id=str(selected["server_id"]),
                            provenance=provenance,
                        )
                    if next_revision is None:
                        result.update({
                            "ok": False,
                            "selection_outcome": "unconfirmed",
                            "error_code": "VPN_AUTO_SELECTION_PERSISTENCE_CAS_FAILED",
                            "error_message": "Runtime readback matched, but current selection state changed before persistence.",
                        })
                    else:
                        result["selection_revision"] = next_revision
                        result["selection_provenance"] = {
                            "decision_id": decision_id,
                            "operation_id": selection_operation_id,
                            "reason_code": reason_code,
                            "origin": safe_origin,
                            "actor_attribution": "caller_supplied_unverified",
                            "selected_at": selected_at,
                        }
                        result["auto_transition"]["correlation_id"] = decision_id
        except TimeoutError as exc:
            result.update({
                "ok": False,
                "applied": False,
                "error_code": "VPN_AUTO_SELECTION_BUSY",
                "error_message": str(exc),
                "retryable": True,
                "selection_outcome": "deferred",
                "auto_transition": {**result["auto_transition"], "outcome": "deferred"},
            })

        if result.get("applied") and post_check:
            post_check_result = check_server_delay(
                selected["server_id"],
                update_state=update_ping_state,
                checked_by=f"selector_post_check:{reason}",
                source="selector",
                timeout_ms=timeout_ms,
            )
            result["post_switch_check"] = post_check_result
            result["post_check_failed_no_rollback"] = post_check_result["ok"] is not True
            result["post_check"].update({
                "status": "passed" if post_check_result["ok"] is True else "failed",
                "result": post_check_result,
                "failed_no_rollback": result["post_check_failed_no_rollback"],
            })

        if result.get("applied") and result.get("selector_readback_matches") and result.get("active_after") == selected["server_id"]:
            write_operational_log(
                event_type="vpn_auto_server_switched",
                message="VPN-auto server was switched.",
                details={
                    "requested_by": requested_by,
                    "management_attribution": attribution,
                    "reason": reason,
                    "active_before": active_before,
                    "active_after": result["active_after"],
                    "selected_server_id": result["selected_server_id"],
                    "selected_server_name": result["selected_server_name"],
                    "selected_ping": result["selected_ping"],
                    "selection_basis": selection_basis,
                    "post_check_failed_no_rollback": result["post_check_failed_no_rollback"],
                    "previous_value": {"server_label": _server_event_label(active_before)},
                    "new_value": {"server_label": safe_human_label(selected.get("server_name"), entity_id=selected.get("server_id"))},
                    "reason_code": _selector_reason_code(reason, origin=origin),
                    "correlation_id": (result.get("selection_provenance") or {}).get("decision_id"),
                    "origin": _safe_selection_origin(origin),
                    "result": "success",
                    "source": "selector",
                },
            )

    return result
