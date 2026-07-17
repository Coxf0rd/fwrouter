from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fwrouter_api.core.config import get_settings
from fwrouter_api.services.artifacts import atomic_write_text


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None

    normalized = value.replace("Z", "+00:00")

    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)

    return parsed.astimezone(timezone.utc)


def _cleanup_jsonl_file(
    path: Path,
    *,
    timestamp_field: str,
    retention_days: int,
    dry_run: bool,
) -> dict[str, Any]:
    if not path.exists() or not path.is_file():
        return {
            "path": str(path),
            "retention_days": retention_days,
            "exists": False,
            "total_lines": 0,
            "kept_lines": 0,
            "deleted_lines": 0,
            "invalid_lines": 0,
            "rewritten": False,
        }

    cutoff = _utc_now() - timedelta(days=retention_days)
    total_lines = 0
    kept_lines_count = 0
    deleted_lines = 0
    invalid_lines = 0
    
    tmp_path = path.with_suffix(".tmp")
    
    try:
        with path.open("r", encoding="utf-8") as handle:
            out_handle = tmp_path.open("w", encoding="utf-8") if not dry_run else None
            
            for line in handle:
                total_lines += 1
                if not line.strip():
                    continue

                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    invalid_lines += 1
                    if out_handle:
                        out_handle.write(line)
                    kept_lines_count += 1
                    continue

                timestamp = _parse_timestamp(str(payload.get(timestamp_field) or ""))
                if timestamp is None:
                    invalid_lines += 1
                    if out_handle:
                        out_handle.write(line)
                    kept_lines_count += 1
                    continue

                if timestamp < cutoff:
                    deleted_lines += 1
                    continue

                if out_handle:
                    out_handle.write(line)
                kept_lines_count += 1
                
        if out_handle:
            out_handle.close()
            if deleted_lines > 0:
                tmp_path.replace(path)
            else:
                tmp_path.unlink(missing_ok=True)
                
    except Exception as e:
        if not dry_run and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise e

    rewritten = deleted_lines > 0

    return {
        "path": str(path),
        "retention_days": retention_days,
        "exists": True,
        "total_lines": total_lines,
        "kept_lines": kept_lines_count,
        "deleted_lines": deleted_lines,
        "invalid_lines": invalid_lines,
        "rewritten": rewritten and not dry_run,
    }


def _cleanup_orphan_tmp_files(
    root: Path,
    *,
    retention_minutes: int,
    dry_run: bool,
) -> dict[str, Any]:
    if not root.exists() or not root.is_dir():
        return {
            "root": str(root),
            "retention_minutes": retention_minutes,
            "exists": False,
            "candidates_count": 0,
            "deleted_count": 0,
            "candidates": [],
            "deleted": [],
            "errors": [],
        }

    cutoff = _utc_now() - timedelta(minutes=retention_minutes)
    candidates: list[dict[str, Any]] = []
    deleted: list[str] = []
    errors: list[dict[str, str]] = []

    for path in sorted(root.glob("*.tmp")):
        try:
            modified_at = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        except OSError as exc:
            errors.append({"path": str(path), "error": str(exc)})
            continue
        if modified_at >= cutoff:
            continue
        candidates.append(
            {
                "path": str(path),
                "modified_at": modified_at.isoformat(),
                "size_bytes": path.stat().st_size,
            }
        )
        if dry_run:
            continue
        try:
            path.unlink()
            deleted.append(str(path))
        except OSError as exc:
            errors.append({"path": str(path), "error": str(exc)})

    return {
        "root": str(root),
        "retention_minutes": retention_minutes,
        "exists": True,
        "candidates_count": len(candidates),
        "deleted_count": len(deleted),
        "candidates": candidates,
        "deleted": deleted,
        "errors": errors,
    }


def cleanup_log_retention(
    *,
    operational_retention_days: int,
    technical_retention_days: int,
    dry_run: bool = True,
) -> dict[str, Any]:
    paths = get_settings().paths
    operational = _cleanup_jsonl_file(
        paths.operational_events_path,
        timestamp_field="created_at",
        retention_days=operational_retention_days,
        dry_run=dry_run,
    )

    technical_files = sorted(paths.technical_log_dir.glob("*.jsonl"))
    technical_results = [
        _cleanup_jsonl_file(
            path,
            timestamp_field="timestamp",
            retention_days=technical_retention_days,
            dry_run=dry_run,
        )
        for path in technical_files
    ]
    tmp_retention_minutes = 15
    orphan_tmp = {
        "operational": _cleanup_orphan_tmp_files(
            paths.operational_log_dir,
            retention_minutes=tmp_retention_minutes,
            dry_run=dry_run,
        ),
        "technical": _cleanup_orphan_tmp_files(
            paths.technical_log_dir,
            retention_minutes=tmp_retention_minutes,
            dry_run=dry_run,
        ),
    }

    return {
        "dry_run": dry_run,
        "operational": operational,
        "technical": {
            "retention_days": technical_retention_days,
            "files_count": len(technical_results),
            "files": technical_results,
            "deleted_lines_count": sum(item["deleted_lines"] for item in technical_results),
        },
        "orphan_tmp": orphan_tmp,
    }
