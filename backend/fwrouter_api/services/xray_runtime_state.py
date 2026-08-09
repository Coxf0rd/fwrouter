from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fwrouter_api.adapters.xray import DEFAULT_XRAY_ADAPTER
from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import db_session


def _xray_bindings_path() -> Path:
    return get_settings().paths.state_dir / "xray" / "fwrouter-bindings.json"


def _load_xray_bindings_state() -> dict[str, Any]:
    path = _xray_bindings_path()
    if not path.exists():
        return {
            "bindings_version": 1,
            "generated_at": None,
            "bindings_count": 0,
            "applied_count": 0,
            "bindings": [],
            "handoff_count": 0,
            "handoff_listeners": [],
        }

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {
            "bindings_version": 1,
            "generated_at": None,
            "bindings_count": 0,
            "applied_count": 0,
            "bindings": [],
            "handoff_count": 0,
            "handoff_listeners": [],
            "error_code": "XRAY_BINDINGS_INVALID_JSON",
        }

    if not isinstance(payload, dict):
        return {
            "bindings_version": 1,
            "generated_at": None,
            "bindings_count": 0,
            "applied_count": 0,
            "bindings": [],
            "handoff_count": 0,
            "handoff_listeners": [],
            "error_code": "XRAY_BINDINGS_INVALID_SHAPE",
        }
    return payload


def _module_state(module_name: str) -> dict[str, Any] | None:
    with db_session() as connection:
        row = connection.execute(
            """
            SELECT module_name, desired_state, lifecycle_mode, runtime_state, apply_state,
                   status_text, error_code, error_message, updated_at
            FROM modules
            WHERE module_name = ?
            """,
            (module_name,),
        ).fetchone()

    return dict(row) if row is not None else None


def _xray_config_egress_summary() -> dict[str, Any]:
    config_path = Path(getattr(DEFAULT_XRAY_ADAPTER, "config_path", get_settings().paths.state_dir / "xray" / "config.json"))

    if not config_path.exists():
        return {
            "state": "missing_config",
            "traffic_available": False,
            "config_path": str(config_path),
            "outbounds_count": 0,
            "outbounds": [],
            "reason": "xray_config_missing",
        }

    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {
            "state": "invalid_config",
            "traffic_available": False,
            "config_path": str(config_path),
            "outbounds_count": 0,
            "outbounds": [],
            "reason": "xray_config_invalid_json",
            "error": {"line": exc.lineno, "column": exc.colno},
        }

    raw_outbounds = payload.get("outbounds") if isinstance(payload, dict) else []
    outbounds = [
        {
            "tag": outbound.get("tag"),
            "protocol": outbound.get("protocol"),
        }
        for outbound in (raw_outbounds if isinstance(raw_outbounds, list) else [])
        if isinstance(outbound, dict)
    ]

    if not outbounds:
        return {
            "state": "missing_outbound",
            "traffic_available": False,
            "config_path": str(config_path),
            "outbounds_count": 0,
            "outbounds": [],
            "reason": "xray_outbound_missing",
        }

    non_blackhole = [
        outbound
        for outbound in outbounds
        if str(outbound.get("protocol") or "").lower() != "blackhole"
    ]
    if not non_blackhole:
        return {
            "state": "blocked",
            "traffic_available": False,
            "config_path": str(config_path),
            "outbounds_count": len(outbounds),
            "outbounds": outbounds,
            "reason": "xray_outbound_blackhole",
        }

    return {
        "state": "configured",
        "traffic_available": True,
        "config_path": str(config_path),
        "outbounds_count": len(outbounds),
        "outbounds": outbounds,
        "reason": "xray_has_non_blackhole_outbound",
    }


def _is_xray_supported_server_config(raw: dict[str, Any] | None) -> tuple[bool, str]:
    if not isinstance(raw, dict):
        return False, "server_raw_missing"
    if str(raw.get("type") or "").lower() != "vless":
        return False, "server_type_not_vless"
    if str(raw.get("network") or "tcp").lower() not in {"tcp", "grpc"}:
        return False, "server_network_unsupported"
    if not bool(raw.get("tls")):
        return False, "server_tls_required"
    if not isinstance(raw.get("reality-opts"), dict):
        return False, "server_reality_required"
    if not str(raw.get("uuid") or "").strip():
        return False, "server_uuid_missing"
    if not str(raw.get("server") or "").strip():
        return False, "server_address_missing"
    if int(raw.get("port") or 0) <= 0:
        return False, "server_port_missing"
    if not str((raw.get("reality-opts") or {}).get("public-key") or "").strip():
        return False, "server_reality_public_key_missing"
    return True, "supported"


