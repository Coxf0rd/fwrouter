from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import get_db_path


DEFAULT_RETENTION_DAYS_BY_STATUS = {
    "success": 1,
    "failed": 14,
    "cancelled": 1,
}

PROTECTED_STATUSES = {"queued", "running"}
ORPHAN_ARTIFACT_RETENTION_DAYS = 1
DEFAULT_MAX_COUNT_BY_STATUS = {
    "success": 500,
    "failed": 100,
    "cancelled": 100,
}
MAX_COUNT_BY_JOB_TYPE_STATUS = {
    ("apply_mutation", "success"): 20,
    ("apply_mutation", "failed"): 20,
    ("apply_control_plane_dry_run", "success"): 10,
    ("manual_apply", "failed"): 10,
    ("maintenance_cleanup", "success"): 10,
    ("traffic_accounting_collect", "success"): 500,
    ("traffic_accounting_collect", "failed"): 50,
}


def _parse_sqlite_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None

    normalized = value.replace("Z", "+00:00")

    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        try:
            parsed = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)

    return parsed.astimezone(timezone.utc)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _job_age_days(job: dict[str, Any], *, now: datetime) -> float | None:
    finished_at = _parse_sqlite_timestamp(job.get("finished_at"))
    created_at = _parse_sqlite_timestamp(job.get("created_at"))
    reference = finished_at or created_at

    if reference is None:
        return None

    return (now - reference).total_seconds() / 86400


def _job_retention_days(job: dict[str, Any]) -> int | None:
    status = str(job.get("status") or "")

    if status in PROTECTED_STATUSES:
        return None

    return DEFAULT_RETENTION_DAYS_BY_STATUS.get(status)


def _job_max_count(job: dict[str, Any]) -> int | None:
    status = str(job.get("status") or "")

    if status in PROTECTED_STATUSES:
        return None

    job_type = str(job.get("job_type") or "")
    return MAX_COUNT_BY_JOB_TYPE_STATUS.get(
        (job_type, status),
        DEFAULT_MAX_COUNT_BY_STATUS.get(status),
    )


def _path_size(path: Path) -> int:
    try:
        if path.is_file():
            return path.stat().st_size
        if path.is_dir():
            total = 0
            for child in path.rglob("*"):
                if child.is_file():
                    total += child.stat().st_size
            return total
    except OSError:
        return 0
    return 0


def _load_jobs() -> list[dict[str, Any]]:
    connection = sqlite3.connect(get_db_path())
    connection.row_factory = sqlite3.Row

    try:
        rows = connection.execute(
            """
            SELECT
                job_id,
                job_type,
                status,
                artifact_dir,
                created_at,
                started_at,
                finished_at
            FROM jobs
            ORDER BY created_at
            """
        ).fetchall()
    finally:
        connection.close()

    return [dict(row) for row in rows]


