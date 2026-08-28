from __future__ import annotations

import json
from typing import Any

from fwrouter_api.db.connection import db_session
from fwrouter_api.services.subject_taxonomy import normalize_subject_type


DETAIL_TABLE_BY_TYPE = {
    "lan": "subject_lan",
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

    aliased = SUBJECT_TYPE_FILTER_ALIASES.get(normalized, normalized)
    return normalize_subject_type(aliased)


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
        "subject_role": row["subject_role"],
        "implementation_kind": row["implementation_kind"],
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


def _metadata_detail(subject: dict[str, Any]) -> dict[str, Any] | None:
    metadata = subject.get("metadata") if isinstance(subject.get("metadata"), dict) else {}
    detail = metadata.get("detail") if isinstance(metadata.get("detail"), dict) else None
    return dict(detail) if detail else None


def get_subject_detail(subject_id: str, subject_type: str) -> dict[str, Any] | None:
    """Return type-specific details for one subject."""

    table_name = DETAIL_TABLE_BY_TYPE.get(canonical_subject_type(subject_type) or subject_type)
    if table_name is None:
        with db_session() as connection:
            row = connection.execute(
                "SELECT * FROM subjects WHERE subject_id = ?",
                (subject_id,),
            ).fetchone()
        if row is None:
            return None
        return _metadata_detail(_row_to_subject(row))

    with db_session() as connection:
        row = connection.execute(
            f"SELECT * FROM {table_name} WHERE subject_id = ?",
            (subject_id,),
        ).fetchone()

    return _row_to_detail(row)


def _load_subject_details(subjects: list[dict[str, Any]]) -> dict[str, dict[str, Any] | None]:
    groups: dict[str, list[str]] = {}
    details: dict[str, dict[str, Any] | None] = {}
    for subject in subjects:
        subject_id = str(subject.get("subject_id") or "")
        table_name = DETAIL_TABLE_BY_TYPE.get(
            canonical_subject_type(str(subject.get("subject_type") or "")) or str(subject.get("subject_type") or "")
        )
        if not subject_id or table_name is None:
            if subject_id and table_name is None:
                detail = _metadata_detail(subject)
                if detail is not None:
                    details[subject_id] = detail
            continue
        groups.setdefault(table_name, []).append(subject_id)

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
                WHERE s.is_active = 1
                  AND s.is_deleted = 0
                  AND s.subject_role = 'external_network_source'
                  AND (
                      json_extract(s.metadata_json, '$.detail.provider_ip') = ?
                      OR json_extract(s.metadata_json, '$.detail.ip_address') = ?
                      OR json_extract(s.metadata_json, '$.detail.tailscale_ip') = ?
                  )
                ORDER BY COALESCE(s.last_seen_at, s.updated_at, s.created_at) DESC
                LIMIT 1
                """,
                (normalized_ip, normalized_ip, normalized_ip),
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
        if normalized_subject_type is None:
            return []
        legacy_values = {
            "external_network_client": ("external_network_client", "tailscale", "tailscale_node"),
            "explicit_external_client": ("explicit_external_client", "xray"),
        }.get(normalized_subject_type, (normalized_subject_type,))
        placeholders = ", ".join("?" for _ in legacy_values)
        where.append(f"subject_type IN ({placeholders})")
        params.extend(legacy_values)

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
