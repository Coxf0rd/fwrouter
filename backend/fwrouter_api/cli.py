from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import stat
import re
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from uuid import uuid4

from fwrouter_api.services import diagnostics
from fwrouter_api.services.reconcile import ReconcileResult, build_reconcile_response


def _count(results: list[ReconcileResult], entity_type: str, state: str | None = None) -> int:
    return sum(
        1
        for result in results
        if result.entity_type == entity_type and (state is None or result.reconcile_state == state)
    )


def _is_xray_result(result: ReconcileResult) -> bool:
    return result.entity_type == "xray" or (
        result.entity_type == "subject"
        and result.details.get("implementation_kind") == "xray"
    )


def _count_xray(results: list[ReconcileResult], state: str | None = None) -> int:
    return sum(
        1
        for result in results
        if _is_xray_result(result) and (state is None or result.reconcile_state == state)
    )


def _print_reconcile_check() -> int:
    response = build_reconcile_response()
    results = response.entities
    summary = response.summary
    system_ok = summary["drift"] == 0 and summary["stale"] == 0 and summary["failed"] == 0
    print("SYSTEM OK" if system_ok else "SYSTEM DRIFT")
    print()

    print("XRay:")
    print(f"  {_count_xray(results)} checked")
    print(f"  {_count_xray(results, 'drift')} drift")
    print(f"  {_count_xray(results, 'stale')} stale")
    print()

    routing = next((result for result in results if result.entity_type == "routing"), None)
    routing_state = "OK" if routing and routing.reconcile_state == "in_sync" else "DRIFT"
    dataplane_state = routing_state
    print("Routing:")
    print(f"  rules={routing_state}")
    print(f"  dataplane={dataplane_state}")

    extra = Counter(result.reconcile_state for result in results)
    if not system_ok:
        print()
        print("Summary:")
        for state in ("drift", "stale", "failed", "unknown"):
            count = extra.get(state, 0)
            if count:
                print(f"  {state}={count}")
    return 0 if system_ok else 1


def _print_diagnose(*, json_output: bool = False) -> int:
    report = diagnostics.build_diagnostic_report()
    if json_output:
        print(json.dumps(report.model_dump(mode="json"), ensure_ascii=False, sort_keys=True))
    else:
        print(diagnostics.format_diagnostic_report(report))
    return 0 if report.status in {"healthy", "warning", "inactive", "disabled"} else 1


def _load_private_identity_manifest(path_value: str) -> dict[str, object]:
    path = Path(path_value)
    parent_info = path.parent.lstat()
    if (stat.S_ISLNK(parent_info.st_mode) or not stat.S_ISDIR(parent_info.st_mode)
            or parent_info.st_uid != os.geteuid()
            or stat.S_IMODE(parent_info.st_mode) & 0o022):
        raise ValueError("IDENTITY_MANIFEST_PARENT_NOT_PRIVATE")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
                or info.st_size <= 0 or info.st_size > 4 * 1024 * 1024):
            raise ValueError("IDENTITY_MANIFEST_FILE_NOT_PRIVATE")
        path_info = path.lstat()
        if (stat.S_ISLNK(path_info.st_mode) or path_info.st_dev != info.st_dev
                or path_info.st_ino != info.st_ino):
            raise ValueError("IDENTITY_MANIFEST_FILE_CHANGED")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            raw = stream.read(4 * 1024 * 1024 + 1)
    finally:
        os.close(descriptor)
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict) or value.get("version") != 1:
        raise ValueError("IDENTITY_MANIFEST_INVALID")
    allowed = {
        "version", "expected_selection_revision", "expected_identity_digest",
        "checkpoint_sha256", "mappings",
    }
    if set(value) - allowed:
        raise ValueError("IDENTITY_MANIFEST_INVALID")
    if type(value.get("expected_selection_revision")) is not int:
        raise ValueError("IDENTITY_MANIFEST_SELECTION_FENCE_REQUIRED")
    digest = str(value.get("expected_identity_digest") or "")
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ValueError("IDENTITY_MANIFEST_PREIMAGE_DIGEST_REQUIRED")
    if not isinstance(value.get("mappings"), list):
        raise ValueError("IDENTITY_MANIFEST_INVALID")
    return value


