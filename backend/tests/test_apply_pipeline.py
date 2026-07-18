from __future__ import annotations

import json
from pathlib import Path

import pytest

from fwrouter_api.adapters.dataplane import NftOwnedTableAdapter
from fwrouter_api.adapters.mihomo import MihomoHealth, MihomoRuntimeState
from fwrouter_api.adapters.scripts import ScriptResult
from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import db_session, initialize_database
from fwrouter_api.jobs.manager import get_default_job_manager
from fwrouter_api.services import apply as apply_service
from fwrouter_api.services import dataplane_global as dataplane_global_service
from fwrouter_api.services.apply import ApplyMode, run_apply_pipeline
from fwrouter_api.services.dataplane_nft import promote_last_good, render_owned_table_candidate
from fwrouter_api.services.jobs import create_job
from fwrouter_api.services.live_probe_cache import clear_live_probe_cache
from fwrouter_api.services.routing_manifest import build_dataplane_manifest_from_state
from fwrouter_api.services.runtime_convergence import _reset_runtime_convergence_state_for_tests


FORBIDDEN_SCRIPT_PATTERNS = (
    "flush ruleset",
    "iptables-restore",
    "systemctl restart",
    "docker restart",
    "tailscale restart",
)


def _configure_env(monkeypatch, tmp_path: Path) -> None:
    get_default_job_manager().wait_for_idle()
    monkeypatch.setenv("FWROUTER_STATE_DIR", str(tmp_path / "state"))
    get_settings.cache_clear()
    clear_live_probe_cache()
    _reset_runtime_convergence_state_for_tests()


def _create_apply_job() -> dict[str, object]:
    return create_job(
        "apply_pipeline_test",
        lock_key="apply-pipeline-test",
        requested_by="pytest",
        input_data={"source": "pytest"},
    )


def _chain_block(candidate: str, chain_name: str) -> str:
    marker = f"    chain {chain_name} {{"
    start = candidate.index(marker)
    end = candidate.index("\n    }", start)
    return candidate[start:end + len("\n    }")]


def test_apply_pipeline_persists_running_phase_result(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    observed_results: list[dict[str, object] | None] = []

    monkeypatch.setattr(
        "fwrouter_api.services.apply.build_dataplane_manifest",
        lambda **kwargs: {
            "contract_version": "v1",
            "generated_at": "2026-01-01T00:00:00+00:00",
            "owned_table": "inet fwrouter_v2",
            "required_chains": [],
            "runtime_enforcement": {
                "dataplane_capability": "nft_owned_table",
                "enforcement_level": "owned_table_ready",
                "traffic_enforcement_guaranteed": False,
                "supported_modes": {"direct": True},
                "missing_runtime_requirements": [],
            },
            "routing_global_state": {"desired_mode": "direct", "applied_mode": "direct"},
            "summary": {"subjects_count": 0, "path_counts": {}, "extra_keys": []},
            "scoped_egress": {},
            "extra": {},
            "global_preflight": {"missing": [], "profile": {"profile": "test"}},
        },
    )
    monkeypatch.setattr(
        "fwrouter_api.services.apply.write_dataplane_manifest",
        lambda **kwargs: {
            "candidate_manifest_path": str(tmp_path / "candidate-manifest.json"),
            "versioned_manifest_path": str(tmp_path / "versioned-manifest.json"),
            "candidate_nft_path": str(tmp_path / "candidate.nft"),
            "snapshot_before_nft_path": str(tmp_path / "snapshot-before.nft"),
            "applied_manifest_path": str(tmp_path / "applied-manifest.json"),
            "current_manifest_path": str(tmp_path / "current-manifest.json"),
        },
    )

    class _FakeCheckResult:
        ok = True
        operation = type("Operation", (), {"value": "check"})()
        message = "check ok"
        error_code = None
        error_message = None
        details = {"stage": "check"}

    class _FakeAdapter:
        def check(self, plan):  # noqa: ANN001
            return _FakeCheckResult()

    monkeypatch.setattr("fwrouter_api.services.apply.DEFAULT_DATAPLANE_ADAPTER", _FakeAdapter())
    monkeypatch.setattr(
        "fwrouter_api.services.apply.update_job_running_result",
        lambda job_id, result=None: observed_results.append(result) or {"job_id": job_id, "result": result},
    )

    job = _create_apply_job()
    result = run_apply_pipeline(job_id=str(job["job_id"]), reason="pytest", mode=ApplyMode.DRY_RUN)
    assert result["ok"] is True
    assert observed_results
    assert observed_results[0]["job_status"] == "running"
    assert observed_results[0]["apply"]["events"][0]["phase"] == "render_candidate"


def test_apply_pipeline_writes_render_failure_artifact(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    job = _create_apply_job()

    def _boom(**kwargs):  # noqa: ANN001
        raise RuntimeError("render exploded")

    monkeypatch.setattr("fwrouter_api.services.apply.build_dataplane_manifest", _boom)

    with pytest.raises(RuntimeError, match="render exploded"):
        run_apply_pipeline(job_id=str(job["job_id"]), reason="pytest", mode=ApplyMode.DRY_RUN)

    result_path = get_settings().paths.generated_dir / "dataplane" / "last-result.json"
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    assert payload["stage"] == "render_candidate"
    assert payload["dataplane"]["error_code"] == "APPLY_RENDER_CANDIDATE_FAILED"


def test_apply_pipeline_accepts_prebuilt_manifest(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    job = _create_apply_job()

    prebuilt_manifest = {
        "contract_version": "v1",
        "generated_at": "stale",
        "owned_table": "inet fwrouter_v2",
        "required_chains": [],
        "runtime_enforcement": {
            "dataplane_capability": "nft_owned_table",
            "enforcement_level": "owned_table_ready",
            "traffic_enforcement_guaranteed": False,
            "supported_modes": {"direct": True},
            "missing_runtime_requirements": [],
        },
        "routing_global_state": {"desired_mode": "direct", "applied_mode": "direct"},
        "summary": {"subjects_count": 0, "path_counts": {}, "extra_keys": []},
        "scoped_egress": {},
        "extra": {},
        "global_preflight": {"missing": [], "profile": {"profile": "test"}},
    }
    captured: dict[str, object] = {}

    def _write_manifest(**kwargs):  # noqa: ANN001
        captured["manifest"] = kwargs["manifest"]
        return {
            "candidate_manifest_path": str(tmp_path / "candidate-manifest.json"),
            "versioned_manifest_path": str(tmp_path / "versioned-manifest.json"),
            "candidate_nft_path": str(tmp_path / "candidate.nft"),
            "snapshot_before_nft_path": str(tmp_path / "snapshot-before.nft"),
            "applied_manifest_path": str(tmp_path / "applied-manifest.json"),
            "current_manifest_path": str(tmp_path / "current-manifest.json"),
        }

    monkeypatch.setattr("fwrouter_api.services.apply.write_dataplane_manifest", _write_manifest)

    class _FakeCheckResult:
        ok = True
        operation = type("Operation", (), {"value": "check"})()
        message = "check ok"
        error_code = None
        error_message = None
        details = {"stage": "check"}

    class _FakeAdapter:
        def check(self, plan):  # noqa: ANN001
            return _FakeCheckResult()

    monkeypatch.setattr("fwrouter_api.services.apply.DEFAULT_DATAPLANE_ADAPTER", _FakeAdapter())

    result = run_apply_pipeline(
        job_id=str(job["job_id"]),
        reason="pytest-prebuilt",
        mode=ApplyMode.DRY_RUN,
        prebuilt_manifest=prebuilt_manifest,
    )

    assert result["ok"] is True
    manifest = captured["manifest"]
    assert manifest["reason"] == "pytest-prebuilt"
    assert manifest["input"] == {}
    assert manifest["generated_at"] != "stale"


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
                    "fwrouter_contours": {
                        "explicit_proxy": {"preserved": True},
                        "transparent_vpn": {
                            "ready": True,
                            "isolated_from_explicit_proxy": True,
                            "redir_port": 5202,
                            "tproxy_port": 5203,
                            "transparent_tcp_listener_present": True,
                            "transparent_udp_listener_present": True,
                            "transparent_tcp_ready": True,
                            "transparent_udp_ready": True,
                        },
                        "domain_selective": {
                            "ready": True,
                            "uses_transparent_contour": True,
                            "explicit_proxy_preserved": True,
                        },
                    },
                },
                "selectors": {
                    "vpn_global_exists": True,
                    "vpn_global_targets_count": 5,
                    "vpn_global_has_vpn_auto": True,
                    "vpn_global_now": "vpn-auto",
                    "vpn_auto_now": "some-server",
                },
                "transparent_runtime": {
                    "transparent_tcp_session_materialized": True,
                    "transparent_udp_session_materialized": True,
                },
            },
        )


class _FakeRunner:
    def __init__(self, responses: dict[str, list[ScriptResult]]) -> None:
        self.responses = {script_id: list(items) for script_id, items in responses.items()}
        self.calls: list[tuple[str, list[str]]] = []

    def run(self, script_id: str, *, extra_args: list[str] | None = None, timeout_seconds: int | None = None):  # noqa: ANN001
        del timeout_seconds
        args = list(extra_args or [])
        self.calls.append((script_id, args))
        queue = self.responses.get(script_id, [])
        if not queue:
            raise AssertionError(f"Unexpected script call: {script_id} {args}")
        return queue.pop(0)


def _script_result(
    script_id: str,
    *,
    returncode: int = 0,
    stdout_payload: dict[str, object] | None = None,
    stderr: str = "",
) -> ScriptResult:
    return ScriptResult(
        script_id=script_id,
        argv=(script_id,),
        returncode=returncode,
        stdout=json.dumps(stdout_payload or {}, ensure_ascii=False),
        stderr=stderr,
    )


def _success_check_result(*, table_exists: bool = False) -> ScriptResult:
    return _script_result(
        "dataplane_check",
        stdout_payload={
            "ok": True,
            "operation": "check",
            "stage": "check",
            "adapter": "nft-owned-table",
            "dataplane_capability": "nft_owned_table",
            "capability": "nft_owned_table",
            "enforcement_level": "owned_table_ready",
            "traffic_enforcement_guaranteed": False,
            "owned_table": "inet fwrouter_v2",
            "table_exists": table_exists,
            "required_chains": {
                "prerouting": table_exists,
                "input": table_exists,
                "output": table_exists,
                "forward": table_exists,
                "postrouting": table_exists,
                "fwrouter_classify": table_exists,
                "fwrouter_direct": table_exists,
                "fwrouter_vpn": table_exists,
                "fwrouter_vpn_full": table_exists,
            },
            "message": "check ok",
        },
    )


def _success_apply_result(previous_state: str = "missing") -> ScriptResult:
    return _script_result(
        "dataplane_apply",
        stdout_payload={
            "ok": True,
            "operation": "apply",
            "stage": "verify",
            "adapter": "nft-owned-table",
            "dataplane_capability": "nft_owned_table",
            "capability": "nft_owned_table",
            "enforcement_level": "owned_table_ready",
            "traffic_enforcement_guaranteed": False,
            "owned_table": "inet fwrouter_v2",
            "previous_table_state": previous_state,
            "table_exists": True,
            "required_chains": {
                "prerouting": True,
                "input": True,
                "output": True,
                "forward": True,
                "postrouting": True,
                "fwrouter_classify": True,
                "fwrouter_direct": True,
                "fwrouter_vpn": True,
                "fwrouter_vpn_full": True,
            },
            "message": "apply ok",
        },
    )


def _success_vpn_apply_result(previous_state: str = "missing") -> ScriptResult:
    return _script_result(
        "dataplane_apply",
        stdout_payload={
            "ok": True,
            "operation": "apply",
            "stage": "verify",
            "adapter": "nft-owned-table",
            "dataplane_capability": "nft_owned_table",
            "capability": "nft_owned_table",
            "enforcement_level": "owned_table_ready",
            "traffic_enforcement_guaranteed": False,
            "owned_table": "inet fwrouter_v2",
            "routing_mode": "vpn",
            "previous_table_state": previous_state,
            "table_exists": True,
            "required_chains": {
                "prerouting": True,
                "input": True,
                "output": True,
                "forward": True,
                "postrouting": True,
                "fwrouter_classify": True,
                "fwrouter_direct": True,
                "fwrouter_vpn": True,
                "fwrouter_vpn_full": True,
            },
            "vpn_contract_ready": True,
            "vpn_external_path_verified": True,
            "vpn_tproxy_port": 5203,
            "transparent_path": {
                "vpn_mark_packets": 12,
                "vpn_mark_tcp_packets": 12,
                "vpn_mark_udp_packets": 0,
                "redirect_handoff_tcp_packets": 12,
                "tproxy_handoff_udp_packets": 0,
                "tproxy_handoff_packets": 12,
                "transparent_flow_observed": True,
                "transparent_tcp_flow_observed": True,
                "transparent_udp_flow_observed": False,
            },
            "message": "vpn apply ok",
        },
    )


