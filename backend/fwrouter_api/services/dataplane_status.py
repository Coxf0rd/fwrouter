from __future__ import annotations

import json
import re
import subprocess
from typing import Any

from fwrouter_api.adapters.mihomo import MihomoHealth
from fwrouter_api.adapters.scripts import DEFAULT_SCRIPT_RUNNER, ScriptRunnerError
from fwrouter_api.core.config import get_settings
from fwrouter_api.services.core_bypass import get_core_bypass_state
from fwrouter_api.services.dataplane_global import (
    build_applied_runtime_enforcement,
    build_global_preflight,
    read_applied_manifest,
    read_effective_rules_artifact,
)
from fwrouter_api.services.dataplane_live import applied_nft_markers_match_live, probe_live_global_mode
from fwrouter_api.services.live_probe_cache import get_live_probe_cache
from fwrouter_api.services.servers import ensure_routing_global_state

DATAPLANE_CAPABILITY_NFT_OWNED_TABLE = "nft_owned_table"
DATAPLANE_CAPABILITY_GLOBAL_ENFORCEMENT = "global_policy_v1"
DATAPLANE_CAPABILITY_FULL_ENFORCEMENT = "full_enforcement"
ENFORCEMENT_LEVEL_OWNED_TABLE_READY = "owned_table_ready"
ENFORCEMENT_LEVEL_OWNED_TABLE_MISSING = "owned_table_missing"
ENFORCEMENT_LEVEL_BYPASS_DIRECT_SAFE = "bypass_direct_safe"
TRANSPARENT_COUNTER_PATTERNS = {
    "vpn_mark_tcp_selective": ("fwrouter_vpn", "fwrouter vpn mark tcp:"),
    "vpn_mark_udp_selective": ("fwrouter_vpn", "fwrouter vpn mark udp:"),
    "vpn_mark_tcp_full": ("fwrouter_vpn_full", "fwrouter vpn mark tcp:"),
    "vpn_mark_udp_full": ("fwrouter_vpn_full", "fwrouter vpn mark udp:"),
    "tproxy_handoff_tcp": ("prerouting", "fwrouter tproxy handoff tcp:"),
    "redirect_handoff_tcp_prerouting": ("prerouting_nat", "fwrouter redirect handoff tcp:"),
    "redirect_handoff_tcp_output": ("output_nat", "fwrouter redirect handoff tcp:"),
    "tproxy_handoff_udp": ("prerouting", "fwrouter tproxy handoff udp:"),
    "full_vpn_redirect_handoff_tcp_prerouting": ("prerouting_nat", "fwrouter full-vpn redirect handoff tcp:"),
    "full_vpn_redirect_handoff_tcp_output": ("output_nat", "fwrouter full-vpn redirect handoff tcp:"),
    "full_vpn_tproxy_handoff_udp": ("prerouting", "fwrouter full-vpn tproxy handoff udp:"),
}


def _runtime_check_paths() -> tuple[str | None, str | None]:
    settings = get_settings()
    applied_manifest_path = settings.paths.generated_dir / "dataplane" / "applied-manifest.json"
    applied_nft_path = settings.paths.generated_dir / "dataplane" / "applied.nft"
    candidate_manifest_path = settings.paths.generated_dir / "dataplane" / "candidate-manifest.json"
    last_good_nft_path = settings.paths.state_dir / "last-good" / "dataplane" / "last-good.nft"
    candidate_nft_path = settings.paths.generated_dir / "dataplane" / "candidate.nft"

    if applied_manifest_path.exists():
        generated_path = str(applied_nft_path) if applied_nft_path.exists() else None
        return generated_path, str(applied_manifest_path)

    if candidate_manifest_path.exists():
        generated_path = str(candidate_nft_path) if candidate_nft_path.exists() else None
        return generated_path, str(candidate_manifest_path)

    return None, None


def _read_live_dataplane_payload() -> dict[str, Any] | None:
    generated_path, manifest_path = _runtime_check_paths()
    if not manifest_path:
        return None

    extra_args: list[str] = []
    extra_args.append(generated_path or "")
    extra_args.append(manifest_path)

    try:
        script_result = DEFAULT_SCRIPT_RUNNER.run("dataplane_check", extra_args=extra_args)
    except ScriptRunnerError:
        return None

    try:
        payload = json.loads(script_result.stdout or "{}")
    except json.JSONDecodeError:
        return None

    if not isinstance(payload, dict):
        return None
    payload["transparent_path"] = inspect_transparent_path_counters()
    applied_nft_path = get_settings().paths.generated_dir / "dataplane" / "applied.nft"
    payload["artifact_consistency"] = applied_nft_markers_match_live(applied_nft_path)
    if not bool(payload["artifact_consistency"].get("ok", True)):
        payload["ok"] = False
        payload["error_code"] = "LIVE_DATAPLANE_ARTIFACT_DRIFT"
        payload["message"] = "Live nftables table is missing markers from applied.nft."
    return payload


