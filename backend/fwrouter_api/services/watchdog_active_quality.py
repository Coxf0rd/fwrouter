from __future__ import annotations

from typing import Any

from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import db_session


DEFAULT_ACTIVE_CHECK_TTL_SECONDS = 60


def recent_successful_active_check(
    *,
    server_id: str | None,
    ttl_seconds: int = DEFAULT_ACTIVE_CHECK_TTL_SECONDS,
    checked_by: str,
    timeout_ms: int,
) -> dict[str, Any] | None:
    normalized_server_id = str(server_id or "").strip()
    if not normalized_server_id:
        return None
    cutoff_modifier = f"-{max(1, int(ttl_seconds))} seconds"
    with db_session() as connection:
        row = connection.execute(
            """
            SELECT status, last_ping_ms, checked_at, error_code, error_message
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
    last_ping_ms = row["last_ping_ms"]
    return {
        "ok": True,
        "server_id": normalized_server_id,
        "status": "success",
        "last_ping_ms": last_ping_ms,
        "latency_label": f"{last_ping_ms} ms" if last_ping_ms is not None else "n/a",
        "checked_by": checked_by,
        "test_url": "cached_server_ping_state",
        "timeout_ms": timeout_ms,
        "error_code": None,
        "error_message": None,
        "updated_state": False,
        "cached": True,
        "cache_ttl_seconds": ttl_seconds,
        "checked_at": row["checked_at"],
    }


def active_quality_degraded(active_check: dict[str, Any] | None) -> bool:
    if not isinstance(active_check, dict):
        return False
    if not bool(active_check.get("ok")):
        return True
    last_ping_ms = active_check.get("last_ping_ms")
    if last_ping_ms is None:
        return False
    try:
        latency_ms = int(last_ping_ms)
    except (TypeError, ValueError):
        return False
    return latency_ms > get_settings().watchdog_active_quality_max_latency_ms


def degraded_active_check(active_check: dict[str, Any]) -> dict[str, Any]:
    if not bool(active_check.get("ok")):
        return active_check
    degraded = dict(active_check)
    latency_ms = degraded.get("last_ping_ms")
    threshold_ms = get_settings().watchdog_active_quality_max_latency_ms
    degraded["ok"] = False
    degraded["status"] = "degraded_latency"
    degraded["error_code"] = "WATCHDOG_ACTIVE_LATENCY_DEGRADED"
    degraded["error_message"] = (
        f"Current VPN-auto server latency {latency_ms} ms exceeds quality threshold {threshold_ms} ms."
    )
    return degraded
