from fwrouter_api.services import subject_taxonomy as taxonomy


def _fake_external_ingress_provider_contracts() -> list[dict[str, object]]:
    return [
        {
            "provider": "provider-a",
            "module_concept": "provider-a",
            "subject_type": "external_network_client",
            "implementation_kind": "provider-a",
            "display_label": "Provider A",
            "identity_kind": "provider_ip",
            "location": "host",
            "integration_mode": "command_probe",
            "refresh_mode": "interval",
            "collector_config": {"script_id": "provider_a_status"},
        }
    ]


def _fake_explicit_external_client_provider_contracts() -> list[dict[str, object]]:
    return [
        {
            "provider": "provider-b",
            "subject_type": "explicit_external_client",
            "implementation_kind": "provider-b",
            "runtime_binding": "provider_b_runtime_bindings",
            "transparent_dataplane_policy": False,
            "virtual_vpn_auto_override": True,
        }
    ]


def test_generic_transparent_ingress_subjects_follow_global_mode() -> None:
    assert taxonomy.subject_follows_global_mode("lan")
    assert taxonomy.subject_follows_global_mode("external_network_client")
    assert not taxonomy.subject_follows_global_mode("explicit_external_client")


def test_explicit_external_client_contract_is_not_transparent_policy(monkeypatch) -> None:
    monkeypatch.setattr(
        taxonomy,
        "explicit_external_client_provider_contracts",
        _fake_explicit_external_client_provider_contracts,
    )

    assert taxonomy.is_explicit_external_client_subject_type("explicit_external_client")
    assert (
        taxonomy.explicit_external_client_runtime_binding("explicit_external_client")
        == "provider_b_runtime_bindings"
    )
    assert taxonomy.explicit_external_client_allows_virtual_vpn_auto("explicit_external_client")
    assert not taxonomy.subject_needs_transparent_policy("explicit_external_client")
    assert taxonomy.subject_needs_transparent_policy("lan")
    assert taxonomy.subject_needs_transparent_policy("external_network_client")


def test_legacy_provider_subject_names_normalize_to_generic_types() -> None:
    assert taxonomy.normalize_subject_type("tailscale_node") == "external_network_client"
    assert taxonomy.normalize_subject_type("tailscale") == "external_network_client"
    assert taxonomy.normalize_subject_type("xray") == "explicit_external_client"
    assert taxonomy.subject_follows_global_mode("tailscale_node")
    assert taxonomy.is_explicit_external_client_subject_type("xray")


def test_external_network_source_display_contract_is_taxonomy_derived(monkeypatch) -> None:
    monkeypatch.setattr(
        taxonomy,
        "external_ingress_provider_contracts",
        _fake_external_ingress_provider_contracts,
    )

    external_source = taxonomy.external_network_source_display_contract("external_network_client")
    assert external_source is not None
    assert external_source["system_id"] == "external-network-provider-a"
    assert external_source["label"] == "Provider A"
    assert external_source["integration_mode"] == "command_probe"
    assert external_source["collector_config"]["script_id"] == "provider_a_status"
    assert taxonomy.external_network_source_display_contract("unknown_external_source") is None


def test_watchdog_counter_prefixes_are_taxonomy_derived() -> None:
    prefixes = taxonomy.watchdog_nft_subject_counter_prefixes()
    assert "lan_" in prefixes
    assert "external_network_client_" in prefixes
    assert "host_" in prefixes
    assert "docker_" in prefixes
    assert "tailscale_node_" not in prefixes
    assert "tailscale_" not in prefixes
    assert "xray_" not in prefixes
