from __future__ import annotations

import hashlib
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
            SELECT
                json_extract(s.metadata_json, '$.detail.client_id') AS client_id,
                json_extract(s.metadata_json, '$.detail.client_uuid') AS client_uuid,
                s.alias
            FROM subjects AS s
            WHERE s.implementation_kind = 'xray'
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
            FROM subjects
            WHERE implementation_kind = 'xray'
              AND (
                  json_extract(metadata_json, '$.detail.client_id') = ?
                  OR json_extract(metadata_json, '$.detail.client_uuid') = ?
              )
            LIMIT 1
            """,
            (client_id, client_id),
        ).fetchone()
    if row is None:
        return None
    return get_subject_with_effective_state(str(row["subject_id"]))


def _delete_xray_subject_projections(subject_ids: list[str]) -> dict[str, Any]:
    scoped_subject_ids = [str(subject_id) for subject_id in subject_ids if str(subject_id or "").strip()]
    if not scoped_subject_ids:
        return {
            "subject_ids": [],
            "subjects_deleted": 0,
            "server_overrides_deleted": 0,
            "user_overrides_deleted": 0,
        }

    placeholders = ", ".join("?" for _ in scoped_subject_ids)
    with db_session() as connection:
        server_overrides = connection.execute(
            f"SELECT count(*) AS count FROM subject_server_overrides WHERE subject_id IN ({placeholders})",
            tuple(scoped_subject_ids),
        ).fetchone()["count"]
        user_overrides = connection.execute(
            f"SELECT count(*) AS count FROM subject_user_overrides WHERE subject_id IN ({placeholders})",
            tuple(scoped_subject_ids),
        ).fetchone()["count"]
        subjects = connection.execute(
            f"""
            SELECT count(*) AS count
            FROM subjects
            WHERE subject_id IN ({placeholders})
              AND implementation_kind = 'xray'
              AND subject_type = 'explicit_external_client'
              AND subject_role = 'vless_client'
            """,
            tuple(scoped_subject_ids),
        ).fetchone()["count"]

        connection.execute(
            f"DELETE FROM subject_server_overrides WHERE subject_id IN ({placeholders})",
            tuple(scoped_subject_ids),
        )
        connection.execute(
            f"DELETE FROM subject_user_overrides WHERE subject_id IN ({placeholders})",
            tuple(scoped_subject_ids),
        )
        connection.execute(
            f"""
            DELETE FROM subjects
            WHERE subject_id IN ({placeholders})
              AND implementation_kind = 'xray'
              AND subject_type = 'explicit_external_client'
              AND subject_role = 'vless_client'
            """,
            tuple(scoped_subject_ids),
        )

    return {
        "subject_ids": scoped_subject_ids,
        "subjects_deleted": int(subjects or 0),
        "server_overrides_deleted": int(server_overrides or 0),
        "user_overrides_deleted": int(user_overrides or 0),
    }


def cleanup_xray_client_projection(client_id: str) -> dict[str, Any]:
    normalized = str(client_id or "").strip()
    if not normalized:
        return _delete_xray_subject_projections([])

    with db_session() as connection:
        rows = connection.execute(
            """
            SELECT subject_id
            FROM subjects
            WHERE implementation_kind = 'xray'
              AND subject_type = 'explicit_external_client'
              AND subject_role = 'vless_client'
              AND (
                  json_extract(metadata_json, '$.detail.client_id') = ?
                  OR json_extract(metadata_json, '$.detail.client_uuid') = ?
                  OR subject_id = ?
                  OR subject_id = ?
              )
            """,
            (normalized, normalized, normalized, f"xray:{normalized}"),
        ).fetchall()

    return _delete_xray_subject_projections([str(row["subject_id"]) for row in rows])


def _subscription_token_digest(token: str) -> str:
    return hashlib.sha1(str(token or "").encode("utf-8")).hexdigest()[:10]


def cleanup_xray_subscription_profile_projection(token_or_slug: str) -> dict[str, Any]:
    token = str(token_or_slug or "").strip().lower()
    if not token:
        return _delete_xray_subject_projections([])

    profile_email_prefix = f"sub-{_subscription_token_digest(token)}-"
    with db_session() as connection:
        rows = connection.execute(
            """
            SELECT subject_id
            FROM subjects
            WHERE implementation_kind = 'xray'
              AND subject_type = 'explicit_external_client'
              AND subject_role = 'vless_client'
              AND (
                  lower(coalesce(json_extract(metadata_json, '$.detail.email'), '')) IN (?, ?)
                  OR lower(coalesce(json_extract(metadata_json, '$.detail.source.email'), '')) IN (?, ?)
                  OR lower(coalesce(json_extract(metadata_json, '$.detail.email'), '')) LIKE ?
                  OR lower(coalesce(json_extract(metadata_json, '$.detail.source.email'), '')) LIKE ?
              )
            """,
            (
                token,
                f"{token}@fwrouter.local",
                token,
                f"{token}@fwrouter.local",
                f"{profile_email_prefix}%@fwrouter.local",
                f"{profile_email_prefix}%@fwrouter.local",
            ),
        ).fetchall()

    return _delete_xray_subject_projections([str(row["subject_id"]) for row in rows])


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
                json_extract(s.metadata_json, '$.detail.client_id') AS client_id,
                json_extract(s.metadata_json, '$.detail.client_uuid') AS client_uuid,
                json_extract(s.metadata_json, '$.detail.email') AS email
            FROM subjects AS s
            WHERE s.implementation_kind = 'xray'
              AND (
                  json_extract(s.metadata_json, '$.detail.client_id') = ?
                  OR json_extract(s.metadata_json, '$.detail.client_uuid') = ?
                  OR s.subject_id = ?
              )
            LIMIT 1
            """,
            (client_id, client_id, client_id),
        ).fetchone()
        if row is None or bool(row["is_deleted"]):
            return {"deleted": False, "client": None}

    cleanup = _delete_xray_subject_projections([str(row["subject_id"])])

    return {
        "deleted": cleanup["subjects_deleted"] > 0,
        "client": {
            "subject_id": str(row["subject_id"]),
            "display_name": row["display_name"],
            "alias": row["alias"],
            "client_id": row["client_id"],
            "client_uuid": row["client_uuid"],
            "email": row["email"],
            "was_active": bool(row["is_active"]),
        },
        "cleanup": cleanup,
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
            FROM subjects
            WHERE implementation_kind = 'xray'
              AND (
                  json_extract(metadata_json, '$.detail.client_id') = ?
                  OR json_extract(metadata_json, '$.detail.client_uuid') = ?
              )
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
