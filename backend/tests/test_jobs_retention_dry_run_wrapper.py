from __future__ import annotations

from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path


def _load_wrapper():
    script_path = Path(__file__).resolve().parents[2] / "host" / "sbin" / "fwrouter-jobs-retention-dry-run"
    loader = SourceFileLoader("fwrouter_jobs_retention_dry_run", str(script_path))
    spec = spec_from_loader(loader.name, loader)
    assert spec is not None
    module = module_from_spec(spec)
    loader.exec_module(module)
    return module


def _response(*, job_input: dict, result: dict, status: str = "success", ok: bool = True) -> dict:
    return {
        "ok": ok,
        "data": {
            "job": {
                "status": status,
                "input": job_input,
                "result": result,
            },
        },
        "error": None,
    }


def test_retention_dry_run_wrapper_accepts_truncated_successful_dry_run(capsys) -> None:
    wrapper = _load_wrapper()
    payload = _response(
        job_input={"dry_run": True},
        result={
            "__truncated__": True,
            "original_bytes": 1_314_123,
            "max_bytes": 262_144,
            "summary": {"_extra_keys": ["handler", "retention"]},
        },
    )

    assert wrapper.validate_response(payload) == 0
    captured = capsys.readouterr()
    assert "verified dry-run from job input" in captured.err


def test_retention_dry_run_wrapper_rejects_missing_retention_without_truncation() -> None:
    wrapper = _load_wrapper()
    payload = _response(job_input={"dry_run": True}, result={"handler": "jobs_retention_cleanup"})

    assert wrapper.validate_response(payload) == 1


def test_retention_dry_run_wrapper_rejects_non_dry_run_input() -> None:
    wrapper = _load_wrapper()
    payload = _response(
        job_input={"dry_run": False},
        result={
            "retention": {
                "dry_run": True,
                "deleted_jobs_count": 0,
                "deleted_artifact_dirs_count": 0,
            },
        },
    )

    assert wrapper.validate_response(payload) == 1


def test_retention_dry_run_wrapper_rejects_deleted_jobs_in_visible_result() -> None:
    wrapper = _load_wrapper()
    payload = _response(
        job_input={"dry_run": True},
        result={
            "retention": {
                "dry_run": True,
                "deleted_jobs_count": 1,
                "deleted_artifact_dirs_count": 0,
            },
        },
    )

    assert wrapper.validate_response(payload) == 1
