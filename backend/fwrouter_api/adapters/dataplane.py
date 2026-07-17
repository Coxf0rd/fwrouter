from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from fwrouter_api.adapters.scripts import (
    DEFAULT_SCRIPT_RUNNER,
    ScriptResult,
    ScriptRunner,
    ScriptRunnerError,
)
from fwrouter_api.services.artifacts import write_job_text_artifact
from fwrouter_api.services.dataplane_nft import OWNED_TABLE, REQUIRED_CHAINS
from fwrouter_api.services.dataplane_status import build_runtime_enforcement_state


class DataplaneOperation(str, Enum):
    CHECK = "check"
    APPLY = "apply"
    ROLLBACK = "rollback"


@dataclass(frozen=True)
class DataplanePlan:
    """Rendered dataplane plan metadata."""

    plan_id: str
    operation: DataplaneOperation
    generated_path: str | None = None
    manifest_path: str | None = None
    rollback_path: str | None = None
    artifact_paths: dict[str, str] = field(default_factory=dict)
    contract_version: str = "2026-05-07.control-plane"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DataplaneResult:
    """Result returned by a dataplane adapter."""

    ok: bool
    operation: DataplaneOperation
    message: str
    details: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None


class DataplaneAdapter:
    """Base interface for FWRouter-owned dataplane operations."""

    def check(self, plan: DataplanePlan) -> DataplaneResult:
        raise NotImplementedError

    def apply(self, plan: DataplanePlan) -> DataplaneResult:
        raise NotImplementedError

    def rollback(self, plan: DataplanePlan) -> DataplaneResult:
        raise NotImplementedError


class NftOwnedTableAdapter(DataplaneAdapter):
    """Real Wave 2.1 adapter for the FWRouter-owned nftables table only."""

    def __init__(self, runner: ScriptRunner | None = None) -> None:
        self._runner = runner or DEFAULT_SCRIPT_RUNNER

    def check(self, plan: DataplanePlan) -> DataplaneResult:
        extra_args = []
        if plan.generated_path:
            extra_args.append(plan.generated_path)
        extra_args.append(str(plan.manifest_path or ""))
        return self._run_operation(
            script_id="dataplane_check",
            operation=DataplaneOperation.CHECK,
            plan=plan,
            extra_args=extra_args,
            stdout_name="dataplane/check.stdout",
            stderr_name="dataplane/check.stderr",
        )

    def apply(self, plan: DataplanePlan) -> DataplaneResult:
        extra_args = [
            str(plan.generated_path or ""),
            str(plan.manifest_path or ""),
            str(plan.artifact_paths.get("snapshot_before_nft_path", "")),
            str(plan.artifact_paths.get("snapshot_state_path", "")),
        ]
        return self._run_operation(
            script_id="dataplane_apply",
            operation=DataplaneOperation.APPLY,
            plan=plan,
            extra_args=extra_args,
            stdout_name="dataplane/apply.stdout",
            stderr_name="dataplane/apply.stderr",
        )

    def rollback(self, plan: DataplanePlan) -> DataplaneResult:
        extra_args = [
            str(plan.artifact_paths.get("snapshot_before_nft_path", "")),
            str(plan.artifact_paths.get("snapshot_state_path", "")),
            str(plan.manifest_path or ""),
        ]
        return self._run_operation(
            script_id="dataplane_rollback",
            operation=DataplaneOperation.ROLLBACK,
            plan=plan,
            extra_args=extra_args,
            stdout_name="dataplane/rollback.stdout",
            stderr_name="dataplane/rollback.stderr",
        )

    def _run_operation(
        self,
        *,
        script_id: str,
        operation: DataplaneOperation,
        plan: DataplanePlan,
        extra_args: list[str],
        stdout_name: str,
        stderr_name: str,
    ) -> DataplaneResult:
        job_id = str(plan.metadata.get("job_id") or "")

        try:
            script_result = self._runner.run(script_id, extra_args=extra_args)
        except ScriptRunnerError as exc:
            if job_id:
                write_job_text_artifact(job_id, stdout_name, "")
                write_job_text_artifact(job_id, stderr_name, str(exc))
            return DataplaneResult(
                ok=False,
                operation=operation,
                message="nftables tooling is not available.",
                details=self._base_details(plan, error_stage=operation.value),
                error_code="NFT_NOT_AVAILABLE",
                error_message=str(exc),
            )

        if job_id:
            write_job_text_artifact(job_id, stdout_name, script_result.stdout)
            write_job_text_artifact(job_id, stderr_name, script_result.stderr)

        return self._result_from_script(script_result=script_result, operation=operation, plan=plan)

    def _result_from_script(
        self,
        *,
        script_result: ScriptResult,
        operation: DataplaneOperation,
        plan: DataplanePlan,
    ) -> DataplaneResult:
        payload = self._parse_payload(script_result.stdout)
        if script_result.returncode == 124:
            return DataplaneResult(
                ok=False,
                operation=operation,
                message="Dataplane script timed out.",
                details=self._base_details(
                    plan,
                    script_result=script_result,
                    error_stage=operation.value,
                ),
                error_code=f"DATAPLANE_{operation.value.upper()}_TIMEOUT",
                error_message=script_result.stderr or "Dataplane operation timed out.",
            )
        if payload is None:
            return DataplaneResult(
                ok=False,
                operation=operation,
                message="Dataplane script returned invalid JSON.",
                details=self._base_details(plan, script_result=script_result, error_stage=operation.value),
                error_code="DATAPLANE_SCRIPT_INVALID_JSON",
                error_message="Dataplane script did not return a valid JSON object.",
            )

        details = {
            **self._base_details(plan, script_result=script_result, error_stage=str(payload.get("stage") or operation.value)),
            **payload,
        }
        ok = bool(payload.get("ok")) and script_result.ok

        return DataplaneResult(
            ok=ok,
            operation=operation,
            message=str(payload.get("message") or f"Dataplane {operation.value} completed."),
            details=details,
            error_code=None if ok else str(payload.get("error_code") or f"NFT_{operation.value.upper()}_FAILED"),
            error_message=None if ok else str(payload.get("error_message") or payload.get("message") or "Dataplane operation failed."),
        )

    @staticmethod
    def _parse_payload(stdout: str) -> dict[str, Any] | None:
        try:
            payload = json.loads(stdout or "{}")
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        return payload

    @staticmethod
    def _base_details(
        plan: DataplanePlan,
        *,
        script_result: ScriptResult | None = None,
        error_stage: str,
    ) -> dict[str, Any]:
        details: dict[str, Any] = {
            "adapter": "nft-owned-table",
            "owned_table": OWNED_TABLE,
            "required_chains": {chain: False for chain in REQUIRED_CHAINS},
            "candidate_path": plan.generated_path,
            "manifest_path": plan.manifest_path,
            "artifact_paths": plan.artifact_paths,
            "error_stage": error_stage,
            **build_runtime_enforcement_state(),
        }
        if script_result is not None:
            details["script"] = script_result.to_dict()
        return details


DEFAULT_DATAPLANE_ADAPTER = NftOwnedTableAdapter()
