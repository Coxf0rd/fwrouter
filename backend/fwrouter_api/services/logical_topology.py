from __future__ import annotations

import json
import hashlib
from datetime import datetime, timezone
from typing import Any

from fwrouter_api.db.connection import db_session
from fwrouter_api.services.runtime_adapters import (
    RUNTIME_CAPABILITY_LOGICAL_GROUP_PROBE,
    RUNTIME_CAPABILITY_LOGICAL_GROUP_PROBE_MANY,
    RUNTIME_CAPABILITY_LOGICAL_GROUP_STATE,
    RUNTIME_CAPABILITY_LOGICAL_GROUP_STATE_MANY,
    RUNTIME_CAPABILITY_LOGICAL_MEMBER_PROBE,
    RUNTIME_ROLE_VPN_DATAPLANE,
    active_runtime_adapter,
    runtime_adapter_operations,
)


PROVIDER_ROLE_VPN_DATAPLANE = "vpn_dataplane"
DEFAULT_STALE_TTL_SECONDS = 1800


def _runtime_context() -> tuple[dict[str, Any], Any | None, set[str]]:
    adapter = active_runtime_adapter(RUNTIME_ROLE_VPN_DATAPLANE)
    operations = runtime_adapter_operations(adapter)
    capabilities = {str(item) for item in adapter.get("capabilities") or []}
    return adapter, operations, capabilities


def _runtime_group_state_available(
    operations: Any | None,
    capabilities: set[str],
) -> bool:
    return (
        RUNTIME_CAPABILITY_LOGICAL_GROUP_STATE in capabilities
        and callable(getattr(operations, "get_logical_group_state", None))
    )


def _native_health_available(
    operations: Any | None,
    capabilities: set[str],
) -> bool:
    return _runtime_group_state_available(operations, capabilities) and (
        (
            RUNTIME_CAPABILITY_LOGICAL_GROUP_PROBE in capabilities
            and callable(getattr(operations, "probe_logical_group", None))
        )
        or (
            RUNTIME_CAPABILITY_LOGICAL_MEMBER_PROBE in capabilities
            and callable(getattr(operations, "probe_logical_member", None))
        )
    )


