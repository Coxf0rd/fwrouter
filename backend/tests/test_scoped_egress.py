from __future__ import annotations
from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import initialize_database


import json
from pathlib import Path

from fastapi.testclient import TestClient

import fwrouter_api.services.apply as apply_service
import fwrouter_api.services.dataplane_global as dataplane_global_service
import fwrouter_api.services.runtime as runtime_service
import fwrouter_api.services.subject_policy as subject_policy_service
from fwrouter_api.adapters.dataplane import DataplaneOperation, DataplaneResult
from fwrouter_api.adapters.mihomo import MihomoHealth, MihomoRuntimeState
from fwrouter_api.db.connection import db_session, initialize_database
from fwrouter_api.jobs.extended_handlers import register_extended_handlers
from fwrouter_api.jobs.manager import get_default_job_manager
from fwrouter_api.main import create_app
from fwrouter_api.services.apply import ApplyMode, run_apply_pipeline
from fwrouter_api.services.jobs import create_job
from fwrouter_api.services.live_probe_cache import clear_live_probe_cache
from fwrouter_api.services.servers import ensure_routing_global_state
from fwrouter_api.services.servers import get_subject_server_override
from fwrouter_api.services.subject_policy import get_subject_with_effective_state
from fwrouter_api.services.system_subjects import ensure_builtin_system_subjects
from fwrouter_api.services.system_summary import build_system_summary


def _configure_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FWROUTER_STATE_DIR", str(tmp_path / "state"))
    get_settings.cache_clear()


def _client() -> TestClient:
    return TestClient(create_app(enable_startup_tasks=False))


class _ReadyMihomoAdapter:
    def health(self) -> MihomoHealth:
        return MihomoHealth(
            runtime_state=MihomoRuntimeState.RUNNING,
            message="mihomo ready",
            details={
                "adapter": "fake",
                "config": {
                    "redir_port": 5202,
                    "tproxy_port": 5203,
                    "tun_enabled": True,
                },
                "selectors": {
                    "vpn_global_exists": True,
                    "vpn_global_targets_count": 5,
                    "vpn_global_has_vpn_auto": True,
                    "vpn_global_now": "vpn-auto",
                },
            },
        )

    def list_servers(self):  # noqa: ANN001
        return []


class _SuccessfulDataplaneAdapter:
    def check(self, plan):  # noqa: ANN001
        return DataplaneResult(
            ok=True,
            operation=DataplaneOperation.CHECK,
            message="check ok",
            details={
                "stage": "check",
                "owned_table": "inet fwrouter_v2",
                "table_exists": True,
                "required_chains": {
                    "prerouting": True,
                    "output": True,
                    "forward": True,
                    "postrouting": True,
                    "fwrouter_classify": True,
                    "fwrouter_direct": True,
                    "fwrouter_vpn": True,
                },
            },
        )

    def apply(self, plan):  # noqa: ANN001
        return DataplaneResult(
            ok=True,
            operation=DataplaneOperation.APPLY,
            message="apply ok",
            details={
                "stage": "verify",
                "owned_table": "inet fwrouter_v2",
                "table_exists": True,
                "routing_mode": "vpn",
                "vpn_contract_ready": True,
                "vpn_external_path_verified": True,
                "vpn_tproxy_port": 5203,
                "required_chains": {
                    "prerouting": True,
                    "output": True,
                    "forward": True,
                    "postrouting": True,
                    "fwrouter_classify": True,
                    "fwrouter_direct": True,
                    "fwrouter_vpn": True,
                },
            },
        )

    def rollback(self, plan):  # noqa: ANN001
        return DataplaneResult(
            ok=True,
            operation=DataplaneOperation.ROLLBACK,
            message="rollback ok",
            details={"stage": "rollback"},
        )


def _patch_runtime(monkeypatch) -> None:
    adapter = _SuccessfulDataplaneAdapter()
    monkeypatch.setattr(apply_service, "DEFAULT_DATAPLANE_ADAPTER", adapter)
    monkeypatch.setattr(runtime_service, "DEFAULT_DATAPLANE_ADAPTER", adapter)
    monkeypatch.setattr(dataplane_global_service, "DEFAULT_MIHOMO_ADAPTER", _ReadyMihomoAdapter())
    monkeypatch.setattr(runtime_service, "DEFAULT_MIHOMO_ADAPTER", _ReadyMihomoAdapter())
    monkeypatch.setattr(
        runtime_service,
        "read_live_dataplane_payload",
        lambda: {
            "ok": True,
            "message": "live direct ok",
            "table_exists": True,
            "required_chains": {
                "prerouting": True,
                "output": True,
                "forward": True,
                "postrouting": True,
                "fwrouter_classify": True,
                "fwrouter_direct": True,
                "fwrouter_vpn": True,
            },
        },
    )
    monkeypatch.setattr(
        runtime_service,
        "build_runtime_enforcement_state",
        lambda **kwargs: {
            "dataplane_capability": "global_policy_v1",
            "capability": "global_policy_v1",
            "enforcement_level": "global_direct_enforced",
            "traffic_enforcement_guaranteed": True,
            "supported_modes": {"direct": True, "selective": True, "vpn": True},
            "missing_runtime_requirements": [],
            "profile": {"profile": "global_v1"},
            "active_mode_matches_intent": True,
            "live_global_mode": "direct",
            "live_selective_default": "direct",
        },
    )
    monkeypatch.setattr(
        apply_service,
        "probe_live_global_mode",
        lambda: {
            "ok": True,
            "table_exists": True,
            "mode": "direct",
            "selective_default": "direct",
        },
    )


