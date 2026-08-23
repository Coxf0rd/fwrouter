from __future__ import annotations

from typing import Any, Callable

from fwrouter_api.db.connection import db_session
from fwrouter_api.services.live_probe_cache import get_live_probe_cache
from fwrouter_api.services.servers import ensure_routing_global_state
from fwrouter_api.services.subject_policy import list_subjects_with_effective_state
from fwrouter_api.services.subject_taxonomy import (
    subject_follows_global_mode,
)


SCOPED_VPN_SUBJECTS_CACHE_TTL_SECONDS = 30


def load_watchdog_module() -> dict[str, Any] | None:
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


def update_watchdog_module(
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

    return load_watchdog_module()


def load_routing_state() -> dict[str, Any] | None:
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


def routing_mode(routing: dict[str, Any] | None) -> str:
    state = routing or {}
    return str(state.get("desired_mode") or state.get("applied_mode") or "direct")


def compute_has_scoped_vpn_subjects() -> bool:
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


def has_scoped_vpn_subjects(
    *,
    loader: Callable[[], bool] = compute_has_scoped_vpn_subjects,
) -> bool:
    return bool(
        get_live_probe_cache(
            "watchdog.has_scoped_vpn_subjects",
            ttl_seconds=SCOPED_VPN_SUBJECTS_CACHE_TTL_SECONDS,
            loader=loader,
        )
    )
