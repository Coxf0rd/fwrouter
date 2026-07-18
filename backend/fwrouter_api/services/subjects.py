from __future__ import annotations

import json
from typing import Any

from fwrouter_api.db.connection import db_session


DETAIL_TABLE_BY_TYPE = {
    "lan": "subject_lan",
    "tailscale": "subject_tailscale",
    "tailscale_node": "subject_tailscale",
    "xray": "subject_xray",
    "docker": "subject_docker",
    "host": "subject_host",
    "fwrouter": "subject_fwrouter",
}
SUBJECT_TYPE_FILTER_ALIASES = {
    "tailscale-nodes": "tailscale_node",
    "tailscale_nodes": "tailscale_node",
    "tailscale-node": "tailscale_node",
}


def canonical_subject_type(subject_type: str | None) -> str | None:
    if subject_type is None:
        return None

    normalized = subject_type.strip().lower()
    if not normalized:
        return None

    if normalized == "tailscale":
        return "tailscale_node"

    return SUBJECT_TYPE_FILTER_ALIASES.get(normalized, normalized)


def _json_loads(value: str | None) -> dict[str, Any] | None:
    if not value:
        return None

    loaded = json.loads(value)
    if isinstance(loaded, dict):
        return loaded

    return {"value": loaded}


def _row_to_subject(row: Any) -> dict[str, Any]:
    raw_subject_type = str(row["subject_type"])
    canonical_type = canonical_subject_type(raw_subject_type) or raw_subject_type
    return {
        "subject_id": row["subject_id"],
        "subject_type": canonical_type,
        "stored_subject_type": raw_subject_type,
        "stable_key": row["stable_key"],
        "display_name": row["display_name"],
        "alias": row["alias"],
        "desired_mode": row["desired_mode"],
        "applied_mode": row["applied_mode"],
        "apply_state": row["apply_state"],
        "runtime_state": row["runtime_state"],
        "is_active": bool(row["is_active"]),
        "is_deleted": bool(row["is_deleted"]),
        "first_seen_at": row["first_seen_at"],
        "last_seen_at": row["last_seen_at"],
        "last_traffic_at": row["last_traffic_at"],
        "inactive_since": row["inactive_since"],
        "deleted_at": row["deleted_at"],
        "metadata": _json_loads(row["metadata_json"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _row_to_detail(row: Any | None) -> dict[str, Any] | None:
    if row is None:
        return None

    result: dict[str, Any] = {}
    for key in row.keys():
        if key == "source_json":
            result["source"] = _json_loads(row[key])
        else:
            result[key] = row[key]

    return result


def get_subject_detail(subject_id: str, subject_type: str) -> dict[str, Any] | None:
    """Return type-specific details for one subject."""

    table_name = DETAIL_TABLE_BY_TYPE.get(canonical_subject_type(subject_type) or subject_type)
    if table_name is None:
        return None

    with db_session() as connection:
        row = connection.execute(
            f"SELECT * FROM {table_name} WHERE subject_id = ?",
            (subject_id,),
        ).fetchone()

    return _row_to_detail(row)


def _load_subject_details(subjects: list[dict[str, Any]]) -> dict[str, dict[str, Any] | None]:
    groups: dict[str, list[str]] = {}
    for subject in subjects:
        subject_id = str(subject.get("subject_id") or "")
        table_name = DETAIL_TABLE_BY_TYPE.get(
            canonical_subject_type(str(subject.get("subject_type") or "")) or str(subject.get("subject_type") or "")
        )
        if not subject_id or table_name is None:
            continue
        groups.setdefault(table_name, []).append(subject_id)

    details: dict[str, dict[str, Any] | None] = {}
    if not groups:
        return details

    with db_session() as connection:
        for table_name, subject_ids in groups.items():
            unique_subject_ids = list(dict.fromkeys(subject_ids))
            placeholders = ",".join("?" for _ in unique_subject_ids)
            rows = connection.execute(
                f"SELECT * FROM {table_name} WHERE subject_id IN ({placeholders})",
                tuple(unique_subject_ids),
            ).fetchall()
            for row in rows:
                details[str(row["subject_id"])] = _row_to_detail(row)

    return details


def get_subject(subject_id: str) -> dict[str, Any] | None:
    """Return one subject with type-specific details."""

    with db_session() as connection:
        row = connection.execute(
            "SELECT * FROM subjects WHERE subject_id = ?",
            (subject_id,),
        ).fetchone()

    if row is None:
        return None

    subject = _row_to_subject(row)
    subject["detail"] = get_subject_detail(
        subject_id=subject["subject_id"],
        subject_type=subject["subject_type"],
    )
    return subject


def find_subject_by_ip(ip_address: str) -> dict[str, Any] | None:
    normalized_ip = str(ip_address or "").strip()
    if not normalized_ip:
        return None

    with db_session() as connection:
        row = connection.execute(
            """
            SELECT s.*
            FROM subjects AS s
            JOIN subject_lan AS sl ON sl.subject_id = s.subject_id
            WHERE s.is_active = 1
              AND s.is_deleted = 0
              AND sl.ip_address = ?
            ORDER BY COALESCE(s.last_seen_at, s.updated_at, s.created_at) DESC
            LIMIT 1
            """,
            (normalized_ip,),
        ).fetchone()
        if row is None:
            row = connection.execute(
                """
                SELECT s.*
                FROM subjects AS s
                JOIN subject_tailscale AS st ON st.subject_id = s.subject_id
                WHERE s.is_active = 1
                  AND s.is_deleted = 0
                  AND st.tailscale_ip = ?
                ORDER BY COALESCE(s.last_seen_at, s.updated_at, s.created_at) DESC
                LIMIT 1
                """,
                (normalized_ip,),
            ).fetchone()

    if row is not None:
        subject = _row_to_subject(row)
        subject["detail"] = get_subject_detail(
            subject_id=subject["subject_id"],
            subject_type=subject["subject_type"],
        )
        return subject
    return None


def update_subject_alias(subject_id: str, alias: str | None) -> dict[str, Any] | None:
    normalized_alias = str(alias or "").strip() or None

    with db_session() as connection:
        row = connection.execute(
            "SELECT subject_id FROM subjects WHERE subject_id = ?",
            (subject_id,),
        ).fetchone()
        if row is None:
            return None

        connection.execute(
            """
            UPDATE subjects
            SET alias = ?, updated_at = CURRENT_TIMESTAMP
            WHERE subject_id = ?
            """,
            (normalized_alias, subject_id),
        )

    return get_subject(subject_id)


def list_subjects(
    *,
    subject_type: str | None = None,
    is_active: bool | None = None,
    include_deleted: bool = False,
    include_detail: bool = True,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Return subjects from SQLite.

    This is read-only and does not run discovery.
    """

    safe_limit = max(1, min(limit, 500))

    where: list[str] = []
    params: list[Any] = []

    if subject_type:
        normalized_subject_type = canonical_subject_type(subject_type)
        if normalized_subject_type not in DETAIL_TABLE_BY_TYPE:
            return []
        if normalized_subject_type == "tailscale_node":
            where.append("subject_type IN ('tailscale_node', 'tailscale')")
        else:
            where.append("subject_type = ?")
            params.append(normalized_subject_type)

    if is_active is not None:
        where.append("is_active = ?")
        params.append(1 if is_active else 0)

    if not include_deleted:
        where.append("is_deleted = 0")

    where_sql = f"WHERE {' AND '.join(where)}" if where else ""

    with db_session() as connection:
        rows = connection.execute(
            f"""
            SELECT *
            FROM subjects
            {where_sql}
            ORDER BY is_active DESC, last_seen_at DESC, created_at DESC
            LIMIT ?
            """,
            (*params, safe_limit),
        ).fetchall()

    subjects = [_row_to_subject(row) for row in rows]
    if not include_detail:
        return subjects

    details = _load_subject_details(subjects)
    for subject in subjects:
        subject["detail"] = details.get(str(subject["subject_id"]))
    return subjects
