from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any
from uuid import uuid4

from fwrouter_api.adapters.dataplane import DataplaneOperation
from fwrouter_api.core.config import get_settings
from fwrouter_api.services.artifacts import build_artifact_summary
from fwrouter_api.services.dataplane_status import get_dataplane_capability
from fwrouter_api.services.jobs import get_job
from fwrouter_api.services.server_layout import SERVER_LAYOUT_CONTRACT_VERSION


class ApplyMode(str, Enum):
    DRY_RUN = "dry_run"
    APPLY = "apply"


class ApplyPhaseTimeoutError(TimeoutError):
    """Raised when one bounded apply phase exceeds its configured timeout."""


class ApplyJobAbortedError(RuntimeError):
    """Raised when the job is no longer active while apply side effects are in flight."""


def build_apply_plan(
    *,
    job_id: str,
    reason: str,
    mode: ApplyMode = ApplyMode.DRY_RUN,
    input_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build an apply plan DTO without changing runtime state."""

    apply_id = str(uuid4())
    artifacts = build_artifact_summary(job_id)

    return {
        "apply_id": apply_id,
        "job_id": job_id,
        "reason": reason,
        "mode": mode.value,
        "input": input_data or {},
        "artifacts": artifacts,
        "dataplane": {
            "operation": DataplaneOperation.CHECK.value,
            "adapter": "nft-owned-table",
            "contract_version": SERVER_LAYOUT_CONTRACT_VERSION,
            "dataplane_capability": get_dataplane_capability(),
        },
    }


def _last_good_manifest_path() -> Path:
    return get_settings().paths.generated_dir / "dataplane" / "last-good-manifest.json"


def _result_manifest_path() -> Path:
    return get_settings().paths.generated_dir / "dataplane" / "last-result.json"


def _ensure_job_context(job_id: str) -> None:
    if get_job(job_id) is None:
        raise ValueError(
            "Apply pipeline requires an existing jobs row before transaction start: "
            f"{job_id}"
        )