def _canonical_timestamp(value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _runtime_failure_snapshot(
    logical_runtime_target: str,
    *,
    error_code: str,
    error_message: str,
) -> dict[str, Any]:
    return {
        "ok": False,
        "logical_runtime_target": logical_runtime_target,
        "effective_member_runtime_identity": None,
        "observed_at": _utc_timestamp(),
        "evidence_source": "runtime_native",
        "members": [],
        "error_code": error_code,
        "error_message": error_message,
    }


def sync_logical_topology(connection: Any, servers: list[Any]) -> None:
    for server in servers:
        raw = getattr(server, "raw", {}) if not isinstance(server, dict) else server.get("raw", {})
        if not isinstance(raw, dict):
            continue
        logical_server_id = str(getattr(server, "server_id", "") if not isinstance(server, dict) else server.get("server_id", ""))
        if not logical_server_id:
            continue
        topology = raw.get("_fwrouter_topology") if isinstance(raw.get("_fwrouter_topology"), dict) else {}
        endpoints = topology.get("endpoints") if isinstance(topology.get("endpoints"), list) else []
        kind = "structured_profile" if topology.get("kind") == "logical_profile" else "concrete_single"
        source_semantic = str(topology.get("source_semantic") or "unspecified")
        policy = "source_defined" if kind == "structured_profile" and source_semantic != "unspecified" else ("fallback" if kind == "structured_profile" and len(endpoints) > 1 else "single")
        connection.execute(
            """
            INSERT INTO logical_server_topology (logical_server_id, topology_kind, selection_policy, source_metadata_json)
            VALUES (?, ?, ?, json(?))
            ON CONFLICT(logical_server_id) DO UPDATE SET topology_kind = excluded.topology_kind,
              selection_policy = excluded.selection_policy, source_metadata_json = excluded.source_metadata_json,
              last_seen_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
            """,
            (logical_server_id, kind, policy, json.dumps({"parser_topology": True, "source_semantic": source_semantic, "runtime_policy": topology.get("runtime_policy") or policy}, ensure_ascii=False)),
        )
        members: list[tuple[str, str, dict[str, Any], int]] = []
        logical_name = str(raw.get("_fwrouter_runtime_name") or raw.get("name") or logical_server_id)
        for index, endpoint in enumerate(endpoints):
            if not isinstance(endpoint, dict) or not isinstance(endpoint.get("runtime"), dict):
                continue
            member_id = str(endpoint.get("identity") or f"{logical_server_id}:{index}")
            members.append((member_id, f"{logical_name} :: {member_id.removeprefix('sub:')[:12] or index + 1}", endpoint["runtime"], index))
        if not members:
            members.append((logical_server_id, logical_name, raw, 0))
        member_ids = {member[0] for member in members}
        for member_id, runtime_name, config, member_order in members:
            encoded = json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            connection.execute(
                """
                INSERT INTO logical_server_members (logical_server_id, member_id, member_runtime_name, member_config_json, transport_fingerprint, member_order, is_active)
                VALUES (?, ?, ?, json(?), ?, ?, 1)
                ON CONFLICT(logical_server_id, member_id) DO UPDATE SET member_runtime_name = excluded.member_runtime_name,
                  member_config_json = excluded.member_config_json, transport_fingerprint = excluded.transport_fingerprint,
                  member_order = excluded.member_order, is_active = 1, last_seen_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                """,
                (logical_server_id, member_id, runtime_name, encoded, hashlib.sha256(encoded.encode()).hexdigest(), member_order),
            )
        placeholders = ", ".join("?" for _ in member_ids)
        connection.execute(f"UPDATE logical_server_members SET is_active = 0, updated_at = CURRENT_TIMESTAMP WHERE logical_server_id = ? AND member_id NOT IN ({placeholders})", (logical_server_id, *sorted(member_ids)))


def _json(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    try:
        result = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return result if isinstance(result, dict) else {}


def _member_status(row: Any) -> str:
    status = str(row["status"] or "unknown")
    if status in {"healthy", "failed"} and bool(row["is_stale"]):
        return "stale"
    if status in {"unknown", "healthy", "failed", "stale", "unsupported"}:
        return status
    return "unknown"


def _logical_runtime_name(connection: Any, logical_server_id: str) -> str:
    row = connection.execute(
        """
        SELECT COALESCE(
            json_extract(s.raw_json, '$._fwrouter_runtime_name'),
            json_extract(s.raw_json, '$.name'),
            s.server_name,
            s.server_id
        ) AS runtime_name
        FROM servers s
        WHERE s.server_id = ?
        """,
        (logical_server_id,),
    ).fetchone()
    if row is None:
        return logical_server_id
    return str(row["runtime_name"] or logical_server_id)


def _persist_member_health(
    connection: Any,
    *,
    logical_server_id: str,
    member_id: str,
    status: str,
    latency_ms: int | None,
    error_code: str | None,
    error_message: str | None,
    evidence: dict[str, Any],
    checked_at: str | None = None,
) -> None:
    checked_at_value = _canonical_timestamp(checked_at)
    connection.execute(
        """
        INSERT INTO logical_server_member_health (logical_server_id, member_id, provider_role, status, latency_ms, checked_at, error_code, error_message, evidence_json, consecutive_failures)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, json(?), ?)
        ON CONFLICT(logical_server_id, member_id, provider_role) DO UPDATE SET
          status = excluded.status, latency_ms = excluded.latency_ms, checked_at = excluded.checked_at,
          error_code = excluded.error_code, error_message = excluded.error_message, evidence_json = excluded.evidence_json,
          consecutive_failures = CASE
            WHEN excluded.status = 'healthy' THEN 0
            WHEN excluded.status = 'failed' THEN logical_server_member_health.consecutive_failures + 1
            ELSE logical_server_member_health.consecutive_failures
          END
        WHERE logical_server_member_health.checked_at IS NULL
           OR (excluded.checked_at IS NOT NULL AND excluded.checked_at >= logical_server_member_health.checked_at)
        """,
        (
            logical_server_id,
            member_id,
            PROVIDER_ROLE_VPN_DATAPLANE,
            status,
            latency_ms,
            checked_at_value,
            error_code,
            error_message,
            json.dumps(evidence, ensure_ascii=False, sort_keys=True),
            1 if status == "failed" else 0,
        ),
    )


def _runtime_snapshot(
    logical_server_id: str,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    adapter, operations, capabilities = _runtime_context()
    if not _native_health_available(operations, capabilities):
        return adapter, None
    with db_session() as connection:
        logical_runtime_name = _logical_runtime_name(connection, logical_server_id)
    try:
        snapshot = operations.get_logical_group_state(logical_runtime_name)
    except Exception as exc:
        snapshot = {
            "ok": False,
            "logical_runtime_target": logical_runtime_name,
            "members": [],
            "error_code": "RUNTIME_LOGICAL_STATE_UNAVAILABLE",
            "error_message": str(exc),
        }
    return adapter, snapshot if isinstance(snapshot, dict) else None


def _import_runtime_snapshot(
    logical_server_id: str,
    snapshot: dict[str, Any],
    *,
    adapter: dict[str, Any],
    probe_reason: str,
    probe_lane: str,
    update_active: bool = True,
) -> dict[str, Any]:
    topology = get_logical_topology(logical_server_id)
    if topology is None:
        return {"ok": False, "error_code": "LOGICAL_SERVER_NOT_FOUND"}
    by_runtime = {
        str(member["runtime_name"]): member
        for member in topology["members"]
        if member["is_active"]
    }
    effective_runtime = str(snapshot.get("effective_member_runtime_identity") or "").strip()
    effective_member = by_runtime.get(effective_runtime)
    imported = 0
    with db_session() as connection:
        for observation in snapshot.get("members") or []:
            if not isinstance(observation, dict):
                continue
            runtime_identity = str(observation.get("runtime_identity") or "").strip()
            member = by_runtime.get(runtime_identity)
            if member is None:
                continue
            status = str(observation.get("status") or "unknown").strip().lower()
            if status not in {"unknown", "healthy", "failed", "stale", "unsupported"}:
                status = "unknown"
            checked_at = _canonical_timestamp(observation.get("checked_at"))
            if status in {"healthy", "failed"} and checked_at is None:
                status = "unknown"
            _persist_member_health(
                connection,
                logical_server_id=logical_server_id,
                member_id=str(member["member_id"]),
                status=status,
                latency_ms=observation.get("latency_ms") if isinstance(observation.get("latency_ms"), int) else None,
                error_code=observation.get("error_code"),
                error_message=observation.get("error_message"),
                checked_at=checked_at,
                evidence={
                    "adapter_id": adapter.get("adapter_id"),
                    "provider_role": PROVIDER_ROLE_VPN_DATAPLANE,
                    "evidence_source": snapshot.get("evidence_source") or "runtime_native",
                    "probe_reason": probe_reason,
                    "probe_lane": probe_lane,
                    "logical_runtime_target": snapshot.get("logical_runtime_target"),
                    "runtime_identity": runtime_identity,
                    "snapshot_observed_at": snapshot.get("observed_at"),
                    "snapshot_id": snapshot.get("snapshot_id"),
                },
            )
            imported += 1
        if update_active and effective_member is not None:
            connection.execute(
                "UPDATE logical_server_topology SET active_member_id = ?, updated_at = CURRENT_TIMESTAMP WHERE logical_server_id = ?",
                (str(effective_member["member_id"]), logical_server_id),
            )
    return {
        "ok": bool(snapshot.get("ok")),
        "logical_server_id": logical_server_id,
        "logical_runtime_target": snapshot.get("logical_runtime_target"),
        "member_id": str(effective_member["member_id"]) if effective_member else None,
        "member_runtime_name": effective_runtime or None,
        "observed_at": snapshot.get("observed_at"),
        "evidence_source": snapshot.get("evidence_source") or "runtime_native",
        "imported": imported,
        "error_code": snapshot.get("error_code"),
        "error_message": snapshot.get("error_message"),
    }


def get_logical_topology(logical_server_id: str) -> dict[str, Any] | None:
    with db_session() as connection:
        topology = connection.execute(
            "SELECT logical_server_id, topology_kind, selection_policy, active_member_id FROM logical_server_topology WHERE logical_server_id = ?",
            (logical_server_id,),
        ).fetchone()
        if topology is None:
            return None
        members = connection.execute(
            """
            SELECT m.member_id, m.member_runtime_name, m.member_order, m.is_active,
                   h.status, h.latency_ms, h.checked_at, h.error_code, h.error_message,
                   CASE
                     WHEN h.checked_at IS NOT NULL AND h.checked_at <= datetime('now', ?) THEN 1
                     ELSE 0
                   END AS is_stale
            FROM logical_server_members m
            LEFT JOIN logical_server_member_health h
              ON h.logical_server_id = m.logical_server_id
             AND h.member_id = m.member_id
             AND h.provider_role = ?
            WHERE m.logical_server_id = ?
            ORDER BY m.member_order, m.member_id
            """,
            (f"-{DEFAULT_STALE_TTL_SECONDS} seconds", PROVIDER_ROLE_VPN_DATAPLANE, logical_server_id),
        ).fetchall()
    active = [row for row in members if bool(row["is_active"])]
    usable = [row for row in active if _member_status(row) == "healthy"]
    active_member_id = topology["active_member_id"] or (usable[0]["member_id"] if usable else None)
    status = "usable" if usable else ("unavailable" if active and all(_member_status(row) == "failed" for row in active) else "unknown")
    return {
        "logical_server_id": topology["logical_server_id"],
        "topology_kind": topology["topology_kind"],
        "selection_policy": topology["selection_policy"],
        "active_member_id": active_member_id,
        "health": {"status": status, "usable_members": len(usable), "total_members": len(active)},
        "members": [
            {
                "member_id": row["member_id"],
                "runtime_name": row["member_runtime_name"],
                "member_order": row["member_order"],
                "is_active": bool(row["is_active"]),
                "is_effective_active": row["member_id"] == active_member_id,
                "presentation_index": int(row["member_order"]) + 1,
                "status": _member_status(row),
                "fresh": _member_status(row) in {"healthy", "failed"},
                "stale": _member_status(row) == "stale",
                "latency_ms": row["latency_ms"],
                "checked_at": row["checked_at"],
                "error_code": row["error_code"],
                "error_message": row["error_message"],
            }
            for row in members
        ],
    }


def get_runtime_logical_topology(logical_server_id: str) -> dict[str, Any] | None:
    topology = get_logical_topology(logical_server_id)
    if topology is None:
        return None
    active_members = [member for member in topology["members"] if member["is_active"]]
    observation = observe_active_member(logical_server_id, update_state=False)
    effective_member_id = str(observation.get("member_id") or "") if observation.get("ok") else ""
    effective_member = next(
        (member for member in active_members if member["member_id"] == effective_member_id),
        None,
    )
    topology["active_member_id"] = effective_member_id or None
    topology["active_member_source"] = observation.get("source") if observation.get("ok") else "runtime_unavailable"
    topology["runtime_observation_ok"] = bool(observation.get("ok"))
    topology["effective_latency_ms"] = (
        effective_member.get("latency_ms")
        if effective_member and effective_member.get("fresh") and effective_member.get("status") == "healthy"
        else None
    )
    for member in topology["members"]:
        member["is_effective_active"] = member["member_id"] == effective_member_id
    return topology


def observe_active_member(logical_server_id: str, *, update_state: bool = True) -> dict[str, Any]:
    topology = get_logical_topology(logical_server_id)
    if topology is None:
        return {"ok": False, "logical_server_id": logical_server_id, "error_code": "LOGICAL_SERVER_NOT_FOUND"}
    active_members = [member for member in topology["members"] if member["is_active"]]
    if len(active_members) == 1:
        member = active_members[0]
        if update_state and topology.get("active_member_id") != member["member_id"]:
            with db_session() as connection:
                connection.execute(
                    "UPDATE logical_server_topology SET active_member_id = ?, updated_at = CURRENT_TIMESTAMP WHERE logical_server_id = ?",
                    (member["member_id"], logical_server_id),
                )
        return {
            "ok": True,
            "logical_server_id": logical_server_id,
            "member_id": member["member_id"],
            "member_runtime_name": member["runtime_name"],
            "runtime_name": member["runtime_name"],
            "source": "single_member",
        }
    adapter, operations, capabilities = _runtime_context()
    with db_session() as connection:
        logical_runtime_name = _logical_runtime_name(connection, logical_server_id)
    if _runtime_group_state_available(operations, capabilities):
        try:
            snapshot = operations.get_logical_group_state(logical_runtime_name)
        except Exception as exc:
            return {
                "ok": False,
                "logical_server_id": logical_server_id,
                "runtime_name": logical_runtime_name,
                "error_code": "RUNTIME_LOGICAL_STATE_UNAVAILABLE",
                "error_message": str(exc),
            }
        selected = str(snapshot.get("effective_member_runtime_identity") or "").strip()
        source = str(snapshot.get("evidence_source") or "runtime_native")
        observed_at = snapshot.get("observed_at")
    elif callable(getattr(operations, "get_proxy_state", None)):
        try:
            proxy_state = operations.get_proxy_state(logical_runtime_name)
        except Exception as exc:
            return {
                "ok": False,
                "logical_server_id": logical_server_id,
                "runtime_name": logical_runtime_name,
                "error_code": "RUNTIME_PROXY_STATE_UNAVAILABLE",
                "error_message": str(exc),
            }
        selected = str(proxy_state.get("now") or "").strip()
        source = "runtime_state"
        observed_at = _utc_timestamp()
    else:
        return {
            "ok": False,
            "logical_server_id": logical_server_id,
            "runtime_name": logical_runtime_name,
            "error_code": "RUNTIME_PROXY_STATE_UNAVAILABLE",
            "error_message": "Active runtime adapter does not expose logical group state.",
        }
    member = next((item for item in active_members if item["runtime_name"] == selected), None)
    if member is None:
        return {
            "ok": False,
            "logical_server_id": logical_server_id,
            "runtime_name": logical_runtime_name,
            "selected_runtime_name": selected or None,
            "error_code": "LOGICAL_ACTIVE_MEMBER_UNKNOWN",
            "error_message": "Mihomo selected member is not in logical topology.",
        }
    if update_state and topology.get("active_member_id") != member["member_id"]:
        with db_session() as connection:
            connection.execute(
                "UPDATE logical_server_topology SET active_member_id = ?, updated_at = CURRENT_TIMESTAMP WHERE logical_server_id = ?",
                (member["member_id"], logical_server_id),
            )
    return {
        "ok": True,
        "logical_server_id": logical_server_id,
        "member_id": member["member_id"],
        "member_runtime_name": member["runtime_name"],
        "runtime_name": logical_runtime_name,
        "selected_runtime_name": selected,
        "source": source,
        "observed_at": observed_at,
        "runtime_adapter_id": adapter.get("adapter_id"),
    }


def observe_effective_members() -> dict[str, Any]:
    with db_session() as connection:
        logical_rows = [
            (
                str(row["logical_server_id"]),
                _logical_runtime_name(connection, str(row["logical_server_id"])),
            )
            for row in connection.execute(
                """
                SELECT m.logical_server_id
                FROM logical_server_members m
                JOIN servers s ON s.server_id = m.logical_server_id
                WHERE m.is_active = 1
                  AND s.inventory_state = 'active'
                GROUP BY m.logical_server_id
                ORDER BY m.logical_server_id
                """
            ).fetchall()
        ]
    results: list[dict[str, Any]] = []
    adapter, operations, capabilities = _runtime_context()
    if (
        _native_health_available(operations, capabilities)
        and RUNTIME_CAPABILITY_LOGICAL_GROUP_STATE_MANY in capabilities
        and callable(getattr(operations, "get_logical_groups_state", None))
    ):
        try:
            snapshots = operations.get_logical_groups_state(
                [runtime_name for _, runtime_name in logical_rows]
            )
        except Exception as exc:
            snapshots = [
                _runtime_failure_snapshot(
                    runtime_name,
                    error_code="RUNTIME_LOGICAL_STATE_UNAVAILABLE",
                    error_message=str(exc),
                )
                for _, runtime_name in logical_rows
            ]
        snapshots_by_target = {
            str(snapshot.get("logical_runtime_target") or ""): snapshot
            for snapshot in snapshots
            if isinstance(snapshot, dict)
        }
        for logical_server_id, runtime_name in logical_rows:
            snapshot = snapshots_by_target.get(runtime_name)
            if snapshot is None:
                results.append(
                    {
                        "ok": False,
                        "logical_server_id": logical_server_id,
                        "error_code": "RUNTIME_LOGICAL_STATE_UNAVAILABLE",
                    }
                )
                continue
            results.append(
                _import_runtime_snapshot(
                    logical_server_id,
                    snapshot,
                    adapter=adapter,
                    probe_reason="background_observation",
                    probe_lane="background",
                )
            )
    else:
        for logical_server_id, _runtime_name in logical_rows:
            item_adapter, snapshot = _runtime_snapshot(logical_server_id)
            if snapshot is not None:
                results.append(
                    _import_runtime_snapshot(
                        logical_server_id,
                        snapshot,
                        adapter=item_adapter,
                        probe_reason="background_observation",
                        probe_lane="background",
                    )
                )
            else:
                results.append(observe_active_member(logical_server_id, update_state=True))
    return {
        "observed": len(results),
        "mapped": sum(1 for result in results if result.get("ok")),
        "results": results,
    }


def check_logical_server_delay(
    logical_server_id: str,
    *,
    test_url: str = "https://www.gstatic.com/generate_204",
    timeout_ms: int = 10000,
    probe_reason: str = "logical_ping",
    probe_lane: str = "manual",
) -> dict[str, Any]:
    topology = get_logical_topology(logical_server_id)
    if topology is None:
        return {"ok": False, "logical_server_id": logical_server_id, "error_code": "LOGICAL_SERVER_NOT_FOUND"}
    active_members = [member for member in topology["members"] if member["is_active"]]
    adapter, operations, capabilities = _runtime_context()
    with db_session() as connection:
        logical_runtime_name = _logical_runtime_name(connection, logical_server_id)
    if _native_health_available(operations, capabilities):
        try:
            if (
                len(active_members) > 1
                and RUNTIME_CAPABILITY_LOGICAL_GROUP_PROBE in capabilities
                and callable(getattr(operations, "probe_logical_group", None))
            ):
                snapshot = operations.probe_logical_group(
                    logical_runtime_name,
                    test_url=test_url,
                    timeout_ms=timeout_ms,
                )
            elif (
                RUNTIME_CAPABILITY_LOGICAL_MEMBER_PROBE in capabilities
                and callable(getattr(operations, "probe_logical_member", None))
            ):
                observation = observe_active_member(logical_server_id, update_state=False)
                if not observation.get("ok"):
                    return {
                        "ok": False,
                        "logical_server_id": logical_server_id,
                        "status": "failed",
                        "latency_ms": None,
                        "member_id": None,
                        "member_runtime_name": None,
                        "error_code": observation.get("error_code"),
                        "error_message": observation.get("error_message"),
                        "observation": observation,
                    }
                snapshot = operations.probe_logical_member(
                    logical_runtime_name,
                    str(observation["member_runtime_name"]),
                    test_url=test_url,
                    timeout_ms=timeout_ms,
                )
            else:
                snapshot = operations.get_logical_group_state(logical_runtime_name)
        except Exception as exc:
            snapshot = _runtime_failure_snapshot(
                logical_runtime_name,
                error_code="RUNTIME_LOGICAL_PROBE_FAILED",
                error_message=str(exc),
            )
        imported = _import_runtime_snapshot(
            logical_server_id,
            snapshot,
            adapter=adapter,
            probe_reason=probe_reason,
            probe_lane=probe_lane,
        )
        refreshed = get_logical_topology(logical_server_id)
        effective = next(
            (
                member
                for member in (refreshed or {}).get("members", [])
                if member.get("member_id") == imported.get("member_id") and member.get("is_active")
            ),
            None,
        )
        ok = bool(
            snapshot.get("ok")
            and effective
            and effective.get("fresh")
            and effective.get("status") == "healthy"
        )
        return {
            "ok": ok,
            "logical_server_id": logical_server_id,
            "status": "success" if ok else "failed",
            "latency_ms": effective.get("latency_ms") if ok and effective else None,
            "member_id": imported.get("member_id"),
            "member_runtime_name": imported.get("member_runtime_name"),
            "error_code": None if ok else snapshot.get("error_code") or (effective or {}).get("error_code") or "RUNTIME_LOGICAL_PROBE_FAILED",
            "error_message": None if ok else snapshot.get("error_message") or (effective or {}).get("error_message"),
            "observation": imported,
            "group_delay": None,
            "probe_backend": "runtime_native",
            "runtime_adapter_id": adapter.get("adapter_id"),
        }
    if not callable(getattr(operations, "check_delay", None)):
        return {
            "ok": False,
            "logical_server_id": logical_server_id,
            "status": "failed",
            "latency_ms": None,
            "member_id": None,
            "member_runtime_name": None,
            "error_code": "RUNTIME_LOCAL_PROBE_UNAVAILABLE",
            "error_message": "Active runtime adapter exposes neither native member health nor local delay probes.",
        }
    group_delay = None
    if len(active_members) > 1:
        group_delay = operations.check_delay(logical_runtime_name, test_url=test_url, timeout_ms=timeout_ms)
    observation = observe_active_member(logical_server_id, update_state=True)
    if not observation.get("ok"):
        return {
            "ok": False,
            "logical_server_id": logical_server_id,
            "status": "failed",
            "latency_ms": None,
            "member_id": None,
            "member_runtime_name": None,
            "error_code": observation.get("error_code"),
            "error_message": observation.get("error_message"),
            "observation": observation,
        }
    member_runtime_name = str(observation["member_runtime_name"])
    result = operations.check_delay(member_runtime_name, test_url=test_url, timeout_ms=timeout_ms)
    status = "healthy" if result.ok else "failed"
    evidence = {
        "provider_role": PROVIDER_ROLE_VPN_DATAPLANE,
        "adapter_id": adapter.get("adapter_id"),
        "evidence_source": "local_fallback",
        "probe_reason": probe_reason,
        "probe_lane": probe_lane,
        "runtime_name": member_runtime_name,
        "logical_runtime_name": observation.get("runtime_name"),
    }
    if group_delay is not None:
        evidence["group_delay"] = group_delay.to_dict()
    with db_session() as connection:
        _persist_member_health(
            connection,
            logical_server_id=logical_server_id,
            member_id=str(observation["member_id"]),
            status=status,
            latency_ms=result.delay_ms,
            error_code=result.error_code,
            error_message=result.error_message,
            evidence=evidence,
            checked_at=_utc_timestamp(),
        )
        if result.ok:
            connection.execute(
                "UPDATE logical_server_topology SET active_member_id = ?, updated_at = CURRENT_TIMESTAMP WHERE logical_server_id = ?",
                (str(observation["member_id"]), logical_server_id),
            )
    return {
        "ok": result.ok,
        "logical_server_id": logical_server_id,
        "status": "success" if result.ok else "failed",
        "latency_ms": result.delay_ms,
        "member_id": str(observation["member_id"]),
        "member_runtime_name": member_runtime_name,
        "error_code": result.error_code,
        "error_message": result.error_message,
        "observation": observation,
        "group_delay": group_delay.to_dict() if group_delay is not None else None,
        "probe_backend": "local_fallback",
        "runtime_adapter_id": adapter.get("adapter_id"),
    }


def check_member_delay(
    logical_server_id: str,
    member_id: str,
    *,
    timeout_ms: int = 10000,
    probe_reason: str = "member_ping",
    probe_lane: str = "manual",
) -> dict[str, Any]:
    topology = get_logical_topology(logical_server_id)
    if topology is None:
        return {"ok": False, "error_code": "LOGICAL_SERVER_NOT_FOUND"}
    member = next((item for item in topology["members"] if item["member_id"] == member_id and item["is_active"]), None)
    if member is None:
        return {"ok": False, "error_code": "LOGICAL_MEMBER_NOT_FOUND"}
    adapter, operations, capabilities = _runtime_context()
    with db_session() as connection:
        logical_runtime_name = _logical_runtime_name(connection, logical_server_id)
    if _native_health_available(operations, capabilities):
        try:
            if (
                RUNTIME_CAPABILITY_LOGICAL_MEMBER_PROBE in capabilities
                and callable(getattr(operations, "probe_logical_member", None))
            ):
                snapshot = operations.probe_logical_member(
                    logical_runtime_name,
                    member["runtime_name"],
                    timeout_ms=timeout_ms,
                )
            elif (
                RUNTIME_CAPABILITY_LOGICAL_GROUP_PROBE in capabilities
                and callable(getattr(operations, "probe_logical_group", None))
            ):
                snapshot = operations.probe_logical_group(
                    logical_runtime_name,
                    timeout_ms=timeout_ms,
                )
            else:
                snapshot = operations.get_logical_group_state(logical_runtime_name)
        except Exception as exc:
            snapshot = _runtime_failure_snapshot(
                logical_runtime_name,
                error_code="RUNTIME_LOGICAL_MEMBER_PROBE_FAILED",
                error_message=str(exc),
            )
        _import_runtime_snapshot(
            logical_server_id,
            snapshot,
            adapter=adapter,
            probe_reason=probe_reason,
            probe_lane=probe_lane,
            update_active=True,
        )
        refreshed = get_logical_topology(logical_server_id)
        current = next(
            (item for item in (refreshed or {}).get("members", []) if item.get("member_id") == member_id),
            None,
        )
        ok = bool(
            snapshot.get("ok")
            and current
            and current.get("fresh")
            and current.get("status") == "healthy"
        )
        return {
            "ok": ok,
            "logical_server_id": logical_server_id,
            "member_id": member_id,
            "status": "healthy" if ok else str((current or {}).get("status") or "unknown"),
            "latency_ms": current.get("latency_ms") if ok and current else None,
            "error_code": None if ok else snapshot.get("error_code") or (current or {}).get("error_code"),
            "error_message": None if ok else snapshot.get("error_message") or (current or {}).get("error_message"),
            "probe_backend": "runtime_native",
            "runtime_adapter_id": adapter.get("adapter_id"),
        }
    if not callable(getattr(operations, "check_delay", None)):
        return {"ok": False, "error_code": "RUNTIME_LOCAL_PROBE_UNAVAILABLE"}
    result = operations.check_delay(member["runtime_name"], timeout_ms=timeout_ms)
    status = "healthy" if result.ok else "failed"
    with db_session() as connection:
        _persist_member_health(
            connection,
            logical_server_id=logical_server_id,
            member_id=member_id,
            status=status,
            latency_ms=result.delay_ms,
            error_code=result.error_code,
            error_message=result.error_message,
            evidence={
                "provider_role": PROVIDER_ROLE_VPN_DATAPLANE,
                "adapter_id": adapter.get("adapter_id"),
                "evidence_source": "local_fallback",
                "probe_reason": probe_reason,
                "probe_lane": probe_lane,
                "runtime_identity": member["runtime_name"],
            },
            checked_at=_utc_timestamp(),
        )
    return {"ok": result.ok, "logical_server_id": logical_server_id, "member_id": member_id, "status": status, "latency_ms": result.delay_ms, "error_code": result.error_code, "error_message": result.error_message, "probe_backend": "local_fallback", "runtime_adapter_id": adapter.get("adapter_id")}


def _rotate_probe_rows(rows: list[Any], cursor: tuple[str, str]) -> list[Any]:
    return [row for row in rows if (str(row["logical_server_id"]), str(row["member_id"])) > cursor] + [
        row for row in rows if (str(row["logical_server_id"]), str(row["member_id"])) <= cursor
    ]


def probe_members(*, budget: int = 12, timeout_ms: int = 5000, healthy_ttl_seconds: int = 1800, failed_ttl_seconds: int = 120) -> dict[str, Any]:
    safe_budget = max(1, min(int(budget), 20))
    observations = observe_effective_members()
    with db_session() as connection:
        rows = connection.execute(
            """
            SELECT m.logical_server_id, m.member_id, m.member_order,
                   t.active_member_id, h.status, h.checked_at
            FROM logical_server_members m
            JOIN logical_server_topology t ON t.logical_server_id = m.logical_server_id
            JOIN servers s ON s.server_id = m.logical_server_id
            LEFT JOIN logical_server_member_health h
              ON h.logical_server_id = m.logical_server_id AND h.member_id = m.member_id AND h.provider_role = ?
            WHERE m.is_active = 1
              AND s.inventory_state = 'active'
              AND (
                  h.checked_at IS NULL
                  OR (h.status = 'healthy' AND h.checked_at <= datetime('now', ?))
                  OR (h.status != 'healthy' AND h.checked_at <= datetime('now', ?))
              )
            ORDER BY m.logical_server_id, m.member_order, m.member_id
            """,
            (PROVIDER_ROLE_VPN_DATAPLANE, f"-{max(1, int(healthy_ttl_seconds))} seconds", f"-{max(1, int(failed_ttl_seconds))} seconds"),
        ).fetchall()
        state = connection.execute(
            "SELECT cursor_logical_server_id, cursor_member_id FROM logical_server_probe_state WHERE id = 1"
        ).fetchone()
        cursor = (str(state["cursor_logical_server_id"] or ""), str(state["cursor_member_id"] or "")) if state else ("", "")
    active_rows = _rotate_probe_rows(
        [row for row in rows if row["active_member_id"] == row["member_id"]],
        cursor,
    )
    selected_rows = active_rows[: max(1, safe_budget // 3)]
    selected_keys = {
        (str(row["logical_server_id"]), str(row["member_id"])) for row in selected_rows
    }
    remaining = [
        row
        for row in rows
        if (str(row["logical_server_id"]), str(row["member_id"])) not in selected_keys
    ]
    categories = [
        _rotate_probe_rows([row for row in remaining if row["checked_at"] is None], cursor),
        _rotate_probe_rows([row for row in remaining if row["checked_at"] is not None and row["status"] != "healthy"], cursor),
        _rotate_probe_rows([row for row in remaining if row["checked_at"] is not None and row["status"] == "healthy"], cursor),
    ]
    while len(selected_rows) < safe_budget and any(categories):
        for category in categories:
            if category and len(selected_rows) < safe_budget:
                selected_rows.append(category.pop(0))
    selected = [
        (str(row["logical_server_id"]), str(row["member_id"])) for row in selected_rows
    ]
    adapter, operations, capabilities = _runtime_context()
    probe_backend = (
        "runtime_native"
        if _native_health_available(operations, capabilities)
        else "local_fallback"
    )
    if (
        probe_backend == "runtime_native"
        and RUNTIME_CAPABILITY_LOGICAL_GROUP_PROBE_MANY in capabilities
        and callable(getattr(operations, "probe_logical_groups", None))
    ):
        logical_ids = list(dict.fromkeys(logical_server_id for logical_server_id, _ in selected))
        with db_session() as connection:
            runtime_names = {
                logical_server_id: _logical_runtime_name(connection, logical_server_id)
                for logical_server_id in logical_ids
            }
        try:
            snapshots = operations.probe_logical_groups(
                [runtime_names[logical_server_id] for logical_server_id in logical_ids],
                timeout_ms=timeout_ms,
            )
        except Exception as exc:
            snapshots = [
                _runtime_failure_snapshot(
                    runtime_names[logical_server_id],
                    error_code="RUNTIME_LOGICAL_GROUP_PROBE_FAILED",
                    error_message=str(exc),
                )
                for logical_server_id in logical_ids
            ]
        snapshots_by_target = {
            str(snapshot.get("logical_runtime_target") or ""): snapshot
            for snapshot in snapshots
            if isinstance(snapshot, dict)
        }
        for logical_server_id in logical_ids:
            snapshot = snapshots_by_target.get(runtime_names[logical_server_id])
            if snapshot is not None:
                _import_runtime_snapshot(
                    logical_server_id,
                    snapshot,
                    adapter=adapter,
                    probe_reason="background_member_probe",
                    probe_lane="background",
                )
        results = []
        for logical_server_id, member_id in selected:
            topology = get_logical_topology(logical_server_id)
            snapshot = snapshots_by_target.get(runtime_names[logical_server_id]) or {}
            member = next(
                (item for item in (topology or {}).get("members", []) if item.get("member_id") == member_id),
                None,
            )
            results.append(
                {
                    "ok": bool(snapshot.get("ok") and member and member.get("fresh") and member.get("status") == "healthy"),
                    "logical_server_id": logical_server_id,
                    "member_id": member_id,
                    "status": str((member or {}).get("status") or "unknown"),
                    "latency_ms": (member or {}).get("latency_ms"),
                    "probe_backend": "runtime_native",
                    "runtime_adapter_id": adapter.get("adapter_id"),
                }
            )
    else:
        results = [
            check_member_delay(
                logical_server_id,
                member_id,
                timeout_ms=timeout_ms,
                probe_reason="background_member_probe",
                probe_lane="background",
            )
            for logical_server_id, member_id in selected
        ]
    if selected:
        with db_session() as connection:
            connection.execute(
                """
                INSERT INTO logical_server_probe_state (id, cursor_logical_server_id, cursor_member_id)
                VALUES (1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET cursor_logical_server_id = excluded.cursor_logical_server_id,
                  cursor_member_id = excluded.cursor_member_id, updated_at = CURRENT_TIMESTAMP
                """,
                selected[-1],
            )
    return {
        "ok": all(item["ok"] for item in results),
        "budget": safe_budget,
        "probed": len(results),
        "probe_backend": probe_backend,
        "runtime_adapter_id": adapter.get("adapter_id"),
        "runtime_observations": observations,
        "results": results,
    }
