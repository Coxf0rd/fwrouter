from __future__ import annotations

from typing import Any

from fwrouter_api.services import rules as rules_service
from fwrouter_api.services.rules_state_files import get_manual_rules_texts
from fwrouter_api.services.rules_state_metadata import _upsert_ruleset_metadata
from fwrouter_api.services.rules_state_store import (
    _rules_state_with_updates,
    _upsert_rules_state_record,
    get_rules_state,
)


def effective_rules_with_selective_default(
    effective_artifact: dict[str, Any] | None,
    *,
    selective_default: str,
) -> dict[str, Any]:
    normalized = str(selective_default or "").strip().lower()
    if normalized not in {"direct", "vpn"}:
        raise ValueError("selective_default must be one of: direct, vpn")

    artifact = dict(effective_artifact or {})
    if not isinstance(artifact.get("rules"), list):
        artifact["rules"] = []
    artifact["selective_default"] = normalized
    artifact["default_action"] = normalized.upper()
    artifact["generated_at"] = rules_service._utc_now_iso()
    return artifact


def _job_exists(job_id: str | None) -> bool:
    normalized = str(job_id or "").strip()
    if not normalized:
        return False
    with rules_service.db_session() as connection:
        row = connection.execute(
            "SELECT 1 FROM jobs WHERE job_id = ? LIMIT 1",
            (normalized,),
        ).fetchone()
    return row is not None


def sync_active_selective_default(
    *,
    selective_default: str,
    job_id: str | None = None,
    effective_artifact: dict[str, Any] | None = None,
) -> dict[str, Any]:
    current = get_manual_rules_texts()
    active_effective = (
        effective_artifact
        if isinstance(effective_artifact, dict)
        else current["effective"]
        if isinstance(current.get("effective"), dict)
        else None
    )
    updated_effective = effective_rules_with_selective_default(
        active_effective,
        selective_default=selective_default,
    )

    effective_text = rules_service.render_effective_rules_text(updated_effective)
    rules_service.atomic_write_json(current["effective_json_path"], updated_effective)
    rules_service.atomic_write_text(current["effective_text_path"], effective_text)

    metadata = dict(current["metadata"] if isinstance(current.get("metadata"), dict) else {})
    metadata["selective_default"] = updated_effective["selective_default"]
    metadata["default_action"] = updated_effective["default_action"]
    metadata["effective_counts"] = updated_effective.get("effective_counts", {})
    metadata["source_counts"] = updated_effective.get("source_counts", {})
    metadata["updated_at"] = rules_service._utc_now_iso()
    rules_service.atomic_write_json(current["metadata_path"], metadata)

    existing_state = get_rules_state()
    persisted_job_id = str(job_id or "").strip() if _job_exists(job_id) else None
    metadata_job_id = persisted_job_id or str(existing_state.get("last_apply_job_id") or "selective-default-sync")
    _upsert_ruleset_metadata(
        ruleset_type=rules_service.RULESET_EFFECTIVE,
        active_path=str(current["effective_json_path"]),
        status="active",
        job_id=metadata_job_id,
        metadata={
            "source_counts": updated_effective.get("source_counts", {}),
            "effective_counts": updated_effective.get("effective_counts", {}),
            "selective_default": updated_effective.get("selective_default"),
        },
    )

    return _rules_state_with_updates(
        effective_json_path=str(current["effective_json_path"]),
        effective_text_path=str(current["effective_text_path"]),
        metadata_path=str(current["metadata_path"]),
        selective_default=updated_effective["selective_default"],
        status="success",
        last_apply_job_id=persisted_job_id or existing_state.get("last_apply_job_id"),
        last_success_at=rules_service._utc_now_iso(),
        error_code=None,
        error_message=None,
    )
