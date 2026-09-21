from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from fwrouter_api.db.connection import db_session
from fwrouter_api.services.logical_topology import (
    check_logical_server_delay,
    check_logical_server_delays,
)
from fwrouter_api.services.runtime_adapters import (
    RUNTIME_ROLE_VPN_DATAPLANE,
    active_runtime_adapter,
    runtime_adapter_operations,
)


DEFAULT_TEST_URL = "https://www.gstatic.com/generate_204"
DEFAULT_TIMEOUT_MS = 10000
DEFAULT_SWEEP_LIMIT = 10
PING_SOURCES = {"manual", "selector", "watchdog", "background"}


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _latency_label(last_ping_ms: int | None) -> str:
    if last_ping_ms is None:
        return "n/a"

    return f"{last_ping_ms} ms"


def _normalize_ping_source(source: str | None = None, *, checked_by: str | None = None) -> str:
    normalized = str(source or "").strip().lower()
    if normalized in PING_SOURCES:
        return normalized

    marker = str(checked_by or "").strip().lower()
    if marker.startswith("selector"):
        return "selector"
    if marker.startswith("watchdog"):
        return "watchdog"
    if "sweep" in marker or marker.startswith("job_") or marker.startswith("background"):
        return "background"
    return "manual"


def _normalize_status(status: str) -> str:
    normalized = str(status or "").strip().lower()
    if normalized in {"unknown", "success", "failed", "skipped"}:
        return normalized
    return "failed"