def inspect_transparent_path_counters() -> dict[str, Any]:
    packet_pattern = re.compile(r"counter packets (\d+) bytes (\d+)")
    results: dict[str, dict[str, int]] = {}
    chain_outputs: dict[str, str | None] = {}

    for chain in {chain for chain, _comment_prefix in TRANSPARENT_COUNTER_PATTERNS.values()}:
        try:
            completed = subprocess.run(
                ["nft", "-a", "-nn", "list", "chain", "inet", "fwrouter_v2", chain],
                check=True,
                capture_output=True,
                text=True,
            )
        except (FileNotFoundError, subprocess.CalledProcessError):
            chain_outputs[chain] = None
        else:
            chain_outputs[chain] = completed.stdout

    for key, (chain, comment_prefix) in TRANSPARENT_COUNTER_PATTERNS.items():
        packets = 0
        bytes_count = 0
        chain_output = chain_outputs.get(chain)
        if chain_output is None:
            results[key] = {"packets": 0, "bytes": 0}
            continue

        for line in chain_output.splitlines():
            if comment_prefix not in line:
                continue
            match = packet_pattern.search(line)
            if not match:
                continue
            packets = int(match.group(1))
            bytes_count = int(match.group(2))
            break

        results[key] = {"packets": packets, "bytes": bytes_count}

    vpn_mark_tcp_packets = (
        results["vpn_mark_tcp_selective"]["packets"]
        + results["vpn_mark_tcp_full"]["packets"]
    )
    vpn_mark_udp_packets = (
        results["vpn_mark_udp_selective"]["packets"]
        + results["vpn_mark_udp_full"]["packets"]
    )
    vpn_mark_packets = vpn_mark_tcp_packets + vpn_mark_udp_packets
    tproxy_handoff_tcp_packets = results["tproxy_handoff_tcp"]["packets"]
    redirect_handoff_tcp_packets = (
        results["redirect_handoff_tcp_prerouting"]["packets"]
        + results["redirect_handoff_tcp_output"]["packets"]
        + results["full_vpn_redirect_handoff_tcp_prerouting"]["packets"]
        + results["full_vpn_redirect_handoff_tcp_output"]["packets"]
    )
    transparent_tcp_handoff_packets = tproxy_handoff_tcp_packets + redirect_handoff_tcp_packets
    tproxy_handoff_udp_packets = (
        results["tproxy_handoff_udp"]["packets"]
        + results["full_vpn_tproxy_handoff_udp"]["packets"]
    )
    tproxy_handoff_packets = transparent_tcp_handoff_packets + tproxy_handoff_udp_packets
    mark_observed = vpn_mark_packets > 0
    transparent_tcp_flow_observed = transparent_tcp_handoff_packets > 0
    transparent_udp_flow_observed = tproxy_handoff_udp_packets > 0
    handoff_observed = transparent_tcp_flow_observed or transparent_udp_flow_observed

    failure_stage = None
    if vpn_mark_tcp_packets > 0 and not transparent_tcp_flow_observed:
        failure_stage = "vpn_mark_tcp_without_transparent_tcp_handoff"
    elif vpn_mark_udp_packets > 0 and not transparent_udp_flow_observed:
        failure_stage = "vpn_mark_udp_without_tproxy_handoff"

    return {
        **results,
        "vpn_mark_tcp": {
            "packets": vpn_mark_tcp_packets,
            "bytes": results["vpn_mark_tcp_selective"]["bytes"] + results["vpn_mark_tcp_full"]["bytes"],
        },
        "vpn_mark_udp": {
            "packets": vpn_mark_udp_packets,
            "bytes": results["vpn_mark_udp_selective"]["bytes"] + results["vpn_mark_udp_full"]["bytes"],
        },
        "vpn_mark_packets": vpn_mark_packets,
        "vpn_mark_tcp_packets": vpn_mark_tcp_packets,
        "vpn_mark_udp_packets": vpn_mark_udp_packets,
        "tproxy_handoff_tcp_packets": tproxy_handoff_tcp_packets,
        "redirect_handoff_tcp_packets": redirect_handoff_tcp_packets,
        "transparent_tcp_handoff_packets": transparent_tcp_handoff_packets,
        "tproxy_handoff_udp_packets": tproxy_handoff_udp_packets,
        "tproxy_handoff_packets": tproxy_handoff_packets,
        "mark_observed": mark_observed,
        "handoff_observed": handoff_observed,
        "transparent_tcp_flow_observed": transparent_tcp_flow_observed,
        "transparent_udp_flow_observed": transparent_udp_flow_observed,
        "transparent_flow_observed": handoff_observed,
        "failure_stage": failure_stage,
    }


def read_live_dataplane_payload() -> dict[str, Any] | None:
    """Return current dataplane_check payload for runtime-style probes."""

    return get_live_probe_cache(
        "dataplane_status.live_payload",
        ttl_seconds=2.0,
        loader=_read_live_dataplane_payload,
    )


def _live_owned_table_ready(payload: dict[str, Any] | None) -> bool:
    if not isinstance(payload, dict):
        return False
    if not bool(payload.get("table_exists")):
        return False
    required_chains = payload.get("required_chains")
    if not isinstance(required_chains, dict):
        return False
    if not all(bool(required_chains.get(chain)) for chain in required_chains):
        return False
    artifact_consistency = payload.get("artifact_consistency")
    if isinstance(artifact_consistency, dict) and not bool(artifact_consistency.get("ok", True)):
        return False
    return True


