from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from fwrouter_api.core.config import get_settings


def _json_dumps(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def atomic_write_text(path: Path, text: str) -> None:
    """Atomically write text to a file in the same filesystem."""

    path.parent.mkdir(parents=True, exist_ok=True)

    with NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as tmp:
        tmp.write(text)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_path = Path(tmp.name)

    tmp_path.replace(path)


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """Atomically write a JSON object."""

    atomic_write_text(path, _json_dumps(data))


def atomic_copy_file(source: Path, destination: Path) -> None:
    """Atomically copy a file within the same filesystem tree."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        "wb",
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
        delete=False,
    ) as tmp:
        with source.open("rb") as src:
            shutil.copyfileobj(src, tmp)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_path = Path(tmp.name)

    tmp_path.replace(destination)


def get_job_artifact_dir(job_id: str) -> Path:
    """Return artifact directory for a job."""

    return get_settings().paths.jobs_dir / job_id


def ensure_job_artifact_dir(job_id: str) -> Path:
    """Create and return artifact directory for a job."""

    artifact_dir = get_job_artifact_dir(job_id)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    return artifact_dir


def _resolve_job_artifact_path(job_id: str, name: str) -> Path:
    if name in {"", ".", ".."}:
        raise ValueError(f"Invalid artifact name: {name}")

    path = ensure_job_artifact_dir(job_id) / Path(name)
    if any(part in {"", ".", ".."} for part in path.relative_to(get_job_artifact_dir(job_id)).parts):
        raise ValueError(f"Invalid artifact name: {name}")

    return path


def write_job_json_artifact(
    job_id: str,
    name: str,
    data: dict[str, Any],
) -> Path:
    """Write one JSON artifact under /var/lib/fwrouter-v2/jobs/<job_id>/."""

    path = _resolve_job_artifact_path(job_id, name)

    if path.suffix != ".json":
        path = path.with_suffix(".json")

    atomic_write_json(path, data)
    return path


def write_job_text_artifact(
    job_id: str,
    name: str,
    text: str,
) -> Path:
    """Write one text artifact under /var/lib/fwrouter-v2/jobs/<job_id>/."""

    path = _resolve_job_artifact_path(job_id, name)

    atomic_write_text(path, text)
    return path


def build_artifact_summary(job_id: str) -> dict[str, Any]:
    """Return filesystem paths used by one job, without creating files."""

    settings = get_settings()

    return {
        "job_id": job_id,
        "artifact_dir": str(get_job_artifact_dir(job_id)),
        "generated_root": str(settings.paths.generated_dir),
        "jobs_root": str(settings.paths.jobs_dir),
    }
