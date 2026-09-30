from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fwrouter_api.services.logical_topology import check_logical_server_delay, check_logical_server_delays, get_logical_topologies, get_logical_topology, get_runtime_logical_topologies
from fwrouter_api.services.server_inventory import list_servers

DEFAULT_MANUAL_CHECK_TIMEOUT_MS = 10000
_TEST_URL = "https://www.gstatic.com/generate_204"


def _error(code: str, message: str, *, server_id: str) -> dict[str, Any]:
    return {"ok": False, "api_ok": False, "server_id": server_id, "status": "failed", "aggregate": {"status": "failed", "total": 0, "healthy": 0, "failed": 0}, "members": [], "error_code": code, "error_message": message}


def _result_from_topology(server_id: str, topology: dict[str, Any] | None, probe: dict[str, Any]) -> dict[str, Any]:
    if topology is None:
        return _error("LOGICAL_SERVER_NOT_FOUND", f"Logical server not found: {server_id}", server_id=server_id)
    members = []
    for member in topology.get("members", []):
        if not member.get("is_active"):
            continue
        status = str(member.get("status") or "unknown")
        fresh = bool(member.get("fresh")) or member.get("freshness") == "fresh"
        members.append({"member_id": str(member.get("member_id") or ""), "presentation_index": int(member.get("presentation_index") or 0), "status": status, "latency_ms": member.get("latency_ms") if isinstance(member.get("latency_ms"), int) else None, "checked_at": member.get("checked_at"), "freshness": member.get("freshness") or ("fresh" if fresh else "unknown"), "error_code": member.get("error_code"), "error_message": member.get("error_message"), "runtime_identity": member.get("runtime_name")})
    healthy = sum(1 for member in members if member["status"] == "healthy" and member["freshness"] == "fresh")
    failed = len(members) - healthy
    health_status = "success" if members and failed == 0 else ("partial" if healthy else "failed")
    runtime_confirmed = topology.get("runtime_observation_ok") is not False
    runtime_unconfirmed = not runtime_confirmed
    status = "unconfirmed" if runtime_unconfirmed and health_status == "success" else health_status
    reason = str(topology.get("health_reason") or "")
    runtime_error_code = "RUNTIME_NOT_APPLIED" if reason == "runtime_not_applied" else "RUNTIME_READBACK_UNCONFIRMED"
    runtime_message = "Runtime confirms that no active member is applied." if reason == "runtime_not_applied" else "Runtime readback did not confirm the active member."
    successful = status == "success" and runtime_confirmed
    return {"ok": bool(probe.get("ok")) and successful, "api_ok": True, "server_id": server_id, "status": status, "health_status": health_status, "aggregate": {"status": health_status, "total": len(members), "healthy": healthy, "failed": failed}, "members": members, "active_member_id": topology.get("active_member_id"), "runtime_observation_ok": runtime_confirmed, "effective_latency_ms": topology.get("effective_latency_ms") if runtime_confirmed else None, "checked_at": max((str(member.get("checked_at") or "") for member in members), default=None), "error_code": None if successful else (probe.get("error_code") or (runtime_error_code if runtime_unconfirmed else None)), "error_message": None if successful else (probe.get("error_message") or (runtime_message if runtime_unconfirmed else None)), "probe_backend": probe.get("probe_backend")}


def run_manual_check(server_id: str, *, timeout_ms: int = DEFAULT_MANUAL_CHECK_TIMEOUT_MS, checked_by: str = "ui_manual_check") -> dict[str, Any]:
    normalized_id = str(server_id or "").strip()
    topology = get_logical_topology(normalized_id)
    if topology is None:
        return _error("LOGICAL_SERVER_NOT_FOUND", f"Logical server not found: {normalized_id}", server_id=normalized_id)
    if not any(member.get("is_active") for member in topology.get("members", [])):
        return _error("LOGICAL_SERVER_MEMBERS_UNAVAILABLE", "Logical server has no active members.", server_id=normalized_id)
    try:
        probe = check_logical_server_delay(normalized_id, test_url=_TEST_URL, timeout_ms=max(1000, min(int(timeout_ms), 30000)), probe_reason="manual_health_refresh", probe_lane="manual")
    except Exception as exc:
        return _error("RUNTIME_MANUAL_CHECK_FAILED", str(exc), server_id=normalized_id)
    refreshed = get_runtime_logical_topologies([normalized_id]).get(normalized_id)
    return _result_from_topology(normalized_id, refreshed, probe)


