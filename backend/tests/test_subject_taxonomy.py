from fwrouter_api.services.subject_taxonomy import (
    external_network_source_display_contract,
    explicit_external_client_allows_virtual_vpn_auto,
    explicit_external_client_runtime_binding,
    is_explicit_external_client_subject_type,
    subject_follows_global_mode,
    subject_needs_transparent_policy,
    transparent_ingress_contract,
    watchdog_nft_subject_counter_prefixes,
)


def test_transparent_ingress_subjects_follow_global_mode_with_legacy_alias() -> None:
    assert subject_follows_global_mode("lan")
    assert subject_follows_global_mode("tailscale_node")
    assert subject_follows_global_mode("tailscale")
    assert not subject_follows_global_mode("xray")


def test_explicit_external_client_contract_is_not_transparent_policy() -> None:
    assert is_explicit_external_client_subject_type("xray")
    assert explicit_external_client_runtime_binding("xray") == "xray_runtime_bindings"
    assert explicit_external_client_allows_virtual_vpn_auto("xray")
    assert not subject_needs_transparent_policy("xray")
    assert subject_needs_transparent_policy("lan")
    assert subject_needs_transparent_policy("tailscale_node")


def test_external_ingress_contract_exposes_provider_specific_matcher_data() -> None:
    tailscale = transparent_ingress_contract("tailscale_node")
    assert tailscale is not None
    assert tailscale["provider"] == "tailscale"
    assert tailscale["identity_kind"] == "tailscale_ip"
    assert tailscale["ingress_interface"] == "tailscale0"


def test_external_network_source_display_contract_is_taxonomy_derived() -> None:
    tailscale = external_network_source_display_contract("tailscale_node")
    assert tailscale is not None
    assert tailscale["system_id"] == "external-network-tailscale"
    assert tailscale["label"] == "Tailscale"
    assert tailscale["integration_mode"] == "command_probe"
    assert tailscale["collector_config"]["script_id"] == "tailscale_status"
    assert external_network_source_display_contract("unknown_external_source") is None


def test_watchdog_counter_prefixes_are_taxonomy_derived() -> None:
    prefixes = watchdog_nft_subject_counter_prefixes()
    assert "lan_" in prefixes
    assert "external_network_client_" in prefixes
    assert "host_" in prefixes
    assert "docker_" in prefixes
    assert "xray_" not in prefixes
