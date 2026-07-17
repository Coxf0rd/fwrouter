from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import get_db_path


APPLY_VERSION_RETENTION_DAYS = 1
APPLY_VERSION_MAX_COUNT = 20
ORPHAN_MANIFEST_RETENTION_DAYS = 1
_RESERVED_MANIFEST_NAMES = {
    "candidate-manifest.json",
    "current-manifest.json",
    "applied-manifest.json",
    "last-good-manifest.json",
    "last-result.json",
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


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


def _generated_dataplane_dir() -> Path:
    return get_settings().paths.generated_dir / "dataplane"


def _resolve_manifest_path(value: str | None) -> Path | None:
    if not value:
        return None
    path = Path(value)
    if path.is_absolute():
        return path
    return _generated_dataplane_dir() / path


def _is_versioned_manifest_path(path: Path) -> bool:
    if path.suffix != ".json" or path.name in _RESERVED_MANIFEST_NAMES:
        return False
    try:
        UUID(path.stem)
    except ValueError:
        return False
    return True


def _file_size(path: Path | None) -> int:
    if path is None:
        return 0
    try:
        return path.stat().st_size if path.exists() and path.is_file() else 0
    except OSError:
        return 0


def _load_apply_versions() -> list[dict[str, Any]]:
    connection = sqlite3.connect(get_db_path())
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT apply_id, job_id, manifest_path, created_at, promoted_at, status
            FROM apply_versions
            ORDER BY created_at DESC, apply_id DESC
            """
        ).fetchall()
    finally:
        connection.close()
    return [dict(row) for row in rows]


def cleanup_apply_versions_retention(
    *,
    retention_days: int = APPLY_VERSION_RETENTION_DAYS,
    max_count: int = APPLY_VERSION_MAX_COUNT,
    orphan_retention_days: int = ORPHAN_MANIFEST_RETENTION_DAYS,
    dry_run: bool = True,
) -> dict[str, Any]:
    now = _utc_now()
    cutoff = now - timedelta(days=retention_days)
    rows = _load_apply_versions()

    candidate_rows: list[dict[str, Any]] = []
    kept_rows: list[dict[str, Any]] = []

    for index, row in enumerate(rows):
        created_at = _parse_sqlite_timestamp(str(row.get("created_at") or ""))
        delete_due_to_age = created_at is not None and created_at < cutoff
        delete_due_to_count = index >= max_count
        should_delete = delete_due_to_age and delete_due_to_count
        manifest_path = _resolve_manifest_path(str(row.get("manifest_path") or ""))
        row_payload = {
            "apply_id": str(row["apply_id"]),
            "job_id": row.get("job_id"),
            "status": str(row.get("status") or ""),
            "created_at": row.get("created_at"),
            "manifest_path": str(manifest_path) if manifest_path is not None else None,
            "manifest_size_bytes": _file_size(manifest_path),
            "delete_reason": (
                "age+count"
                if delete_due_to_age and delete_due_to_count
                else ("age" if delete_due_to_age else ("count" if delete_due_to_count else None))
            ),
        }
        if should_delete:
            row_payload["delete_reason"] = "age+count"
            candidate_rows.append(row_payload)
        else:
            kept_rows.append(row_payload)

    deleted_apply_ids: list[str] = []
    deleted_manifest_files: list[str] = []
    deleted_manifest_bytes = 0
    errors: list[dict[str, str]] = []

    if not dry_run and candidate_rows:
        connection = sqlite3.connect(get_db_path())
        try:
            connection.execute("BEGIN")
            for candidate in candidate_rows:
                connection.execute(
                    "DELETE FROM apply_versions WHERE apply_id = ?",
                    (candidate["apply_id"],),
                )
                deleted_apply_ids.append(candidate["apply_id"])
            connection.commit()
        finally:
            connection.close()

    for candidate in candidate_rows:
        manifest_path_value = candidate.get("manifest_path")
        if not manifest_path_value:
            continue
        manifest_path = Path(manifest_path_value)
        if not _is_versioned_manifest_path(manifest_path):
            continue
        if dry_run:
            continue
        try:
            if manifest_path.exists():
                deleted_manifest_bytes += manifest_path.stat().st_size
                manifest_path.unlink()
                deleted_manifest_files.append(str(manifest_path))
        except OSError as exc:
            errors.append({"path": str(manifest_path), "error": str(exc)})

    known_manifest_paths = {
        str(Path(path))
        for path in (row.get("manifest_path") for row in [*kept_rows, *candidate_rows])
        if path
    }
    orphan_cutoff = now - timedelta(days=orphan_retention_days)
    dataplane_dir = _generated_dataplane_dir()
    orphan_candidates: list[dict[str, Any]] = []
    orphan_deleted: list[str] = []

    if dataplane_dir.exists():
        for path in sorted(dataplane_dir.glob("*.json")):
            if not _is_versioned_manifest_path(path):
                continue
            if str(path) in known_manifest_paths:
                continue
            modified_at = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            if modified_at >= orphan_cutoff:
                continue
            orphan_candidates.append(
                {
                    "path": str(path),
                    "modified_at": modified_at.isoformat(),
                    "size_bytes": path.stat().st_size,
                }
            )
            if dry_run:
                continue
            try:
                orphan_size = path.stat().st_size if path.exists() else 0
                path.unlink()
                deleted_manifest_bytes += orphan_size
                orphan_deleted.append(str(path))
            except OSError as exc:
                errors.append({"path": str(path), "error": str(exc)})

    return {
        "dry_run": dry_run,
        "retention_days": retention_days,
        "max_count": max_count,
        "existing_apply_versions_count": len(rows),
        "candidates_count": len(candidate_rows),
        "candidates_size_bytes": sum(int(candidate.get("manifest_size_bytes") or 0) for candidate in candidate_rows),
        "candidates": candidate_rows,
        "deleted_apply_versions_count": len(deleted_apply_ids),
        "deleted_apply_versions": deleted_apply_ids,
        "deleted_manifest_files_count": len(deleted_manifest_files),
        "deleted_manifest_bytes": deleted_manifest_bytes,
        "deleted_manifest_files": deleted_manifest_files,
        "orphan_manifest_retention_days": orphan_retention_days,
        "orphan_manifest_candidates_count": len(orphan_candidates),
        "orphan_manifest_candidates_size_bytes": sum(int(candidate.get("size_bytes") or 0) for candidate in orphan_candidates),
        "orphan_manifest_candidates": orphan_candidates,
        "orphan_manifest_deleted_count": len(orphan_deleted),
        "orphan_manifest_deleted": orphan_deleted,
        "errors": errors,
    }
