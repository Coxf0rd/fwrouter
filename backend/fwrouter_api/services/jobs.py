from __future__ import annotations

from datetime import datetime, timedelta
import json
import sqlite3
from typing import Any
from uuid import uuid4

from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import get_db_path
from fwrouter_api.db.connection import db_session
from fwrouter_api.jobs.base import JobStatus, utc_now


def _json_dumps(value: dict[str, Any] | None) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _json_loads(value: str | None) -> dict[str, Any] | None:
    if not value:
        return None
    loaded = json.loads(value)
    if not isinstance(loaded, dict):
        return {"value": loaded}
    return loaded


def _trim_scalar(value: Any) -> Any:
    if isinstance(value, str):
        return value if len(value) <= 256 else f"{value[:253]}..."
    return value


def _summarize_result_value(value: Any, *, depth: int = 0) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return _trim_scalar(value)

    if isinstance(value, list):
        if depth >= 1:
            return {"type": "list", "length": len(value)}
        return {
            "type": "list",
            "length": len(value),
            "sample": [_summarize_result_value(item, depth=depth + 1) for item in value[:3]],
        }

    if isinstance(value, dict):
        preferred_keys = (
            "job_status",
            "status",
            "stage",
            "message",
            "error_code",
            "error_message",
            "job_id",
            "changed",
            "ok",
            "apply",
            "rules_state",
            "reconcile_reason",
            "reconcile_action",
            "candidate",
            "config_validation",
            "promoted",
            "container",
            "mihomo",
        )
        summary: dict[str, Any] = {}
        added: set[str] = set()
        for key in preferred_keys:
            if key in value:
                summary[key] = _summarize_result_value(value[key], depth=depth + 1)
                added.add(key)
        if depth == 0:
            extra_keys = [key for key in value.keys() if key not in added][:8]
            if extra_keys:
                summary["_extra_keys"] = extra_keys
        summary["_keys_count"] = len(value)
        return summary

    return {"type": type(value).__name__}


def _encode_job_result(result: dict[str, Any] | None) -> str | None:
    if result is None:
        return None

    raw = _json_dumps(result)
    if raw is None:
        return None

    max_bytes = int(get_settings().job_result_max_bytes)
    if len(raw.encode("utf-8")) <= max_bytes:
        return raw

    summary_payload = {
        "__truncated__": True,
        "original_bytes": len(raw.encode("utf-8")),
        "max_bytes": max_bytes,
        "summary": _summarize_result_value(result),
    }
    summarized = _json_dumps(summary_payload)
    if summarized is None:
        return None
    if len(summarized.encode("utf-8")) <= max_bytes:
        return summarized

    fallback = {
        "__truncated__": True,
        "original_bytes": len(raw.encode("utf-8")),
        "max_bytes": max_bytes,
        "summary": {
            "job_status": result.get("job_status"),
            "status": result.get("status"),
            "stage": result.get("stage"),
            "error_code": result.get("error_code"),
            "error_message": _trim_scalar(result.get("error_message")),
            "_keys_count": len(result),
        },
    }
    return _json_dumps(fallback)


def _row_to_job(row: Any) -> dict[str, Any]:
    return {
        "job_id": row["job_id"],
        "job_type": row["job_type"],
        "status": row["status"],
        "lock_key": row["lock_key"],
        "requested_by": row["requested_by"],
        "input": _json_loads(row["input_json"]),
        "result": _json_loads(row["result_json"]),
        "error_code": row["error_code"],
        "error_message": row["error_message"],
        "artifact_dir": row["artifact_dir"],
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "updated_at": row["updated_at"],
        "finished_at": row["finished_at"],
    }


def _parse_job_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None

ACTIVE_JOB_STATUSES = (JobStatus.QUEUED.value, JobStatus.RUNNING.value)


class JobLockConflictError(RuntimeError):
    """Raised when a job lock_key is already held by an active job."""

    def __init__(self, lock_key: str, active_job: dict[str, Any]) -> None:
        super().__init__(f"Job lock is already active: {lock_key}")
        self.lock_key = lock_key
        self.active_job = active_job


def get_active_lock_lease(
    lock_key: str,
    *,
    exclude_job_id: str | None = None,
) -> dict[str, Any] | None:
    """Return current active lock lease metadata for a conflicting job, if any."""

    active_job = find_active_lock_conflict(lock_key, exclude_job_id=exclude_job_id)
    if active_job is None:
        return None

    return {
        "lock_key": lock_key,
        "owner_job_id": active_job["job_id"],
        "owner_status": active_job["status"],
        "acquired_at": active_job.get("created_at"),
        "heartbeat_at": active_job.get("updated_at") or active_job.get("started_at"),
        "requested_by": active_job.get("requested_by"),
    }


