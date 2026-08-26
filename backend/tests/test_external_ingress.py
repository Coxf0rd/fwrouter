from __future__ import annotations

import json

from fwrouter_api.services.external_ingress import (
    external_ingress_clients_from_payload,
    probe_external_ingress_runtime,
)
from fwrouter_api.services.live_probe_cache import clear_live_probe_cache


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
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "ok": self.ok,
        }


def test_external_ingress_mapper_uses_registry_contract() -> None:
    clients = external_ingress_clients_from_payload(
        "tailscale",
        {
            "Peer": {
                "peer-a": {
                    "ID": "peer-a",
                    "HostName": "phone",
                    "Online": True,
                    "TailscaleIPs": ["100.64.0.21"],
                    "UsesExitNode": True,
                },
                "peer-b": {
                    "ID": "peer-b",
                    "Online": False,
                    "TailscaleIPs": ["100.64.0.22"],
                },
            },
        },
    )

    assert clients == [
        {
            "provider": "tailscale",
            "provider_node_id": "peer-a",
            "subject_type": "tailscale_node",
            "subject_id_prefix": "tailscale-node:",
            "stable_key": "peer-a",
            "display_name": "phone",
            "ip_address": "100.64.0.21",
            "user_name": None,
            "online": True,
            "routing_hint": True,
            "import_reason": "routing_hint",
            "source_json": {
                "ID": "peer-a",
                "HostName": "phone",
                "Online": True,
                "TailscaleIPs": ["100.64.0.21"],
                "UsesExitNode": True,
            },
        }
    ]


def test_external_ingress_probe_uses_generic_runtime(monkeypatch) -> None:
    clear_live_probe_cache()
    payload = {
        "Self": {
            "HostName": "fwrouter-ts",
            "Online": True,
            "BackendState": "Running",
            "TailscaleIPs": ["100.64.0.12"],
        },
        "Peer": {
            "peer-a": {
                "ID": "peer-a",
                "HostName": "phone",
                "Online": True,
                "TailscaleIPs": ["100.64.0.21"],
            }
        },
    }
    monkeypatch.setattr(
        "fwrouter_api.services.external_ingress.DEFAULT_SCRIPT_RUNNER.run",
        lambda script_id, extra_args=None: _FakeScriptResult(script_id, json.dumps(payload)),
    )

    result = probe_external_ingress_runtime("tailscale")

    assert result["adapter"] == "external_ingress"
    assert result["provider"] == "tailscale"
    assert result["script_id"] == "tailscale_status"
    assert result["details"]["hostname"] == "fwrouter-ts"
    assert result["details"]["provider_ips"] == ["100.64.0.12"]
