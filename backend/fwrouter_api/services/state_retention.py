from __future__ import annotations

import shutil
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fwrouter_api.core.config import get_settings


DATAPLANE_SNAPSHOT_RETENTION_DAYS = 1
DATAPLANE_SNAPSHOT_MAX_COUNT = 20
CONTROL_PLANE_SNAPSHOT_RETENTION_DAYS = 14
CONTROL_PLANE_SNAPSHOT_MAX_COUNT = 16
DATABASE_BACKUP_RETENTION_DAYS = 14
DATABASE_BACKUP_MAX_COUNT = 8
DEBUG_ARTIFACT_RETENTION_DAYS = 14
DEBUG_ARTIFACT_MAX_COUNT = 8


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _path_mtime(path: Path) -> datetime:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)


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


def _read_manifest_plan_id(path: Path) -> str | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    plan_id = payload.get("plan_id")
    return str(plan_id) if plan_id else None


def _protected_dataplane_snapshot_names() -> set[str]:
    settings = get_settings()
    generated_dataplane = settings.paths.generated_dir / "dataplane"
    last_good_dataplane = settings.paths.state_dir / "last-good" / "dataplane"
    manifest_paths = [
        generated_dataplane / "current-manifest.json",
        generated_dataplane / "applied-manifest.json",
        last_good_dataplane / "last-good-manifest.json",
    ]
    return {
        plan_id
        for plan_id in (_read_manifest_plan_id(path) for path in manifest_paths)
        if plan_id
    }


def _cleanup_path_set(
    root: Path,
    *,
    paths: list[Path],
    retention_days: int,
    max_count: int,
    dry_run: bool,
    protected_names: set[str] | None = None,
    delete_policy: str = "age_or_count",
) -> dict[str, Any]:
    cutoff = _utc_now() - timedelta(days=retention_days)
    sorted_paths = sorted(paths, key=_path_mtime, reverse=True)
    keep_names = {path.name for path in sorted_paths[:max_count]}
    protected = protected_names or set()
    deleted: list[str] = []
    deleted_bytes = 0
    candidates: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    for path in sorted_paths:
        if path.name in protected:
            continue
        modified_at = _path_mtime(path)
        delete_due_to_age = modified_at < cutoff
        delete_due_to_count = path.name not in keep_names
        should_delete = (
            delete_due_to_age and delete_due_to_count
            if delete_policy == "age_and_count"
            else delete_due_to_age or delete_due_to_count
        )
        if not should_delete:
            continue
        reason = "age+count" if delete_due_to_age and delete_due_to_count else "age" if delete_due_to_age else "count"
        size_bytes = _path_size(path)
        candidates.append(
            {
                "path": str(path),
                "modified_at": modified_at.isoformat(),
                "reason": reason,
                "size_bytes": size_bytes,
            }
        )
        if dry_run:
            continue
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
            deleted.append(str(path))
            deleted_bytes += size_bytes
        except OSError as exc:
            errors.append({"path": str(path), "error": str(exc)})

    return {
        "root": str(root),
        "retention_days": retention_days,
        "max_count": max_count,
        "delete_policy": delete_policy,
        "protected_names": sorted(protected),
        "existing_count": len(sorted_paths),
        "candidates_count": len(candidates),
        "candidates_size_bytes": sum(int(candidate.get("size_bytes") or 0) for candidate in candidates),
        "candidates": candidates,
        "deleted_count": len(deleted),
        "deleted_bytes": deleted_bytes,
        "deleted": deleted,
        "errors": errors,
    }


def cleanup_state_retention(*, dry_run: bool = True) -> dict[str, Any]:
    settings = get_settings()
    snapshots_root = settings.paths.state_dir / "last-good" / "dataplane" / "snapshots"
    snapshot_dirs = [path for path in snapshots_root.iterdir() if path.is_dir()] if snapshots_root.exists() else []
    transfer_root = settings.paths.state_dir / "transfer"
    transfer_files = (
        list(transfer_root.glob("control-plane-snapshot.*.json"))
        if transfer_root.exists()
        else []
    )
    backups_root = settings.paths.state_dir / "backups"
    backup_files = list(backups_root.glob("*.bak")) if backups_root.exists() else []
    debug_root = settings.paths.state_dir / "debug"
    debug_entries = list(debug_root.iterdir()) if debug_root.exists() else []

    return {
        "dry_run": dry_run,
        "dataplane_snapshots": _cleanup_path_set(
            snapshots_root,
            paths=snapshot_dirs,
            retention_days=DATAPLANE_SNAPSHOT_RETENTION_DAYS,
            max_count=DATAPLANE_SNAPSHOT_MAX_COUNT,
            dry_run=dry_run,
            protected_names=_protected_dataplane_snapshot_names(),
            delete_policy="age_and_count",
        ),
        "control_plane_snapshots": _cleanup_path_set(
            transfer_root,
            paths=transfer_files,
            retention_days=CONTROL_PLANE_SNAPSHOT_RETENTION_DAYS,
            max_count=CONTROL_PLANE_SNAPSHOT_MAX_COUNT,
            dry_run=dry_run,
        ),
        "database_backups": _cleanup_path_set(
            backups_root,
            paths=backup_files,
            retention_days=DATABASE_BACKUP_RETENTION_DAYS,
            max_count=DATABASE_BACKUP_MAX_COUNT,
            dry_run=dry_run,
        ),
        "debug_artifacts": _cleanup_path_set(
            debug_root,
            paths=debug_entries,
            retention_days=DEBUG_ARTIFACT_RETENTION_DAYS,
            max_count=DEBUG_ARTIFACT_MAX_COUNT,
            dry_run=dry_run,
        ),
    }
