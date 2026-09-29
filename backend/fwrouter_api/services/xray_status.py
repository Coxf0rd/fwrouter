from __future__ import annotations

import json
from pathlib import Path
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


def _managed_identity_sets(
    bindings: list[dict[str, Any]], client_modes: list[dict[str, Any]], payload: dict[str, Any],
) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    from fwrouter_api.services.xray_materialize import _xray_config_sections

    prefixes = ("sub-", "vpn-auto-")
    expected = {
        (str(item.get("client_uuid") or item.get("client_id") or ""), str(item.get("client_email") or ""))
        for item in [*bindings, *client_modes]
        if str(item.get("client_email") or "").lower().startswith(prefixes)
    }
    inbounds, _, _, _ = _xray_config_sections(payload)
    actual: set[tuple[str, str]] = set()
    for inbound in inbounds:
        if not isinstance(inbound, dict) or str(inbound.get("tag") or "") != "vless-ws":
            continue
        settings = inbound.get("settings") if isinstance(inbound.get("settings"), dict) else {}
        for client in settings.get("clients", []) if isinstance(settings.get("clients"), list) else []:
            if not isinstance(client, dict):
                continue
            email = str(client.get("email") or "")
            if email.lower().startswith(prefixes):
                actual.add((str(client.get("id") or ""), email))
    return expected, actual


def _get_xray_status_uncached() -> dict[str, Any]:
    health = DEFAULT_XRAY_ADAPTER.health()
    details = dict(health.details)
    bindings_state = _load_xray_bindings_state()
    generation_checkpoint = Path(_xray_bindings_path()).parent / ".generation" / "generation-checkpoint.json"
    generation_pending = generation_checkpoint.exists()
    generation_phase = "pending"
    if generation_pending:
        try:
            checkpoint_data = json.loads(generation_checkpoint.read_text(encoding="utf-8"))
            generation_phase = str(checkpoint_data.get("phase") or "pending")
        except Exception:
            generation_phase = "recovery_required"
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
    applied_bindings = bindings_state.get("bindings") if isinstance(bindings_state.get("bindings"), list) else []
    applied_modes = bindings_state.get("client_modes") if isinstance(bindings_state.get("client_modes"), list) else []
    if applied_bindings or applied_modes:
        from fwrouter_api.services.xray_materialize import (
            _verify_active_config_bindings,
            _verify_active_config_client_modes,
        )

        binding_check = _verify_active_config_bindings(applied_bindings)
        mode_check = _verify_active_config_client_modes(applied_modes)
        from fwrouter_api.services.xray_materialize import _load_active_config_payload
        active_payload, _ = _load_active_config_payload()
        managed_identity_known = isinstance(active_payload, dict)
        if managed_identity_known:
            expected_managed, actual_managed = _managed_identity_sets(applied_bindings, applied_modes, active_payload)
        else:
            expected_managed, actual_managed = set(), set()
        managed_identity_parity = managed_identity_known and expected_managed == actual_managed
        generation_parity = {
            "status": (
                "unknown" if not managed_identity_known
                else "verified" if binding_check.get("ok") and mode_check.get("ok") and managed_identity_parity
                else "drift"
            ),
            "ok": (bool(binding_check.get("ok")) and bool(mode_check.get("ok")) and managed_identity_parity) if managed_identity_known else None,
            "verified_bindings_count": int(binding_check.get("verified_bindings_count") or 0),
            "missing_binding_count": len(binding_check.get("missing_clients") or []) + len(binding_check.get("missing_rules") or []),
            "verified_modes_count": int(mode_check.get("verified_client_modes_count") or 0),
            "missing_mode_count": len(mode_check.get("missing_clients") or []) + len(mode_check.get("missing_rules") or []),
            "expected_managed_identity_count": len(expected_managed),
            "actual_managed_identity_count": len(actual_managed) if managed_identity_known else None,
        }
    else:
        generation_parity = {"status": "unknown", "ok": None}

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
    if generation_parity.get("ok") is not True:
        forced_vpn_ready = False

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

    if generation_pending:
        forced_vpn_ready = False
        message = "Xray generation apply is pending verification or recovery."
    elif generation_parity.get("ok") is False:
        message = "Xray managed client bindings have runtime drift and require reconciliation."

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
                "generation": {
                    "pending": generation_pending,
                    "phase": generation_phase if generation_pending else None,
                    "parity": generation_parity,
                },
            },
        },
    }
