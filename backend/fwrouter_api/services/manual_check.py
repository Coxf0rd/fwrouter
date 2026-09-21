from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from fwrouter_api.db.connection import db_session
from fwrouter_api.services.logical_topology import get_logical_topologies, get_logical_topology, get_logical_runtime_name
from fwrouter_api.services.runtime_adapters import (
    RUNTIME_CAPABILITY_LOGICAL_GROUP_PROBE,
    RUNTIME_CAPABILITY_LOGICAL_GROUP_PROBE_MANY,
    RUNTIME_CAPABILITY_LOGICAL_MEMBER_PROBE,
    RUNTIME_ROLE_VPN_DATAPLANE,
    active_runtime_adapter,
    runtime_adapter_operations,
)
from fwrouter_api.services.server_inventory import list_servers


DEFAULT_MANUAL_CHECK_TIMEOUT_MS = 10000
_TEST_URL = "https://www.gstatic.com/generate_204"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _error(code: str, message: str, *, server_id: str, adapter_id: str | None = None) -> dict[str, Any]:
    return {
        "ok": False,
        "api_ok": False,
        "server_id": server_id,
        "status": "failed",
        "aggregate": {"status": "failed", "total": 0, "healthy": 0, "failed": 0},
        "members": [],
        "error_code": code,
        "error_message": message,
        "runtime_adapter_id": adapter_id,
    }


