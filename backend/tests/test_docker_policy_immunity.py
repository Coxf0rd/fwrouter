from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from _test_support import configure_test_state_dir
from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import db_session, initialize_database
from fwrouter_api.services import provider_recovery, routing_manifest, subject_policy
from fwrouter_api.services import apply as apply_service
from fwrouter_api.services.apply import ApplyMode, run_apply_pipeline
from fwrouter_api.services.dataplane_nft_render import (
    DockerPolicyConflictError,
    render_owned_table_candidate,
)
from fwrouter_api.services.docker_policy import docker_vpn_immunity_conflict
from fwrouter_api.services.scoped_egress import build_scoped_subject_runtime, build_scoped_egress_diagnostics, summarize_scoped_subjects
from fwrouter_api.services.server_layout import protected_docker_runtime_role
from fwrouter_api.services.subjects import list_subjects
from fwrouter_api.services.jobs import create_job


def _subject(path: str, *, address: str = "172.25.0.2", applied: bool = True) -> dict:
    return {
        "subject_id": "docker:openflux:openflux",
        "subject_type": "docker",
        "display_name": "OpenFlux",
        "is_active": True,
        "dataplane_path": path,
        "scoped_runtime": {
            "applied": applied,
            "status": "applied" if applied else "pending",
            "matcher": {
                "family": "ipv4",
                "value": address,
                "nft_expr": "ip saddr",
            },
        },
    }


def _manifest(subjects: list[dict], *, extra: dict | None = None) -> dict:
    return {
        "summary": {"global_mode": "vpn", "selective_default": "direct"},
        "global_preflight": {
            "vpn_contour": {"redir_port": 5204, "tproxy_port": 5202},
            "selective_vpn_policy_ready": True,
        },
        "subjects": subjects,
        "extra": {"rules_effective": {"rules": []}, **(extra or {})},
    }


def test_docker_full_vpn_reaches_classify_instead_of_infrastructure_accept(isolated_host_observations) -> None:
    subject = _subject("vpn")
    candidate = render_owned_table_candidate(_manifest([subject]))
    assert 'ip saddr 172.25.0.2 goto fwrouter_vpn_full comment "scoped vpn override: docker:openflux:openflux"' in candidate
    infrastructure_set = candidate.split("set infrastructure_ipv4", 1)[1].split("    }", 1)[0]
    assert "172.25.0.2" not in infrastructure_set


def test_docker_direct_and_blocked_paths_are_not_immune_to_classification(isolated_host_observations) -> None:
    direct = render_owned_table_candidate(_manifest([_subject("direct")]))
    assert 'ip saddr 172.25.0.2 goto fwrouter_direct comment "scoped direct override: docker:openflux:openflux"' in direct
    assert "172.25.0.2" not in direct.split("set infrastructure_ipv4", 1)[1].split("    }", 1)[0]

    blocked = render_owned_table_candidate(_manifest([_subject("blocked")]))
    assert 'ip saddr 172.25.0.2 reject with icmpx type admin-prohibited comment "scoped disabled block: docker:openflux:openflux"' in blocked
    assert "172.25.0.2" not in blocked.split("set infrastructure_ipv4", 1)[1].split("    }", 1)[0]


def test_full_vpn_fails_closed_for_protected_runtime_and_overlapping_protected_network(isolated_host_observations) -> None:
    protected = _subject("vpn", address="172.25.0.10")
    protected["scoped_runtime"]["infrastructure_role"] = "vpn_dataplane"
    with pytest.raises(DockerPolicyConflictError, match="protected_runtime_cannot_use_scoped_vpn"):
        render_owned_table_candidate(_manifest([protected]))

    ordinary = _subject("vpn")
    with pytest.raises(DockerPolicyConflictError, match="docker_vpn_matcher_overlaps_infrastructure"):
        render_owned_table_candidate(
            _manifest([ordinary], extra={"infrastructure_ipv4": ["172.25.0.0/24"]})
        )