def _unverified_vpn_apply_result(previous_state: str = "missing") -> ScriptResult:
    return _script_result(
        "dataplane_apply",
        stdout_payload={
            "ok": True,
            "operation": "apply",
            "stage": "verify",
            "adapter": "nft-owned-table",
            "dataplane_capability": "nft_owned_table",
            "capability": "nft_owned_table",
            "enforcement_level": "owned_table_ready",
            "traffic_enforcement_guaranteed": False,
            "owned_table": "inet fwrouter_v2",
            "routing_mode": "vpn",
            "previous_table_state": previous_state,
            "table_exists": True,
            "required_chains": {
                "prerouting": True,
                "input": True,
                "output": True,
                "forward": True,
                "postrouting": True,
                "fwrouter_classify": True,
                "fwrouter_direct": True,
                "fwrouter_vpn": True,
                "fwrouter_vpn_full": True,
            },
            "vpn_contract_ready": True,
            "vpn_external_path_verified": False,
            "vpn_tproxy_port": 5202,
            "message": "vpn apply unverified",
        },
    )


def _failed_apply_result(stage: str = "apply") -> ScriptResult:
    return _script_result(
        "dataplane_apply",
        returncode=1,
        stdout_payload={
            "ok": False,
            "operation": "apply",
            "stage": stage,
            "adapter": "nft-owned-table",
            "error_code": "NFT_APPLY_FAILED",
            "error_message": "forced apply failure",
            "message": "forced apply failure",
        },
        stderr="forced apply failure",
    )


def _success_rollback_result(previous_state: str = "present") -> ScriptResult:
    return _script_result(
        "dataplane_rollback",
        stdout_payload={
            "ok": True,
            "operation": "rollback",
            "stage": "rollback",
            "adapter": "nft-owned-table",
            "dataplane_capability": "nft_owned_table",
            "capability": "nft_owned_table",
            "enforcement_level": "owned_table_ready",
            "traffic_enforcement_guaranteed": False,
            "owned_table": "inet fwrouter_v2",
            "previous_table_state": previous_state,
            "message": "rollback ok",
        },
    )


def _failed_rollback_result() -> ScriptResult:
    return _script_result(
        "dataplane_rollback",
        returncode=1,
        stdout_payload={
            "ok": False,
            "operation": "rollback",
            "stage": "rollback",
            "adapter": "nft-owned-table",
            "error_code": "NFT_ROLLBACK_RESTORE_FAILED",
            "error_message": "forced rollback failure",
            "message": "forced rollback failure",
        },
        stderr="forced rollback failure",
    )


def _enable_vpn_module() -> None:
    with db_session() as connection:
        connection.execute(
            "UPDATE modules SET desired_state = 'enabled' WHERE module_name = 'vpn'"
        )


def _live_mode_probe(mode: str, *, selective_default: str | None = None) -> dict[str, object]:
    return {
        "ok": True,
        "table_exists": True,
        "mode": mode,
        "selective_default": selective_default,
        "error_code": None,
        "error_message": None,
        "raw_chain": f"mock live mode: {mode}",
    }


