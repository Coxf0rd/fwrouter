from __future__ import annotations

import json
from typing import Any

from fwrouter_api.db.connection import db_session
from fwrouter_api.services.subject_taxonomy import explicit_external_client_allows_virtual_vpn_auto


VIRTUAL_XRAY_VPN_AUTO_SERVER_ID = "virtual:xray:vpn-auto"
MANUAL_SERVER_TTL_HOURS = 24
GLOBAL_FIXED_SERVER_TTL_HOURS = 24


from fwrouter_api.services.server_global_selection import _validate_user_selectable_server


def _get_subject_row(subject_id: str) -> Any | None:
    with db_session() as connection:
        return connection.execute(
            """
            SELECT
                subject_id,
                subject_type,
                stable_key,
                display_name,
                alias,
                desired_mode,
                applied_mode,
                apply_state,
                runtime_state,
                is_active,
                is_deleted,
                updated_at
            FROM subjects
            WHERE subject_id = ?
              AND COALESCE(is_deleted, 0) = 0
            """,
            (subject_id,),
        ).fetchone()


def set_subject_server_override(
    subject_id: str,
    server_id: str,
    *,
    requested_by: str = "user",
) -> dict[str, Any]:
    """Persist user/device manual server override with 24h TTL.

    User manual selected server is valid only if the server is active and
    currently available in at least one user-visible list: vpn-auto or global-list.
    This function only stores desired override state. Runtime materialization
    depends on subject-specific scoped egress support:
    - LAN and Tailscale-node subjects can materialize inside the owned nft contour.
    - Xray subjects keep the override in control-plane/runtime state, but still
      require a future Xray-specific runtime matcher before they can be applied.
    """

    subject = _get_subject_row(subject_id)
    if subject is None:
        return {
            "ok": False,
            "subject_id": subject_id,
            "server": None,
            "error_code": "SUBJECT_NOT_FOUND",
            "error_message": f"Subject not found or deleted: {subject_id}",
        }

    subject_type = str(subject["subject_type"] or "")
    if str(server_id or "").strip() == VIRTUAL_XRAY_VPN_AUTO_SERVER_ID:
        if not explicit_external_client_allows_virtual_vpn_auto(subject_type):
            return {
                "ok": False,
                "subject_id": subject_id,
                "subject": dict(subject),
                "server": None,
                "error_code": "SERVER_OVERRIDE_VPN_AUTO_XRAY_ONLY",
                "error_message": "Virtual vpn-auto override is supported only for compatible explicit external clients.",
            }
        validation = {
            "ok": True,
            "error_code": None,
            "error_message": None,
            "server": {
                "server_id": server_id,
                "server_name": "vpn-auto",
                "inventory_state": "active",
                "vpn_auto": True,
                "global_list": True,
                "virtual": True,
            },
        }
    else:
        validation = _validate_user_selectable_server(server_id)

    if not validation["ok"]:
        return {
            "ok": False,
            "subject_id": subject_id,
            "subject": dict(subject),
            "server": validation["server"],
            "error_code": validation["error_code"],
            "error_message": validation["error_message"],
        }

    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO subject_server_overrides (
                subject_id,
                selected_server_id,
                selected_until,
                apply_state,
                error_code,
                error_message,
                updated_at
            )
            VALUES (
                ?,
                ?,
                datetime('now', '+' || ? || ' hours'),
                'pending',
                NULL,
                NULL,
                CURRENT_TIMESTAMP
            )
            ON CONFLICT(subject_id) DO UPDATE SET
                selected_server_id = excluded.selected_server_id,
                selected_until = excluded.selected_until,
                apply_state = 'pending',
                error_code = NULL,
                error_message = NULL,
                updated_at = CURRENT_TIMESTAMP
            """,
            (subject_id, server_id, MANUAL_SERVER_TTL_HOURS),
        )

        row = connection.execute(
            """
            SELECT
                subject_id,
                selected_server_id,
                selected_until,
                apply_state,
                error_code,
                error_message,
                updated_at
            FROM subject_server_overrides
            WHERE subject_id = ?
            """,
            (subject_id,),
        ).fetchone()

    return {
        "ok": True,
        "requested_by": requested_by,
        "override": dict(row),
        "server": validation["server"],
    }


def clear_subject_server_override(
    subject_id: str,
    *,
    requested_by: str = "user",
) -> dict[str, Any]:
    """Clear manual server override and return subject to global/auto behavior."""

    with db_session() as connection:
        row_before = connection.execute(
            """
            SELECT
                subject_id,
                selected_server_id,
                selected_until,
                apply_state,
                error_code,
                error_message,
                updated_at
            FROM subject_server_overrides
            WHERE subject_id = ?
            """,
            (subject_id,),
        ).fetchone()

        connection.execute(
            """
            DELETE FROM subject_server_overrides
            WHERE subject_id = ?
            """,
            (subject_id,),
        )

    return {
        "ok": True,
        "requested_by": requested_by,
        "subject_id": subject_id,
        "cleared_override": dict(row_before) if row_before else None,
    }


def get_subject_server_override(subject_id: str) -> dict[str, Any] | None:
    """Return non-expired manual server override for one subject."""

    with db_session() as connection:
        row = connection.execute(
            """
            SELECT
                subject_id,
                selected_server_id,
                selected_until,
                apply_state,
                error_code,
                error_message,
                updated_at
            FROM subject_server_overrides
            WHERE subject_id = ?
              AND selected_until > CURRENT_TIMESTAMP
            """,
            (subject_id,),
        ).fetchone()

    return dict(row) if row else None


def update_subject_server_override_apply_status(
    subject_id: str,
    *,
    apply_state: str,
    error_code: str | None = None,
    error_message: str | None = None,
) -> dict[str, Any] | None:
    with db_session() as connection:
        connection.execute(
            """
            UPDATE subject_server_overrides
            SET
                apply_state = ?,
                error_code = ?,
                error_message = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE subject_id = ?
            """,
            (apply_state, error_code, error_message, subject_id),
        )

    return get_subject_server_override(subject_id)