def _global_manual_server_ids(scope: str) -> list[str]:
    scope = str(scope or "").strip().lower()
    if scope not in {"admin_all", "user_global", "user_vpn_auto"}:
        raise ValueError("Unsupported manual-check scope.")
    result = []
    for server in list_servers(inventory_state="active", limit=1000, observe_runtime=False):
        server_id = str(server.get("server_id") or "").strip()
        preferences = server.get("preferences") if isinstance(server.get("preferences"), dict) else {}
        if not server_id or server_id.startswith("virtual:") or preferences.get("manually_deleted_at"):
            continue
        if scope == "user_global" and preferences.get("global_list") is False:
            continue
        if scope == "user_vpn_auto" and not (preferences.get("global_list") is not False and preferences.get("vpn_auto")):
            continue
        result.append(server_id)
    return sorted(dict.fromkeys(result))


def _aggregate_global_manual_result(scope: str, results: list[dict[str, Any]], started: datetime) -> dict[str, Any]:
    groups = {"total": len(results), "success": 0, "partial": 0, "failed": 0, "unconfirmed": 0}
    members = {"total": 0, "success": 0, "failed": 0}
    for result in results:
        status = str(result.get("status") or "failed")
        groups[status if status in {"success", "partial", "failed", "unconfirmed"} else "failed"] += 1
        aggregate = result.get("aggregate") if isinstance(result.get("aggregate"), dict) else {}
        members["total"] += int(aggregate.get("total") or 0)
        members["success"] += int(aggregate.get("healthy") or 0)
        members["failed"] += int(aggregate.get("failed") or 0)
    status = "failed" if groups["failed"] and not groups["success"] and not groups["partial"] and not groups["unconfirmed"] else (
        "unconfirmed" if groups["unconfirmed"] and not groups["failed"] and not groups["partial"] else (
            "success" if groups["failed"] == 0 and groups["partial"] == 0 and groups["unconfirmed"] == 0 else (
                "partial" if groups["success"] or groups["partial"] or groups["unconfirmed"] else "failed"
            )
        )
    )
    finished = datetime.now(timezone.utc)
    return {"ok": True, "api_ok": True, "scope": scope, "status": status, "groups": groups, "members": members, "results": results, "checked_at": finished.isoformat(), "duration_ms": max(0, int((finished - started).total_seconds() * 1000))}


def run_global_manual_check(*, scope: str, timeout_ms: int = DEFAULT_MANUAL_CHECK_TIMEOUT_MS, checked_by: str = "ui_manual_check") -> dict[str, Any]:
    server_ids = _global_manual_server_ids(scope)
    topologies = get_logical_topologies(server_ids)
    targets = [server_id for server_id in server_ids if topologies.get(server_id) and any(member.get("is_active") for member in topologies[server_id].get("members", []))]
    if not targets:
        return {"ok": False, "api_ok": False, "scope": scope, "error_code": "MANUAL_CHECK_NO_TARGETS", "error_message": "No logical servers with active members are available."}
    started = datetime.now(timezone.utc)
    safe_timeout = max(1000, min(int(timeout_ms), 30000))
    probes = check_logical_server_delays(targets, test_url=_TEST_URL, timeout_ms=safe_timeout, probe_reason="manual_health_refresh", probe_lane="manual")
    by_id = {str(item.get("logical_server_id")): item for item in (probes or []) if isinstance(item, dict)}
    if probes is None:
        by_id = {server_id: run_manual_check(server_id, timeout_ms=safe_timeout, checked_by=checked_by) for server_id in targets}
    refreshed = get_runtime_logical_topologies(targets)
    results = [_result_from_topology(server_id, refreshed.get(server_id), by_id.get(server_id, {})) for server_id in targets]
    return _aggregate_global_manual_result(scope, results, started)
