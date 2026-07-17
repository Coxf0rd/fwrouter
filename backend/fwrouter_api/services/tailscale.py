from __future__ import annotations

import json
from typing import Any

from fwrouter_api.adapters.scripts import DEFAULT_SCRIPT_RUNNER, ScriptRunnerError
from fwrouter_api.services.live_probe_cache import get_live_probe_cache


def _peer_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    peers_value = payload.get("Peer") or payload.get("Peers") or {}
    if isinstance(peers_value, dict):
        return [item for item in peers_value.values() if isinstance(item, dict)]
    if isinstance(peers_value, list):
        return [item for item in peers_value if isinstance(item, dict)]
    return []


def _has_routing_hint(peer: dict[str, Any]) -> bool:
    return bool(
        peer.get("through_fwrouter")
        or peer.get("fwrouter_routed")
        or peer.get("routed_via_server")
        or peer.get("UsesExitNode")
        or peer.get("ExitNode")
        or peer.get("UsesThisServerAsExit")
    )


def _is_importable_peer(peer: dict[str, Any]) -> bool:
    if peer.get("Online") is False:
        return False

    if _has_routing_hint(peer):
        return True

    tail_addrs = peer.get("TailscaleIPs") or peer.get("Addresses") or []
    return bool(isinstance(tail_addrs, list) and tail_addrs)


def probe_tailscale_runtime() -> dict[str, Any]:
    return get_live_probe_cache(
        "tailscale.runtime",
        ttl_seconds=5.0,
        loader=_probe_tailscale_runtime_uncached,
    )


def _probe_tailscale_runtime_uncached() -> dict[str, Any]:
    try:
        result = DEFAULT_SCRIPT_RUNNER.run("tailscale_status")
    except ScriptRunnerError as exc:
        return {
            "ok": False,
            "adapter": "allowlist",
            "script_id": "tailscale_status",
            "runtime_state": "not_configured",
            "message": str(exc),
            "error_code": "TAILSCALE_SCRIPT_ERROR",
            "error_message": str(exc),
            "details": {
                "script_available": False,
                "script_error": str(exc),
                "peers_visible_count": 0,
                "importable_peers_count": 0,
            },
        }

    if not result.ok:
        message = result.stderr.strip() or "tailscale_status failed."
        return {
            "ok": False,
            "adapter": "allowlist",
            "script_id": result.script_id,
            "runtime_state": "degraded",
            "message": message,
            "error_code": "TAILSCALE_STATUS_FAILED",
            "error_message": message,
            "details": {
                "script_available": True,
                "script_result": result.to_dict(),
                "peers_visible_count": 0,
                "importable_peers_count": 0,
            },
        }

    try:
        payload = json.loads(result.stdout) if result.stdout.strip() else {}
    except json.JSONDecodeError as exc:
        message = f"tailscale_status returned invalid JSON: {exc}"
        return {
            "ok": False,
            "adapter": "allowlist",
            "script_id": result.script_id,
            "runtime_state": "degraded",
            "message": message,
            "error_code": "TAILSCALE_STATUS_INVALID_JSON",
            "error_message": message,
            "details": {
                "script_available": True,
                "script_result": result.to_dict(),
                "json_error": str(exc),
                "peers_visible_count": 0,
                "importable_peers_count": 0,
            },
        }

    if not isinstance(payload, dict):
        payload = {}

    self_info = payload.get("Self") if isinstance(payload.get("Self"), dict) else {}
    peers = _peer_items(payload)
    importable_count = sum(1 for peer in peers if _is_importable_peer(peer))
    hostname = str(
        self_info.get("HostName")
        or self_info.get("DNSName")
        or self_info.get("Name")
        or ""
    ).strip()
    tailscale_ips = self_info.get("TailscaleIPs") or self_info.get("Addresses") or []

    return {
        "ok": True,
        "adapter": "allowlist",
        "script_id": result.script_id,
        "runtime_state": "running",
        "message": "Tailscale status is available through the allowlisted host probe.",
        "error_code": None,
        "error_message": None,
        "details": {
            "script_available": True,
            "script_result": result.to_dict(),
            "hostname": hostname or None,
            "online": bool(self_info.get("Online", True)),
            "backend_state": self_info.get("BackendState"),
            "tailscale_ips": tailscale_ips if isinstance(tailscale_ips, list) else [],
            "peers_visible_count": len(peers),
            "importable_peers_count": importable_count,
        },
    }


TAILSCALE_ACTION_TO_SCRIPT_ID = {
    "start": "tailscale_start",
    "stop": "tailscale_stop",
    "restart": "tailscale_restart",
}


def run_tailscale_lifecycle_action(action: str) -> dict[str, Any]:
    normalized_action = action.strip().lower()
    script_id = TAILSCALE_ACTION_TO_SCRIPT_ID.get(normalized_action)
    if script_id is None:
        return {
            "ok": False,
            "action": normalized_action,
            "error_code": "TAILSCALE_ACTION_INVALID",
            "error_message": "Allowed tailscale actions: start, stop, restart.",
            "runtime": probe_tailscale_runtime(),
        }

    try:
        result = DEFAULT_SCRIPT_RUNNER.run(script_id)
    except ScriptRunnerError as exc:
        return {
            "ok": False,
            "action": normalized_action,
            "error_code": "TAILSCALE_ACTION_RUNNER_ERROR",
            "error_message": str(exc),
            "runtime": probe_tailscale_runtime(),
        }

    runtime = probe_tailscale_runtime()
    return {
        "ok": result.ok,
        "action": normalized_action,
        "script_id": script_id,
        "script_result": result.to_dict(),
        "runtime": runtime,
        "error_code": None if result.ok else "TAILSCALE_ACTION_FAILED",
        "error_message": None if result.ok else (result.stderr.strip() or f"tailscale {normalized_action} failed."),
    }