def test_full_vpn_rejects_unready_matcher_but_core_bypass_remains_available(isolated_host_observations) -> None:
    with pytest.raises(DockerPolicyConflictError, match="docker_vpn_runtime_not_ready"):
        render_owned_table_candidate(_manifest([_subject("vpn", applied=False)]))

    bypass = _manifest(
        [_subject("vpn", applied=False)],
        extra={"core_bypass": {"enabled": True, "reason": "operator safe mode"}},
    )
    assert "intentional bypass/direct-safe mode" in render_owned_table_candidate(bypass)


def test_protected_docker_role_uses_exact_compose_service_and_container_identity() -> None:
    assert protected_docker_runtime_role(
        {
            "subject_type": "docker",
            "detail": {"compose_service": "fwrouter-xray", "container_name": "/fwrouter-xray"},
        }
    ) == "explicit_client_runtime"
    assert protected_docker_runtime_role(
        {
            "subject_type": "docker",
            "detail": {"compose_service": "user-xray", "container_name": "fwrouter-xray"},
        }
    ) is None
    assert protected_docker_runtime_role(
        {
            "subject_type": "docker",
            "detail": {"compose_service": "fwrouter-xray", "container_name": "other"},
        }
    ) is None


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ({"NetworkMode": "bridge", "IPv4InventoryComplete": True, "IPv4Addresses": ["172.25.0.2"], "IPv6InventoryComplete": True, "IPv6Addresses": []}, "applied"),
        ({"NetworkMode": "bridge", "IPv4InventoryComplete": True, "IPv4Addresses": ["172.25.0.2"], "IPv6InventoryComplete": False, "IPv6Addresses": []}, "pending_docker_ipv6_inventory"),
        ({"NetworkMode": "bridge", "IPv4InventoryComplete": True, "IPv4Addresses": ["172.25.0.2"], "IPv6InventoryComplete": True, "IPv6Addresses": ["2001:db8::2"]}, "pending_docker_ipv6_unsupported"),
        ({"NetworkMode": "host", "IPv4InventoryComplete": True, "IPv4Addresses": ["172.25.0.2"], "IPv6InventoryComplete": True, "IPv6Addresses": []}, "pending_docker_host_network"),
    ],
)
def test_docker_vpn_runtime_requires_complete_ipv4_only_isolated_inventory(source: dict, expected: str) -> None:
    subject = {
        "subject_id": "docker:openflux:openflux",
        "subject_type": "docker",
        "is_active": True,
        "detail": {"ip_address": "172.25.0.2", "source": source},
    }
    runtime = build_scoped_subject_runtime(
        subject,
        dataplane_path="vpn",
        selected_server_id="server-a",
        selected_server_source="global_fixed",
        server_override=None,
        vpn_supported=True,
        bypass_enabled=False,
    )
    assert runtime["status"] == expected
    assert runtime["applied"] is (expected == "applied")


