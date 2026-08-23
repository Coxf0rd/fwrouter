from __future__ import annotations

import json
from typing import Any

from fwrouter_api.db.connection import db_session
from fwrouter_api.services.subject_taxonomy import explicit_external_client_allows_virtual_vpn_auto


VIRTUAL_XRAY_VPN_AUTO_SERVER_ID = "virtual:xray:vpn-auto"
MANUAL_SERVER_TTL_HOURS = 24
GLOBAL_FIXED_SERVER_TTL_HOURS = 24


def get_routing_global_state() -> dict[str, Any] | None:
    """Return global routing/server selection state."""

    with db_session() as connection:
        row = connection.execute(
            """
            SELECT
                id,
                desired_mode,
                applied_mode,
                selective_default,
                server_mode,
                desired_fixed_server_id,
                applied_fixed_server_id,
                fixed_server_until,
                active_auto_server_id,
                apply_state,
                error_code,
                error_message,
                updated_at
            FROM routing_global_state
            WHERE id = 1
            """
        ).fetchone()

    if row is None:
        return None
    if row["server_mode"] == "fixed" and row["fixed_server_until"] is not None:
        # Do not compare against local process time; SQLite CURRENT_TIMESTAMP is
        # the same clock used when writing the TTL.
        with db_session() as connection:
            expired_now = connection.execute(
                """
                SELECT fixed_server_until <= CURRENT_TIMESTAMP
                FROM routing_global_state
                WHERE id = 1
                """
            ).fetchone()
        if expired_now is not None and bool(expired_now[0]):
            _clear_expired_global_fixed_server_state(row)
            return get_routing_global_state()

    return {
        "desired_mode": row["desired_mode"],
        "applied_mode": row["applied_mode"],
        "selective_default": row["selective_default"],
        "server_mode": row["server_mode"],
        "desired_fixed_server_id": row["desired_fixed_server_id"],
        "applied_fixed_server_id": row["applied_fixed_server_id"],
        "fixed_server_until": row["fixed_server_until"],
        "active_auto_server_id": row["active_auto_server_id"],
        "apply_state": row["apply_state"],
        "error_code": row["error_code"],
        "error_message": row["error_message"],
        "updated_at": row["updated_at"],
    }


def _clear_expired_global_fixed_server_state(row: Any) -> None:
    write_details = {
        "desired_fixed_server_id": row["desired_fixed_server_id"],
        "applied_fixed_server_id": row["applied_fixed_server_id"],
        "fixed_server_until": row["fixed_server_until"],
    }
    with db_session() as connection:
        connection.execute(
            """
            UPDATE routing_global_state
            SET
                server_mode = 'auto',
                desired_fixed_server_id = NULL,
                applied_fixed_server_id = NULL,
                fixed_server_until = NULL,
                apply_state = 'pending',
                error_code = NULL,
                error_message = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = 1
              AND server_mode = 'fixed'
              AND fixed_server_until IS NOT NULL
              AND fixed_server_until <= CURRENT_TIMESTAMP
            """
        )

    from fwrouter_api.services.logs import write_operational_log

    write_operational_log(
        event_type="global_fixed_server_expired",
        message="Global fixed server TTL expired and desired state was returned to auto.",
        details=write_details,
    )


def expire_global_fixed_server(
    *,
    dry_run: bool = True,
    apply_runtime: bool = False,
) -> dict[str, Any]:
    with db_session() as connection:
        row = connection.execute(
            """
            SELECT
                desired_fixed_server_id,
                applied_fixed_server_id,
                fixed_server_until
            FROM routing_global_state
            WHERE id = 1
              AND server_mode = 'fixed'
              AND fixed_server_until IS NOT NULL
              AND fixed_server_until <= CURRENT_TIMESTAMP
            """
        ).fetchone()

        expired = dict(row) if row is not None else None

    if expired is not None and not dry_run:
        _clear_expired_global_fixed_server_state(expired)
        if apply_runtime:
            from fwrouter_api.services.server_global_selection import apply_global_auto_server

            runtime_apply = apply_global_auto_server(requested_by="global_fixed_server_ttl")
        else:
            runtime_apply = {"skipped": True}
    else:
        runtime_apply = {"skipped": True}

    return {
        "dry_run": dry_run,
        "runtime_apply": runtime_apply,
        "expired_global_fixed_server_count": 1 if expired is not None else 0,
        "expired_global_fixed_server": expired,
    }


def ensure_routing_global_state() -> dict[str, Any]:
    """Ensure singleton global routing state row exists."""

    with db_session() as connection:
        connection.execute(
            """
            INSERT OR IGNORE INTO routing_global_state (
                id,
                desired_mode,
                selective_default,
                server_mode,
                apply_state
            )
            VALUES (1, 'direct', 'direct', 'auto', 'clean')
            """
        )

    state = get_routing_global_state()
    if state is None:
        raise RuntimeError("Failed to initialize routing_global_state row")
    return state


def set_global_mode(
    mode: str,
    *,
    requested_by: str = "api",
) -> dict[str, Any]:
    from fwrouter_api.services.apply_orchestrator import set_global_mode as run_global_mode_transaction

    return run_global_mode_transaction(mode, requested_by=requested_by)


def reconcile_current_routing_if_drift(
    *,
    requested_by: str = "api",
) -> dict[str, Any]:
    from fwrouter_api.services.apply_orchestrator import (
        reconcile_current_routing_if_drift as run_routing_drift_reconcile,
    )

    return run_routing_drift_reconcile(requested_by=requested_by)


def set_selective_default(
    selective_default: str,
    *,
    requested_by: str = "api",
) -> dict[str, Any]:
    from fwrouter_api.services.apply_orchestrator import set_selective_default as run_selective_default_transaction

    return run_selective_default_transaction(
        selective_default,
        requested_by=requested_by,
    )
