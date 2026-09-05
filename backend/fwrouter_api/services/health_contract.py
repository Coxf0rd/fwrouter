from __future__ import annotations

from typing import Any, Literal


UserHealth = Literal[
    "healthy",
    "warning",
    "degraded",
    "failed",
    "inactive",
    "disabled",
    "unknown",
]

USER_HEALTH_STATES = {
    "healthy",
    "warning",
    "degraded",
    "failed",
    "inactive",
    "disabled",
    "unknown",
}

_HEALTH_RANK = {
    "healthy": 0,
    "inactive": 0,
    "disabled": 0,
    "unknown": 1,
    "warning": 2,
    "degraded": 3,
    "failed": 4,
}

_ALIASES = {
    "ok": "healthy",
    "success": "healthy",
    "clean": "healthy",
    "idle": "healthy",
    "active": "healthy",
    "running": "healthy",
    "in_sync": "healthy",
    "error": "failed",
    "failure": "failed",
    "critical": "failed",
    "runtime_failed": "failed",
    "unavailable": "failed",
    "drift": "degraded",
    "runtime_drift": "degraded",
    "failed_adapter": "degraded",
    "missing": "degraded",
    "stale": "warning",
    "observation_stale": "warning",
    "intent_newer_than_runtime": "warning",
    "legacy_ambiguous": "warning",
    "pending": "warning",
    "applying": "warning",
    "not_applicable": "inactive",
    "not_configured": "inactive",
    "stopped": "inactive",
    "paused": "inactive",
}

_SEVERITY_BY_HEALTH = {
    "healthy": "none",
    "warning": "warning",
    "degraded": "warning",
    "failed": "error",
    "inactive": "info",
    "disabled": "none",
    "unknown": "warning",
}


def normalize_health_state(value: Any, *, default: UserHealth = "unknown") -> UserHealth:
    normalized = str(value or "").strip().lower()
    if normalized in USER_HEALTH_STATES:
        return normalized  # type: ignore[return-value]
    return _ALIASES.get(normalized, default)  # type: ignore[return-value]


def health_severity(value: Any) -> str:
    return _SEVERITY_BY_HEALTH[normalize_health_state(value)]


def max_health(values: list[Any], *, ignore_inactive: bool = True) -> UserHealth:
    selected: UserHealth = "healthy"
    saw_unknown = False
    for value in values:
        state = normalize_health_state(value)
        if ignore_inactive and state in {"inactive", "disabled"}:
            continue
        if state == "unknown":
            saw_unknown = True
        if _HEALTH_RANK[state] > _HEALTH_RANK[selected]:
            selected = state  # type: ignore[assignment]
    if selected == "healthy" and saw_unknown:
        return "unknown"
    return selected
