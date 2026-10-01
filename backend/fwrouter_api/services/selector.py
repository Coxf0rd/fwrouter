from __future__ import annotations

from datetime import datetime, timezone
import json
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


def _persist_active_auto_server_id(
    server_id: str | None,
    *,
    provenance: dict[str, Any] | None = None,
) -> None:
    with db_session() as connection:
        connection.execute(
            """
            UPDATE routing_global_state
            SET
                active_auto_server_id = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = 1
            """,
            (server_id,),
        )
        if provenance is not None:
            connection.execute(
                """
                INSERT INTO settings (key, value_json, updated_at)
                VALUES ('routing.auto_selection_provenance', ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET
                    value_json = excluded.value_json,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (json.dumps(provenance, ensure_ascii=False, separators=(",", ":")),),
            )


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
    if not runtime_vpn_auto_server_targets and fallback_active_server_id:
        runtime_vpn_auto_server_targets = [fallback_active_server_id]
        if not runtime_vpn_auto_targets:
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
    active_auto_server_valid = bool(
        active_auto_server_id
        and active_auto_server_id in auto_selectable_candidate_ids
        and _candidate_has_auto_health(active_auto_candidate)
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

    inventory_ids = {server.server_id for server in runtime_operations.list_servers()}
    vpn_auto_restore_required = bool(
        server_mode == "auto"
        and active_auto_server_id
        and active_auto_server_id in inventory_ids
    )
    if server_mode == "auto" and active_auto_server_id and active_auto_server_id not in inventory_ids:
        result["vpn_auto_restore"] = {
            "ok": True,
            "skipped": True,
            "skip_reason": "active_auto_server_not_in_runtime_inventory",
            "requested_server_id": active_auto_server_id,
        }
    elif vpn_auto_restore_required:
        apply_auto = runtime_operations.apply_server_to_selector(
            "vpn-auto",
            active_auto_server_id,
        )
        result["vpn_auto_restore"] = apply_auto.to_dict()
    else:
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
            0 if is_auto_eligible(vpn_auto=item.get("vpn_auto"), vpn_auto_priority=item.get("vpn_auto_priority"), inventory_state=item.get("inventory_state"), manually_deleted_at=item.get("manually_deleted_at")) else 1,
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
        if is_auto_eligible(vpn_auto=candidate.get("vpn_auto"), vpn_auto_priority=candidate.get("vpn_auto_priority"), inventory_state=candidate.get("inventory_state"), manually_deleted_at=candidate.get("manually_deleted_at"))
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
    active_runtime_target = str(health_selectors.get("vpn_auto_now") or "").strip() or None
    effective_runtime_target_before = str(getattr(health, "active_server_id", "") or "").strip() or None
    global_selector_before = str(health_selectors.get("vpn_global_now") or "").strip() or None
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
    candidates = [
        candidate for candidate in ranked_candidates
        if str(candidate.get("runtime_target") or candidate["server_id"]) in runtime_inventory_targets
    ]

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

    if selected is None and candidate_server_id is None:
        from fwrouter_api.services.provider_managed import provider_candidates
        selected, best_latency_candidate = _select_candidate_with_priority(provider_candidates(exclude_active=True))
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
            1 for candidate in candidates if is_auto_eligible(vpn_auto=candidate.get("vpn_auto"), vpn_auto_priority=candidate.get("vpn_auto_priority"), inventory_state=candidate.get("inventory_state"), manually_deleted_at=candidate.get("manually_deleted_at"))
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
        result["ok"] = True
        result["applied"] = False
        result["active_after"] = active_before
        result["active_after_runtime_target"] = active_runtime_target
        result["effective_route"]["changed"] = False
        result["selection_basis"] = "selected server already active"
        result["noop"] = True
        result["noop_reason"] = "selected_server_already_active"
        result["selection_outcome"] = "noop"
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
        apply_result = runtime_operations.apply_server(
            selected_runtime_target
        )
        result["applied"] = apply_result.ok
        result["apply_result"] = apply_result.to_dict()
        apply_details = getattr(apply_result, "details", None)
        apply_details = apply_details if isinstance(apply_details, dict) else {}
        observed_target = str(
            apply_details.get("selector_after")
            or ""
        ).strip() or None
        observed_id, observed_name, observed_identity_status = _runtime_target_identity(
            observed_target, all_candidates
        )
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
        # A successful command response is not proof that selection took effect.
        # Keep `applied` as the physical command result, but make the operation
        # successful only after the selected target is confirmed by readback.
        result["ok"] = result["selection_outcome"] == "selected"
        result["auto_transition"].update({
            "active_after_id": observed_id,
            "active_after_name": observed_name,
            "active_after_runtime_target": observed_target,
            "outcome": result["selection_outcome"],
            "changed": (
                active_runtime_target != observed_target
                if active_runtime_target is not None and observed_target is not None
                else None
            ),
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
                "selected_server_id": str(selected["server_id"]),
                "selected_server_label": selected_label,
                "reason_code": reason_code,
                "origin": safe_origin,
                "actor_attribution": "caller_supplied_unverified",
                "selected_at": selected_at,
            }
            result["selection_provenance"] = {
                "decision_id": decision_id,
                "reason_code": reason_code,
                "origin": safe_origin,
                "actor_attribution": "caller_supplied_unverified",
                "selected_at": selected_at,
            }
            result["auto_transition"]["correlation_id"] = decision_id
            _persist_active_auto_server_id(str(selected["server_id"]), provenance=provenance)

        if apply_result.ok and post_check:
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

        if apply_result.ok and selector_readback_matches and observed_id == selected["server_id"]:
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
