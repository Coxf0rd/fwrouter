from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
import uuid

from fwrouter_api.db.connection import get_db_path, inspect_existing_database_schema
from fwrouter_api.services.database_admin import (
    rebuild_control_plane_database,
)
from fwrouter_api.db.schema_state import summarize_schema_state
from fwrouter_api.services.maintenance import run_control_plane_maintenance
from fwrouter_api.services.maintenance_lifecycle_admission import (
    DEFAULT_STATE_ROOT,
    MaintenanceAdmissionError,
    acquire_shared_lease_fd,
    offline_rebuild_lease,
    runtime_shared_lease,
    startup_allowed,
)


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


def main() -> int:
    parser = argparse.ArgumentParser(description="FWRouter control-plane maintenance runner")
    subparsers = parser.add_subparsers(dest="command")

    maintenance_parser = subparsers.add_parser("cleanup", help="Run control-plane cleanup maintenance.")
    maintenance_parser.add_argument("--dry-run", action="store_true", help="Preview maintenance without deleting data.")

    subparsers.add_parser("schema-check", help="Inspect SQLite schema state and detect drift.")
    subparsers.add_parser("startup-check", help="Read-only systemd gate for an interrupted offline rebuild.")
    subparsers.add_parser("run-api", help="Hold shared maintenance admission across host preflight and API startup.")

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
    rebuild_parser.add_argument("--operation-id", help=argparse.SUPPRESS)
    rebuild_parser.add_argument("--maintenance-lock-fd", type=int, help=argparse.SUPPRESS)
    rebuild_parser.add_argument("--lifecycle-lock-fd", type=int, help=argparse.SUPPRESS)
    rebuild_parser.add_argument("--lifecycle-state-root", help=argparse.SUPPRESS)
    rebuild_parser.add_argument(
        "--requested-by",
        default="fwrouter_api_maintenance",
        help="Operator marker recorded in rebuild logs.",
    )

    args = parser.parse_args()

    # This barrier has one fixed host identity. A DB/state path override must
    # never redirect admission away from the lock shared with systemd/installer.
    state_root = DEFAULT_STATE_ROOT
    if args.command == "startup-check":
        try:
            allowed = startup_allowed(state_root)
        except MaintenanceAdmissionError:
            allowed = False
        if not allowed:
            print("fwrouter-maintenance: startup blocked by pending offline maintenance", flush=True)
            return 1
        print("fwrouter-maintenance: startup allowed", flush=True)
        return 0

    if args.command == "run-api":
        # ExecCondition remains a cheap early rejection. This held lease closes
        # the race between that condition, boot preflight side effects and API
        # lifetime: an exclusive rebuild cannot start in those gaps.
        fd = acquire_shared_lease_fd(state_root)
        try:
            os.set_inheritable(fd, True)
            preflight = subprocess.run(
                ["/usr/local/libexec/fwrouter/fwrouter-boot-preflight.sh"],
                check=False,
                pass_fds=(fd,),
            )
            if preflight.returncode != 0:
                return int(preflight.returncode)
            os.execv(sys.executable, [
                sys.executable, "-m", "uvicorn", "fwrouter_api.main:app",
                "--host", "127.0.0.1", "--port", "5000", "--no-access-log",
            ])
        finally:
            os.close(fd)

    if args.command == "schema-check":
        with runtime_shared_lease(state_root):
            result = _inspect_existing_schema()
            print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
            if not bool(result.get("ok")):
                raise SystemExit(1)
            return 0
    elif args.command == "rebuild-db":
        # The inherited descriptor is the lifecycle owner's held EX lease;
        # the marker and quiescence are independently revalidated here.
        try:
            requested_root = Path(args.lifecycle_state_root or state_root)
            if requested_root != state_root:
                raise MaintenanceAdmissionError("lifecycle state root does not match installed settings")
            with offline_rebuild_lease(
                args.maintenance_lock_fd, args.lifecycle_lock_fd, state_root=state_root,
            ) as operation_id:
                try:
                    requested_operation_id = uuid.UUID(args.operation_id or "").hex
                except (ValueError, AttributeError) as exc:
                    raise MaintenanceAdmissionError("offline operation identity is missing or invalid") from exc
                if uuid.UUID(operation_id).hex != requested_operation_id:
                    raise MaintenanceAdmissionError("offline operation identity mismatch")
                result = rebuild_control_plane_database(
                    file_path=args.file_path,
                    normalize_runtime_state=not args.no_normalize_runtime_state,
                    requested_by=args.requested_by,
                    operation_id=operation_id,
                )
        except (MaintenanceAdmissionError, TypeError, ValueError) as exc:
            result = {
                "ok": False,
                "stage": "maintenance_admission",
                "error": {"code": "DATABASE_REBUILD_MAINTENANCE_CONTEXT_UNQUALIFIED",
                          "message": str(exc)},
            }
    else:
        with runtime_shared_lease(state_root):
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
        if args.command == "rebuild-db" and not bool(result.get("ok")):
            raise SystemExit(1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
