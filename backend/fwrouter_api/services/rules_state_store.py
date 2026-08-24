from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fwrouter_api.services import rules as rules_service


def _default_rules_paths() -> dict[str, Path]:
    paths = rules_service.get_settings().paths
    rules_dir = paths.rules_dir
    generated_rules_dir = paths.generated_dir / "rules"
    last_good_rules_dir = paths.state_dir / "last-good" / "rules"

    return {
        "manual_draft_path": rules_dir / "manual.draft.txt",
        "manual_active_path": rules_dir / "manual.active.txt",
        "static_direct_path": rules_dir / "static-direct.active.txt",
        "big_direct_path": rules_dir / "big-direct.active.txt",
        "big_vpn_path": rules_dir / "big-vpn.active.txt",
        "effective_candidate_json_path": generated_rules_dir / "effective-rules.candidate.json",
        "effective_candidate_text_path": generated_rules_dir / "effective-rules.candidate.txt",
        "effective_json_path": generated_rules_dir / "effective-rules.json",
        "effective_text_path": generated_rules_dir / "effective-rules.txt",
        "metadata_path": generated_rules_dir / "metadata.json",
        "last_good_manual_active_path": last_good_rules_dir / "manual.active.txt",
        "last_good_big_direct_path": last_good_rules_dir / "big-direct.active.txt",
        "last_good_big_vpn_path": last_good_rules_dir / "big-vpn.active.txt",
        "last_good_effective_json_path": last_good_rules_dir / "effective-rules.json",
        "last_good_effective_text_path": last_good_rules_dir / "effective-rules.txt",
        "last_good_metadata_path": last_good_rules_dir / "metadata.json",
    }


def _normalize_path(value: str | None, fallback: Path) -> Path:
    return Path(value) if value else fallback


def _read_text_if_exists(path: Path | None) -> str | None:
    if path is None or not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def _read_json_if_exists(path: Path | None) -> dict[str, Any] | None:
    text = _read_text_if_exists(path)
    if text is None:
        return None
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError:
        return None
    return loaded if isinstance(loaded, dict) else {"value": loaded}


def _json_dumps(value: dict[str, Any] | None) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _json_loads(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    loaded = json.loads(value)
    return loaded if isinstance(loaded, dict) else {"value": loaded}


def _default_rules_state() -> dict[str, Any]:
    defaults = _default_rules_paths()
    return {
        "manual_draft_path": str(defaults["manual_draft_path"]),
        "manual_active_path": str(defaults["manual_active_path"]),
        "static_direct_path": str(defaults["static_direct_path"]),
        "big_direct_path": str(defaults["big_direct_path"]),
        "big_vpn_path": str(defaults["big_vpn_path"]),
        "effective_json_path": str(defaults["effective_json_path"]),
        "effective_text_path": str(defaults["effective_text_path"]),
        "metadata_path": str(defaults["metadata_path"]),
        "effective_path": str(defaults["effective_json_path"]),
        "selective_default": "direct",
        "last_apply_job_id": None,
        "last_update_job_id": None,
        "status": "not_configured",
        "last_success_at": None,
        "last_failed_at": None,
        "error_code": None,
        "error_message": None,
        "updated_at": None,
    }


def _row_to_rules_state(row: Any | None) -> dict[str, Any]:
    defaults = _default_rules_paths()
    if row is None:
        return _default_rules_state()

    return {
        "manual_draft_path": str(_normalize_path(row["manual_draft_path"], defaults["manual_draft_path"])),
        "manual_active_path": str(_normalize_path(row["manual_active_path"], defaults["manual_active_path"])),
        "static_direct_path": str(_normalize_path(row["static_direct_path"], defaults["static_direct_path"])),
        "big_direct_path": str(_normalize_path(row["big_direct_path"], defaults["big_direct_path"])),
        "big_vpn_path": str(_normalize_path(row["big_vpn_path"], defaults["big_vpn_path"])),
        "effective_json_path": str(_normalize_path(row["effective_json_path"], defaults["effective_json_path"])),
        "effective_text_path": str(_normalize_path(row["effective_text_path"], defaults["effective_text_path"])),
        "metadata_path": str(_normalize_path(row["metadata_path"], defaults["metadata_path"])),
        "effective_path": str(_normalize_path(row["effective_json_path"], defaults["effective_json_path"])),
        "selective_default": row["selective_default"],
        "last_apply_job_id": row["last_apply_job_id"],
        "last_update_job_id": row["last_update_job_id"],
        "status": row["status"],
        "last_success_at": row["last_success_at"],
        "last_failed_at": row["last_failed_at"],
        "error_code": row["error_code"],
        "error_message": row["error_message"],
        "updated_at": row["updated_at"],
    }


def get_rules_state() -> dict[str, Any]:
    with rules_service.db_session() as connection:
        row = connection.execute(
            """
            SELECT
                manual_draft_path,
                manual_active_path,
                static_direct_path,
                big_direct_path,
                big_vpn_path,
                effective_json_path,
                effective_text_path,
                metadata_path,
                selective_default,
                last_apply_job_id,
                last_update_job_id,
                status,
                last_success_at,
                last_failed_at,
                error_code,
                error_message,
                updated_at
            FROM rules_state
            WHERE id = 1
            """
        ).fetchone()
    return _row_to_rules_state(row)


def _upsert_rules_state_record(state: dict[str, Any]) -> dict[str, Any]:
    with rules_service.db_session() as connection:
        connection.execute(
            """
            INSERT INTO rules_state (
                id,
                manual_draft_path,
                manual_active_path,
                static_direct_path,
                big_direct_path,
                big_vpn_path,
                effective_json_path,
                effective_text_path,
                metadata_path,
                selective_default,
                last_apply_job_id,
                last_update_job_id,
                status,
                last_success_at,
                last_failed_at,
                error_code,
                error_message,
                updated_at
            )
            VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(id) DO UPDATE SET
                manual_draft_path = excluded.manual_draft_path,
                manual_active_path = excluded.manual_active_path,
                static_direct_path = excluded.static_direct_path,
                big_direct_path = excluded.big_direct_path,
                big_vpn_path = excluded.big_vpn_path,
                effective_json_path = excluded.effective_json_path,
                effective_text_path = excluded.effective_text_path,
                metadata_path = excluded.metadata_path,
                selective_default = excluded.selective_default,
                last_apply_job_id = excluded.last_apply_job_id,
                last_update_job_id = excluded.last_update_job_id,
                status = excluded.status,
                last_success_at = excluded.last_success_at,
                last_failed_at = excluded.last_failed_at,
                error_code = excluded.error_code,
                error_message = excluded.error_message,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                state["manual_draft_path"],
                state["manual_active_path"],
                state["static_direct_path"],
                state["big_direct_path"],
                state["big_vpn_path"],
                state["effective_json_path"],
                state["effective_text_path"],
                state["metadata_path"],
                state["selective_default"],
                state["last_apply_job_id"],
                state["last_update_job_id"],
                state["status"],
                state["last_success_at"],
                state["last_failed_at"],
                state["error_code"],
                state["error_message"],
            ),
        )
    return get_rules_state()


def _rules_state_with_updates(**updates: Any) -> dict[str, Any]:
    state = get_rules_state()
    state.update(updates)
    return _upsert_rules_state_record(state)
