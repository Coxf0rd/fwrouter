from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import connect, get_db_path, initialize_database
from fwrouter_api.db.schema_state import inspect_database_schema, summarize_schema_state
from fwrouter_api.services.control_plane_transfer import (
    import_control_plane_snapshot,
    resolve_control_plane_snapshot_source,
)
from fwrouter_api.services.logs import write_operational_log, write_technical_log
from fwrouter_api.services.modules import get_module_state
from fwrouter_api.services.system_subjects import ensure_builtin_system_subjects
from fwrouter_api.services.subject_inventory import sync_subject_inventory


def get_database_schema_state() -> dict[str, Any]:
    initialize_database()
    with connect() as connection:
        schema_state = inspect_database_schema(connection)
    return {
        **schema_state,
        "summary": summarize_schema_state(schema_state),
        "db_path": str(get_db_path()),
    }


def _utc_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _backup_dir() -> Path:
    path = get_settings().paths.state_dir / "backups"
    path.mkdir(parents=True, exist_ok=True)
    return path


def backup_database_file() -> dict[str, Any]:
    db_path = get_db_path()
    if not db_path.exists():
        return {
            "ok": True,
            "created": False,
            "backup_path": None,
            "db_path": str(db_path),
        }

    backup_path = _backup_dir() / f"{db_path.name}.{_utc_stamp()}.bak"
    shutil.copy2(db_path, backup_path)
    return {
        "ok": True,
        "created": True,
        "backup_path": str(backup_path),
        "db_path": str(db_path),
    }


def reconcile_control_plane_runtime(*, requested_by: str = "database_reconcile") -> dict[str, Any]:
    ensure_builtin_system_subjects()
    tailscale_module = get_module_state("tailscale")
    discover_tailscale = bool(tailscale_module and tailscale_module.get("desired_state") == "enabled")
    sync_result = sync_subject_inventory(
        requested_by=requested_by,
        discover_docker=True,
        discover_host=True,
        discover_tailscale=discover_tailscale,
        discover_xray=True,
        include_all_tailscale_peers=False,
        lan_clients=[],
        tailscale_nodes=[],
        host_services=[],
    )
    return {
        "builtin_system_subjects_ensured": True,
        "subject_inventory": sync_result,
    }


def rebuild_control_plane_database(
    *,
    snapshot: dict[str, Any] | None = None,
    file_path: str | None = None,
    normalize_runtime_state: bool = True,
    requested_by: str = "database_rebuild",
) -> dict[str, Any]:
    source = resolve_control_plane_snapshot_source(snapshot=snapshot, file_path=file_path)
    if not source["ok"]:
        return {
            "ok": False,
            "stage": "resolve_snapshot",
            "error": source["error"],
        }

    backup = backup_database_file()
    db_path = get_db_path()
    if db_path.exists():
        db_path.unlink()

    schema_state = initialize_database()
    if not schema_state["ok"]:
        write_technical_log(
            component="database_admin",
            event_type="database_rebuild_schema_mismatch",
            message="Newly initialized database still reports schema drift.",
            details=schema_state,
        )
        return {
            "ok": False,
            "stage": "initialize_database",
            "backup": backup,
            "schema": {
                **schema_state,
                "summary": summarize_schema_state(schema_state),
            },
            "error": {
                "code": "DATABASE_SCHEMA_MISMATCH",
                "message": "Freshly initialized database schema does not match expected structure.",
            },
        }

    imported = import_control_plane_snapshot(
        source["snapshot"],
        normalize_runtime_state=normalize_runtime_state,
    )
    if not imported["ok"]:
        return {
            "ok": False,
            "stage": "import_snapshot",
            "backup": backup,
            "import": imported,
            "error": {
                "code": "DATABASE_REBUILD_IMPORT_FAILED",
                "message": "Control-plane snapshot import failed after database rebuild.",
            },
        }

    reconciliation = reconcile_control_plane_runtime(requested_by=requested_by)
    final_schema_state = get_database_schema_state()
    write_operational_log(
        event_type="control_plane_database_rebuilt",
        message="Control-plane SQLite database was rebuilt from snapshot.",
        details={
            "requested_by": requested_by,
            "normalize_runtime_state": normalize_runtime_state,
            "snapshot_source": source["source"],
            "backup": backup,
            "schema": final_schema_state["summary"],
        },
    )
    return {
        "ok": True,
        "backup": backup,
        "source": source["source"],
        "normalize_runtime_state": normalize_runtime_state,
        "import": imported,
        "reconciliation": reconciliation,
        "schema": final_schema_state,
    }


def cleanup_runtime_state(*, requested_by: str = "database_cleanup") -> dict[str, Any]:
    state_dir = get_settings().paths.state_dir
    duplicate_db_paths = [
        state_dir / "db.sqlite",
        state_dir / "state.db",
        state_dir / "state" / "fwrouter.db",
        Path("/opt/fwrouter-api/fwrouter.db"),
    ]
    removed_files: list[str] = []
    kept_files: list[str] = []

    for path in duplicate_db_paths:
        if not path.exists():
            continue
        if path == get_db_path():
            kept_files.append(str(path))
            continue
        if path.stat().st_size == 0:
            path.unlink()
            removed_files.append(str(path))
        else:
            kept_files.append(str(path))

    deleted_subject_rows = 0
    deleted_server_rows = 0
    deleted_snapshot_rows = 0
    deleted_server_pref_rows = 0

    test_subject_ids = {
        "lan-1",
        "lan-2",
        "lan-fail",
        "lan-fail-user",
        "lan-follow",
        "lan-state",
        "lan-traffic",
        "lan-transfer",
        "lan-user",
        "lan-vpn",
        "legacy-ts-1",
        "ts-node-2",
        "tailscale-node:peer-1",
        "tailscale-node:peer-a",
        "tailscale-node:test-peer",
        "xray:uuid-list",
    }

    with connect() as connection:
        placeholders = ", ".join("?" for _ in test_subject_ids)
        deleted_snapshot_rows = connection.execute(
            """
            DELETE FROM traffic_counter_snapshots
            WHERE subject_id IS NOT NULL
              AND subject_id NOT IN (SELECT subject_id FROM subjects)
            """
        ).rowcount
        deleted_server_pref_rows = connection.execute(
            """
            DELETE FROM server_preferences
            WHERE server_id IN (
                SELECT p.server_id
                FROM server_preferences AS p
                JOIN servers AS s ON s.server_id = p.server_id
                WHERE s.inventory_state != 'active'
                  AND lower(s.server_id) = 'test'
            )
            """
        ).rowcount
        deleted_subject_rows = connection.execute(
            f"""
            DELETE FROM subjects
            WHERE subject_id IN ({placeholders})
            """,
            tuple(sorted(test_subject_ids)),
        ).rowcount
        deleted_server_rows = connection.execute(
            """
            DELETE FROM servers
            WHERE lower(server_id) = 'test'
            """
        ).rowcount
        connection.commit()

    result = {
        "ok": True,
        "requested_by": requested_by,
        "removed_files": removed_files,
        "kept_files": kept_files,
        "deleted_subject_rows": deleted_subject_rows,
        "deleted_server_rows": deleted_server_rows,
        "deleted_snapshot_rows": deleted_snapshot_rows,
        "deleted_server_preference_rows": deleted_server_pref_rows,
    }
    write_operational_log(
        event_type="runtime_state_cleanup_completed",
        message="Runtime state cleanup completed.",
        details=result,
    )
    return result
