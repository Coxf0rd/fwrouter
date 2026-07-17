from __future__ import annotations
from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import initialize_database


import json
from pathlib import Path

from fwrouter_api.jobs.extended_handlers import register_extended_handlers
from fwrouter_api.jobs.manager import get_default_job_manager
from fwrouter_api.services.live_probe_cache import clear_live_probe_cache
from fwrouter_api.services.modules import get_module_state, set_module_desired_state
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


def test_enable_tailscale_module_syncs_inventory_and_marks_running(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
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
        "fwrouter_api.services.tailscale.DEFAULT_SCRIPT_RUNNER.run",
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
        "fwrouter_api.services.tailscale.DEFAULT_SCRIPT_RUNNER.run",
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
    assert any(warning["code"] == "FWROUTER_TAILSCALE_DEGRADED" for warning in summary["warnings"])


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
        "fwrouter_api.services.tailscale.DEFAULT_SCRIPT_RUNNER.run",
        lambda script_id, extra_args=None: _FakeScriptResult(script_id, json.dumps(tailscale_payload)),
    )

    summary = get_runtime_summary()

    assert summary["tailscale"]["runtime_state"] == "running"
    assert summary["tailscale"]["details"]["hostname"] == "fwrouter-ts"
    assert summary["tailscale"]["details"]["peers_visible_count"] == 2
    assert summary["tailscale"]["details"]["importable_peers_count"] == 1


def test_tailscale_module_action_updates_runtime_state(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    class _ActionResult(_FakeScriptResult):
        def __init__(self, script_id: str, stdout: str = "") -> None:
            super().__init__(script_id, stdout)

    def _fake_run(script_id: str, extra_args=None):
        if script_id == "tailscale_restart":
            return _ActionResult("tailscale_restart")
        if script_id == "tailscale_status":
            return _ActionResult(
                "tailscale_status",
                json.dumps(
                    {
                        "Self": {
                            "HostName": "fwrouter-ts",
                            "Online": True,
                            "BackendState": "Running",
                            "TailscaleIPs": ["100.64.0.12"],
                        }
                    }
                ),
            )
        raise AssertionError(script_id)

    monkeypatch.setattr("fwrouter_api.services.tailscale.DEFAULT_SCRIPT_RUNNER.run", _fake_run)

    from fwrouter_api.services.modules import run_module_action

    result = run_module_action("tailscale", "restart", requested_by="pytest")

    assert result["action_result"]["ok"] is True
    assert result["module"]["runtime_state"] == "running"
