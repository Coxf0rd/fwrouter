from __future__ import annotations

from typing import Any

from fwrouter_api.adapters.xray import XrayClient
from fwrouter_api.db.connection import db_session
from fwrouter_api.services.subject_inventory import sync_subject_inventory
from fwrouter_api.services.subject_policy import get_subject_with_effective_state


def _subscription_path(client_id: str) -> str:
    return f"/api/v2/xray/clients/{client_id}/subscription"


def _client_alias_map() -> dict[str, str | None]:
    with db_session() as connection:
        rows = connection.execute(
            """
            SELECT sx.client_id, sx.client_uuid, s.alias
            FROM subject_xray AS sx
            JOIN subjects AS s ON s.subject_id = sx.subject_id
            """
        ).fetchall()

    aliases: dict[str, str | None] = {}
    for row in rows:
        if row["client_id"]:
            aliases[str(row["client_id"])] = row["alias"]
        if row["client_uuid"]:
            aliases[str(row["client_uuid"])] = row["alias"]
    return aliases


def _xray_subject_for_client(client_id: str) -> dict[str, Any] | None:
    with db_session() as connection:
        row = connection.execute(
            """
            SELECT subject_id
            FROM subject_xray
            WHERE client_id = ? OR client_uuid = ?
            LIMIT 1
            """,
            (client_id, client_id),
        ).fetchone()
    if row is None:
        return None
    return get_subject_with_effective_state(str(row["subject_id"]))


def _tombstone_local_xray_subject(client_id: str) -> dict[str, Any]:
    with db_session() as connection:
        row = connection.execute(
            """
            SELECT
                s.subject_id,
                s.display_name,
                s.alias,
                s.is_active,
                s.is_deleted,
                sx.client_id,
                sx.client_uuid,
                sx.email
            FROM subject_xray AS sx
            JOIN subjects AS s ON s.subject_id = sx.subject_id
            WHERE sx.client_id = ? OR sx.client_uuid = ? OR s.subject_id = ?
            LIMIT 1
            """,
            (client_id, client_id, client_id),
        ).fetchone()
        if row is None or bool(row["is_deleted"]):
            return {"deleted": False, "client": None}

        connection.execute(
            """
            UPDATE subjects
            SET
                is_deleted = 1,
                is_active = 0,
                runtime_state = 'inactive',
                deleted_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE subject_id = ?
            """,
            (str(row["subject_id"]),),
        )

    return {
        "deleted": True,
        "client": {
            "subject_id": str(row["subject_id"]),
            "display_name": row["display_name"],
            "alias": row["alias"],
            "client_id": row["client_id"],
            "client_uuid": row["client_uuid"],
            "email": row["email"],
            "was_active": bool(row["is_active"]),
        },
    }


def _serialize_client(client: XrayClient, *, alias_override: str | None = None) -> dict[str, Any]:
    alias = alias_override if alias_override is not None else client.alias
    return {
        "client_id": client.client_id,
        "client_uuid": client.client_uuid,
        "email": client.email,
        "alias": alias,
        "enabled": client.enabled,
        "subscription_path": _subscription_path(client.client_id),
        "raw": client.raw,
    }


def _set_local_alias(client_id: str, alias: str | None) -> None:
    normalized_alias = alias.strip() if isinstance(alias, str) else None
    if normalized_alias == "":
        normalized_alias = None

    with db_session() as connection:
        row = connection.execute(
            """
            SELECT subject_id
            FROM subject_xray
            WHERE client_id = ? OR client_uuid = ?
            LIMIT 1
            """,
            (client_id, client_id),
        ).fetchone()

        if row is None:
            return

        connection.execute(
            """
            UPDATE subjects
            SET alias = ?, updated_at = CURRENT_TIMESTAMP
            WHERE subject_id = ?
            """,
            (normalized_alias, row["subject_id"]),
        )


def _sync_xray_inventory(requested_by: str) -> dict[str, Any]:
    return sync_subject_inventory(
        requested_by=requested_by,
        discover_docker=False,
        discover_tailscale=False,
        discover_xray=True,
    )
