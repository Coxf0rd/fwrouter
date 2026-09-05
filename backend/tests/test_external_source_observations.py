from __future__ import annotations

from fwrouter_api.services.external_source_observations import external_source_observations_from_payload


def test_external_source_observations_map_provider_local_identity_and_peers() -> None:
    payload = {
        "Self": {
            "ID": "15",
            "HostName": "minis",
            "DNSName": "minisk.vpn.example",
            "TailscaleIPs": ["100.64.0.12"],
            "Online": True,
        },
        "Peer": {
            "18": {
                "ID": "18",
                "HostName": "peer-18",
                "DNSName": "peer-18.vpn.example",
                "TailscaleIPs": ["100.64.0.18"],
                "Online": True,
            },
            "30": {
                "ID": "30",
                "HostName": "peer-30",
                "DNSName": "peer-30.vpn.example",
                "TailscaleIPs": ["100.64.0.30"],
                "Online": False,
            },
        },
    }

    observations = external_source_observations_from_payload(
        "tailscale",
        payload,
        observed_at="2026-09-05T13:00:00Z",
    )

    assert observations["local_identities"][0]["external_id"] == "15"
    assert observations["local_identities"][0]["is_local_identity"] is True
    assert observations["local_identities"][0]["subject_id"] is None
    assert observations["by_subject_id"]["tailscale-node:18"]["presence"] == "online"
    assert observations["by_subject_id"]["tailscale-node:30"]["presence"] == "offline"
