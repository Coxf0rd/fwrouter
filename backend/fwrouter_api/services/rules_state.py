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


def list_rules_metadata() -> list[dict[str, Any]]:
    with rules_service.db_session() as connection:
        rows = connection.execute(
            """
            SELECT
                ruleset_id,
                ruleset_type,
                version_name,
                source_url,
                active_path,
                downloaded_at,
                activated_at,
                status,
                last_success_at,
                last_failed_at,
                last_error_code,
                last_error_message,
                last_job_id,
                metadata_json
            FROM rules_metadata
            ORDER BY ruleset_type, ruleset_id
            """
        ).fetchall()

    return [
        {
            "ruleset_id": row["ruleset_id"],
            "ruleset_type": row["ruleset_type"],
            "version_name": row["version_name"],
            "source_url": row["source_url"],
            "active_path": row["active_path"],
            "downloaded_at": row["downloaded_at"],
            "activated_at": row["activated_at"],
            "status": row["status"],
            "last_success_at": row["last_success_at"],
            "last_failed_at": row["last_failed_at"],
            "last_error_code": row["last_error_code"],
            "last_error_message": row["last_error_message"],
            "last_job_id": row["last_job_id"],
            "metadata_json": _json_loads(row["metadata_json"]),
        }
        for row in rows
    ]


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