def _runtime_routing_state(applied_manifest: dict[str, Any] | None) -> dict[str, Any] | None:
    persisted_routing = ensure_routing_global_state()
    if isinstance(persisted_routing, dict):
        return persisted_routing

    if not isinstance(applied_manifest, dict):
        return None

    routing = applied_manifest.get("routing_global_state")
    if isinstance(routing, dict):
        return routing

    return None


def get_dataplane_capability() -> str:
    """Return current FWRouter dataplane capability level."""

    return str(build_runtime_enforcement_state()["dataplane_capability"])


def build_bypass_runtime_enforcement(
    *,
    preflight: dict[str, Any] | None = None,
    mihomo_health: MihomoHealth | None = None,
) -> dict[str, Any]:
    resolved_preflight = preflight or build_global_preflight(mihomo_health=mihomo_health)
    return {
        "dataplane_capability": DATAPLANE_CAPABILITY_NFT_OWNED_TABLE,
        "capability": DATAPLANE_CAPABILITY_NFT_OWNED_TABLE,
        "enforcement_level": ENFORCEMENT_LEVEL_BYPASS_DIRECT_SAFE,
        "traffic_enforcement_guaranteed": False,
        "supported_modes": {
            "direct": bool(resolved_preflight["can_enforce_global_direct"]),
            "selective": bool(resolved_preflight["can_enforce_global_selective"]),
            "vpn": bool(resolved_preflight["can_enforce_global_vpn"]),
        },
        "missing_runtime_requirements": [],
        "profile": resolved_preflight["profile"],
        "bypass_active": True,
    }


def build_runtime_enforcement_state(
    *,
    live_payload: dict[str, Any] | None = None,
    mihomo_health: MihomoHealth | None = None,
) -> dict[str, Any]:
    if live_payload is not None or mihomo_health is not None:
        return _build_runtime_enforcement_state_uncached(
            live_payload=live_payload,
            mihomo_health=mihomo_health,
        )

    return get_live_probe_cache(
        "dataplane_status.runtime_enforcement",
        ttl_seconds=2.0,
        loader=_build_runtime_enforcement_state_uncached,
    )


def _build_runtime_enforcement_state_uncached(
    *,
    live_payload: dict[str, Any] | None = None,
    mihomo_health: MihomoHealth | None = None,
) -> dict[str, Any]:
    bypass_state = get_core_bypass_state()
    if bool(bypass_state.get("enabled")):
        state = build_bypass_runtime_enforcement(
            preflight=build_global_preflight(mihomo_health=mihomo_health),
            mihomo_health=mihomo_health,
        )
        state["bypass"] = bypass_state
        return state

    resolved_live_payload = live_payload if live_payload is not None else _read_live_dataplane_payload()
    applied_manifest = read_applied_manifest()
    if isinstance(applied_manifest, dict) and _live_owned_table_ready(resolved_live_payload):
        routing = _runtime_routing_state(applied_manifest)
        if isinstance(routing, dict):
            live_mode_probe = probe_live_global_mode()
            effective_rules_artifact = ((applied_manifest.get("extra") or {}).get("rules_effective"))
            if not isinstance(effective_rules_artifact, dict):
                effective_rules_artifact = read_effective_rules_artifact()
            live_preflight = build_global_preflight(
                routing=routing,
                check_details=resolved_live_payload,
                mihomo_health=mihomo_health,
                effective_rules_artifact=(
                    effective_rules_artifact
                    if isinstance(effective_rules_artifact, dict)
                    else None
                ),
            )
            return build_applied_runtime_enforcement(
                routing=routing,
                # Recompute preflight from the live runtime instead of trusting a
                # stale snapshot stored in the historical applied manifest.
                preflight=live_preflight,
                live_mode_probe=live_mode_probe,
            )

    preflight = build_global_preflight(mihomo_health=mihomo_health)
    missing_runtime_requirements = list(preflight["missing"])
    if not _live_owned_table_ready(resolved_live_payload):
        missing_runtime_requirements.append("live_owned_table_missing")
    return {
        "dataplane_capability": DATAPLANE_CAPABILITY_NFT_OWNED_TABLE,
        "capability": DATAPLANE_CAPABILITY_NFT_OWNED_TABLE,
        "enforcement_level": (
            ENFORCEMENT_LEVEL_OWNED_TABLE_READY
            if _live_owned_table_ready(resolved_live_payload)
            else ENFORCEMENT_LEVEL_OWNED_TABLE_MISSING
        ),
        "traffic_enforcement_guaranteed": False,
        "supported_modes": {
            "direct": bool(preflight["can_enforce_global_direct"]),
            "selective": bool(preflight["can_enforce_global_selective"]),
            "vpn": bool(preflight["can_enforce_global_vpn"]),
        },
        "missing_runtime_requirements": missing_runtime_requirements,
        "profile": preflight["profile"],
        "bypass_active": False,
        "active_mode_matches_intent": False,
        "live_global_mode": None,
        "live_selective_default": None,
    }
