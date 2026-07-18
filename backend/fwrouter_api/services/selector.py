from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from fwrouter_api.adapters.mihomo import DEFAULT_MIHOMO_ADAPTER
from fwrouter_api.db.connection import db_session
from fwrouter_api.services.logs import write_operational_log
from fwrouter_api.services.server_ping import check_server_delay


DEFAULT_ON_DEMAND_LIMIT = 10
DEFAULT_ON_DEMAND_TIMEOUT_MS = 10000
MIN_VPN_AUTO_PRIORITY = -1
MAX_VPN_AUTO_PRIORITY = 5
TRAFFIC_COLLECT_TIMER_PATH = Path("/etc/systemd/system/fwrouter-traffic-collect.timer")
TRAFFIC_COLLECT_SERVICE_PATH = Path("/etc/systemd/system/fwrouter-traffic-collect.service")
TRAFFIC_COLLECT_SCRIPT_PATH = Path("/usr/local/libexec/fwrouter/traffic-collect-api.sh")


def _persist_active_auto_server_id(server_id: str | None) -> None:
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
        if int(candidate.get("vpn_auto_priority") or 0) >= 0
    ]


def get_vpn_auto_state() -> dict[str, Any]:
    from fwrouter_api.services.servers import ensure_routing_global_state
    from fwrouter_api.services.traffic import get_traffic_accounting_state

    routing = dict(ensure_routing_global_state() or {})
    candidates = _load_selector_candidates()
    auto_selectable_candidates = _auto_selectable_candidates(candidates)
    enabled_candidate_ids = [str(candidate["server_id"]) for candidate in candidates]
    enabled_candidate_names = [str(candidate["server_name"]) for candidate in candidates]
    enabled_candidate_target_names = [
        str(candidate.get("server_name") or candidate.get("server_id") or "")
        for candidate in candidates
        if str(candidate.get("server_name") or candidate.get("server_id") or "").strip()
    ]
    auto_selectable_candidate_ids = [
        str(candidate["server_id"]) for candidate in auto_selectable_candidates
    ]
    auto_selectable_candidate_names = [
        str(candidate["server_name"]) for candidate in auto_selectable_candidates
    ]
    auto_selectable_candidate_target_names = [
        str(candidate.get("server_name") or candidate.get("server_id") or "")
        for candidate in auto_selectable_candidates
        if str(candidate.get("server_name") or candidate.get("server_id") or "").strip()
    ]

    health = DEFAULT_MIHOMO_ADAPTER.health()
    runtime_state = getattr(health.runtime_state, "value", str(health.runtime_state))
    details = health.details if isinstance(health.details, dict) else {}
    selectors = details.get("selectors") if isinstance(details.get("selectors"), dict) else {}
    mihomo_vpn_auto_targets = [
        str(target)
        for target in (selectors.get("vpn_auto_targets") or [])
        if str(target or "").strip()
    ]
    mihomo_vpn_global_targets = [
        str(target)
        for target in (selectors.get("vpn_global_targets") or [])
        if str(target or "").strip()
    ]
    mihomo_vpn_auto_server_targets = [
        target for target in mihomo_vpn_auto_targets if target != "DIRECT"
    ]
    fallback_active_server_id = str(getattr(health, "active_server_id", "") or "").strip()
    if not mihomo_vpn_auto_server_targets and fallback_active_server_id:
        mihomo_vpn_auto_server_targets = [fallback_active_server_id]
        if not mihomo_vpn_auto_targets:
            mihomo_vpn_auto_targets = [fallback_active_server_id]

    active_auto_server_id = str(routing.get("active_auto_server_id") or "").strip() or None
    active_auto_server_valid = bool(
        active_auto_server_id
        and active_auto_server_id in auto_selectable_candidate_ids
        and (
            active_auto_server_id in mihomo_vpn_auto_server_targets
            or any(
                candidate_id == active_auto_server_id and candidate_name in mihomo_vpn_auto_server_targets
                for candidate_id, candidate_name in zip(
                    auto_selectable_candidate_ids,
                    auto_selectable_candidate_target_names,
                )
            )
        )
    )
    config_consistent = set(auto_selectable_candidate_target_names).issubset(
        set(mihomo_vpn_auto_server_targets)
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
        problem_code = "mihomo_controller_unreachable"
        recommended_action = "restore_mihomo_runtime"
    elif not enabled_candidate_ids:
        problem_code = "vpn_auto_no_candidates"
        recommended_action = "assign_vpn_auto_candidates"
    elif not auto_selectable_candidate_ids:
        problem_code = "vpn_auto_no_auto_selectable_candidates"
        recommended_action = "set_nonnegative_priority_for_mihomo_auto_candidate"
    elif not config_consistent:
        problem_code = "vpn_auto_candidates_not_in_mihomo_config"
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
        "mihomo_vpn_auto_targets_count": len(mihomo_vpn_auto_targets),
        "mihomo_vpn_auto_targets": mihomo_vpn_auto_targets,
        "mihomo_vpn_global_targets_count": len(mihomo_vpn_global_targets),
        "mihomo_vpn_global_targets": mihomo_vpn_global_targets,
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
        "mihomo_runtime_state": runtime_state,
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

    health = DEFAULT_MIHOMO_ADAPTER.health()
    runtime_state = getattr(health.runtime_state, "value", str(health.runtime_state))
    result: dict[str, Any] = {
        "ok": False,
        "requested_by": requested_by,
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

    if runtime_state != "running":
        result["skipped"] = True
        result["skip_reason"] = "mihomo_controller_unreachable"
        return result

    inventory_ids = {server.server_id for server in DEFAULT_MIHOMO_ADAPTER.list_servers()}
    vpn_auto_restore_required = bool(
        server_mode == "auto"
        and active_auto_server_id
        and active_auto_server_id in inventory_ids
    )
    if server_mode == "auto" and active_auto_server_id and active_auto_server_id not in inventory_ids:
        result["vpn_auto_restore"] = {
            "ok": False,
            "skipped": True,
            "skip_reason": "active_auto_server_not_in_mihomo_inventory",
            "requested_server_id": active_auto_server_id,
        }
    elif vpn_auto_restore_required:
        apply_auto = DEFAULT_MIHOMO_ADAPTER.apply_server_to_selector(
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
    apply_global = DEFAULT_MIHOMO_ADAPTER.apply_server_to_selector(
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
            """
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
                ping.error_code,
                ping.error_message,
                CASE
                    WHEN c.server_id IS NOT NULL THEN s.server_name
                    ELSE s.server_id
                END AS mihomo_target
            FROM servers s
            LEFT JOIN server_preferences sp ON sp.server_id = s.server_id
            LEFT JOIN server_ping_state ping ON ping.server_id = s.server_id
            LEFT JOIN server_custom_https_proxy c ON c.server_id = s.server_id
            WHERE s.inventory_state = 'active'
              AND COALESCE(sp.vpn_auto, 0) = 1
              AND COALESCE(sp.manually_deleted_at, '') = ''
            ORDER BY
                CASE ping.status WHEN 'success' THEN 0 ELSE 1 END,
                CASE WHEN ping.last_ping_ms IS NULL THEN 1 ELSE 0 END,
                ping.last_ping_ms ASC,
                s.server_name ASC
            """
        ).fetchall()

    return [
        {
            "server_id": row["server_id"],
            "server_name": row["server_name"],
            "provider_name": row["provider_name"],
            "inventory_state": row["inventory_state"],
            "mihomo_target": row["mihomo_target"] or row["server_id"],
            "vpn_auto": bool(row["vpn_auto"]) if row["vpn_auto"] is not None else False,
            "vpn_auto_priority": max(
                MIN_VPN_AUTO_PRIORITY,
                min(MAX_VPN_AUTO_PRIORITY, int(row["vpn_auto_priority"] or 0)),
            ),
            "global_list": bool(row["global_list"]) if row["global_list"] is not None else True,
            "ping": {
                "status": row["ping_status"] or "unknown",
                "last_ping_ms": row["last_ping_ms"],
                "checked_at": row["checked_at"],
                "error_code": row["error_code"],
                "error_message": row["error_message"],
            },
        }
        for row in rows
    ]


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
            0 if int(item.get("vpn_auto_priority") or 0) >= 0 else 1,
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
            if candidate.get("server_id") == active_before:
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


def _priority_latency_multiplier(priority: int) -> float:
    normalized = max(0, min(MAX_VPN_AUTO_PRIORITY, int(priority or 0)))
    if normalized <= 0:
        return 0.0
    if normalized == 1:
        return 1.5
    return float(normalized)


def _select_candidate_with_priority(
    candidates: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    auto_selectable = [
        candidate
        for candidate in candidates
        if int(candidate.get("vpn_auto_priority") or 0) >= 0
    ]
    best_by_ping = _select_best_successful_candidate(auto_selectable)
    if best_by_ping is None:
        return None, None

    best_ping_ms = int(best_by_ping["ping"]["last_ping_ms"])
    priority_eligible = [
        candidate
        for candidate in auto_selectable
        if candidate["ping"]["status"] == "success"
        and candidate["ping"]["last_ping_ms"] is not None
        and int(candidate.get("vpn_auto_priority") or 0) > 0
        and int(candidate["ping"]["last_ping_ms"]) <= best_ping_ms * _priority_latency_multiplier(
            int(candidate.get("vpn_auto_priority") or 0)
        )
    ]

    if not priority_eligible:
        return best_by_ping, None

    selected = sorted(
        priority_eligible,
        key=lambda item: (
            -int(item.get("vpn_auto_priority") or 0),
            int(item["ping"]["last_ping_ms"]),
            item["server_name"],
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
    check_on_demand: bool = False,
    update_ping_state: bool = True,
    on_demand_limit: int = DEFAULT_ON_DEMAND_LIMIT,
    timeout_ms: int = DEFAULT_ON_DEMAND_TIMEOUT_MS,
    exclude_active: bool = False,
    post_check: bool = True,
) -> dict[str, Any]:
    """Select a vpn-auto server.

    Default dry-run does not ping servers and uses stored server_ping_state.

    When check_on_demand=True or apply=True, selector checks a bounded list of
    candidates now, chooses the lowest successful latency, and optionally applies
    it through Mihomo. This is the mode for "need to change server".

    If apply=True and post_check=True, selector checks delay for the selected
    server after switching. A failed post-check does not rollback the switch.
    """

    health = DEFAULT_MIHOMO_ADAPTER.health()
    active_before = health.active_server_id
    mihomo_servers = DEFAULT_MIHOMO_ADAPTER.list_servers()
    mihomo_server_ids = {server.server_id for server in mihomo_servers}

    candidates = [
        candidate
        for candidate in _load_selector_candidates()
        if str(candidate.get("mihomo_target") or candidate["server_id"]) in mihomo_server_ids
    ]

    if exclude_active and active_before:
        candidates = [
            candidate
            for candidate in candidates
            if candidate["server_id"] != active_before
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
            active_before=active_before,
            limit=safe_limit,
        )

        on_demand_results = [
            _candidate_with_on_demand_ping(
                candidate,
                checked_by=checked_by,
                update_ping_state=update_ping_state,
                timeout_ms=timeout_ms,
            )
            for candidate in shortlist
        ]

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
        selection_basis = (
            "stored server_ping_state with vpn-auto priority"
            if selected and best_latency_candidate is not None
            else (
                "stored server_ping_state, then known latency, then server name"
                if selected
                else "no active SQLite vpn_auto servers matched Mihomo inventory"
            )
        )

    result: dict[str, Any] = {
        "ok": selected is not None,
        "reason": reason,
        "requested_by": requested_by,
        "apply": apply,
        "check_on_demand": should_check_on_demand,
        "update_ping_state": update_ping_state if should_check_on_demand else False,
        "active_before": active_before,
        "active_after": active_before,
        "exclude_active": exclude_active,
        "selected_server_id": selected["server_id"] if selected else None,
        "selected_server_name": selected["server_name"] if selected else None,
        "candidates_count": len(candidates),
        "auto_selectable_candidates_count": sum(
            1 for candidate in candidates if int(candidate.get("vpn_auto_priority") or 0) >= 0
        ),
        "mihomo_servers_count": len(mihomo_servers),
        "selection_basis": selection_basis,
        "selected_ping": selected["ping"] if selected else None,
        "selected_vpn_auto_priority": int(selected.get("vpn_auto_priority") or 0) if selected else 0,
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
                "selected_ping_ms": selected["ping"]["last_ping_ms"],
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
    }

    if not selected:
        return result

    if apply:
        apply_result = DEFAULT_MIHOMO_ADAPTER.apply_server(
            str(selected.get("mihomo_target") or selected["server_id"])
        )
        result["applied"] = apply_result.ok
        result["apply_result"] = apply_result.to_dict()
        result["active_after"] = selected["server_id"] if apply_result.ok else apply_result.active_server_id
        result["ok"] = apply_result.ok

        if apply_result.ok:
            _persist_active_auto_server_id(str(selected["server_id"]))

        if apply_result.ok and post_check:
            post_check_result = check_server_delay(
                selected["server_id"],
                update_state=update_ping_state,
                checked_by=f"selector_post_check:{reason}",
                timeout_ms=timeout_ms,
            )
            result["post_switch_check"] = post_check_result
            result["post_check_failed_no_rollback"] = post_check_result["ok"] is not True

        if apply_result.ok:
            write_operational_log(
                event_type="vpn_auto_server_switched",
                message="VPN-auto server was switched.",
                details={
                    "requested_by": requested_by,
                    "reason": reason,
                    "active_before": active_before,
                    "active_after": result["active_after"],
                    "selected_server_id": result["selected_server_id"],
                    "selected_server_name": result["selected_server_name"],
                    "selected_ping": result["selected_ping"],
                    "selection_basis": selection_basis,
                    "post_check_failed_no_rollback": result["post_check_failed_no_rollback"],
                },
            )

    return result
