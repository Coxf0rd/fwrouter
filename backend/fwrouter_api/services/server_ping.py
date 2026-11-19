from __future__ import annotations

import json
import hashlib
from datetime import datetime, timezone
from threading import Lock
from time import monotonic
from typing import Any

from fwrouter_api.db.connection import db_session
from fwrouter_api.services.logical_topology import (
    check_logical_server_delay,
    check_logical_server_delays,
    persist_logical_server_probe_result,
)
from fwrouter_api.services.runtime_adapters import (
    RUNTIME_ROLE_VPN_DATAPLANE,
    active_runtime_adapter,
    runtime_adapter_operations,
)
from fwrouter_api.services.live_probe_cache import get_live_probe_cache_many
from fwrouter_api.services.vpn_auto_selection_state import read_selection_fence


DEFAULT_TEST_URL = "https://www.gstatic.com/generate_204"
DEFAULT_TIMEOUT_MS = 10000
DEFAULT_SWEEP_LIMIT = 10
PING_SOURCES = {"manual", "selector", "watchdog", "background"}
_AUTOMATIC_PROBE_SUCCESS_TTL_SECONDS = 15.0
_AUTOMATIC_PROBE_FAILURE_BACKOFF_MAX_SECONDS = 30.0
_AUTOMATIC_PROBE_FAILURES: dict[str, tuple[int, float]] = {}
_AUTOMATIC_PROBE_FAILURES_LOCK = Lock()


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
    # Provider refresh verification is an automatic probe of the selected
    # provider member. Keep its provenance distinct from selector/watchdog
    # probes, and only accept the exact internal caller pair. In particular,
    # arbitrary `provider_validation` labels must not make a manual probe
    # eligible for automatic selection.
    if normalized == "provider_validation" and marker == "provider_generation":
        return normalized
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
    expected_selection_revision: int | None = None,
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
        if expected_selection_revision is not None:
            connection.execute("BEGIN IMMEDIATE")
            if read_selection_fence(connection).get("revision") != expected_selection_revision:
                return {"updated_state": False, "stale_observation": True}
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
            cursor = connection.execute(
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
                WHERE server_ping_state.checked_at IS NULL
                   OR julianday(excluded.checked_at) > julianday(server_ping_state.checked_at)
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
        "updated_state": normalized_source == "manual" or cursor.rowcount > 0,
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


def _automatic_probe_context(
    server_id: str,
    *,
    test_url: str,
    timeout_ms: int,
    adapter_context: tuple[str, str] | None = None,
    selection_revision: int | None = None,
) -> tuple[str, str, str, str]:
    target = resolve_server_runtime_target(server_id)["runtime_target"]
    if adapter_context is None:
        adapter = active_runtime_adapter(RUNTIME_ROLE_VPN_DATAPLANE)
        operations = runtime_adapter_operations(adapter)
        incarnation_reader = getattr(operations, "runtime_incarnation", None)
        try:
            incarnation = str(incarnation_reader() or "") if callable(incarnation_reader) else ""
        except Exception:
            incarnation = ""
        adapter_id = str(adapter.get("adapter_id") or "unknown")
    else:
        adapter_id, incarnation = adapter_context
    context = json.dumps(
        [str(server_id), adapter_id, incarnation, target, test_url, int(timeout_ms), selection_revision],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    key = "server_ping.target." + hashlib.sha256(context.encode("utf-8")).hexdigest()
    return key, target, adapter_id, incarnation


def _probe_backoff_active(key: str) -> bool:
    with _AUTOMATIC_PROBE_FAILURES_LOCK:
        failure = _AUTOMATIC_PROBE_FAILURES.get(key)
        return bool(failure and failure[1] > monotonic())


def _record_probe_outcome(key: str, status: str) -> None:
    with _AUTOMATIC_PROBE_FAILURES_LOCK:
        if status == "success":
            _AUTOMATIC_PROBE_FAILURES.pop(key, None)
        elif status == "failed":
            failures = _AUTOMATIC_PROBE_FAILURES.get(key, (0, 0.0))[0] + 1
            delay = min(_AUTOMATIC_PROBE_FAILURE_BACKOFF_MAX_SECONDS, float(2 ** min(failures, 5)))
            _AUTOMATIC_PROBE_FAILURES[key] = (failures, monotonic() + delay)
        if len(_AUTOMATIC_PROBE_FAILURES) > 512:
            now = monotonic()
            expired = [item for item, value in _AUTOMATIC_PROBE_FAILURES.items() if value[1] <= now]
            for item in expired:
                _AUTOMATIC_PROBE_FAILURES.pop(item, None)
            while len(_AUTOMATIC_PROBE_FAILURES) > 512:
                _AUTOMATIC_PROBE_FAILURES.pop(next(iter(_AUTOMATIC_PROBE_FAILURES)))


def _deferred_probe_result(server_id: str, target: str, checked_by: str) -> dict[str, Any]:
    return {
        "ok": False,
        "server_id": server_id,
        "runtime_target": target,
        "mihomo_target": target,
        "source": "unknown",
        "status": "unknown",
        "latency_ms": None,
        "last_ping_ms": None,
        "checked_at": None,
        "latency_label": "n/a",
        "checked_by": checked_by,
        "error_code": "VPN_HEALTH_PROBE_BACKOFF",
        "error_message": "Automatic health check is in bounded backoff after a failed probe.",
        "updated_state": False,
    }


def check_server_delay(
    server_id: str,
    *,
    update_state: bool = False,
    checked_by: str = "manual",
    source: str | None = None,
    test_url: str = DEFAULT_TEST_URL,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    expected_selection_revision: int | None = None,
    force_refresh: bool = False,
) -> dict[str, Any]:
    ping_source = _normalize_ping_source(source, checked_by=checked_by)
    if ping_source != "manual" and expected_selection_revision is None:
        try:
            with db_session() as connection:
                expected_selection_revision = read_selection_fence(connection).get("revision")
        except Exception:
            expected_selection_revision = None
    if ping_source == "manual":
        return _check_server_delay_uncached(
            server_id, update_state=update_state, checked_by=checked_by,
            source=source, test_url=test_url, timeout_ms=timeout_ms,
            expected_selection_revision=expected_selection_revision,
        )

    key, target, _adapter_id, _incarnation = _automatic_probe_context(
        server_id, test_url=test_url, timeout_ms=timeout_ms,
        selection_revision=expected_selection_revision,
    )
    if _probe_backoff_active(key) and not force_refresh:
        return _deferred_probe_result(server_id, target, checked_by)

    def load_one() -> dict[str, Any]:
        result = _check_server_delay_uncached(
            server_id, update_state=False, checked_by=checked_by,
            source=source, test_url=test_url, timeout_ms=timeout_ms,
            expected_selection_revision=expected_selection_revision,
        )
        _record_probe_outcome(key, str(result.get("status") or "unknown"))
        return result

    try:
        if force_refresh:
            result = _check_server_delay_uncached(
                server_id, update_state=False, checked_by=checked_by,
                source=source, test_url=test_url, timeout_ms=timeout_ms,
                expected_selection_revision=expected_selection_revision,
            )
        else:
            cached, reused = get_live_probe_cache_many(
                [key],
                ttl_seconds=_AUTOMATIC_PROBE_SUCCESS_TTL_SECONDS,
                loader=lambda keys: {candidate_key: load_one() for candidate_key in keys},
                wait_timeout_seconds=max(1.0, float(timeout_ms) / 1000.0 + 1.0),
                include_cached_keys=True,
            )
            if key not in cached:
                raise TimeoutError("matching health probe is still running")
            result = dict(cached[key])
            result["probe_cache_reused"] = key in reused
    except TimeoutError:
        return {**_deferred_probe_result(server_id, target, checked_by),
                "error_code": "VPN_HEALTH_PROBE_ALREADY_IN_PROGRESS",
                "error_message": "A matching automatic health check is still running."}

    pending = result.pop("pending_observation", None)
    if expected_selection_revision is not None:
        with db_session() as connection:
            fence = read_selection_fence(connection)
        current_key, _, _, _ = _automatic_probe_context(
            server_id, test_url=test_url, timeout_ms=timeout_ms,
            selection_revision=expected_selection_revision,
        )
        if fence.get("revision") != expected_selection_revision or current_key != key:
            return {**result, "status": "unknown", "ok": False, "stale_observation": True,
                    "last_ping_ms": None, "latency_ms": None, "updated_state": False}

    if update_state and result.get("checked_at"):
        if pending:
            persistence = persist_logical_server_probe_result(
                server_id, pending, adapter_id=_adapter_id, probe_reason=checked_by,
                probe_lane=ping_source, expected_selection_revision=expected_selection_revision,
            )
            if not persistence.get("ok"):
                return {**result, "status": "unknown", "ok": False, "stale_observation": True,
                        "last_ping_ms": None, "latency_ms": None, "updated_state": False}
        persisted = record_ping_result(
            server_id=server_id, status=str(result.get("status") or "unknown"),
            latency_ms=result.get("latency_ms"), runtime_target=target,
            source=str(result.get("source") or ping_source), checked_at=str(result["checked_at"]),
            checked_by=str(result.get("checked_by") or checked_by),
            error_code=result.get("error_code"), error_message=result.get("error_message"),
            metadata={"adapter": _adapter_id, "test_url": test_url, "timeout_ms": timeout_ms},
            expected_selection_revision=expected_selection_revision,
        )
        result["updated_state"] = persisted.get("updated_state", False)
        if persisted.get("stale_observation"):
            result.update(ok=False, status="unknown", last_ping_ms=None, latency_ms=None, stale_observation=True)
    return result


def _check_server_delay_uncached(
    server_id: str,
    *,
    update_state: bool = False,
    checked_by: str = "manual",
    source: str | None = None,
    test_url: str = DEFAULT_TEST_URL,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    expected_selection_revision: int | None = None,
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
        persist_observation=expected_selection_revision is None,
        expected_selection_revision=expected_selection_revision,
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
        "pending_observation": delay.get("pending_observation"),
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
    expected_selection_revision: int | None = None,
) -> list[dict[str, Any]]:
    ordered_ids = list(dict.fromkeys(str(item) for item in server_ids if str(item).strip()))
    ping_source = _normalize_ping_source(source, checked_by=checked_by)
    if ping_source != "manual" and expected_selection_revision is None:
        try:
            with db_session() as connection:
                expected_selection_revision = read_selection_fence(connection).get("revision")
        except Exception:
            expected_selection_revision = None
    adapter = active_runtime_adapter(RUNTIME_ROLE_VPN_DATAPLANE)
    operations = runtime_adapter_operations(adapter)
    incarnation_reader = getattr(operations, "runtime_incarnation", None)
    try:
        batch_incarnation = str(incarnation_reader() or "") if callable(incarnation_reader) else ""
    except Exception:
        batch_incarnation = ""
    adapter_context = (str(adapter.get("adapter_id") or "unknown"), batch_incarnation)
    contexts = {
        server_id: (
            "server_ping.manual." + hashlib.sha256(server_id.encode("utf-8")).hexdigest(),
            resolve_server_runtime_target(server_id)["runtime_target"],
            "unknown",
            "",
        ) if ping_source == "manual" else
        _automatic_probe_context(
            server_id, test_url=test_url, timeout_ms=timeout_ms,
            adapter_context=adapter_context,
            selection_revision=expected_selection_revision,
        )
        for server_id in ordered_ids
    }
    targets = [contexts[server_id][1] for server_id in ordered_ids]
    key_by_id = {server_id: contexts[server_id][0] for server_id in ordered_ids}
    id_by_key = {key: server_id for server_id, key in key_by_id.items()}

    def load_results(keys_to_load: list[str]) -> dict[str, dict[str, Any]]:
        probe_ids = [id_by_key[key] for key in keys_to_load if not _probe_backoff_active(key)]
        results_by_key = {
            key_by_id[server_id]: _deferred_probe_result(
                server_id, contexts[server_id][1], checked_by,
            )
            for server_id in ordered_ids
            if key_by_id[server_id] in keys_to_load and key_by_id[server_id] not in {key_by_id[item] for item in probe_ids}
        }
        if not probe_ids:
            return results_by_key
        batch = check_logical_server_delays(
            probe_ids,
            test_url=test_url,
            timeout_ms=timeout_ms,
            probe_reason=checked_by,
            probe_lane=ping_source,
            persist_observation=False,
            expected_selection_revision=expected_selection_revision,
        )
        if batch is None:
            check_one = (
                _check_server_delay_uncached if fallback_check in (None, check_server_delay) else fallback_check
            )
            raw_results = [
                check_one(
                    server_id,
                    update_state=False,
                    checked_by=checked_by,
                    source=source,
                    test_url=test_url,
                    timeout_ms=timeout_ms,
                    expected_selection_revision=expected_selection_revision,
                )
                for server_id in probe_ids
            ]
            checked_at = _utc_timestamp()
            normalized = [
                {
                    **result,
                    "server_id": server_id,
                    "runtime_target": result.get("runtime_target") or contexts[server_id][1],
                    "mihomo_target": result.get("mihomo_target") or result.get("runtime_target") or contexts[server_id][1],
                    "source": result.get("source") or ping_source,
                    "checked_by": result.get("checked_by") or checked_by,
                    "status": result.get("status") or ("success" if result.get("ok") else "failed"),
                    "latency_ms": result.get("latency_ms", result.get("last_ping_ms")),
                    "last_ping_ms": result.get("last_ping_ms", result.get("latency_ms")),
                    "latency_label": result.get("latency_label") or _latency_label(result.get("latency_ms", result.get("last_ping_ms"))),
                    "test_url": result.get("test_url") or test_url,
                    "timeout_ms": result.get("timeout_ms") or timeout_ms,
                    "checked_at": result.get("checked_at") or checked_at,
                    "updated_state": False,
                    "pending_observation": result.get("pending_observation"),
                }
                for server_id, result in zip(probe_ids, raw_results)
            ]
            for server_id, result in zip(probe_ids, normalized):
                if ping_source != "manual":
                    _record_probe_outcome(key_by_id[server_id], str(result.get("status") or "unknown"))
                results_by_key[key_by_id[server_id]] = result
            return results_by_key

        checked_at = _utc_timestamp()
        for server_id, delay in zip(probe_ids, batch):
            runtime_target = contexts[server_id][1]
            status = "success" if delay.get("ok") else "failed"
            result = {
                "ok": bool(delay.get("ok")),
                "server_id": server_id,
                "runtime_target": runtime_target,
                "mihomo_target": runtime_target,
                "source": ping_source,
                "status": status,
                "latency_ms": delay.get("latency_ms"),
                "last_ping_ms": delay.get("latency_ms"),
                "checked_at": checked_at,
                "latency_label": _latency_label(delay.get("latency_ms")),
                "checked_by": checked_by,
                "test_url": test_url,
                "timeout_ms": timeout_ms,
                "error_code": delay.get("error_code"),
                "error_message": delay.get("error_message"),
                "updated_state": False,
                "active_member_id": delay.get("member_id"),
                "active_member_runtime_name": delay.get("member_runtime_name"),
                "probe_backend": delay.get("probe_backend"),
                "runtime_adapter_id": delay.get("runtime_adapter_id"),
                "probe_details": delay,
            }
            if ping_source != "manual":
                _record_probe_outcome(key_by_id[server_id], status)
            results_by_key[key_by_id[server_id]] = result
        return results_by_key

    if ping_source == "manual" or not ordered_ids:
        results_by_key = load_results([key_by_id[item] for item in ordered_ids])
        results = [results_by_key[key_by_id[item]] for item in ordered_ids]
    else:
        # Share the established bounded probe cache for a short interval. Its
        # per-key in-flight coordination coalesces simultaneous selector,
        # watchdog and refresh requests with the same target/context snapshot.
        try:
            cached_results, reused_keys = get_live_probe_cache_many(
                [key_by_id[item] for item in ordered_ids],
                ttl_seconds=2.0,
                loader=load_results,
                wait_timeout_seconds=max(1.0, float(timeout_ms) / 1000.0 + 1.0),
                include_cached_keys=True,
            )
            # Cache values are shared between callers. Never mutate the cached
            # dictionaries while decorating this caller's result.
            results = [dict(cached_results[key_by_id[item]]) for item in ordered_ids if key_by_id[item] in cached_results]
            missing_keys = {key_by_id[item] for item in ordered_ids} - set(cached_results)
            for key in missing_keys:
                item = id_by_key[key]
                cached_results[key] = {
                    **_deferred_probe_result(item, contexts[item][1], checked_by),
                    "error_code": "VPN_HEALTH_PROBE_ALREADY_IN_PROGRESS",
                    "error_message": "A matching automatic health check is still running.",
                }
            results = [dict(cached_results[key_by_id[item]]) for item in ordered_ids]
            for result, server_id in zip(results, ordered_ids):
                result["probe_cache_reused"] = key_by_id[server_id] in reused_keys
        except TimeoutError:
            return [
                {
                    "ok": False,
                    "server_id": server_id,
                    "runtime_target": target,
                    "mihomo_target": target,
                    "source": ping_source,
                    "status": "unknown",
                    "latency_ms": None,
                    "last_ping_ms": None,
                    "checked_at": None,
                    "latency_label": "n/a",
                    "checked_by": checked_by,
                    "error_code": "VPN_HEALTH_PROBE_ALREADY_IN_PROGRESS",
                    "error_message": "A matching automatic health check is still running.",
                    "updated_state": False,
                }
                for server_id, target in zip(ordered_ids, targets)
            ]

    observation_stale = False
    if expected_selection_revision is not None:
        try:
            with db_session() as connection:
                current_fence = read_selection_fence(connection)
            observation_stale = current_fence.get("revision") != expected_selection_revision
        except (ValueError, KeyError, TypeError):
            observation_stale = True
    try:
        current_adapter = active_runtime_adapter(RUNTIME_ROLE_VPN_DATAPLANE)
        current_operations = runtime_adapter_operations(current_adapter)
        current_incarnation_reader = getattr(current_operations, "runtime_incarnation", None)
        current_incarnation = str(current_incarnation_reader() or "") if callable(current_incarnation_reader) else ""
        observation_stale = observation_stale or (
            str(current_adapter.get("adapter_id") or "unknown") != adapter_context[0]
            or current_incarnation != batch_incarnation
        )
    except Exception:
        observation_stale = True
    for result in results:
        target_changed = resolve_server_runtime_target(result["server_id"])["runtime_target"] != result["runtime_target"]
        if observation_stale or target_changed:
            result["stale_observation"] = True
            result["updated_state"] = False

    if update_state and not observation_stale:
        for result in results:
            if not result.get("checked_at") or result.get("stale_observation"):
                result["updated_state"] = False
                continue
            pending = result.get("pending_observation")
            if pending:
                persisted = persist_logical_server_probe_result(
                    result["server_id"], pending, adapter_id=adapter_context[0],
                    probe_reason=checked_by, probe_lane=ping_source,
                    expected_selection_revision=expected_selection_revision,
                )
                if not persisted.get("ok"):
                    result.update(status="unknown", ok=False, stale_observation=True, updated_state=False)
                    continue
            details = result.get("probe_details")
            metadata = {
                "adapter": result.get("runtime_adapter_id"),
                "test_url": test_url,
                "timeout_ms": timeout_ms,
                "checked_at": result["checked_at"],
                "runtime_target": result["runtime_target"],
                "mihomo_target": result["runtime_target"],
                "active_member_id": result.get("active_member_id"),
                "active_member_runtime_name": result.get("active_member_runtime_name"),
                "details": details or {
                    "error_code": result.get("error_code"),
                    "probe_backend": result.get("probe_backend"),
                },
            }
            persisted_ping = record_ping_result(
                server_id=result["server_id"], status=result["status"],
                latency_ms=result.get("latency_ms"), runtime_target=result["runtime_target"],
                source=ping_source, checked_at=result["checked_at"], checked_by=checked_by,
                error_code=result.get("error_code"), error_message=result.get("error_message"),
                metadata=metadata, expected_selection_revision=expected_selection_revision,
            )
            result["updated_state"] = persisted_ping.get("updated_state", False)
            if persisted_ping.get("stale_observation"):
                result["stale_observation"] = True
    for result in results:
        result.pop("probe_details", None)
        result.pop("pending_observation", None)
        if result.get("stale_observation"):
            result.update(ok=False, status="unknown", last_ping_ms=None, latency_ms=None)
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
