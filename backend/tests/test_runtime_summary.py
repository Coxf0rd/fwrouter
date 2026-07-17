from __future__ import annotations
from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import initialize_database


import json
from pathlib import Path

from fwrouter_api.services.runtime import get_runtime_summary
from fwrouter_api.services.artifacts import atomic_write_json, atomic_write_text
from fwrouter_api.services.live_probe_cache import clear_live_probe_cache
from fwrouter_api.services.system_summary import build_system_summary


def _configure_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FWROUTER_STATE_DIR", str(tmp_path / "state"))
    get_settings.cache_clear()
    clear_live_probe_cache()


def test_runtime_summary_contains_layout_and_modules(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    summary = get_runtime_summary()

    assert summary["backend"]["layout"]["app_root"] == "/opt/fwrouter-api"
    assert summary["backend"]["database"]["ok"] is True
    assert isinstance(summary["modules"], list)
    assert summary["dataplane"]["state"] == "owned_table_missing"
    assert summary["dataplane"]["supported_modes"]["direct"] is True
    assert summary["dataplane"]["supported_modes"]["selective"] is False
    assert isinstance(summary["dataplane"]["supported_modes"]["vpn"], bool)
    assert summary["traffic_accounting"]["retention_months"] == 12

def test_runtime_summary_exposes_dataplane_capability(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    summary = get_runtime_summary()

    assert summary["dataplane"]["dataplane_capability"] == "nft_owned_table"
    assert summary["dataplane"]["capability"] == "nft_owned_table"
    assert summary["dataplane"]["enforcement_level"] == "owned_table_missing"
    assert summary["dataplane"]["traffic_enforcement_guaranteed"] is False
    assert "live_owned_table_missing" in summary["dataplane"]["missing_runtime_requirements"]
    assert summary["dataplane"]["drift"]["detected"] is True
    assert summary["dataplane"]["drift"]["code"] == "ACTIVE_DATAPLANE_MODE_MISMATCH"


def test_runtime_summary_includes_scoped_egress_diagnostics(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    summary = get_runtime_summary()

    assert "scoped_egress" in summary["dataplane"]
    assert "scoped_egress_readiness" in summary["dataplane"]
    assert summary["dataplane"]["scoped_egress"]["state"] == "disabled"
    assert summary["dataplane"]["scoped_egress_readiness"]["state"] in {"ready", "blocked"}


def test_system_summary_reports_runtime_status_instead_of_skeleton(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    summary = build_system_summary()

    assert summary["backend"]["status"] in {"ready", "degraded", "active"}
    assert "runtime" in summary["backend"]["message"].lower()


def test_runtime_summary_uses_persisted_subscription_state(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    from fwrouter_api.services.subscription import save_subscription_url

    save_subscription_url("https://example.test/subscription")

    summary = get_runtime_summary()

    assert summary["subscription"]["adapter"] == "http"
    assert summary["subscription"]["refresh_available"] is True
    assert summary["subscription"]["status"] == "idle"
    assert summary["subscription"]["state"]["url_saved"] is True
    assert summary["subscription"]["error_code"] is None


def test_runtime_summary_exposes_automation_flags(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    monkeypatch.setenv("FWROUTER_STARTUP_RECOVERY_ENABLED", "false")
    monkeypatch.setenv("FWROUTER_MAINTENANCE_SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("FWROUTER_RUNTIME_CONVERGENCE_SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("FWROUTER_WATCHDOG_SCHEDULER_ENABLED", "false")
    get_settings.cache_clear()
    initialize_database()

    summary = get_runtime_summary()

    assert summary["automation"]["startup_recovery"]["status"] == "disabled_by_config"
    assert summary["automation"]["startup_live_recovery"]["status"] == "disabled_by_config"
    assert summary["automation"]["startup_apply_reconcile"]["status"] == "disabled_by_config"
    assert summary["automation"]["maintenance_scheduler"]["status"] == "disabled_by_config"
    assert summary["automation"]["runtime_convergence_scheduler"]["status"] == "disabled_by_config"
    assert summary["automation"]["watchdog_scheduler"]["status"] == "disabled_by_config"


def test_runtime_summary_marks_temporary_direct_safe_bootstrap_when_not_enforced(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    summary = get_runtime_summary()

    assert summary["automation"]["startup_live_recovery"]["phase"] == "startup_live_recovery_pending"
    assert "temporary direct-safe contour" in summary["automation"]["startup_live_recovery"]["message"]
    assert summary["automation"]["startup_apply_reconcile"]["phase"] == "startup_apply_reconcile_available"
    assert summary["automation"]["startup_dnsmasq_reconcile"]["phase"] == "startup_dnsmasq_reconcile"
    assert summary["automation"]["startup_recovery"]["bootstrap_mode"] == "temporary_direct_safe_bootstrap"
    assert summary["automation"]["startup_recovery"]["phase"] == "startup_live_recovery_pending"
    assert "temporary direct-safe contour" in summary["automation"]["startup_recovery"]["message"]


def test_runtime_summary_exposes_routing_drift(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    monkeypatch.setattr(
        "fwrouter_api.services.runtime.build_runtime_enforcement_state",
        lambda **kwargs: {
            "dataplane_capability": "global_policy_v1",
            "capability": "global_policy_v1",
            "enforcement_level": "global_direct_only",
            "traffic_enforcement_guaranteed": False,
            "supported_modes": {"direct": True, "selective": True, "vpn": True},
            "missing_runtime_requirements": ["active_dataplane_mode_mismatch"],
            "profile": {"profile": "global_v1"},
            "active_mode_matches_intent": False,
            "live_global_mode": "direct",
            "live_selective_default": "direct",
        },
    )

    summary = get_runtime_summary()

    assert summary["dataplane"]["drift"]["detected"] is True
    assert summary["dataplane"]["drift"]["code"] == "ACTIVE_DATAPLANE_MODE_MISMATCH"


def test_system_summary_warns_on_active_dataplane_mode_mismatch(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    monkeypatch.setattr(
        "fwrouter_api.services.system_summary.build_runtime_enforcement_state",
        lambda: {
            "dataplane_capability": "global_policy_v1",
            "capability": "global_policy_v1",
            "enforcement_level": "global_direct_only",
            "traffic_enforcement_guaranteed": False,
            "supported_modes": {"direct": True, "selective": True, "vpn": True},
            "missing_runtime_requirements": ["active_dataplane_mode_mismatch"],
            "profile": {"profile": "global_v1"},
            "active_mode_matches_intent": False,
            "live_global_mode": "direct",
            "live_selective_default": "direct",
        },
    )

    summary = build_system_summary()

    warning_codes = {item["code"] for item in summary["warnings"]}
    assert "FWROUTER_ACTIVE_DATAPLANE_MODE_MISMATCH" in warning_codes


def test_runtime_summary_reuses_short_ttl_cache(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    calls: list[int] = []

    def _load() -> dict[str, object]:
        calls.append(1)
        return {"calls": len(calls)}

    monkeypatch.setattr("fwrouter_api.services.runtime._build_runtime_summary", _load)

    first = get_runtime_summary()
    second = get_runtime_summary()

    assert first == {"calls": 1}
    assert second == {"calls": 1}
    assert len(calls) == 1


def test_system_summary_reuses_short_ttl_cache(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    calls: list[int] = []

    def _load(*, schema_summary=None) -> dict[str, object]:
        calls.append(1)
        return {"calls": len(calls), "schema_ok": bool((schema_summary or {}).get("ok", True))}

    monkeypatch.setattr("fwrouter_api.services.system_summary._build_system_summary_uncached", _load)

    first = build_system_summary()
    second = build_system_summary()

    assert first == {"calls": 1, "schema_ok": True}
    assert second == {"calls": 1, "schema_ok": True}
    assert len(calls) == 1


def test_runtime_summary_checks_applied_dataplane_artifacts(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    settings = get_settings()
    generated_dir = settings.paths.generated_dir / "dataplane"
    last_good_dir = settings.paths.state_dir / "last-good" / "dataplane"

    atomic_write_text(last_good_dir / "last-good.nft", "table inet fwrouter_v2 {}\n")
    atomic_write_json(
        generated_dir / "applied-manifest.json",
        {
            "routing_global_state": {"desired_mode": "direct", "applied_mode": "direct"},
            "global_preflight": {
                "profile": {"profile": "global_v1"},
                "missing": [],
                "can_enforce_global_direct": True,
                "can_enforce_global_selective": False,
                "can_enforce_global_vpn": False,
            },
        },
    )
    from fwrouter_api.services.servers import ensure_routing_global_state
    from fwrouter_api.db.connection import db_session

    ensure_routing_global_state()
    with db_session() as connection:
        connection.execute(
            """
            UPDATE routing_global_state
            SET
                desired_mode = 'selective',
                applied_mode = 'selective',
                selective_default = 'direct',
                updated_at = CURRENT_TIMESTAMP
            WHERE id = 1
            """
        )

    monkeypatch.setattr(
        "fwrouter_api.services.runtime.read_live_dataplane_payload",
        lambda: {
            "ok": True,
            "message": "applied manifest ok",
            "table_exists": True,
            "required_chains": {
                "fwrouter_classify": True,
                "fwrouter_direct": True,
            },
        },
    )

    summary = get_runtime_summary()

    assert summary["dataplane"]["check_ok"] is True
    assert summary["dataplane"]["message"] == "applied manifest ok"


def test_runtime_summary_recomputes_selective_enforcement_from_applied_rules(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    settings = get_settings()
    generated_dir = settings.paths.generated_dir / "dataplane"
    last_good_dir = settings.paths.state_dir / "last-good" / "dataplane"

    atomic_write_text(last_good_dir / "last-good.nft", "table inet fwrouter_v2 {}\n")
    atomic_write_json(
        generated_dir / "applied-manifest.json",
        {
            "routing_global_state": {
                "desired_mode": "selective",
                "applied_mode": "selective",
                "selective_default": "direct",
            },
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
    from fwrouter_api.services.servers import ensure_routing_global_state
    from fwrouter_api.db.connection import db_session

    ensure_routing_global_state()
    with db_session() as connection:
        connection.execute(
            """
            UPDATE routing_global_state
            SET
                desired_mode = 'selective',
                applied_mode = 'selective',
                selective_default = 'direct',
                updated_at = CURRENT_TIMESTAMP
            WHERE id = 1
            """
        )

    monkeypatch.setattr(
        "fwrouter_api.services.runtime.read_live_dataplane_payload",
        lambda: {
            "ok": True,
            "message": "applied manifest ok",
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
        "fwrouter_api.services.dataplane_status.probe_live_global_mode",
        lambda: {
            "ok": True,
            "table_exists": True,
            "mode": "selective",
            "selective_default": "direct",
        },
    )
    monkeypatch.setattr(
        "fwrouter_api.services.runtime.build_runtime_enforcement_state",
        lambda **kwargs: {
            "dataplane_capability": "global_policy_v1",
            "capability": "global_policy_v1",
            "enforcement_level": "global_selective_enforced",
            "traffic_enforcement_guaranteed": True,
            "supported_modes": {"direct": True, "selective": True, "vpn": True},
            "missing_runtime_requirements": [],
            "profile": {"profile": "global_v1"},
            "active_mode_matches_intent": True,
            "live_global_mode": "selective",
            "live_selective_default": "direct",
        },
    )

    summary = get_runtime_summary()

    assert summary["dataplane"]["enforcement_level"] == "global_selective_enforced"
    assert summary["dataplane"]["traffic_enforcement_guaranteed"] is True
    assert summary["dataplane"]["drift"]["detected"] is False


def test_runtime_summary_prefers_persisted_routing_over_stale_applied_manifest(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    settings = get_settings()
    generated_dir = settings.paths.generated_dir / "dataplane"
    last_good_dir = settings.paths.state_dir / "last-good" / "dataplane"

    atomic_write_text(last_good_dir / "last-good.nft", "table inet fwrouter_v2 {}\n")
    atomic_write_json(
        generated_dir / "applied-manifest.json",
        {
            "routing_global_state": {
                "desired_mode": "selective",
                "applied_mode": "direct",
                "selective_default": "direct",
            },
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

    from fwrouter_api.services.servers import ensure_routing_global_state
    from fwrouter_api.db.connection import db_session

    ensure_routing_global_state()
    with db_session() as connection:
        connection.execute(
            """
            UPDATE routing_global_state
            SET
                desired_mode = 'selective',
                applied_mode = 'selective',
                selective_default = 'direct',
                updated_at = CURRENT_TIMESTAMP
            WHERE id = 1
            """
        )

    monkeypatch.setattr(
        "fwrouter_api.services.runtime.read_live_dataplane_payload",
        lambda: {
            "ok": True,
            "message": "live selective ok",
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
        "fwrouter_api.services.dataplane_status.probe_live_global_mode",
        lambda: {
            "ok": True,
            "table_exists": True,
            "mode": "selective",
            "selective_default": "direct",
        },
    )
    monkeypatch.setattr(
        "fwrouter_api.services.runtime.build_runtime_enforcement_state",
        lambda **kwargs: {
            "dataplane_capability": "global_policy_v1",
            "capability": "global_policy_v1",
            "enforcement_level": "global_selective_enforced",
            "traffic_enforcement_guaranteed": True,
            "supported_modes": {"direct": True, "selective": True, "vpn": True},
            "missing_runtime_requirements": [],
            "profile": {"profile": "global_v1"},
            "active_mode_matches_intent": True,
            "live_global_mode": "selective",
            "live_selective_default": "direct",
        },
    )

    summary = get_runtime_summary()

    assert summary["routing"]["applied_mode"] == "selective"
    assert summary["dataplane"]["enforcement_level"] == "global_selective_enforced"
    assert summary["dataplane"]["drift"]["detected"] is False


def test_runtime_summary_projects_core_and_vpn_modules_from_live_runtime(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    settings = get_settings()
    generated_dir = settings.paths.generated_dir / "dataplane"
    last_good_dir = settings.paths.state_dir / "last-good" / "dataplane"

    atomic_write_text(last_good_dir / "last-good.nft", "table inet fwrouter_v2 {}\n")
    atomic_write_json(
        generated_dir / "applied-manifest.json",
        {
            "routing_global_state": {
                "desired_mode": "selective",
                "applied_mode": "selective",
                "selective_default": "direct",
            },
            "extra": {"rules_effective": {"selective_default": "direct", "rules": []}},
        },
    )

    monkeypatch.setattr(
        "fwrouter_api.services.runtime._cached_mihomo_health",
        lambda: type(
            "_Health",
            (),
            {
                "runtime_state": type("_State", (), {"value": "running"})(),
                "message": "mihomo running",
                "details": {"adapter": "fake", "config": {"redir_port": 5202, "tproxy_port": 5203, "tun_enabled": True}, "selectors": {"vpn_global_exists": True, "vpn_global_targets_count": 1, "vpn_global_has_vpn_auto": True}},
                "active_server_id": "server-1",
            },
        )(),
    )
    monkeypatch.setattr(
        "fwrouter_api.services.runtime.read_live_dataplane_payload",
        lambda: {
            "ok": True,
            "message": "live selective ok",
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
        "fwrouter_api.services.dataplane_status.probe_live_global_mode",
        lambda: {
            "ok": True,
            "table_exists": True,
            "mode": "selective",
            "selective_default": "direct",
        },
    )
    monkeypatch.setattr(
        "fwrouter_api.services.runtime.build_runtime_enforcement_state",
        lambda **kwargs: {
            "dataplane_capability": "global_policy_v1",
            "capability": "global_policy_v1",
            "enforcement_level": "global_selective_enforced",
            "traffic_enforcement_guaranteed": True,
            "supported_modes": {"direct": True, "selective": True, "vpn": True},
            "missing_runtime_requirements": [],
            "profile": {"profile": "global_v1"},
            "active_mode_matches_intent": True,
            "live_global_mode": "selective",
            "live_selective_default": "direct",
        },
    )

    summary = get_runtime_summary()
    modules = {item["module_name"]: item for item in summary["modules"]}

    assert modules["core"]["runtime_state"] == "running"
    assert modules["core"]["state_source"] == "runtime_projection"
    assert modules["vpn"]["runtime_state"] == "running"
    assert modules["vpn"]["state_source"] == "runtime_projection"


def test_runtime_summary_exposes_split_transparent_tcp_udp_status(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    monkeypatch.setattr(
        "fwrouter_api.services.runtime.build_runtime_enforcement_state",
        lambda **kwargs: {
            "dataplane_capability": "global_policy_v1",
            "capability": "global_policy_v1",
            "enforcement_level": "global_selective_enforced",
            "traffic_enforcement_guaranteed": True,
            "supported_modes": {"direct": True, "selective": True, "vpn": True},
            "missing_runtime_requirements": [],
            "profile": {
                "profile": "global_v1",
                "mihomo": {
                    "contours": {
                        "transparent_contour_ready": False,
                        "transparent_tcp_ready": False,
                        "transparent_udp_ready": True,
                    }
                },
            },
            "active_mode_matches_intent": True,
            "live_global_mode": "selective",
            "live_selective_default": "direct",
        },
    )

    summary = get_runtime_summary()

    assert summary["dataplane"]["profile"]["mihomo"]["contours"]["transparent_tcp_ready"] is False
    assert summary["dataplane"]["profile"]["mihomo"]["contours"]["transparent_udp_ready"] is True
