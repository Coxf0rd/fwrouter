from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import re
import stat
from typing import Any

from fwrouter_api.core.config import get_settings
from fwrouter_api.services.events import known_event_category_for_retention
from fwrouter_api.services.jsonl_io import (
    JsonlLeaseError,
    jsonl_lease,
    open_jsonl_read,
    remove_jsonl_temp,
    replace_jsonl,
)


_PROTECTED_SECURITY_EVENT_CODES = {
    "client_list.mutation_denied",
    "client_list.mutation_conflict",
}


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
    if not path.parent.exists():
        return {
            "path": str(path),
            "retention_days": retention_days,
            "exists": False,
            "total_lines": 0,
            "kept_lines": 0,
            "deleted_lines": 0,
            "invalid_lines": 0,
            "held_lines": 0,
            "rewritten": False,
        }

    cutoff = _utc_now() - timedelta(days=retention_days)
    with jsonl_lease(path) as lease:
        try:
            handle = open_jsonl_read(lease)
        except FileNotFoundError:
            return {
                "path": str(path), "retention_days": retention_days, "exists": False,
                "total_lines": 0, "kept_lines": 0, "deleted_lines": 0,
                "invalid_lines": 0, "held_lines": 0, "rewritten": False,
            }
        with handle:
            expected = os.fstat(handle.fileno())
            total_lines = kept_lines_count = deleted_lines = invalid_lines = held_lines = 0
            has_expired_line = False
            for line in handle:
                total_lines += 1
                if not line.strip():
                    continue
                payload = _decode_object(line)
                if payload is None:
                    invalid_lines += 1
                    kept_lines_count += 1
                    continue
                if _retention_hold(payload):
                    held_lines += 1
                    kept_lines_count += 1
                    continue
                timestamp = _parse_timestamp(str(payload.get(timestamp_field) or ""))
                if timestamp is None:
                    invalid_lines += 1
                    kept_lines_count += 1
                    continue
                if timestamp < cutoff:
                    deleted_lines += 1
                    has_expired_line = True
                else:
                    kept_lines_count += 1

            if not has_expired_line or dry_run:
                return {
                    "path": str(path), "retention_days": retention_days, "exists": True,
                    "total_lines": total_lines, "kept_lines": kept_lines_count,
                    "deleted_lines": deleted_lines, "invalid_lines": invalid_lines,
                    "held_lines": held_lines, "rewritten": False,
                }

            handle.seek(0)
            rewrite_counts = {"kept": 0, "deleted": 0, "invalid": 0, "held": 0}

            def retained_lines():
                for line in handle:
                    if not line.strip():
                        continue
                    payload = _decode_object(line)
                    if payload is None:
                        rewrite_counts["invalid"] += 1
                        rewrite_counts["kept"] += 1
                        yield line
                        continue
                    if _retention_hold(payload):
                        rewrite_counts["held"] += 1
                        rewrite_counts["kept"] += 1
                        yield line
                        continue
                    timestamp = _parse_timestamp(str(payload.get(timestamp_field) or ""))
                    if timestamp is None:
                        rewrite_counts["invalid"] += 1
                        rewrite_counts["kept"] += 1
                        yield line
                    elif timestamp < cutoff:
                        rewrite_counts["deleted"] += 1
                    else:
                        rewrite_counts["kept"] += 1
                        yield line

            replace_jsonl(lease, retained_lines(), expected=expected)
            return {
                "path": str(path), "retention_days": retention_days, "exists": True,
                "total_lines": total_lines, "kept_lines": rewrite_counts["kept"],
                "deleted_lines": rewrite_counts["deleted"],
                "invalid_lines": rewrite_counts["invalid"], "held_lines": rewrite_counts["held"],
                "rewritten": rewrite_counts["deleted"] > 0,
            }


def _decode_object(line: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(line)
    except (json.JSONDecodeError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def _retention_hold(payload: dict[str, Any]) -> bool:
    details = payload.get("details")
    if details is not None and not isinstance(details, dict):
        return True
    if not isinstance(details, dict):
        details = {}
    code = str(payload.get("event_code") or details.get("event_code") or "").strip()
    if code in _PROTECTED_SECURITY_EVENT_CODES:
        return True
    explicit_categories: set[str] = set()
    for source in (payload, details):
        if "event_category" not in source:
            continue
        explicit = str(source.get("event_category") or "").strip().lower()
        if explicit not in {"audit", "operational", "diagnostic"}:
            return True
        explicit_categories.add(explicit)
    if len(explicit_categories) > 1:
        return True
    code_category = known_event_category_for_retention("", event_code=code)
    if explicit_categories and code_category and next(iter(explicit_categories)) != code_category:
        return True
    if "event_code" in payload and "event_code" in details:
        if str(payload.get("event_code") or "").strip() != str(details.get("event_code") or "").strip():
            return True
    category = known_event_category_for_retention(
        str(payload.get("event_type") or ""),
        details={**details, **{
            key: payload[key] for key in ("event_code", "event_category") if key in payload
        }},
        event_code=code,
    )
    return category is None or category == "audit"


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
        target_name = _tmp_target_name(path.name, root=root)
        if target_name is None:
            continue
        target = root / target_name
        try:
            with jsonl_lease(target) as lease:
                info = os.stat(path.name, dir_fd=lease.directory_fd, follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
                    raise JsonlLeaseError("temporary owner, link count, or file type is unsafe")
                modified_at = datetime.fromtimestamp(info.st_mtime, tz=timezone.utc)
                if modified_at >= cutoff:
                    continue
                candidates.append(
                    {"path": str(path), "modified_at": modified_at.isoformat(), "size_bytes": info.st_size}
                )
                if dry_run:
                    continue
                remove_jsonl_temp(lease, path.name)
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


def _tmp_target_name(name: str, *, root: Path) -> str | None:
    if name.endswith(".tmp") and not name.startswith("."):
        target_name = name[:-4] + ".jsonl"
        try:
            info = (root / target_name).lstat()
        except OSError:
            return None
        if (stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
                and info.st_nlink == 1):
            return target_name
        return None
    if name.startswith(".") and name.endswith(".tmp"):
        match = re.fullmatch(
            r"\.(.+\.jsonl)\.rewrite-[0-9]+-[0-9a-f]{16}\.tmp", name
        )
        if match:
            return match.group(1)
        old_scrub = re.fullmatch(r"\.(.+\.jsonl)\.[A-Za-z0-9_]{8}\.scrub\.tmp", name)
        if old_scrub:
            return old_scrub.group(1)
    return None


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
