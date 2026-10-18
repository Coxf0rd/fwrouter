from __future__ import annotations

from typing import Any

from fwrouter_api.services.dataplane_global import read_effective_rules_artifact
from fwrouter_api.services.dataplane_nft_artifacts import get_dataplane_artifact_paths as get_dataplane_artifact_paths, promote_last_good as promote_last_good
from fwrouter_api.services.dataplane_nft_artifacts import write_candidate_artifacts as _write_candidate_artifacts
from fwrouter_api.services.dataplane_nft_constants import (
    CONTROL_PLANE_INPUT_PORTS as CONTROL_PLANE_INPUT_PORTS,
    OWNED_TABLE as OWNED_TABLE,
    REQUIRED_CHAINS as REQUIRED_CHAINS,
    ROOT_UID as ROOT_UID,
    STATIC_SECURE_DNS_BYPASS_IPV4 as STATIC_SECURE_DNS_BYPASS_IPV4,
    utc_timestamp as utc_timestamp,
)
from fwrouter_api.services.dataplane_nft_render import render_owned_table_candidate as _render_owned_table_candidate


def render_owned_table_candidate(manifest: dict[str, Any] | None = None) -> str:
    return _render_owned_table_candidate(
        manifest,
        rules_effective_loader=read_effective_rules_artifact,
    )


def write_candidate_artifacts(
    *,
    job_id: str,
    apply_id: str,
    manifest: dict[str, Any],
) -> dict[str, str]:
    return _write_candidate_artifacts(
        job_id=job_id,
        apply_id=apply_id,
        manifest=manifest,
        renderer=render_owned_table_candidate,
    )
