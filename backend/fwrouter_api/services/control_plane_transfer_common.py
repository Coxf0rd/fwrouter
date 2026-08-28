from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import db_session


CONTROL_PLANE_SNAPSHOT_VERSION = "2026-05-14.control-plane-transfer.v2"
TRANSFER_DIRNAME = "transfer"
CONTROL_PLANE_TABLES = (
    "settings",
    "modules",
    "subjects",
    "subject_lan",
    "subject_docker",
    "subject_host",
    "subject_fwrouter",
    "servers",
    "server_custom_https_proxy",
    "server_preferences",
    "server_ping_state",
    "routing_global_state",
    "subject_server_overrides",
    "subject_user_overrides",
    "subscription_state",
    "rules_state",
    "rules_metadata",
)


def _utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _fetch_rows(query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with db_session() as connection:
        rows = connection.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def _fetch_one(query: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    with db_session() as connection:
        row = connection.execute(query, params).fetchone()
    return dict(row) if row else None


def _json_loads_or_none(value: str | None) -> Any:
    if not value:
        return None
    return json.loads(value)


def _detail_table_for_subject_type(subject_type: str) -> str | None:
    mapping = {
        "lan": "subject_lan",
        "docker": "subject_docker",
        "host": "subject_host",
        "fwrouter": "subject_fwrouter",
    }
    return mapping.get(subject_type)


def _transfer_dir() -> Path:
    path = get_settings().paths.state_dir / TRANSFER_DIRNAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def _snapshot_file_path() -> Path:
    return _transfer_dir() / f"control-plane-snapshot.{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"


def _parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def _state_from_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    state = snapshot.get("state")
    return state if isinstance(state, dict) else {}


def _insert_rows(connection, query: str, rows: list[tuple[Any, ...]]) -> None:  # noqa: ANN001
    if rows:
        connection.executemany(query, rows)
