from __future__ import annotations

from pathlib import Path
from typing import Any

from fwrouter_api.core.config import get_settings
from fwrouter_api.services.artifacts import atomic_copy_file, atomic_write_json, atomic_write_text
from fwrouter_api.services.dataplane_nft_render import render_owned_table_candidate


def get_dataplane_artifact_paths(*, job_id: str, apply_id: str) -> dict[str, str]:
    paths = get_settings().paths
    generated_dir = paths.generated_dir / "dataplane"
    last_good_dir = paths.state_dir / "last-good" / "dataplane"
    snapshot_dir = last_good_dir / "snapshots" / apply_id
    job_dir = paths.jobs_dir / job_id / "dataplane"

    return {
        "generated_dir": str(generated_dir),
        "last_good_dir": str(last_good_dir),
        "snapshot_dir": str(snapshot_dir),
        "job_dataplane_dir": str(job_dir),
        "candidate_nft_path": str(generated_dir / "candidate.nft"),
        "candidate_manifest_path": str(generated_dir / "candidate-manifest.json"),
        "current_nft_path": str(generated_dir / "current.nft"),
        "current_manifest_path": str(generated_dir / "current-manifest.json"),
        "applied_nft_path": str(generated_dir / "applied.nft"),
        "applied_manifest_path": str(generated_dir / "applied-manifest.json"),
        "last_good_nft_path": str(last_good_dir / "last-good.nft"),
        "last_good_manifest_path": str(last_good_dir / "last-good-manifest.json"),
        "snapshot_before_nft_path": str(snapshot_dir / "fwrouter_v2.before.nft"),
        "snapshot_state_path": str(snapshot_dir / "snapshot-state.json"),
        "snapshot_candidate_nft_path": str(snapshot_dir / "candidate.nft"),
        "snapshot_manifest_path": str(snapshot_dir / "manifest.json"),
        "job_candidate_nft_path": str(job_dir / "candidate.nft"),
        "job_candidate_manifest_path": str(job_dir / "candidate-manifest.json"),
        "job_result_path": str(job_dir / "result.json"),
        "job_check_stdout_path": str(job_dir / "check.stdout"),
        "job_check_stderr_path": str(job_dir / "check.stderr"),
        "job_apply_stdout_path": str(job_dir / "apply.stdout"),
        "job_apply_stderr_path": str(job_dir / "apply.stderr"),
        "job_rollback_stdout_path": str(job_dir / "rollback.stdout"),
        "job_rollback_stderr_path": str(job_dir / "rollback.stderr"),
    }


def write_candidate_artifacts(
    *,
    job_id: str,
    apply_id: str,
    manifest: dict[str, Any],
    renderer: Any = render_owned_table_candidate,
) -> dict[str, str]:
    artifact_paths = get_dataplane_artifact_paths(job_id=job_id, apply_id=apply_id)
    candidate_text = renderer(manifest)

    atomic_write_text(Path(artifact_paths["candidate_nft_path"]), candidate_text)
    atomic_write_text(Path(artifact_paths["job_candidate_nft_path"]), candidate_text)
    atomic_write_text(Path(artifact_paths["snapshot_candidate_nft_path"]), candidate_text)

    candidate_manifest_path = Path(artifact_paths["candidate_manifest_path"])
    atomic_write_json(candidate_manifest_path, manifest)
    atomic_copy_file(candidate_manifest_path, Path(artifact_paths["job_candidate_manifest_path"]))
    atomic_copy_file(candidate_manifest_path, Path(artifact_paths["snapshot_manifest_path"]))

    return artifact_paths


def promote_last_good(
    *,
    manifest: dict[str, Any],
    artifact_paths: dict[str, str],
) -> None:
    candidate_text = Path(artifact_paths["candidate_nft_path"]).read_text(encoding="utf-8")
    atomic_write_text(Path(artifact_paths["current_nft_path"]), candidate_text)
    atomic_write_text(Path(artifact_paths["applied_nft_path"]), candidate_text)
    atomic_write_text(Path(artifact_paths["last_good_nft_path"]), candidate_text)
    applied_manifest_path = Path(artifact_paths["applied_manifest_path"])
    atomic_write_json(applied_manifest_path, manifest)
    atomic_copy_file(applied_manifest_path, Path(artifact_paths["last_good_manifest_path"]))

    current_manifest_path = artifact_paths.get("current_manifest_path")
    if current_manifest_path:
        atomic_copy_file(applied_manifest_path, Path(current_manifest_path))
