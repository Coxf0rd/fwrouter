from __future__ import annotations

from pathlib import Path

from fwrouter_api.core.config import get_settings
from fwrouter_api.services.subject_inventory_scheduler import (
    _run_subject_inventory_job,
    start_subject_inventory_scheduler,
    stop_subject_inventory_scheduler,
)


def _configure_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FWROUTER_STATE_DIR", str(tmp_path / "state"))
    get_settings.cache_clear()


class _FakeManager:
    def __init__(self) -> None:
        self.created: list[dict[str, object]] = []
        self.started: list[str] = []

    def create(
        self,
        job_type,
        *,
        lock_key=None,
        requested_by=None,
        input_data=None,
        artifact_dir=None,
    ):
        job = {
            "job_id": "job-1",
            "job_type": job_type,
            "lock_key": lock_key,
            "requested_by": requested_by,
            "input": input_data or {},
        }
        self.created.append(job)
        return job

    def start_job_and_wait(self, job_id, *, timeout_seconds=None):
        self.started.append(job_id)
        return {"job_id": job_id, "status": "success"}


def test_subject_inventory_scheduler_respects_enabled_config(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    monkeypatch.setenv("FWROUTER_SUBJECT_INVENTORY_SCHEDULER_ENABLED", "false")
    get_settings.cache_clear()

    assert start_subject_inventory_scheduler() is False
    stop_subject_inventory_scheduler()


def test_subject_inventory_scheduler_submits_docker_and_host_sync(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    fake_manager = _FakeManager()
    monkeypatch.setattr(
        "fwrouter_api.services.subject_inventory_scheduler.get_default_job_manager",
        lambda: fake_manager,
    )

    _run_subject_inventory_job()

    assert fake_manager.started == ["job-1"]
    assert fake_manager.created[0]["job_type"] == "subject_inventory_sync"
    assert fake_manager.created[0]["lock_key"] == "subject_inventory_sync"
    assert fake_manager.created[0]["requested_by"] == "subject-inventory-scheduler"
    assert fake_manager.created[0]["input"] == {
        "discover_docker": True,
        "discover_host": True,
        "discover_external_ingress_providers": [],
        "discover_xray": False,
    }
