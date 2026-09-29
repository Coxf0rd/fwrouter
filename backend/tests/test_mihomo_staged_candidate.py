from __future__ import annotations

from pathlib import Path

from fwrouter_api.services import mihomo_config


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
