from __future__ import annotations

import json
from typing import Any

from fwrouter_api.db.connection import db_session


WATCHDOG_STATE_ROW_ID = 1
_UNSET = object()


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


def ensure_watchdog_runtime_state_row() -> None:
    with db_session() as connection:
        connection.execute(
            """
            INSERT OR IGNORE INTO watchdog_state (id)
            VALUES (1)
            """
        )


def empty_watchdog_runtime_state() -> dict[str, Any]:
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


def load_watchdog_runtime_state() -> dict[str, Any]:
    try:
        ensure_watchdog_runtime_state_row()
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
        return empty_watchdog_runtime_state()
    if row is None:
        return empty_watchdog_runtime_state()
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


def update_watchdog_runtime_state(
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
        ensure_watchdog_runtime_state_row()
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
            return load_watchdog_runtime_state()
        assignments.append("updated_at = CURRENT_TIMESTAMP")
        params.append(WATCHDOG_STATE_ROW_ID)
        with db_session() as connection:
            connection.execute(
                f"UPDATE watchdog_state SET {', '.join(assignments)} WHERE id = ?",
                tuple(params),
            )
    except Exception:
        return load_watchdog_runtime_state()
    return load_watchdog_runtime_state()