def _json_dumps(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _json_loads(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        loaded = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def record_ping_result(
    *,
    server_id: str,
    status: str,
    latency_ms: int | None = None,
    runtime_target: str | None = None,
    source: str = "manual",
    checked_at: str | None = None,
    checked_by: str | None = None,
    error_code: str | None,
    error_message: str | None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized_server_id = str(server_id or "").strip()
    normalized_source = _normalize_ping_source(source, checked_by=checked_by)
    normalized_status = _normalize_status(status)
    checked_at_value = checked_at or _utc_timestamp()
    checked_by_value = checked_by or normalized_source
    target = runtime_target or resolve_server_runtime_target(normalized_server_id)["runtime_target"]
    details = {
        **(metadata or {}),
        "runtime_target": target,
        "source": normalized_source,
    }

    with db_session() as connection:
        if normalized_source == "manual":
            connection.execute(
                """
                INSERT INTO server_ping_state (
                    server_id,
                    status,
                    last_ping_ms,
                    checked_at,
                    checked_by,
                    error_code,
                    error_message,
                    metadata_json,
                    manual_status,
                    manual_ping_ms,
                    manual_checked_at,
                    manual_checked_by,
                    manual_error_code,
                    manual_error_message,
                    manual_metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, json(?), ?, ?, ?, ?, ?, ?, json(?))
                ON CONFLICT(server_id) DO UPDATE SET
                    status = excluded.status,
                    last_ping_ms = excluded.last_ping_ms,
                    checked_at = excluded.checked_at,
                    checked_by = excluded.checked_by,
                    error_code = excluded.error_code,
                    error_message = excluded.error_message,
                    metadata_json = excluded.metadata_json,
                    manual_status = excluded.manual_status,
                    manual_ping_ms = excluded.manual_ping_ms,
                    manual_checked_at = excluded.manual_checked_at,
                    manual_checked_by = excluded.manual_checked_by,
                    manual_error_code = excluded.manual_error_code,
                    manual_error_message = excluded.manual_error_message,
                    manual_metadata_json = excluded.manual_metadata_json
                """,
                (
                    normalized_server_id,
                    normalized_status,
                    latency_ms,
                    checked_at_value,
                    checked_by_value,
                    error_code,
                    error_message,
                    _json_dumps(details),
                    normalized_status,
                    latency_ms,
                    checked_at_value,
                    checked_by_value,
                    error_code,
                    error_message,
                    _json_dumps(details),
                ),
            )
        else:
            connection.execute(
                """
                INSERT INTO server_ping_state (
                    server_id,
                    status,
                    last_ping_ms,
                    checked_at,
                    checked_by,
                    error_code,
                    error_message,
                    metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, json(?))
                ON CONFLICT(server_id) DO UPDATE SET
                    status = excluded.status,
                    last_ping_ms = excluded.last_ping_ms,
                    checked_at = excluded.checked_at,
                    checked_by = excluded.checked_by,
                    error_code = excluded.error_code,
                    error_message = excluded.error_message,
                    metadata_json = excluded.metadata_json
                """,
                (
                    normalized_server_id,
                    normalized_status,
                    latency_ms,
                    checked_at_value,
                    checked_by_value,
                    error_code,
                    error_message,
                    _json_dumps(details),
                ),
            )

    return {
        "server_id": normalized_server_id,
        "runtime_target": target,
        "source": normalized_source,
        "status": normalized_status,
        "latency_ms": latency_ms,
        "checked_at": checked_at_value,
        "checked_by": checked_by_value,
        "error_code": error_code,
        "error_message": error_message,
        "metadata": details,
    }


def resolve_server_runtime_target(server_id: str) -> dict[str, Any]:
    """Resolve a stable server id to the Mihomo runtime proxy target."""

    normalized = str(server_id or "").strip()
    if not normalized:
        return {
            "ok": False,
            "server_id": normalized,
            "runtime_target": normalized,
            "error_code": "SERVER_ID_EMPTY",
            "error_message": "Server id is empty.",
        }

    with db_session() as connection:
        row = connection.execute(
            """
            SELECT
                s.server_id,
                s.server_name,
                s.provider_name,
                CASE
                    WHEN c.server_id IS NOT NULL THEN s.server_name
                    ELSE COALESCE(
                        json_extract(s.raw_json, '$._fwrouter_runtime_name'),
                        json_extract(s.raw_json, '$.name'),
                        s.server_name
                    )
                END AS runtime_target
            FROM servers AS s
            LEFT JOIN server_custom_https_proxy AS c ON c.server_id = s.server_id
            WHERE s.server_id = ?
            """,
            (normalized,),
        ).fetchone()

    if row is None:
        return {
            "ok": False,
            "server_id": normalized,
            "runtime_target": normalized,
            "error_code": "SERVER_NOT_FOUND",
            "error_message": f"Server not found: {normalized}",
        }
    return {
        "ok": True,
        "server_id": str(row["server_id"]),
        "server_name": row["server_name"],
        "provider_name": row["provider_name"],
        "runtime_target": str(row["runtime_target"] or row["server_id"]),
        "error_code": None,
        "error_message": None,
    }


def _observation_from_row(
    row: Any,
    *,
    runtime_target: str,
    prefix: str = "",
    default_source: str,
) -> dict[str, Any]:
    status_key = f"{prefix}status"
    ping_key = f"{prefix}ping_ms" if prefix else "last_ping_ms"
    checked_at_key = f"{prefix}checked_at"
    checked_by_key = f"{prefix}checked_by"
    error_code_key = f"{prefix}error_code"
    error_message_key = f"{prefix}error_message"
    metadata_key = f"{prefix}metadata_json"
    if row is None or row[status_key] is None:
        return {
            "runtime_target": runtime_target,
            "source": default_source,
            "status": "unknown",
            "latency_ms": None,
            "last_ping_ms": None,
            "checked_at": None,
            "checked_by": None,
            "error_code": None,
            "error_message": None,
            "metadata": {},
        }
    source = "manual" if prefix == "manual_" else _normalize_ping_source(
        checked_by=row[checked_by_key],
    )
    latency_ms = row[ping_key]
    return {
        "runtime_target": runtime_target,
        "source": source,
        "status": _normalize_status(row[status_key]),
        "latency_ms": latency_ms,
        "last_ping_ms": latency_ms,
        "checked_at": row[checked_at_key],
        "checked_by": row[checked_by_key],
        "error_code": row[error_code_key],
        "error_message": row[error_message_key],
        "metadata": _json_loads(row[metadata_key]),
    }


def get_server_ping_state(server_id: str) -> dict[str, Any]:
    target = resolve_server_runtime_target(server_id)
    normalized_server_id = target["server_id"]
    runtime_target = target["runtime_target"]
    with db_session() as connection:
        row = connection.execute(
            """
            SELECT
                status,
                last_ping_ms,
                checked_at,
                checked_by,
                error_code,
                error_message,
                metadata_json,
                manual_status,
                manual_ping_ms,
                manual_checked_at,
                manual_checked_by,
                manual_error_code,
                manual_error_message,
                manual_metadata_json
            FROM server_ping_state
            WHERE server_id = ?
            """,
            (normalized_server_id,),
        ).fetchone()

    runtime_observation = _observation_from_row(
        row,
        runtime_target=runtime_target,
        default_source="background",
    )
    manual_observation = _observation_from_row(
        row,
        runtime_target=runtime_target,
        prefix="manual_",
        default_source="manual",
    )
    return {
        "ok": target["ok"],
        "server_id": normalized_server_id,
        "runtime_target": runtime_target,
        "status": manual_observation["status"],
        "latency_ms": manual_observation["latency_ms"],
        "checked_at": manual_observation["checked_at"],
        "source": "manual",
        "error_code": manual_observation["error_code"],
        "error_message": manual_observation["error_message"],
        "manual": manual_observation,
        "background": runtime_observation,
        "runtime": runtime_observation,
        "resolver": target,
    }


def get_recent_runtime_ping_success(
    server_id: str,
    *,
    ttl_seconds: int,
) -> dict[str, Any] | None:
    normalized_server_id = str(server_id or "").strip()
    if not normalized_server_id:
        return None

    cutoff_modifier = f"-{max(1, int(ttl_seconds))} seconds"
    target = resolve_server_runtime_target(normalized_server_id)
    with db_session() as connection:
        row = connection.execute(
            """
            SELECT status, last_ping_ms, checked_at, checked_by, error_code, error_message, metadata_json
            FROM server_ping_state
            WHERE server_id = ?
              AND status = 'success'
              AND checked_at >= datetime('now', ?)
            LIMIT 1
            """,
            (normalized_server_id, cutoff_modifier),
        ).fetchone()

    if row is None:
        return None

    return _observation_from_row(
        row,
        runtime_target=target["runtime_target"],
        default_source="background",
    )


def _load_active_server_ids(*, limit: int | None = None) -> list[str]:
    query = """
        SELECT s.server_id
        FROM servers s
        LEFT JOIN server_preferences sp ON sp.server_id = s.server_id
        LEFT JOIN server_ping_state ping ON ping.server_id = s.server_id
        WHERE s.inventory_state = 'active'
          AND COALESCE(sp.manually_deleted_at, '') = ''
        ORDER BY s.server_name ASC
    """

    params: tuple[Any, ...] = ()

    if limit is not None:
        query += " LIMIT ?"
        params = (limit,)

    query = query.replace(
        "ORDER BY s.server_name ASC",
        """
        ORDER BY
            CASE WHEN COALESCE(sp.vpn_auto, 0) = 1 THEN 0 ELSE 1 END,
            CASE WHEN COALESCE(sp.vpn_auto_priority, 0) > 0 THEN 0 ELSE 1 END,
            COALESCE(sp.vpn_auto_priority, 0) DESC,
            CASE
                WHEN COALESCE(ping.checked_at, '') = '' THEN 0
                WHEN ping.status = 'success' THEN 1
                ELSE 2
            END,
            COALESCE(ping.checked_at, '') ASC,
            CASE ping.status WHEN 'success' THEN 0 WHEN 'unknown' THEN 1 ELSE 2 END,
            s.server_name ASC
        """
    )

    with db_session() as connection:
        rows = connection.execute(query, params).fetchall()

    return [str(row["server_id"]) for row in rows]


def _runtime_target_for_server_id(server_id: str) -> str:
    return resolve_server_runtime_target(server_id)["runtime_target"]


def _server_id_for_runtime_target(target: str) -> str:
    normalized = str(target or "").strip()
    if not normalized:
        return normalized

    with db_session() as connection:
        row = connection.execute(
            """
            SELECT
                s.server_id
            FROM servers AS s
            LEFT JOIN server_custom_https_proxy AS c ON c.server_id = s.server_id
            WHERE (
                    (c.server_id IS NOT NULL AND s.server_name = ?)
                    OR COALESCE(
                        json_extract(s.raw_json, '$._fwrouter_runtime_name'),
                        json_extract(s.raw_json, '$.name'),
                        s.server_name
                    ) = ?
                )
              AND s.inventory_state = 'active'
            ORDER BY s.updated_at DESC
            LIMIT 1
            """,
            (normalized, normalized),
        ).fetchone()

    if row is None:
        return normalized
    return str(row["server_id"] or normalized)


def check_server_delay(
    server_id: str,
    *,
    update_state: bool = False,
    checked_by: str = "manual",
    source: str | None = None,
    test_url: str = DEFAULT_TEST_URL,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
) -> dict[str, Any]:

    ping_source = _normalize_ping_source(source, checked_by=checked_by)
    target = resolve_server_runtime_target(server_id)
    runtime_target = target["runtime_target"]
    runtime_adapter = active_runtime_adapter(RUNTIME_ROLE_VPN_DATAPLANE)
    runtime_operations = runtime_adapter_operations(runtime_adapter)
    delay = check_logical_server_delay(
        server_id,
        test_url=test_url,
        timeout_ms=timeout_ms,
        probe_reason=checked_by,
        probe_lane=ping_source,
    )
    if delay.get("error_code") == "LOGICAL_SERVER_NOT_FOUND":
        if callable(getattr(runtime_operations, "check_delay", None)):
            direct_delay = runtime_operations.check_delay(
                runtime_target,
                test_url=test_url,
                timeout_ms=timeout_ms,
            )
            delay = {
                "ok": direct_delay.ok,
                "status": "success" if direct_delay.ok else "failed",
                "latency_ms": direct_delay.delay_ms,
                "member_id": None,
                "member_runtime_name": None,
                "error_code": direct_delay.error_code,
                "error_message": direct_delay.error_message,
                "details": direct_delay.details,
                "probe_backend": "local_fallback",
            }
        else:
            delay = {
                "ok": False,
                "status": "failed",
                "latency_ms": None,
                "member_id": None,
                "member_runtime_name": None,
                "error_code": "RUNTIME_LOCAL_PROBE_UNAVAILABLE",
                "error_message": "Active runtime adapter cannot probe this target.",
                "details": {},
            }
    status = "success" if delay["ok"] else "failed"
    metadata = {
        "adapter": runtime_adapter.get("adapter_id"),
        "test_url": test_url,
        "timeout_ms": timeout_ms,
        "checked_at": _utc_timestamp(),
        "runtime_target": runtime_target,
        "mihomo_target": runtime_target,
        "active_member_id": delay.get("member_id"),
        "active_member_runtime_name": delay.get("member_runtime_name"),
        "details": delay,
    }

    if update_state:
        record_ping_result(
            server_id=server_id,
            status=status,
            latency_ms=delay.get("latency_ms"),
            runtime_target=runtime_target,
            source=ping_source,
            checked_by=checked_by,
            error_code=delay.get("error_code"),
            error_message=delay.get("error_message"),
            metadata=metadata,
        )

    return {
        "ok": delay["ok"],
        "server_id": server_id,
        "runtime_target": runtime_target,
        "mihomo_target": runtime_target,
        "source": ping_source,
        "status": status,
        "latency_ms": delay.get("latency_ms"),
        "last_ping_ms": delay.get("latency_ms"),
        "checked_at": metadata["checked_at"],
        "latency_label": _latency_label(delay.get("latency_ms")),
        "checked_by": checked_by,
        "test_url": test_url,
        "timeout_ms": timeout_ms,
        "error_code": delay.get("error_code"),
        "error_message": delay.get("error_message"),
        "updated_state": update_state,
        "active_member_id": delay.get("member_id"),
        "active_member_runtime_name": delay.get("member_runtime_name"),
        "probe_backend": delay.get("probe_backend"),
        "runtime_adapter_id": runtime_adapter.get("adapter_id"),
    }


def check_server_delays(
    server_ids: list[str],
    *,
    update_state: bool = False,
    checked_by: str = "manual",
    source: str | None = None,
    test_url: str = DEFAULT_TEST_URL,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    fallback_check: Any | None = None,
) -> list[dict[str, Any]]:
    ordered_ids = list(dict.fromkeys(str(item) for item in server_ids if str(item).strip()))
    ping_source = _normalize_ping_source(source, checked_by=checked_by)
    batch = check_logical_server_delays(
        ordered_ids,
        test_url=test_url,
        timeout_ms=timeout_ms,
        probe_reason=checked_by,
        probe_lane=ping_source,
    )
    if batch is None:
        check_one = fallback_check or check_server_delay
        return [
            check_one(
                server_id,
                update_state=update_state,
                checked_by=checked_by,
                source=source,
                test_url=test_url,
                timeout_ms=timeout_ms,
            )
            for server_id in ordered_ids
        ]
    results: list[dict[str, Any]] = []
    for server_id, delay in zip(ordered_ids, batch):
        runtime_target = resolve_server_runtime_target(server_id)["runtime_target"]
        status = "success" if delay.get("ok") else "failed"
        metadata = {
            "adapter": delay.get("runtime_adapter_id"),
            "test_url": test_url,
            "timeout_ms": timeout_ms,
            "checked_at": _utc_timestamp(),
            "runtime_target": runtime_target,
            "mihomo_target": runtime_target,
            "active_member_id": delay.get("member_id"),
            "active_member_runtime_name": delay.get("member_runtime_name"),
            "details": delay,
        }
        if update_state:
            record_ping_result(
                server_id=server_id,
                status=status,
                latency_ms=delay.get("latency_ms"),
                runtime_target=runtime_target,
                source=ping_source,
                checked_by=checked_by,
                error_code=delay.get("error_code"),
                error_message=delay.get("error_message"),
                metadata=metadata,
            )
        results.append(
            {
                "ok": bool(delay.get("ok")),
                "server_id": server_id,
                "runtime_target": runtime_target,
                "mihomo_target": runtime_target,
                "source": ping_source,
                "status": status,
                "latency_ms": delay.get("latency_ms"),
                "last_ping_ms": delay.get("latency_ms"),
                "checked_at": metadata["checked_at"],
                "latency_label": _latency_label(delay.get("latency_ms")),
                "checked_by": checked_by,
                "test_url": test_url,
                "timeout_ms": timeout_ms,
                "error_code": delay.get("error_code"),
                "error_message": delay.get("error_message"),
                "updated_state": update_state,
                "active_member_id": delay.get("member_id"),
                "active_member_runtime_name": delay.get("member_runtime_name"),
                "probe_backend": delay.get("probe_backend"),
                "runtime_adapter_id": delay.get("runtime_adapter_id"),
            }
        )
    return results


def check_active_server_delay(
    *,
    update_state: bool = False,
    checked_by: str = "manual",
    source: str | None = None,
    test_url: str = DEFAULT_TEST_URL,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
) -> dict[str, Any]:
    runtime_adapter = active_runtime_adapter(RUNTIME_ROLE_VPN_DATAPLANE)
    runtime_operations = runtime_adapter_operations(runtime_adapter)
    if not callable(getattr(runtime_operations, "health", None)):
        return {
            "ok": False,
            "server_id": None,
            "status": "failed",
            "last_ping_ms": None,
            "latency_label": "n/a",
            "checked_by": checked_by,
            "test_url": test_url,
            "timeout_ms": timeout_ms,
            "error_code": "RUNTIME_HEALTH_UNAVAILABLE",
            "error_message": "Active runtime adapter does not expose runtime health.",
            "updated_state": False,
        }
    health = runtime_operations.health()

    if not health.active_server_id:
        return {
            "ok": False,
            "server_id": None,
            "status": "failed",
            "last_ping_ms": None,
            "latency_label": "n/a",
            "checked_by": checked_by,
            "test_url": test_url,
            "timeout_ms": timeout_ms,
            "error_code": "MIHOMO_ACTIVE_SERVER_MISSING",
            "error_message": "Mihomo active server is not set.",
            "updated_state": False,
        }

    return check_server_delay(
        _server_id_for_runtime_target(health.active_server_id),
        update_state=update_state,
        checked_by=checked_by,
        source=source,
        test_url=test_url,
        timeout_ms=timeout_ms,
    )


def check_server_delay_sweep(
    *,
    update_state: bool = False,
    checked_by: str = "manual_sweep",
    test_url: str = DEFAULT_TEST_URL,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    limit: int = DEFAULT_SWEEP_LIMIT,
) -> dict[str, Any]:

    safe_limit = max(1, min(limit, 100))
    server_ids = _load_active_server_ids(limit=safe_limit)

    results = [
        check_server_delay(
            server_id,
            update_state=update_state,
            checked_by=checked_by,
            test_url=test_url,
            timeout_ms=timeout_ms,
        )
        for server_id in server_ids
    ]

    success_count = sum(1 for item in results if item["status"] == "success")
    failed_count = sum(1 for item in results if item["status"] == "failed")

    return {
        "ok": bool(results) and success_count > 0,
        "update_state": update_state,
        "checked_by": checked_by,
        "test_url": test_url,
        "timeout_ms": timeout_ms,
        "requested_limit": limit,
        "effective_limit": safe_limit,
        "sqlite_candidates_count": len(server_ids),
        "checked_count": len(results),
        "success_count": success_count,
        "failed_count": failed_count,
        "results": results,
    }
