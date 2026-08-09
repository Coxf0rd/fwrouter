from __future__ import annotations

from typing import Any

from fwrouter_api.adapters.mihomo import DEFAULT_MIHOMO_ADAPTER
from fwrouter_api.adapters.xray import DEFAULT_XRAY_ADAPTER
from fwrouter_api.services.live_probe_cache import get_live_probe_cache
from fwrouter_api.services.xray_runtime_state import (
    _load_xray_bindings_state,
    _module_state,
    _sync_xray_module_runtime_state,
    _xray_bindings_path,
    _xray_config_egress_summary,
)


def get_xray_status() -> dict[str, Any]:
    return get_live_probe_cache(
        "xray.status",
        ttl_seconds=2.0,
        loader=_get_xray_status_uncached,
    )


def _get_xray_status_uncached() -> dict[str, Any]:
    health = DEFAULT_XRAY_ADAPTER.health()
    details = dict(health.details)
    bindings_state = _load_xray_bindings_state()
    egress = _xray_config_egress_summary()
    module = _module_state("xray") or {
        "module_name": "xray",
        "desired_state": "disabled",
        "lifecycle_mode": "none",
        "runtime_state": "not_configured",
        "apply_state": "clean",
        "status_text": "Xray module state row is missing.",
        "error_code": "XRAY_MODULE_ROW_MISSING",
        "error_message": None,
        "updated_at": None,
    }

    clients_count = int(details.get("clients_count") or 0)
    bindings_count = int(bindings_state.get("bindings_count") or 0)
    applied_count = int(bindings_state.get("applied_count") or 0)

    required_handoff_ports = list(
        dict.fromkeys(
            int(handoff.get("port") or 0)
            for handoff in (bindings_state.get("handoff_listeners") or [])
            if handoff.get("port")
        )
    )

    listeners_missing = [
        port for port in required_handoff_ports
        if not DEFAULT_MIHOMO_ADAPTER.check_port(port, host="172.18.0.1")
    ]
    listeners_ready = len(required_handoff_ports) > 0 and not listeners_missing

    runtime_running = health.runtime_state.value == "running"
    module_enabled = str(module.get("desired_state") or "") == "enabled"
    traffic_available = bool(
        runtime_running
        and module_enabled
        and egress.get("traffic_available")
        and (not required_handoff_ports or listeners_ready)
    )

    verified_count = applied_count if listeners_ready else 0
    forced_vpn_ready = bool(
        traffic_available
        and clients_count > 0
        and bindings_count > 0
        and applied_count > 0
        and listeners_ready
    )

    message = health.message
    if not module_enabled:
        message = "Xray runtime may be running, but FWRouter Xray module is disabled."
    elif not bool(egress.get("traffic_available")):
        message = "Xray runtime is up, but Xray egress is not ready (missing outbounds)."
    elif required_handoff_ports and not listeners_ready:
        message = f"Xray runtime is up, but {len(listeners_missing)} required Mihomo egress ports are not listening yet."
    elif clients_count == 0:
        message = "Xray runtime and egress are ready, but no clients are configured."
    elif forced_vpn_ready:
        message = "Xray runtime and managed forced-VPN egress are ready."
    else:
        message = "Xray runtime has clients, but managed forced-VPN bindings are not fully applied or verified."

    module = _sync_xray_module_runtime_state(
        module=module,
        runtime_running=runtime_running,
        forced_vpn_ready=forced_vpn_ready,
        traffic_available=traffic_available,
        message=message,
    )

    return {
        "adapter": details.get("adapter", "xray"),
        "runtime_state": health.runtime_state.value,
        "message": message,
        "forced_vpn_ready": forced_vpn_ready,
        "traffic_available": traffic_available,
        "module": module,
        "egress": egress,
        "details": {
            **details,
            "forced_vpn_ready": forced_vpn_ready,
            "traffic_available": traffic_available,
            "listeners_ready": listeners_ready,
            "listeners_missing": sorted(listeners_missing),
            "egress": egress,
            "module": module,
            "bindings": {
                "bindings_count": bindings_count,
                "applied_count": applied_count,
                "verified_count": verified_count,
                "generated_at": bindings_state.get("generated_at"),
                "handoff_count": int(bindings_state.get("handoff_count") or 0),
                "handoff_listeners": bindings_state.get("handoff_listeners") or [],
                "state_path": str(_xray_bindings_path()),
            },
        },
    }
