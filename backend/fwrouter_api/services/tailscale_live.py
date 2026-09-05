from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from fwrouter_api.adapters.scripts import DEFAULT_SCRIPT_RUNNER, ScriptRunnerError
from fwrouter_api.services.live_probe_cache import get_live_probe_cache


TAILSCALE_STATUS_SCRIPT_ID = "tailscale_status"


def _utc_timestamp() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _first_string(item: dict[str, Any], fields: tuple[str, ...]) -> str:
    for field in fields:
        value = item.get(field)
        if isinstance(value, list):
            value = value[0] if value else ""
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _first_list(item: dict[str, Any], fields: tuple[str, ...]) -> list[str]:
    for field in fields:
        value = item.get(field)
        if isinstance(value, list):
            return [str(entry) for entry in value if str(entry or "").strip()]
        if value not in (None, ""):
            return [str(value)]
    return []


def _node_observation(item: dict[str, Any], *, observed_at: str, is_self: bool) -> dict[str, Any]:
    node_id = _first_string(item, ("ID", "NodeID", "IDShort"))
    addresses = _first_list(item, ("TailscaleIPs", "Addresses"))
    hostname = _first_string(item, ("HostName", "DNSName", "Name"))
    dns_name = str(item.get("DNSName") or "").strip() or None
    return {
        "provider": "tailscale",
        "node_id": node_id,
        "subject_id": f"tailscale-node:{node_id}" if node_id else None,
        "hostname": hostname or None,
        "dns_name": dns_name,
        "ip_address": addresses[0] if addresses else None,
        "tailscale_ips": addresses,
        "online": bool(item.get("Online", False)),
        "observed_at": observed_at,
        "is_self": is_self,
        "classification": "infrastructure" if is_self else "external_network_source",
    }


def parse_tailscale_status_payload(payload: dict[str, Any], *, observed_at: str | None = None) -> dict[str, Any]:
    checked_at = observed_at or _utc_timestamp()
    self_info = payload.get("Self") if isinstance(payload.get("Self"), dict) else {}
    peers_value = payload.get("Peer")
    if not isinstance(peers_value, dict):
        peers_value = payload.get("Peers")
    if isinstance(peers_value, dict):
        peer_items = [item for item in peers_value.values() if isinstance(item, dict)]
    elif isinstance(peers_value, list):
        peer_items = [item for item in peers_value if isinstance(item, dict)]
    else:
        peer_items = []

    self_observation = _node_observation(self_info, observed_at=checked_at, is_self=True) if self_info else None
    peers = [
        observation
        for item in peer_items
        if (observation := _node_observation(item, observed_at=checked_at, is_self=False)).get("node_id")
    ]
    return {
        "ok": True,
        "provider": "tailscale",
        "observed_at": checked_at,
        "self": self_observation,
        "peers": peers,
        "peers_by_subject_id": {
            str(peer["subject_id"]): peer
            for peer in peers
            if peer.get("subject_id")
        },
    }


def read_tailscale_live_state() -> dict[str, Any]:
    observed_at = _utc_timestamp()
    try:
        result = DEFAULT_SCRIPT_RUNNER.run(TAILSCALE_STATUS_SCRIPT_ID)
    except ScriptRunnerError as exc:
        return {
            "ok": False,
            "provider": "tailscale",
            "observed_at": observed_at,
            "error_code": "TAILSCALE_STATUS_UNAVAILABLE",
            "error_message": str(exc),
            "self": None,
            "peers": [],
            "peers_by_subject_id": {},
        }
    if not result.ok:
        message = result.stderr.strip() or f"{TAILSCALE_STATUS_SCRIPT_ID} failed."
        return {
            "ok": False,
            "provider": "tailscale",
            "observed_at": observed_at,
            "error_code": "TAILSCALE_STATUS_FAILED",
            "error_message": message,
            "self": None,
            "peers": [],
            "peers_by_subject_id": {},
        }
    try:
        payload = json.loads(result.stdout) if result.stdout.strip() else {}
    except json.JSONDecodeError as exc:
        return {
            "ok": False,
            "provider": "tailscale",
            "observed_at": observed_at,
            "error_code": "TAILSCALE_STATUS_INVALID_JSON",
            "error_message": str(exc),
            "self": None,
            "peers": [],
            "peers_by_subject_id": {},
        }
    return parse_tailscale_status_payload(payload if isinstance(payload, dict) else {}, observed_at=observed_at)


def cached_tailscale_live_state(*, force_refresh: bool = False) -> dict[str, Any]:
    return get_live_probe_cache(
        "tailscale.live_status",
        ttl_seconds=5.0,
        loader=read_tailscale_live_state,
        force_refresh=force_refresh,
    )


__all__ = ["cached_tailscale_live_state", "parse_tailscale_status_payload", "read_tailscale_live_state"]
