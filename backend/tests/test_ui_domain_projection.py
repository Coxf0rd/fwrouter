from fwrouter_api.services.ui_state_common import (
    _domain_category_for_inventory_role,
    _implementation_label_for_kind,
)


def test_inventory_roles_project_to_domain_categories() -> None:
    assert _domain_category_for_inventory_role("lan_client") == "local_client"
    assert _domain_category_for_inventory_role("vless_client") == "external_client"
    assert _domain_category_for_inventory_role("external_network_source") == "external_network_source"
    assert _domain_category_for_inventory_role("docker_runtime") == "service"
    assert _domain_category_for_inventory_role("host_runtime") == "service"
    assert _domain_category_for_inventory_role("router_core") == "infrastructure"


def test_implementation_labels_are_details_not_categories() -> None:
    assert _implementation_label_for_kind("xray") == "Xray/VLESS"
    assert _implementation_label_for_kind("tailscale") == "Tailscale"
    assert _implementation_label_for_kind("docker") == "Docker"