def _load_server_config_for_xray_binding(server_id: str | None) -> dict[str, Any] | None:
    normalized_server_id = str(server_id or "").strip()
    if not normalized_server_id:
        return None

    with db_session() as connection:
        row = connection.execute(
            """
            SELECT server_id, server_name, raw_json
            FROM servers
            WHERE server_id = ?
              AND inventory_state = 'active'
            """,
            (normalized_server_id,),
        ).fetchone()

    if row is None:
        return None

    try:
        raw = json.loads(row["raw_json"] or "{}")
    except json.JSONDecodeError:
        return None

    if not isinstance(raw, dict):
        return None

    return {
        "server_id": row["server_id"],
        "server_name": row["server_name"],
        "raw": raw,
    }


def _xray_materializable_egress_candidate() -> dict[str, Any]:
    from fwrouter_api.services.servers import ensure_routing_global_state

    routing = ensure_routing_global_state()
    server_mode = str(routing.get("server_mode") or "auto")
    selected_server_id = None
    selected_server_source = None

    fixed_server_id = routing.get("applied_fixed_server_id") or routing.get("desired_fixed_server_id")
    if server_mode == "fixed" and fixed_server_id:
        selected_server_id = str(fixed_server_id)
        selected_server_source = "global_fixed"
    elif routing.get("active_auto_server_id"):
        selected_server_id = str(routing.get("active_auto_server_id"))
        selected_server_source = "vpn_auto"

    if not selected_server_id:
        return {
            "ok": False,
            "reason": "selected_server_missing",
            "routing": {
                "server_mode": server_mode,
                "desired_mode": routing.get("desired_mode"),
                "applied_mode": routing.get("applied_mode"),
                "active_auto_server_id": routing.get("active_auto_server_id"),
            },
        }

    server_config = _load_server_config_for_xray_binding(selected_server_id)
    raw = server_config.get("raw") if isinstance(server_config, dict) else None
    supported, reason = _is_xray_supported_server_config(raw if isinstance(raw, dict) else None)

    return {
        "ok": supported,
        "reason": reason,
        "selected_server_id": selected_server_id,
        "selected_server_source": selected_server_source,
        "server_name": server_config.get("server_name") if isinstance(server_config, dict) else None,
        "server_shape": {
            "type": raw.get("type") if isinstance(raw, dict) else None,
            "network": raw.get("network") if isinstance(raw, dict) else None,
            "tls": raw.get("tls") if isinstance(raw, dict) else None,
            "has_reality_opts": isinstance(raw.get("reality-opts"), dict) if isinstance(raw, dict) else False,
            "has_uuid": bool(raw.get("uuid")) if isinstance(raw, dict) else False,
        },
    }


def _sync_xray_module_runtime_state(
    *,
    module: dict[str, Any],
    runtime_running: bool,
    forced_vpn_ready: bool,
    traffic_available: bool,
    message: str,
) -> dict[str, Any]:
    if str(module.get("desired_state") or "") != "enabled":
        return module

    runtime_state = "running" if runtime_running else "not_configured"
    apply_state = "clean" if forced_vpn_ready else "pending"

    if runtime_running and not traffic_available:
        runtime_state = "running"
        apply_state = "pending"

    current_runtime_state = str(module.get("runtime_state") or "")
    current_apply_state = str(module.get("apply_state") or "")
    current_status_text = str(module.get("status_text") or "")

    if (
        current_runtime_state != runtime_state
        or current_apply_state != apply_state
        or current_status_text != message
        or module.get("error_code") is not None
        or module.get("error_message") is not None
    ):
        with db_session() as connection:
            connection.execute(
                """
                UPDATE modules
                SET
                    runtime_state = ?,
                    apply_state = ?,
                    status_text = ?,
                    error_code = NULL,
                    error_message = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE module_name = 'xray'
                """,
                (runtime_state, apply_state, message),
            )

    updated = dict(module)
    updated["runtime_state"] = runtime_state
    updated["apply_state"] = apply_state
    updated["status_text"] = message
    updated["error_code"] = None
    updated["error_message"] = None
    return updated
