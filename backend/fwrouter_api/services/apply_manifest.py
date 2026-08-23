from __future__ import annotations

import time
from typing import Any

from fwrouter_api.adapters.dataplane import DataplaneOperation
from fwrouter_api.services.server_layout import SERVER_LAYOUT_CONTRACT_VERSION


def _runtime_mode_from_manifest(manifest: dict[str, Any]) -> str:
    routing = manifest.get("routing_global_state")
    if not isinstance(routing, dict):
        return "direct"
    return str(routing.get("desired_mode") or routing.get("applied_mode") or "direct")


def _manifest_requests_core_bypass(manifest: dict[str, Any]) -> bool:
    extra = manifest.get("extra")
    if not isinstance(extra, dict):
        return False
    core_bypass = extra.get("core_bypass")
    return isinstance(core_bypass, dict) and bool(core_bypass.get("enabled"))


def _render_failure_result(
    *,
    plan: dict[str, Any],
    stage: str,
    error_code: str,
    error_message: str,
    manifest_state: dict[str, Any] | None,
    memory_snapshot: dict[str, Any],
) -> dict[str, Any]:
    return {
        "ok": False,
        "apply_id": plan["apply_id"],
        "job_id": plan["job_id"],
        "mode": plan["mode"],
        "reason": plan["reason"],
        "dataplane_capability": "nft_owned_table",
        "enforcement_level": "unknown",
        "traffic_enforcement_guaranteed": False,
        "supported_modes": {},
        "missing_runtime_requirements": [],
        "stage": stage,
        "manifest": {
            "summary": {
                "render_failed": True,
                "manifest_state_provided": manifest_state is not None,
            },
            "paths": {},
            "contract_version": SERVER_LAYOUT_CONTRACT_VERSION,
            "owned_table": None,
            "required_chains": [],
            "generated_at": None,
            "profile": None,
        },
        "scoped_egress": {},
        "preflight": {},
        "dataplane": {
            "ok": False,
            "operation": DataplaneOperation.CHECK.value,
            "message": error_message,
            "error_code": error_code,
            "error_message": error_message,
            "details": {
                "stage": stage,
                "memory": memory_snapshot,
            },
        },
        "rollback": None,
    }


def _materialize_manifest(
    *,
    prebuilt_manifest: dict[str, Any],
    plan_id: str,
    reason: str,
    input_data: dict[str, Any] | None,
) -> dict[str, Any]:
    manifest = dict(prebuilt_manifest)
    manifest["plan_id"] = plan_id
    manifest["reason"] = reason
    manifest["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    manifest["input"] = input_data or {}
    return manifest
