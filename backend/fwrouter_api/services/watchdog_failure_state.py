from __future__ import annotations

from datetime import datetime, timedelta
from threading import Lock
from typing import Any, Callable

from fwrouter_api.services.watchdog_runtime_state import (
    load_watchdog_runtime_state,
    update_watchdog_runtime_state,
)


_TRAFFIC_FAILURE_LOCK = Lock()
_TRAFFIC_FAILURE_CANDIDATE: dict[str, Any] | None = None


def get_traffic_failure_candidate() -> dict[str, Any] | None:
    return _TRAFFIC_FAILURE_CANDIDATE


def set_traffic_failure_candidate(candidate: dict[str, Any] | None) -> None:
    global _TRAFFIC_FAILURE_CANDIDATE
    with _TRAFFIC_FAILURE_LOCK:
        _TRAFFIC_FAILURE_CANDIDATE = candidate


def reset_traffic_failure_candidate() -> None:
    set_traffic_failure_candidate(None)
    update_watchdog_runtime_state(
        path_key=None,
        failure_candidate=None,
        last_processed_decision_id=None,
    )


def traffic_failure_confirmation(
    *,
    active_server_id: str | None,
    traffic_signal: dict[str, Any],
    confirm_seconds: int,
    path_key: str | None,
    now_fn: Callable[[], datetime],
    parse_timestamp: Callable[[str | None], datetime | None],
) -> dict[str, Any]:
    normalized_server_id = str(active_server_id or "").strip()
    normalized_path_key = str(path_key or normalized_server_id or "").strip()
    collected_at = str(traffic_signal.get("last_collected_at") or "").strip()
    decision_id = str(traffic_signal.get("decision_id") or collected_at or "").strip()
    now = now_fn()
    threshold = max(30, int(confirm_seconds or 60))

    if not normalized_path_key or not collected_at or not decision_id or not bool(traffic_signal.get("traffic_stalled")):
        reset_traffic_failure_candidate()
        return {
            "confirmed": False,
            "pending": False,
            "reason": "traffic_not_stalled",
            "confirm_seconds": threshold,
        }

    global _TRAFFIC_FAILURE_CANDIDATE
    with _TRAFFIC_FAILURE_LOCK:
        state = load_watchdog_runtime_state()
        candidate = state.get("failure_candidate")
        if not isinstance(candidate, dict):
            candidate = _TRAFFIC_FAILURE_CANDIDATE
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
            _TRAFFIC_FAILURE_CANDIDATE = candidate
            update_watchdog_runtime_state(
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
            first_seen_at = parse_timestamp(str(candidate.get("first_seen_at") or ""))
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

        first_seen_at = parse_timestamp(str(candidate.get("first_seen_at") or ""))
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
        _TRAFFIC_FAILURE_CANDIDATE = candidate
        update_watchdog_runtime_state(
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

        _TRAFFIC_FAILURE_CANDIDATE = None
        update_watchdog_runtime_state(
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


def failover_cooldown_status(
    *,
    now_fn: Callable[[], datetime],
    parse_timestamp: Callable[[str | None], datetime | None],
) -> dict[str, Any]:
    state = load_watchdog_runtime_state()
    cooldown_until = parse_timestamp(str(state.get("cooldown_until") or ""))
    now = now_fn()
    active = bool(cooldown_until and cooldown_until > now)
    return {
        "active": active,
        "cooldown_until": cooldown_until.isoformat() if cooldown_until is not None else None,
        "remaining_seconds": max(0, int((cooldown_until - now).total_seconds())) if active and cooldown_until else 0,
        "state": state,
    }


def record_successful_failover(
    *,
    path_key: str | None,
    previous_target_id: str | None,
    selected_target_id: str | None,
    cooldown_seconds: int,
    now_fn: Callable[[], datetime],
) -> dict[str, Any]:
    now = now_fn()
    cooldown_until = now + timedelta(seconds=max(30, int(cooldown_seconds or 300)))
    return update_watchdog_runtime_state(
        path_key=path_key,
        failure_candidate=None,
        last_successful_failover_at=now.isoformat(),
        failover_path_key=path_key,
        previous_target_id=previous_target_id,
        selected_target_id=selected_target_id,
        cooldown_until=cooldown_until.isoformat(),
    )


def cooldown_fields(cooldown: dict[str, Any] | None) -> dict[str, Any]:
    state = cooldown if isinstance(cooldown, dict) else {}
    return {
        "cooldown_active": bool(state.get("active")),
        "cooldown_until": state.get("cooldown_until"),
        "cooldown_remaining_seconds": int(state.get("remaining_seconds") or 0),
    }


def runtime_response_fields(
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
        **cooldown_fields(cooldown),
    }
