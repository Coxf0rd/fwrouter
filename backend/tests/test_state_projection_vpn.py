from __future__ import annotations

from fwrouter_api.services.state_projection import build_vpn_state_projection
from fwrouter_api.services.state_snapshot import StateSnapshot


def test_vpn_projection_uses_canonical_runtime_identity(monkeypatch) -> None:
    snapshot = StateSnapshot()
    monkeypatch.setattr(snapshot, "module", lambda _name: {"module_name": "vpn", "desired_state": "enabled"})
    monkeypatch.setattr(
        snapshot,
        "routing_global_state",
        lambda: {"desired_mode": "vpn", "server_mode": "auto", "active_auto_server_id": "canonical-server"},
    )
    monkeypatch.setattr(
        snapshot,
        "mihomo_health",
        lambda: {"runtime_state": "running", "active_server_id": "mihomo-selector-target"},
    )
    monkeypatch.setattr(
        "fwrouter_api.services.state_projection._server_id_for_runtime_target",
        lambda target: "canonical-server" if target == "mihomo-selector-target" else target,
    )
    monkeypatch.setattr(
        "fwrouter_api.services.state_projection._read_server_runtime_summary_readonly",
        lambda server_id: {"server_id": server_id} if server_id else {},
    )

    vpn = build_vpn_state_projection(snapshot=snapshot)["vpn"]

    assert vpn["observation"]["evidence"]["server_health"]["active_matches_selected"] is True
    assert vpn["observation"]["evidence"]["server_health"]["active"] == {"server_id": "canonical-server"}
    assert vpn["effective"]["active_server_id"] == "mihomo-selector-target"
