from __future__ import annotations

import json
import hashlib
from typing import Any

from fwrouter_api.adapters.mihomo import DEFAULT_MIHOMO_ADAPTER
from fwrouter_api.db.connection import db_session


PROVIDER_ROLE_VPN_DATAPLANE = "vpn_dataplane"
DEFAULT_STALE_TTL_SECONDS = 1800


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
) -> None:
    connection.execute(
        """
        INSERT INTO logical_server_member_health (logical_server_id, member_id, provider_role, status, latency_ms, checked_at, error_code, error_message, evidence_json, consecutive_failures)
        VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?, ?, json(?), ?)
        ON CONFLICT(logical_server_id, member_id, provider_role) DO UPDATE SET
          status = excluded.status, latency_ms = excluded.latency_ms, checked_at = excluded.checked_at,
          error_code = excluded.error_code, error_message = excluded.error_message, evidence_json = excluded.evidence_json,
          consecutive_failures = CASE WHEN excluded.status = 'healthy' THEN 0 ELSE logical_server_member_health.consecutive_failures + 1 END
        """,
        (
            logical_server_id,
            member_id,
            PROVIDER_ROLE_VPN_DATAPLANE,
            status,
            latency_ms,
            error_code,
            error_message,
            json.dumps(evidence, ensure_ascii=False, sort_keys=True),
            0 if status == "healthy" else 1,
        ),
    )


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
                "status": _member_status(row),
                "latency_ms": row["latency_ms"],
                "checked_at": row["checked_at"],
                "error_code": row["error_code"],
                "error_message": row["error_message"],
            }
            for row in members
        ],
    }


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
    with db_session() as connection:
        logical_runtime_name = _logical_runtime_name(connection, logical_server_id)
    try:
        proxy_state = DEFAULT_MIHOMO_ADAPTER.get_proxy_state(logical_runtime_name)
    except Exception as exc:
        return {
            "ok": False,
            "logical_server_id": logical_server_id,
            "runtime_name": logical_runtime_name,
            "error_code": "MIHOMO_PROXY_STATE_UNAVAILABLE",
            "error_message": str(exc),
        }
    selected = str(proxy_state.get("now") or "").strip()
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
        "source": "mihomo_runtime",
    }


def check_logical_server_delay(
    logical_server_id: str,
    *,
    test_url: str = "https://www.gstatic.com/generate_204",
    timeout_ms: int = 10000,
) -> dict[str, Any]:
    topology = get_logical_topology(logical_server_id)
    if topology is None:
        return {"ok": False, "logical_server_id": logical_server_id, "error_code": "LOGICAL_SERVER_NOT_FOUND"}
    active_members = [member for member in topology["members"] if member["is_active"]]
    group_delay = None
    if len(active_members) > 1:
        with db_session() as connection:
            logical_runtime_name = _logical_runtime_name(connection, logical_server_id)
        group_delay = DEFAULT_MIHOMO_ADAPTER.check_delay(logical_runtime_name, test_url=test_url, timeout_ms=timeout_ms)
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
    result = DEFAULT_MIHOMO_ADAPTER.check_delay(member_runtime_name, test_url=test_url, timeout_ms=timeout_ms)
    status = "healthy" if result.ok else "failed"
    evidence = {
        "provider_role": PROVIDER_ROLE_VPN_DATAPLANE,
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
    }


def check_member_delay(logical_server_id: str, member_id: str, *, timeout_ms: int = 10000) -> dict[str, Any]:
    topology = get_logical_topology(logical_server_id)
    if topology is None:
        return {"ok": False, "error_code": "LOGICAL_SERVER_NOT_FOUND"}
    member = next((item for item in topology["members"] if item["member_id"] == member_id and item["is_active"]), None)
    if member is None:
        return {"ok": False, "error_code": "LOGICAL_MEMBER_NOT_FOUND"}
    result = DEFAULT_MIHOMO_ADAPTER.check_delay(member["runtime_name"], timeout_ms=timeout_ms)
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
            evidence={"provider_role": PROVIDER_ROLE_VPN_DATAPLANE, "runtime_name": member["runtime_name"]},
        )
        if result.ok:
            connection.execute(
                "UPDATE logical_server_topology SET active_member_id = ?, updated_at = CURRENT_TIMESTAMP WHERE logical_server_id = ?",
                (member_id, logical_server_id),
            )
    return {"ok": result.ok, "logical_server_id": logical_server_id, "member_id": member_id, "status": status, "latency_ms": result.delay_ms, "error_code": result.error_code, "error_message": result.error_message}


def probe_members(*, budget: int = 3, timeout_ms: int = 5000, healthy_ttl_seconds: int = 1800, failed_ttl_seconds: int = 120) -> dict[str, Any]:
    safe_budget = max(1, min(int(budget), 20))
    with db_session() as connection:
        rows = connection.execute(
            """
            SELECT m.logical_server_id, m.member_id, m.member_order,
                   t.active_member_id, h.status, h.checked_at
            FROM logical_server_members m
            JOIN logical_server_topology t ON t.logical_server_id = m.logical_server_id
            LEFT JOIN logical_server_member_health h
              ON h.logical_server_id = m.logical_server_id AND h.member_id = m.member_id AND h.provider_role = ?
            WHERE m.is_active = 1
              AND (
                  h.checked_at IS NULL
                  OR (h.status = 'healthy' AND h.checked_at <= datetime('now', ?))
                  OR (h.status != 'healthy' AND h.checked_at <= datetime('now', ?))
              )
            ORDER BY CASE WHEN t.active_member_id = m.member_id THEN 0 ELSE 1 END,
                     m.logical_server_id, m.member_order, m.member_id
            """,
            (PROVIDER_ROLE_VPN_DATAPLANE, f"-{max(1, int(healthy_ttl_seconds))} seconds", f"-{max(1, int(failed_ttl_seconds))} seconds"),
        ).fetchall()
        state = connection.execute(
            "SELECT cursor_logical_server_id, cursor_member_id FROM logical_server_probe_state WHERE id = 1"
        ).fetchone()
        cursor = (str(state["cursor_logical_server_id"] or ""), str(state["cursor_member_id"] or "")) if state else ("", "")
    ordered = [(str(row["logical_server_id"]), str(row["member_id"])) for row in rows]
    after = [item for item in ordered if item > cursor]
    selected = (after + [item for item in ordered if item <= cursor])[:safe_budget]
    results = [check_member_delay(logical_server_id, member_id, timeout_ms=timeout_ms) for logical_server_id, member_id in selected]
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
    return {"ok": all(item["ok"] for item in results), "budget": safe_budget, "probed": len(results), "results": results}