def _lock_tokens(lock_key: str | None) -> set[str]:
    if not lock_key:
        return set()

    normalized = lock_key.replace(",", "+").replace(" ", "+")
    return {token for token in normalized.split("+") if token}


def find_active_lock_conflict(
    lock_key: str,
    *,
    exclude_job_id: str | None = None,
) -> dict[str, Any] | None:
    """Return active queued/running job holding a conflicting lock, if any.

    Lock keys may represent one or more serialized scopes using `+`, for
    example `apply+rules`. Two jobs conflict when their lock token sets overlap.
    """

    wanted_tokens = _lock_tokens(lock_key)
    if not wanted_tokens:
        return None

    cleanup_stale_running_jobs()

    query = """
        SELECT *
        FROM jobs
        WHERE status IN (?, ?)
    """
    params: list[Any] = [
        JobStatus.QUEUED.value,
        JobStatus.RUNNING.value,
    ]

    if exclude_job_id is not None:
        query += " AND job_id <> ?"
        params.append(exclude_job_id)

    query += " ORDER BY created_at ASC"

    with db_session() as connection:
        rows = connection.execute(query, tuple(params)).fetchall()

    for row in rows:
        active_job = _row_to_job(row)
        active_tokens = _lock_tokens(active_job.get("lock_key"))
        if wanted_tokens & active_tokens:
            return active_job

    return None


def create_job(
    job_type: str,
    *,
    lock_key: str | None = None,
    requested_by: str | None = None,
    input_data: dict[str, Any] | None = None,
    artifact_dir: str | None = None,
) -> dict[str, Any]:
    """Create a queued job in SQLite and return its DTO.

    If lock_key is set, another queued/running job with the same lock_key blocks
    creation and raises JobLockConflictError.
    """

    cleanup_stale_running_jobs()

    if lock_key:
        active_lease = get_active_lock_lease(lock_key)
        if active_lease is not None:
            active_job = get_job_without_cleanup(str(active_lease["owner_job_id"])) or active_lease
            raise JobLockConflictError(lock_key, active_job)

    job_id = str(uuid4())

    try:
        with db_session() as connection:
            connection.execute(
                """
                INSERT INTO jobs (
                    job_id,
                    job_type,
                    status,
                    lock_key,
                    requested_by,
                    input_json,
                    artifact_dir
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    job_type,
                    JobStatus.QUEUED.value,
                    lock_key,
                    requested_by,
                    _json_dumps(input_data),
                    artifact_dir,
                ),
            )

            row = connection.execute(
                "SELECT * FROM jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
    except sqlite3.IntegrityError:
        if lock_key:
            active_job = find_active_lock_conflict(lock_key)
            if active_job is not None:
                raise JobLockConflictError(lock_key, active_job) from None
        raise

    return _row_to_job(row)


def get_job(job_id: str) -> dict[str, Any] | None:
    """Return one job by id."""

    cleanup_stale_running_jobs()

    with db_session() as connection:
        row = connection.execute(
            "SELECT * FROM jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()

    return _row_to_job(row) if row else None


def get_job_without_cleanup(job_id: str) -> dict[str, Any] | None:
    """Return one job by id without triggering stale cleanup."""

    with db_session() as connection:
        row = connection.execute(
            "SELECT * FROM jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()

    return _row_to_job(row) if row else None


def list_jobs(
    *,
    limit: int = 50,
    job_type: str | None = None,
    status: str | None = None,
) -> list[dict[str, Any]]:
    """Return recent jobs, newest first."""

    cleanup_stale_running_jobs()

    safe_limit = max(1, min(limit, 200))

    where: list[str] = []
    params: list[Any] = []

    if job_type:
        where.append("job_type = ?")
        params.append(job_type)

    if status:
        where.append("status = ?")
        params.append(status)

    where_sql = f"WHERE {' AND '.join(where)}" if where else ""

    with db_session() as connection:
        rows = connection.execute(
            f"""
            SELECT *
            FROM jobs
            {where_sql}
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (*params, safe_limit),
        ).fetchall()

    return [_row_to_job(row) for row in rows]


