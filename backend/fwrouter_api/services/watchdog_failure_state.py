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


def reset_stalled_traffic_failure_candidate() -> None:
    global _TRAFFIC_FAILURE_CANDIDATE
    with _TRAFFIC_FAILURE_LOCK:
        state = load_watchdog_runtime_state()
        candidate = state.get("failure_candidate")
        if not isinstance(candidate, dict):
            candidate = _TRAFFIC_FAILURE_CANDIDATE
        if isinstance(candidate, dict) and candidate.get("kind") == "active_quality_degraded":
            _TRAFFIC_FAILURE_CANDIDATE = candidate
            return
        _TRAFFIC_FAILURE_CANDIDATE = None
        update_watchdog_runtime_state(
            path_key=None,
            failure_candidate=None,
            last_processed_decision_id=None,
        )


def active_quality_degraded_confirmation(
    *,
    active_server_id: str | None,
    active_check: dict[str, Any],
    traffic_signal: dict[str, Any],
    confirm_seconds: int,
    bad_checks_required: int,
    path_key: str | None,
    now_fn: Callable[[], datetime],
    parse_timestamp: Callable[[str | None], datetime | None],
) -> dict[str, Any]:
    normalized_server_id = str(active_server_id or "").strip()
    normalized_path_key = str(path_key or normalized_server_id or "").strip()
    collected_at = str(traffic_signal.get("last_collected_at") or "").strip()
    decision_id = str(traffic_signal.get("decision_id") or collected_at or "").strip()
    now = now_fn()
    threshold = max(30, int(confirm_seconds or 180))
    required_bad_checks = max(1, int(bad_checks_required or 2))

    if (
        not normalized_path_key
        or not normalized_server_id
        or not collected_at
        or not decision_id
        or not bool(traffic_signal.get("response_observed"))
    ):
        return {
            "confirmed": False,
            "pending": False,
            "reason": "active_quality_not_evaluable",
            "confirm_seconds": threshold,
            "bad_checks_required": required_bad_checks,
        }

    global _TRAFFIC_FAILURE_CANDIDATE
    with _TRAFFIC_FAILURE_LOCK:
        state = load_watchdog_runtime_state()
        candidate = state.get("failure_candidate")
        if not isinstance(candidate, dict):
            candidate = _TRAFFIC_FAILURE_CANDIDATE
        if (
            not isinstance(candidate, dict)
            or candidate.get("kind") != "active_quality_degraded"
            or candidate.get("path_key") != normalized_path_key
            or candidate.get("server_id") != normalized_server_id
        ):
            candidate = {
                "kind": "active_quality_degraded",
                "path_key": normalized_path_key,
                "server_id": normalized_server_id,
                "first_seen_at": now.isoformat(),
                "last_seen_at": now.isoformat(),
                "last_collected_at": collected_at,
                "decision_id": decision_id,
                "bad_checks": 1,
                "good_checks": 0,
                "active_check": _compact_active_quality_check(active_check),
                "traffic_signal": _compact_quality_traffic_signal(traffic_signal),
            }
            _TRAFFIC_FAILURE_CANDIDATE = candidate
            update_watchdog_runtime_state(
                path_key=normalized_path_key,
                failure_candidate=candidate,
                last_processed_decision_id=decision_id,
            )
            return {
                "confirmed": False,
                "pending": False,
                "reason": "first_active_quality_degraded_check",
                "path_key": normalized_path_key,
                "server_id": normalized_server_id,
                "first_seen_at": candidate["first_seen_at"],
                "last_seen_at": candidate["last_seen_at"],
                "decision_id": decision_id,
                "bad_checks": 1,
                "good_checks": 0,
                "confirm_seconds": threshold,
                "bad_checks_required": required_bad_checks,
            }

        first_seen_at = parse_timestamp(str(candidate.get("first_seen_at") or ""))
        if first_seen_at is None:
            first_seen_at = now
            candidate["first_seen_at"] = first_seen_at.isoformat()
        if candidate.get("decision_id") != decision_id:
            candidate["bad_checks"] = int(candidate.get("bad_checks") or 0) + 1
            candidate["good_checks"] = 0
            candidate["last_collected_at"] = collected_at
            candidate["decision_id"] = decision_id
        candidate["last_seen_at"] = now.isoformat()
        candidate["active_check"] = _compact_active_quality_check(active_check)
        candidate["latest_signal"] = _compact_quality_traffic_signal(traffic_signal)

        bad_checks = int(candidate.get("bad_checks") or 0)
        age_seconds = max(0, int((now - first_seen_at).total_seconds()))
        _TRAFFIC_FAILURE_CANDIDATE = candidate
        update_watchdog_runtime_state(
            path_key=normalized_path_key,
            failure_candidate=candidate,
            last_processed_decision_id=decision_id,
        )

        if bad_checks < required_bad_checks:
            return {
                "confirmed": False,
                "pending": False,
                "reason": "active_quality_bad_checks_window",
                "path_key": normalized_path_key,
                "server_id": normalized_server_id,
                "first_seen_at": first_seen_at.isoformat(),
                "last_seen_at": candidate["last_seen_at"],
                "decision_id": decision_id,
                "bad_checks": bad_checks,
                "good_checks": int(candidate.get("good_checks") or 0),
                "age_seconds": age_seconds,
                "confirm_seconds": threshold,
                "bad_checks_required": required_bad_checks,
            }

        if age_seconds < threshold:
            return {
                "confirmed": False,
                "pending": True,
                "reason": "active_quality_confirmation_window",
                "path_key": normalized_path_key,
                "server_id": normalized_server_id,
                "first_seen_at": first_seen_at.isoformat(),
                "last_seen_at": candidate["last_seen_at"],
                "decision_id": decision_id,
                "bad_checks": bad_checks,
                "good_checks": int(candidate.get("good_checks") or 0),
                "age_seconds": age_seconds,
                "confirm_seconds": threshold,
                "bad_checks_required": required_bad_checks,
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
            "reason": "active_quality_degraded_confirmed",
            "path_key": normalized_path_key,
            "server_id": normalized_server_id,
            "first_seen_at": first_seen_at.isoformat(),
            "last_seen_at": candidate["last_seen_at"],
            "decision_id": decision_id,
            "bad_checks": bad_checks,
            "age_seconds": age_seconds,
            "confirm_seconds": threshold,
            "bad_checks_required": required_bad_checks,
        }


