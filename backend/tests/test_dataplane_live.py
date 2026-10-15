from fwrouter_api.services import dataplane_live
from fwrouter_api.services.dataplane_live import (
    _classify_live_global_mode,
    live_mode_matches_intent,
)


def test_live_global_mode_classifies_normal_and_degraded_selective_defaults() -> None:
    normal_direct = '''
        chain fwrouter_classify {
            goto fwrouter_direct comment "global direct v1"
        }
    '''
    normal_vpn = '''
        chain fwrouter_classify {
            goto fwrouter_vpn_full comment "selective default vpn"
        }
    '''
    degraded_direct = '''
        chain fwrouter_classify {
            goto fwrouter_direct comment "selective degraded default direct"
        }
    '''

    assert _classify_live_global_mode(normal_vpn) == ("selective", "vpn")
    assert _classify_live_global_mode(degraded_direct) == ("selective", "direct")
    assert live_mode_matches_intent(
        expected_mode="selective", expected_selective_default="direct",
        probe={"ok": True, "mode": "selective", "selective_default": "direct"})
    assert not live_mode_matches_intent(
        expected_mode="selective", expected_selective_default="vpn",
        probe={"ok": True, "mode": "selective", "selective_default": "direct"})
    assert _classify_live_global_mode(normal_direct) == ("direct", None)
    assert live_mode_matches_intent(
        expected_mode="direct", probe={"ok": True, "mode": "direct"})
    assert _classify_live_global_mode(normal_vpn) == ("selective", "vpn")
    normal_vpn_mode = _classify_live_global_mode('''
        chain fwrouter_classify {
            goto fwrouter_vpn_full comment "global vpn v1"
        }
    ''')
    assert normal_vpn_mode == ("vpn", None)
    assert live_mode_matches_intent(
        expected_mode="vpn", probe={"ok": True, "mode": normal_vpn_mode[0]})
    normal_core_bypass = _classify_live_global_mode('''
        chain fwrouter_classify {
            goto fwrouter_core_bypass comment "fwrouter core bypass"
        }
    ''')
    assert normal_core_bypass == ("core_bypass", None)
    assert live_mode_matches_intent(
        expected_mode="core_bypass", probe={"ok": True, "mode": normal_core_bypass[0]})


def test_live_global_mode_keeps_conflicting_selective_defaults_ambiguous() -> None:
    conflicting = '''
        chain fwrouter_classify {
            goto fwrouter_vpn_full comment "selective default vpn"
            goto fwrouter_direct comment "selective degraded default direct"
        }
    '''

    probe_mode, selective_default = _classify_live_global_mode(conflicting)
    assert (probe_mode, selective_default) == ("unknown", None)
    assert not live_mode_matches_intent(
        expected_mode="selective",
        expected_selective_default="vpn",
        probe={"ok": True, "mode": probe_mode, "selective_default": selective_default},
    )
    assert not live_mode_matches_intent(
        expected_mode="selective",
        expected_selective_default="direct",
        probe={"ok": True, "mode": probe_mode, "selective_default": selective_default},
    )


def test_live_global_mode_does_not_infer_default_from_selective_marker_alone() -> None:
    marker_without_default = '''
        chain fwrouter_classify {
            goto fwrouter_direct comment "selective default unexpected"
        }
    '''

    probe_mode, selective_default = _classify_live_global_mode(marker_without_default)
    probe = {"ok": True, "mode": probe_mode, "selective_default": selective_default}
    assert (probe_mode, selective_default) == ("unknown", None)
    assert not live_mode_matches_intent(
        expected_mode="selective", expected_selective_default="vpn", probe=probe)
    assert not live_mode_matches_intent(
        expected_mode="selective", expected_selective_default="direct", probe=probe)


def test_applied_nft_markers_detect_stale_reserved_lan_ingress_rules(
    monkeypatch,
    tmp_path,
) -> None:
    applied = tmp_path / "applied.nft"
    applied.write_text(
        'table inet fwrouter_v2 { chain prerouting { '
        'comment "fwrouter tproxy handoff udp:5203"; } }\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        dataplane_live,
        "_read_live_table",
        lambda: {
            "ok": True,
            "raw_table": '''table inet fwrouter_v2 {
                chain prerouting {
                    comment "fwrouter tproxy handoff udp:5203"
                    comment "block IPv6 from LAN ingress rescue0"
                    comment "allow LAN DNS capture before VPN classify rescue0"
                }
            }''',
        },
    )

    result = dataplane_live.applied_nft_markers_match_live(applied)

    assert result["ok"] is False
    assert result["reason"] == "live_table_has_stale_lan_role_markers"
    assert result["missing_markers_count"] == 0
    assert result["stale_lan_role_markers"] == [
        "allow LAN DNS capture before VPN classify rescue0",
        "block IPv6 from LAN ingress rescue0",
    ]


def test_applied_nft_markers_ignore_current_lan_and_unrelated_rescue_comments(
    monkeypatch,
    tmp_path,
) -> None:
    applied = tmp_path / "applied.nft"
    applied.write_text(
        'table inet fwrouter_v2 { chain prerouting { '
        'comment "fwrouter tproxy handoff udp:5203"; } }\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        dataplane_live,
        "_read_live_table",
        lambda: {
            "ok": True,
            "raw_table": '''table inet fwrouter_v2 {
                chain prerouting {
                    comment "fwrouter tproxy handoff udp:5203"
                    comment "block IPv6 from LAN ingress enp2s0"
                    comment "allow LAN DNS capture before VPN classify enp2s0"
                    comment "rescue service marker"
                }
            }''',
        },
    )

    result = dataplane_live.applied_nft_markers_match_live(applied)

    assert result["ok"] is True
    assert result["stale_lan_role_markers_count"] == 0
    assert result["stale_lan_role_markers"] == []
