from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from threading import Event, Lock, Thread
from typing import Any

from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import db_session
from fwrouter_api.services.core_bypass import is_core_bypass_enabled
from fwrouter_api.services.live_probe_cache import get_live_probe_cache
from fwrouter_api.services.logs import write_operational_log, write_technical_log
from fwrouter_api.services.runtime_convergence import get_last_runtime_convergence_status
from fwrouter_api.services.runtime_adapters import active_vpn_dataplane_adapter
from fwrouter_api.services.servers import (
    ensure_routing_global_state,
    set_global_mode,
)
from fwrouter_api.services.subject_policy import list_subjects_with_effective_state
from fwrouter_api.services.subject_taxonomy import (
    subject_follows_global_mode,
    watchdog_nft_subject_counter_prefixes,
)
from fwrouter_api.services.vpn_runtime_control import get_vpn_runtime_controller


DEFAULT_WATCHDOG_TIMEOUT_MS = 10000
DEFAULT_WATCHDOG_CANDIDATE_LIMIT = 4
DEFAULT_WATCHDOG_ACTIVE_CHECK_TTL_SECONDS = 60
SCOPED_VPN_SUBJECTS_CACHE_TTL_SECONDS = 30
VPN_AUTO_STATE_CACHE_TTL_SECONDS = 45
WATCHDOG_NFT_SUBJECT_COUNTER_PREFIXES = watchdog_nft_subject_counter_prefixes()

WATCHDOG_RUNTIME_RUNNING = "running"
WATCHDOG_RUNTIME_PAUSED = "paused"
WATCHDOG_RUNTIME_DEGRADED = "degraded"
WATCHDOG_RUNTIME_STOPPED = "stopped"
WATCHDOG_RUNTIME_FAILED = "failed"

_WATCHDOG_THREAD: Thread | None = None
_WATCHDOG_STOP_EVENT = Event()
_WATCHDOG_LOCK = Lock()
_WATCHDOG_FAILURE_LOG_LOCK = Lock()
_WATCHDOG_LAST_FAILURE_FINGERPRINT: str | None = None
_WATCHDOG_LAST_FAILURE_LOGGED_AT: datetime | None = None
_WATCHDOG_ISSUE_LOGGED_AT_BY_FINGERPRINT: dict[str, datetime] = {}
WATCHDOG_FAILURE_LOG_SUPPRESSION_SECONDS = 300
_WATCHDOG_TRAFFIC_FAILURE_LOCK = Lock()
_WATCHDOG_TRAFFIC_FAILURE_CANDIDATE: dict[str, Any] | None = None
WATCHDOG_STATE_ROW_ID = 1
_UNSET = object()


def _active_watchdog_vpn_adapter() -> dict[str, Any]:
    try:
        return active_vpn_dataplane_adapter()
    except Exception as exc:
        return {
            "role": "vpn_dataplane",
            "adapter_id": "unknown",
            "lifecycle_mode": "unknown",
            "ready": False,
            "source": {},
            "reason": "vpn_adapter_probe_failed",
            "error_message": str(exc),
        }


def _watchdog_adapter_subject(adapter: dict[str, Any], routing: dict[str, Any] | None = None) -> str | None:
    source = adapter.get("source") if isinstance(adapter.get("source"), dict) else {}
    return (
        str(source.get("system_id") or source.get("module") or "").strip()
        or str((routing or {}).get("active_auto_server_id") or "").strip()
        or None
    )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_timestamp() -> str:
    return _utc_now().isoformat()


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except ValueError:
        return None


def _load_watchdog_module() -> dict[str, Any] | None:
    with db_session() as connection:
        row = connection.execute(
            """
            SELECT
                module_name,
                desired_state,
                runtime_state,
                apply_state,
                status_text,
                error_code,
                error_message,
                updated_at
            FROM modules
            WHERE module_name = 'watchdog'
            """
        ).fetchone()

    return dict(row) if row is not None else None


def _update_watchdog_module(
    *,
    runtime_state: str,
    status_text: str,
    error_code: str | None = None,
    error_message: str | None = None,
) -> dict[str, Any] | None:
    with db_session() as connection:
        connection.execute(
            """
            UPDATE modules
            SET
                runtime_state = ?,
                apply_state = 'clean',
                status_text = ?,
                error_code = ?,
                error_message = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE module_name = 'watchdog'
            """,
            (runtime_state, status_text, error_code, error_message),
        )

    return _load_watchdog_module()


def _load_routing_state() -> dict[str, Any] | None:
    with db_session() as connection:
        row = connection.execute(
            """
            SELECT
                desired_mode,
                applied_mode,
                server_mode,
                active_auto_server_id
            FROM routing_global_state
            WHERE id = 1
            """
        ).fetchone()

    if row is None:
        return ensure_routing_global_state()
    return dict(row)


def _routing_mode(routing: dict[str, Any] | None) -> str:
    state = routing or {}
    # Watchdog must be proactive and check the DESIRED mode, not the APPLIED one.
    return str(state.get("desired_mode") or state.get("applied_mode") or "direct")


def _compute_has_scoped_vpn_subjects() -> bool:
    subjects = list_subjects_with_effective_state(
        is_active=True,
        include_deleted=False,
        limit=1000,
    )
    for subject in subjects:
        subject_type = str(subject.get("subject_type") or "").strip().lower()
        if not subject_follows_global_mode(subject_type):
            continue
        effective_state = subject.get("effective_state")
        if not isinstance(effective_state, dict):
            continue
        effective_mode = str(effective_state.get("effective_mode") or "").strip().lower()
        dataplane_path = str(effective_state.get("dataplane_path") or "").strip().lower()
        if effective_mode in {"vpn", "selective"} or dataplane_path in {"vpn", "selective"}:
            return True
    return False


def _has_scoped_vpn_subjects() -> bool:
    return bool(
        get_live_probe_cache(
            "watchdog.has_scoped_vpn_subjects",
            ttl_seconds=SCOPED_VPN_SUBJECTS_CACHE_TTL_SECONDS,
            loader=_compute_has_scoped_vpn_subjects,
        )
    )


def _reset_watchdog_traffic_failure_candidate() -> None:
    global _WATCHDOG_TRAFFIC_FAILURE_CANDIDATE
    with _WATCHDOG_TRAFFIC_FAILURE_LOCK:
        _WATCHDOG_TRAFFIC_FAILURE_CANDIDATE = None
    _update_watchdog_runtime_state(
        path_key=None,
        failure_candidate=None,
        last_processed_decision_id=None,
    )


def _json_loads_dict(value: str | None) -> dict[str, Any] | None:
    if not value:
        return None
    try:
        loaded = json.loads(value)
    except json.JSONDecodeError:
        return None
    return loaded if isinstance(loaded, dict) else None


def _json_dumps_dict(value: dict[str, Any] | None) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _ensure_watchdog_runtime_state_row() -> None:
    with db_session() as connection:
        connection.execute(
            """
            INSERT OR IGNORE INTO watchdog_state (id)
            VALUES (1)
            """
        )


def _load_watchdog_runtime_state() -> dict[str, Any]:
    try:
        _ensure_watchdog_runtime_state_row()
        with db_session() as connection:
            row = connection.execute(
                """
                SELECT
                    id,
                    path_key,
                    failure_candidate_json,
                    last_processed_decision_id,
                    last_successful_failover_at,
                    failover_path_key,
                    previous_target_id,
                    selected_target_id,
                    cooldown_until,
                    updated_at
                FROM watchdog_state
                WHERE id = 1
                """
            ).fetchone()
    except Exception:
        return {
            "id": WATCHDOG_STATE_ROW_ID,
            "path_key": None,
            "failure_candidate": None,
            "last_processed_decision_id": None,
            "last_successful_failover_at": None,
            "failover_path_key": None,
            "previous_target_id": None,
            "selected_target_id": None,
            "cooldown_until": None,
            "updated_at": None,
        }
    if row is None:
        return {
            "id": WATCHDOG_STATE_ROW_ID,
            "path_key": None,
            "failure_candidate": None,
            "last_processed_decision_id": None,
            "last_successful_failover_at": None,
            "failover_path_key": None,
            "previous_target_id": None,
            "selected_target_id": None,
            "cooldown_until": None,
            "updated_at": None,
        }
    return {
        "id": row["id"],
        "path_key": row["path_key"],
        "failure_candidate": _json_loads_dict(row["failure_candidate_json"]),
        "last_processed_decision_id": row["last_processed_decision_id"],
        "last_successful_failover_at": row["last_successful_failover_at"],
        "failover_path_key": row["failover_path_key"],
        "previous_target_id": row["previous_target_id"],
        "selected_target_id": row["selected_target_id"],
        "cooldown_until": row["cooldown_until"],
        "updated_at": row["updated_at"],
    }


def _update_watchdog_runtime_state(
    *,
    path_key: Any = _UNSET,
    failure_candidate: Any = _UNSET,
    last_processed_decision_id: Any = _UNSET,
    last_successful_failover_at: Any = _UNSET,
    failover_path_key: Any = _UNSET,
    previous_target_id: Any = _UNSET,
    selected_target_id: Any = _UNSET,
    cooldown_until: Any = _UNSET,
) -> dict[str, Any]:
    try:
        _ensure_watchdog_runtime_state_row()
        assignments: list[str] = []
        params: list[Any] = []
        if path_key is not _UNSET:
            assignments.append("path_key = ?")
            params.append(path_key)
        if failure_candidate is not _UNSET:
            assignments.append("failure_candidate_json = ?")
            params.append(_json_dumps_dict(failure_candidate))
        if last_processed_decision_id is not _UNSET:
            assignments.append("last_processed_decision_id = ?")
            params.append(last_processed_decision_id)
        if last_successful_failover_at is not _UNSET:
            assignments.append("last_successful_failover_at = ?")
            params.append(last_successful_failover_at)
        if failover_path_key is not _UNSET:
            assignments.append("failover_path_key = ?")
            params.append(failover_path_key)
        if previous_target_id is not _UNSET:
            assignments.append("previous_target_id = ?")
            params.append(previous_target_id)
        if selected_target_id is not _UNSET:
            assignments.append("selected_target_id = ?")
            params.append(selected_target_id)
        if cooldown_until is not _UNSET:
            assignments.append("cooldown_until = ?")
            params.append(cooldown_until)
        if not assignments:
            return _load_watchdog_runtime_state()
        assignments.append("updated_at = CURRENT_TIMESTAMP")
        params.append(WATCHDOG_STATE_ROW_ID)
        with db_session() as connection:
            connection.execute(
                f"UPDATE watchdog_state SET {', '.join(assignments)} WHERE id = ?",
                tuple(params),
            )
    except Exception:
        return _load_watchdog_runtime_state()
    return _load_watchdog_runtime_state()


