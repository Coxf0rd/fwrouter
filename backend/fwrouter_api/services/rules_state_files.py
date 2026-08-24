from __future__ import annotations

from pathlib import Path
from typing import Any

from fwrouter_api.services import rules as rules_service
from fwrouter_api.services.rules_state_store import (
    _default_rules_paths,
    _read_json_if_exists,
    _read_text_if_exists,
    get_rules_state,
)


def _ensure_seed_files(paths: dict[str, Path]) -> None:
    for key in ("static_direct_path", "big_direct_path", "big_vpn_path"):
        path = paths[key]
        if not path.exists():
            rules_service.atomic_write_text(path, "")


def get_manual_rules_texts() -> dict[str, Any]:
    state = get_rules_state()
    paths = {key: Path(value) for key, value in state.items() if key.endswith("_path")}
    _ensure_seed_files(paths)

    return {
        "state": state,
        "draft_path": paths["manual_draft_path"],
        "active_path": paths["manual_active_path"],
        "static_direct_path": paths["static_direct_path"],
        "big_direct_path": paths["big_direct_path"],
        "big_vpn_path": paths["big_vpn_path"],
        "effective_json_path": paths["effective_json_path"],
        "effective_text_path": paths["effective_text_path"],
        "metadata_path": paths["metadata_path"],
        "draft_text": _read_text_if_exists(paths["manual_draft_path"]) or "",
        "active_text": _read_text_if_exists(paths["manual_active_path"]) or "",
        "static_direct_text": _read_text_if_exists(paths["static_direct_path"]) or "",
        "big_direct_text": _read_text_if_exists(paths["big_direct_path"]) or "",
        "big_vpn_text": _read_text_if_exists(paths["big_vpn_path"]) or "",
        "effective": _read_json_if_exists(paths["effective_json_path"]),
        "effective_text": _read_text_if_exists(paths["effective_text_path"]),
        "metadata": _read_json_if_exists(paths["metadata_path"]),
        "last_good_paths": {key: value for key, value in _default_rules_paths().items() if key.startswith("last_good_")},
    }


def _build_metadata_file(
    *,
    job_id: str,
    status: str,
    selective_default: str,
    source_counts: dict[str, Any],
    effective_counts: dict[str, Any],
    versions: dict[str, Any] | None = None,
    source_urls: dict[str, list[str]] | None = None,
    fetch_summary: dict[str, Any] | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "last_job_id": job_id,
        "selective_default": selective_default,
        "source_counts": source_counts,
        "effective_counts": effective_counts,
        "versions": versions or {},
        "source_urls": source_urls or {},
        "fetch_summary": fetch_summary or {},
        "rules_pipeline_version": rules_service.RULES_PIPELINE_VERSION,
        "last_error_code": error_code,
        "last_error_message": error_message,
        "updated_at": rules_service._utc_now_iso(),
    }


def _mirror_file(source: Path, destination: Path) -> None:
    if source.exists():
        rules_service.atomic_write_text(destination, source.read_text(encoding="utf-8"))
    elif destination.exists():
        destination.unlink()


def _snapshot_last_good_rules(paths: dict[str, Any]) -> None:
    _mirror_file(paths["active_path"], paths["last_good_paths"]["last_good_manual_active_path"])
    _mirror_file(paths["big_direct_path"], paths["last_good_paths"]["last_good_big_direct_path"])
    _mirror_file(paths["big_vpn_path"], paths["last_good_paths"]["last_good_big_vpn_path"])
    _mirror_file(paths["effective_json_path"], paths["last_good_paths"]["last_good_effective_json_path"])
    _mirror_file(paths["effective_text_path"], paths["last_good_paths"]["last_good_effective_text_path"])
    _mirror_file(paths["metadata_path"], paths["last_good_paths"]["last_good_metadata_path"])


