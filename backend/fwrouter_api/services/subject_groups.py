from __future__ import annotations

from typing import Any

from fwrouter_api.db.connection import db_session


XRAY_SUBSCRIPTION_GROUP_PREFIX = "xray-subscription:"


def _localpart(email: str) -> str:
    return str(email or "").split("@", 1)[0].strip().lower()


def xray_subscription_group_from_values(
    *,
    email: str,
    alias: str | None = None,
    display_name: str | None = None,
) -> tuple[str, str] | None:
    normalized_email = str(email or "").strip().lower()
    if not normalized_email.startswith("sub-"):
        return None

    label_source = str(alias or display_name or "").strip()
    parts = [part.strip() for part in label_source.split(" / ") if part.strip()]
    if len(parts) >= 2:
        group_label = parts[1] or parts[0]
        return f"{XRAY_SUBSCRIPTION_GROUP_PREFIX}{group_label.lower()}", group_label
    if parts:
        return f"{XRAY_SUBSCRIPTION_GROUP_PREFIX}{parts[0].lower()}", parts[0]

    token = _localpart(normalized_email).rsplit("-", 1)[0]
    return f"{XRAY_SUBSCRIPTION_GROUP_PREFIX}{token}", token


def xray_subscription_group_from_row(row: Any) -> tuple[str, str] | None:
    return xray_subscription_group_from_values(
        email=str(row["email"] or ""),
        alias=str(row["alias"] or "") or None,
        display_name=str(row["display_name"] or "") or None,
    )


def resolve_xray_subscription_group_subject_ids(group_subject_id: str) -> list[str]:
    target = str(group_subject_id or "").strip().lower()
    if not target.startswith(XRAY_SUBSCRIPTION_GROUP_PREFIX):
        return []

    with db_session() as connection:
        rows = connection.execute(
            """
            SELECT s.subject_id, s.display_name, s.alias, sx.email
            FROM subjects AS s
            JOIN subject_xray AS sx ON sx.subject_id = s.subject_id
            WHERE s.is_deleted = 0
            ORDER BY COALESCE(s.last_seen_at, s.updated_at) DESC
            """
        ).fetchall()

    subject_ids: list[str] = []
    for row in rows:
        group = xray_subscription_group_from_row(row)
        if group and group[0].lower() == target:
            subject_ids.append(str(row["subject_id"]))
    return subject_ids