def _watchdog_traffic_failure_confirmation(
    *,
    active_server_id: str | None,
    traffic_signal: dict[str, Any],
    confirm_seconds: int,
    path_key: str | None = None,
) -> dict[str, Any]:
    """Debounce traffic-only watchdog failure detection across fresh snapshots."""

    global _WATCHDOG_TRAFFIC_FAILURE_CANDIDATE

    normalized_server_id = str(active_server_id or "").strip()
    normalized_path_key = str(path_key or normalized_server_id or "").strip()
    collected_at = str(traffic_signal.get("last_collected_at") or "").strip()
    decision_id = str(traffic_signal.get("decision_id") or collected_at or "").strip()
    now = _utc_now()
    threshold = max(30, int(confirm_seconds or 60))

    if not normalized_path_key or not collected_at or not decision_id or not bool(traffic_signal.get("traffic_stalled")):
        _reset_watchdog_traffic_failure_candidate()
        return {
            "confirmed": False,
            "pending": False,
            "reason": "traffic_not_stalled",
            "confirm_seconds": threshold,
        }

    with _WATCHDOG_TRAFFIC_FAILURE_LOCK:
        state = _load_watchdog_runtime_state()
        candidate = state.get("failure_candidate")
        if not isinstance(candidate, dict):
            candidate = _WATCHDOG_TRAFFIC_FAILURE_CANDIDATE
        if (
            not isinstance(candidate, dict)
            or candidate.get("path_key") != normalized_path_key
        ):
            candidate = {
                "path_key": normalized_path_key,
                "server_id": normalized_server_id,
                "first_seen_at": now.isoformat(),
                "last_collected_at": collected_at,
                "decision_id": decision_id,
                "traffic_signal": {
                    "total_rx_delta": traffic_signal.get("total_rx_delta"),
                    "total_tx_delta": traffic_signal.get("total_tx_delta"),
                    "active_samples_count": traffic_signal.get("active_samples_count"),
                },
            }
            _WATCHDOG_TRAFFIC_FAILURE_CANDIDATE = candidate
            _update_watchdog_runtime_state(
                path_key=normalized_path_key,
                failure_candidate=candidate,
                last_processed_decision_id=decision_id,
            )
            return {
                "confirmed": False,
                "pending": True,
                "reason": "first_stalled_traffic_snapshot",
                "path_key": normalized_path_key,
                "server_id": normalized_server_id,
                "first_seen_at": now.isoformat(),
                "last_collected_at": collected_at,
                "decision_id": decision_id,
                "confirm_seconds": threshold,
            }

        if candidate.get("decision_id") == decision_id:
            first_seen_at = _parse_timestamp(str(candidate.get("first_seen_at") or ""))
            if first_seen_at is None:
                first_seen_at = now
                candidate["first_seen_at"] = first_seen_at.isoformat()
            return {
                "confirmed": False,
                "pending": True,
                "reason": "same_stalled_traffic_snapshot",
                "path_key": normalized_path_key,
                "server_id": normalized_server_id,
                "first_seen_at": first_seen_at.isoformat(),
                "last_collected_at": collected_at,
                "decision_id": decision_id,
                "age_seconds": max(0, int((now - first_seen_at).total_seconds())),
                "confirm_seconds": threshold,
            }

        first_seen_at = _parse_timestamp(str(candidate.get("first_seen_at") or ""))
        if first_seen_at is None:
            first_seen_at = now
        age_seconds = max(0, int((now - first_seen_at).total_seconds()))
        candidate["last_collected_at"] = collected_at
        candidate["decision_id"] = decision_id
        candidate["latest_signal"] = {
            "total_rx_delta": traffic_signal.get("total_rx_delta"),
            "total_tx_delta": traffic_signal.get("total_tx_delta"),
            "active_samples_count": traffic_signal.get("active_samples_count"),
        }
        _WATCHDOG_TRAFFIC_FAILURE_CANDIDATE = candidate
        _update_watchdog_runtime_state(
            path_key=normalized_path_key,
            failure_candidate=candidate,
            last_processed_decision_id=decision_id,
        )

        if age_seconds < threshold:
            return {
                "confirmed": False,
                "pending": True,
                "reason": "stalled_traffic_confirmation_window",
                "path_key": normalized_path_key,
                "server_id": normalized_server_id,
                "first_seen_at": first_seen_at.isoformat(),
                "last_collected_at": collected_at,
                "decision_id": decision_id,
                "age_seconds": age_seconds,
                "confirm_seconds": threshold,
            }

        _WATCHDOG_TRAFFIC_FAILURE_CANDIDATE = None
        _update_watchdog_runtime_state(
            path_key=normalized_path_key,
            failure_candidate=None,
            last_processed_decision_id=decision_id,
        )
        return {
            "confirmed": True,
            "pending": False,
            "reason": "stalled_traffic_confirmed",
            "path_key": normalized_path_key,
            "server_id": normalized_server_id,
            "first_seen_at": first_seen_at.isoformat(),
            "last_collected_at": collected_at,
            "decision_id": decision_id,
            "age_seconds": age_seconds,
            "confirm_seconds": threshold,
        }


def _watchdog_failover_cooldown_status() -> dict[str, Any]:
    state = _load_watchdog_runtime_state()
    cooldown_until = _parse_timestamp(str(state.get("cooldown_until") or ""))
    now = _utc_now()
    active = bool(cooldown_until and cooldown_until > now)
    return {
        "active": active,
        "cooldown_until": cooldown_until.isoformat() if cooldown_until is not None else None,
        "remaining_seconds": max(0, int((cooldown_until - now).total_seconds())) if active and cooldown_until else 0,
        "state": state,
    }


def _record_watchdog_successful_failover(
    *,
    path_key: str | None,
    previous_target_id: str | None,
    selected_target_id: str | None,
    cooldown_seconds: int,
) -> dict[str, Any]:
    now = _utc_now()
    cooldown_until = now + timedelta(seconds=max(30, int(cooldown_seconds or 300)))
    return _update_watchdog_runtime_state(
        path_key=path_key,
        failure_candidate=None,
        last_successful_failover_at=now.isoformat(),
        failover_path_key=path_key,
        previous_target_id=previous_target_id,
        selected_target_id=selected_target_id,
        cooldown_until=cooldown_until.isoformat(),
    )


def _watchdog_cooldown_fields(cooldown: dict[str, Any] | None) -> dict[str, Any]:
    state = cooldown if isinstance(cooldown, dict) else {}
    return {
        "cooldown_active": bool(state.get("active")),
        "cooldown_until": state.get("cooldown_until"),
        "cooldown_remaining_seconds": int(state.get("remaining_seconds") or 0),
    }


def _watchdog_runtime_response_fields(
    *,
    runtime_state: dict[str, Any],
    path_key: str | None = None,
    cooldown: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "path_key": path_key or runtime_state.get("path_key"),
        "selection_mode": runtime_state.get("selection_mode"),
        "active_target_id": runtime_state.get("active_target_id"),
        "failover_supported": bool(runtime_state.get("failover_supported")),
        **_watchdog_cooldown_fields(cooldown),
    }