def cleanup_stale_running_jobs(
    *,
    stale_after_seconds: int | None = None,
) -> list[dict[str, Any]]:
    """Mark overdue active jobs as failed and return affected jobs.

    Queued jobs can become orphaned after a backend restart or interrupted
    process lifetime. Those rows still hold their lock_key and would block new
    mutations forever if we cleaned up only `running` rows.
    """

    settings = get_settings()
    timeout_seconds = (
        int(settings.job_stale_timeout_seconds)
        if stale_after_seconds is None
        else int(stale_after_seconds)
    )
    if timeout_seconds < 0:
        return []

    cutoff = (utc_now() - timedelta(seconds=timeout_seconds)).strftime("%Y-%m-%d %H:%M:%S")
    stale_rows: list[Any] = []

    with db_session() as connection:
        stale_rows = connection.execute(
            """
            SELECT *
            FROM jobs
            WHERE status IN (?, ?)
              AND updated_at <= ?
            ORDER BY updated_at ASC
            """,
            (JobStatus.QUEUED.value, JobStatus.RUNNING.value, cutoff),
        ).fetchall()

        if stale_rows:
            finished_at = utc_now().strftime("%Y-%m-%d %H:%M:%S")
            connection.execute(
                """
                UPDATE jobs
                SET
                    status = ?,
                    error_code = CASE
                        WHEN status = ? THEN ?
                        ELSE ?
                    END,
                    error_message = CASE
                        WHEN status = ? THEN ?
                        ELSE ?
                    END,
                    finished_at = ?,
                    updated_at = ?
                WHERE status IN (?, ?)
                  AND updated_at <= ?
                """,
                (
                    JobStatus.FAILED.value,
                    JobStatus.QUEUED.value,
                    "JOB_STALE_QUEUE_TIMEOUT",
                    "JOB_STALE_TIMEOUT",
                    JobStatus.QUEUED.value,
                    "Job marked failed because it remained queued past the stale timeout window.",
                    "Job marked failed because it exceeded timeout / stale running lease.",
                    finished_at,
                    finished_at,
                    JobStatus.QUEUED.value,
                    JobStatus.RUNNING.value,
                    cutoff,
                ),
            )

    stale_jobs: list[dict[str, Any]] = []
    for row in stale_rows:
        stale_job = _row_to_job(row)
        stale_job["status"] = JobStatus.FAILED.value
        if stale_job.get("status") == JobStatus.QUEUED.value or row["status"] == JobStatus.QUEUED.value:
            stale_job["error_code"] = "JOB_STALE_QUEUE_TIMEOUT"
            stale_job["error_message"] = (
                "Job marked failed because it remained queued past the stale timeout window."
            )
        else:
            stale_job["error_code"] = "JOB_STALE_TIMEOUT"
            stale_job["error_message"] = (
                "Job marked failed because it exceeded timeout / stale running lease."
            )
        if stale_job.get("finished_at") is None:
            stale_job["finished_at"] = utc_now().strftime("%Y-%m-%d %H:%M:%S")
        stale_jobs.append(stale_job)
    return stale_jobs