def _run_identity_migration(args: argparse.Namespace) -> int:
    try:
        manifest = _load_private_identity_manifest(args.manifest)
        if args.apply and not args.expected_plan_sha256:
            raise ValueError("IDENTITY_MIGRATION_PLAN_DIGEST_REQUIRED")
        if not args.apply and args.expected_plan_sha256:
            raise ValueError("IDENTITY_MIGRATION_PLAN_DIGEST_UNEXPECTED")
        from fwrouter_api.services.xray_subscription_service import migrate_subscription_profile_identities

        result = migrate_subscription_profile_identities(
            manifest["mappings"],
            expected_selection_revision=manifest["expected_selection_revision"],
            expected_identity_digest=manifest["expected_identity_digest"],
            checkpoint_sha256=manifest.get("checkpoint_sha256"),
            apply=bool(args.apply),
            expected_plan_sha256=args.expected_plan_sha256,
        )
        safe_keys = {
            "ok", "status", "mappings_count", "changed_count", "preserved_identity_count",
            "selection_revision", "identity_preimage_sha256", "plan_sha256",
            "checkpoint_sha256", "native_applied", "public_snapshots_changed",
            "fresh_generation_required",
        }
        print(json.dumps({key: value for key, value in result.items() if key in safe_keys},
                         ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as exc:
        candidate = str(exc)
        code = candidate if re.fullmatch(r"IDENTITY_[A-Z0-9_]+", candidate) else type(exc).__name__
        print(json.dumps({"ok": False, "error_code": code}, sort_keys=True))
        return 1


def _run_checkpoint_supersession(args: argparse.Namespace) -> int:
    if not args.apply:
        print(json.dumps({"ok": False, "error_code": "XRAY_SUPERSESSION_APPLY_REQUIRED"}))
        return 2
    try:
        from fwrouter_api.services.xray_subscription_service import reconcile_xray_subscription_profile_nodes

        result = reconcile_xray_subscription_profile_nodes(
            requested_by="operator",
            include_vpn_auto=True,
            _supersede_checkpoint_sha256=args.checkpoint_sha256,
            _migration_plan_sha256=args.migration_plan_sha256,
        )
        output = {
            "ok": bool(result.get("ok")),
            "status": result.get("status"),
            "stage": result.get("stage") if isinstance(result.get("stage"), str)
            and re.fullmatch(r"[a-z0-9_]+", result["stage"]) else None,
            "error_code": result.get("error_code") if isinstance(result.get("error_code"), str)
            and re.fullmatch(r"[A-Z0-9_]+", result["error_code"]) else None,
            "failed_operation": result.get("failed_operation")
            if isinstance(result.get("failed_operation"), str)
            and re.fullmatch(r"[A-Z0-9_]{1,96}", result["failed_operation"]) else None,
            "compensation_reason": result.get("compensation_reason")
            if isinstance(result.get("compensation_reason"), str)
            and re.fullmatch(r"[a-z0-9_]{1,96}", result["compensation_reason"]) else None,
            "supersedes_checkpoint_sha256": args.checkpoint_sha256,
            "migration_plan_sha256": args.migration_plan_sha256,
            "native_applied": bool(result.get("generation_apply")),
            "recovery_status": (result.get("generation_recovery") or {}).get("recovered")
            if isinstance(result.get("generation_recovery"), dict)
            and isinstance((result.get("generation_recovery") or {}).get("recovered"), str)
            and re.fullmatch(r"[a-z0-9_]+", (result.get("generation_recovery") or {})["recovered"])
            else None,
            "recovery_reason": (result.get("generation_recovery") or {}).get("reason")
            if isinstance(result.get("generation_recovery"), dict)
            and isinstance((result.get("generation_recovery") or {}).get("reason"), str)
            and re.fullmatch(r"[a-z0-9_]+", (result.get("generation_recovery") or {})["reason"])
            else None,
        }
        print(json.dumps(output, ensure_ascii=False, sort_keys=True))
        return 0 if output["ok"] else 1
    except Exception as exc:
        candidate = str(exc)
        code = candidate if re.fullmatch(r"XRAY_[A-Z0-9_]+", candidate) else type(exc).__name__
        print(json.dumps({"ok": False, "error_code": code}, sort_keys=True))
        return 1


def _run_legacy_inventory_admission(args: argparse.Namespace) -> int:
    try:
        from fwrouter_api.services.xray_subscription_service import (
            admit_legacy_subscription_inventory, inspect_legacy_subscription_inventory_admission,
        )

        if args.plan:
            result = inspect_legacy_subscription_inventory_admission()
            safe_keys = {
                "ok", "status", "eligible", "action", "native_proof_required", "inventory_digest", "inventory_count",
                "generation_status", "generation_reason", "checkpoint_mode", "checkpoint_phase",
                "source_fingerprint", "reason",
            }
            output = {key: result[key] for key in safe_keys if key in result}
            print(json.dumps(output, ensure_ascii=False, sort_keys=True))
            return 0 if output.get("ok") is True else 1
        if not args.expected_inventory_digest or not args.confirm:
            print(json.dumps({"ok": False, "status": "blocked", "error_code": "ADMISSION_CONFIRMATION_REQUIRED"}))
            return 2

        result = admit_legacy_subscription_inventory(
            expected_digest=args.expected_inventory_digest,
            operator_confirmation=args.confirm,
        )
        safe_keys = {
            "ok", "status", "reason", "digest", "revision", "already_admitted",
            "last_good_retained",
        }
        output = {key: result[key] for key in safe_keys if key in result}
        if (result.get("ok") is True and result.get("status") == "verified"
                and result.get("proof_receipt_written") is True):
            output["proof_receipt_written"] = True
        print(json.dumps(output, ensure_ascii=False, sort_keys=True))
        return 0 if output.get("ok") is True else 1
    except Exception as exc:
        candidate = str(exc)
        code = candidate if re.fullmatch(r"XRAY_[A-Z0-9_]+", candidate) else type(exc).__name__
        print(json.dumps({"ok": False, "status": "blocked", "error_code": code}, sort_keys=True))
        return 1


_SCHEMA_25_IDENTITY_TABLES = frozenset({
    "subscription_node_identities",
    "subscription_node_aliases",
    "subscription_node_credentials",
    "subscription_node_identity_transitions",
    "subscription_node_credential_history",
})


def _column_contract(connection: sqlite3.Connection, table_name: str) -> dict[str, tuple[str, int, str | None, int]]:
    """Return column semantics without depending on SQLite declaration formatting."""
    return {
        str(row["name"]): (
            " ".join(str(row["type"] or "").upper().split()),
            int(row["notnull"]),
            " ".join(str(row["dflt_value"]).strip().split())
            if row["dflt_value"] is not None else None,
            int(row["pk"]),
        )
        for row in connection.execute(f"PRAGMA table_info({table_name})")
    }


def _foreign_key_contract(connection: sqlite3.Connection, table_name: str) -> tuple[tuple[object, ...], ...]:
    rows = connection.execute(f"PRAGMA foreign_key_list({table_name})").fetchall()
    return tuple(sorted(
        (
            str(row["table"]), str(row["from"]), str(row["to"]),
            str(row["on_update"]).upper(), str(row["on_delete"]).upper(),
            str(row["match"]).upper(), int(row["id"]), int(row["seq"]),
        )
        for row in rows
    ))


def _index_contracts(connection: sqlite3.Connection, table_name: str) -> set[tuple[object, ...]]:
    """Describe indexes semantically; custom non-unique performance indexes may be additive."""
    contracts: set[tuple[object, ...]] = set()
    for index in connection.execute(f"PRAGMA index_list({table_name})"):
        quoted_index = '"' + str(index["name"]).replace('"', '""') + '"'
        columns = tuple(
            (int(row["seqno"]),
             str(row["name"]) if row["name"] is not None else None,
             int(row["desc"]), str(row["coll"] or "BINARY").upper(), int(row["key"]))
            for row in connection.execute(f"PRAGMA index_xinfo({quoted_index})")
            if int(row["key"])
        )
        contracts.add((int(index["unique"]), int(index["partial"]), columns))
    return contracts


def _schema_version_matches_canonical_structure(
    connection: sqlite3.Connection,
    schema_version: int,
) -> bool:
    """Require a supported schema version to match canonical SQLite structure.

    `inspect_database_schema` intentionally audits selected critical tables. An
    upgrade admission check has a stricter job: it must reject missing or
    altered baseline tables before creating a backup or applying migration.
    Build the reference from the repository's canonical schema.sql. For v24,
    remove only the five additive v25 identity tables. Compare SQLite metadata
    so equivalent DDL formatting and harmless custom non-unique indexes remain
    compatible.
    """
    expected = sqlite3.connect(":memory:")
    expected.row_factory = sqlite3.Row
    try:
        schema_path = Path(__file__).parent / "db" / "schema.sql"
        expected.executescript(schema_path.read_text(encoding="utf-8"))
        if schema_version == 24:
            for table_name in (
                "subscription_node_credential_history",
                "subscription_node_identity_transitions",
                "subscription_node_credentials",
                "subscription_node_aliases",
                "subscription_node_identities",
            ):
                expected.execute(f'DROP TABLE "{table_name}"')
            expected.execute(
                "UPDATE schema_meta SET value = '24' WHERE key = 'schema_version'"
            )
        elif schema_version != 25:
            return False

        expected_tables = {
            str(row["name"])
            for row in expected.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        actual_tables = {
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        if not expected_tables.issubset(actual_tables):
            return False
        for table_name in expected_tables:
            if _column_contract(expected, table_name) != _column_contract(connection, table_name):
                return False
            if _foreign_key_contract(expected, table_name) != _foreign_key_contract(connection, table_name):
                return False
            expected_indexes = _index_contracts(expected, table_name)
            actual_indexes = _index_contracts(connection, table_name)
            if not expected_indexes.issubset(actual_indexes):
                return False
            # Additional non-unique indexes do not alter the data contract.
            # Unknown UNIQUE indexes can change accepted writes, so fail closed.
            if any(index not in expected_indexes and index[0] for index in actual_indexes):
                return False
        return True
    finally:
        expected.close()


def _database_upgrade_preflight(path: Path, *, expected_schema: int, target_schema: int) -> int:
    _validate_database_path(path)
    from fwrouter_api.db.schema_state import EXPECTED_SCHEMA_VERSION, inspect_database_schema

    uri = path.absolute().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=5.0)
    connection.row_factory = sqlite3.Row
    canonical_structure_matches = False
    try:
        state = inspect_database_schema(connection)
        try:
            integrity = [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]
            foreign_key_violations = list(connection.execute("PRAGMA foreign_key_check"))
        except sqlite3.Error as exc:
            raise ValueError("DATABASE_SCHEMA_PREFLIGHT_FAILED") from exc
        actual_value = state.get("actual_schema_version")
        try:
            actual_schema = int(actual_value) if actual_value is not None else None
        except (TypeError, ValueError):
            actual_schema = None
        if (
            (expected_schema, target_schema) in {(24, 25), (25, 25)}
            and actual_schema == expected_schema
            and integrity == ["ok"]
            and not foreign_key_violations
        ):
            canonical_structure_matches = _schema_version_matches_canonical_structure(
                connection, expected_schema,
            )
    finally:
        connection.close()
    actual_value = state.get("actual_schema_version")
    try:
        actual_schema = int(actual_value) if actual_value is not None else None
    except (TypeError, ValueError):
        actual_schema = None
    if actual_schema != expected_schema:
        raise ValueError("DATABASE_SCHEMA_EXPECTATION_STALE")
    if integrity != ["ok"] or foreign_key_violations:
        raise ValueError("DATABASE_SCHEMA_PREFLIGHT_FAILED")

    if expected_schema == target_schema == int(EXPECTED_SCHEMA_VERSION):
        if not state.get("ok"):
            raise ValueError("DATABASE_SCHEMA_PREFLIGHT_FAILED")
        if not canonical_structure_matches:
            raise ValueError("DATABASE_SCHEMA_PREFLIGHT_FAILED")
        return actual_schema

    if (expected_schema, target_schema) != (24, 25):
        raise ValueError("DATABASE_SCHEMA_UPGRADE_TARGET_UNSUPPORTED")
    if not canonical_structure_matches:
        raise ValueError("DATABASE_SCHEMA_PREFLIGHT_FAILED")
    tables = state.get("tables") if isinstance(state.get("tables"), dict) else {}
    if set(_SCHEMA_25_IDENTITY_TABLES) - set(tables):
        raise ValueError("DATABASE_SCHEMA_PREFLIGHT_FAILED")
    # The only tolerated v24 drift is the version marker plus all five v25
    # identity tables being wholly absent. Partial table creation or any
    # baseline table mismatch means this is not a supported upgrade preimage.
    if any(bool(tables[name].get("exists")) for name in _SCHEMA_25_IDENTITY_TABLES):
        raise ValueError("DATABASE_SCHEMA_PREFLIGHT_FAILED")
    if any(not table.get("ok") for name, table in tables.items()
           if name not in _SCHEMA_25_IDENTITY_TABLES):
        raise ValueError("DATABASE_SCHEMA_PREFLIGHT_FAILED")
    problems = state.get("problems") if isinstance(state.get("problems"), list) else []
    allowed_problem_keys = {
        ("SCHEMA_VERSION_MISMATCH", None),
        *(("TABLE_SCHEMA_MISMATCH", name) for name in _SCHEMA_25_IDENTITY_TABLES),
    }
    if any((problem.get("code"), problem.get("table")) not in allowed_problem_keys
           for problem in problems):
        raise ValueError("DATABASE_SCHEMA_PREFLIGHT_FAILED")
    if len(problems) != len(_SCHEMA_25_IDENTITY_TABLES) + 1:
        raise ValueError("DATABASE_SCHEMA_PREFLIGHT_FAILED")
    return actual_schema


def _validate_database_path(path: Path) -> None:
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current = current / component
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise ValueError("DATABASE_PATH_SYMLINK_REJECTED")
        if current != absolute and not stat.S_ISDIR(info.st_mode):
            raise ValueError("DATABASE_PATH_INVALID")
    info = absolute.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
        raise ValueError("DATABASE_PATH_INVALID")


def _create_schema_upgrade_backup(db_path: Path, expected_schema: int) -> tuple[Path, str]:
    _validate_database_path(db_path)
    backup_dir = db_path.parent / "backups"
    try:
        backup_dir.mkdir(mode=0o700)
    except FileExistsError:
        pass
    info = backup_dir.lstat()
    if (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700):
        raise ValueError("DATABASE_BACKUP_DIRECTORY_INVALID")
    backup_path = backup_dir / f"{db_path.name}.schema-upgrade-{uuid4().hex}.bak"
    descriptor = os.open(
        backup_path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    os.close(descriptor)
    source = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30.0)
    destination = sqlite3.connect(backup_path, timeout=30.0)
    try:
        source.backup(destination)
        destination.commit()
        integrity = destination.execute("PRAGMA integrity_check").fetchone()
        version_row = destination.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()
        if (not integrity or integrity[0] != "ok" or not version_row
                or int(version_row[0]) != expected_schema):
            raise ValueError("DATABASE_BACKUP_VALIDATION_FAILED")
    finally:
        destination.close()
        source.close()
    backup_path.chmod(0o600)
    with backup_path.open("rb") as stream:
        os.fsync(stream.fileno())
    descriptor = os.open(backup_dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    digest = hashlib.sha256(backup_path.read_bytes()).hexdigest()
    return backup_path, digest


def _run_database_upgrade(args: argparse.Namespace) -> int:
    backup_path: Path | None = None
    backup_sha256: str | None = None
    try:
        from fwrouter_api.db.connection import get_db_path, initialize_database
        from fwrouter_api.db.schema_state import EXPECTED_SCHEMA_VERSION

        db_path = get_db_path()
        if not db_path.is_file():
            raise ValueError("DATABASE_SCHEMA_FILE_UNAVAILABLE")
        target_version = int(EXPECTED_SCHEMA_VERSION)
        if not (
            (args.expected_schema == target_version and target_version == 25)
            or (args.expected_schema == 24 and target_version == 25)
        ):
            raise ValueError("DATABASE_SCHEMA_UPGRADE_TARGET_UNSUPPORTED")
        current = _database_upgrade_preflight(
            db_path, expected_schema=args.expected_schema, target_schema=target_version,
        )
        if current == target_version and args.expected_schema == target_version:
            print(json.dumps({"ok": True, "status": "already_current", "current_schema": current,
                              "target_schema": target_version, "database_changed": False}, sort_keys=True))
            return 0
        if target_version != args.expected_schema + 1:
            raise ValueError("DATABASE_SCHEMA_UPGRADE_TARGET_UNSUPPORTED")
        if not args.apply:
            print(json.dumps({"ok": True, "status": "dry_run", "current_schema": current,
                              "target_schema": target_version, "database_changed": False},
                             sort_keys=True))
            return 0
        if not args.confirm_offline:
            raise ValueError("DATABASE_OFFLINE_CONFIRMATION_REQUIRED")
        from fwrouter_api.adapters.xray_common import xray_writer_guard
        with xray_writer_guard(timeout_seconds=30.0):
            _database_upgrade_preflight(
                db_path, expected_schema=args.expected_schema, target_schema=target_version,
            )
            backup_path, backup_sha256 = _create_schema_upgrade_backup(db_path, args.expected_schema)
            _database_upgrade_preflight(
                db_path, expected_schema=args.expected_schema, target_schema=target_version,
            )
            schema = initialize_database()
        if not schema.get("ok") or schema.get("actual_schema_version") != str(target_version):
            print(json.dumps({"ok": False, "status": "failed", "error_code": "DATABASE_SCHEMA_UPGRADE_FAILED",
                              "backup_path": str(backup_path), "backup_sha256": backup_sha256,
                              "actual_schema": schema.get("actual_schema_version")}, sort_keys=True))
            return 1
        print(json.dumps({"ok": True, "status": "applied", "current_schema": current,
                          "target_schema": target_version, "database_changed": True,
                          "backup_path": str(backup_path), "backup_sha256": backup_sha256}, sort_keys=True))
        return 0
    except Exception as exc:
        candidate = str(exc)
        code = candidate if re.fullmatch(r"DATABASE_[A-Z0-9_]+", candidate) else type(exc).__name__
        print(json.dumps({"ok": False, "error_code": code,
                          "backup_path": str(backup_path) if backup_path else None,
                          "backup_sha256": backup_sha256}, sort_keys=True))
        return 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fwrouter")
    subparsers = parser.add_subparsers(dest="command")
    reconcile_parser = subparsers.add_parser("reconcile")
    reconcile_subparsers = reconcile_parser.add_subparsers(dest="reconcile_command")
    reconcile_subparsers.add_parser("check")
    diagnose_parser = subparsers.add_parser("diagnose")
    diagnose_parser.add_argument("--json", action="store_true", dest="json_output")
    xray_parser = subparsers.add_parser("xray")
    xray_subparsers = xray_parser.add_subparsers(dest="xray_command")
    identity_parser = xray_subparsers.add_parser("identity-migration")
    identity_parser.add_argument("--manifest", required=True)
    identity_parser.add_argument("--apply", action="store_true")
    identity_parser.add_argument("--expected-plan-sha256")
    generation_parser = xray_subparsers.add_parser("generation")
    generation_subparsers = generation_parser.add_subparsers(dest="generation_command")
    supersede_parser = generation_subparsers.add_parser("supersede")
    supersede_parser.add_argument("--checkpoint-sha256", required=True)
    supersede_parser.add_argument("--migration-plan-sha256", required=True)
    supersede_parser.add_argument("--apply", action="store_true")
    legacy_admission_parser = generation_subparsers.add_parser("admit-legacy")
    legacy_admission_parser.add_argument("--plan", action="store_true")
    legacy_admission_parser.add_argument("--expected-inventory-digest")
    legacy_admission_parser.add_argument("--confirm")
    database_parser = subparsers.add_parser("database")
    database_subparsers = database_parser.add_subparsers(dest="database_command")
    upgrade_parser = database_subparsers.add_parser("upgrade")
    upgrade_parser.add_argument("--expected-schema", required=True, type=int)
    upgrade_parser.add_argument("--apply", action="store_true")
    upgrade_parser.add_argument(
        "--confirm-offline", action="store_true",
        help="confirm the API and background workers are stopped before applying the migration",
    )
    args = parser.parse_args(argv)

    if args.command == "reconcile" and args.reconcile_command == "check":
        return _print_reconcile_check()
    if args.command == "diagnose":
        return _print_diagnose(json_output=bool(args.json_output))
    if args.command == "xray" and args.xray_command == "identity-migration":
        return _run_identity_migration(args)
    if args.command == "xray" and args.xray_command == "generation" and args.generation_command == "supersede":
        return _run_checkpoint_supersession(args)
    if args.command == "xray" and args.xray_command == "generation" and args.generation_command == "admit-legacy":
        return _run_legacy_inventory_admission(args)
    if args.command == "database" and args.database_command == "upgrade":
        return _run_database_upgrade(args)
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
