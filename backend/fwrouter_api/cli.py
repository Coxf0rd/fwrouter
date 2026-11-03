from __future__ import annotations

import argparse
import json
import os
import stat
import re
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fwrouter_api.services.reconcile import ReconcileResult

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


def build_reconcile_response():
    """Keep the existing CLI seam without eager domain imports for config."""
    from fwrouter_api.services.reconcile import build_reconcile_response as build

    return build()


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
    from fwrouter_api.services import diagnostics

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


def _run_native_proof(args: argparse.Namespace) -> int:
    from fwrouter_api.services.native_proof import collect_native_proof

    proof = collect_native_proof(
        scope=str(args.scope),
        expected_backend_manifest_sha256=str(args.expected_backend_manifest_sha256),
        expected_xray_image_digest=args.expected_xray_image_digest,
        expected_mihomo_image_digest=args.expected_mihomo_image_digest,
    )
    print(json.dumps(proof, ensure_ascii=False, sort_keys=True))
    return 0 if proof.get("status") == "PASS" else 1


def _run_core_ip_receipt_reattest(args: argparse.Namespace) -> int:
    from fwrouter_api.services.native_proof import reattest_core_ip_receipt

    result = reattest_core_ip_receipt(
        expected_tool_manifest_sha256=args.expected_tool_manifest_sha256,
        expected_installed_backend_manifest_sha256=args.expected_installed_backend_manifest_sha256,
        apply=bool(args.apply), expected_plan_sha256=args.expected_plan_sha256,
        confirmation=args.confirm,
    )
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0 if result.get("status") in {"READY", "NOOP", "PASS"} else 1


def _is_root_process() -> bool:
    """Keep the owner CLI's effective-UID check explicit and testable."""
    return os.geteuid() == 0