def test_debug_db_state(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _enable_vpn_module()
    with db_session() as connection:
        rows = connection.execute("SELECT * FROM modules").fetchall()
        for row in rows:
            print(f"DEBUG MODULE: {dict(row)}")
    assert True


def test_dataplane_scripts_are_safe_owned_table_only(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    monorepo_root = Path(__file__).resolve().parents[2]
    legacy_root = Path(__file__).resolve().parents[3]
    script_root = monorepo_root / "host/libexec/fwrouter"
    if not script_root.exists():
        script_root = legacy_root / "usr/local/libexec/fwrouter"
    scripts = [
        script_root / "dataplane-check.sh",
        script_root / "dataplane-apply.sh",
        script_root / "dataplane-rollback.sh",
    ]
    common_script = script_root / "dataplane-common.sh"

    for script_path in scripts:
        content = script_path.read_text(encoding="utf-8")
        assert "fwrouter_v2" in content
        for forbidden in FORBIDDEN_SCRIPT_PATTERNS:
            assert forbidden not in content

    apply_content = scripts[1].read_text(encoding="utf-8")
    rollback_content = scripts[2].read_text(encoding="utf-8")
    check_content = scripts[0].read_text(encoding="utf-8")
    common_content = common_script.read_text(encoding="utf-8")

    assert 'nft -c -f "$CANDIDATE_PATH"' in check_content
    assert "nft delete table inet fwrouter_v2" in apply_content
    assert "nft delete table inet fwrouter_v2" in rollback_content
    assert "fwrouter_classify" in check_content
    assert "fwrouter_classify" in apply_content
    assert "fwrouter redirect handoff tcp:" in check_content
    assert "fwrouter tproxy handoff udp:" in check_content
    assert ". /usr/local/libexec/fwrouter/dataplane-common.sh" in check_content
    assert ". /usr/local/libexec/fwrouter/dataplane-common.sh" in apply_content
    assert "fwmark 0x0*" in common_content
    assert "summary.requires_vpn_policy_routing" in common_content
    assert 'resolve_vpn_policy_required "$CANDIDATE_PATH"' in apply_content
    assert 'resolve_vpn_policy_required "$CANDIDATE_PATH"' in check_content
    assert 'resolve_vpn_policy_required "$SNAPSHOT_PATH"' in rollback_content


def test_apply_pipeline_writes_candidate_artifacts(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _enable_vpn_module()
    job = _create_apply_job()
    fake_runner = _FakeRunner({"dataplane_check": [_success_check_result()]})
    monkeypatch.setattr(
        apply_service,
        "DEFAULT_DATAPLANE_ADAPTER",
        NftOwnedTableAdapter(runner=fake_runner),
    )

    result = run_apply_pipeline(
        job_id=str(job["job_id"]),
        reason="test",
        mode=ApplyMode.DRY_RUN,
        input_data={"source": "pytest"},
    )

    assert result["ok"] is True
    assert result["stage"] == "check"
    assert result["dataplane_capability"] == "nft_owned_table"
    assert result["enforcement_level"] == "owned_table_ready"
    assert result["traffic_enforcement_guaranteed"] is False
    assert result["supported_modes"]["direct"] is True
    assert isinstance(result["supported_modes"]["selective"], bool)
    assert isinstance(result["supported_modes"]["vpn"], bool)
    manifest_paths = result["manifest"]["paths"]
    assert Path(manifest_paths["candidate_nft_path"]).exists()
    assert Path(manifest_paths["candidate_manifest_path"]).exists()
    assert Path(manifest_paths["job_candidate_nft_path"]).exists()
    assert Path(manifest_paths["job_candidate_manifest_path"]).exists()
    assert Path(manifest_paths["job_check_stdout_path"]).exists()
    assert result["manifest"]["owned_table"] == "inet fwrouter_v2"
    assert result["manifest"]["required_chains"] == [
        "prerouting",
        "input",
        "output",
        "forward",
        "postrouting",
        "fwrouter_classify",
        "fwrouter_direct",
        "fwrouter_vpn",
        "fwrouter_vpn_full",
    ]

    candidate_manifest = json.loads(Path(manifest_paths["candidate_manifest_path"]).read_text(encoding="utf-8"))
    assert candidate_manifest["plan_id"] == result["apply_id"]
    assert candidate_manifest["runtime_enforcement"]["dataplane_capability"] == "nft_owned_table"
    assert candidate_manifest["runtime_enforcement"]["enforcement_level"] == "owned_table_ready"
    assert candidate_manifest["runtime_enforcement"]["traffic_enforcement_guaranteed"] is False
    assert candidate_manifest["owned_table"] == "inet fwrouter_v2"
    assert candidate_manifest["generated_at"]

    candidate_nft = Path(manifest_paths["candidate_nft_path"]).read_text(encoding="utf-8")
    assert "table inet fwrouter_v2" in candidate_nft
    assert "set protected_ipv4" in candidate_nft
    assert "set secure_dns_bypass_ipv4" in candidate_nft
    assert "chain fwrouter_classify" in candidate_nft
    assert "chain fwrouter_direct" in candidate_nft
    assert "chain fwrouter_vpn" in candidate_nft

    with db_session() as connection:
        apply_row = connection.execute(
            "SELECT job_id, status FROM apply_versions WHERE apply_id = ?",
            (result["apply_id"],),
        ).fetchone()

    assert apply_row is not None
    assert apply_row["job_id"] == job["job_id"]
    assert apply_row["status"] == "generated"
    assert fake_runner.calls == [
        ("dataplane_check", [manifest_paths["candidate_nft_path"], manifest_paths["candidate_manifest_path"]])
    ]


def test_apply_pipeline_hot_swaps_global_mode_classify_chain(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _enable_vpn_module()
    job = _create_apply_job()
    check_payload = json.loads(_success_check_result(table_exists=True).stdout)
    check_payload["vpn_contract_ready"] = True
    check_payload["vpn_external_path_verified"] = True
    fake_runner = _FakeRunner(
        {"dataplane_check": [_script_result("dataplane_check", stdout_payload=check_payload)]}
    )
    monkeypatch.setattr(
        apply_service,
        "DEFAULT_DATAPLANE_ADAPTER",
        NftOwnedTableAdapter(runner=fake_runner),
    )
    monkeypatch.setattr(dataplane_global_service, "DEFAULT_MIHOMO_ADAPTER", _ReadyMihomoAdapter())
    monkeypatch.setattr(
        apply_service,
        "probe_live_global_mode",
        lambda: {
            "ok": True,
            "mode": "selective",
            "selective_default": "direct",
            "error_code": None,
            "error_message": None,
        },
    )
    monkeypatch.setattr(
        apply_service,
        "reconcile_dnsmasq_rules",
        lambda: (_ for _ in ()).throw(AssertionError("dnsmasq reconcile must be skipped for classify hot-swap")),
    )
    observed_nft_payloads: list[str] = []

    class _Completed:
        returncode = 0
        stdout = ""
        stderr = ""

    def _fake_run(argv, *, check=False, capture_output=True, text=True):  # noqa: ANN001
        if argv[:4] == ["ip", "-json", "address", "show"]:
            completed = _Completed()
            completed.stdout = "[]"
            return completed
        if argv == ["nft", "list", "chain", "inet", "fwrouter_v2", "fwrouter_classify"]:
            completed = _Completed()
            completed.stdout = observed_nft_payloads[-1] if observed_nft_payloads else ""
            return completed
        assert argv[:2] == ["nft", "-f"]
        observed_nft_payloads.append(Path(argv[2]).read_text(encoding="utf-8"))
        assert check is False
        assert capture_output is True
        assert text is True
        return _Completed()

    monkeypatch.setattr(apply_service.subprocess, "run", _fake_run)

    result = run_apply_pipeline(
        job_id=str(job["job_id"]),
        reason="set_global_mode",
        mode=ApplyMode.APPLY,
        input_data={"intent": "set_global_mode", "mode": "selective"},
        manifest_state={
            "routing_global_state": {
                "desired_mode": "selective",
                "applied_mode": "selective",
                "selective_default": "direct",
                "server_mode": "auto",
                "desired_fixed_server_id": None,
                "applied_fixed_server_id": None,
                "active_auto_server_id": None,
            },
            "subjects": [],
            "extra": {
                "rules_effective": {
                    "selective_default": "direct",
                    "rules": [
                        {"action": "DIRECT", "kind": "domain", "value": "example.org"},
                        {"action": "VPN", "kind": "domain", "value": "example.com"},
                    ],
                }
            },
        },
    )

    assert result["ok"] is True
    assert result["dataplane"]["details"]["hot_swap"] is True
    assert result["dataplane"]["details"]["hot_swap_scope"] == "fwrouter_classify"
    assert observed_nft_payloads
    assert "flush chain inet fwrouter_v2 fwrouter_classify" in observed_nft_payloads[0]
    assert 'comment "selective direct IPv4"' in observed_nft_payloads[0]
    assert fake_runner.calls == [
        ("dataplane_check", [
            result["manifest"]["paths"]["candidate_nft_path"],
            result["manifest"]["paths"]["candidate_manifest_path"],
        ])
    ]


def test_apply_pipeline_requires_existing_job_for_fk(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    with pytest.raises(ValueError, match="existing jobs row"):
        run_apply_pipeline(
            job_id="missing-job",
            reason="fk-check",
            mode=ApplyMode.DRY_RUN,
            input_data={"source": "pytest"},
        )

    with db_session() as connection:
        count = connection.execute("SELECT COUNT(*) FROM apply_versions").fetchone()[0]

    assert count == 0


def test_render_owned_table_candidate_classifies_tailscale_exit_node_payload() -> None:
    manifest = {
        "summary": {
            "global_mode": "selective",
            "selective_default": "direct",
        },
        "global_preflight": {
            "vpn_contour": {
                "redir_port": 5202,
                "tproxy_port": 5203,
                "fwmark_hex": "0x00000100",
                "proxy_bypass_mark_hex": "0x00000200",
            },
            "selective_vpn_ready": True,
            "profile": {
                "mihomo": {
                    "contours": {
                        "selective_path_kind": "domain_aware",
                    }
                }
            },
        },
        "subjects": [],
        "extra": {
            "rules_effective": {
                "rules": [],
            }
        },
    }

    candidate = render_owned_table_candidate(manifest)

    assert 'iifname "tailscale0" accept comment "immunity: tailscale ingress"' not in candidate
    assert 'oifname "tailscale0" accept comment "immunity: tailscale egress"' in candidate


def test_render_owned_table_candidate_uses_auto_merge_for_interval_sets() -> None:
    manifest = {
        "summary": {
            "global_mode": "selective",
            "selective_default": "direct",
        },
        "global_preflight": {
            "vpn_contour": {
                "redir_port": 5202,
                "tproxy_port": 5203,
                "fwmark_hex": "0x00000100",
                "proxy_bypass_mark_hex": "0x00000200",
            },
            "selective_vpn_ready": True,
            "selective_degraded": False,
            "profile": {
                "mihomo": {
                    "contours": {
                        "selective_path_kind": "domain_aware",
                    }
                }
            },
        },
        "subjects": [],
        "extra": {
            "rules_effective": {
                "rules": [
                    {"action": "VPN", "kind": "cidr", "value": "157.240.205.174/32"},
                    {"action": "VPN", "kind": "cidr", "value": "157.240.205.0/24"},
                    {"action": "DIRECT", "kind": "cidr", "value": "188.40.167.82/32"},
                    {"action": "DIRECT", "kind": "cidr", "value": "188.40.167.0/24"},
                ],
            }
        },
    }

    candidate = render_owned_table_candidate(manifest)

    assert "set vpn_ipv4 {" in candidate
    assert "set direct_ipv4 {" in candidate
    assert "set dns_vpn_ipv4 {" in candidate
    assert "set dns_direct_ipv4 {" in candidate
    assert "flags interval, timeout;" in candidate
    assert "timeout 3600s;" in candidate
    assert 'ip daddr @dns_vpn_ipv4 goto fwrouter_vpn comment "selective dns vpn IPv4"' in candidate
    assert 'ip daddr @dns_direct_ipv4 goto fwrouter_direct comment "selective dns direct IPv4"' in candidate
    assert candidate.count("auto-merge;") >= 2


def test_render_owned_table_candidate_includes_cloudflare_secure_dns_bypass_endpoints() -> None:
    manifest = {
        "summary": {
            "global_mode": "selective",
            "selective_default": "direct",
        },
        "global_preflight": {
            "vpn_contour": {
                "redir_port": 5202,
                "tproxy_port": 5203,
                "fwmark_hex": "0x00000100",
                "proxy_bypass_mark_hex": "0x00000200",
            },
            "profile": {
                "mihomo": {
                    "contours": {
                        "selective_path_kind": "domain_aware",
                    }
                }
            },
        },
        "subjects": [],
        "extra": {
            "rules_effective": {
                "rules": [],
            }
        },
    }

    candidate = render_owned_table_candidate(manifest)

    assert "172.64.41.3" in candidate
    assert "172.64.41.4" in candidate
    assert "162.159.61.3" in candidate
    assert "162.159.61.4" in candidate


def test_render_owned_table_candidate_ignores_rules_effective_summary(monkeypatch) -> None:
    monkeypatch.setattr(
        "fwrouter_api.services.dataplane_nft.read_effective_rules_artifact",
        lambda: {
            "selective_default": "direct",
            "rules": [
                {"action": "VPN", "kind": "cidr", "value": "203.0.113.0/24"},
            ],
        },
    )
    manifest = {
        "summary": {
            "global_mode": "selective",
            "selective_default": "direct",
        },
        "global_preflight": {
            "vpn_contour": {
                "redir_port": 5202,
                "tproxy_port": 5203,
                "fwmark_hex": "0x00000100",
                "proxy_bypass_mark_hex": "0x00000200",
            },
            "profile": {
                "mihomo": {
                    "contours": {
                        "selective_path_kind": "domain_aware",
                    }
                }
            },
        },
        "subjects": [],
        "extra": {
            "rules_effective_summary": {
                "selective_default": "direct",
                "effective_counts": {"cidr": 1},
            }
        },
    }

    candidate = render_owned_table_candidate(manifest)

    assert "203.0.113.0/24" in candidate


def test_render_owned_table_candidate_is_pure_without_runtime_discovery(monkeypatch) -> None:
    manifest = {
        "summary": {
            "global_mode": "selective",
            "selective_default": "direct",
        },
        "global_preflight": {
            "vpn_contour": {
                "redir_port": 5202,
                "tproxy_port": 5203,
                "fwmark_hex": "0x00000100",
                "proxy_bypass_mark_hex": "0x00000200",
            },
            "selective_vpn_ready": True,
            "selective_degraded": False,
            "profile": {
                "mihomo": {
                    "contours": {
                        "selective_path_kind": "domain_aware",
                    }
                }
            },
        },
        "subjects": [],
        "extra": {
            "rules_effective": {
                "selective_default": "direct",
                "rules": [],
                "direct_ipv4": ["198.51.100.7"],
                "direct_ipv6": [],
                "vpn_ipv4": ["203.0.113.8"],
                "vpn_ipv6": [],
                "protected_ipv4": ["10.0.0.0/8"],
                "protected_ipv6": ["fc00::/7"],
            },
            "infrastructure_ipv4": ["172.18.0.0/16"],
            "secure_dns_bypass_ipv4": ["1.1.1.1"],
        },
    }

    def _unexpected(*args, **kwargs):  # noqa: ANN001
        raise AssertionError("render_owned_table_candidate must not perform runtime discovery")

    monkeypatch.setattr("socket.getaddrinfo", _unexpected)
    monkeypatch.setattr("subprocess.run", _unexpected)

    candidate = render_owned_table_candidate(manifest)

    assert "172.18.0.0/16" in candidate
    assert "1.1.1.1" in candidate


def test_render_owned_table_candidate_allows_lan_dns_capture_before_scoped_vpn() -> None:
    manifest = {
        "summary": {
            "global_mode": "direct",
            "selective_default": "direct",
            "requires_vpn_policy_routing": True,
        },
        "global_preflight": {
            "vpn_contour": {
                "redir_port": 5202,
                "tproxy_port": 5203,
                "fwmark_hex": "0x00000100",
                "proxy_bypass_mark_hex": "0x00000200",
            },
            "selective_vpn_ready": True,
            "selective_degraded": False,
            "dnsmasq_selective_status": {
                "router_dns_interfaces": ["enp2s0"],
            },
            "profile": {
                "mihomo": {
                    "contours": {
                        "selective_path_kind": "domain_aware",
                    }
                }
            },
        },
        "subjects": [
            {
                "subject_id": "lan:pixel",
                "subject_type": "lan",
                "display_name": "Pixel",
                "desired_mode": "vpn",
                "applied_mode": "vpn",
                "runtime_state": "active",
                "is_active": True,
                "dataplane_path": "vpn",
                "selected_server_id": "vpn-global",
                "selected_server_source": "vpn_auto",
                "scoped_runtime": {
                    "matcher": {
                        "family": "ipv4",
                        "nft_expr": "ip saddr",
                        "value": "192.168.0.71",
                    }
                },
            }
        ],
        "extra": {
            "rules_effective": {
                "selective_default": "direct",
                "rules": [],
                "direct_ipv4": [],
                "direct_ipv6": [],
                "vpn_ipv4": [],
                "vpn_ipv6": [],
                "protected_ipv4": ["192.168.0.0/16"],
                "protected_ipv6": ["fc00::/7"],
            },
        },
    }

    candidate = render_owned_table_candidate(manifest)

    dns_capture = 'iifname "enp2s0" meta l4proto { tcp, udp } th dport 53 accept comment "allow LAN DNS capture before VPN classify enp2s0"'
    vpn_override = 'ip saddr 192.168.0.71 goto fwrouter_vpn_full comment "scoped vpn override: lan:pixel"'
    assert dns_capture in candidate
    assert vpn_override in candidate
    assert candidate.index(dns_capture) < candidate.index(vpn_override)


def test_render_owned_table_candidate_uses_port_only_tproxy_target() -> None:
    manifest = {
        "summary": {
            "global_mode": "selective",
            "selective_default": "direct",
            "requires_vpn_policy_routing": True,
        },
        "global_preflight": {
            "vpn_contour": {
                "redir_port": 5202,
                "tproxy_port": 5203,
                "fwmark_hex": "0x00000100",
                "proxy_bypass_mark_hex": "0x00000200",
            },
            "selective_vpn_ready": True,
            "selective_degraded": False,
            "profile": {
                "mihomo": {
                    "contours": {
                        "selective_path_kind": "domain_aware",
                    }
                }
            },
        },
        "subjects": [],
        "extra": {
            "rules_effective": {
                "selective_default": "direct",
                "rules": [],
            }
        },
    }

    candidate = render_owned_table_candidate(manifest)

    assert "redirect to :5202" in candidate
    assert "tproxy to :5203" in candidate


def test_render_owned_table_candidate_uses_split_redir_and_udp_tproxy_contract() -> None:
    manifest = {
        "summary": {
            "global_mode": "selective",
            "selective_default": "direct",
            "requires_vpn_policy_routing": True,
        },
        "global_preflight": {
            "vpn_contour": {
                "redir_port": 5202,
                "tproxy_port": 5203,
                "fwmark_hex": "0x00000100",
                "proxy_bypass_mark_hex": "0x00000200",
            },
            "selective_vpn_ready": True,
            "selective_degraded": False,
            "profile": {
                "mihomo": {
                    "contours": {
                        "selective_path_kind": "domain_aware",
                    }
                }
            },
        },
        "subjects": [],
        "extra": {
            "rules_effective": {
                "selective_default": "direct",
                "rules": [],
            }
        },
    }

    candidate = render_owned_table_candidate(manifest)

    assert "redirect to :5202" in candidate
    assert "tproxy to :5203" in candidate


def test_render_owned_table_candidate_keeps_decision_logic_in_classify_chain() -> None:
    manifest = {
        "summary": {
            "global_mode": "selective",
            "selective_default": "direct",
        },
        "global_preflight": {
            "vpn_contour": {
                "tproxy_port": 5202,
                "fwmark_hex": "0x00000100",
                "proxy_bypass_mark_hex": "0x00000200",
            },
            "selective_vpn_ready": True,
            "selective_degraded": False,
            "profile": {
                "mihomo": {
                    "contours": {
                        "selective_path_kind": "domain_aware",
                    }
                }
            },
        },
        "subjects": [
            {
                "subject_id": "sub-1",
                "subject_type": "docker",
                "display_name": "docker-1",
                "is_active": True,
                "dataplane_path": "selective",
                "scoped_runtime": {
                    "matcher": {
                        "nft_expr": "ip saddr",
                        "value": "10.10.10.10",
                        "family": "ipv4",
                    }
                },
            }
        ],
        "extra": {
            "rules_effective": {
                "rules": [
                    {"action": "DIRECT", "kind": "cidr", "value": "198.51.100.0/24"},
                    {"action": "VPN", "kind": "cidr", "value": "203.0.113.0/24"},
                ],
            }
        },
    }

    candidate = render_owned_table_candidate(manifest)
    classify_chain = _chain_block(candidate, "fwrouter_classify")

    assert 'scoped selective direct IPv4: sub-1' in classify_chain
    assert 'scoped selective vpn IPv4: sub-1' in classify_chain
    assert 'selective default direct' in classify_chain
    assert 'meta mark set 0x00000100' not in classify_chain
    assert 'tproxy to :5202' not in classify_chain


def test_render_owned_table_candidate_marks_vpn_policy_required_for_scoped_selective_in_direct_mode() -> None:
    manifest = {
        "summary": {
            "global_mode": "direct",
            "selective_default": "direct",
            "requires_vpn_policy_routing": True,
        },
        "global_preflight": {
            "vpn_policy_required": True,
            "vpn_contour": {
                "tproxy_port": 5202,
                "fwmark_hex": "0x00000100",
                "proxy_bypass_mark_hex": "0x00000200",
            },
            "selective_vpn_ready": True,
            "selective_degraded": False,
            "selective_rules": {
                "requires_vpn_runtime": True,
            },
            "profile": {
                "mihomo": {
                    "contours": {
                        "selective_path_kind": "domain_aware",
                    }
                }
            },
        },
        "subjects": [
            {
                "subject_id": "lan-71",
                "subject_type": "lan",
                "display_name": "Pixel",
                "is_active": True,
                "dataplane_path": "selective",
                "scoped_runtime": {
                    "matcher": {
                        "nft_expr": "ip saddr",
                        "value": "192.168.0.71",
                        "family": "ipv4",
                    }
                },
            }
        ],
        "extra": {
            "rules_effective": {
                "rules": [
                    {"action": "VPN", "kind": "cidr", "value": "157.240.205.0/24"},
                ],
            }
        },
    }

    candidate = render_owned_table_candidate(manifest)

    assert 'scoped selective vpn IPv4: lan-71' in candidate
    assert 'fwrouter vpn policy contract required v1' in candidate


def test_build_dataplane_manifest_requires_vpn_policy_routing_for_scoped_selective_in_direct_mode(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "fwrouter_api.services.routing_manifest.build_global_preflight",
        lambda **kwargs: {
            "can_enforce_global_direct": True,
            "can_enforce_global_selective": True,
            "can_enforce_global_vpn": True,
            "missing": [],
            "profile": {
                "profile": "test",
                "vpn_routing_contract": {
                    "tproxy_port": 5202,
                    "fwmark_hex": "0x00000100",
                    "routing_table_id": 100,
                },
            },
            "vpn_contour": {
                "tproxy_port": 5202,
                "fwmark_hex": "0x00000100",
                "routing_table_id": 100,
            },
            "selective_vpn_ready": True,
            "selective_degraded": False,
            "selective_rules": {
                "requires_vpn_runtime": True,
                "selective_default": "direct",
            },
        },
    )

    manifest = build_dataplane_manifest_from_state(
        plan_id="plan-1",
        reason="pytest",
        routing={
            "desired_mode": "direct",
            "applied_mode": "direct",
            "selective_default": "direct",
            "server_mode": "auto",
            "desired_fixed_server_id": None,
            "applied_fixed_server_id": None,
            "active_auto_server_id": None,
        },
        subjects=[
            {
                "subject_id": "lan:71",
                "subject_type": "lan",
                "display_name": "Pixel 9",
                "desired_mode": "selective",
                "applied_mode": "selective",
                "runtime_state": "running",
                "is_active": True,
                "effective_state": {
                    "effective_mode": "selective",
                    "mode_source": "admin",
                    "dataplane_path": "selective",
                    "selected_server_id": None,
                    "selected_server_source": "global_auto",
                    "runtime_enforcement": {},
                    "scoped_runtime": {
                        "state": "applied",
                        "eligible": True,
                        "applied": True,
                        "matcher": {
                            "nft_expr": "ip saddr",
                            "value": "192.168.0.71",
                            "family": "ipv4",
                        },
                    },
                },
            }
        ],
        extra={"rules_effective": {"rules": [{"action": "VPN", "kind": "domain", "value": "instagram.com"}]}},
    )

    assert manifest["summary"]["requires_vpn_policy_routing"] is True
    assert manifest["global_preflight"]["vpn_policy_required"] is True
    assert manifest["vpn_contour"]["required"] is True


def test_build_dataplane_manifest_recomputes_subject_effective_state_from_planned_runtime(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "fwrouter_api.services.routing_manifest.build_global_preflight",
        lambda **kwargs: {
            "can_enforce_global_direct": True,
            "can_enforce_global_selective": True,
            "can_enforce_global_vpn": True,
            "missing": [],
            "profile": {
                "profile": "test",
                "vpn_routing_contract": {
                    "redir_port": 5202,
                    "tproxy_port": 5203,
                    "fwmark_hex": "0x00000100",
                    "routing_table_id": 100,
                },
            },
            "vpn_contour": {
                "redir_port": 5202,
                "tproxy_port": 5203,
                "fwmark_hex": "0x00000100",
                "routing_table_id": 100,
            },
            "selective_vpn_ready": True,
            "selective_degraded": False,
            "selective_rules": {
                "requires_vpn_runtime": True,
                "selective_default": "direct",
                "path_kind": "domain_aware",
            },
        },
    )
    monkeypatch.setattr(
        "fwrouter_api.services.subject_policy.build_scoped_subject_runtime",
        lambda subject, **kwargs: {
            "state": "applied",
            "eligible": True,
            "applied": True,
            "reason": "subject_selective_runtime_materialized",
            "required_capability": None,
            "matcher": {"nft_expr": "ip saddr", "value": "192.168.0.71", "family": "ipv4"},
        },
    )

    manifest = build_dataplane_manifest_from_state(
        plan_id="plan-recompute",
        reason="pytest",
        routing={
            "desired_mode": "direct",
            "applied_mode": "direct",
            "selective_default": "direct",
            "server_mode": "auto",
            "desired_fixed_server_id": None,
            "applied_fixed_server_id": None,
            "active_auto_server_id": None,
        },
        subjects=[
            {
                "subject_id": "lan:71",
                "subject_type": "lan",
                "display_name": "Pixel 9",
                "desired_mode": "selective",
                "applied_mode": "selective",
                "runtime_state": "active",
                "is_active": True,
                "effective_state": {
                    "effective_mode": "selective",
                    "mode_source": "admin_locked",
                    "dataplane_path": "direct",
                    "selected_server_id": None,
                    "selected_server_source": "direct",
                    "runtime_enforcement": {
                        "supported_modes": {"direct": True, "selective": False, "vpn": True},
                    },
                    "scoped_runtime": {
                        "state": None,
                        "eligible": False,
                        "applied": False,
                    },
                },
            }
        ],
        extra={"rules_effective": {"rules": [{"action": "VPN", "kind": "domain", "value": "instagram.com"}]}},
    )

    subject_entry = manifest["subjects"][0]
    assert subject_entry["effective_mode"] == "selective"
    assert subject_entry["dataplane_path"] == "selective"
    assert subject_entry["enforcement"]["supported_modes"]["selective"] is True


def test_build_dataplane_manifest_does_not_require_vpn_policy_routing_for_pure_direct(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "fwrouter_api.services.routing_manifest.build_global_preflight",
        lambda **kwargs: {
            "can_enforce_global_direct": True,
            "can_enforce_global_selective": True,
            "can_enforce_global_vpn": True,
            "missing": [],
            "profile": {
                "profile": "test",
                "vpn_routing_contract": {
                    "tproxy_port": 5202,
                    "fwmark_hex": "0x00000100",
                    "routing_table_id": 100,
                },
            },
            "vpn_contour": {
                "tproxy_port": 5202,
                "fwmark_hex": "0x00000100",
                "routing_table_id": 100,
            },
            "selective_vpn_ready": True,
            "selective_degraded": False,
            "selective_rules": {
                "requires_vpn_runtime": False,
                "selective_default": "direct",
            },
        },
    )

    manifest = build_dataplane_manifest_from_state(
        plan_id="plan-2",
        reason="pytest",
        routing={
            "desired_mode": "direct",
            "applied_mode": "direct",
            "selective_default": "direct",
            "server_mode": "auto",
            "desired_fixed_server_id": None,
            "applied_fixed_server_id": None,
            "active_auto_server_id": None,
        },
        subjects=[
            {
                "subject_id": "lan:72",
                "subject_type": "lan",
                "display_name": "Direct host",
                "desired_mode": "direct",
                "applied_mode": "direct",
                "runtime_state": "running",
                "is_active": True,
                "effective_state": {
                    "effective_mode": "direct",
                    "mode_source": "admin",
                    "dataplane_path": "direct",
                    "selected_server_id": None,
                    "selected_server_source": "direct",
                    "runtime_enforcement": {},
                    "scoped_runtime": {},
                },
            }
        ],
        extra={"rules_effective": {"rules": []}},
    )

    assert manifest["summary"]["requires_vpn_policy_routing"] is False
    assert manifest["global_preflight"]["vpn_policy_required"] is False
    assert manifest["vpn_contour"] is None


def test_build_dataplane_manifest_ignores_xray_forced_vpn_for_transparent_policy_requirement(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "fwrouter_api.services.routing_manifest.build_global_preflight",
        lambda **kwargs: {
            "can_enforce_global_direct": True,
            "can_enforce_global_selective": True,
            "can_enforce_global_vpn": True,
            "missing": [],
            "profile": {
                "profile": "test",
                "vpn_routing_contract": {
                    "redir_port": 5202,
                    "tproxy_port": 5203,
                    "fwmark_hex": "0x00000100",
                    "routing_table_id": 100,
                },
            },
            "vpn_contour": {
                "redir_port": 5202,
                "tproxy_port": 5203,
                "fwmark_hex": "0x00000100",
                "routing_table_id": 100,
            },
            "selective_vpn_ready": True,
            "selective_degraded": False,
            "selective_rules": {
                "requires_vpn_runtime": False,
                "selective_default": "direct",
            },
        },
    )

    manifest = build_dataplane_manifest_from_state(
        plan_id="plan-xray-only",
        reason="pytest",
        routing={
            "desired_mode": "direct",
            "applied_mode": "direct",
            "selective_default": "direct",
            "server_mode": "auto",
            "desired_fixed_server_id": None,
            "applied_fixed_server_id": None,
            "active_auto_server_id": None,
        },
        subjects=[
            {
                "subject_id": "xray:test-client",
                "subject_type": "xray",
                "display_name": "Subscription client",
                "desired_mode": "enabled",
                "applied_mode": None,
                "runtime_state": "active",
                "is_active": True,
                "effective_state": {
                    "effective_mode": "forced_vpn",
                    "mode_source": "xray_runtime",
                    "dataplane_path": "vpn",
                    "selected_server_id": "vpn-auto",
                    "selected_server_source": "xray_binding",
                    "runtime_enforcement": {},
                    "scoped_runtime": {},
                },
            }
        ],
        extra={"rules_effective": {"rules": []}},
    )

    assert manifest["summary"]["requires_vpn_policy_routing"] is False
    assert manifest["global_preflight"]["vpn_policy_required"] is False
    assert manifest["vpn_contour"] is None


def test_render_owned_table_candidate_does_not_infer_transparent_vpn_requirement_from_xray_forced_vpn_only() -> None:
    manifest = {
        "summary": {
            "global_mode": "direct",
            "selective_default": "direct",
            "requires_vpn_policy_routing": False,
        },
        "global_preflight": {
            "vpn_policy_required": False,
            "vpn_contour": {
                "redir_port": 5202,
                "tproxy_port": 5203,
                "fwmark_hex": "0x00000100",
                "proxy_bypass_mark_hex": "0x00000200",
            },
            "selective_vpn_ready": True,
            "selective_degraded": False,
            "selective_rules": {
                "requires_vpn_runtime": False,
                "selective_default": "direct",
            },
            "profile": {
                "mihomo": {
                    "contours": {
                        "selective_path_kind": "domain_aware",
                    }
                }
            },
        },
        "subjects": [
            {
                "subject_id": "xray:test-client",
                "subject_type": "xray",
                "is_active": True,
                "dataplane_path": "vpn",
                "effective_state": {
                    "effective_mode": "forced_vpn",
                    "dataplane_path": "vpn",
                },
                "scoped_runtime": {},
            }
        ],
    }

    candidate = render_owned_table_candidate(manifest)

    assert "fwrouter vpn policy contract required v1" not in candidate


def test_render_owned_table_candidate_keeps_direct_and_vpn_terminal_chains_separate() -> None:
    manifest = {
        "summary": {
            "global_mode": "vpn",
            "selective_default": "direct",
        },
        "global_preflight": {
            "vpn_contour": {
                "tproxy_port": 5202,
                "fwmark_hex": "0x00000100",
                "proxy_bypass_mark_hex": "0x00000200",
            },
            "dnsmasq_selective_status": {
                "router_dns_interfaces": ["enp2s0"],
            },
            "profile": {
                "mihomo": {
                    "contours": {
                        "selective_path_kind": "ip_only",
                    }
                }
            },
        },
        "subjects": [],
        "extra": {"rules_effective": {"rules": []}},
    }

    candidate = render_owned_table_candidate(manifest)
    direct_chain = _chain_block(candidate, "fwrouter_direct")
    vpn_chain = _chain_block(candidate, "fwrouter_vpn")

    assert 'meta mark set 0x00000100' not in direct_chain
    assert 'tproxy to :5202' not in direct_chain
    assert 'global direct path' in direct_chain
    assert 'meta mark set 0x00000100' in vpn_chain
    assert 'skip mihomo outbound recapture' in vpn_chain


def test_render_owned_table_candidate_counts_vpn_rx_only_for_proxy_marked_output() -> None:
    manifest = {
        "summary": {
            "global_mode": "direct",
            "selective_default": "direct",
        },
        "global_preflight": {
            "vpn_contour": {
                "redir_port": 5202,
                "tproxy_port": 5203,
                "fwmark_hex": "0x00000100",
                "proxy_bypass_mark_hex": "0x00000200",
            },
            "profile": {
                "mihomo": {
                    "contours": {
                        "selective_path_kind": "domain_aware",
                    }
                }
            },
        },
        "subjects": [
            {
                "subject_id": "lan:aa-bb",
                "subject_type": "lan",
                "is_active": True,
                "dataplane_path": "direct",
                "scoped_runtime": {
                    "matcher": {
                        "family": "ipv4",
                        "nft_expr": "ip saddr",
                        "value": "192.168.0.10",
                    }
                },
            }
        ],
        "extra": {"rules_effective": {"rules": []}},
    }

    candidate = render_owned_table_candidate(manifest)
    output_chain = _chain_block(candidate, "output")
    forward_chain = _chain_block(candidate, "forward")

    assert 'ip daddr 192.168.0.10 counter name "cnt_lan_aa_bb_direct_rx"' in forward_chain
    assert 'meta mark 0x00000200 ip daddr 192.168.0.10 counter name "cnt_lan_aa_bb_vpn_rx"' in output_chain
    assert '\n        ip daddr 192.168.0.10 counter name "cnt_lan_aa_bb_vpn_rx"' not in output_chain


def test_render_owned_table_candidate_places_immunity_before_classify_capture() -> None:
    manifest = {
        "summary": {
            "global_mode": "selective",
            "selective_default": "direct",
        },
        "global_preflight": {
            "vpn_contour": {
                "tproxy_port": 5202,
                "fwmark_hex": "0x00000100",
                "proxy_bypass_mark_hex": "0x00000200",
            },
            "dnsmasq_selective_status": {
                "router_dns_interfaces": ["enp2s0"],
            },
            "profile": {
                "mihomo": {
                    "contours": {
                        "selective_path_kind": "domain_aware",
                    }
                }
            },
        },
        "subjects": [],
        "extra": {"rules_effective": {"rules": []}},
    }

    candidate = render_owned_table_candidate(manifest)
    prerouting_chain = _chain_block(candidate, "prerouting")

    assert prerouting_chain.index('socket transparent 1 accept comment "immunity: established tproxy sessions"') < prerouting_chain.index('jump fwrouter_classify comment "FWRouter global classify"')
    assert prerouting_chain.index('ip saddr @infrastructure_ipv4 accept comment "immunity: infrastructure outbound"') < prerouting_chain.index('jump fwrouter_classify comment "FWRouter global classify"')
    assert prerouting_chain.index('block IPv6 from LAN ingress enp2s0') < prerouting_chain.index('jump fwrouter_classify comment "FWRouter global classify"')
    assert prerouting_chain.index('reject secure DNS bypass TCP from LAN') < prerouting_chain.index('jump fwrouter_classify comment "FWRouter global classify"')


def test_promote_last_good_syncs_current_and_applied_nft_artifacts(tmp_path: Path) -> None:
    candidate_path = tmp_path / "candidate.nft"
    current_path = tmp_path / "current.nft"
    applied_path = tmp_path / "applied.nft"
    last_good_path = tmp_path / "last-good.nft"
    current_manifest_path = tmp_path / "current-manifest.json"
    applied_manifest_path = tmp_path / "applied-manifest.json"
    last_good_manifest_path = tmp_path / "last-good-manifest.json"

    candidate_text = "table inet fwrouter_v2 {}\n"
    candidate_path.write_text(candidate_text, encoding="utf-8")

    manifest = {
        "routing_global_state": {"desired_mode": "selective", "applied_mode": "selective"},
        "summary": {"global_mode": "selective"},
    }
    promote_last_good(
        manifest=manifest,
        artifact_paths={
            "candidate_nft_path": str(candidate_path),
            "current_nft_path": str(current_path),
            "applied_nft_path": str(applied_path),
            "last_good_nft_path": str(last_good_path),
            "current_manifest_path": str(current_manifest_path),
            "applied_manifest_path": str(applied_manifest_path),
            "last_good_manifest_path": str(last_good_manifest_path),
        },
    )

    assert current_path.read_text(encoding="utf-8") == candidate_text
    assert applied_path.read_text(encoding="utf-8") == candidate_text
    assert last_good_path.read_text(encoding="utf-8") == candidate_text
    assert json.loads(current_manifest_path.read_text(encoding="utf-8"))["summary"]["global_mode"] == "selective"
    assert json.loads(applied_manifest_path.read_text(encoding="utf-8"))["summary"]["global_mode"] == "selective"


def test_apply_pipeline_apply_mode_records_owned_table_success(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _enable_vpn_module()
    job = _create_apply_job()
    fake_runner = _FakeRunner(
        {
            "dataplane_check": [_success_check_result(table_exists=False)],
            "dataplane_apply": [_success_apply_result("missing")],
        }
    )
    monkeypatch.setattr(
        apply_service,
        "DEFAULT_DATAPLANE_ADAPTER",
        NftOwnedTableAdapter(runner=fake_runner),
    )
    monkeypatch.setattr(apply_service, "probe_live_global_mode", lambda: _live_mode_probe("direct"))

    result = run_apply_pipeline(
        job_id=str(job["job_id"]),
        reason="apply-attempt",
        mode=ApplyMode.APPLY,
        input_data={"source": "pytest"},
    )

    assert result["ok"] is True
    assert result["job_id"] == job["job_id"]
    assert result["dataplane_capability"] == "global_policy_v1"
    assert result["enforcement_level"] == "global_direct_enforced"
    assert result["traffic_enforcement_guaranteed"] is True
    assert result["rollback"] is None
    assert result["stage"] == "verify"

    manifest_paths = result["manifest"]["paths"]
    assert Path(manifest_paths["last_good_nft_path"]).exists()
    assert Path(manifest_paths["last_good_manifest_path"]).exists()
    assert Path(manifest_paths["applied_manifest_path"]).exists()
    assert Path(manifest_paths["current_manifest_path"]).exists()
    assert Path(manifest_paths["snapshot_candidate_nft_path"]).exists()
    assert Path(manifest_paths["snapshot_manifest_path"]).exists()
    assert Path(manifest_paths["job_apply_stdout_path"]).exists()

    assert fake_runner.calls == [
        ("dataplane_check", [manifest_paths["candidate_nft_path"], manifest_paths["candidate_manifest_path"]]),
        (
            "dataplane_apply",
            [
                manifest_paths["candidate_nft_path"],
                manifest_paths["candidate_manifest_path"],
                manifest_paths["snapshot_before_nft_path"],
                manifest_paths["snapshot_state_path"],
            ],
        ),
    ]

    with db_session() as connection:
        row = connection.execute(
            "SELECT job_id, status, summary_json FROM apply_versions WHERE apply_id = ?",
            (result["apply_id"],),
        ).fetchone()

    assert row is not None
    assert row["job_id"] == job["job_id"]
    assert row["status"] == "applied"
    assert "inet fwrouter_v2" in str(row["summary_json"])
    assert "global_policy_v1" in str(row["summary_json"])
    assert json.loads(Path(manifest_paths["current_manifest_path"]).read_text(encoding="utf-8"))["plan_id"] == result["apply_id"]


def test_apply_pipeline_retries_transient_live_mode_probe_before_rollback(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _enable_vpn_module()
    job = _create_apply_job()
    fake_runner = _FakeRunner(
        {
            "dataplane_check": [_success_check_result(table_exists=False)],
            "dataplane_apply": [_success_apply_result("missing")],
        }
    )
    monkeypatch.setattr(
        apply_service,
        "DEFAULT_DATAPLANE_ADAPTER",
        NftOwnedTableAdapter(runner=fake_runner),
    )
    probes = iter(
        [
            _live_mode_probe("unknown"),
            _live_mode_probe("direct"),
        ]
    )
    monkeypatch.setattr(apply_service, "probe_live_global_mode", lambda: next(probes))
    monkeypatch.setattr(apply_service.time, "sleep", lambda _seconds: None)

    result = run_apply_pipeline(
        job_id=str(job["job_id"]),
        reason="apply-transient-live-probe",
        mode=ApplyMode.APPLY,
        input_data={"source": "pytest"},
    )

    assert result["ok"] is True
    assert result["rollback"] is None
    assert result["dataplane"]["details"]["active_mode_matches_intent"] is True
    assert result["dataplane"]["details"]["live_mode_probe_retries"] == [
        {
            "attempt": 1,
            "ok": True,
            "mode": "direct",
            "selective_default": None,
            "error_code": None,
            "error_message": None,
        }
    ]
    assert [call[0] for call in fake_runner.calls] == ["dataplane_check", "dataplane_apply"]


def test_apply_pipeline_apply_failure_invokes_owned_table_rollback(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _enable_vpn_module()
    job = _create_apply_job()
    fake_runner = _FakeRunner(
        {
            "dataplane_check": [_success_check_result(table_exists=True)],
            "dataplane_apply": [_failed_apply_result("apply")],
            "dataplane_rollback": [_success_rollback_result("present")],
        }
    )
    monkeypatch.setattr(
        apply_service,
        "DEFAULT_DATAPLANE_ADAPTER",
        NftOwnedTableAdapter(runner=fake_runner),
    )

    result = apply_service.run_apply_pipeline(
        job_id=str(job["job_id"]),
        reason="apply-failure-needs-rollback",
        mode=ApplyMode.APPLY,
        input_data={"source": "pytest"},
    )

    manifest_paths = result["manifest"]["paths"]
    assert result["ok"] is False
    assert result["stage"] == "apply"
    assert result["rollback"] is not None
    assert result["rollback"]["ok"] is True
    assert result["dataplane"]["error_code"] == "NFT_APPLY_FAILED"
    assert result["enforcement_level"] == "owned_table_ready"
    assert result["preflight"]["can_enforce_global_direct"] is True
    assert fake_runner.calls == [
        ("dataplane_check", [manifest_paths["candidate_nft_path"], manifest_paths["candidate_manifest_path"]]),
        (
            "dataplane_apply",
            [
                manifest_paths["candidate_nft_path"],
                manifest_paths["candidate_manifest_path"],
                manifest_paths["snapshot_before_nft_path"],
                manifest_paths["snapshot_state_path"],
            ],
        ),
        (
            "dataplane_rollback",
            [
                manifest_paths["snapshot_before_nft_path"],
                manifest_paths["snapshot_state_path"],
                manifest_paths["candidate_manifest_path"],
            ],
        ),
    ]

    with db_session() as connection:
        row = connection.execute(
            "SELECT status FROM apply_versions WHERE apply_id = ?",
            (result["apply_id"],),
        ).fetchone()

    assert row is not None
    assert row["status"] == "rolled_back"
    assert not Path(manifest_paths["applied_manifest_path"]).exists()
    assert not Path(manifest_paths["current_manifest_path"]).exists()


def test_apply_pipeline_failed_selective_apply_does_not_promote_current_or_applied(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _enable_vpn_module()
    job = _create_apply_job()
    fake_runner = _FakeRunner(
        {
            "dataplane_check": [_success_check_result(table_exists=True)],
            "dataplane_apply": [_failed_apply_result("apply")],
            "dataplane_rollback": [_success_rollback_result("present")],
        }
    )
    monkeypatch.setattr(
        apply_service,
        "DEFAULT_DATAPLANE_ADAPTER",
        NftOwnedTableAdapter(runner=fake_runner),
    )

    result = run_apply_pipeline(
        job_id=str(job["job_id"]),
        reason="failed-selective-no-promote",
        mode=ApplyMode.APPLY,
        input_data={"source": "pytest"},
        manifest_state={
            "routing_global_state": {
                "desired_mode": "selective",
                "applied_mode": "selective",
                "selective_default": "direct",
            },
            "subjects": [],
            "extra": {"rules_effective": {"rules": []}},
        },
    )

    manifest_paths = result["manifest"]["paths"]
    assert result["ok"] is False
    assert not Path(manifest_paths["applied_manifest_path"]).exists()
    assert not Path(manifest_paths["current_manifest_path"]).exists()


def test_apply_pipeline_apply_mode_records_verified_global_vpn_success(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _enable_vpn_module()
    job = _create_apply_job()
    fake_runner = _FakeRunner(
        {
            "dataplane_check": [_success_check_result(table_exists=False)],
            "dataplane_apply": [_success_vpn_apply_result("missing")],
        }
    )
    monkeypatch.setattr(
        apply_service,
        "DEFAULT_DATAPLANE_ADAPTER",
        NftOwnedTableAdapter(runner=fake_runner),
    )
    monkeypatch.setattr(dataplane_global_service, "DEFAULT_MIHOMO_ADAPTER", _ReadyMihomoAdapter())
    monkeypatch.setattr(apply_service, "probe_live_global_mode", lambda: _live_mode_probe("vpn"))

    result = run_apply_pipeline(
        job_id=str(job["job_id"]),
        reason="apply-global-vpn",
        mode=ApplyMode.APPLY,
        input_data={"source": "pytest"},
        manifest_state={
            "routing_global_state": {
                "desired_mode": "vpn",
                "applied_mode": "vpn",
                "selective_default": "direct",
            },
            "subjects": [],
            "extra": {"rules_effective": {"rules": []}},
        },
    )

    assert result["ok"] is True
    assert result["dataplane_capability"] == "global_policy_v1"
    assert result["enforcement_level"] == "global_vpn_enforced"
    assert result["traffic_enforcement_guaranteed"] is True
    assert result["preflight"]["can_enforce_global_vpn"] is True
    assert result["preflight"]["vpn_external_path_verified"] is False
    applied_manifest = json.loads(
        Path(result["manifest"]["paths"]["applied_manifest_path"]).read_text(encoding="utf-8")
    )
    assert applied_manifest["runtime_enforcement"]["enforcement_level"] == "global_vpn_enforced"
    assert applied_manifest["global_preflight"]["vpn_external_path_verified"] is True
    candidate_text = Path(result["manifest"]["paths"]["candidate_nft_path"]).read_text(encoding="utf-8")
    assert 'goto fwrouter_direct comment "host output stays direct in global vpn mode"' in candidate_text
    assert 'goto fwrouter_vpn_mark comment "fwrouter global vpn output mark v1"' not in candidate_text


def test_apply_pipeline_global_vpn_verify_uses_requested_mode_while_applied_mode_is_stale(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _enable_vpn_module()
    job = _create_apply_job()
    fake_runner = _FakeRunner(
        {
            "dataplane_check": [_success_check_result(table_exists=False)],
            "dataplane_apply": [_success_vpn_apply_result("present")],
        }
    )
    monkeypatch.setattr(
        apply_service,
        "DEFAULT_DATAPLANE_ADAPTER",
        NftOwnedTableAdapter(runner=fake_runner),
    )
    monkeypatch.setattr(dataplane_global_service, "DEFAULT_MIHOMO_ADAPTER", _ReadyMihomoAdapter())
    monkeypatch.setattr(apply_service, "probe_live_global_mode", lambda: _live_mode_probe("vpn"))

    result = run_apply_pipeline(
        job_id=str(job["job_id"]),
        reason="apply-global-vpn-with-stale-applied-mode",
        mode=ApplyMode.APPLY,
        input_data={"source": "pytest"},
        manifest_state={
            "routing_global_state": {
                "desired_mode": "vpn",
                "applied_mode": "selective",
                "apply_state": "applying",
                "selective_default": "direct",
            },
            "subjects": [],
            "extra": {"rules_effective": {"rules": []}},
        },
    )

    assert result["ok"] is True
    assert result["enforcement_level"] == "global_vpn_enforced"
    assert result["traffic_enforcement_guaranteed"] is True
    assert result["dataplane"]["details"]["live_mode_probe"]["mode"] == "vpn"
    assert result["dataplane"]["details"]["active_mode_matches_intent"] is True


def test_apply_pipeline_apply_mode_records_selective_ip_only_success(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _enable_vpn_module()
    job = _create_apply_job()
    fake_runner = _FakeRunner(
        {
            "dataplane_check": [_success_check_result(table_exists=False)],
            "dataplane_apply": [_success_vpn_apply_result("missing")],
        }
    )
    monkeypatch.setattr(
        apply_service,
        "DEFAULT_DATAPLANE_ADAPTER",
        NftOwnedTableAdapter(runner=fake_runner),
    )
    monkeypatch.setattr(dataplane_global_service, "DEFAULT_MIHOMO_ADAPTER", _ReadyMihomoAdapter())
    monkeypatch.setattr(
        apply_service,
        "probe_live_global_mode",
        lambda: _live_mode_probe("selective", selective_default="direct"),
    )
    monkeypatch.setattr(
        apply_service,
        "inspect_dnsmasq_selective_status",
        lambda: {"ok": True, "missing": []},
    )
    monkeypatch.setattr(
        apply_service,
        "reconcile_dnsmasq_rules",
        lambda: {
            "ok": True,
            "router_dns_ipv4": ["192.168.0.1"],
            "message": "dnsmasq ready",
        },
    )

    result = run_apply_pipeline(
        job_id=str(job["job_id"]),
        reason="apply-global-selective-ip-only",
        mode=ApplyMode.APPLY,
        input_data={"source": "pytest"},
        manifest_state={
            "routing_global_state": {
                "desired_mode": "selective",
                "applied_mode": "selective",
                "selective_default": "direct",
            },
            "subjects": [],
            "extra": {
                "rules_effective": {
                    "selective_default": "direct",
                    "rules": [
                        {"action": "DIRECT", "kind": "ipv4_cidr", "value": "1.1.1.0/24"},
                        {"action": "VPN", "kind": "ipv4_cidr", "value": "8.8.8.0/24"},
                    ],
                }
            },
        },
    )

    assert result["ok"] is True
    assert result["enforcement_level"] == "global_selective_enforced"
    assert result["traffic_enforcement_guaranteed"] is True
    assert result["supported_modes"]["selective"] is True

    candidate_text = Path(result["manifest"]["paths"]["candidate_nft_path"]).read_text(encoding="utf-8")
    assert "selective direct IPv4" in candidate_text
    assert "8.8.8.0/24" in candidate_text
    assert "selective default direct" in candidate_text
    assert 'chain prerouting {' in candidate_text
    assert 'reject secure DNS bypass TCP from LAN' in candidate_text
    assert 'reject secure DNS bypass UDP from LAN' in candidate_text
    assert 'fwrouter vpn mark tcp:5202' in candidate_text
    assert 'fwrouter vpn mark udp:5203' in candidate_text
    assert 'meta l4proto tcp meta mark set 0x00000101' in candidate_text
    assert 'meta mark 0x00000101 meta l4proto tcp' in candidate_text
    assert 'fwrouter redirect handoff tcp:5202' in candidate_text
    assert 'fwrouter tproxy handoff udp:5203' in candidate_text
    assert 'tproxy handoff tcp:5203' not in candidate_text
    assert 'tproxy handoff udp:5203' in candidate_text
    assert 'chain fwrouter_vpn {' in candidate_text
    assert 'force transparent web clients off QUIC onto TCP' in candidate_text
    assert 'meta l4proto udp meta mark set 0x00000100' in candidate_text
    assert 'goto fwrouter_direct comment "host output stays direct in selective mode"' in candidate_text
    assert 'goto fwrouter_vpn_mark comment "selective default vpn"' not in candidate_text
    assert 'goto fwrouter_vpn_mark comment "selective domain-aware transparent default"' not in candidate_text


def test_apply_pipeline_apply_mode_records_selective_domain_aware_success(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _enable_vpn_module()
    job = _create_apply_job()
    fake_runner = _FakeRunner(
        {
            "dataplane_check": [_success_check_result(table_exists=False)],
            "dataplane_apply": [_success_vpn_apply_result("missing")],
        }
    )
    monkeypatch.setattr(
        apply_service,
        "DEFAULT_DATAPLANE_ADAPTER",
        NftOwnedTableAdapter(runner=fake_runner),
    )
    monkeypatch.setattr(dataplane_global_service, "DEFAULT_MIHOMO_ADAPTER", _ReadyMihomoAdapter())
    monkeypatch.setattr(
        apply_service,
        "probe_live_global_mode",
        lambda: _live_mode_probe("selective", selective_default="direct"),
    )
    monkeypatch.setattr(
        apply_service,
        "inspect_dnsmasq_selective_status",
        lambda: {"ok": True, "missing": []},
    )
    original_build_global_preflight = apply_service.build_global_preflight
    monkeypatch.setattr(
        apply_service,
        "build_global_preflight",
        lambda **kwargs: original_build_global_preflight(
            **{k: v for k, v in kwargs.items() if k != "effective_rules_artifact"},
            effective_rules_artifact={
                "selective_default": "direct",
                "rules": [
                    {"action": "DIRECT", "kind": "domain", "value": "example.org"},
                    {"action": "VPN", "kind": "domain", "value": "example.com"},
                ],
            },
        ),
    )
    cache_clears: list[str] = []
    monkeypatch.setattr(
        apply_service,
        "clear_live_probe_cache",
        lambda: cache_clears.append("cleared"),
    )
    monkeypatch.setattr(
        apply_service,
        "reconcile_dnsmasq_rules",
        lambda: {
            "ok": True,
            "router_dns_ipv4": ["192.168.0.1"],
            "message": "dnsmasq ready",
        },
    )

    result = run_apply_pipeline(
        job_id=str(job["job_id"]),
        reason="apply-global-selective-domain-aware",
        mode=ApplyMode.APPLY,
        input_data={"source": "pytest"},
        manifest_state={
            "routing_global_state": {
                "desired_mode": "selective",
                "applied_mode": "selective",
                "selective_default": "direct",
            },
            "subjects": [],
            "extra": {
                "rules_effective": {
                    "selective_default": "direct",
                    "rules": [
                        {"action": "DIRECT", "kind": "domain", "value": "example.org"},
                        {"action": "VPN", "kind": "domain", "value": "example.com"},
                    ],
                }
            },
        },
    )

    assert result["ok"] is True
    assert cache_clears == ["cleared"]
    assert result["enforcement_level"] == "global_selective_enforced"
    assert result["traffic_enforcement_guaranteed"] is True
    assert result["supported_modes"]["selective"] is True
    assert result["dataplane"]["details"]["dnsmasq_reconcile"]["ok"] is True

    candidate_text = Path(result["manifest"]["paths"]["candidate_nft_path"]).read_text(encoding="utf-8")
    assert 'set secure_dns_bypass_ipv4' in candidate_text
    assert 'reject secure DNS bypass TCP from LAN' in candidate_text
    assert 'reject secure DNS bypass UDP from LAN' in candidate_text
    assert 'fwrouter vpn mark tcp:5202' in candidate_text
    assert 'fwrouter vpn mark udp:5203' in candidate_text
    assert 'meta l4proto tcp meta mark set 0x00000101' in candidate_text
    assert 'meta mark 0x00000101 meta l4proto tcp' in candidate_text
    assert 'fwrouter redirect handoff tcp:5202' in candidate_text
    assert 'fwrouter tproxy handoff udp:5203' in candidate_text
    assert 'tproxy handoff tcp:5203' not in candidate_text
    assert 'tproxy handoff udp:5203' in candidate_text
    assert 'force transparent web clients off QUIC onto TCP' in candidate_text
    assert 'goto fwrouter_direct comment "host output stays direct in selective mode"' in candidate_text
    assert 'selective domain-aware via mihomo' not in candidate_text


def test_apply_pipeline_subject_only_fast_apply_reconciles_dnsmasq_but_skips_global_probe(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _enable_vpn_module()
    job = _create_apply_job()
    fake_runner = _FakeRunner(
        {
            "dataplane_check": [_success_check_result(table_exists=False)],
            "dataplane_apply": [_success_vpn_apply_result("missing")],
        }
    )
    monkeypatch.setattr(
        apply_service,
        "DEFAULT_DATAPLANE_ADAPTER",
        NftOwnedTableAdapter(runner=fake_runner),
    )
    monkeypatch.setattr(dataplane_global_service, "DEFAULT_MIHOMO_ADAPTER", _ReadyMihomoAdapter())
    monkeypatch.setattr(
        apply_service,
        "probe_live_global_mode",
        lambda: (_ for _ in ()).throw(AssertionError("global live probe must be skipped for fast subject apply")),
    )
    reconcile_calls: list[str] = []
    monkeypatch.setattr(
        apply_service,
        "reconcile_dnsmasq_rules",
        lambda: (
            reconcile_calls.append("called"),
            {
                "ok": True,
                "restart_required": True,
                "restart_reason": "nftset_runtime_refresh",
            },
        )[1],
    )
    monkeypatch.setattr(
        apply_service,
        "_verify_fast_subject_apply",
        lambda context: {
            "ok": True,
            "error_code": None,
            "error_message": None,
            "subject_id": context["subject_id"],
            "target_mode": context["target_mode"],
            "raw_chain": "",
        },
    )

    result = run_apply_pipeline(
        job_id=str(job["job_id"]),
        reason="subject-fast-apply",
        mode=ApplyMode.APPLY,
        input_data={
            "intent": "set_subject_admin_mode",
            "subject_id": "lan:test-fast",
            "mode": "selective",
            "fast_subject_apply": {
                "enabled": True,
                "subject_id": "lan:test-fast",
                "subject_type": "lan",
                "target_mode": "selective",
            },
        },
        manifest_state={
            "routing_global_state": {
                "desired_mode": "direct",
                "applied_mode": "direct",
                "selective_default": "direct",
                "server_mode": "auto",
                "desired_fixed_server_id": None,
                "applied_fixed_server_id": None,
                "active_auto_server_id": None,
            },
            "subjects": [
                {
                    "subject_id": "lan:test-fast",
                    "subject_type": "lan",
                    "display_name": "Fast LAN",
                    "desired_mode": "selective",
                    "applied_mode": "selective",
                    "runtime_state": "active",
                    "is_active": True,
                    "detail": {
                        "ip_address": "192.168.10.44",
                    },
                    "effective_state": {
                        "effective_mode": "selective",
                        "mode_source": "admin_locked",
                        "dataplane_path": "selective",
                        "selected_server_id": None,
                        "selected_server_source": "direct",
                        "runtime_enforcement": {},
                        "scoped_runtime": {},
                    },
                }
            ],
            "extra": {
                "rules_effective": {
                    "selective_default": "direct",
                    "rules": [
                        {"action": "DIRECT", "kind": "domain", "value": "example.org"},
                        {"action": "VPN", "kind": "domain", "value": "example.com"},
                    ],
                }
            },
        },
    )

    assert result["ok"] is True
    assert reconcile_calls == ["called"]
    assert result["dataplane"]["details"]["dnsmasq_reconcile"]["ok"] is True
    assert result["dataplane"]["details"]["dnsmasq_reconcile"]["restart_reason"] == "nftset_runtime_refresh"
    assert result["dataplane"]["details"]["live_mode_probe"]["fast_subject_apply"] is True
    assert result["dataplane"]["details"]["fast_subject_verify"]["ok"] is True


def test_apply_pipeline_subject_only_fast_apply_hot_swaps_classify_chain(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _enable_vpn_module()
    job = _create_apply_job()
    fake_runner = _FakeRunner(
        {"dataplane_check": [_success_check_result(table_exists=True)]}
    )
    monkeypatch.setattr(
        apply_service,
        "DEFAULT_DATAPLANE_ADAPTER",
        NftOwnedTableAdapter(runner=fake_runner),
    )
    monkeypatch.setattr(dataplane_global_service, "DEFAULT_MIHOMO_ADAPTER", _ReadyMihomoAdapter())
    monkeypatch.setattr(
        apply_service,
        "reconcile_dnsmasq_rules",
        lambda: (_ for _ in ()).throw(AssertionError("dnsmasq reconcile must be skipped for direct subject hot-swap")),
    )
    monkeypatch.setattr(
        apply_service,
        "_verify_fast_subject_apply",
        lambda context: {
            "ok": True,
            "error_code": None,
            "error_message": None,
            "subject_id": context["subject_id"],
            "target_mode": context["target_mode"],
            "raw_chain": "",
        },
    )
    observed_nft_payloads: list[str] = []

    class _Completed:
        returncode = 0
        stdout = ""
        stderr = ""

    def _fake_run(argv, *, check=False, capture_output=True, text=True):  # noqa: ANN001
        if argv[:4] == ["ip", "-json", "address", "show"]:
            completed = _Completed()
            completed.stdout = "[]"
            return completed
        if argv == ["nft", "list", "chain", "inet", "fwrouter_v2", "fwrouter_classify"]:
            completed = _Completed()
            completed.stdout = observed_nft_payloads[-1] if observed_nft_payloads else ""
            return completed
        assert argv[:2] == ["nft", "-f"]
        observed_nft_payloads.append(Path(argv[2]).read_text(encoding="utf-8"))
        return _Completed()

    monkeypatch.setattr(apply_service.subprocess, "run", _fake_run)

    result = run_apply_pipeline(
        job_id=str(job["job_id"]),
        reason="subject-fast-apply",
        mode=ApplyMode.APPLY,
        input_data={
            "intent": "set_subject_admin_mode",
            "subject_id": "lan:test-fast",
            "mode": "direct",
            "fast_subject_apply": {
                "enabled": True,
                "subject_id": "lan:test-fast",
                "subject_type": "lan",
                "target_mode": "direct",
            },
        },
        manifest_state={
            "routing_global_state": {
                "desired_mode": "direct",
                "applied_mode": "direct",
                "selective_default": "direct",
                "server_mode": "auto",
                "desired_fixed_server_id": None,
                "applied_fixed_server_id": None,
                "active_auto_server_id": None,
            },
            "subjects": [
                {
                    "subject_id": "lan:test-fast",
                    "subject_type": "lan",
                    "display_name": "Fast LAN",
                    "desired_mode": "direct",
                    "applied_mode": "direct",
                    "runtime_state": "active",
                    "is_active": True,
                    "detail": {
                        "ip_address": "192.168.10.44",
                    },
                    "effective_state": {
                        "effective_mode": "direct",
                        "mode_source": "admin_locked",
                        "dataplane_path": "direct",
                        "selected_server_id": None,
                        "selected_server_source": "direct",
                        "runtime_enforcement": {},
                        "scoped_runtime": {
                            "status": "applied",
                            "eligible": True,
                            "applied": True,
                            "matcher": {
                                "family": "ipv4",
                                "nft_expr": "ip saddr",
                                "value": "192.168.10.44",
                            },
                        },
                    },
                }
            ],
            "extra": {
                "rules_effective": {
                    "selective_default": "direct",
                    "rules": [],
                }
            },
        },
    )

    assert result["ok"] is True
    assert result["dataplane"]["details"]["hot_swap"] is True
    assert result["dataplane"]["details"]["hot_swap_kind"] == "subject_mode"
    assert result["dataplane"]["details"]["hot_swap_scope"] == "fwrouter_classify"
    assert observed_nft_payloads
    assert "flush chain inet fwrouter_v2 fwrouter_classify" in observed_nft_payloads[0]
    assert 'comment "scoped direct override: lan:test-fast"' in observed_nft_payloads[0]
    assert fake_runner.calls == [
        ("dataplane_check", [
            result["manifest"]["paths"]["candidate_nft_path"],
            result["manifest"]["paths"]["candidate_manifest_path"],
        ])
    ]


def test_apply_pipeline_rolls_back_when_dnsmasq_selective_contract_reconcile_fails(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _enable_vpn_module()
    job = _create_apply_job()
    fake_runner = _FakeRunner(
        {
            "dataplane_check": [_success_check_result(table_exists=False)],
            "dataplane_apply": [_success_vpn_apply_result("missing")],
            "dataplane_rollback": [_success_rollback_result("applied")],
        }
    )
    monkeypatch.setattr(
        apply_service,
        "DEFAULT_DATAPLANE_ADAPTER",
        NftOwnedTableAdapter(runner=fake_runner),
    )
    monkeypatch.setattr(dataplane_global_service, "DEFAULT_MIHOMO_ADAPTER", _ReadyMihomoAdapter())
    monkeypatch.setattr(
        apply_service,
        "inspect_dnsmasq_selective_status",
        lambda: {"ok": True, "missing": []},
    )
    original_build_global_preflight = apply_service.build_global_preflight
    monkeypatch.setattr(
        apply_service,
        "build_global_preflight",
        lambda **kwargs: original_build_global_preflight(
            **{k: v for k, v in kwargs.items() if k != "effective_rules_artifact"},
            effective_rules_artifact={
                "selective_default": "direct",
                "rules": [
                    {"action": "VPN", "kind": "domain", "value": "example.com"},
                ],
            },
        ),
    )
    monkeypatch.setattr(
        apply_service,
        "reconcile_dnsmasq_rules",
        lambda: {
            "ok": False,
            "error_code": "DNSMASQ_SELECTIVE_CONTRACT_INCOMPLETE",
            "message": "router DNS contract incomplete",
        },
    )

    result = run_apply_pipeline(
        job_id=str(job["job_id"]),
        reason="apply-global-selective-domain-aware-dnsmasq-fail",
        mode=ApplyMode.APPLY,
        input_data={"source": "pytest"},
        manifest_state={
            "routing_global_state": {
                "desired_mode": "selective",
                "applied_mode": "selective",
                "selective_default": "direct",
            },
            "subjects": [],
            "extra": {
                "rules_effective": {
                    "selective_default": "direct",
                    "rules": [
                        {"action": "VPN", "kind": "domain", "value": "example.com"},
                    ],
                }
            },
        },
    )

    assert result["ok"] is False
    assert result["dataplane"]["error_code"] == "DNSMASQ_SELECTIVE_CONTRACT_INCOMPLETE"
    assert result["rollback"]["ok"] is True
    assert result["dataplane"]["details"]["dnsmasq_reconcile"]["ok"] is False


def test_apply_pipeline_selective_degraded_blocks_vpn_sets_and_fails_open_to_direct(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _enable_vpn_module()
    job = _create_apply_job()
    fake_runner = _FakeRunner(
        {
            "dataplane_check": [_success_check_result(table_exists=False)],
            "dataplane_apply": [_success_vpn_apply_result("missing")],
        }
    )
    monkeypatch.setattr(
        apply_service,
        "DEFAULT_DATAPLANE_ADAPTER",
        NftOwnedTableAdapter(runner=fake_runner),
    )
    monkeypatch.setattr(
        dataplane_global_service,
        "DEFAULT_MIHOMO_ADAPTER",
        type(
            "_DegradedMihomoAdapter",
            (),
            {
                "health": lambda self: MihomoHealth(
                    runtime_state=MihomoRuntimeState.DEGRADED,
                    message="controller unavailable",
                    details={
                        "adapter": "fake",
                        "config": {
                            "redir_port": 5202,
                            "tproxy_port": 5203,
                            "tun_enabled": True,
                            "fwrouter_contours": {
                                "explicit_proxy": {"preserved": True},
                                "transparent_vpn": {
                                    "ready": False,
                                    "isolated_from_explicit_proxy": True,
                                    "redir_port": 5202,
                                    "tproxy_port": 5203,
                                    "transparent_tcp_listener_present": True,
                                    "transparent_udp_listener_present": True,
                                    "transparent_tcp_ready": False,
                                    "transparent_udp_ready": True,
                                },
                                "domain_selective": {
                                    "path_kind": "domain_aware",
                                    "ready": True,
                                    "uses_transparent_contour": True,
                                    "explicit_proxy_preserved": True,
                                },
                            },
                        },
                        "selectors": {
                            "vpn_global_exists": True,
                            "vpn_global_targets_count": 1,
                            "vpn_global_has_vpn_auto": True,
                            "vpn_global_now": "vpn-auto",
                            "vpn_auto_now": "DIRECT",
                        },
                        "transparent_runtime": {
                            "transparent_tcp_session_materialized": False,
                            "transparent_udp_session_materialized": False,
                        },
                    },
                )
            },
        )(),
    )
    monkeypatch.setattr(
        apply_service,
        "probe_live_global_mode",
        lambda: _live_mode_probe("selective", selective_default="direct"),
    )
    monkeypatch.setattr(
        apply_service,
        "inspect_dnsmasq_selective_status",
        lambda: {"ok": True, "missing": []},
    )
    monkeypatch.setattr(
        apply_service,
        "reconcile_dnsmasq_rules",
        lambda: {
            "ok": True,
            "router_dns_ipv4": ["192.168.0.1"],
            "message": "dnsmasq ready",
        },
    )

    result = run_apply_pipeline(
        job_id=str(job["job_id"]),
        reason="apply-global-selective-degraded",
        mode=ApplyMode.APPLY,
        input_data={"source": "pytest"},
        manifest_state={
            "routing_global_state": {
                "desired_mode": "selective",
                "applied_mode": "selective",
                "selective_default": "vpn",
            },
            "subjects": [],
            "extra": {
                "rules_effective": {
                    "selective_default": "vpn",
                    "rules": [
                        {"action": "VPN", "kind": "domain", "value": "example.com"},
                    ],
                }
            },
        },
    )

    assert result["ok"] is True
    assert result["enforcement_level"] == "global_selective_enforced"
    assert result["traffic_enforcement_guaranteed"] is True
    assert result["missing_runtime_requirements"]

    candidate_text = Path(result["manifest"]["paths"]["candidate_nft_path"]).read_text(encoding="utf-8")
    assert 'selective degraded block VPN IPv4' in candidate_text
    assert 'selective degraded default direct' in candidate_text


def test_apply_pipeline_fwrouter_subject_vpn_override_is_ignored_for_host_output(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _enable_vpn_module()
    job = _create_apply_job()
    fake_runner = _FakeRunner(
        {
            "dataplane_check": [_success_check_result(table_exists=False)],
            "dataplane_apply": [_success_vpn_apply_result("missing")],
        }
    )
    monkeypatch.setattr(
        apply_service,
        "DEFAULT_DATAPLANE_ADAPTER",
        NftOwnedTableAdapter(runner=fake_runner),
    )
    monkeypatch.setattr(dataplane_global_service, "DEFAULT_MIHOMO_ADAPTER", _ReadyMihomoAdapter())
    monkeypatch.setattr(
        apply_service,
        "probe_live_global_mode",
        lambda: _live_mode_probe("direct"),
    )

    result = run_apply_pipeline(
        job_id=str(job["job_id"]),
        reason="apply-fwrouter-host-vpn",
        mode=ApplyMode.APPLY,
        input_data={"source": "pytest"},
        manifest_state={
            "routing_global_state": {
                "desired_mode": "direct",
                "applied_mode": "direct",
                "selective_default": "direct",
            },
                "subjects": [
                    {
                        "subject_id": "fwrouter:global",
                        "subject_type": "fwrouter",
                        "display_name": "FWRouter global traffic",
                        "desired_mode": "vpn",
                        "applied_mode": "vpn",
                        "runtime_state": "running",
                        "is_active": True,
                        "effective_state": {
                            "effective_mode": "vpn",
                            "mode_source": "admin_override",
                            "dataplane_path": "vpn",
                            "selected_server_id": None,
                            "selected_server_source": "global",
                            "runtime_enforcement": {},
                            "scoped_runtime": {},
                        },
                    }
                ],
            "extra": {"rules_effective": {"rules": []}},
        },
    )

    assert result["ok"] is True
    candidate_text = Path(result["manifest"]["paths"]["candidate_nft_path"]).read_text(encoding="utf-8")
    protected_bypass = 'ip daddr @protected_ipv4 goto fwrouter_direct comment "host output to protected IPv4 always direct"'
    local_bypass = 'fib daddr type local goto fwrouter_direct comment "host output to local destination always direct"'
    management_bypass = 'meta l4proto tcp tcp sport { 22 } goto fwrouter_direct comment "management tcp output direct"'
    assert local_bypass in candidate_text
    assert protected_bypass in candidate_text
    assert management_bypass in candidate_text
    assert 'comment "fwrouter subject vpn override"' not in candidate_text
    assert 'goto fwrouter_direct comment "host output stays direct in global direct mode"' in candidate_text


def test_apply_pipeline_rollback_handles_missing_previous_table(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _enable_vpn_module()
    job = _create_apply_job()
    fake_runner = _FakeRunner(
        {
            "dataplane_check": [_success_check_result(table_exists=False)],
            "dataplane_apply": [_failed_apply_result("verify")],
            "dataplane_rollback": [_success_rollback_result("missing")],
        }
    )
    monkeypatch.setattr(
        apply_service,
        "DEFAULT_DATAPLANE_ADAPTER",
        NftOwnedTableAdapter(runner=fake_runner),
    )

    result = run_apply_pipeline(
        job_id=str(job["job_id"]),
        reason="missing-previous-table",
        mode=ApplyMode.APPLY,
        input_data={"source": "pytest"},
    )

    assert result["ok"] is False
    assert result["stage"] == "verify"
    assert result["rollback"] is not None
    assert result["rollback"]["details"]["previous_table_state"] == "missing"


def test_apply_pipeline_vpn_verify_failure_rolls_back_and_keeps_state_unchanged(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _enable_vpn_module()
    job = _create_apply_job()
    fake_runner = _FakeRunner(
        {
            "dataplane_check": [_success_check_result(table_exists=False)],
            "dataplane_apply": [_unverified_vpn_apply_result("missing")],
            "dataplane_rollback": [_success_rollback_result("missing")],
        }
    )
    monkeypatch.setattr(
        apply_service,
        "DEFAULT_DATAPLANE_ADAPTER",
        NftOwnedTableAdapter(runner=fake_runner),
    )
    monkeypatch.setattr(dataplane_global_service, "DEFAULT_MIHOMO_ADAPTER", _ReadyMihomoAdapter())
    monkeypatch.setattr(apply_service, "probe_live_global_mode", lambda: _live_mode_probe("vpn"))

    result = run_apply_pipeline(
        job_id=str(job["job_id"]),
        reason="apply-global-vpn-unverified",
        mode=ApplyMode.APPLY,
        input_data={"source": "pytest"},
        manifest_state={
            "routing_global_state": {
                "desired_mode": "vpn",
                "applied_mode": "vpn",
                "selective_default": "direct",
            },
            "subjects": [],
            "extra": {"rules_effective": {"rules": []}},
        },
    )

    assert result["ok"] is False
    assert result["stage"] == "verify"
    assert result["dataplane"]["error_code"] == "GLOBAL_VPN_VERIFY_FAILED"
    assert result["rollback"] is not None
    assert result["rollback"]["ok"] is True
    assert not Path(result["manifest"]["paths"]["applied_manifest_path"]).exists()
    assert not Path(result["manifest"]["paths"]["current_manifest_path"]).exists()


def test_apply_pipeline_detects_live_mode_mismatch_and_rolls_back(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _enable_vpn_module()
    job = _create_apply_job()
    fake_runner = _FakeRunner(
        {
            "dataplane_check": [_success_check_result(table_exists=False)],
            "dataplane_apply": [_success_vpn_apply_result("missing")],
            "dataplane_rollback": [_success_rollback_result("missing")],
        }
    )
    monkeypatch.setattr(
        apply_service,
        "DEFAULT_DATAPLANE_ADAPTER",
        NftOwnedTableAdapter(runner=fake_runner),
    )
    monkeypatch.setattr(dataplane_global_service, "DEFAULT_MIHOMO_ADAPTER", _ReadyMihomoAdapter())
    monkeypatch.setattr(
        apply_service,
        "probe_live_global_mode",
        lambda: _live_mode_probe("direct"),
    )

    result = run_apply_pipeline(
        job_id=str(job["job_id"]),
        reason="apply-global-vpn-live-mismatch",
        mode=ApplyMode.APPLY,
        input_data={"source": "pytest"},
        manifest_state={
            "routing_global_state": {
                "desired_mode": "vpn",
                "applied_mode": "vpn",
                "selective_default": "direct",
            },
            "subjects": [],
            "extra": {"rules_effective": {"rules": []}},
        },
    )

    assert result["ok"] is False
    assert result["stage"] == "verify"
    assert result["dataplane"]["error_code"] == "ACTIVE_DATAPLANE_MODE_MISMATCH"
    assert result["rollback"] is not None
    assert result["rollback"]["ok"] is True
    assert not Path(result["manifest"]["paths"]["applied_manifest_path"]).exists()
    assert not Path(result["manifest"]["paths"]["current_manifest_path"]).exists()


def test_apply_pipeline_timeout_returns_failed_without_promotion(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _enable_vpn_module()
    job = _create_apply_job()
    fake_runner = _FakeRunner(
        {
            "dataplane_check": [_success_check_result(table_exists=False)],
            "dataplane_apply": [
                ScriptResult(
                    script_id="dataplane_apply",
                    argv=("dataplane_apply",),
                    returncode=124,
                    stdout="",
                    stderr="Command timed out after 20 seconds.",
                    duration_seconds=20.0,
                )
            ],
            "dataplane_rollback": [_success_rollback_result("missing")],
        }
    )
    monkeypatch.setattr(
        apply_service,
        "DEFAULT_DATAPLANE_ADAPTER",
        NftOwnedTableAdapter(runner=fake_runner),
    )

    result = run_apply_pipeline(
        job_id=str(job["job_id"]),
        reason="timed-out-apply",
        mode=ApplyMode.APPLY,
        input_data={"source": "pytest"},
    )

    assert result["ok"] is False
    assert result["dataplane"]["error_code"] == "DATAPLANE_APPLY_TIMEOUT"
    assert result["rollback"] is not None
    assert not Path(result["manifest"]["paths"]["applied_manifest_path"]).exists()


def test_apply_pipeline_reports_rollback_failure_without_broad_reset(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _enable_vpn_module()
    job = _create_apply_job()
    fake_runner = _FakeRunner(
        {
            "dataplane_check": [_success_check_result(table_exists=True)],
            "dataplane_apply": [_failed_apply_result("apply")],
            "dataplane_rollback": [_failed_rollback_result()],
        }
    )
    monkeypatch.setattr(
        apply_service,
        "DEFAULT_DATAPLANE_ADAPTER",
        NftOwnedTableAdapter(runner=fake_runner),
    )

    result = run_apply_pipeline(
        job_id=str(job["job_id"]),
        reason="rollback-failure",
        mode=ApplyMode.APPLY,
        input_data={"source": "pytest"},
    )

    assert result["ok"] is False
    assert result["rollback"] is not None
    assert result["rollback"]["ok"] is False
    assert result["rollback"]["error_code"] == "NFT_ROLLBACK_RESTORE_FAILED"

    with db_session() as connection:
        row = connection.execute(
            "SELECT status FROM apply_versions WHERE apply_id = ?",
            (result["apply_id"],),
        ).fetchone()

    assert row is not None
    assert row["status"] == "failed"


def test_apply_pipeline_result_exposes_owned_table_enforcement(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _enable_vpn_module()
    job = _create_apply_job()
    fake_runner = _FakeRunner({"dataplane_check": [_success_check_result()]})
    monkeypatch.setattr(
        apply_service,
        "DEFAULT_DATAPLANE_ADAPTER",
        NftOwnedTableAdapter(runner=fake_runner),
    )

    result = run_apply_pipeline(
        job_id=str(job["job_id"]),
        reason="capability-contract",
        mode=ApplyMode.DRY_RUN,
        input_data={"source": "pytest"},
    )

    assert result["enforcement_level"] == "owned_table_ready"
    assert result["dataplane_capability"] == "nft_owned_table"
    assert result["traffic_enforcement_guaranteed"] is False