def _seed_server(server_id: str) -> None:
    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO servers (
                server_id,
                server_name,
                provider_name,
                inventory_state,
                raw_json
            )
            VALUES (?, ?, 'provider', 'active', '{}')
            """,
            (server_id, server_id),
        )
        connection.execute(
            """
            INSERT INTO server_preferences (
                server_id,
                vpn_auto,
                global_list
            )
            VALUES (?, 1, 1)
            """,
            (server_id,),
        )
        connection.execute(
            "INSERT OR IGNORE INTO server_ping_state (server_id, status) VALUES (?, 'success')",
            (server_id,),
        )


def _seed_lan_subject(
    subject_id: str,
    *,
    desired_mode: str,
    ip_address: str | None,
) -> None:
    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO subjects (
                subject_id,
                subject_type,
                stable_key,
                display_name,
                desired_mode,
                applied_mode,
                runtime_state,
                is_active
            )
            VALUES (?, 'lan', ?, ?, ?, ?, 'active', 1)
            """,
            (subject_id, subject_id, subject_id, desired_mode, desired_mode),
        )
        connection.execute(
            """
            INSERT INTO subject_lan (
                subject_id,
                mac_address,
                ip_address,
                hostname,
                dhcp_hostname
            )
            VALUES (?, 'aa:bb:cc:dd:ee:ff', ?, ?, 'pytest')
            """,
            (subject_id, ip_address, subject_id),
        )


def _seed_docker_subject(
    subject_id: str,
    *,
    desired_mode: str,
    ip_address: str | None,
) -> None:
    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO subjects (
                subject_id,
                subject_type,
                stable_key,
                display_name,
                desired_mode,
                applied_mode,
                runtime_state,
                is_active
            )
            VALUES (?, 'docker', ?, ?, ?, ?, 'running', 1)
            """,
            (subject_id, subject_id, subject_id, desired_mode, desired_mode),
        )
        connection.execute(
            """
            INSERT INTO subject_docker (
                subject_id,
                compose_project,
                compose_service,
                container_name,
                container_id,
                image_name,
                ip_address,
                network_name
            )
            VALUES (?, 'pytest', 'svc', 'svc', 'container-1', 'image:latest', ?, 'bridge')
            """,
            (subject_id, ip_address),
        )


def _seed_routing_state(*, desired_mode: str, active_auto_server_id: str | None = None) -> None:
    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO routing_global_state (
                id,
                desired_mode,
                applied_mode,
                selective_default,
                server_mode,
                active_auto_server_id,
                apply_state
            )
            VALUES (1, ?, ?, 'direct', 'auto', ?, 'clean')
            ON CONFLICT(id) DO UPDATE SET
                desired_mode = excluded.desired_mode,
                applied_mode = excluded.applied_mode,
                active_auto_server_id = excluded.active_auto_server_id,
                apply_state = 'clean',
                error_code = NULL,
                error_message = NULL,
                updated_at = CURRENT_TIMESTAMP
            """,
            (desired_mode, desired_mode, active_auto_server_id),
        )


def _create_apply_job() -> dict[str, object]:
    return create_job(
        "apply_pipeline_test",
        lock_key="apply-pipeline-test",
        requested_by="pytest",
        input_data={"source": "pytest"},
    )