def _recent_successful_active_check(
    *,
    server_id: str | None,
    ttl_seconds: int = DEFAULT_WATCHDOG_ACTIVE_CHECK_TTL_SECONDS,
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


def _watchdog_active_probe_degraded(active_check: dict[str, Any] | None) -> bool:
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
    return latency_ms > get_settings().watchdog_active_probe_max_latency_ms


def _watchdog_degraded_active_check(active_check: dict[str, Any]) -> dict[str, Any]:
    if not bool(active_check.get("ok")):
        return active_check
    degraded = dict(active_check)
    latency_ms = degraded.get("last_ping_ms")
    threshold_ms = get_settings().watchdog_active_probe_max_latency_ms
    degraded["ok"] = False
    degraded["status"] = "degraded_latency"
    degraded["error_code"] = "WATCHDOG_ACTIVE_LATENCY_DEGRADED"
    degraded["error_message"] = (
        f"Active VPN server latency {latency_ms} ms exceeds watchdog threshold {threshold_ms} ms."
    )
    return degraded


def detect_recent_vpn_traffic_attempts(
    *,
    window_seconds: int | None = None,
) -> dict[str, Any]:
    settings = get_settings()
    resolved_window = window_seconds or settings.watchdog_traffic_window_seconds
    cutoff_dt = _utc_now() - timedelta(seconds=resolved_window)
    cutoff = cutoff_dt.isoformat()

    with db_session() as connection:
        rows = connection.execute(
            """
            SELECT
                counter_key,
                subject_id,
                path,
                rx_bytes,
                tx_bytes,
                collected_at,
                metadata_json
            FROM traffic_counter_snapshots
            WHERE path = 'vpn'
              AND collected_at >= ?
            ORDER BY collected_at DESC
            LIMIT 200
            """,
            (cutoff,),
        ).fetchall()

    samples: list[dict[str, Any]] = []
    ignored_samples: list[dict[str, Any]] = []
    total_rx_delta = 0
    total_tx_delta = 0
    dataplane_rx_delta = 0
    dataplane_tx_delta = 0
    adapter_rx_delta = 0
    adapter_tx_delta = 0
    for row in rows:
        metadata = json.loads(row["metadata_json"]) if row["metadata_json"] else {}
        rx_delta = int(metadata.get("rx_delta") or 0)
        tx_delta = int(metadata.get("tx_delta") or 0)
        activity_observed = bool(metadata.get("activity_observed")) or rx_delta > 0 or tx_delta > 0
        sample = {
            "counter_key": row["counter_key"],
            "subject_id": row["subject_id"],
            "collected_at": row["collected_at"],
            "rx_delta": rx_delta,
            "tx_delta": tx_delta,
            "activity_observed": activity_observed,
            "metadata": metadata,
        }
        signal_kind = _watchdog_traffic_sample_kind(sample)
        if signal_kind is not None:
            total_rx_delta += rx_delta
            total_tx_delta += tx_delta
            effective_rx_delta, effective_tx_delta = _watchdog_effective_sample_deltas(sample)
            if signal_kind == "adapter":
                adapter_rx_delta += effective_rx_delta
                adapter_tx_delta += effective_tx_delta
            else:
                dataplane_rx_delta += effective_rx_delta
                dataplane_tx_delta += effective_tx_delta
            samples.append(sample)
        else:
            ignored_samples.append(sample)

    current_observation = _build_watchdog_current_observation(
        samples,
        correlation_seconds=settings.watchdog_signal_correlation_seconds,
    )
    last_collected_at = samples[0]["collected_at"] if samples else None
    latest_samples = [
        sample for sample in samples
        if sample.get("collected_at") == last_collected_at
    ]
    latest_active_count = 0
    latest_rx_delta = 0
    latest_tx_delta = 0
    latest_dataplane_rx_delta = 0
    latest_dataplane_tx_delta = 0
    latest_adapter_rx_delta = 0
    latest_adapter_tx_delta = 0
    for sample in latest_samples:
        signal_kind = _watchdog_traffic_sample_kind(sample)
        if signal_kind is None:
            continue
        rx_delta = int(sample.get("rx_delta") or 0)
        tx_delta = int(sample.get("tx_delta") or 0)
        latest_rx_delta += rx_delta
        latest_tx_delta += tx_delta
        effective_rx_delta, effective_tx_delta = _watchdog_effective_sample_deltas(sample)
        if signal_kind == "adapter":
            latest_adapter_rx_delta += effective_rx_delta
            latest_adapter_tx_delta += effective_tx_delta
        else:
            latest_dataplane_rx_delta += effective_rx_delta
            latest_dataplane_tx_delta += effective_tx_delta
        if bool(sample.get("activity_observed")):
            latest_active_count += 1

    last_collected_age_seconds = None
    last_collected_dt = _parse_timestamp(last_collected_at)
    if last_collected_dt is not None:
        last_collected_age_seconds = max(
            0,
            int((_utc_now() - last_collected_dt).total_seconds()),
        )

    settings = get_settings()
    signal_stale = (
        last_collected_age_seconds is None
        or last_collected_age_seconds > max(settings.watchdog_traffic_window_seconds, resolved_window)
    )
    authoritative_response_source = str(current_observation.get("response_source") or "none")
    if current_observation.get("tx_observed"):
        authoritative_tx_delta = int(current_observation.get("authoritative_tx_delta") or 0)
        authoritative_rx_delta = int(current_observation.get("authoritative_rx_delta") or 0)
    else:
        authoritative_rx_delta = 0
        authoritative_tx_delta = 0
        authoritative_response_source = "none"
    path_state = str(current_observation.get("path_state") or "idle")

    return {
        "observed": latest_active_count > 0,
        "window_seconds": resolved_window,
        "source": "traffic_counter_snapshots",
        "checked_samples_count": len(samples),
        "ignored_samples_count": len(ignored_samples),
        "active_samples_count": latest_active_count,
        "latest_samples_count": len(latest_samples),
        "window_active_samples_count": sum(1 for sample in samples if bool(sample.get("activity_observed"))),
        "total_rx_delta": total_rx_delta,
        "total_tx_delta": total_tx_delta,
        "dataplane_rx_delta": dataplane_rx_delta,
        "dataplane_tx_delta": dataplane_tx_delta,
        "adapter_rx_delta": adapter_rx_delta,
        "adapter_tx_delta": adapter_tx_delta,
        "latest_rx_delta": latest_rx_delta,
        "latest_tx_delta": latest_tx_delta,
        "latest_dataplane_rx_delta": latest_dataplane_rx_delta,
        "latest_dataplane_tx_delta": latest_dataplane_tx_delta,
        "latest_adapter_rx_delta": latest_adapter_rx_delta,
        "latest_adapter_tx_delta": latest_adapter_tx_delta,
        "authoritative_rx_delta": authoritative_rx_delta,
        "authoritative_tx_delta": authoritative_tx_delta,
        "authoritative_response_source": authoritative_response_source,
        "response_observed": authoritative_rx_delta > 0,
        "outbound_observed": authoritative_tx_delta > 0,
        "traffic_stalled": authoritative_tx_delta > 0 and authoritative_rx_delta <= 0,
        "path_state": path_state if not signal_stale else "idle",
        "decision_id": current_observation.get("decision_id"),
        "current_observation": current_observation,
        "last_collected_at": last_collected_at,
        "last_collected_age_seconds": last_collected_age_seconds,
        "fresh": not signal_stale,
        "authoritative": not signal_stale,
        "signal_authority": "authoritative" if not signal_stale else "unavailable",
        "safe_for_watchdog_auto": not signal_stale,
        "samples": samples,
        "ignored_samples": ignored_samples[:20],
    }


def _watchdog_traffic_sample_kind(sample: dict[str, Any]) -> str | None:
    counter_key = str(sample.get("counter_key") or "")
    metadata = sample.get("metadata") if isinstance(sample.get("metadata"), dict) else {}
    source = str(metadata.get("source") or "")
    watchdog_signal = str(metadata.get("watchdog_signal") or "").strip().lower()
    connection_type = str(metadata.get("connection_type") or metadata.get("module_role") or "").strip().lower()

    if watchdog_signal == "dataplane":
        return "dataplane"
    if watchdog_signal in {"adapter_response", "external_vpn_module_response"} and (
        connection_type in {"external_vpn_module", "vpn_module"}
        or watchdog_signal == "external_vpn_module_response"
    ):
        return "adapter"

    if source == "nftables":
        if counter_key == "fwrouter:global:vpn":
            return "dataplane"
        if _watchdog_nft_named_vpn_counter_allowed(counter_key):
            return "dataplane"
    return None


def _watchdog_nft_named_vpn_counter_allowed(counter_key: str) -> bool:
    if not counter_key.startswith("nft:counter:cnt_"):
        return False
    if not (counter_key.endswith("_vpn_tx") or counter_key.endswith("_vpn_rx")):
        return False
    counter_name = counter_key[len("nft:counter:cnt_"):]
    return counter_name.startswith(WATCHDOG_NFT_SUBJECT_COUNTER_PREFIXES)


def _watchdog_effective_sample_deltas(sample: dict[str, Any]) -> tuple[int, int]:
    rx_delta = int(sample.get("rx_delta") or 0)
    tx_delta = int(sample.get("tx_delta") or 0)
    counter_key = str(sample.get("counter_key") or "")
    metadata = sample.get("metadata") if isinstance(sample.get("metadata"), dict) else {}
    source = str(metadata.get("source") or "")
    scope = str(metadata.get("scope") or "")

    if counter_key == "fwrouter:global:vpn" and source == "nftables" and scope == "global":
        return 0, rx_delta + tx_delta
    if source == "nftables" and _watchdog_nft_named_vpn_counter_allowed(counter_key):
        total_delta = rx_delta + tx_delta
        if counter_key.endswith("_vpn_tx"):
            return 0, total_delta
        if counter_key.endswith("_vpn_rx"):
            return total_delta, 0
    return rx_delta, tx_delta


def _watchdog_sample_collected_dt(sample: dict[str, Any]) -> datetime | None:
    return _parse_timestamp(str(sample.get("collected_at") or "").strip() or None)


def _watchdog_sample_identity(sample: dict[str, Any]) -> dict[str, Any]:
    effective_rx, effective_tx = _watchdog_effective_sample_deltas(sample)
    return {
        "counter_key": sample.get("counter_key"),
        "subject_id": sample.get("subject_id"),
        "collected_at": sample.get("collected_at"),
        "rx_delta": effective_rx,
        "tx_delta": effective_tx,
        "kind": _watchdog_traffic_sample_kind(sample),
    }


def _watchdog_observation_decision_id(
    *,
    tx_samples: list[dict[str, Any]],
    rx_samples: list[dict[str, Any]],
    correlation_seconds: int,
    path_state: str,
) -> str | None:
    if not tx_samples:
        return None
    payload = {
        "correlation_seconds": correlation_seconds,
        "path_state": path_state,
        "tx_samples": [_watchdog_sample_identity(sample) for sample in tx_samples],
        "rx_samples": [_watchdog_sample_identity(sample) for sample in rx_samples],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:24]


def _build_watchdog_current_observation(
    samples: list[dict[str, Any]],
    *,
    correlation_seconds: int,
) -> dict[str, Any]:
    """Build the bounded traffic observation used for the current health decision."""

    resolved_correlation = max(1, int(correlation_seconds or 30))
    dataplane_tx_samples: list[tuple[datetime, dict[str, Any], int]] = []
    for sample in samples:
        if _watchdog_traffic_sample_kind(sample) != "dataplane":
            continue
        collected_dt = _watchdog_sample_collected_dt(sample)
        if collected_dt is None:
            continue
        _effective_rx, effective_tx = _watchdog_effective_sample_deltas(sample)
        if effective_tx > 0:
            dataplane_tx_samples.append((collected_dt, sample, effective_tx))

    if not dataplane_tx_samples:
        return {
            "path_state": "idle",
            "tx_observed": False,
            "rx_observed": False,
            "authoritative_tx_delta": 0,
            "authoritative_rx_delta": 0,
            "response_source": "none",
            "correlation_seconds": resolved_correlation,
            "decision_id": None,
        }

    anchor_dt = max(item[0] for item in dataplane_tx_samples)
    correlated: list[tuple[datetime, dict[str, Any], str, int, int]] = []
    for sample in samples:
        collected_dt = _watchdog_sample_collected_dt(sample)
        if collected_dt is None:
            continue
        if abs((collected_dt - anchor_dt).total_seconds()) > resolved_correlation:
            continue
        signal_kind = _watchdog_traffic_sample_kind(sample)
        if signal_kind is None:
            continue
        effective_rx, effective_tx = _watchdog_effective_sample_deltas(sample)
        correlated.append((collected_dt, sample, signal_kind, effective_rx, effective_tx))

    tx_samples = [
        sample
        for collected_dt, sample, signal_kind, _effective_rx, effective_tx in correlated
        if signal_kind == "dataplane" and collected_dt == anchor_dt and effective_tx > 0
    ]
    authoritative_tx_delta = sum(
        effective_tx
        for collected_dt, _sample, signal_kind, _effective_rx, effective_tx in correlated
        if signal_kind == "dataplane" and collected_dt == anchor_dt and effective_tx > 0
    )
    dataplane_rx_items = [
        (collected_dt, sample, effective_rx)
        for collected_dt, sample, signal_kind, effective_rx, _effective_tx in correlated
        if signal_kind == "dataplane" and effective_rx > 0
    ]
    adapter_rx_items = [
        (collected_dt, sample, effective_rx)
        for collected_dt, sample, signal_kind, effective_rx, _effective_tx in correlated
        if signal_kind == "adapter" and effective_rx > 0
    ]

    if dataplane_rx_items:
        rx_items = dataplane_rx_items
        response_source = "dataplane"
    elif adapter_rx_items:
        rx_items = adapter_rx_items
        response_source = "adapter_fallback"
    else:
        rx_items = []
        response_source = "none"

    authoritative_rx_delta = sum(item[2] for item in rx_items)
    path_state = "healthy" if authoritative_tx_delta > 0 and authoritative_rx_delta > 0 else "suspected_failure"
    rx_samples = [item[1] for item in rx_items]
    rx_observed_at = max((item[0] for item in rx_items), default=None)
    decision_id = _watchdog_observation_decision_id(
        tx_samples=tx_samples,
        rx_samples=rx_samples,
        correlation_seconds=resolved_correlation,
        path_state=path_state,
    )

    return {
        "path_state": path_state,
        "tx_observed": authoritative_tx_delta > 0,
        "rx_observed": authoritative_rx_delta > 0,
        "authoritative_tx_delta": authoritative_tx_delta,
        "authoritative_rx_delta": authoritative_rx_delta,
        "response_source": response_source,
        "correlation_seconds": resolved_correlation,
        "tx_observed_at": anchor_dt.isoformat(),
        "rx_observed_at": rx_observed_at.isoformat() if rx_observed_at is not None else None,
        "decision_id": decision_id,
        "tx_samples": [_watchdog_sample_identity(sample) for sample in tx_samples],
        "rx_samples": [_watchdog_sample_identity(sample) for sample in rx_samples],
    }


def _paused_result(
    *,
    status: str,
    reason: str,
    message: str,
    module: dict[str, Any] | None,
    routing: dict[str, Any] | None,
    traffic_signal: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "ok": True,
        "automated": True,
        "status": status,
        "reason": reason,
        "traffic_attempts_observed": False,
        "allow_switch": False,
        "active_server_id": (routing or {}).get("active_auto_server_id"),
        "active_check": None,
        "selector": None,
        "action": "none",
        "message": message,
        "traffic_signal": traffic_signal,
        "module": module,
        "routing": routing,
    }


def _write_watchdog_operational_event(
    *,
    event_type: str,
    level: str,
    message: str,
    details: dict[str, Any],
) -> None:
    write_operational_log(
        event_type=event_type,
        level=level,
        subject_id=None,
        message=message,
        details=details,
    )


def _should_write_watchdog_issue_log(fingerprint: str) -> bool:
    global _WATCHDOG_LAST_FAILURE_FINGERPRINT, _WATCHDOG_LAST_FAILURE_LOGGED_AT

    now = _utc_now()
    with _WATCHDOG_FAILURE_LOG_LOCK:
        if (
            _WATCHDOG_LAST_FAILURE_FINGERPRINT == fingerprint
            and _WATCHDOG_LAST_FAILURE_LOGGED_AT is not None
            and (now - _WATCHDOG_LAST_FAILURE_LOGGED_AT).total_seconds()
            < WATCHDOG_FAILURE_LOG_SUPPRESSION_SECONDS
        ):
            return False

        last_for_fingerprint = _WATCHDOG_ISSUE_LOGGED_AT_BY_FINGERPRINT.get(fingerprint)
        if (
            last_for_fingerprint is not None
            and (now - last_for_fingerprint).total_seconds()
            < WATCHDOG_FAILURE_LOG_SUPPRESSION_SECONDS
        ):
            _WATCHDOG_LAST_FAILURE_FINGERPRINT = fingerprint
            _WATCHDOG_LAST_FAILURE_LOGGED_AT = last_for_fingerprint
            return False

        _WATCHDOG_LAST_FAILURE_FINGERPRINT = fingerprint
        _WATCHDOG_LAST_FAILURE_LOGGED_AT = now
        _WATCHDOG_ISSUE_LOGGED_AT_BY_FINGERPRINT[fingerprint] = now
        return True


def _compact_watchdog_traffic_signal(signal: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(signal, dict):
        return None
    keys = (
        "observed",
        "response_observed",
        "traffic_stalled",
        "authoritative",
        "safe_for_watchdog_auto",
        "last_collected_at",
        "last_fresh_sample_at",
        "rx_delta",
        "tx_delta",
    )
    return {key: signal.get(key) for key in keys if key in signal}


def _watchdog_decision_fingerprint(details: dict[str, Any]) -> str:
    return json.dumps(
        {
            "event_type": details.get("event_type"),
            "status": details.get("status"),
            "error_code": details.get("error_code"),
            "active_server_id": details.get("active_server_id"),
            "message": details.get("message") or details.get("error_message"),
            "selector_error": (
                details.get("selector", {}).get("error_message")
                if isinstance(details.get("selector"), dict)
                else None
            ),
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _write_watchdog_decision_log(
    *,
    level: str,
    event_type: str,
    message: str,
    result: dict[str, Any],
    error_code: str | None = None,
) -> None:
    details = {
        "event_type": event_type,
        "status": result.get("status"),
        "reason": result.get("reason"),
        "message": result.get("message") or message,
        "error_code": error_code or result.get("error_code"),
        "error_message": result.get("error_message") or result.get("message") or message,
        "active_server_id": result.get("active_server_id"),
        "allow_switch": result.get("allow_switch"),
        "action": result.get("action"),
        "traffic_signal": _compact_watchdog_traffic_signal(result.get("traffic_signal")),
        "traffic_failure_confirmation": result.get("traffic_failure_confirmation"),
        "selector": result.get("selector"),
        "timestamp": _utc_timestamp(),
    }
    fingerprint = _watchdog_decision_fingerprint(details)
    if not _should_write_watchdog_issue_log(fingerprint):
        return
    write_technical_log(
        component="watchdog",
        level=level,
        event_type=event_type,
        message=message,
        details=details,
    )


def run_vpn_watchdog_check(
    *,
    traffic_attempts_observed: bool = False,
    allow_switch: bool = False,
    update_ping_state: bool = True,
    timeout_ms: int = DEFAULT_WATCHDOG_TIMEOUT_MS,
    candidate_limit: int = DEFAULT_WATCHDOG_CANDIDATE_LIMIT,
    reason: str = "manual_watchdog_check",
    log_events: bool = False,
) -> dict[str, Any]:
    """Evaluate VPN watchdog state.

    This function intentionally does not treat "no traffic" as failure.
    A failure can only be evaluated when the caller tells us that attempts
    through the active VPN dataplane adapter were observed.

    With managed Mihomo it can check/switch vpn-auto. With an external VPN
    adapter it never calls Mihomo selector APIs.
    """

    vpn_adapter = _active_watchdog_vpn_adapter()
    runtime_controller = get_vpn_runtime_controller(vpn_adapter, routing=_load_routing_state())
    runtime_state = runtime_controller.get_state()
    active_server_id = str(runtime_state.get("active_target_id") or "").strip() or None
    if not bool(runtime_state.get("ready")):
        return {
            "ok": False,
            "status": "runtime_unavailable",
            "reason": reason,
            "traffic_attempts_observed": traffic_attempts_observed,
            "allow_switch": allow_switch,
            "active_server_id": active_server_id,
            "active_check": None,
            "selector": None,
            "action": "none",
            "vpn_adapter": vpn_adapter,
            "vpn_runtime": runtime_state,
            "path_key": runtime_state.get("path_key"),
            "error_code": "WATCHDOG_RUNTIME_UNAVAILABLE",
            "error_message": str(vpn_adapter.get("error_message") or "VPN dataplane adapter is not ready."),
            "message": "VPN runtime is unavailable; watchdog suppressed server switching.",
        }

    if not bool(runtime_state.get("failover_supported")) and not bool(runtime_state.get("probe_supported")):
        if not traffic_attempts_observed:
            result = {
                "ok": True,
                "status": "no_failure_no_traffic",
                "reason": reason,
                "traffic_attempts_observed": False,
                "allow_switch": False,
                "active_server_id": active_server_id or _watchdog_adapter_subject(vpn_adapter),
                "active_check": None,
                "selector": None,
                "action": "none",
                "vpn_adapter": vpn_adapter,
                "vpn_runtime": runtime_state,
                "path_key": runtime_state.get("path_key"),
                "message": "No VPN traffic attempts observed; watchdog does not treat idle external runtime as failure.",
            }
            if log_events:
                _write_watchdog_operational_event(
                    event_type="vpn_watchdog_no_traffic",
                    level="info",
                    message=result["message"],
                    details=result,
                )
            return result
        return {
            "ok": True,
            "status": "external_runtime_active",
            "reason": reason,
            "traffic_attempts_observed": True,
            "allow_switch": False,
            "active_server_id": active_server_id or _watchdog_adapter_subject(vpn_adapter),
            "active_check": {
                "ok": True,
                "status": "external_runtime_ready",
                "source": "vpn_dataplane_adapter",
            },
            "selector": None,
            "action": "none",
            "vpn_adapter": vpn_adapter,
            "vpn_runtime": runtime_state,
            "path_key": runtime_state.get("path_key"),
            "message": "External VPN runtime is active; watchdog did not run Mihomo selector checks.",
        }

    # If no server is active, we MUST select one to boot the system.
    # OR if we have traffic, we must check the active server's health.
    if active_server_id is None or traffic_attempts_observed:
        # Pass-through to the health check and failover logic
        pass
    else:
        # We have a server and no traffic, so assume it's idle and healthy.
        result = {
            "ok": True,
            "status": "no_failure_no_traffic",
            "reason": reason,
            "traffic_attempts_observed": False,
            "allow_switch": allow_switch,
            "active_server_id": active_server_id,
            "active_check": None,
            "selector": None,
            "action": "none",
            "vpn_adapter": vpn_adapter,
            "vpn_runtime": runtime_state,
            "path_key": runtime_state.get("path_key"),
            "message": "No VPN-auto traffic attempts observed; watchdog does not treat idle state as failure.",
        }
        if log_events:
            _write_watchdog_operational_event(
                event_type="vpn_watchdog_no_traffic",
                level="info",
                message=result["message"],
                details=result,
            )
        return result

    if active_server_id is None:
        selector_result = runtime_controller.initial_select(
            apply=allow_switch,
            reason=reason,
            update_ping_state=update_ping_state,
            candidate_limit=candidate_limit,
            timeout_ms=timeout_ms,
        )
        selector = selector_result.get("selector")
        return {
            "ok": bool(selector_result.get("ok")),
            "status": "initial_auto_selected" if allow_switch and selector_result.get("ok") else "needs_initial_auto_selection",
            "reason": reason,
            "traffic_attempts_observed": traffic_attempts_observed,
            "allow_switch": allow_switch,
            "active_server_id": selector_result.get("selected_target_id"),
            "active_check": None,
            "selector": selector,
            "action": selector_result.get("action") or "none",
            "vpn_adapter": vpn_adapter,
            "vpn_runtime": selector_result.get("runtime_state") or runtime_state,
            "path_key": (selector_result.get("runtime_state") or runtime_state).get("path_key"),
            "message": (
                "Watchdog bootstrap selected a valid vpn-auto server."
                if allow_switch and selector_result.get("ok")
                else "VPN-auto has no valid active server selected."
            ),
        }

    checked_by = f"watchdog_active_check:{reason}"
    active_check = _recent_successful_active_check(
        server_id=active_server_id,
        checked_by=checked_by,
        timeout_ms=timeout_ms,
    )
    if active_check is None:
        active_check = runtime_controller.probe(
            update_ping_state=update_ping_state,
            timeout_ms=timeout_ms,
            reason=reason,
        )
    if active_check is None:
        active_check = {
            "ok": False,
            "status": "probe_unavailable",
            "server_id": active_server_id,
            "error_code": "WATCHDOG_PROBE_UNAVAILABLE",
            "error_message": "Active VPN runtime does not expose an active probe.",
        }

    if active_check["ok"]:
        result = {
            "ok": True,
            "status": "healthy",
            "reason": reason,
            "traffic_attempts_observed": True,
            "allow_switch": allow_switch,
            "active_server_id": active_server_id,
            "active_check": active_check,
            "selector": None,
            "action": "none",
            "vpn_adapter": vpn_adapter,
            "vpn_runtime": runtime_state,
            "path_key": runtime_state.get("path_key"),
            "message": "VPN-auto traffic attempts observed and active server check succeeded.",
        }

        if log_events:
            _write_watchdog_operational_event(
                event_type="vpn_watchdog_healthy",
                level="info",
                message=result["message"],
                details=result,
            )

        return result

    failover = runtime_controller.failover(
        apply=allow_switch,
        reason=reason,
        update_ping_state=update_ping_state,
        candidate_limit=candidate_limit,
        timeout_ms=timeout_ms,
    )
    selector = failover.get("selector")

    if failover["ok"]:
        # After a successful switch, we must trigger a dataplane reconciliation
        # to ensure routing rules are updated for the new reality.
        if allow_switch:
            current_mode = _routing_mode(_load_routing_state())
            if current_mode in {"vpn", "selective"}:
                set_global_mode(current_mode, requested_by="watchdog_failover")

        result = {
            "ok": True,
            "status": "failover_applied" if allow_switch else "failover_candidate_found",
            "reason": reason,
            "traffic_attempts_observed": True,
            "allow_switch": allow_switch,
            "active_server_id": active_server_id,
            "active_check": active_check,
            "selector": selector,
            "action": failover.get("action") or ("switch_vpn_auto" if allow_switch else "dry_run_only"),
            "vpn_adapter": vpn_adapter,
            "vpn_runtime": failover.get("runtime_state") or runtime_state,
            "path_key": (failover.get("runtime_state") or runtime_state).get("path_key"),
            "runtime_failover": failover,
            "message": (
                "VPN-auto active check failed; failover candidate was applied."
                if allow_switch
                else "VPN-auto active check failed; failover candidate found in dry-run."
            ),
        }

        if log_events:
            _write_watchdog_operational_event(
                event_type="vpn_watchdog_failover",
                level="warning",
                message=result["message"],
                details=result,
            )

        return result

    result = {
        "ok": False,
        "status": "fail_open_direct_recommended",
        "reason": reason,
        "traffic_attempts_observed": True,
        "allow_switch": allow_switch,
        "active_server_id": active_server_id,
        "active_check": active_check,
        "selector": selector,
        "action": "none" if failover.get("error_code") == "WATCHDOG_FAILOVER_UNAVAILABLE" else "fail_open_direct_recommended",
        "vpn_adapter": vpn_adapter,
        "vpn_runtime": failover.get("runtime_state") or runtime_state,
        "path_key": (failover.get("runtime_state") or runtime_state).get("path_key"),
        "runtime_failover": failover,
        "message": (
            "VPN runtime does not expose automatic failover."
            if failover.get("error_code") == "WATCHDOG_FAILOVER_UNAVAILABLE"
            else "VPN-auto active check failed and no working failover candidate was found."
        ),
    }

    if log_events:
        _write_watchdog_operational_event(
            event_type="vpn_watchdog_fail_open_direct",
            level="error",
            message=result["message"],
            details=result,
        )

    return result


def run_vpn_watchdog_auto_check(
    *,
    allow_switch: bool = True,
    update_ping_state: bool = True,
    timeout_ms: int = DEFAULT_WATCHDOG_TIMEOUT_MS,
    candidate_limit: int = DEFAULT_WATCHDOG_CANDIDATE_LIMIT,
    traffic_window_seconds: int | None = None,
    reason: str = "auto_watchdog_check",
    log_events: bool = False,
) -> dict[str, Any]:
    """Run watchdog with backend-owned traffic signal and module state updates."""

    module = _load_watchdog_module()
    routing = _load_routing_state()

    if module is None:
        return _paused_result(
            status="watchdog_module_missing",
            reason=reason,
            message="Watchdog module row is missing.",
            module=None,
            routing=routing,
        )

    if module["desired_state"] != "enabled":
        return _paused_result(
            status="watchdog_disabled",
            reason=reason,
            message="Watchdog automation is disabled.",
            module=module,
            routing=routing,
        )

    if is_core_bypass_enabled():
        updated_module = _update_watchdog_module(
            runtime_state=WATCHDOG_RUNTIME_PAUSED,
            status_text="Watchdog paused because FWRouter core bypass is active.",
        )
        return _paused_result(
            status="paused_core_bypass",
            reason=reason,
            message="Watchdog paused because FWRouter core bypass is active.",
            module=updated_module,
            routing=routing,
        )

    mode = _routing_mode(routing)
    scoped_vpn_subjects = _has_scoped_vpn_subjects()
    if mode not in {"vpn", "selective"} and not scoped_vpn_subjects:
        updated_module = _update_watchdog_module(
            runtime_state=WATCHDOG_RUNTIME_PAUSED,
            status_text=f"Watchdog paused because global mode is {mode}.",
        )
        return _paused_result(
            status="paused_not_vpn",
            reason=reason,
            message=f"Watchdog paused because global mode is {mode}.",
            module=updated_module,
            routing=routing,
        )

    runtime_convergence = get_last_runtime_convergence_status(
        mode=mode,
        scoped_vpn_subjects=scoped_vpn_subjects,
    )
    if not bool(runtime_convergence.get("ok")):
        updated_module = _update_watchdog_module(
            runtime_state=WATCHDOG_RUNTIME_DEGRADED,
            status_text="Watchdog could not repair selective/VPN runtime convergence.",
            error_code=runtime_convergence.get("error_code") or "WATCHDOG_RUNTIME_CONVERGENCE_FAILED",
            error_message=runtime_convergence.get("error_message")
            or "Selective/VPN runtime convergence failed.",
        )
        result = {
            "ok": False,
            "automated": True,
            "status": "runtime_convergence_failed",
            "reason": reason,
            "traffic_attempts_observed": False,
            "allow_switch": False,
            "active_server_id": (routing or {}).get("active_auto_server_id"),
            "active_check": None,
            "selector": None,
            "action": "none",
            "message": "Watchdog could not repair selective/VPN runtime convergence.",
            "traffic_signal": None,
            "safe_for_watchdog_auto": False,
            "module": updated_module,
            "routing": routing,
            "runtime_convergence": runtime_convergence,
        }
        _write_watchdog_decision_log(
            level="warning",
            event_type="watchdog_switch_suppressed",
            message="Watchdog did not switch VPN-auto because runtime convergence is unhealthy.",
            result=result,
            error_code=str(updated_module.get("error_code") or "WATCHDOG_RUNTIME_CONVERGENCE_FAILED"),
        )
        return result

    vpn_adapter = _active_watchdog_vpn_adapter()
    runtime_controller = get_vpn_runtime_controller(vpn_adapter, routing=routing)
    runtime_state = runtime_controller.get_state()
    active_server_id = str(runtime_state.get("active_target_id") or _watchdog_adapter_subject(vpn_adapter, routing) or "").strip() or None
    path_key = str(runtime_state.get("path_key") or active_server_id or "").strip() or None
    selection_mode = str(runtime_state.get("selection_mode") or "unknown").strip().lower()
    vpn_auto_state = runtime_state.get("selector_state") if isinstance(runtime_state.get("selector_state"), dict) else None
    runtime_response_fields = _watchdog_runtime_response_fields(
        runtime_state=runtime_state,
        path_key=path_key,
        cooldown=_watchdog_failover_cooldown_status(),
    )
    if not bool(runtime_state.get("ready")):
        updated_module = _update_watchdog_module(
            runtime_state=WATCHDOG_RUNTIME_DEGRADED,
            status_text="Watchdog suppressed switching because VPN dataplane adapter is not ready.",
            error_code="WATCHDOG_RUNTIME_UNAVAILABLE",
            error_message=str(vpn_adapter.get("error_message") or "VPN dataplane adapter is not ready."),
        )
        result = {
            "ok": False,
            "automated": True,
            "status": "runtime_unavailable",
            "reason": reason,
            "traffic_attempts_observed": False,
            "allow_switch": False,
            "active_server_id": active_server_id,
            "active_check": None,
            "selector": None,
            "action": "none",
            "message": "VPN runtime is unavailable; watchdog suppressed server switching.",
            "traffic_signal": None,
            "safe_for_watchdog_auto": False,
            "module": updated_module,
            "routing": routing,
            "runtime_convergence": runtime_convergence,
            "vpn_adapter": vpn_adapter,
            "vpn_runtime": runtime_state,
            **runtime_response_fields,
        }
        _write_watchdog_decision_log(
            level="warning",
            event_type="watchdog_switch_suppressed",
            message="Watchdog suppressed server switching because VPN runtime is unavailable.",
            result=result,
            error_code="WATCHDOG_RUNTIME_UNAVAILABLE",
        )
        return result

    if (
        selection_mode == "auto"
        and bool(runtime_state.get("initial_select_supported"))
        and not bool(runtime_state.get("active_target_valid"))
    ):
        if allow_switch:
            initial_select = runtime_controller.initial_select(
                apply=True,
                reason=reason,
                update_ping_state=update_ping_state,
                candidate_limit=candidate_limit,
                timeout_ms=timeout_ms,
            )
            selector = initial_select.get("selector")
            if selector["ok"]:
                updated_module = _update_watchdog_module(
                    runtime_state=WATCHDOG_RUNTIME_RUNNING,
                    status_text="Watchdog bootstrap selected a valid vpn-auto server.",
                )
                return {
                    "ok": True,
                    "automated": True,
                    "status": "initial_auto_selected",
                    "reason": reason,
                    "traffic_attempts_observed": False,
                    "allow_switch": True,
                    "active_server_id": initial_select.get("selected_target_id") or selector.get("active_after"),
                    "active_check": None,
                    "selector": selector,
                    "action": initial_select.get("action") or "switch_vpn_auto",
                    "message": "Watchdog bootstrap selected a valid vpn-auto server without waiting for traffic attempts.",
                    "traffic_signal": None,
                    "safe_for_watchdog_auto": False,
                    "module": updated_module,
                    "routing": routing,
                    "vpn_auto_state": (initial_select.get("runtime_state") or {}).get("selector_state") or vpn_auto_state,
                    "runtime_convergence": runtime_convergence,
                    "vpn_adapter": vpn_adapter,
                    "vpn_runtime": initial_select.get("runtime_state") or runtime_state,
                    "path_key": (initial_select.get("runtime_state") or runtime_state).get("path_key"),
                    "selection_mode": selection_mode,
                    "active_target_id": initial_select.get("selected_target_id"),
                    "failover_supported": bool((initial_select.get("runtime_state") or runtime_state).get("failover_supported")),
                    **_watchdog_cooldown_fields(_watchdog_failover_cooldown_status()),
                    "runtime_failover": initial_select,
                }

        updated_module = _update_watchdog_module(
            runtime_state=WATCHDOG_RUNTIME_DEGRADED,
            status_text="VPN-auto is missing a valid active server and needs initial selection.",
            error_code="WATCHDOG_INITIAL_AUTO_SELECTION_REQUIRED",
            error_message="VPN-auto has no valid active server selected.",
        )
        result = {
            "ok": True,
            "automated": True,
            "status": "needs_initial_auto_selection",
            "reason": reason,
            "traffic_attempts_observed": False,
            "allow_switch": False,
            "active_server_id": (routing or {}).get("active_auto_server_id"),
            "active_check": None,
            "selector": None,
            "action": "none",
            "message": "VPN-auto has no valid active server selected.",
            "traffic_signal": None,
            "safe_for_watchdog_auto": False,
            "module": updated_module,
            "routing": routing,
            "vpn_auto_state": vpn_auto_state,
            "runtime_convergence": runtime_convergence,
            "vpn_adapter": vpn_adapter,
            "vpn_runtime": runtime_state,
            **runtime_response_fields,
        }
        _write_watchdog_decision_log(
            level="warning",
            event_type="watchdog_switch_suppressed",
            message="Watchdog did not switch VPN-auto because no valid active auto server is selected.",
            result=result,
            error_code="WATCHDOG_INITIAL_AUTO_SELECTION_REQUIRED",
        )
        return result

    traffic_signal = detect_recent_vpn_traffic_attempts(
        window_seconds=traffic_window_seconds,
    )
    if not bool(traffic_signal.get("authoritative")):
        updated_module = _update_watchdog_module(
            runtime_state=WATCHDOG_RUNTIME_DEGRADED,
            status_text="Watchdog traffic signal is stale or unavailable; automatic switching is suppressed.",
            error_code="WATCHDOG_SIGNAL_UNAVAILABLE",
            error_message="Fresh traffic counter snapshots are required for authoritative watchdog decisions.",
        )
        result = {
            "ok": True,
            "automated": True,
            "status": "paused_signal_unavailable",
            "reason": reason,
            "traffic_attempts_observed": False,
            "allow_switch": False,
            "active_server_id": active_server_id,
            "active_check": None,
            "selector": None,
            "action": "none",
            "message": "Watchdog traffic signal is stale or unavailable; automatic switching is suppressed.",
            "traffic_signal": traffic_signal,
            "safe_for_watchdog_auto": False,
            "module": updated_module,
            "routing": routing,
            "runtime_convergence": runtime_convergence,
            "vpn_adapter": vpn_adapter,
            "vpn_runtime": runtime_state,
            **runtime_response_fields,
        }
        _write_watchdog_decision_log(
            level="warning",
            event_type="watchdog_switch_suppressed",
            message="Watchdog did not switch VPN-auto because the traffic signal is stale or unavailable.",
            result=result,
            error_code="WATCHDOG_SIGNAL_UNAVAILABLE",
        )
        return result

    if not bool(traffic_signal.get("observed")):
        _reset_watchdog_traffic_failure_candidate()
    elif bool(traffic_signal.get("response_observed")):
        _reset_watchdog_traffic_failure_candidate()
        active_check = None
        if (
            selection_mode == "auto"
            and bool(runtime_state.get("probe_supported"))
            and active_server_id
        ):
            checked_by = f"watchdog_active_check:{reason}"
            active_check = _recent_successful_active_check(
                server_id=active_server_id,
                checked_by=checked_by,
                timeout_ms=timeout_ms,
            )
            if active_check is None:
                active_check = runtime_controller.probe(
                    update_ping_state=update_ping_state,
                    timeout_ms=timeout_ms,
                    reason=reason,
                )

        if active_check is not None and _watchdog_active_probe_degraded(active_check):
            active_check = _watchdog_degraded_active_check(active_check)
            if not bool(runtime_state.get("failover_supported")):
                message = "VPN traffic has responses, but active VPN runtime has no FWRouter failover adapter."
                result = {
                    "ok": False,
                    "status": "external_runtime_failover_unavailable",
                    "reason": reason,
                    "traffic_attempts_observed": True,
                    "allow_switch": False,
                    "active_server_id": active_server_id,
                    "active_check": active_check,
                    "selector": None,
                    "action": "none",
                    "path_state": "degraded_active_probe",
                    "message": message,
                    "failover_supported": False,
                    "active_target_id": active_server_id,
                    **_watchdog_cooldown_fields(None),
                    "traffic_signal": traffic_signal,
                    "safe_for_watchdog_auto": bool(traffic_signal.get("safe_for_watchdog_auto")),
                    "routing": routing,
                    "runtime_convergence": runtime_convergence,
                    "vpn_adapter": vpn_adapter,
                    "vpn_runtime": runtime_state,
                    **runtime_response_fields,
                    "vpn_auto_state": vpn_auto_state,
                }
                updated_module = _update_watchdog_module(
                    runtime_state=WATCHDOG_RUNTIME_DEGRADED,
                    status_text=result["message"],
                    error_code="WATCHDOG_EXTERNAL_FAILOVER_UNAVAILABLE",
                    error_message=result["message"],
                )
                result["module"] = updated_module
                _write_watchdog_decision_log(
                    level="warning",
                    event_type="watchdog_switch_suppressed",
                    message="Watchdog active delay check failed but the active VPN runtime has no failover adapter.",
                    result=result,
                    error_code="WATCHDOG_EXTERNAL_FAILOVER_UNAVAILABLE",
                )
                return result

            cooldown = _watchdog_failover_cooldown_status()
            if allow_switch and bool(cooldown.get("active")):
                message = "Active VPN server is degraded, but automatic failover is in cooldown."
                updated_module = _update_watchdog_module(
                    runtime_state=WATCHDOG_RUNTIME_DEGRADED,
                    status_text=message,
                    error_code="WATCHDOG_FAILOVER_COOLDOWN",
                    error_message=message,
                )
                result = {
                    "ok": True,
                    "automated": True,
                    "status": "failover_cooldown",
                    "reason": reason,
                    "traffic_attempts_observed": True,
                    "allow_switch": False,
                    "active_server_id": active_server_id,
                    "active_check": active_check,
                    "selector": None,
                    "action": "none",
                    "path_state": "degraded_active_probe",
                    "message": message,
                    "traffic_signal": traffic_signal,
                    "failover_cooldown": {
                        "active": True,
                        "cooldown_until": cooldown.get("cooldown_until"),
                        "remaining_seconds": cooldown.get("remaining_seconds"),
                    },
                    "failover_supported": bool(runtime_state.get("failover_supported")),
                    "active_target_id": active_server_id,
                    **_watchdog_cooldown_fields({
                        "active": True,
                        "cooldown_until": cooldown.get("cooldown_until"),
                        "remaining_seconds": cooldown.get("remaining_seconds"),
                    }),
                    "safe_for_watchdog_auto": bool(traffic_signal.get("safe_for_watchdog_auto")),
                    "module": updated_module,
                    "routing": routing,
                    "runtime_convergence": runtime_convergence,
                    "vpn_adapter": vpn_adapter,
                    "vpn_runtime": runtime_state,
                    "path_key": path_key,
                    "selection_mode": selection_mode,
                    "vpn_auto_state": vpn_auto_state,
                }
                _write_watchdog_decision_log(
                    level="warning",
                    event_type="watchdog_switch_suppressed",
                    message="Watchdog active delay check failed but automatic failover is in cooldown.",
                    result=result,
                    error_code="WATCHDOG_FAILOVER_COOLDOWN",
                )
                return result

            failover = runtime_controller.failover(
                apply=allow_switch,
                reason=reason,
                update_ping_state=update_ping_state,
                candidate_limit=candidate_limit,
                timeout_ms=timeout_ms,
            )
            selector = failover.get("selector")

            if failover["ok"]:
                cooldown_state = None
                if allow_switch and bool(failover.get("applied")):
                    current_mode = _routing_mode(_load_routing_state())
                    if current_mode in {"vpn", "selective"}:
                        set_global_mode(current_mode, requested_by="watchdog_failover")
                    cooldown_state = _record_watchdog_successful_failover(
                        path_key=path_key,
                        previous_target_id=str(failover.get("previous_target_id") or active_server_id or "") or None,
                        selected_target_id=str(failover.get("selected_target_id") or "") or None,
                        cooldown_seconds=get_settings().watchdog_failover_cooldown_seconds,
                    )

                result = {
                    "ok": True,
                    "status": "failover_applied" if allow_switch else "failover_candidate_found",
                    "reason": reason,
                    "traffic_attempts_observed": True,
                    "allow_switch": allow_switch,
                    "active_server_id": active_server_id,
                    "active_check": active_check,
                    "selector": selector,
                    "action": failover.get("action") or ("switch_vpn_auto" if allow_switch else "dry_run_only"),
                    "path_state": "degraded_active_probe",
                    "message": (
                        "Active VPN server delay check failed; failover candidate was applied."
                        if allow_switch
                        else "Active VPN server delay check failed; failover candidate found in dry-run."
                    ),
                    "runtime_failover": failover,
                    "failover_cooldown": {
                        "active": bool(cooldown_state),
                        "cooldown_until": cooldown_state.get("cooldown_until") if isinstance(cooldown_state, dict) else None,
                        "remaining_seconds": get_settings().watchdog_failover_cooldown_seconds if cooldown_state else 0,
                    },
                    "failover_supported": bool(runtime_state.get("failover_supported")),
                    "active_target_id": active_server_id,
                    **_watchdog_cooldown_fields({
                        "active": bool(cooldown_state),
                        "cooldown_until": cooldown_state.get("cooldown_until") if isinstance(cooldown_state, dict) else None,
                        "remaining_seconds": get_settings().watchdog_failover_cooldown_seconds if cooldown_state else 0,
                    }),
                }
                updated_module = _update_watchdog_module(
                    runtime_state=WATCHDOG_RUNTIME_RUNNING,
                    status_text=result["message"],
                )
                return {
                    **result,
                    "automated": True,
                    "traffic_signal": traffic_signal,
                    "safe_for_watchdog_auto": bool(traffic_signal.get("safe_for_watchdog_auto")),
                    "module": updated_module,
                    "routing": routing,
                    "runtime_convergence": runtime_convergence,
                    "vpn_adapter": vpn_adapter,
                    "vpn_runtime": failover.get("runtime_state") or runtime_state,
                    "path_key": (failover.get("runtime_state") or runtime_state).get("path_key"),
                    "selection_mode": selection_mode,
                    "vpn_auto_state": (failover.get("runtime_state") or {}).get("selector_state") or vpn_auto_state,
                }

            result = {
                "ok": False,
                "status": "fail_open_direct_recommended",
                "reason": reason,
                "traffic_attempts_observed": True,
                "allow_switch": allow_switch,
                "active_server_id": active_server_id,
                "active_check": active_check,
                "selector": selector,
                "action": "fail_open_direct_recommended",
                "path_state": "degraded_active_probe",
                "message": "Active VPN server delay check failed and no working failover candidate was found.",
                "runtime_failover": failover,
                "failover_supported": bool(runtime_state.get("failover_supported")),
                "active_target_id": active_server_id,
                **_watchdog_cooldown_fields(None),
            }
            updated_module = _update_watchdog_module(
                runtime_state=WATCHDOG_RUNTIME_DEGRADED,
                status_text=result["message"],
                error_code="WATCHDOG_FAIL_OPEN_DIRECT_RECOMMENDED",
                error_message=result["message"],
            )
            result = {
                **result,
                "automated": True,
                "traffic_signal": traffic_signal,
                "safe_for_watchdog_auto": bool(traffic_signal.get("safe_for_watchdog_auto")),
                "module": updated_module,
                "routing": routing,
                "runtime_convergence": runtime_convergence,
                "vpn_adapter": vpn_adapter,
                "vpn_runtime": failover.get("runtime_state") or runtime_state,
                "path_key": (failover.get("runtime_state") or runtime_state).get("path_key"),
                "selection_mode": selection_mode,
                "vpn_auto_state": (failover.get("runtime_state") or {}).get("selector_state") or vpn_auto_state,
            }
            _write_watchdog_decision_log(
                level="error",
                event_type="watchdog_switch_suppressed",
                message="Watchdog active delay check failed and found no working failover candidate.",
                result=result,
                error_code="WATCHDOG_FAIL_OPEN_DIRECT_RECOMMENDED",
            )
            return result

        updated_module = _update_watchdog_module(
            runtime_state=WATCHDOG_RUNTIME_RUNNING,
            status_text="Watchdog saw VPN traffic responses and active probe is healthy.",
        )
        return {
            "ok": True,
            "automated": True,
            "status": "healthy_traffic",
            "reason": reason,
            "traffic_attempts_observed": True,
            "allow_switch": False,
            "active_server_id": active_server_id,
            "active_check": active_check,
            "selector": None,
            "action": "none",
            "message": "VPN traffic has response bytes and active server probe is healthy.",
            "traffic_signal": traffic_signal,
            "safe_for_watchdog_auto": bool(traffic_signal.get("safe_for_watchdog_auto")),
            "module": updated_module,
            "routing": routing,
            "runtime_convergence": runtime_convergence,
            "vpn_adapter": vpn_adapter,
            "vpn_runtime": runtime_state,
            **runtime_response_fields,
            "vpn_auto_state": vpn_auto_state,
        }
    elif bool(traffic_signal.get("traffic_stalled")):
        confirmation = _watchdog_traffic_failure_confirmation(
            active_server_id=active_server_id,
            traffic_signal=traffic_signal,
            confirm_seconds=get_settings().watchdog_traffic_failure_confirm_seconds,
            path_key=path_key,
        )
        if not bool(confirmation.get("confirmed")):
            updated_module = _update_watchdog_module(
                runtime_state=WATCHDOG_RUNTIME_RUNNING,
                status_text="Watchdog saw outbound-only VPN traffic and is waiting for confirmation.",
            )
            result = {
                "ok": True,
                "automated": True,
                "status": "traffic_failure_pending",
                "reason": reason,
                "traffic_attempts_observed": True,
                "allow_switch": False,
                "active_server_id": active_server_id,
                "active_check": None,
                "selector": None,
                "action": "none",
                "message": "Outbound-only VPN traffic was observed once; failover is pending confirmation.",
                "traffic_signal": traffic_signal,
                "traffic_failure_confirmation": confirmation,
                "safe_for_watchdog_auto": bool(traffic_signal.get("safe_for_watchdog_auto")),
                "module": updated_module,
                "routing": routing,
                "runtime_convergence": runtime_convergence,
                "vpn_adapter": vpn_adapter,
                "vpn_runtime": runtime_state,
                **runtime_response_fields,
                "vpn_auto_state": vpn_auto_state,
            }
            _write_watchdog_decision_log(
                level="warning",
                event_type="watchdog_switch_suppressed",
                message="Watchdog saw outbound-only VPN traffic but is waiting for confirmation before switching.",
                result=result,
                error_code="WATCHDOG_TRAFFIC_FAILURE_PENDING",
            )
            return result

        active_check = {
            "ok": False,
            "status": "traffic_stalled",
            "server_id": active_server_id,
            "error_code": "WATCHDOG_TRAFFIC_STALLED_CONFIRMED",
            "error_message": "Outbound VPN traffic had no response bytes across the confirmation window.",
            "source": "traffic_counter_snapshots",
        }

        if selection_mode == "manual":
            message = "VPN traffic stall was confirmed, but automatic failover is suppressed by manual selection mode."
            updated_module = _update_watchdog_module(
                runtime_state=WATCHDOG_RUNTIME_DEGRADED,
                status_text=message,
                error_code="WATCHDOG_MANUAL_SELECTION",
                error_message=message,
            )
            result = {
                "ok": True,
                "automated": True,
                "status": "manual_selection",
                "reason": reason,
                "traffic_attempts_observed": True,
                "allow_switch": False,
                "active_server_id": active_server_id,
                "active_check": active_check,
                "selector": None,
                "action": "none",
                "path_state": "confirmed_failure",
                "message": message,
                "traffic_signal": traffic_signal,
                "traffic_failure_confirmation": confirmation,
                "failover_supported": bool(runtime_state.get("failover_supported")),
                "active_target_id": active_server_id,
                **_watchdog_cooldown_fields(None),
                "safe_for_watchdog_auto": bool(traffic_signal.get("safe_for_watchdog_auto")),
                "module": updated_module,
                "routing": routing,
                "runtime_convergence": runtime_convergence,
                "vpn_adapter": vpn_adapter,
                "vpn_runtime": runtime_state,
                **runtime_response_fields,
                "vpn_auto_state": vpn_auto_state,
            }
            _write_watchdog_decision_log(
                level="warning",
                event_type="watchdog_switch_suppressed",
                message="Watchdog confirmed a VPN traffic stall but manual selection mode suppresses failover.",
                result=result,
                error_code="WATCHDOG_MANUAL_SELECTION",
            )
            return result

        if not bool(runtime_state.get("failover_supported")):
            message = "VPN traffic stall was confirmed, but the active VPN runtime has no FWRouter failover adapter."
            result = {
                "ok": False,
                "status": "external_runtime_failover_unavailable",
                "reason": reason,
                "traffic_attempts_observed": True,
                "allow_switch": False,
                "active_server_id": active_server_id,
                "active_check": active_check,
                "selector": None,
                "action": "none",
                "path_state": "confirmed_failure",
                "message": message,
                "traffic_failure_confirmation": confirmation,
                "failover_supported": False,
                "active_target_id": active_server_id,
                **_watchdog_cooldown_fields(None),
            }
            updated_module = _update_watchdog_module(
                runtime_state=WATCHDOG_RUNTIME_DEGRADED,
                status_text=result["message"],
                error_code="WATCHDOG_EXTERNAL_FAILOVER_UNAVAILABLE",
                error_message=result["message"],
            )
            result = {
                **result,
                "automated": True,
                "traffic_signal": traffic_signal,
                "safe_for_watchdog_auto": bool(traffic_signal.get("safe_for_watchdog_auto")),
                "module": updated_module,
                "routing": routing,
                "runtime_convergence": runtime_convergence,
                "vpn_adapter": vpn_adapter,
                "vpn_runtime": runtime_state,
                **runtime_response_fields,
                "vpn_auto_state": vpn_auto_state,
            }
            _write_watchdog_decision_log(
                level="warning",
                event_type="watchdog_switch_suppressed",
                message="Watchdog confirmed a VPN traffic stall but the active VPN runtime has no failover adapter.",
                result=result,
                error_code="WATCHDOG_EXTERNAL_FAILOVER_UNAVAILABLE",
            )
            return result

        cooldown = _watchdog_failover_cooldown_status()
        if allow_switch and bool(cooldown.get("active")):
            message = "VPN traffic stall was confirmed, but automatic failover is in cooldown."
            updated_module = _update_watchdog_module(
                runtime_state=WATCHDOG_RUNTIME_DEGRADED,
                status_text=message,
                error_code="WATCHDOG_FAILOVER_COOLDOWN",
                error_message=message,
            )
            result = {
                "ok": True,
                "automated": True,
                "status": "failover_cooldown",
                "reason": reason,
                "traffic_attempts_observed": True,
                "allow_switch": False,
                "active_server_id": active_server_id,
                "active_check": active_check,
                "selector": None,
                "action": "none",
                "path_state": "confirmed_failure",
                "message": message,
                "traffic_signal": traffic_signal,
                "traffic_failure_confirmation": confirmation,
                "failover_cooldown": {
                    "active": True,
                    "cooldown_until": cooldown.get("cooldown_until"),
                    "remaining_seconds": cooldown.get("remaining_seconds"),
                },
                "failover_supported": bool(runtime_state.get("failover_supported")),
                "active_target_id": active_server_id,
                **_watchdog_cooldown_fields({
                    "active": True,
                    "cooldown_until": cooldown.get("cooldown_until"),
                    "remaining_seconds": cooldown.get("remaining_seconds"),
                }),
                "safe_for_watchdog_auto": bool(traffic_signal.get("safe_for_watchdog_auto")),
                "module": updated_module,
                "routing": routing,
                "runtime_convergence": runtime_convergence,
                "vpn_adapter": vpn_adapter,
                "vpn_runtime": runtime_state,
                "path_key": path_key,
                "selection_mode": selection_mode,
                "vpn_auto_state": vpn_auto_state,
            }
            _write_watchdog_decision_log(
                level="warning",
                event_type="watchdog_switch_suppressed",
                message="Watchdog confirmed a VPN traffic stall but automatic failover is in cooldown.",
                result=result,
                error_code="WATCHDOG_FAILOVER_COOLDOWN",
            )
            return result

        failover = runtime_controller.failover(
            apply=allow_switch,
            reason=reason,
            update_ping_state=update_ping_state,
            candidate_limit=candidate_limit,
            timeout_ms=timeout_ms,
        )
        selector = failover.get("selector")

        if failover["ok"]:
            cooldown_state = None
            if allow_switch and bool(failover.get("applied")):
                current_mode = _routing_mode(_load_routing_state())
                if current_mode in {"vpn", "selective"}:
                    set_global_mode(current_mode, requested_by="watchdog_failover")
                cooldown_state = _record_watchdog_successful_failover(
                    path_key=path_key,
                    previous_target_id=str(failover.get("previous_target_id") or active_server_id or "") or None,
                    selected_target_id=str(failover.get("selected_target_id") or "") or None,
                    cooldown_seconds=get_settings().watchdog_failover_cooldown_seconds,
                )

            result = {
                "ok": True,
                "status": "failover_applied" if allow_switch else "failover_candidate_found",
                "reason": reason,
                "traffic_attempts_observed": True,
                "allow_switch": allow_switch,
                "active_server_id": active_server_id,
                "active_check": active_check,
                "selector": selector,
                "action": failover.get("action") or ("switch_vpn_auto" if allow_switch else "dry_run_only"),
                "path_state": "confirmed_failure",
                "message": (
                    "VPN traffic stall was confirmed; failover candidate was applied."
                    if allow_switch
                    else "VPN traffic stall was confirmed; failover candidate found in dry-run."
                ),
                "traffic_failure_confirmation": confirmation,
                "runtime_failover": failover,
                "failover_cooldown": {
                    "active": bool(cooldown_state),
                    "cooldown_until": cooldown_state.get("cooldown_until") if isinstance(cooldown_state, dict) else None,
                    "remaining_seconds": get_settings().watchdog_failover_cooldown_seconds if cooldown_state else 0,
                },
                "failover_supported": bool(runtime_state.get("failover_supported")),
                "active_target_id": active_server_id,
                **_watchdog_cooldown_fields({
                    "active": bool(cooldown_state),
                    "cooldown_until": cooldown_state.get("cooldown_until") if isinstance(cooldown_state, dict) else None,
                    "remaining_seconds": get_settings().watchdog_failover_cooldown_seconds if cooldown_state else 0,
                }),
            }
            updated_module = _update_watchdog_module(
                runtime_state=WATCHDOG_RUNTIME_RUNNING,
                status_text=result["message"],
            )
            return {
                **result,
                "automated": True,
                "traffic_signal": traffic_signal,
                "safe_for_watchdog_auto": bool(traffic_signal.get("safe_for_watchdog_auto")),
                "module": updated_module,
                "routing": routing,
                "runtime_convergence": runtime_convergence,
                "vpn_adapter": vpn_adapter,
                "vpn_runtime": failover.get("runtime_state") or runtime_state,
                "path_key": (failover.get("runtime_state") or runtime_state).get("path_key"),
                "selection_mode": selection_mode,
                "vpn_auto_state": (failover.get("runtime_state") or {}).get("selector_state") or vpn_auto_state,
            }

        result = {
            "ok": False,
            "status": "fail_open_direct_recommended",
            "reason": reason,
            "traffic_attempts_observed": True,
            "allow_switch": allow_switch,
            "active_server_id": active_server_id,
            "active_check": active_check,
            "selector": selector,
            "action": "fail_open_direct_recommended",
            "path_state": "confirmed_failure",
            "message": "VPN traffic stall was confirmed and no working failover candidate was found.",
            "traffic_failure_confirmation": confirmation,
            "runtime_failover": failover,
            "failover_supported": bool(runtime_state.get("failover_supported")),
            "active_target_id": active_server_id,
            **_watchdog_cooldown_fields(None),
        }
        updated_module = _update_watchdog_module(
            runtime_state=WATCHDOG_RUNTIME_DEGRADED,
            status_text=result["message"],
            error_code="WATCHDOG_FAIL_OPEN_DIRECT_RECOMMENDED",
            error_message=result["message"],
        )
        result = {
            **result,
            "automated": True,
            "traffic_signal": traffic_signal,
            "safe_for_watchdog_auto": bool(traffic_signal.get("safe_for_watchdog_auto")),
            "module": updated_module,
            "routing": routing,
            "runtime_convergence": runtime_convergence,
            "vpn_adapter": vpn_adapter,
            "vpn_runtime": failover.get("runtime_state") or runtime_state,
            "path_key": (failover.get("runtime_state") or runtime_state).get("path_key"),
            "selection_mode": selection_mode,
            "vpn_auto_state": (failover.get("runtime_state") or {}).get("selector_state") or vpn_auto_state,
        }
        _write_watchdog_decision_log(
            level="error",
            event_type="watchdog_switch_suppressed",
            message="Watchdog confirmed a VPN traffic stall but found no working failover candidate.",
            result=result,
            error_code="WATCHDOG_FAIL_OPEN_DIRECT_RECOMMENDED",
        )
        return result

    result = run_vpn_watchdog_check(
        traffic_attempts_observed=traffic_signal["observed"],
        allow_switch=allow_switch,
        update_ping_state=update_ping_state,
        timeout_ms=timeout_ms,
        candidate_limit=candidate_limit,
        reason=reason,
        log_events=log_events,
    )

    if result["status"] == "no_failure_no_traffic":
        updated_module = _update_watchdog_module(
            runtime_state=WATCHDOG_RUNTIME_RUNNING,
            status_text="Watchdog enabled and waiting for VPN-auto traffic activity.",
        )
    elif result["status"] in {"healthy", "failover_applied", "external_runtime_active"}:
        updated_module = _update_watchdog_module(
            runtime_state=WATCHDOG_RUNTIME_RUNNING,
            status_text=result["message"],
        )
    elif result["status"] == "failover_candidate_found":
        updated_module = _update_watchdog_module(
            runtime_state=WATCHDOG_RUNTIME_DEGRADED,
            status_text=result["message"],
        )
    elif result["status"] == "runtime_unavailable":
        updated_module = _update_watchdog_module(
            runtime_state=WATCHDOG_RUNTIME_DEGRADED,
            status_text=result["message"],
            error_code="WATCHDOG_RUNTIME_UNAVAILABLE",
            error_message=str(result.get("error_message") or result["message"]),
        )
    elif result["status"] == "external_runtime_failover_unavailable":
        updated_module = _update_watchdog_module(
            runtime_state=WATCHDOG_RUNTIME_DEGRADED,
            status_text=result["message"],
            error_code="WATCHDOG_EXTERNAL_FAILOVER_UNAVAILABLE",
            error_message=result["message"],
        )
    else:
        updated_module = _update_watchdog_module(
            runtime_state=WATCHDOG_RUNTIME_DEGRADED,
            status_text=result["message"],
            error_code="WATCHDOG_FAIL_OPEN_DIRECT_RECOMMENDED",
            error_message=result["message"],
        )

    result = {
        **result,
        "automated": True,
        "traffic_signal": traffic_signal,
        "safe_for_watchdog_auto": bool((traffic_signal or {}).get("safe_for_watchdog_auto")),
        "module": updated_module,
        "routing": routing,
        "runtime_convergence": runtime_convergence,
        "vpn_adapter": result.get("vpn_adapter") or vpn_adapter,
    }
    if result["status"] in {
        "failover_candidate_found",
        "fail_open_direct_recommended",
        "runtime_unavailable",
        "external_runtime_failover_unavailable",
    }:
        _write_watchdog_decision_log(
            level="error" if result["status"] == "fail_open_direct_recommended" else "warning",
            event_type="watchdog_switch_suppressed",
            message=(
                "Watchdog active check failed but did not apply a server switch."
                if result["status"] == "failover_candidate_found"
                else "Watchdog suppressed server switching because VPN runtime is unavailable."
                if result["status"] == "runtime_unavailable"
                else "Watchdog confirmed a VPN traffic stall but the external VPN adapter has no failover adapter."
                if result["status"] == "external_runtime_failover_unavailable"
                else "Watchdog active check failed and found no working failover candidate."
            ),
            result=result,
            error_code=(
                "WATCHDOG_FAIL_OPEN_DIRECT_RECOMMENDED"
                if result["status"] == "fail_open_direct_recommended"
                else "WATCHDOG_RUNTIME_UNAVAILABLE"
                if result["status"] == "runtime_unavailable"
                else "WATCHDOG_EXTERNAL_FAILOVER_UNAVAILABLE"
                if result["status"] == "external_runtime_failover_unavailable"
                else "WATCHDOG_DRY_RUN_ONLY"
            ),
        )
    return result


def run_watchdog_scheduler_tick() -> dict[str, Any]:
    """Run one safe scheduler tick and convert exceptions into diagnostics."""

    settings = get_settings()

    try:
        return run_vpn_watchdog_auto_check(
            allow_switch=True,
            update_ping_state=True,
            timeout_ms=DEFAULT_WATCHDOG_TIMEOUT_MS,
            candidate_limit=DEFAULT_WATCHDOG_CANDIDATE_LIMIT,
            traffic_window_seconds=settings.watchdog_traffic_window_seconds,
            reason="scheduler_watchdog_check",
            log_events=settings.watchdog_scheduler_log_events,
        )
    except Exception as exc:  # pragma: no cover - defensive background safety
        updated_module = _update_watchdog_module(
            runtime_state=WATCHDOG_RUNTIME_FAILED,
            status_text="Watchdog scheduler tick failed.",
            error_code="WATCHDOG_SCHEDULER_FAILED",
            error_message=str(exc),
        )
        details = {
            "error_code": "WATCHDOG_SCHEDULER_FAILED",
            "error_message": str(exc),
            "timestamp": _utc_timestamp(),
        }
        if _should_write_watchdog_issue_log(str(exc)):
            write_technical_log(
                component="watchdog",
                level="error",
                event_type="watchdog_scheduler_failed",
                message="Watchdog scheduler tick failed.",
                details=details,
            )
        return {
            "ok": False,
            "automated": True,
            "status": "scheduler_failed",
            "reason": "scheduler_watchdog_check",
            "traffic_attempts_observed": False,
            "allow_switch": True,
            "active_server_id": None,
            "active_check": None,
            "selector": None,
            "action": "none",
            "message": "Watchdog scheduler tick failed.",
            "module": updated_module,
            "error_code": "WATCHDOG_SCHEDULER_FAILED",
            "error_message": str(exc),
        }


def _watchdog_scheduler_loop() -> None:
    settings = get_settings()
    interval = settings.watchdog_auto_interval_seconds

    while not _WATCHDOG_STOP_EVENT.is_set():
        run_watchdog_scheduler_tick()
        if _WATCHDOG_STOP_EVENT.wait(interval):
            break


def start_watchdog_scheduler() -> bool:
    settings = get_settings()
    if not settings.watchdog_scheduler_enabled:
        _update_watchdog_module(
            runtime_state=WATCHDOG_RUNTIME_STOPPED,
            status_text="Watchdog scheduler is disabled by config.",
            error_code="WATCHDOG_DISABLED_BY_CONFIG",
            error_message="FWROUTER_WATCHDOG_SCHEDULER_ENABLED is false.",
        )
        return False

    global _WATCHDOG_THREAD
    with _WATCHDOG_LOCK:
        if _WATCHDOG_THREAD is not None and _WATCHDOG_THREAD.is_alive():
            return False

        _WATCHDOG_STOP_EVENT.clear()
        _WATCHDOG_THREAD = Thread(
            target=_watchdog_scheduler_loop,
            name="fwrouter-watchdog",
            daemon=True,
        )
        _WATCHDOG_THREAD.start()
        return True


def stop_watchdog_scheduler(*, timeout_seconds: float = 2.0) -> bool:
    global _WATCHDOG_THREAD
    with _WATCHDOG_LOCK:
        if _WATCHDOG_THREAD is None:
            return False

        _WATCHDOG_STOP_EVENT.set()
        _WATCHDOG_THREAD.join(timeout=timeout_seconds)
        stopped = not _WATCHDOG_THREAD.is_alive()
        _WATCHDOG_THREAD = None
        return stopped