def _upsert_ruleset_metadata(
    *,
    ruleset_type: str,
    active_path: str,
    status: str,
    job_id: str,
    metadata: dict[str, Any],
    version_name: str | None = None,
    source_urls: list[str] | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> None:
    now = rules_service._utc_now_iso()
    source_url = ",".join(source_urls or [])
    with rules_service.db_session() as connection:
        connection.execute(
            """
            INSERT INTO rules_metadata (
                ruleset_id,
                ruleset_type,
                version_name,
                source_url,
                active_path,
                downloaded_at,
                activated_at,
                status,
                last_success_at,
                last_failed_at,
                last_error_code,
                last_error_message,
                last_job_id,
                metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ruleset_id) DO UPDATE SET
                ruleset_type = excluded.ruleset_type,
                version_name = excluded.version_name,
                source_url = excluded.source_url,
                active_path = excluded.active_path,
                downloaded_at = excluded.downloaded_at,
                activated_at = excluded.activated_at,
                status = excluded.status,
                last_success_at = excluded.last_success_at,
                last_failed_at = excluded.last_failed_at,
                last_error_code = excluded.last_error_code,
                last_error_message = excluded.last_error_message,
                last_job_id = excluded.last_job_id,
                metadata_json = excluded.metadata_json
            """,
            (
                ruleset_type,
                ruleset_type,
                version_name,
                source_url,
                active_path,
                now,
                now if status in {"active", "success"} else None,
                status,
                now if status in {"active", "success"} else None,
                now if status == "failed" else None,
                error_code,
                error_message,
                job_id,
                _json_dumps(metadata),
            ),
        )


def update_rules_metadata_records(
    *,
    job_id: str,
    effective_artifact: dict[str, Any],
    big_direct_version: str | None = None,
    big_vpn_version: str | None = None,
    source_urls: dict[str, list[str]] | None = None,
    fetch_summary: dict[str, Any] | None = None,
    status: str = "active",
    error_code: str | None = None,
    error_message: str | None = None,
) -> None:
    paths = get_manual_rules_texts()
    counts = effective_artifact.get("source_counts", {})
    effective_counts = effective_artifact.get("effective_counts", {})
    urls = source_urls or {}

    rows = [
        (rules_service.RULESET_MANUAL, str(paths["active_path"]), None, {"count": counts.get(rules_service.RULESET_MANUAL, 0)}),
        (rules_service.RULESET_STATIC_DIRECT, str(paths["static_direct_path"]), None, {"count": counts.get(rules_service.RULESET_STATIC_DIRECT, 0)}),
        (
            rules_service.RULESET_BIG_DIRECT,
            str(paths["big_direct_path"]),
            big_direct_version,
            {"count": counts.get(rules_service.RULESET_BIG_DIRECT, 0), "fetch_summary": (fetch_summary or {}).get(rules_service.RULESET_BIG_DIRECT, {})},
        ),
        (
            rules_service.RULESET_BIG_VPN,
            str(paths["big_vpn_path"]),
            big_vpn_version,
            {"count": counts.get(rules_service.RULESET_BIG_VPN, 0), "fetch_summary": (fetch_summary or {}).get(rules_service.RULESET_BIG_VPN, {})},
        ),
        (
            rules_service.RULESET_EFFECTIVE,
            str(paths["effective_json_path"]),
            None,
            {"source_counts": counts, "effective_counts": effective_counts, "selective_default": effective_artifact.get("selective_default"), "fetch_summary": fetch_summary or {}},
        ),
    ]

    for ruleset_type, active_path, version_name, metadata in rows:
        _upsert_ruleset_metadata(
            ruleset_type=ruleset_type,
            active_path=active_path,
            status=status,
            job_id=job_id,
            metadata=metadata,
            version_name=version_name,
            source_urls=urls.get(ruleset_type, []),
            error_code=error_code,
            error_message=error_message,
        )


def mark_rules_metadata_update_failed(
    *,
    job_id: str,
    code: str,
    message: str,
) -> None:
    """Record a failed update without replacing last known active metadata."""
    now = rules_service._utc_now_iso()
    paths = get_manual_rules_texts()
    rows = [
        (rules_service.RULESET_MANUAL, str(paths["active_path"]), ""),
        (rules_service.RULESET_STATIC_DIRECT, str(paths["static_direct_path"]), ""),
        (
            rules_service.RULESET_BIG_DIRECT,
            str(paths["big_direct_path"]),
            ",".join(rules_service._configured_rules_sources().get(rules_service.RULESET_BIG_DIRECT, [])),
        ),
        (
            rules_service.RULESET_BIG_VPN,
            str(paths["big_vpn_path"]),
            ",".join(rules_service._configured_rules_sources().get(rules_service.RULESET_BIG_VPN, [])),
        ),
        (rules_service.RULESET_EFFECTIVE, str(paths["effective_json_path"]), ""),
    ]

    with rules_service.db_session() as connection:
        for ruleset_type, active_path, source_url in rows:
            existing = connection.execute(
                "SELECT status FROM rules_metadata WHERE ruleset_id = ?",
                (ruleset_type,),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO rules_metadata (
                        ruleset_id,
                        ruleset_type,
                        source_url,
                        active_path,
                        downloaded_at,
                        activated_at,
                        status,
                        last_failed_at,
                        last_error_code,
                        last_error_message,
                        last_job_id,
                        metadata_json
                    )
                    VALUES (?, ?, ?, ?, ?, NULL, 'failed', ?, ?, ?, ?, ?)
                    """,
                    (
                        ruleset_type,
                        ruleset_type,
                        source_url,
                        active_path,
                        now,
                        now,
                        code,
                        message,
                        job_id,
                        _json_dumps({"count": 0}),
                    ),
                )
                continue

            preserved_status = str(existing["status"] or "")
            next_status = "failed" if preserved_status in {"", "not_configured", "running"} else preserved_status
            connection.execute(
                """
                UPDATE rules_metadata
                SET
                    source_url = CASE WHEN ? != '' THEN ? ELSE source_url END,
                    active_path = ?,
                    status = ?,
                    last_failed_at = ?,
                    last_error_code = ?,
                    last_error_message = ?,
                    last_job_id = ?
                WHERE ruleset_id = ?
                """,
                (
                    source_url,
                    source_url,
                    active_path,
                    next_status,
                    now,
                    code,
                    message,
                    job_id,
                    ruleset_type,
                ),
            )


def mark_rules_job_running(*, job_id: str, update_type: str) -> dict[str, Any]:
    state = get_rules_state()
    return _upsert_rules_state_record(
        {
            **state,
            "status": "running",
            "last_apply_job_id": job_id if update_type == "manual_apply" else state["last_apply_job_id"],
            "last_update_job_id": job_id if update_type == "full_update" else state["last_update_job_id"],
            "error_code": None,
            "error_message": None,
        }
    )


def _repair_stale_running_rules_state(state: dict[str, Any]) -> dict[str, Any]:
    if state.get("status") != "running":
        return state

    from fwrouter_api.services import jobs as jobs_service

    active_job = jobs_service.find_active_lock_conflict(rules_service.LOCK_RULES_APPLY)
    if active_job is not None:
        return state

    now = rules_service._utc_now_iso()
    return _upsert_rules_state_record(
        {
            **state,
            "status": "failed",
            "last_failed_at": now,
            "error_code": "RULES_JOB_STALE",
            "error_message": "Rules state was running, but no active rules job exists.",
        }
    )


def mark_rules_job_failed(
    *,
    job_id: str,
    code: str,
    message: str,
    update_type: str,
    effective_artifact: dict[str, Any] | None = None,
    source_urls: dict[str, list[str]] | None = None,
    fetch_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    del update_type
    now = rules_service._utc_now_iso()
    state = get_rules_state()
    updated = _upsert_rules_state_record(
        {**state, "status": "failed", "last_failed_at": now, "error_code": code, "error_message": message}
    )
    del effective_artifact, source_urls, fetch_summary
    mark_rules_metadata_update_failed(job_id=job_id, code=code, message=message)
    return updated


def mark_rules_job_success(
    *,
    job_id: str,
    update_type: str,
) -> dict[str, Any]:
    now = rules_service._utc_now_iso()
    state = get_rules_state()
    updated = _upsert_rules_state_record(
        {
            **state,
            "status": "success",
            "last_apply_job_id": job_id if update_type == "manual_apply" else state["last_apply_job_id"],
            "last_update_job_id": job_id if update_type == "full_update" else state["last_update_job_id"],
            "last_success_at": now,
            "error_code": None,
            "error_message": None,
        }
    )
    return updated


def get_rules_overview() -> dict[str, Any]:
    state = _repair_stale_running_rules_state(get_rules_state())
    texts = get_manual_rules_texts()
    metadata = texts["metadata"] if isinstance(texts["metadata"], dict) else {}
    return {
        "state": state,
        "metadata": list_rules_metadata(),
        "sources": {
            "configured": rules_service._configured_rules_sources(),
            "last_effective": {
                "versions": metadata.get("versions", {}),
                "source_urls": metadata.get("source_urls", {}),
                "fetch_summary": metadata.get("fetch_summary", {}),
            },
        },
        "manual": {
            "draft_text": texts["draft_text"] or None,
            "active_text": texts["active_text"] or None,
            "draft_validation": rules_service.validate_manual_rules(texts["draft_text"] or ""),
            "active_validation": rules_service.validate_manual_rules(texts["active_text"] or ""),
            "effective": texts["effective"],
        },
        "lists": {
            "static_direct_text": texts["static_direct_text"] or "",
            "big_direct_text": texts["big_direct_text"] or "",
            "big_vpn_text": texts["big_vpn_text"] or "",
        },
        "artifacts": {
            "effective_text": texts["effective_text"],
            "metadata": texts["metadata"],
            "candidate_json_path": str(_default_rules_paths()["effective_candidate_json_path"]),
            "candidate_text_path": str(_default_rules_paths()["effective_candidate_text_path"]),
        },
    }


def get_rules_summary() -> dict[str, Any]:
    state = _repair_stale_running_rules_state(get_rules_state())
    defaults = _default_rules_paths()
    metadata_file = _read_json_if_exists(defaults["metadata_path"])
    metadata = metadata_file if isinstance(metadata_file, dict) else {}
    draft_text = _read_text_if_exists(defaults["manual_draft_path"]) or ""
    active_text = _read_text_if_exists(defaults["manual_active_path"]) or ""

    return {
        "state": state,
        "metadata": list_rules_metadata(),
        "sources": {
            "configured": rules_service._configured_rules_sources(),
            "last_effective": {
                "versions": metadata.get("versions", {}),
                "source_urls": metadata.get("source_urls", {}),
                "fetch_summary": metadata.get("fetch_summary", {}),
            },
        },
        "manual": {
            "draft_text": draft_text or None,
            "active_text": active_text or None,
            "draft_validation": rules_service.validate_manual_rules(draft_text),
            "active_validation": rules_service.validate_manual_rules(active_text),
        },
    }


def save_manual_draft(text: str) -> dict[str, Any]:
    defaults = _default_rules_paths()
    rules_service.atomic_write_text(defaults["manual_draft_path"], text)
    validation = rules_service.validate_manual_rules(text)
    _rules_state_with_updates(
        manual_draft_path=str(defaults["manual_draft_path"]),
        manual_active_path=str(defaults["manual_active_path"]),
        static_direct_path=str(defaults["static_direct_path"]),
        big_direct_path=str(defaults["big_direct_path"]),
        big_vpn_path=str(defaults["big_vpn_path"]),
        effective_json_path=str(defaults["effective_json_path"]),
        effective_text_path=str(defaults["effective_text_path"]),
        metadata_path=str(defaults["metadata_path"]),
        status="pending",
        error_code=None,
        error_message=None,
    )
    overview = get_rules_overview()
    overview["manual"]["draft_validation"] = validation
    return overview


def get_effective_rules() -> dict[str, Any]:
    texts = get_manual_rules_texts()
    return {
        "effective": texts["effective"],
        "effective_text": texts["effective_text"],
        "metadata": texts["metadata"],
        "paths": {
            "effective_json_path": str(texts["effective_json_path"]),
            "effective_text_path": str(texts["effective_text_path"]),
            "metadata_path": str(texts["metadata_path"]),
        },
    }
