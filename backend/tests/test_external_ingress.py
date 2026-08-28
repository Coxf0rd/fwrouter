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
        connection_id="connection-a",
    )

    assert clients == [
        {
            "provider": "tailscale",
            "connection_id": "connection-a",
            "provider_node_id": "peer-a",
            "subject_type": "external_network_client",
            "subject_id_prefix": "connection-a:",
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


def test_external_ingress_mapper_requires_connection_id() -> None:
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
            },
        },
    )

    assert clients == []


def test_external_ingress_probe_requires_connection_id(monkeypatch) -> None:
    clear_live_probe_cache()

    def _fake_run(script_id: str, extra_args=None):
        raise AssertionError(script_id)

    monkeypatch.setattr("fwrouter_api.services.external_ingress.DEFAULT_SCRIPT_RUNNER.run", _fake_run)

    result = probe_external_ingress_runtime("tailscale")

    assert result["ok"] is False
    assert result["connection_id"] is None
    assert result["runtime_state"] == "not_configured"
    assert result["error_code"] == "EXTERNAL_INGRESS_CONNECTION_REQUIRED"


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

    result = probe_external_ingress_runtime("tailscale", connection_id="connection-a")

    assert result["adapter"] == "external_ingress"
    assert result["provider"] == "tailscale"
    assert result["connection_id"] == "connection-a"
    assert result["script_id"] == "tailscale_status"
    assert result["details"]["hostname"] == "fwrouter-ts"
    assert result["details"]["provider_ips"] == ["100.64.0.12"]


def _generic_provider_contract(provider: str) -> dict[str, object] | None:
    if provider not in {"provider-a", "provider-b"}:
        return None
    return {
        "provider": provider,
        "module_concept": provider,
        "subject_type": "external_network_client",
        "subject_role": "external_network_source",
        "identity_kind": "provider_ip",
        "status_mapping": {
            "peer_collection_fields": ["peers"],
            "peer_identity_fields": ["id"],
            "peer_address_fields": ["ip"],
            "peer_name_fields": ["name"],
            "peer_user_fields": ["user"],
            "peer_routing_hint_fields": ["routed"],
            "peer_online_field": "online",
            "self_field": "self",
            "self_hostname_fields": ["hostname"],
            "self_address_fields": ["ips"],
            "self_online_field": "online",
            "self_state_field": "state",
        },
        "runtime_probe": {
            "script_id": f"{provider}_status",
            "cache_key": f"external_ingress.runtime.{provider}",
        },
    }


def test_external_ingress_mapper_can_scope_subjects_by_connection_id(monkeypatch) -> None:
    monkeypatch.setattr(
        "fwrouter_api.services.external_ingress.external_ingress_contract",
        _generic_provider_contract,
    )
    payload = {
        "peers": [
            {
                "id": "node-a",
                "name": "Node A",
                "online": True,
                "ip": "198.18.0.10",
                "routed": True,
            }
        ],
    }

    first = external_ingress_clients_from_payload(
        "provider-a",
        payload,
        connection_id="connection-a",
    )
    second = external_ingress_clients_from_payload(
        "provider-a",
        payload,
        connection_id="connection-b",
    )

    assert first[0]["subject_id_prefix"] == "connection-a:"
    assert second[0]["subject_id_prefix"] == "connection-b:"
    assert first[0]["connection_id"] == "connection-a"
    assert second[0]["connection_id"] == "connection-b"


def test_external_ingress_probe_cache_is_scoped_by_connection_id(monkeypatch) -> None:
    clear_live_probe_cache()
    monkeypatch.setattr(
        "fwrouter_api.services.external_ingress.external_ingress_contract",
        _generic_provider_contract,
    )
    calls: list[list[str]] = []

    def fake_run(script_id, extra_args=None):
        calls.append([script_id, *(extra_args or [])])
        return _FakeScriptResult(
            script_id,
            json.dumps(
                {
                    "self": {
                        "hostname": extra_args[0] if extra_args else "default",
                        "online": True,
                        "state": "running",
                        "ips": ["198.18.0.1"],
                    },
                    "peers": [],
                }
            ),
        )

    monkeypatch.setattr("fwrouter_api.services.external_ingress.DEFAULT_SCRIPT_RUNNER.run", fake_run)

    first = probe_external_ingress_runtime(
        "provider-a",
        connection_id="connection-a",
        collector_config={"extra_args": ["home"]},
    )
    second = probe_external_ingress_runtime(
        "provider-a",
        connection_id="connection-b",
        collector_config={"extra_args": ["lab"]},
    )

    assert first["connection_id"] == "connection-a"
    assert second["connection_id"] == "connection-b"
    assert first["details"]["hostname"] == "home"
    assert second["details"]["hostname"] == "lab"
    assert calls == [["provider-a_status", "home"], ["provider-a_status", "lab"]]
