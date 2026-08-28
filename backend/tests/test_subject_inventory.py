from __future__ import annotations
from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import initialize_database


import json
from pathlib import Path

from fwrouter_api.db.connection import db_session
from fwrouter_api.services.external_connections_registry import upsert_external_connection_record
from fwrouter_api.services.subject_inventory import sync_subject_inventory
from fwrouter_api.services.subjects import find_subject_by_ip, list_subjects, update_subject_alias


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
        if script_id in {"docker_inventory", "docker_ps"}:
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


def test_subject_inventory_sync_preserves_manual_lan_alias(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    sync_subject_inventory(
        requested_by="pytest",
        discover_docker=False,
        lan_clients=[{"mac_address": "AA:BB:CC:DD:EE:FF", "ip_address": "192.168.0.10", "hostname": "phone"}],
    )
    assert update_subject_alias("lan:aa-bb-cc-dd-ee-ff", "My phone") is not None

    sync_subject_inventory(
        requested_by="pytest",
        discover_docker=False,
        lan_clients=[{"mac_address": "AA:BB:CC:DD:EE:FF", "ip_address": "192.168.0.10", "hostname": "dhcp-phone"}],
    )

    subjects = list_subjects(subject_type="lan")
    subject = next(item for item in subjects if item["subject_id"] == "lan:aa-bb-cc-dd-ee-ff")
    assert subject["display_name"] == "dhcp-phone"
    assert subject["alias"] == "My phone"


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
                ('connection-a:active', 'external_network_client', 'connection-a:active', 'Active external', 'global', 'active', 1, 0, '2026-07-16 12:00:00')
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
            UPDATE subjects
            SET
                subject_role = 'external_network_source',
                implementation_kind = 'provider-a',
                metadata_json = json(?)
            WHERE subject_id = 'connection-a:active'
            """,
            (
                json.dumps(
                    {
                        "provider": "provider-a",
                        "connection_id": "connection-a",
                        "detail": {
                            "node_id": "node-1",
                            "provider_ip": "100.64.0.10",
                            "hostname": "active-external",
                            "user_name": "tester",
                            "online": True,
                        },
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            ),
        )

    lan = find_subject_by_ip("192.168.0.10")
    external = find_subject_by_ip("100.64.0.10")

    assert lan is not None
    assert lan["subject_id"] == "lan:active"
    assert lan["detail"]["hostname"] == "active-lan"
    assert find_subject_by_ip("192.168.0.11") is None
    assert external is not None
    assert external["subject_id"] == "connection-a:active"
    assert external["detail"]["hostname"] == "active-external"


def test_subject_inventory_sync_imports_docker_with_string_labels(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    def _fake_run(script_id: str, extra_args=None):
        if script_id in {"docker_inventory", "docker_ps"}:
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


def test_subject_inventory_sync_imports_routed_and_online_tailscale_peers(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    upsert_external_connection_record(
        {
            "connection_id": "connection-a",
            "system_id": "connection-a",
            "label": "Connection A",
            "connection_type": "external_network_source",
            "runtime_type": "tailscale",
            "integration_mode": "command_probe",
            "refresh_mode": "manual",
            "collector_config": {"script_id": "tailscale_status"},
        }
    )

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
                "HostName": "online-overlay",
                "TailscaleIPs": ["100.64.0.3"],
                "Online": True,
            },
            "peer-3": {
                "ID": "peer-3",
                "HostName": "offline-overlay",
                "TailscaleIPs": ["100.64.0.4"],
                "Online": False,
            },
        }
    }

    def _fake_run(script_id: str, extra_args=None):
        if script_id in {"docker_inventory", "docker_ps"}:
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

    assert result["synced_counts"]["external_network_client"] == 2
    subjects = list_subjects(subject_type="external_network_client")
    assert {subject["display_name"] for subject in subjects} == {"routed-node", "online-overlay"}
    assert {subject["subject_id"] for subject in subjects} == {
        "connection-a:peer-1",
        "connection-a:peer-2",
    }
    with db_session() as connection:
        metadata = [
            json.loads(row["metadata_json"])
            for row in connection.execute(
                "SELECT metadata_json FROM subjects WHERE subject_type = 'external_network_client'"
            ).fetchall()
        ]
    assert {item["connection_id"] for item in metadata} == {"connection-a"}


def test_external_ingress_provider_discovery_requires_registered_connection(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    def _fake_run(script_id: str, extra_args=None):
        raise AssertionError(script_id)

    monkeypatch.setattr("fwrouter_api.services.subject_inventory._run_script", _fake_run)

    result = sync_subject_inventory(
        requested_by="pytest",
        discover_docker=False,
        discover_external_ingress_providers=["tailscale"],
    )

    assert result["synced_counts"]["external_network_client"] == 0
    assert result["external_ingress_policy"]["providers"] == ["tailscale"]
    assert result["external_ingress_policy"]["connections"] == []
    assert result["warnings"][0]["error_code"] == "EXTERNAL_INGRESS_CONNECTION_REQUIRED"
    assert list_subjects(subject_type="external_network_client") == []


def test_external_ingress_sync_scopes_subjects_and_stale_state_by_connection(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    for connection_id, extra_arg in (("connection-a", "home"), ("connection-b", "lab")):
        upsert_external_connection_record(
            {
                "connection_id": connection_id,
                "system_id": connection_id,
                "label": connection_id,
                "connection_type": "external_network_source",
                "runtime_type": "tailscale",
                "integration_mode": "command_probe",
                "refresh_mode": "manual",
                "collector_config": {"script_id": "tailscale_status", "extra_args": [extra_arg]},
            }
        )

    payloads = {
        "home": {
            "Peer": {
                "shared-node": {
                    "ID": "shared-node",
                    "HostName": "Home",
                    "TailscaleIPs": ["100.64.0.2"],
                    "Online": True,
                }
            }
        },
        "lab": {
            "Peer": {
                "shared-node": {
                    "ID": "shared-node",
                    "HostName": "Lab",
                    "TailscaleIPs": ["100.64.0.3"],
                    "Online": True,
                }
            }
        },
    }

    def _fake_run(script_id: str, extra_args=None):
        return _FakeScriptResult(script_id, json.dumps(payloads[extra_args[0]]))

    monkeypatch.setattr("fwrouter_api.services.subject_inventory._run_script", _fake_run)

    result = sync_subject_inventory(
        requested_by="pytest",
        discover_docker=False,
        discover_external_ingress_providers=["tailscale"],
    )

    assert result["synced_counts"]["external_network_client"] == 2
    assert set(result["external_ingress_policy"]["connections"]) == {"connection-a", "connection-b"}
    subjects = list_subjects(subject_type="external_network_client")
    assert {subject["subject_id"] for subject in subjects} == {
        "connection-a:shared-node",
        "connection-b:shared-node",
    }
    assert {subject["runtime_state"] for subject in subjects} == {"active"}

    payloads["home"] = {"Peer": {}}
    result = sync_subject_inventory(
        requested_by="pytest",
        discover_docker=False,
        discover_external_ingress_providers=["tailscale"],
    )

    subjects_by_id = {subject["subject_id"]: subject for subject in list_subjects(subject_type="external_network_client")}
    assert result["stale_counts"]["external_network_client"] == 1
    assert subjects_by_id["connection-a:shared-node"]["runtime_state"] == "inactive"
    assert subjects_by_id["connection-b:shared-node"]["runtime_state"] == "active"


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


def test_subject_inventory_sync_maps_ssh_service_to_builtin_subject(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO subjects (
                subject_id, subject_type, stable_key, display_name, desired_mode,
                runtime_state, is_active, is_deleted, inactive_since
            ) VALUES (
                'host:ssh-service', 'host', 'host:ssh-service', 'ssh',
                'direct', 'missing', 0, 0, datetime('now', '-1 hour')
            )
            """
        )
        connection.execute(
            """
            INSERT INTO subject_host (
                subject_id, systemd_unit, process_name
            ) VALUES (
                'host:ssh-service', 'ssh.service', 'ssh'
            )
            """
        )

    def _fake_run(script_id: str, extra_args=None):
        if script_id == "host_services":
            return _FakeScriptResult(
                "host_services",
                json.dumps(
                    [
                        {
                            "systemd_unit": "ssh.service",
                            "process_name": "OpenSSH Server",
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
    assert result["tombstoned_counts"]["host_legacy"] == 1
    subjects = list_subjects(subject_type="host", include_deleted=True)
    subject_ids = {subject["subject_id"] for subject in subjects}
    assert "host:ssh" in subject_ids
    legacy = next(subject for subject in subjects if subject["subject_id"] == "host:ssh-service")
    assert legacy["is_deleted"] is True
    ssh = next(subject for subject in subjects if subject["subject_id"] == "host:ssh")
    assert ssh["is_active"] is True
    assert ssh["runtime_state"] == "running"


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


def test_subject_inventory_sync_does_not_mark_docker_missing_when_discovery_fails(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO subjects (
                subject_id, subject_type, stable_key, display_name, desired_mode,
                runtime_state, is_active, is_deleted, last_seen_at
            ) VALUES (
                'docker:project:service', 'docker', 'docker:project:service', 'service',
                'direct', 'running', 1, 0, CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            """
            INSERT INTO subject_docker (
                subject_id, compose_project, compose_service, container_name, image_name
            ) VALUES (
                'docker:project:service',
                'project',
                'service',
                'project-service-1',
                'example:latest'
            )
            """
        )

    def _fake_run(script_id: str, extra_args=None):
        if script_id in {"docker_inventory", "docker_ps"}:
            return _FakeScriptResult("docker_ps", "", ok=False, stderr="docker unavailable")
        raise AssertionError(script_id)

    monkeypatch.setattr("fwrouter_api.services.subject_inventory._run_script", _fake_run)

    result = sync_subject_inventory(requested_by="pytest", discover_docker=True)

    assert result["warnings"][0]["error_code"] == "DOCKER_PS_FAILED"
    assert "docker" not in result["stale_counts"]
    docker_subject = next(
        subject
        for subject in list_subjects(subject_type="docker")
        if subject["subject_id"] == "docker:project:service"
    )
    assert docker_subject["is_active"] is True
    assert docker_subject["runtime_state"] == "running"


def test_subject_inventory_sync_tombstones_inactive_legacy_compose_docker_subject(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO subjects (
                subject_id, subject_type, stable_key, display_name, desired_mode,
                runtime_state, is_active, is_deleted, inactive_since
            ) VALUES (
                'docker:project-service', 'docker', 'docker:project-service', 'service',
                'direct', 'missing', 0, 0, datetime('now', '-1 hour')
            )
            """
        )
        connection.execute(
            """
            INSERT INTO subject_docker (
                subject_id, compose_project, compose_service, container_name, image_name
            ) VALUES (
                'docker:project-service', 'project', 'service', 'project-service-1', 'example:old'
            )
            """
        )

    def _fake_run(script_id: str, extra_args=None):
        if script_id in {"docker_inventory", "docker_ps"}:
            return _FakeScriptResult(
                "docker_ps",
                json.dumps(
                    {
                        "ID": "abc",
                        "Image": "example:new",
                        "Names": "project-service-1",
                        "Labels": {
                            "com.docker.compose.project": "project",
                            "com.docker.compose.service": "service",
                        },
                        "State": "running",
                    }
                ),
            )
        raise AssertionError(script_id)

    monkeypatch.setattr("fwrouter_api.services.subject_inventory._run_script", _fake_run)

    result = sync_subject_inventory(requested_by="pytest", discover_docker=True)

    assert result["synced_counts"]["docker"] == 1
    assert result["tombstoned_counts"]["docker_legacy"] == 1
    subjects = list_subjects(subject_type="docker", include_deleted=True)
    canonical = next(
        subject for subject in subjects if subject["subject_id"] == "docker:project:service"
    )
    legacy = next(
        subject for subject in subjects if subject["subject_id"] == "docker:project-service"
    )
    assert canonical["is_active"] is True
    assert legacy["is_deleted"] is True


def test_subject_inventory_sync_imports_enriched_docker_runtime_details(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    def _fake_run(script_id: str, extra_args=None):
        if script_id in {"docker_inventory", "docker_ps"}:
            return _FakeScriptResult(
                script_id,
                json.dumps(
                    {
                        "ID": "abc",
                        "Image": "ghcr.io/example/homeassistant:latest",
                        "Names": "homeassistant",
                        "Labels": {
                            "com.docker.compose.project": "compose",
                            "com.docker.compose.service": "homeassistant",
                        },
                        "State": "running",
                        "NetworkMode": "host",
                        "ProcessUids": [0, 65532],
                        "IPAddress": "",
                        "NetworkName": None,
                        "Listeners": [
                            {"proto": "tcp", "address": "0.0.0.0", "port": 8123, "pid": 123}
                        ],
                    }
                ),
            )
        raise AssertionError(script_id)

    monkeypatch.setattr("fwrouter_api.services.subject_inventory._run_script", _fake_run)

    result = sync_subject_inventory(requested_by="pytest", discover_docker=True)

    assert result["sources"]["docker"]["script_id"] == "docker_inventory"
    subject = next(subject for subject in list_subjects(subject_type="docker") if subject["subject_id"] == "docker:compose:homeassistant")
    assert subject["detail"]["ip_address"] is None
    assert subject["detail"]["source"]["NetworkMode"] == "host"
    assert subject["detail"]["source"]["ProcessUids"] == [0, 65532]
    assert subject["detail"]["source"]["Listeners"][0]["port"] == 8123
