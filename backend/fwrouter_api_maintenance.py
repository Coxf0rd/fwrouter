from __future__ import annotations

import argparse
import json

from fwrouter_api.services.bootstrap import bootstrap_backend
from fwrouter_api.services.database_admin import (
    get_database_schema_state,
    rebuild_control_plane_database,
)
from fwrouter_api.services.maintenance import run_control_plane_maintenance


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

    bootstrap_backend()
    if args.command == "schema-check":
        result = get_database_schema_state()
    elif args.command == "rebuild-db":
        result = rebuild_control_plane_database(
            file_path=args.file_path,
            normalize_runtime_state=not args.no_normalize_runtime_state,
            requested_by=args.requested_by,
        )
    else:
        dry_run = bool(getattr(args, "dry_run", False))
        result = run_control_plane_maintenance(dry_run=dry_run)

    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
