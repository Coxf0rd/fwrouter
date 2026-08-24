from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fwrouter_api.db.connection import initialize_database
from fwrouter_api.services.control_plane_transfer_common import _state_from_snapshot, _transfer_dir


def _resolve_transfer_snapshot_path(file_path: str) -> Path:
    candidate = Path(file_path)
    if not candidate.is_absolute():
        candidate = _transfer_dir() / candidate
    resolved = candidate.resolve(strict=False)
    transfer_root = _transfer_dir().resolve(strict=False)
    if not resolved.is_relative_to(transfer_root):
        raise ValueError("Snapshot file path must stay inside the transfer directory.")
    return resolved


def _load_snapshot_file(path: Path) -> dict[str, Any]:
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError("Snapshot file must contain a JSON object.")
    return loaded


def resolve_control_plane_snapshot_source(
    *,
    snapshot: dict[str, Any] | None = None,
    file_path: str | None = None,
) -> dict[str, Any]:
    if isinstance(snapshot, dict) and snapshot:
        return {
            "ok": True,
            "snapshot": snapshot,
            "source": {
                "kind": "payload",
                "file_path": None,
            },
        }

    if not file_path:
        return {
            "ok": False,
            "error": {
                "code": "CONTROL_PLANE_SNAPSHOT_SOURCE_REQUIRED",
                "message": "Provide either snapshot payload or file_path.",
            },
        }

    try:
        resolved = _resolve_transfer_snapshot_path(file_path)
    except ValueError as exc:
        return {
            "ok": False,
            "error": {
                "code": "CONTROL_PLANE_SNAPSHOT_FILE_PATH_INVALID",
                "message": str(exc),
            },
        }

    if not resolved.exists() or not resolved.is_file():
        return {
            "ok": False,
            "error": {
                "code": "CONTROL_PLANE_SNAPSHOT_FILE_NOT_FOUND",
                "message": f"Snapshot file was not found: {resolved}",
            },
        }

    try:
        loaded = _load_snapshot_file(resolved)
    except json.JSONDecodeError as exc:
        return {
            "ok": False,
            "error": {
                "code": "CONTROL_PLANE_SNAPSHOT_FILE_INVALID_JSON",
                "message": f"Snapshot file is not valid JSON: {exc}",
            },
        }
    except ValueError as exc:
        return {
            "ok": False,
            "error": {
                "code": "CONTROL_PLANE_SNAPSHOT_FILE_INVALID",
                "message": str(exc),
            },
        }

    return {
        "ok": True,
        "snapshot": loaded,
        "source": {
            "kind": "file",
            "file_path": str(resolved),
            "file_name": resolved.name,
            "size_bytes": resolved.stat().st_size,
            "modified_at": datetime.fromtimestamp(resolved.stat().st_mtime, tz=UTC)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z"),
        },
    }


def list_control_plane_snapshot_files() -> dict[str, Any]:
    initialize_database()
    snapshots: list[dict[str, Any]] = []
    for path in sorted(_transfer_dir().glob("control-plane-snapshot.*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
        item: dict[str, Any] = {
            "file_name": path.name,
            "file_path": str(path),
            "size_bytes": path.stat().st_size,
            "modified_at": datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z"),
        }
        try:
            snapshot = _load_snapshot_file(path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            item["read_error"] = str(exc)
            snapshots.append(item)
            continue
        state = _state_from_snapshot(snapshot)
        item.update(
            {
                "snapshot_version": snapshot.get("snapshot_version"),
                "exported_at": snapshot.get("exported_at"),
                "subjects_count": len(state.get("subjects") or []),
                "servers_count": len(state.get("servers") or []),
                "include_secrets": bool(
                    ((snapshot.get("export_options") if isinstance(snapshot.get("export_options"), dict) else {}) or {}).get("include_secrets")
                ),
                "warnings_count": len(snapshot.get("warnings") or []),
            }
        )
        snapshots.append(item)
    return {
        "snapshots": snapshots,
        "transfer_dir": str(_transfer_dir()),
    }