def _find_retention_candidates(
    *,
    now: datetime,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    rows = _load_jobs()
    newest_by_type_status = sorted(
        rows,
        key=lambda job: (
            _parse_sqlite_timestamp(job.get("created_at")) or datetime.min.replace(tzinfo=timezone.utc),
            str(job.get("job_id") or ""),
        ),
        reverse=True,
    )
    rank_by_job_id: dict[str, int] = {}
    seen_by_type_status: dict[tuple[str, str], int] = {}
    for job in newest_by_type_status:
        key = (str(job.get("job_type") or ""), str(job.get("status") or ""))
        seen_by_type_status[key] = seen_by_type_status.get(key, 0) + 1
        rank_by_job_id[str(job["job_id"])] = seen_by_type_status[key]

    for job in rows:
        retention_days = _job_retention_days(job)
        max_count = _job_max_count(job)
        age_days = _job_age_days(job, now=now)

        if retention_days is None and max_count is None:
            continue

        delete_due_to_age = (
            retention_days is not None
            and age_days is not None
            and age_days >= retention_days
        )
        rank = rank_by_job_id.get(str(job["job_id"]))
        delete_due_to_count = max_count is not None and rank is not None and rank > max_count

        if not delete_due_to_age and not delete_due_to_count:
            continue

        artifact_dir = job.get("artifact_dir") or str(_default_job_artifact_dir(str(job["job_id"])))
        artifact_path = Path(artifact_dir)
        candidates.append(
            {
                "job_id": job["job_id"],
                "job_type": job["job_type"],
                "status": job["status"],
                "created_at": job["created_at"],
                "finished_at": job["finished_at"],
                "age_days": round(age_days, 3) if age_days is not None else None,
                "retention_days": retention_days,
                "max_count": max_count,
                "rank_for_type_status": rank,
                "delete_reason": (
                    "age+count"
                    if delete_due_to_age and delete_due_to_count
                    else ("age" if delete_due_to_age else "count")
                ),
                "artifact_dir": artifact_dir,
                "artifact_size_bytes": _path_size(artifact_path) if artifact_path.exists() else 0,
            }
        )

    return candidates


def _list_artifact_dirs() -> list[Path]:
    jobs_dir = get_settings().paths.jobs_dir

    if not jobs_dir.exists():
        return []

    return sorted(path for path in jobs_dir.iterdir() if path.is_dir())


def _default_job_artifact_dir(job_id: str) -> Path:
    return get_settings().paths.jobs_dir / job_id


def _find_orphan_artifact_dirs(*, now: datetime) -> list[dict[str, Any]]:
    jobs_dir = get_settings().paths.jobs_dir
    artifact_dirs = _list_artifact_dirs()
    cutoff = now - timedelta(days=ORPHAN_ARTIFACT_RETENTION_DAYS)

    connection = sqlite3.connect(get_db_path())
    try:
        rows = connection.execute(
            """
            SELECT job_id, artifact_dir
            FROM jobs
            """
        ).fetchall()
    finally:
        connection.close()

    known_names = {row[0] for row in rows}
    known_paths = {str(Path(row[1])) for row in rows if row[1]}

    orphans: list[dict[str, Any]] = []

    for artifact_dir in artifact_dirs:
        if artifact_dir.name in known_names or str(artifact_dir) in known_paths:
            continue
        if artifact_dir.parent != jobs_dir:
            continue
        modified_at = datetime.fromtimestamp(artifact_dir.stat().st_mtime, tz=timezone.utc)
        if modified_at >= cutoff:
            continue

        orphans.append(
            {
                "path": str(artifact_dir),
                "modified_at": modified_at.isoformat(),
                "retention_days": ORPHAN_ARTIFACT_RETENTION_DAYS,
                "artifact_size_bytes": _path_size(artifact_dir),
            }
        )

    return sorted(orphans, key=lambda item: item["path"])


def cleanup_jobs_retention(*, dry_run: bool = True) -> dict[str, Any]:
    """Clean old job rows and their artifact directories.

    By default this is a dry-run. Running/queued jobs are never deleted.
    Orphan artifact directories older than the grace window are deleted too.
    """

    now = _utc_now()
    candidates = _find_retention_candidates(now=now)
    orphan_candidates = _find_orphan_artifact_dirs(now=now)

    deleted_jobs: list[str] = []
    deleted_artifact_dirs: list[str] = []
    deleted_artifact_bytes = 0
    errors: list[dict[str, str]] = []

    if not dry_run and (candidates or orphan_candidates):
        connection = sqlite3.connect(get_db_path())
        try:
            connection.execute("BEGIN")

            for candidate in candidates:
                job_id = candidate["job_id"]
                connection.execute(
                    """
                    UPDATE rules_state
                    SET
                        last_apply_job_id = CASE
                            WHEN last_apply_job_id = ? THEN NULL
                            ELSE last_apply_job_id
                        END,
                        last_update_job_id = CASE
                            WHEN last_update_job_id = ? THEN NULL
                            ELSE last_update_job_id
                        END,
                        updated_at = CASE
                            WHEN last_apply_job_id = ? OR last_update_job_id = ?
                                THEN CURRENT_TIMESTAMP
                            ELSE updated_at
                        END
                    WHERE id = 1
                    """,
                    (job_id, job_id, job_id, job_id),
                )
                connection.execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))
                deleted_jobs.append(job_id)
            connection.commit()
        finally:
            connection.close()

        for candidate in candidates:
            artifact_dir = candidate.get("artifact_dir") or str(_default_job_artifact_dir(candidate["job_id"]))
            artifact_path = Path(artifact_dir)
            try:
                if artifact_path.exists() and artifact_path.is_dir():
                    artifact_size = _path_size(artifact_path)
                    shutil.rmtree(artifact_path)
                    deleted_artifact_bytes += artifact_size
                    deleted_artifact_dirs.append(str(artifact_path))
            except OSError as exc:
                errors.append(
                    {
                        "path": str(artifact_path),
                        "error": str(exc),
                    }
                )

        for orphan in orphan_candidates:
            artifact_path = Path(orphan["path"])
            try:
                if artifact_path.exists() and artifact_path.is_dir():
                    artifact_size = _path_size(artifact_path)
                    shutil.rmtree(artifact_path)
                    deleted_artifact_bytes += artifact_size
                    deleted_artifact_dirs.append(str(artifact_path))
            except OSError as exc:
                errors.append(
                    {
                        "path": str(artifact_path),
                        "error": str(exc),
                    }
                )

    return {
        "dry_run": dry_run,
        "policy": {
            "retention_days_by_status": DEFAULT_RETENTION_DAYS_BY_STATUS,
            "max_count_by_status": DEFAULT_MAX_COUNT_BY_STATUS,
            "max_count_by_job_type_status": {
                f"{job_type}:{status}": max_count
                for (job_type, status), max_count in MAX_COUNT_BY_JOB_TYPE_STATUS.items()
            },
            "protected_statuses": sorted(PROTECTED_STATUSES),
            "orphan_artifact_retention_days": ORPHAN_ARTIFACT_RETENTION_DAYS,
            "orphan_artifact_dirs_deleted": not dry_run,
        },
        "candidates_count": len(candidates),
        "candidates_artifact_size_bytes": sum(int(candidate.get("artifact_size_bytes") or 0) for candidate in candidates),
        "candidates": candidates,
        "deleted_jobs_count": len(deleted_jobs),
        "deleted_jobs": deleted_jobs,
        "deleted_artifact_dirs_count": len(deleted_artifact_dirs),
        "deleted_artifact_bytes": deleted_artifact_bytes,
        "deleted_artifact_dirs": deleted_artifact_dirs,
        "orphan_artifact_dirs_count": len(orphan_candidates),
        "orphan_artifact_dirs_size_bytes": sum(int(candidate.get("artifact_size_bytes") or 0) for candidate in orphan_candidates),
        "orphan_artifact_dirs": orphan_candidates,
        "errors": errors,
    }