def _runtime_member_map(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for item in snapshot.get("members") or []:
        if not isinstance(item, dict):
            continue
        identity = str(item.get("runtime_identity") or item.get("member_runtime_identity") or "").strip()
        if identity:
            result[identity] = item
    return result


def _manual_status(item: dict[str, Any]) -> str:
    status = str(item.get("status") or "failed").strip().lower()
    return "success" if status in {"healthy", "success"} else "failed"


def _persist_manual_result(
    server_id: str,
    *,
    status: str,
    latency_ms: int | None,
    checked_at: str,
    checked_by: str,
    error_code: str | None,
    error_message: str | None,
    metadata: dict[str, Any],
) -> None:
    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO server_ping_state (
                server_id, manual_status, manual_ping_ms, manual_checked_at,
                manual_checked_by, manual_error_code, manual_error_message,
                manual_metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, json(?))
            ON CONFLICT(server_id) DO UPDATE SET
                manual_status = excluded.manual_status,
                manual_ping_ms = excluded.manual_ping_ms,
                manual_checked_at = excluded.manual_checked_at,
                manual_checked_by = excluded.manual_checked_by,
                manual_error_code = excluded.manual_error_code,
                manual_error_message = excluded.manual_error_message,
                manual_metadata_json = excluded.manual_metadata_json
            """,
            (
                server_id,
                status,
                latency_ms,
                checked_at,
                checked_by,
                error_code,
                error_message,
                json.dumps(metadata, ensure_ascii=False, sort_keys=True),
            ),
        )


def _complete_manual_result(
    normalized_id: str,
    topology: dict[str, Any],
    snapshot: dict[str, Any],
    *,
    adapter_id: str | None,
    checked_by: str,
    runtime_target: str,
) -> dict[str, Any]:
    active_members = [member for member in topology["members"] if member.get("is_active")]
    mapped: list[dict[str, Any]] = []
    by_runtime = _runtime_member_map(snapshot)
    for member in active_members:
        runtime_identity = str(member["runtime_name"])
        observed = by_runtime.get(runtime_identity) or {
            "status": "failed",
            "error_code": snapshot.get("error_code") or "RUNTIME_MEMBER_CHECK_MISSING",
            "error_message": snapshot.get("error_message") or "Runtime returned no result for member.",
        }
        status = _manual_status(observed)
        mapped.append({
            "member_id": str(member["member_id"]),
            "presentation_index": int(member["presentation_index"]),
            "status": status,
            "latency_ms": observed.get("latency_ms") if isinstance(observed.get("latency_ms"), int) else None,
            "checked_at": observed.get("checked_at") or snapshot.get("observed_at") or _now(),
            "error_code": observed.get("error_code"),
            "error_message": observed.get("error_message"),
            "runtime_identity": runtime_identity,
            "source": snapshot.get("evidence_source") or "runtime_adapter",
            "runtime_adapter_id": adapter_id,
        })
    healthy = sum(1 for item in mapped if item["status"] == "success")
    failed = len(mapped) - healthy
    aggregate_status = "success" if failed == 0 else ("failed" if healthy == 0 else "partial")
    checked_at = str(snapshot.get("observed_at") or _now())
    error_code = None if aggregate_status == "success" else "MANUAL_MEMBER_CHECK_FAILED"
    error_message = None if aggregate_status == "success" else "One or more members failed the manual check."
    metadata = {
        "operation": "manual_check", "runtime_target": runtime_target,
        "runtime_adapter_id": adapter_id, "checked_at": checked_at,
        "aggregate": {"status": aggregate_status, "total": len(mapped), "healthy": healthy, "failed": failed},
        "members": mapped,
    }
    _persist_manual_result(
        normalized_id,
        status="success" if aggregate_status == "success" else "failed",
        latency_ms=mapped[0]["latency_ms"] if len(mapped) == 1 and aggregate_status == "success" else None,
        checked_at=checked_at, checked_by=checked_by,
        error_code=error_code, error_message=error_message, metadata=metadata,
    )
    return {
        "ok": True, "api_ok": True, "server_id": normalized_id,
        "runtime_target": runtime_target, "runtime_adapter_id": adapter_id,
        "status": aggregate_status, "aggregate": metadata["aggregate"],
        "members": mapped, "checked_at": checked_at,
        "error_code": error_code, "error_message": error_message,
    }


def run_manual_check(
    server_id: str,
    *,
    timeout_ms: int = DEFAULT_MANUAL_CHECK_TIMEOUT_MS,
    checked_by: str = "ui_manual_check",
) -> dict[str, Any]:
    normalized_id = str(server_id or "").strip()
    topology = get_logical_topology(normalized_id)
    if topology is None:
        return _error("LOGICAL_SERVER_NOT_FOUND", f"Logical server not found: {normalized_id}", server_id=normalized_id)

    active_members = [member for member in topology["members"] if member.get("is_active")]
    if not active_members:
        return _error("LOGICAL_SERVER_MEMBERS_UNAVAILABLE", "Logical server has no active members.", server_id=normalized_id)

    adapter = active_runtime_adapter(RUNTIME_ROLE_VPN_DATAPLANE)
    adapter_id = str(adapter.get("adapter_id") or "") or None
    operations = runtime_adapter_operations(adapter)
    capabilities = {str(item) for item in adapter.get("capabilities") or []}
    runtime_target = get_logical_runtime_name(normalized_id)
    safe_timeout = max(1000, min(int(timeout_ms), 30000))
    snapshot: dict[str, Any] = {}

    try:
        if (
            RUNTIME_CAPABILITY_LOGICAL_GROUP_PROBE_MANY in capabilities
            and callable(getattr(operations, "probe_logical_groups", None))
        ):
            snapshots = operations.probe_logical_groups(
                [runtime_target], test_url=_TEST_URL, timeout_ms=safe_timeout
            )
            snapshot = next(
                (item for item in snapshots if str(item.get("logical_runtime_target") or "") == runtime_target),
                {},
            ) if isinstance(snapshots, list) else {}
        elif (
            RUNTIME_CAPABILITY_LOGICAL_GROUP_PROBE in capabilities
            and callable(getattr(operations, "probe_logical_group", None))
        ):
            snapshot = operations.probe_logical_group(
                runtime_target, test_url=_TEST_URL, timeout_ms=safe_timeout
            )
        elif (
            RUNTIME_CAPABILITY_LOGICAL_MEMBER_PROBE in capabilities
            and callable(getattr(operations, "probe_logical_member", None))
        ):
            member_results: list[dict[str, Any]] = []
            for member in active_members:
                member_snapshot = operations.probe_logical_member(
                    runtime_target,
                    str(member["runtime_name"]),
                    test_url=_TEST_URL,
                    timeout_ms=safe_timeout,
                )
                found = _runtime_member_map(member_snapshot).get(str(member["runtime_name"]))
                member_results.append(found or {
                    "runtime_identity": str(member["runtime_name"]),
                    "status": "failed",
                    "checked_at": member_snapshot.get("observed_at") if isinstance(member_snapshot, dict) else None,
                    "error_code": member_snapshot.get("error_code") if isinstance(member_snapshot, dict) else "RUNTIME_MEMBER_PROBE_FAILED",
                    "error_message": member_snapshot.get("error_message") if isinstance(member_snapshot, dict) else "Member probe failed.",
                })
            snapshot = {
                "ok": all(_manual_status(item) == "success" for item in member_results),
                "logical_runtime_target": runtime_target,
                "observed_at": _now(),
                "evidence_source": "runtime_native",
                "members": member_results,
            }
        elif callable(getattr(operations, "check_delay", None)):
            member_results = []
            for member in active_members:
                result = operations.check_delay(
                    str(member["runtime_name"]), test_url=_TEST_URL, timeout_ms=safe_timeout
                )
                member_results.append({
                    "runtime_identity": str(member["runtime_name"]),
                    "status": "healthy" if result.ok else "failed",
                    "latency_ms": result.delay_ms,
                    "checked_at": _now(),
                    "error_code": result.error_code,
                    "error_message": result.error_message,
                })
            snapshot = {
                "ok": all(_manual_status(item) == "success" for item in member_results),
                "logical_runtime_target": runtime_target,
                "observed_at": _now(),
                "evidence_source": "runtime_adapter",
                "members": member_results,
            }
        else:
            result = _error(
                "RUNTIME_MANUAL_CHECK_UNSUPPORTED",
                "Active runtime adapter does not support manual member checks.",
                server_id=normalized_id,
                adapter_id=adapter_id,
            )
            _persist_manual_result(
                normalized_id, status="failed", latency_ms=None, checked_at=_now(), checked_by=checked_by,
                error_code=result["error_code"], error_message=result["error_message"], metadata=result,
            )
            return result
    except Exception as exc:
        result = _error("RUNTIME_MANUAL_CHECK_FAILED", str(exc), server_id=normalized_id, adapter_id=adapter_id)
        _persist_manual_result(
            normalized_id, status="failed", latency_ms=None, checked_at=_now(), checked_by=checked_by,
            error_code=result["error_code"], error_message=result["error_message"], metadata=result,
        )
        return result

    return _complete_manual_result(
        normalized_id, topology, snapshot,
        adapter_id=adapter_id, checked_by=checked_by, runtime_target=runtime_target,
    )


def _global_manual_server_ids(scope: str) -> list[str]:
    normalized_scope = str(scope or "").strip().lower()
    if normalized_scope not in {"admin_all", "user_global", "user_vpn_auto"}:
        raise ValueError("Unsupported manual-check scope.")
    servers = list_servers(inventory_state="active", limit=1000, observe_runtime=False)
    result: list[str] = []
    for server in servers:
        server_id = str(server.get("server_id") or "").strip()
        preferences = server.get("preferences") if isinstance(server.get("preferences"), dict) else {}
        if not server_id or server_id.startswith("virtual:") or preferences.get("manually_deleted_at"):
            continue
        if normalized_scope == "user_global" and preferences.get("global_list") is False:
            continue
        if normalized_scope == "user_vpn_auto" and not (
            preferences.get("global_list") is not False and preferences.get("vpn_auto")
        ):
            continue
        result.append(server_id)
    return sorted(dict.fromkeys(result))


def _aggregate_global_manual_result(scope: str, results: list[dict[str, Any]], started: datetime) -> dict[str, Any]:
    groups = {"total": len(results), "success": 0, "partial": 0, "failed": 0}
    members = {"total": 0, "success": 0, "failed": 0}
    for result in results:
        status = str(result.get("status") or "failed")
        groups[status if status in {"success", "partial", "failed"} else "failed"] += 1
        aggregate = result.get("aggregate") if isinstance(result.get("aggregate"), dict) else {}
        members["total"] += int(aggregate.get("total") or 0)
        members["success"] += int(aggregate.get("healthy") or 0)
        members["failed"] += int(aggregate.get("failed") or 0)
    status = "success" if groups["failed"] == 0 and groups["partial"] == 0 else (
        "failed" if groups["success"] == 0 and groups["partial"] == 0 else "partial"
    )
    finished = datetime.now(timezone.utc)
    return {
        "ok": True, "api_ok": True, "scope": scope, "status": status,
        "groups": groups, "members": members, "results": results,
        "checked_at": finished.isoformat(),
        "duration_ms": max(0, int((finished - started).total_seconds() * 1000)),
    }


def run_global_manual_check(
    *,
    scope: str,
    timeout_ms: int = DEFAULT_MANUAL_CHECK_TIMEOUT_MS,
    checked_by: str = "ui_manual_check",
) -> dict[str, Any]:
    server_ids = _global_manual_server_ids(scope)
    topologies = get_logical_topologies(server_ids)
    targets = [
        server_id for server_id in server_ids
        if topologies.get(server_id) and any(member.get("is_active") for member in topologies[server_id]["members"])
    ]
    if not targets:
        return {"ok": False, "api_ok": False, "scope": scope, "error_code": "MANUAL_CHECK_NO_TARGETS", "error_message": "No logical servers with active members are available."}

    started = datetime.now(timezone.utc)
    adapter = active_runtime_adapter(RUNTIME_ROLE_VPN_DATAPLANE)
    adapter_id = str(adapter.get("adapter_id") or "") or None
    operations = runtime_adapter_operations(adapter)
    capabilities = {str(item) for item in adapter.get("capabilities") or []}
    safe_timeout = max(1000, min(int(timeout_ms), 30000))
    runtime_targets = [get_logical_runtime_name(server_id) for server_id in targets]
    snapshots: dict[str, dict[str, Any]] = {}
    try:
        if RUNTIME_CAPABILITY_LOGICAL_GROUP_PROBE_MANY in capabilities and callable(getattr(operations, "probe_logical_groups", None)):
            raw = operations.probe_logical_groups(runtime_targets, test_url=_TEST_URL, timeout_ms=safe_timeout)
            if isinstance(raw, list):
                snapshots = {str(item.get("logical_runtime_target") or ""): item for item in raw if isinstance(item, dict)}
        elif (
            RUNTIME_CAPABILITY_LOGICAL_GROUP_PROBE in capabilities
            and callable(getattr(operations, "probe_logical_group", None))
        ):
            raw = [operations.probe_logical_group(target, test_url=_TEST_URL, timeout_ms=safe_timeout) for target in runtime_targets]
            snapshots = {str(item.get("logical_runtime_target") or ""): item for item in raw if isinstance(item, dict)}
        elif callable(getattr(operations, "check_delay", None)):
            results = [run_manual_check(server_id, timeout_ms=safe_timeout, checked_by=checked_by) for server_id in targets]
            return _aggregate_global_manual_result(scope, results, started)
        else:
            snapshots = {
                runtime_target: {
                    "ok": False, "logical_runtime_target": runtime_target, "observed_at": _now(),
                    "evidence_source": "runtime_adapter", "members": [],
                    "error_code": "RUNTIME_MANUAL_CHECK_UNSUPPORTED", "error_message": "Active runtime adapter does not support manual member checks.",
                }
                for runtime_target in runtime_targets
            }
    except Exception as exc:
        snapshots = {
            runtime_target: {
                "ok": False, "logical_runtime_target": runtime_target, "observed_at": _now(),
                "evidence_source": "runtime_adapter", "members": [],
                "error_code": "RUNTIME_MANUAL_CHECK_FAILED", "error_message": str(exc),
            }
            for runtime_target in runtime_targets
        }

    results: list[dict[str, Any]] = []
    for server_id, runtime_target in zip(targets, runtime_targets):
        snapshot = snapshots.get(runtime_target) or {
            "ok": False, "logical_runtime_target": runtime_target, "observed_at": _now(),
            "evidence_source": "runtime_adapter", "members": [],
            "error_code": "RUNTIME_MEMBER_CHECK_MISSING", "error_message": "Runtime returned no result for logical server.",
        }
        results.append(_complete_manual_result(
            server_id, topologies[server_id], snapshot,
            adapter_id=adapter_id, checked_by=checked_by, runtime_target=runtime_target,
        ))
    return _aggregate_global_manual_result(scope, results, started)