def test_subject_effective_state_exposes_scoped_runtime(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _seed_server("server-1")
    _seed_routing_state(desired_mode="vpn", active_auto_server_id="server-1")
    _seed_lan_subject("lan-1", desired_mode="vpn", ip_address="192.168.10.4")

    monkeypatch.setattr(
        subject_policy_service,
        "build_runtime_enforcement_state",
        lambda: {
            "supported_modes": {"direct": True, "selective": False, "vpn": True},
            "enforcement_level": "global_vpn_enforced",
            "traffic_enforcement_guaranteed": True,
        },
    )

    subject = get_subject_with_effective_state("lan-1")

    assert subject is not None
    scoped_runtime = subject["effective_state"]["scoped_runtime"]
    assert scoped_runtime["eligible"] is True
    assert scoped_runtime["applied"] is True
    assert scoped_runtime["status"] == "applied"
    assert scoped_runtime["match_key"] == "ip:192.168.10.4"


def test_subject_effective_state_exposes_scoped_runtime_for_selective_lan(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _seed_routing_state(desired_mode="direct")
    _seed_lan_subject("lan-selective", desired_mode="selective", ip_address="192.168.10.44")

    monkeypatch.setattr(
        subject_policy_service,
        "_default_subject_runtime_enforcement",
        lambda **kwargs: {
            "supported_modes": {"direct": True, "selective": True, "vpn": True},
            "enforcement_level": "global_direct_enforced",
            "traffic_enforcement_guaranteed": True,
        },
    )

    subject = get_subject_with_effective_state("lan-selective")

    assert subject is not None
    scoped_runtime = subject["effective_state"]["scoped_runtime"]
    assert scoped_runtime["tracked"] is True
    assert scoped_runtime["eligible"] is True
    assert scoped_runtime["applied"] is True
    assert scoped_runtime["status"] == "applied"
    assert scoped_runtime["match_key"] == "ip:192.168.10.44"
    assert scoped_runtime["materialized_by"] == "nft_subject_classify"


def test_subject_server_override_endpoint_runs_apply(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    register_extended_handlers(get_default_job_manager())
    _patch_runtime(monkeypatch)
    _seed_server("server-1")
    _seed_routing_state(desired_mode="direct")
    _seed_lan_subject("lan-override", desired_mode="vpn", ip_address="192.168.20.5")

    with _client() as client:
        response = client.post(
            "/api/v2/subjects/lan-override/server-override",
            json={"server_id": "server-1", "requested_by": "pytest"},
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["ok"] is True
        override_payload = payload["data"]["server_override"]
        if isinstance(override_payload, dict) and "server_override" in override_payload:
            override_payload = override_payload["server_override"]
        assert override_payload["apply_state"] == "clean"

        subject = client.get("/api/v2/subjects/lan-override").json()["data"]["subject"]
        assert subject["effective_state"]["scoped_runtime"]["status"] == "applied"

        runtime = client.get("/api/v2/runtime").json()["data"]["runtime"]
        assert runtime["dataplane"]["scoped_egress"]["state"] == "active"
        assert runtime["dataplane"]["scoped_egress"]["applied_count"] >= 1
        assert runtime["dataplane"]["scoped_egress_readiness"]["state"] == "ready"

        scoped = client.get("/api/v2/runtime/scoped-egress").json()["data"]["scoped_egress"]
        assert scoped["diagnostics"]["state"] == "active"
        assert scoped["readiness"]["ready_for_server_rollout"] is True


def test_subject_server_override_pending_when_subject_not_in_vpn_path(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    register_extended_handlers(get_default_job_manager())
    _patch_runtime(monkeypatch)
    _seed_server("server-1")
    _seed_routing_state(desired_mode="direct")
    _seed_lan_subject("lan-direct", desired_mode="direct", ip_address="192.168.30.6")

    with _client() as client:
        response = client.post(
            "/api/v2/subjects/lan-direct/server-override",
            json={"server_id": "server-1", "requested_by": "pytest"},
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["ok"] is True
        override_payload = payload["data"]["server_override"]
        if isinstance(override_payload, dict) and "server_override" in override_payload:
            override_payload = override_payload["server_override"]
        assert override_payload["apply_state"] == "pending"

        subject = client.get("/api/v2/subjects/lan-direct").json()["data"]["subject"]
        assert subject["effective_state"]["scoped_runtime"]["status"] == "pending_not_vpn_path"


def test_apply_pipeline_renders_scoped_vpn_rules(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _patch_runtime(monkeypatch)
    _seed_server("server-1")
    _seed_routing_state(desired_mode="direct")
    _seed_lan_subject("lan-candidate", desired_mode="vpn", ip_address="192.168.40.7")

    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO subject_server_overrides (
                subject_id,
                selected_server_id,
                selected_until,
                apply_state
            )
            VALUES (?, ?, datetime('now', '+24 hours'), 'pending')
            """,
            ("lan-candidate", "server-1"),
        )

    job = _create_apply_job()
    result = run_apply_pipeline(
        job_id=str(job["job_id"]),
        reason="pytest_scoped_egress",
        mode=ApplyMode.APPLY,
    )

    assert result["ok"] is True
    assert result["scoped_egress"]["applied_count"] >= 1
    candidate_path = Path(result["manifest"]["paths"]["candidate_nft_path"])
    candidate_text = candidate_path.read_text(encoding="utf-8")
    # Removed NFT comment expectation
    assert "192.168.40.7" in candidate_text
    assert get_subject_server_override("lan-candidate") is not None


def test_docker_subject_server_override_is_counted_as_scoped_when_matcher_resolves(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    register_extended_handlers(get_default_job_manager())
    _patch_runtime(monkeypatch)
    _seed_server("server-1")
    _seed_routing_state(desired_mode="direct")
    _seed_docker_subject("docker-scoped", desired_mode="vpn", ip_address="172.18.0.8")

    with _client() as client:
        response = client.post(
            "/api/v2/subjects/docker-scoped/server-override",
            json={"server_id": "server-1", "requested_by": "pytest"},
        )

        assert response.status_code == 200
        subject = client.get("/api/v2/subjects/docker-scoped").json()["data"]["subject"]
        scoped_runtime = subject["effective_state"]["scoped_runtime"]
        assert scoped_runtime["eligible"] is True
        assert scoped_runtime["applied"] is True
        assert scoped_runtime["status"] == "applied"

        runtime = client.get("/api/v2/runtime/scoped-egress").json()["data"]["scoped_egress"]
        assert runtime["diagnostics"]["eligible_count"] >= 1
        assert runtime["diagnostics"]["inventory_counts"]["eligible_for_scoped_vpn"] >= 1


def test_fwrouter_subject_is_excluded_from_scoped_vpn_accounting(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _patch_runtime(monkeypatch)
    ensure_builtin_system_subjects()
    ensure_routing_global_state()

    with db_session() as connection:
        connection.execute(
            """
            UPDATE subjects
            SET desired_mode = 'vpn', applied_mode = 'vpn'
            WHERE subject_id = 'fwrouter:global'
            """
        )

    summary = runtime_service.get_runtime_summary()
    bindings = summary["dataplane"]["scoped_egress"]["bindings"]
    assert all(binding["subject_id"] != "fwrouter:global" for binding in bindings)
    assert summary["dataplane"]["scoped_egress"]["inventory_counts"]["control_plane_direct_safe"] >= 1


def test_scoped_egress_flags_selective_runtime_when_transparent_tcp_is_unhealthy(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _seed_routing_state(desired_mode="direct")
    _seed_lan_subject("lan-selective-unhealthy", desired_mode="selective", ip_address="192.168.10.55")

    monkeypatch.setattr(
        runtime_service,
        "build_runtime_enforcement_state",
        lambda **kwargs: {
            "dataplane_capability": "global_policy_v1",
            "capability": "global_policy_v1",
            "enforcement_level": "global_direct_enforced",
            "traffic_enforcement_guaranteed": True,
            "supported_modes": {"direct": True, "selective": True, "vpn": True},
            "missing_runtime_requirements": [],
            "profile": {
                "mihomo": {
                    "contours": {
                        "transparent_tcp_ready": False,
                        "transparent_udp_ready": True,
                    }
                }
            },
            "active_mode_matches_intent": True,
            "live_global_mode": "direct",
            "live_selective_default": "direct",
        },
    )
    monkeypatch.setattr(
        subject_policy_service,
        "build_runtime_enforcement_state",
        lambda: {
            "supported_modes": {"direct": True, "selective": True, "vpn": True},
            "enforcement_level": "global_direct_enforced",
            "traffic_enforcement_guaranteed": True,
        },
    )
    clear_live_probe_cache()

    summary = runtime_service.get_runtime_summary()

    blockers = summary["dataplane"]["scoped_egress"]["blockers"]
    statuses = {item.get("status") for item in blockers}
    assert "selective_materialized_but_transparent_tcp_unhealthy" in statuses


def test_system_summary_exposes_scoped_egress_blocked_warning(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    register_extended_handlers(get_default_job_manager())
    _patch_runtime(monkeypatch)
    _seed_server("server-1")
    _seed_routing_state(desired_mode="direct")
    _seed_lan_subject("lan-blocked", desired_mode="vpn", ip_address="192.168.50.8")

    with _client() as client:
        client.post(
            "/api/v2/core/bypass/enable",
            json={"confirm_apply": True, "requested_by": "pytest"},
        )
        client.post(
            "/api/v2/subjects/lan-blocked/server-override",
            json={"server_id": "server-1", "requested_by": "pytest"},
        )

    summary = build_system_summary()
    assert summary["backend"]["readiness"]["scoped_egress"]["state"] == "blocked"
    assert any(
        warning["code"] == "FWROUTER_SCOPED_EGRESS_BLOCKED"
        for warning in summary["warnings"]
    )
