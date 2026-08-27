from __future__ import annotations
from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import initialize_database


import json
from pathlib import Path

from fwrouter_api.jobs.extended_handlers import register_extended_handlers
from fwrouter_api.jobs.manager import get_default_job_manager
from fwrouter_api.services.external_connections_registry import upsert_external_connection_record
from fwrouter_api.services.live_probe_cache import clear_live_probe_cache
from fwrouter_api.services.modules import (
    ModuleStateError,
    get_module_state,
    set_module_desired_state,
    set_module_lifecycle_mode,
)
from fwrouter_api.services.runtime import get_runtime_summary
from fwrouter_api.services.system_summary import build_system_summary


def _configure_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FWROUTER_STATE_DIR", str(tmp_path / "state"))
    get_settings.cache_clear()
    clear_live_probe_cache()


class _FakeScriptResult:
    def __init__(self, script_id: str, stdout: str, *, returncode: int = 0, stderr: str = "") -> None:
        self.script_id = script_id
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def to_dict(self) -> dict[str, object]:
        return {
            "script_id": self.script_id,
            "argv": ["/usr/bin/tailscale", "status", "--json"],
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "ok": self.ok,
        }


def _register_tailscale_connection() -> None:
    upsert_external_connection_record(
        {
            "connection_id": "tailscale-connection",
            "system_id": "tailscale-connection",
            "label": "Tailscale Connection",
            "connection_type": "external_network_source",
            "runtime_type": "tailscale",
            "integration_mode": "command_probe",
            "refresh_mode": "manual",
            "collector_config": {"script_id": "tailscale_status"},
        }
    )


def test_enable_tailscale_module_syncs_inventory_and_marks_running(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _register_tailscale_connection()
    register_extended_handlers(get_default_job_manager())
    tailscale_payload = {
        "Self": {
            "HostName": "fwrouter-ts",
            "Online": True,
            "BackendState": "Running",
            "TailscaleIPs": ["100.64.0.12"],
        },
        "Peer": {
            "peer-a": {
                "ID": "peer-a",
                "HostName": "peer-a",
                "Online": True,
                "TailscaleIPs": ["100.64.0.21"],
                "UsesExitNode": True,
            }
        },
    }

    monkeypatch.setattr(
        "fwrouter_api.services.subject_inventory.DEFAULT_SCRIPT_RUNNER.run",
        lambda script_id, extra_args=None: _FakeScriptResult(script_id, json.dumps(tailscale_payload)),
    )
    monkeypatch.setattr(
        "fwrouter_api.services.external_ingress.DEFAULT_SCRIPT_RUNNER.run",
        lambda script_id, extra_args=None: _FakeScriptResult(script_id, json.dumps(tailscale_payload)),
    )

    result = set_module_desired_state("tailscale", "enabled", requested_by="pytest", run_now=True)
    module = get_module_state("tailscale")

    assert result["job"] is not None
    assert result["job"]["status"] == "success"
    assert module is not None
    assert module["desired_state"] == "enabled"
    assert module["runtime_state"] == "running"
    assert module["apply_state"] == "clean"
    assert "tailscale_node subjects were synced" in str(module["status_text"])


def test_enable_tailscale_module_marks_degraded_on_probe_failure(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _register_tailscale_connection()
    register_extended_handlers(get_default_job_manager())

    monkeypatch.setattr(
        "fwrouter_api.services.subject_inventory.DEFAULT_SCRIPT_RUNNER.run",
        lambda script_id, extra_args=None: _FakeScriptResult(
            script_id,
            "",
            returncode=1,
            stderr="tailscale unavailable",
        ),
    )
    monkeypatch.setattr(
        "fwrouter_api.services.external_ingress.DEFAULT_SCRIPT_RUNNER.run",
        lambda script_id, extra_args=None: _FakeScriptResult(
            script_id,
            "",
            returncode=1,
            stderr="tailscale unavailable",
        ),
    )

    set_module_desired_state("tailscale", "enabled", requested_by="pytest", run_now=True)
    module = get_module_state("tailscale")
    summary = build_system_summary()

    assert module is not None
    assert module["runtime_state"] == "degraded"
    assert module["apply_state"] == "failed"
    assert module["error_code"] == "TAILSCALE_STATUS_FAILED"
    assert any(
        warning["code"] == "FWROUTER_EXTERNAL_INGRESS_DEGRADED"
        for warning in summary["warnings"]
    )


def test_disable_tailscale_module_marks_control_plane_paused(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    result = set_module_desired_state("tailscale", "disabled", requested_by="pytest", run_now=False)

    assert result["job"] is None
    assert result["module"]["runtime_state"] == "paused"
    assert result["module"]["apply_state"] == "clean"


def test_runtime_summary_exposes_tailscale_probe(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    tailscale_payload = {
        "Self": {
            "HostName": "fwrouter-ts",
            "Online": True,
            "BackendState": "Running",
            "TailscaleIPs": ["100.64.0.12"],
        },
        "Peer": {
            "peer-a": {
                "ID": "peer-a",
                "HostName": "peer-a",
                "Online": True,
                "TailscaleIPs": ["100.64.0.21"],
                "UsesExitNode": True,
            },
            "peer-b": {
                "ID": "peer-b",
                "HostName": "peer-b",
                "Online": False,
                "TailscaleIPs": ["100.64.0.22"],
            },
        },
    }

    monkeypatch.setattr(
        "fwrouter_api.services.external_ingress.DEFAULT_SCRIPT_RUNNER.run",
        lambda script_id, extra_args=None: _FakeScriptResult(script_id, json.dumps(tailscale_payload)),
    )

    summary = get_runtime_summary()

    assert summary["tailscale"]["runtime_state"] == "running"
    assert summary["tailscale"]["details"]["hostname"] == "fwrouter-ts"
    assert summary["tailscale"]["details"]["peers_visible_count"] == 2
    assert summary["tailscale"]["details"]["importable_peers_count"] == 1


def test_tailscale_module_rejects_managed_lifecycle_mode(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    try:
        set_module_lifecycle_mode("tailscale", "managed")
    except ModuleStateError as exc:
        assert "not supported" in str(exc)
    else:
        raise AssertionError("Tailscale must remain an external or disabled module")