def restore_last_good_rules() -> dict[str, str]:
    current = get_manual_rules_texts()
    _mirror_file(current["last_good_paths"]["last_good_manual_active_path"], current["active_path"])
    _mirror_file(current["last_good_paths"]["last_good_big_direct_path"], current["big_direct_path"])
    _mirror_file(current["last_good_paths"]["last_good_big_vpn_path"], current["big_vpn_path"])
    _mirror_file(current["last_good_paths"]["last_good_effective_json_path"], current["effective_json_path"])
    _mirror_file(current["last_good_paths"]["last_good_effective_text_path"], current["effective_text_path"])
    _mirror_file(current["last_good_paths"]["last_good_metadata_path"], current["metadata_path"])
    return {
        "manual_active_path": str(current["active_path"]),
        "big_direct_path": str(current["big_direct_path"]),
        "big_vpn_path": str(current["big_vpn_path"]),
        "effective_json_path": str(current["effective_json_path"]),
        "effective_text_path": str(current["effective_text_path"]),
        "metadata_path": str(current["metadata_path"]),
    }


def write_rules_candidate(
    *,
    job_id: str,
    effective_artifact: dict[str, Any],
    candidate_text: str,
    downloads: dict[str, str] | None = None,
    download_metadata: dict[str, Any] | None = None,
    validations: dict[str, dict[str, Any]] | None = None,
) -> dict[str, str]:
    paths = _default_rules_paths()
    rules_service.atomic_write_json(paths["effective_candidate_json_path"], effective_artifact)
    rules_service.atomic_write_text(paths["effective_candidate_text_path"], candidate_text)

    rules_service.write_job_json_artifact(job_id, "rules/effective-rules.candidate.json", effective_artifact)
    rules_service.write_job_text_artifact(job_id, "rules/effective-rules.candidate.txt", candidate_text)

    for name, text in (downloads or {}).items():
        rules_service.write_job_text_artifact(job_id, f"rules/downloaded/{name}.txt", text)

    for name, metadata in (download_metadata or {}).items():
        rules_service.write_job_json_artifact(job_id, f"rules/downloaded/{name}.json", metadata)

    for name, validation in (validations or {}).items():
        rules_service.write_job_json_artifact(job_id, f"rules/validated/{name}.json", validation)
        rules_service.write_job_text_artifact(
            job_id,
            f"rules/validated/{name}.txt",
            str(validation.get("normalized_text") or ""),
        )

    return {
        "effective_candidate_json_path": str(paths["effective_candidate_json_path"]),
        "effective_candidate_text_path": str(paths["effective_candidate_text_path"]),
    }


def write_active_rules_state(
    *,
    manual_active_text: str | None,
    big_direct_text: str | None,
    big_vpn_text: str | None,
    effective_artifact: dict[str, Any],
    metadata: dict[str, Any],
) -> dict[str, Any]:
    current = get_manual_rules_texts()
    _snapshot_last_good_rules(current)

    if manual_active_text is not None:
        rules_service.atomic_write_text(current["active_path"], manual_active_text)
    if big_direct_text is not None:
        rules_service.atomic_write_text(current["big_direct_path"], big_direct_text)
    if big_vpn_text is not None:
        rules_service.atomic_write_text(current["big_vpn_path"], big_vpn_text)

    effective_text = rules_service.render_effective_rules_text(effective_artifact)
    rules_service.atomic_write_json(current["effective_json_path"], effective_artifact)
    rules_service.atomic_write_text(current["effective_text_path"], effective_text)
    rules_service.atomic_write_json(current["metadata_path"], metadata)

    return {
        "manual_active_path": str(current["active_path"]),
        "big_direct_path": str(current["big_direct_path"]),
        "big_vpn_path": str(current["big_vpn_path"]),
        "effective_json_path": str(current["effective_json_path"]),
        "effective_text_path": str(current["effective_text_path"]),
        "effective_path": str(current["effective_json_path"]),
        "metadata_path": str(current["metadata_path"]),
    }
