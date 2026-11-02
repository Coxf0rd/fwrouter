from __future__ import annotations

from pathlib import Path

from fwrouter_api.services import mihomo_config


def _prepare_manual_candidate_promotion(monkeypatch, tmp_path: Path):
    from fwrouter_api.services import mihomo_reconcile, mihomo_reconcile_fingerprint

    candidate = tmp_path / "state" / "generated" / "mihomo" / "config.next.yaml"
    base = tmp_path / "state" / "generated" / "mihomo" / "config.yaml"
    candidate.parent.mkdir(parents=True)
    candidate.write_text("candidate generation B\n", encoding="utf-8")
    base.write_text("active generation A\n", encoding="utf-8")
    monkeypatch.setattr(mihomo_reconcile.config, "_resolved_candidate_config_path", lambda: str(candidate))
    monkeypatch.setattr(mihomo_reconcile.config, "_resolved_base_config_path", lambda: str(base))
    monkeypatch.setattr(mihomo_reconcile.config, "managed_runtime_operation_blocked", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(mihomo_reconcile.config, "write_technical_log", lambda **_kwargs: None)
    monkeypatch.setattr(mihomo_reconcile_fingerprint, "_resolved_candidate_config_path", lambda: candidate)
    current = {"fingerprint": {"hash": "b" * 64, "payload": {}}}
    monkeypatch.setattr(
        mihomo_reconcile_fingerprint, "current_mihomo_input_fingerprint",
        lambda _routing=None: current["fingerprint"],
    )
    monkeypatch.setattr(
        mihomo_reconcile, "current_mihomo_input_fingerprint",
        lambda _routing=None: current["fingerprint"],
    )
    assert mihomo_reconcile_fingerprint.record_candidate_provenance(
        candidate_path=candidate, fingerprint=current["fingerprint"],
    )
    return mihomo_reconcile, current, candidate, base


def test_mihomo_compat_exports_and_patch_views_preserve_identity(monkeypatch) -> None:
    from fwrouter_api.services import mihomo_config_builder, mihomo_reconcile

    expected_star_exports = set(
        """
        APPLIED_MANIFEST_PATH Any BASE_CONFIG_PATH DEFAULT_FULL_VPN_TCP_REDIR_PORT annotations
        DEFAULT_FULL_VPN_UDP_TPROXY_PORT DEFAULT_TRANSPARENT_TCP_REDIR_PORT
        DEFAULT_TRANSPARENT_UDP_TPROXY_PORT EXPLICIT_MIXED_LISTENER_BIND
        EXPLICIT_MIXED_LISTENER_NAME EXPLICIT_MIXED_LISTENER_PORT
        FULL_VPN_REDIR_LISTENER_NAME FULL_VPN_RULE_NAME FULL_VPN_TPROXY_LISTENER_NAME
        LEGACY_INBOUND_KEYS MIHOMO_CANDIDATE_CONFIG_PATH MIHOMO_CONTROLLER_ADDRESS
        Path SUBJECT_SELECTOR_PREFIX TRANSPARENT_BIND_ADDRESS TRANSPARENT_REDIR_LISTENER_NAME
        TRANSPARENT_TPROXY_LISTENER_NAME TRANSPARENT_TPROXY_PROXY_NAME
        TRANSPARENT_TPROXY_RULE_NAME XRAY_MIHOMO_LISTENER_PREFIX atomic_write_text
        build_mihomo_config dump_mihomo_config get_mihomo_config_status
        managed_runtime_operation_blocked mihomo_runtime_satisfies_routing
        promote_mihomo_candidate_config reconcile_mihomo_runtime
        reconcile_mihomo_selective_default_fast resolve_mihomo_runtime_proxy_rows
        resolve_runtime_proxy_rows restart_mihomo_container subject_selector_name
        validate_and_promote_mihomo_candidate_config validate_mihomo_candidate_config
        write_mihomo_candidate_config write_operational_log write_technical_log yaml
        """.split()
    )
    actual_star_exports = {
        name for name in vars(mihomo_config) if not name.startswith("_")
    }
    assert actual_star_exports == expected_star_exports, {
        "missing": sorted(expected_star_exports - actual_star_exports),
        "extra": sorted(actual_star_exports - expected_star_exports),
    }

    assert mihomo_config.build_mihomo_config is mihomo_config_builder.build_mihomo_config
    assert mihomo_config.write_mihomo_candidate_config is mihomo_config_builder.write_mihomo_candidate_config
    assert mihomo_config.validate_mihomo_candidate_config is mihomo_config_builder.validate_mihomo_candidate_config
    assert mihomo_config.promote_mihomo_candidate_config is mihomo_reconcile.promote_mihomo_candidate_config
    assert mihomo_config.reconcile_mihomo_runtime is mihomo_reconcile.reconcile_mihomo_runtime

    original = mihomo_reconcile._resolved_base_config_path

    def replacement() -> str:
        return "/tmp/mihomo-compat-alias.yaml"

    with monkeypatch.context() as patch:
        patch.setattr(mihomo_config, "_resolved_base_config_path", replacement)
        assert mihomo_config._resolved_base_config_path is replacement
        assert mihomo_reconcile.config._resolved_base_config_path is replacement
        assert mihomo_reconcile._resolved_base_config_path is replacement
    assert mihomo_config._resolved_base_config_path is original
    assert mihomo_reconcile.config._resolved_base_config_path is original

    def replacement_view() -> str:
        return "/tmp/mihomo-view-alias.yaml"

    with monkeypatch.context() as patch:
        patch.setattr(mihomo_reconcile.config, "_resolved_base_config_path", replacement_view)
        assert mihomo_config._resolved_base_config_path is replacement_view
        assert mihomo_reconcile._resolved_base_config_path is replacement_view
    assert mihomo_config._resolved_base_config_path is original
    assert mihomo_reconcile._resolved_base_config_path is original


def test_legacy_inventory_resolver_patch_is_scoped_to_inventory_consumer(monkeypatch) -> None:
    from fwrouter_api.services import mihomo_config_proxies

    unrelated_proxy_resolver = mihomo_config_proxies.resolve_mihomo_runtime_proxy_rows
    inventory_resolver = mihomo_config_proxies._resolve_config_inventory_rows
    rows = [{"raw": {"name": "inventory-proxy"}}]

    with monkeypatch.context() as patch:
        patch.setattr(
            mihomo_config,
            "resolve_mihomo_runtime_proxy_rows",
            lambda **_kwargs: rows,
        )
        assert mihomo_config_proxies._resolve_config_inventory_rows is not inventory_resolver
        assert mihomo_config_proxies.resolve_mihomo_runtime_proxy_rows is unrelated_proxy_resolver
        assert mihomo_config_proxies._resolve_config_inventory_rows(
            inventory_state="active", limit=1000,
        ) == rows
        assert mihomo_config_proxies._runtime_proxy_inventory_count() == 1
        assert mihomo_config._runtime_proxy_inventory_count() == 1

    assert mihomo_config_proxies._resolve_config_inventory_rows is inventory_resolver
    assert mihomo_config_proxies.resolve_mihomo_runtime_proxy_rows is unrelated_proxy_resolver


def test_legacy_inventory_count_patch_targets_validator_binding_only(monkeypatch) -> None:
    from fwrouter_api.services import mihomo_config_proxies, mihomo_config_validation

    validator_binding = mihomo_config_validation._runtime_proxy_inventory_count
    proxy_owner_function = mihomo_config_proxies._runtime_proxy_inventory_count

    def fake_inventory_count() -> int:
        return 7

    with monkeypatch.context() as patch:
        patch.setattr(mihomo_config, "_runtime_proxy_inventory_count", fake_inventory_count)
        assert mihomo_config._runtime_proxy_inventory_count is fake_inventory_count
        assert mihomo_config_validation._runtime_proxy_inventory_count is fake_inventory_count
        assert mihomo_config_proxies._runtime_proxy_inventory_count is proxy_owner_function

    assert mihomo_config._runtime_proxy_inventory_count is validator_binding
    assert mihomo_config_validation._runtime_proxy_inventory_count is validator_binding
    assert mihomo_config_proxies._runtime_proxy_inventory_count is proxy_owner_function


def test_staged_candidate_writer_uses_only_explicit_path_and_handoff_assignments(monkeypatch, tmp_path: Path) -> None:
    staged_path = tmp_path / "staged" / "config.yaml"
    active_candidate = tmp_path / "active" / "config.next.yaml"
    assignments = [{
        "listener_name": "xray-handoff-test",
        "listen": "127.0.0.1",
        "port": 5210,
        "selected_server_id": "server-test",
        "proxy": "runtime proxy name",
    }]
    calls: list[tuple[dict | None, list[dict] | None]] = []
    config = {"rules": [], "proxies": [], "proxy-groups": [], "fwrouter": {}}

    def build(_routing=None, *, xray_handoff_assignments=None):
        calls.append((_routing, xray_handoff_assignments))
        return config

    monkeypatch.setattr(mihomo_config, "build_mihomo_config", build)
    monkeypatch.setattr(mihomo_config, "_resolved_candidate_config_path", lambda: active_candidate)
    monkeypatch.setattr(mihomo_config, "write_technical_log", lambda **_kwargs: None)

    result = mihomo_config.write_mihomo_candidate_config(
        candidate_path=staged_path,
        xray_handoff_assignments=assignments,
    )

    assert Path(result["candidate_path"]) == staged_path
    assert staged_path.is_file()
    assert not active_candidate.exists()
    assert calls == [(None, assignments)]


def test_candidate_validator_reads_explicit_candidate_path(monkeypatch, tmp_path: Path) -> None:
    candidate_path = tmp_path / "staged.yaml"
    candidate_path.write_text("candidate", encoding="utf-8")
    observed: list[str] = []
    structural = {
        "allow_lan_enabled": True,
        "routing_mark_value": 512,
        "expected_routing_mark_value": 512,
        "legacy_inbound_keys_present": [],
        "mixed_listener_count": 1,
        "mixed_listener_bind": "127.0.0.1",
        "mixed_listener_port": 5201,
        "mixed_listener_proxy": "vpn-global",
        "proxy_inventory_ok": True,
        "candidate_proxies_count": 1,
        "runtime_proxy_inventory_count": 1,
        "vpn_auto_present": True,
        "vpn_global_present": True,
        "vpn_global_has_vpn_auto": True,
        "fixed_global_membership_ok": True,
        "selector_groups_ok": True,
        "transparent_required": False,
        "transparent_listener_count": 0,
        "transparent_listener_present": False,
        "transparent_listener_bind_valid": True,
        "transparent_listener_bind": "0.0.0.0",
        "transparent_listener_port": 5203,
        "transparent_redir_port": 5202,
        "transparent_listener_proxy": "vpn-global",
        "transparent_rule_name": "fwrouter-transparent",
        "transparent_direct_proxy_ok": True,
        "transparent_inbound_rule": "",
        "transparent_inbound_rules": [],
        "transparent_inbound_rule_ok": True,
        "transparent_subrules_ok": True,
        "full_vpn_subrules_ok": True,
        "expected_full_vpn_subrules": ["MATCH,vpn-global", "NETWORK,udp,REJECT"],
        "transparent_final_match_rule": "MATCH,DIRECT",
        "expected_transparent_final_match_rule": "MATCH,DIRECT",
        "transparent_state_consistency_ok": True,
        "final_match_rule": "MATCH,DIRECT",
        "expected_final_match_rule": "MATCH,DIRECT",
        "state_consistency_ok": True,
        "xray_handoff_targets_missing": [],
    }
    monkeypatch.setattr(mihomo_config, "_safe_load_yaml", lambda _path: {})
    monkeypatch.setattr(mihomo_config, "_resolved_candidate_config_path", lambda: tmp_path / "default.yaml")
    monkeypatch.setattr(mihomo_config, "_resolved_selective_default", lambda _routing: "direct")
    monkeypatch.setattr(mihomo_config, "_validate_candidate_structure", lambda *_args, **_kwargs: structural)
    monkeypatch.setattr(
        mihomo_config,
        "_validate_candidate_with_binary",
        lambda path: observed.append(path) or {"ok": True, "stdout_tail": "", "stderr_tail": ""},
    )
    monkeypatch.setattr(mihomo_config, "write_technical_log", lambda **_kwargs: None)

    result = mihomo_config.validate_mihomo_candidate_config(candidate_path=candidate_path)

    assert result["ok"] is True
    assert observed == [str(candidate_path)]


def test_manual_candidate_promotion_rejects_stale_input_provenance(monkeypatch, tmp_path: Path) -> None:
    reconcile, current, _candidate, base = _prepare_manual_candidate_promotion(monkeypatch, tmp_path)
    base_before = base.read_bytes()
    current["fingerprint"] = {"hash": "a" * 64, "payload": {}}

    result = reconcile.promote_mihomo_candidate_config()

    assert result["ok"] is False
    assert result["error_code"] == "MIHOMO_CANDIDATE_PROVENANCE_UNVERIFIED"
    assert base.read_bytes() == base_before


def test_manual_candidate_promotion_accepts_current_provenance(monkeypatch, tmp_path: Path) -> None:
    reconcile, _current, candidate, base = _prepare_manual_candidate_promotion(monkeypatch, tmp_path)

    result = reconcile.promote_mihomo_candidate_config()

    assert result["ok"] is True
    assert base.read_bytes() == candidate.read_bytes()


def test_staged_candidate_rejects_changed_inputs_even_with_inventory_admission(monkeypatch, tmp_path: Path) -> None:
    import hashlib

    from fwrouter_api.services import mihomo_reconcile_fingerprint, xray_subscription_service

    staged = tmp_path / "staged" / "config.yaml"
    staged.parent.mkdir()
    staged.write_bytes(b"staged validated generation")
    base = tmp_path / "active" / "config.yaml"
    candidate = tmp_path / "active" / "config.next.yaml"
    captured = {"hash": "b" * 64, "payload": {}}
    calls = {"promote": 0, "restart": 0}

    monkeypatch.setattr(mihomo_config, "_resolved_base_config_path", lambda: str(base))
    monkeypatch.setattr(mihomo_config, "_resolved_candidate_config_path", lambda: str(candidate))
    monkeypatch.setattr(
        mihomo_reconcile_fingerprint, "current_mihomo_input_fingerprint",
        lambda *_args, **_kwargs: {"hash": "a" * 64, "payload": {}},
    )
    monkeypatch.setattr(
        mihomo_config, "promote_mihomo_candidate_config",
        lambda **_kwargs: calls.__setitem__("promote", calls["promote"] + 1) or {"ok": True},
    )
    monkeypatch.setattr(
        "fwrouter_api.services.mihomo_runtime.restart_mihomo_container",
        lambda **_kwargs: calls.__setitem__("restart", calls["restart"] + 1) or {"ok": True},
    )
    admission = object()

    result = xray_subscription_service._apply_staged_mihomo_candidate(
        str(staged), hashlib.sha256(staged.read_bytes()).hexdigest(),
        inventory_admission=admission,
        candidate_input_fingerprint=captured,
    )

    assert result["ok"] is False
    assert result["stage"] == "candidate_provenance"
    assert result["error_code"] == "MIHOMO_CANDIDATE_PROVENANCE_UNVERIFIED"
    assert result["last_good_retained"] is True
    assert calls == {"promote": 0, "restart": 0}
    assert not candidate.exists()
