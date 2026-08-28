from fwrouter_api.services.external_provider_registry import (
    explicit_external_client_provider_contracts,
    external_ingress_provider_contract,
)


def test_tailscale_provider_contract_stays_provider_specific() -> None:
    contract = external_ingress_provider_contract("tailscale")

    assert contract is not None
    assert contract["provider"] == "tailscale"
    assert contract["subject_type"] == "external_network_client"
    assert contract["identity_kind"] == "tailscale_ip"
    assert contract["ingress_interface"] == "tailscale0"
    assert contract["payload_source_cidr"] == "100.64.0.0/10"
    assert contract["collector_config"]["script_id"] == "tailscale_status"
    assert "subject_id_prefix" not in contract


def test_xray_provider_contract_stays_provider_specific() -> None:
    contracts = {
        str(contract["provider"]): contract
        for contract in explicit_external_client_provider_contracts()
    }

    assert contracts["xray"]["subject_type"] == "explicit_external_client"
    assert contracts["xray"]["implementation_kind"] == "xray"
    assert contracts["xray"]["runtime_binding"] == "xray_runtime_bindings"