def active_quality_recovery_confirmation(
    *,
    active_server_id: str | None,
    traffic_signal: dict[str, Any],
    recovery_checks_required: int,
    path_key: str | None,
    now_fn: Callable[[], datetime],
) -> dict[str, Any]:
    normalized_server_id = str(active_server_id or "").strip()
    normalized_path_key = str(path_key or normalized_server_id or "").strip()
    decision_id = str(traffic_signal.get("decision_id") or traffic_signal.get("last_collected_at") or "").strip()
    required_good_checks = max(1, int(recovery_checks_required or 2))

    global _TRAFFIC_FAILURE_CANDIDATE
    with _TRAFFIC_FAILURE_LOCK:
        state = load_watchdog_runtime_state()
        candidate = state.get("failure_candidate")
        if not isinstance(candidate, dict):
            candidate = _TRAFFIC_FAILURE_CANDIDATE
        if (
            not isinstance(candidate, dict)
            or candidate.get("kind") != "active_quality_degraded"
            or candidate.get("path_key") != normalized_path_key
            or candidate.get("server_id") != normalized_server_id
        ):
            return {
                "recovered": False,
                "pending": False,
                "reason": "no_active_quality_candidate",
                "good_checks_required": required_good_checks,
            }

        if candidate.get("decision_id") != decision_id:
            candidate["good_checks"] = int(candidate.get("good_checks") or 0) + 1
            candidate["decision_id"] = decision_id
        candidate["last_seen_at"] = now_fn().isoformat()
        good_checks = int(candidate.get("good_checks") or 0)
        if good_checks >= required_good_checks:
            _TRAFFIC_FAILURE_CANDIDATE = None
            update_watchdog_runtime_state(
                path_key=normalized_path_key,
                failure_candidate=None,
                last_processed_decision_id=decision_id or None,
            )
            return {
                "recovered": True,
                "pending": False,
                "reason": "active_quality_recovered",
                "path_key": normalized_path_key,
                "server_id": normalized_server_id,
                "good_checks": good_checks,
                "good_checks_required": required_good_checks,
            }

        _TRAFFIC_FAILURE_CANDIDATE = candidate
        update_watchdog_runtime_state(
            path_key=normalized_path_key,
            failure_candidate=candidate,
            last_processed_decision_id=decision_id or None,
        )
        return {
            "recovered": False,
            "pending": True,
            "reason": "active_quality_recovery_window",
            "path_key": normalized_path_key,
            "server_id": normalized_server_id,
            "good_checks": good_checks,
            "good_checks_required": required_good_checks,
        }


def _compact_active_quality_check(active_check: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": bool(active_check.get("ok")),
        "status": active_check.get("status"),
        "server_id": active_check.get("server_id"),
        "last_ping_ms": active_check.get("last_ping_ms"),
        "error_code": active_check.get("error_code"),
        "error_message": active_check.get("error_message"),
    }


def _compact_quality_traffic_signal(traffic_signal: dict[str, Any]) -> dict[str, Any]:
    return {
        "response_observed": bool(traffic_signal.get("response_observed")),
        "outbound_observed": bool(traffic_signal.get("outbound_observed")),
        "authoritative_rx_delta": traffic_signal.get("authoritative_rx_delta"),
        "authoritative_tx_delta": traffic_signal.get("authoritative_tx_delta"),
        "active_samples_count": traffic_signal.get("active_samples_count"),
    }


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
