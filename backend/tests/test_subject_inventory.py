from __future__ import annotations
from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import initialize_database


import json
from pathlib import Path

from fwrouter_api.db.connection import db_session
from fwrouter_api.services.subject_inventory import sync_subject_inventory
from fwrouter_api.services.subjects import find_subject_by_ip, list_subjects


def _configure_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FWROUTER_STATE_DIR", str(tmp_path / "state"))
    get_settings.cache_clear()


class _FakeScriptResult:
    def __init__(self, script_id: str, stdout: str, *, ok: bool = True, stderr: str = "") -> None:
        self.script_id = script_id
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = 0 if ok else 1
        self.argv = (script_id,)

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def to_dict(self) -> dict[str, object]:
        return {"script_id": self.script_id, "ok": self.ok}


def test_subject_inventory_sync_imports_docker_and_lan(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    def _fake_run(script_id: str, extra_args=None):
        if script_id == "docker_ps":
            return _FakeScriptResult(
                "docker_ps",
                json.dumps(
                    {
                        "ID": "abc",
                        "Image": "ghcr.io/example/app:latest",
                        "Names": "homeassistant",
                        "Labels": {
                            "com.docker.compose.project": "compose",
                            "com.docker.compose.service": "homeassistant",
                        },
                        "State": "running",
                    }
                ),
            )
        raise AssertionError(script_id)

    monkeypatch.setattr("fwrouter_api.services.subject_inventory._run_script", _fake_run)

    result = sync_subject_inventory(
        requested_by="pytest",
        discover_docker=True,
        lan_clients=[{"mac_address": "AA:BB:CC:DD:EE:FF", "ip_address": "192.168.0.10", "hostname": "phone"}],
    )

    assert result["synced_counts"]["docker"] == 1
    assert result["synced_counts"]["lan"] >= 1
    docker_subjects = list_subjects(subject_type="docker")
    lan_subjects = list_subjects(subject_type="lan")
    assert len(docker_subjects) == 1
    assert any(subject["display_name"] == "phone" for subject in lan_subjects)
    assert docker_subjects[0]["display_name"] == "homeassistant"


def test_find_subject_by_ip_uses_direct_active_detail_lookup(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO subjects (
                subject_id, subject_type, stable_key, display_name, desired_mode,
                runtime_state, is_active, is_deleted, last_seen_at
            ) VALUES
                ('lan:active', 'lan', 'lan:active', 'Active LAN', 'global', 'active', 1, 0, '2026-07-16 10:00:00'),
                ('lan:inactive', 'lan', 'lan:inactive', 'Inactive LAN', 'global', 'inactive', 0, 0, '2026-07-16 11:00:00'),
                ('tailscale:active', 'tailscale', 'tailscale:active', 'Active TS', 'global', 'active', 1, 0, '2026-07-16 12:00:00')
            """
        )
        connection.execute(
            """
            INSERT INTO subject_lan (subject_id, mac_address, ip_address, hostname)
            VALUES
                ('lan:active', 'AA:BB:CC:DD:EE:01', '192.168.0.10', 'active-lan'),
                ('lan:inactive', 'AA:BB:CC:DD:EE:02', '192.168.0.11', 'inactive-lan')
            """
        )
        connection.execute(
            """
            INSERT INTO subject_tailscale (subject_id, node_id, tailscale_ip, hostname, user_name, online)
            VALUES ('tailscale:active', 'node-1', '100.64.0.10', 'active-ts', 'tester', 1)
            """
        )

    lan = find_subject_by_ip("192.168.0.10")
    tailscale = find_subject_by_ip("100.64.0.10")

    assert lan is not None
    assert lan["subject_id"] == "lan:active"
    assert lan["detail"]["hostname"] == "active-lan"
    assert find_subject_by_ip("192.168.0.11") is None
    assert tailscale is not None
    assert tailscale["subject_id"] == "tailscale:active"
    assert tailscale["detail"]["hostname"] == "active-ts"


def test_subject_inventory_sync_imports_docker_with_string_labels(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    def _fake_run(script_id: str, extra_args=None):
        if script_id == "docker_ps":
            return _FakeScriptResult(
                "docker_ps",
                json.dumps(
                    {
                        "ID": "abc",
                        "Image": "ghcr.io/example/app:latest",
                        "Names": "fwrouter-mihomo",
                        "Labels": "com.docker.compose.project=fwrouter-mihomo,com.docker.compose.service=mihomo",
                        "State": "running",
                    }
                ),
            )
        raise AssertionError(script_id)

    monkeypatch.setattr("fwrouter_api.services.subject_inventory._run_script", _fake_run)

    result = sync_subject_inventory(
        requested_by="pytest",
        discover_docker=True,
    )

    assert result["synced_counts"]["docker"] == 1
    docker_subjects = list_subjects(subject_type="docker")
    assert len(docker_subjects) == 1
    assert docker_subjects[0]["subject_id"] == "docker:fwrouter-mihomo:mihomo"
    assert docker_subjects[0]["display_name"] == "mihomo"


def test_subject_inventory_sync_imports_only_routed_tailscale_peers(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    tailscale_payload = {
        "Peer": {
            "peer-1": {
                "ID": "peer-1",
                "HostName": "routed-node",
                "TailscaleIPs": ["100.64.0.2"],
                "Online": True,
                "through_fwrouter": True,
            },
            "peer-2": {
                "ID": "peer-2",
                "HostName": "overlay-only",
                "TailscaleIPs": ["100.64.0.3"],
                "Online": True,
            },
        }
    }

    def _fake_run(script_id: str, extra_args=None):
        if script_id == "docker_ps":
            return _FakeScriptResult("docker_ps", "")
        if script_id == "tailscale_status":
            return _FakeScriptResult("tailscale_status", json.dumps(tailscale_payload))
        raise AssertionError(script_id)

    monkeypatch.setattr("fwrouter_api.services.subject_inventory._run_script", _fake_run)

    result = sync_subject_inventory(
        requested_by="pytest",
        discover_docker=False,
        discover_tailscale=True,
    )

    assert result["synced_counts"]["tailscale_node"] == 1
    subjects = list_subjects(subject_type="tailscale_node")
    assert len(subjects) == 1
    assert subjects[0]["display_name"] == "routed-node"


def test_subject_inventory_sync_preserves_existing_desired_mode(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    sync_subject_inventory(
        requested_by="pytest",
        discover_docker=False,
        lan_clients=[{"mac_address": "AA:BB:CC:DD:EE:FF", "ip_address": "192.168.0.10", "hostname": "phone"}],
    )

    subject = next(
        item
        for item in list_subjects(subject_type="lan")
        if item["subject_id"] == "lan:aa-bb-cc-dd-ee-ff"
    )
    with db_session() as connection:
        connection.execute(
            """
            UPDATE subjects
            SET desired_mode = ?, applied_mode = ?, apply_state = 'clean'
            WHERE subject_id = ?
            """,
            ("selective", "selective", subject["subject_id"]),
        )

    sync_subject_inventory(
        requested_by="pytest",
        discover_docker=False,
        lan_clients=[{"mac_address": "AA:BB:CC:DD:EE:FF", "ip_address": "192.168.0.10", "hostname": "phone"}],
    )

    refreshed = next(
        item
        for item in list_subjects(subject_type="lan")
        if item["subject_id"] == "lan:aa-bb-cc-dd-ee-ff"
    )
    assert refreshed["desired_mode"] == "selective"
    assert refreshed["applied_mode"] == "selective"


def test_subject_inventory_sync_imports_host_services(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    def _fake_run(script_id: str, extra_args=None):
        if script_id == "host_services":
            return _FakeScriptResult(
                "host_services",
                json.dumps(
                    [
                        {
                            "systemd_unit": "nginx.service",
                            "process_name": "Nginx Web Server",
                            "runtime_state": "running",
                            "is_active": True,
                        }
                    ]
                ),
            )
        raise AssertionError(script_id)

    monkeypatch.setattr("fwrouter_api.services.subject_inventory._run_script", _fake_run)

    result = sync_subject_inventory(
        requested_by="pytest",
        discover_docker=False,
        discover_host=True,
    )

    assert result["synced_counts"]["host"] == 1
    subjects = list_subjects(subject_type="host")
    assert len(subjects) == 1
    assert subjects[0]["display_name"] == "Nginx Web Server"


def test_subject_inventory_sync_does_not_mark_host_missing_when_host_discovery_is_disabled(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    def _fake_run(script_id: str, extra_args=None):
        if script_id == "host_services":
            return _FakeScriptResult(
                "host_services",
                json.dumps(
                    [
                        {
                            "systemd_unit": "nginx.service",
                            "process_name": "Nginx Web Server",
                            "runtime_state": "running",
                            "is_active": True,
                        }
                    ]
                ),
            )
        if script_id == "tailscale_status":
            return _FakeScriptResult("tailscale_status", json.dumps({"Peer": {}}))
        raise AssertionError(script_id)

    monkeypatch.setattr("fwrouter_api.services.subject_inventory._run_script", _fake_run)

    initial = sync_subject_inventory(
        requested_by="pytest",
        discover_docker=False,
        discover_host=True,
    )
    assert initial["synced_counts"]["host"] == 1
    assert initial["stale_counts"]["host"] == 0

    follow_up = sync_subject_inventory(
        requested_by="pytest",
        discover_docker=False,
        discover_host=False,
        discover_tailscale=True,
    )

    subjects = list_subjects(subject_type="host")
    assert len(subjects) == 1
    assert subjects[0]["is_active"] is True
    assert subjects[0]["runtime_state"] == "running"
    assert "host" not in follow_up["stale_counts"]
