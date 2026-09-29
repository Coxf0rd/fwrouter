from __future__ import annotations

import json
from typing import Any

from fwrouter_api.db.connection import db_session
from fwrouter_api.services.events import create_event_context, safe_human_label, write_audit_event
from fwrouter_api.services.subject_taxonomy import explicit_external_client_allows_virtual_vpn_auto


VIRTUAL_XRAY_VPN_AUTO_SERVER_ID = "virtual:xray:vpn-auto"
MANUAL_SERVER_TTL_HOURS = 24
GLOBAL_FIXED_SERVER_TTL_HOURS = 24


from fwrouter_api.services.server_global_selection import _validate_user_selectable_server


def _safe_server_label(connection: Any, server_id: str | None) -> str | None:
    if not server_id:
        return None
    row = connection.execute("SELECT server_name FROM servers WHERE server_id = ?", (server_id,)).fetchone()
    return safe_human_label(row["server_name"]) if row else None


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
    job_id: str | None = None,
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
        previous = connection.execute(
            """
            SELECT selected_server_id
            FROM subject_server_overrides
            WHERE subject_id = ? AND selected_until > CURRENT_TIMESTAMP
            """,
            (subject_id,),
        ).fetchone()
        previous_server_id = str(previous["selected_server_id"] or "") or None if previous else None
        previous_server_label = _safe_server_label(connection, previous_server_id)
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
        if previous_server_id != server_id:
            write_audit_event(
                actor=requested_by,
                actor_attribution="caller_supplied",
                source="api",
                action="server_assignment_changed",
                event_code="server.assignment_changed",
                legacy_event_type="mutation_set_subject_server_override_success",
                entity_type="subject",
                entity_id=subject_id,
                previous_value={
                    "server_id": previous_server_id,
                    "server_mode": "fixed" if previous_server_id else "global",
                    "server_label": previous_server_label,
                },
                new_value={
                    "server_id": server_id,
                    "server_mode": "fixed",
                    "server_label": safe_human_label(validation["server"].get("server_name")),
                },
                context=create_event_context(job_id=job_id, entity_id=subject_id),
                details={"outcome": "intent_committed"},
                connection=connection,
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
    job_id: str | None = None,
) -> dict[str, Any]:
    """Clear manual server override and return subject to global/auto behavior."""

    with db_session() as connection:
        row_before = connection.execute(
            """
            SELECT
                subject_id,
                selected_server_id,
                selected_until,
                selected_until > CURRENT_TIMESTAMP AS is_active,
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
        previous_server_id = (
            str(row_before["selected_server_id"] or "") or None
            if row_before and bool(row_before["is_active"])
            else None
        )
        if previous_server_id is not None:
            write_audit_event(
                actor=requested_by,
                actor_attribution="caller_supplied",
                source="api",
                action="server_assignment_changed",
                event_code="server.assignment_changed",
                legacy_event_type="mutation_clear_subject_server_override_success",
                entity_type="subject",
                entity_id=subject_id,
                previous_value={
                    "server_id": previous_server_id,
                    "server_mode": "fixed",
                    "server_label": _safe_server_label(connection, previous_server_id),
                },
                new_value={"server_id": None, "server_mode": "global", "server_label": None},
                context=create_event_context(job_id=job_id, entity_id=subject_id),
                details={"outcome": "intent_committed"},
                connection=connection,
            )

    cleared_override = dict(row_before) if row_before else None
    if cleared_override is not None:
        cleared_override.pop("is_active", None)
    return {
        "ok": True,
        "requested_by": requested_by,
        "subject_id": subject_id,
        "cleared_override": cleared_override,
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


def sync_applied_runtime_binding_override_statuses(bindings: list[dict[str, Any]]) -> dict[str, Any]:
    subject_ids = sorted(
        {
            str(binding.get("subject_id") or "").strip()
            for binding in bindings
            if str(binding.get("status") or "") == "applied"
            and str(binding.get("subject_id") or "").strip()
        }
    )
    if not subject_ids:
        return {"updated_count": 0, "subject_ids": []}

    placeholders = ", ".join("?" for _ in subject_ids)
    with db_session() as connection:
        updated_count = connection.execute(
            f"""
            UPDATE subject_server_overrides
            SET
                apply_state = 'clean',
                error_code = NULL,
                error_message = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE subject_id IN ({placeholders})
              AND (
                  apply_state != 'clean'
                  OR error_code IS NOT NULL
                  OR error_message IS NOT NULL
              )
            """,
            tuple(subject_ids),
        ).rowcount

    return {"updated_count": updated_count, "subject_ids": subject_ids}
