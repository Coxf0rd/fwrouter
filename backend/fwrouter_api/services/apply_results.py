from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from fwrouter_api.db.connection import db_session
from fwrouter_api.services.artifacts import atomic_write_json, write_job_json_artifact
from fwrouter_api.services.logs import write_operational_log
from fwrouter_api.services.runtime_prewarm import prime_runtime_read_models_async as _prime_runtime_read_models_async


def build_apply_result(
    *,
    plan: dict[str, Any],
    mode: Any,
    reason: str,
    manifest: dict[str, Any],
    manifest_paths: dict[str, Any],
    result_runtime_enforcement: dict[str, Any],
    stage: str,
    preflight: dict[str, Any],
    operation_result: Any,
    rollback_result: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "ok": operation_result.ok,
        "apply_id": plan["apply_id"],
        "job_id": plan["job_id"],
        "mode": mode.value,
        "reason": reason,
        "dataplane_capability": result_runtime_enforcement["dataplane_capability"],
        "enforcement_level": result_runtime_enforcement["enforcement_level"],
        "traffic_enforcement_guaranteed": result_runtime_enforcement["traffic_enforcement_guaranteed"],
        "supported_modes": result_runtime_enforcement.get("supported_modes", {}),
        "missing_runtime_requirements": result_runtime_enforcement.get("missing_runtime_requirements", []),
        "stage": stage,
        "manifest": {
            "summary": manifest["summary"],
            "paths": manifest_paths,
            "contract_version": manifest["contract_version"],
            "owned_table": manifest["owned_table"],
            "required_chains": manifest["required_chains"],
            "generated_at": manifest["generated_at"],
            "profile": manifest.get("dataplane_profile"),
        },
        "scoped_egress": manifest.get("scoped_egress", {}),
        "preflight": preflight,
        "dataplane": {
            "ok": operation_result.ok,
            "operation": operation_result.operation.value,
            "message": operation_result.message,
            "error_code": operation_result.error_code,
            "error_message": operation_result.error_message,
            "details": operation_result.details,
        },
        "rollback": rollback_result,
    }


def persist_apply_result(
    *,
    job_id: str,
    result_manifest_path: Path,
    result: dict[str, Any],
    plan: dict[str, Any],
    mode: Any,
    reason: str,
    manifest: dict[str, Any],
    manifest_paths: dict[str, Any],
    result_runtime_enforcement: dict[str, Any],
    rollback_result: dict[str, Any] | None,
) -> None:
    write_job_json_artifact(job_id, "dataplane/result.json", result)
    atomic_write_json(result_manifest_path, result)

    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO apply_versions (
                apply_id,
                job_id,
                manifest_path,
                artifact_dir,
                promoted_at,
                status,
                summary_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(apply_id) DO UPDATE SET
                manifest_path = excluded.manifest_path,
                artifact_dir = excluded.artifact_dir,
                promoted_at = excluded.promoted_at,
                status = excluded.status,
                summary_json = excluded.summary_json
            """,
            (
                plan["apply_id"],
                job_id,
                manifest_paths["versioned_manifest_path"],
                plan["artifacts"]["artifact_dir"],
                manifest["generated_at"] if result["ok"] and mode.value == "apply" else None,
                (
                    "generated"
                    if mode.value == "dry_run"
                    else (
                        "applied"
                        if result["ok"]
                        else ("rolled_back" if rollback_result and rollback_result["ok"] else "failed")
                    )
                ),
                json.dumps(
                    {
                        "mode": mode.value,
                        "reason": reason,
                        "path_counts": manifest["summary"]["path_counts"],
                        "dataplane_capability": result_runtime_enforcement["dataplane_capability"],
                        "enforcement_level": result_runtime_enforcement["enforcement_level"],
                        "owned_table": manifest["owned_table"],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            ),
        )


def write_apply_result_log(
    *,
    result: dict[str, Any],
    mode: Any,
    reason: str,
    plan: dict[str, Any],
    manifest: dict[str, Any],
    stage: str,
) -> None:
    if result["ok"]:
        if mode.value == "apply":
            facade = sys.modules.get("fwrouter_api.services.apply")
            prime_runtime_read_models_async = getattr(
                facade,
                "prime_runtime_read_models_async",
                _prime_runtime_read_models_async,
            )
            prime_runtime_read_models_async(
                include_global_profiles=reason not in {"set_global_mode", "set_selective_default"}
            )
        write_operational_log(
            event_type="apply_dry_run_completed"
            if mode.value == "dry_run"
            else "apply_completed",
            message="Apply pipeline dry-run completed."
            if mode.value == "dry_run"
            else "Apply pipeline completed for the FWRouter-owned nftables table.",
            details={
                "job_id": plan["job_id"],
                "apply_id": plan["apply_id"],
                "mode": mode.value,
                "reason": reason,
                "owned_table": manifest["owned_table"],
            },
        )
        return

    write_operational_log(
        event_type="apply_failed",
        level="warning",
        message="Apply pipeline failed in the FWRouter-owned nftables contour.",
        details={
            "job_id": plan["job_id"],
            "apply_id": plan["apply_id"],
            "mode": mode.value,
            "reason": reason,
            "stage": stage,
            "owned_table": manifest["owned_table"],
            "error_code": result["dataplane"]["error_code"],
            "error_message": result["dataplane"]["error_message"],
        },
    )
