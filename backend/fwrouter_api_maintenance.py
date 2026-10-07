from __future__ import annotations

import argparse
import json
import sqlite3

from fwrouter_api.db.connection import get_db_path, inspect_existing_database_schema
from fwrouter_api.services.database_admin import (
    rebuild_control_plane_database,
)
from fwrouter_api.db.schema_state import summarize_schema_state
from fwrouter_api.services.maintenance import run_control_plane_maintenance


def _inspect_existing_schema() -> dict[str, object]:
    """Read schema state without initializing or migrating the database."""
    try:
        state = inspect_existing_database_schema()
    except sqlite3.Error:
        return {
            "ok": False,
            "status": "unavailable",
            "summary": {"ok": False, "status": "unavailable"},
            "db_path": str(get_db_path()),
            "error": {"code": "DATABASE_UNAVAILABLE"},
        }
    return {
        **state,
        "summary": summarize_schema_state(state),
        "db_path": str(get_db_path()),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="FWRouter control-plane maintenance runner")
    subparsers = parser.add_subparsers(dest="command")

    maintenance_parser = subparsers.add_parser("cleanup", help="Run control-plane cleanup maintenance.")
    maintenance_parser.add_argument("--dry-run", action="store_true", help="Preview maintenance without deleting data.")

    subparsers.add_parser("schema-check", help="Inspect SQLite schema state and detect drift.")

    rebuild_parser = subparsers.add_parser(
        "rebuild-db",
        help="Rebuild fwrouter.db from control-plane snapshot and reconcile runtime inventory.",
    )
    rebuild_parser.add_argument("--file-path", required=True, help="Snapshot JSON path inside transfer dir.")
    rebuild_parser.add_argument(
        "--no-normalize-runtime-state",
        action="store_true",
        help="Preserve runtime/apply state from snapshot instead of resetting it.",
    )
    rebuild_parser.add_argument(
        "--requested-by",
        default="fwrouter_api_maintenance",
        help="Operator marker recorded in rebuild logs.",
    )

    args = parser.parse_args()

    if args.command == "schema-check":
        result = _inspect_existing_schema()
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        if not bool(result.get("ok")):
            raise SystemExit(1)
        return
    elif args.command == "rebuild-db":
        # Rebuild owns its snapshot resolution and selection-fence preflight.
        # Do not run API startup/bootstrap side effects ahead of those gates.
        result = rebuild_control_plane_database(
            file_path=args.file_path,
            normalize_runtime_state=not args.no_normalize_runtime_state,
            requested_by=args.requested_by,
        )
    else:
        # Cleanup assumes an existing, compatible database. Admission is a
        # read-only schema check; startup initialization/reconciliation belongs
        # to the API/installer lifecycle, not this maintenance command.
        schema = _inspect_existing_schema()
        if not bool(schema.get("ok")):
            print(json.dumps({
                "event": "control_plane_maintenance_rejected",
                "ok": False,
                "error": schema.get("error") or {"code": "DATABASE_SCHEMA_MISMATCH"},
                "schema": schema.get("summary"),
            }, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
            raise SystemExit(1)
        dry_run = bool(getattr(args, "dry_run", False))
        result = run_control_plane_maintenance(dry_run=dry_run)

    if args.command in {None, "cleanup"}:
        summary = {
            "event": "control_plane_maintenance_completed",
            "dry_run": bool(getattr(args, "dry_run", False)),
            "operational_logs_deleted": result.get("operational_logs", {}).get("deleted_count", 0),
            "subjects_deleted": result.get("subjects", {}).get("deleted_count", 0),
            "jobs_deleted": result.get("jobs_retention", {}).get("deleted_jobs_count", 0),
            "apply_versions_deleted": result.get("apply_versions_retention", {}).get("deleted_apply_versions_count", 0),
            "technical_log_lines_deleted": result.get("log_retention", {}).get("technical", {}).get("deleted_lines_count", 0),
            "traffic_history_deleted": result.get("traffic_history", {}).get("deleted_count", 0),
            "database_vacuumed": result.get("database_storage", {}).get("vacuumed", False),
        }
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
