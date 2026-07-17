from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from fwrouter_api.adapters.mihomo import DEFAULT_MIHOMO_ADAPTER
from fwrouter_api.db.connection import db_session


DEFAULT_TEST_URL = "https://www.gstatic.com/generate_204"
DEFAULT_TIMEOUT_MS = 10000
DEFAULT_SWEEP_LIMIT = 10


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _latency_label(last_ping_ms: int | None) -> str:
    if last_ping_ms is None:
        return "n/a"

    return f"{last_ping_ms} ms"


def _upsert_ping_state(
    *,
    server_id: str,
    status: str,
    last_ping_ms: int | None,
    checked_by: str,
    error_code: str | None,
    error_message: str | None,
    metadata: dict[str, Any],
) -> None:
    with db_session() as connection:
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
            VALUES (?, ?, ?, CURRENT_TIMESTAMP, ?, ?, ?, json(?))
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
                server_id,
                status,
                last_ping_ms,
                checked_by,
                error_code,
                error_message,
                json.dumps(metadata, ensure_ascii=False, sort_keys=True),
            ),
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


def _mihomo_target_for_server_id(server_id: str) -> str:
    """Return the runtime Mihomo target name for a persisted server id."""

    normalized = str(server_id or "").strip()
    if not normalized:
        return normalized

    with db_session() as connection:
        row = connection.execute(
            """
            SELECT s.server_name
            FROM servers AS s
            JOIN server_custom_https_proxy AS c ON c.server_id = s.server_id
            WHERE s.server_id = ?
            """,
            (normalized,),
        ).fetchone()

    if row is None:
        return normalized
    return str(row["server_name"] or normalized)


def _server_id_for_mihomo_target(target: str) -> str:
    """Return the persisted server id for a runtime Mihomo target when known."""

    normalized = str(target or "").strip()
    if not normalized:
        return normalized

    with db_session() as connection:
        row = connection.execute(
            """
            SELECT s.server_id
            FROM servers AS s
            JOIN server_custom_https_proxy AS c ON c.server_id = s.server_id
            WHERE s.server_name = ?
              AND s.inventory_state = 'active'
            ORDER BY s.updated_at DESC
            LIMIT 1
            """,
            (normalized,),
        ).fetchone()

    if row is None:
        return normalized
    return str(row["server_id"] or normalized)


def check_server_delay(
    server_id: str,
    *,
    update_state: bool = False,
    checked_by: str = "manual",
    test_url: str = DEFAULT_TEST_URL,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
) -> dict[str, Any]:
    """Check one server delay through Mihomo.

    With update_state=False this is a dry-run and does not write SQLite.
    With update_state=True it updates server_ping_state for this server.
    """

    mihomo_target = _mihomo_target_for_server_id(server_id)
    delay = DEFAULT_MIHOMO_ADAPTER.check_delay(
        mihomo_target,
        test_url=test_url,
        timeout_ms=timeout_ms,
    )

    status = "success" if delay.ok else "failed"
    metadata = {
        "adapter": "mihomo",
        "test_url": test_url,
        "timeout_ms": timeout_ms,
        "checked_at": _utc_timestamp(),
        "mihomo_target": mihomo_target,
        "details": delay.details,
    }

    if update_state:
        _upsert_ping_state(
            server_id=server_id,
            status=status,
            last_ping_ms=delay.delay_ms,
            checked_by=checked_by,
            error_code=delay.error_code,
            error_message=delay.error_message,
            metadata=metadata,
        )

    return {
        "ok": delay.ok,
        "server_id": server_id,
        "mihomo_target": mihomo_target,
        "status": status,
        "last_ping_ms": delay.delay_ms,
        "latency_label": _latency_label(delay.delay_ms),
        "checked_by": checked_by,
        "test_url": test_url,
        "timeout_ms": timeout_ms,
        "error_code": delay.error_code,
        "error_message": delay.error_message,
        "updated_state": update_state,
    }


def check_active_server_delay(
    *,
    update_state: bool = False,
    checked_by: str = "manual",
    test_url: str = DEFAULT_TEST_URL,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
) -> dict[str, Any]:
    """Check delay for currently active Mihomo server."""

    health = DEFAULT_MIHOMO_ADAPTER.health()

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
        _server_id_for_mihomo_target(health.active_server_id),
        update_state=update_state,
        checked_by=checked_by,
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
    """Check delay for a bounded list of active servers.

    This is intentionally bounded by limit because each server check can take up
    to timeout_ms. With update_state=False this is a dry-run and does not write
    SQLite. With update_state=True it updates server_ping_state per checked
    server.
    """

    safe_limit = max(1, min(limit, 100))
    sqlite_server_ids = _load_active_server_ids(limit=safe_limit)
    mihomo_server_ids = {
        server.server_id for server in DEFAULT_MIHOMO_ADAPTER.list_servers()
    }

    server_ids = [
        server_id
        for server_id in sqlite_server_ids
        if _mihomo_target_for_server_id(server_id) in mihomo_server_ids
    ]

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
        "sqlite_candidates_count": len(sqlite_server_ids),
        "mihomo_servers_count": len(mihomo_server_ids),
        "checked_count": len(results),
        "success_count": success_count,
        "failed_count": failed_count,
        "results": results,
    }