def _run_legacy_inventory_admission(args: argparse.Namespace) -> int:
    try:
        from fwrouter_api.services.xray_subscription_service import (
            admit_legacy_subscription_inventory, inspect_legacy_subscription_inventory_admission,
        )

        if args.plan:
            plan_kwargs = {"require_native_proof": True} if args.require_native_proof else {}
            result = inspect_legacy_subscription_inventory_admission(**plan_kwargs)
            safe_keys = {
                "ok", "status", "eligible", "action", "native_proof_required", "inventory_digest", "inventory_count",
                "generation_status", "generation_reason", "checkpoint_mode", "checkpoint_phase",
                "source_fingerprint", "reason", "native_generation_proof_refresh",
            }
            output = {key: result[key] for key in safe_keys if key in result}
            print(json.dumps(output, ensure_ascii=False, sort_keys=True))
            return 0 if output.get("ok") is True else 1
        if not args.expected_inventory_digest or not args.confirm:
            print(json.dumps({"ok": False, "status": "blocked", "error_code": "ADMISSION_CONFIRMATION_REQUIRED"}))
            return 2

        admission_kwargs = {
            "expected_digest": args.expected_inventory_digest,
            "operator_confirmation": args.confirm,
        }
        if args.require_native_proof:
            admission_kwargs["require_native_proof"] = True
        result = admit_legacy_subscription_inventory(**admission_kwargs)
        safe_keys = {
            "ok", "status", "reason", "digest", "revision", "already_admitted",
            "last_good_retained",
        }
        output = {key: result[key] for key in safe_keys if key in result}
        # The strict failure detail is classified by the native-proof owner.
        # Keep it limited to failed admission responses and verify it against
        # that same allowlist before extending the CLI's public JSON projection.
        native_proof_reason = result.get("native_proof_reason")
        if result.get("ok") is False and isinstance(native_proof_reason, str):
            from fwrouter_api.services.native_proof import _SAFE_REASON_CODES

            if native_proof_reason in _SAFE_REASON_CODES:
                output["native_proof_reason"] = native_proof_reason
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


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(argv) if argv is not None else None
    if arguments is None:
        import sys
        arguments = sys.argv[1:]
    if arguments and arguments[0] == "database":
        # Retain the established, non-mutating current-schema verification
        # command. Historical 24/25/26 upgrade execution is retired.
        database_parser = argparse.ArgumentParser(prog="fwrouter database")
        database_subparsers = database_parser.add_subparsers(dest="database_command")
        upgrade_parser = database_subparsers.add_parser("upgrade")
        upgrade_parser.add_argument("--expected-schema", required=True, type=int)
        upgrade_parser.add_argument("--apply", action="store_true")
        upgrade_parser.add_argument("--confirm-offline", action="store_true")
        upgrade_args = database_parser.parse_args(arguments[1:])
        if upgrade_args.database_command != "upgrade":
            database_parser.print_help()
            return 2
        if upgrade_args.expected_schema == 27:
            try:
                import sqlite3
                from fwrouter_api.core.config import get_settings
                from fwrouter_api.db.schema_state import inspect_database_schema

                path = get_settings().paths.db_path
                _validate_database_path(path)
                connection = sqlite3.connect(f"{path.absolute().as_uri()}?mode=ro", uri=True, timeout=5)
                connection.row_factory = sqlite3.Row
                try:
                    state = inspect_database_schema(
                        connection, allow_legacy_schema27_profiles=False,
                    )
                    integrity = [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]
                    foreign_keys = list(connection.execute("PRAGMA foreign_key_check"))
                finally:
                    connection.close()
                if not state["ok"] or integrity != ["ok"] or foreign_keys:
                    raise ValueError("DATABASE_SCHEMA_PREFLIGHT_FAILED")
                print(json.dumps({"ok": True, "status": "already_current", "current_schema": 27,
                                  "target_schema": 27, "database_changed": False}, sort_keys=True))
                return 0
            except Exception as exc:
                code = str(exc) if str(exc).startswith("DATABASE_") else type(exc).__name__
                print(json.dumps({"ok": False, "error_code": code}, sort_keys=True))
                return 1
        print(json.dumps({"ok": False, "error_code": "DATABASE_SCHEMA_MIGRATION_RETIRED",
                          "supported_schema": 27}, sort_keys=True))
        return 2
    if arguments and arguments[0] == "api-clients":
        # Host policy provisioning is independent of database/runtime lifecycle.
        # The bounded admin entrypoint enforces its own root/path/lease checks.
        from fwrouter_api.services.api_client_registry_admin import admin_main
        return admin_main(arguments[1:])
    if arguments and arguments[0] == "config":
        # The configuration client is a plain HTTP client. Dispatch before
        # importing lifecycle, diagnostic, or domain mutation services.
        from fwrouter_api.services.config_cli import main as config_main
        return config_main(arguments[1:])
    if arguments[:2] == ["core", "ip-receipt-reattest"] and not _is_root_process():
        print(json.dumps({"status": "BLOCKED", "reason": "CORE_IP_RECEIPT_REATTEST_ROOT_REQUIRED"}))
        return 1
    from fwrouter_api.services.maintenance_lifecycle_admission import (
        DEFAULT_STATE_ROOT,
        runtime_shared_lease,
    )
    parser = argparse.ArgumentParser(prog="fwrouter")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("config", help="Read or conditionally update presentation configuration through the API.")
    subparsers.add_parser("api-clients", help="Manage protected host-owned API client credentials.")
    reconcile_parser = subparsers.add_parser("reconcile")
    reconcile_subparsers = reconcile_parser.add_subparsers(dest="reconcile_command")
    reconcile_subparsers.add_parser("check")
    diagnose_parser = subparsers.add_parser("diagnose")
    diagnose_parser.add_argument("--json", action="store_true", dest="json_output")
    native_proof_parser = subparsers.add_parser("native-proof")
    native_proof_parser.add_argument("--scope", choices=("ui",), required=True)
    native_proof_parser.add_argument("--expected-backend-manifest-sha256", required=True)
    native_proof_parser.add_argument("--expected-xray-image-digest", required=True)
    native_proof_parser.add_argument("--expected-mihomo-image-digest", required=True)
    core_parser = subparsers.add_parser("core", help="Bounded Core-owned evidence operations.")
    core_subparsers = core_parser.add_subparsers(dest="core_command")
    core_reattest_parser = core_subparsers.add_parser(
        "ip-receipt-reattest", help="Plan or explicitly re-attest only the Core IP receipt binding.",
    )
    core_reattest_mode = core_reattest_parser.add_mutually_exclusive_group(required=True)
    core_reattest_mode.add_argument("--plan", action="store_true")
    core_reattest_mode.add_argument("--apply", action="store_true")
    core_reattest_parser.add_argument("--expected-tool-manifest-sha256", required=True)
    core_reattest_parser.add_argument("--expected-installed-backend-manifest-sha256", required=True)
    core_reattest_parser.add_argument("--expected-plan-sha256")
    core_reattest_parser.add_argument("--confirm")
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
    legacy_admission_parser.add_argument(
        "--require-native-proof", action="store_true",
        help="require a complete persisted Native Generation proof when re-proving a verified generation",
    )
    rollout_proof_parser = generation_subparsers.add_parser("proof-only-refresh")
    rollout_proof_parser.add_argument("--expected-operation-id", required=True)
    rollout_proof_parser.add_argument("--expected-backend-manifest-sha256", required=True)
    rollout_proof_parser.add_argument("--expected-generation-digest", required=True)
    rollout_proof_parser.add_argument("--expected-generation-revision", type=int, required=True)
    rollout_proof_parser.add_argument("--expected-checkpoint-sha256", required=True)
    rollout_proof_parser.add_argument("--lifecycle-lock-fd", type=int, required=True)
    rollout_proof_parser.add_argument("--maintenance-lease-fd", type=int, required=True)
    args = parser.parse_args(arguments)
    # This single owner command validates an inherited exclusive lease itself;
    # acquiring a nested shared lease would deadlock and release its barrier.
    if args.command == "xray" and args.xray_command == "generation" and args.generation_command == "proof-only-refresh":
        from fwrouter_api.services.rollout_native_evidence import refresh_rollout_native_evidence
        result = refresh_rollout_native_evidence(
            expected_operation_id=args.expected_operation_id,
            expected_backend_manifest_sha256=args.expected_backend_manifest_sha256,
            expected_generation_digest=args.expected_generation_digest,
            expected_generation_revision=args.expected_generation_revision,
            expected_checkpoint_sha256=args.expected_checkpoint_sha256,
            lifecycle_lock_fd=args.lifecycle_lock_fd,
            maintenance_lease_fd=args.maintenance_lease_fd,
        )
        print(json.dumps(result, sort_keys=True))
        return 0 if result.get("ok") is True else 1
    if args.command == "core" and args.core_command == "ip-receipt-reattest":
        if args.apply != bool(args.expected_plan_sha256 and args.confirm):
            print(json.dumps({"status": "BLOCKED", "reason": "CORE_IP_RECEIPT_REATTEST_CONFIRMATION_REQUIRED"}))
            return 2
        from fwrouter_api.services.maintenance_lifecycle_admission import (
            MaintenanceAdmissionError,
            core_owner_shared_lease,
        )
        try:
            with core_owner_shared_lease(
                expected_tool_manifest_sha256=args.expected_tool_manifest_sha256,
                expected_installed_backend_manifest_sha256=args.expected_installed_backend_manifest_sha256,
                state_root=DEFAULT_STATE_ROOT,
            ):
                return _run_core_ip_receipt_reattest(args)
        except MaintenanceAdmissionError as exc:
            reason = str(exc)
            if not re.fullmatch(r"CORE_OWNER_[A-Z0-9_]+", reason):
                reason = "CORE_OWNER_ADMISSION_FAILED"
            print(json.dumps({"status": "BLOCKED", "reason": reason}, sort_keys=True))
            return 1
    with runtime_shared_lease(DEFAULT_STATE_ROOT):
        if args.command == "reconcile" and args.reconcile_command == "check":
            return _print_reconcile_check()
        if args.command == "diagnose":
            return _print_diagnose(json_output=bool(args.json_output))
        if args.command == "native-proof":
            return _run_native_proof(args)
        if args.command == "xray" and args.xray_command == "identity-migration":
            return _run_identity_migration(args)
        if args.command == "xray" and args.xray_command == "generation" and args.generation_command == "supersede":
            return _run_checkpoint_supersession(args)
        if args.command == "xray" and args.xray_command == "generation" and args.generation_command == "admit-legacy":
            return _run_legacy_inventory_admission(args)
        parser.print_help()
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
