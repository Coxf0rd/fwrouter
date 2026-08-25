from __future__ import annotations

from typing import Any

from fwrouter_api.services.dataplane_global import read_effective_rules_artifact
from fwrouter_api.services.dataplane_nft_artifacts import get_dataplane_artifact_paths, promote_last_good
from fwrouter_api.services.dataplane_nft_artifacts import write_candidate_artifacts as _write_candidate_artifacts
from fwrouter_api.services.dataplane_nft_chains import (
    _as_nft_port,
    _build_classify_chain_lines,
    _build_disabled_forward_guard_lines,
    _build_disabled_input_guard_lines,
    _build_disabled_output_guard_lines,
    _build_output_entry_chain_lines,
    _build_output_nat_chain_lines,
    _build_prerouting_entry_chain_lines,
    _build_prerouting_nat_chain_lines,
    _build_terminal_direct_chain_lines,
    _build_terminal_vpn_chain_lines,
    _build_vpn_mark_chain_lines,
    _external_ingress_immunity_lines,
    _format_nft_ports,
    _is_disabled_subject,
    _nft_port_match_value,
    _reverse_ip_expr,
)
from fwrouter_api.services.dataplane_nft_constants import (
    CONTROL_PLANE_INPUT_PORTS,
    OWNED_TABLE,
    REQUIRED_CHAINS,
    ROOT_UID,
    STATIC_SECURE_DNS_BYPASS_IPV4,
    _derive_mark_hex,
    _derive_tcp_redirect_mark_hex,
    utc_timestamp,
)
from fwrouter_api.services.dataplane_nft_render import render_owned_table_candidate as _render_owned_table_candidate
from fwrouter_api.services.dataplane_nft_sets import (
    _build_scoped_vpn_sets,
    _chunk_elements,
    _read_manifest_extra_ipv4_list,
    _render_dns_runtime_set,
    _render_set,
    _resolve_lan_ingress_interfaces,
    _resolve_rules_effective_artifact,
    _resolve_tproxy_handoff_ipv4,
    _safe_set_suffix,
)


def render_owned_table_candidate(manifest: dict[str, Any] | None = None) -> str:
    return _render_owned_table_candidate(
        manifest,
        rules_effective_loader=read_effective_rules_artifact,
    )


def write_candidate_artifacts(
    *,
    job_id: str,
    apply_id: str,
    manifest: dict[str, Any],
) -> dict[str, str]:
    return _write_candidate_artifacts(
        job_id=job_id,
        apply_id=apply_id,
        manifest=manifest,
        renderer=render_owned_table_candidate,
    )