def mark_job_running(job_id: str) -> dict[str, Any] | None:
    """Mark a queued job as running.

    Returns None if the job does not exist.
    """

    now = utc_now().strftime("%Y-%m-%d %H:%M:%S")

    with db_session() as connection:
        row = connection.execute(
            "SELECT * FROM jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()

        if row is None:
            return None

        if row["status"] != JobStatus.QUEUED.value:
            return _row_to_job(row)

        connection.execute(
            """
            UPDATE jobs
            SET status = ?, started_at = ?, updated_at = ?
            WHERE job_id = ?
            """,
            (JobStatus.RUNNING.value, now, now, job_id),
        )

        updated = connection.execute(
            "SELECT * FROM jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()

    return _row_to_job(updated)


def touch_job_running(job_id: str) -> dict[str, Any] | None:
    """Refresh running job lease timestamp."""

    now = utc_now().strftime("%Y-%m-%d %H:%M:%S")

    with db_session() as connection:
        connection.execute(
            """
            UPDATE jobs
            SET updated_at = ?
            WHERE job_id = ?
              AND status = ?
            """,
            (now, job_id, JobStatus.RUNNING.value),
        )
        row = connection.execute(
            "SELECT * FROM jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()

    return _row_to_job(row) if row else None


def update_job_running_result(
    job_id: str,
    *,
    result: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Persist intermediate result payload for a running job."""

    now = utc_now().strftime("%Y-%m-%d %H:%M:%S")

    with db_session() as connection:
        connection.execute(
            """
            UPDATE jobs
            SET
                result_json = ?,
                updated_at = ?
            WHERE job_id = ?
              AND status = ?
            """,
            (
                _encode_job_result(result),
                now,
                job_id,
                JobStatus.RUNNING.value,
            ),
        )
        row = connection.execute(
            "SELECT * FROM jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()

    return _row_to_job(row) if row else None


def mark_job_success(
    job_id: str,
    *,
    result: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Mark a job as successful."""

    now = utc_now().strftime("%Y-%m-%d %H:%M:%S")

    with db_session() as connection:
        connection.execute(
            """
            UPDATE jobs
            SET
                status = ?,
                result_json = ?,
                error_code = NULL,
                error_message = NULL,
                finished_at = ?,
                updated_at = ?
            WHERE job_id = ?
              AND status = ?
            """,
            (
                JobStatus.SUCCESS.value,
                _encode_job_result(result),
                now,
                now,
                job_id,
                JobStatus.RUNNING.value,
            ),
        )

        row = connection.execute(
            "SELECT * FROM jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()

    return _row_to_job(row) if row else None


def mark_job_failed(
    job_id: str,
    *,
    error_code: str,
    error_message: str,
    result: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Mark a job as failed."""

    now = utc_now().strftime("%Y-%m-%d %H:%M:%S")

    with db_session() as connection:
        connection.execute(
            """
            UPDATE jobs
            SET
                status = ?,
                result_json = ?,
                error_code = ?,
                error_message = ?,
                finished_at = ?,
                updated_at = ?
            WHERE job_id = ?
              AND status = ?
            """,
            (
                JobStatus.FAILED.value,
                _encode_job_result(result),
                error_code,
                error_message,
                now,
                now,
                job_id,
                JobStatus.RUNNING.value,
            ),
        )

        row = connection.execute(
            "SELECT * FROM jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()

    return _row_to_job(row) if row else None


def compact_oversized_job_results(
    *,
    max_bytes: int | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    settings = get_settings()
    limit_bytes = int(max_bytes if max_bytes is not None else settings.job_result_max_bytes)
    connection = sqlite3.connect(get_db_path())
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT job_id, status, result_json
            FROM jobs
            WHERE result_json IS NOT NULL
              AND length(CAST(result_json AS BLOB)) > ?
              AND status IN (?, ?, ?)
            ORDER BY length(CAST(result_json AS BLOB)) DESC
            """,
            (
                limit_bytes,
                JobStatus.SUCCESS.value,
                JobStatus.FAILED.value,
                JobStatus.CANCELLED.value,
            ),
        ).fetchall()

        candidates: list[dict[str, Any]] = []
        for row in rows:
            original = str(row["result_json"] or "")
            try:
                parsed = json.loads(original)
            except json.JSONDecodeError:
                parsed = {
                    "__truncated__": True,
                    "original_bytes": len(original.encode("utf-8")),
                    "max_bytes": limit_bytes,
                    "summary": {"error": "invalid_json"},
                }
            if not isinstance(parsed, dict):
                parsed = {"value": parsed}
            encoded = _encode_job_result(parsed)
            if encoded is None:
                continue
            original_bytes = len(original.encode("utf-8"))
            compacted_bytes = len(encoded.encode("utf-8"))
            if compacted_bytes >= original_bytes:
                continue
            candidates.append(
                {
                    "job_id": str(row["job_id"]),
                    "status": str(row["status"]),
                    "original_bytes": original_bytes,
                    "compacted_bytes": compacted_bytes,
                    "bytes_saved": original_bytes - compacted_bytes,
                    "encoded": encoded,
                }
            )

        updated_jobs: list[str] = []
        bytes_saved_total = 0
        if not dry_run and candidates:
            connection.execute("BEGIN")
            for candidate in candidates:
                connection.execute(
                    "UPDATE jobs SET result_json = ? WHERE job_id = ?",
                    (candidate["encoded"], candidate["job_id"]),
                )
                updated_jobs.append(candidate["job_id"])
                bytes_saved_total += int(candidate["bytes_saved"])
            connection.commit()

        return {
            "dry_run": dry_run,
            "max_bytes": limit_bytes,
            "candidates_count": len(candidates),
            "candidates": [
                {
                    key: value
                    for key, value in candidate.items()
                    if key != "encoded"
                }
                for candidate in candidates
            ],
            "updated_jobs_count": len(updated_jobs),
            "updated_jobs": updated_jobs,
            "bytes_saved": bytes_saved_total if not dry_run else sum(int(item["bytes_saved"]) for item in candidates),
        }
    finally:
        connection.close()