def test_inventory_reader_reports_all_ipv6_network_addresses_fail_closed() -> None:
    script = Path(__file__).parents[2] / "host/libexec/fwrouter/docker-inventory.py"
    spec = importlib.util.spec_from_file_location("docker_inventory_contract", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    addresses, complete = module._network_ipv6_inventory(
        {
            "NetworkSettings": {
                "Networks": {
                    "a": {"GlobalIPv6Address": "2001:db8::2"},
                    "b": {"GlobalIPv6Address": ""},
                }
            }
        }
    )
    assert addresses == ["2001:db8::2"]
    assert complete is True
    assert module._network_ipv6_inventory({"NetworkSettings": {}}) == ([], False)
    assert module._network_ipv6_inventory(
        {"NetworkSettings": {"Networks": {"a": {"GlobalIPv6Address": "not-an-ip"}}}}
    ) == ([], False)
    # `docker inspect` must explicitly report an empty address; an absent
    # field cannot be treated as proof that the address inventory is complete.
    assert module._network_ipv6_inventory(
        {"NetworkSettings": {"Networks": {"a": {"IPAddress": "172.25.0.2"}}}}
    ) == ([], False)
    assert module._network_ipv6_inventory(
        {"NetworkSettings": {"Networks": {"a": {"GlobalIPv6Address": None}}}}
    ) == ([], False)


def test_policy_conflict_checks_host_addresses_and_cidr_extra() -> None:
    docker = _subject("vpn")
    docker["scoped_runtime"]["applied"] = True
    host = {
        "subject_id": "host:router",
        "subject_type": "host",
        "scoped_runtime": {"matcher": {"family": "ipv4", "value": "172.25.0.2"}},
    }
    assert docker_vpn_immunity_conflict([docker, host]) == (
        "docker:openflux:openflux",
        "docker_vpn_matcher_overlaps_infrastructure",
    )


def _seed_manifest_database(monkeypatch, tmp_path: Path, address: str) -> list[dict]:
    configure_test_state_dir(monkeypatch, tmp_path)
    initialize_database()
    source = {
        "NetworkMode": "bridge",
        "IPAddress": address,
        "IPv4InventoryComplete": True, "IPv4Addresses": [address], "IPv6InventoryComplete": True,
        "IPv6Addresses": [],
    }
    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO servers (server_id, server_name, provider_name, inventory_state, raw_json)
            VALUES ('srv-1', 'Test server', 'pytest', 'active', '{}')
            """
        )
        connection.execute(
            """
            INSERT INTO routing_global_state (
                id, desired_mode, applied_mode, selective_default, server_mode,
                desired_fixed_server_id, applied_fixed_server_id, apply_state
            ) VALUES (1, 'direct', 'direct', 'direct', 'fixed', 'srv-1', 'srv-1', 'clean')
            """
        )
        connection.execute(
            """
            INSERT INTO subjects (
                subject_id, subject_type, subject_role, implementation_kind,
                stable_key, display_name, desired_mode, applied_mode, runtime_state,
                is_active, is_deleted
            ) VALUES (
                'docker:openflux:openflux', 'docker', 'docker_runtime', 'docker',
                'docker:openflux:openflux', 'OpenFlux', 'vpn', 'vpn', 'running', 1, 0
            )
            """
        )
        connection.execute(
            """
            INSERT INTO subject_docker (
                subject_id, compose_project, compose_service, container_name,
                container_id, image_name, ip_address, network_name, source_json
            ) VALUES (
                'docker:openflux:openflux', 'openflux', 'openflux', 'openflux',
                'test-container', 'test/image', ?, 'bridge', ?
            )
            """,
            (address, json.dumps(source)),
        )
    preflight = {
        "profile": {"profile": "unit-test"},
        "can_enforce_global_direct": True,
        "can_enforce_global_selective": True,
        "can_enforce_global_vpn": True,
        "missing": [],
        "selective_rules": {"requires_vpn_runtime": False},
        "vpn_contour": {"ready": True, "redir_port": 5204, "tproxy_port": 5202},
        "selective_vpn_policy_ready": True,
    }
    monkeypatch.setattr(routing_manifest, "build_global_preflight", lambda **_kwargs: dict(preflight))
    monkeypatch.setattr(subject_policy, "build_global_preflight", lambda **_kwargs: dict(preflight))
    monkeypatch.setattr(provider_recovery, "emergency_override", lambda: None)
    monkeypatch.setattr(provider_recovery, "override_manifest_state", lambda routing, subjects, **_kwargs: (routing, subjects))
    return list_subjects(subject_type="docker")


def test_database_manifest_preserves_protected_cidr_and_projects_conflict_pending(
    monkeypatch, tmp_path: Path, isolated_host_observations
) -> None:
    subjects = _seed_manifest_database(monkeypatch, tmp_path, "172.25.0.2")
    manifest = routing_manifest.build_dataplane_manifest_from_state(
        plan_id="test-docker-protected-cidr",
        reason="pytest",
        routing={
            "desired_mode": "direct",
            "applied_mode": "direct",
            "selective_default": "direct",
            "server_mode": "fixed",
            "desired_fixed_server_id": "srv-1",
            "applied_fixed_server_id": "srv-1",
            "active_auto_server_id": None,
        },
        subjects=subjects,
        extra={"infrastructure_ipv4": ["172.25.0.0/24"], "rules_effective": {"rules": []}},
    )
    docker = next(item for item in manifest["subjects"] if item["subject_type"] == "docker")
    assert manifest["extra"]["infrastructure_ipv4"] == ["172.25.0.0/24"]
    assert docker["scoped_runtime"]["applied"] is False
    assert docker["scoped_runtime"]["status"] == "pending_infrastructure_immunity_conflict"
    assert docker["scoped_runtime"]["reason"] == "docker_vpn_matcher_overlaps_infrastructure"
    with pytest.raises(DockerPolicyConflictError):
        render_owned_table_candidate(manifest)


def test_database_manifest_rebuild_uses_current_docker_ip_after_inventory_change(
    monkeypatch, tmp_path: Path, isolated_host_observations
) -> None:
    subjects = _seed_manifest_database(monkeypatch, tmp_path, "172.25.0.2")
    first = routing_manifest.build_dataplane_manifest_from_state(
        plan_id="test-docker-ip-old",
        reason="pytest",
        routing={"desired_mode": "direct", "applied_mode": "direct", "selective_default": "direct", "server_mode": "fixed", "desired_fixed_server_id": "srv-1", "applied_fixed_server_id": "srv-1"},
        subjects=subjects,
        extra={"rules_effective": {"rules": []}},
    )
    first_candidate = render_owned_table_candidate(first)
    with db_session() as connection:
        connection.execute(
            "UPDATE subject_docker SET ip_address = '172.25.0.3', source_json = json_set(source_json, '$.IPAddress', '172.25.0.3', '$.IPv4Addresses', json('[\"172.25.0.3\"]')) WHERE subject_id = 'docker:openflux:openflux'"
        )
    refreshed_subjects = list_subjects(subject_type="docker")
    second = routing_manifest.build_dataplane_manifest_from_state(
        plan_id="test-docker-ip-new",
        reason="pytest",
        routing={"desired_mode": "direct", "applied_mode": "direct", "selective_default": "direct", "server_mode": "fixed", "desired_fixed_server_id": "srv-1", "applied_fixed_server_id": "srv-1"},
        subjects=refreshed_subjects,
        extra={"rules_effective": {"rules": []}},
    )
    second_candidate = render_owned_table_candidate(second)
    assert "ip saddr 172.25.0.2 goto fwrouter_vpn_full" in first_candidate
    assert "ip saddr 172.25.0.2 goto fwrouter_vpn_full" not in second_candidate
    assert "ip saddr 172.25.0.3 goto fwrouter_vpn_full" in second_candidate


def test_policy_conflict_stops_apply_before_native_adapter_or_candidate_publication(
    monkeypatch, tmp_path: Path
) -> None:
    configure_test_state_dir(monkeypatch, tmp_path)
    initialize_database()
    calls: list[str] = []

    def reject(**_kwargs):
        raise DockerPolicyConflictError("docker:openflux:openflux: protected conflict")

    class Adapter:
        def check(self, _plan):
            calls.append("check")
            raise AssertionError("native adapter must not run for a rejected candidate")

        def apply(self, _plan):
            calls.append("apply")
            raise AssertionError("native adapter must not run for a rejected candidate")

    monkeypatch.setattr(apply_service, "build_dataplane_manifest", reject)
    monkeypatch.setattr(apply_service, "DEFAULT_DATAPLANE_ADAPTER", Adapter())
    monkeypatch.setattr(
        apply_service,
        "build_apply_plan",
        lambda **_kwargs: {
            "apply_id": "docker-policy-rejected",
            "job_id": str(_kwargs["job_id"]),
            "mode": _kwargs["mode"].value,
            "reason": _kwargs["reason"],
            "input": {},
            "dataplane": {"operation": "check", "adapter": "nft-owned-table"},
        },
    )
    job = create_job("docker_policy_conflict_test", lock_key="docker-policy-conflict-test", requested_by="pytest")
    with pytest.raises(DockerPolicyConflictError):
        run_apply_pipeline(job_id=str(job["job_id"]), reason="pytest", mode=ApplyMode.APPLY)

    result_path = get_settings().paths.generated_dir / "dataplane" / "last-result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["dataplane"]["error_code"] == "DOCKER_VPN_POLICY_CONFLICT"
    assert calls == []
    assert not (get_settings().paths.generated_dir / "dataplane" / "current-manifest.json").exists()


@pytest.mark.parametrize("inactive_path", ["direct", "vpn", "unknown"])
def test_stale_docker_ip_reuse_cannot_shadow_full_vpn(isolated_host_observations, inactive_path):
    active = _subject("vpn")
    stale = _subject(inactive_path)
    stale["subject_id"] = "docker:removed:stale"
    stale["is_active"] = False
    with pytest.raises(DockerPolicyConflictError, match="overlaps_infrastructure"):
        render_owned_table_candidate(_manifest([active, stale]))


@pytest.mark.parametrize("source_patch", [
    {"IPv4InventoryComplete": False},
    {"IPv4Addresses": []},
    {"IPv4Addresses": ["172.25.0.2", "172.26.0.2"]},
    {"IPv4Addresses": ["172.25.0.99"]},
    {"IPv4Addresses": ["invalid"]},
    {"IPv4Addresses": ["2001:db8::2"]},
])
def test_docker_full_vpn_rejects_incomplete_or_multiple_ipv4(source_patch):
    source = {"IPv4InventoryComplete": True, "IPv4Addresses": ["172.25.0.2"], "IPv6InventoryComplete": True, "IPv6Addresses": []}
    source.update(source_patch)
    runtime = build_scoped_subject_runtime(
        {"subject_id": "docker:test:test", "subject_type": "docker", "is_active": True,
         "detail": {"ip_address": "172.25.0.2", "source": source}},
        dataplane_path="vpn", selected_server_id="server-a", selected_server_source="global_fixed",
        server_override=None, vpn_supported=True, bypass_enabled=False,
    )
    assert not runtime["applied"]
    assert runtime["status"] in {"pending_docker_ipv4_inventory", "pending_docker_multi_ipv4_unsupported"}


def test_all_network_ipv4_inventory_is_complete_and_deduplicated():
    script = Path(__file__).parents[2] / "host/libexec/fwrouter/docker-inventory.py"
    spec = importlib.util.spec_from_file_location("docker_ipv4_inventory", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    inspect = {"NetworkSettings": {"Networks": {
        "a": {"IPAddress": "172.25.0.2"}, "b": {"IPAddress": "172.26.0.2"},
    }}}
    assert module._network_address_inventory(inspect, 4) == (["172.25.0.2", "172.26.0.2"], True)
    inspect["NetworkSettings"]["Networks"]["b"]["IPAddress"] = "172.25.0.2"
    assert module._network_address_inventory(inspect, 4) == (["172.25.0.2"], True)
    del inspect["NetworkSettings"]["Networks"]["b"]["IPAddress"]
    assert module._network_address_inventory(inspect, 4) == ([], False)


def test_partial_manifest_nested_vpn_path_cannot_become_immune(isolated_host_observations):
    subject = _subject("vpn")
    subject["effective_state"] = {"dataplane_path": subject.pop("dataplane_path")}
    candidate = render_owned_table_candidate(_manifest([subject]))
    assert "172.25.0.2" not in candidate.split("set infrastructure_ipv4", 1)[1].split("    }", 1)[0]
    assert 'ip saddr 172.25.0.2 goto fwrouter_vpn_full' in candidate


@pytest.mark.parametrize("other_mode", ["direct", "vpn", "blocked"])
def test_duplicate_active_docker_ip_cannot_depend_on_rule_order(isolated_host_observations, other_mode):
    vpn = _subject("vpn")
    other = _subject(other_mode)
    other["subject_id"] = "docker:other:other"
    for subjects in ([vpn, other], [other, vpn]):
        with pytest.raises(DockerPolicyConflictError, match="overlaps_infrastructure"):
            render_owned_table_candidate(_manifest(subjects))


@pytest.mark.parametrize("status", [
    "pending_infrastructure_immunity_conflict", "pending_docker_host_network",
    "pending_docker_ipv4_inventory", "pending_docker_multi_ipv4_unsupported",
    "pending_docker_direct_source_coverage", "pending_docker_ipv6_inventory", "pending_docker_ipv6_unsupported",
])
def test_docker_refusal_remains_visible_in_pending_diagnostics(status):
    binding = {"subject_id": "docker:test:test", "status": status, "applied": False,
               "resolution_reason": "explicit_fail_closed_reason"}
    diagnostics = build_scoped_egress_diagnostics(
        summary={"bindings": [binding]}, runtime_enforcement={}, bypass={},
    )
    assert diagnostics["pending_bindings_sample"] == [binding]
    assert diagnostics["pending_bindings_sample"][0]["resolution_reason"] == "explicit_fail_closed_reason"


@pytest.mark.parametrize("saved_override", [None, {"selected_server_id": "srv-1"}])
@pytest.mark.parametrize("patch", [
    {"IPv4InventoryComplete": False}, {"IPv4Addresses": ["172.25.0.2", "172.26.0.2"]},
    {"IPv6InventoryComplete": False}, {"IPv6Addresses": ["2001:db8::2"]},
])
def test_docker_direct_refuses_uncovered_sources_under_global_vpn(isolated_host_observations, patch, saved_override):
    source = {"IPv4InventoryComplete": True, "IPv4Addresses": ["172.25.0.2"], "IPv6InventoryComplete": True, "IPv6Addresses": []}
    source.update(patch)
    subject = {"subject_id": "docker:test:test", "display_name": "Synthetic Docker", "subject_type": "docker", "is_active": True,
               "detail": {"ip_address": "172.25.0.2", "source": source}}
    runtime = build_scoped_subject_runtime(subject, dataplane_path="direct", selected_server_id=None,
        selected_server_source=None, server_override=saved_override, vpn_supported=True, bypass_enabled=False, global_mode="vpn")
    assert runtime["status"] == "pending_docker_direct_source_coverage"
    assert not runtime["applied"]
    summary = summarize_scoped_subjects([{**subject, "effective_state": {"scoped_runtime": runtime}}])
    assert summary["state"] == "degraded"
    assert summary["pending_count"] == 1
    assert summary["applied_count"] == 0
    assert len(summary["bindings"]) == 1
    manifest_subject = {**_subject("direct"), "scoped_runtime": runtime}
    with pytest.raises(DockerPolicyConflictError, match="docker_direct_source_coverage_incomplete"):
        render_owned_table_candidate(_manifest([manifest_subject]))
    # Without global capture, Direct does not need an unavailable VPN matcher.
    direct = build_scoped_subject_runtime(subject, dataplane_path="direct", selected_server_id=None,
        selected_server_source=None, server_override=saved_override, vpn_supported=True, bypass_enabled=False, global_mode="direct")
    assert direct["status"] == ("pending_not_vpn_path" if saved_override is not None else "not_applicable")


@pytest.mark.parametrize("detail", [
    {"source": {"NetworkMode": "host"}},
    {"compose_service": "mihomo", "container_name": "fwrouter-mihomo"},
])
def test_direct_immunity_exemptions_do_not_require_bridged_inventory(detail):
    runtime = build_scoped_subject_runtime({"subject_type": "docker", "detail": detail}, dataplane_path="direct",
        selected_server_id=None, selected_server_source=None, server_override=None, vpn_supported=True,
        bypass_enabled=False, global_mode="vpn")
    assert runtime["status"] == "not_applicable"


def test_database_direct_coverage_refusal_survives_manifest_projection(monkeypatch, tmp_path, isolated_host_observations):
    _seed_manifest_database(monkeypatch, tmp_path, "172.25.0.2")
    with db_session() as connection:
        connection.execute("UPDATE subjects SET desired_mode='direct', applied_mode='direct' WHERE subject_type='docker'")
        connection.execute("UPDATE subject_docker SET source_json=json_set(source_json, '$.IPv4Addresses', json('[\"172.25.0.2\",\"172.26.0.2\"]'))")
    manifest = routing_manifest.build_dataplane_manifest_from_state(
        plan_id="docker-direct-coverage", reason="pytest",
        routing={"desired_mode": "vpn", "applied_mode": "vpn", "selective_default": "direct", "server_mode": "fixed", "desired_fixed_server_id": "srv-1", "applied_fixed_server_id": "srv-1"},
        subjects=list_subjects(subject_type="docker"), extra={"rules_effective": {"rules": []}},
    )
    runtime = manifest["subjects"][0]["scoped_runtime"]
    assert runtime["status"] == "pending_docker_direct_source_coverage"
    assert not runtime["applied"]
    with pytest.raises(DockerPolicyConflictError, match="docker_direct_source_coverage_incomplete"):
        render_owned_table_candidate(manifest)
